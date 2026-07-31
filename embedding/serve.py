"""
本地 BGE-M3 Embedding 微服务（方案 A：OpenAI 兼容 HTTP 接口）。

在 embedding/venv 内运行（依赖 sentence-transformers + torch，均已隔离在该 venv），
把本地 BGE-M3 模型包装成主应用可直接消费的 OpenAI 兼容 /embeddings 接口。
主应用侧无需任何改动：注册一个 provider_type=openai_compatible 的 Provider，
base_url 指向本服务即可（见 infrastructure/llm_provider.py 的 _do_embed）。

只用 Python 标准库 http.server，不引入 web 框架，保持 venv 零额外依赖。

启动：
    embedding/venv/Scripts/python.exe embedding/serve.py --port 8100

接口：
    GET  /health              -> {"status": "ready"|"loading", "model": ..., "dim": ...}
    POST /embeddings          -> OpenAI 兼容，请求 {"model","input": str|[str]}
    POST /v1/embeddings       -> 同上（兼容带 /v1 前缀的 base_url）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
# 离线加载：HF 缓存指向本地已下载的模型目录，禁止联网
os.environ.setdefault("HF_HOME", str(BASE_DIR / "models"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [emb-serve] %(levelname)s %(message)s")
logger = logging.getLogger("embedding.serve")

MODEL_NAME = "BAAI/bge-m3"

# ---- 全局模型状态（进程内单例，encode 加锁串行） ----
_model = None
_model_dim: int | None = None
_encode_lock = threading.Lock()


def _load_model() -> None:
    """加载 BGE-M3 到全局单例。有 CUDA 用 CUDA，否则回退 CPU。"""
    global _model, _model_dim
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("加载模型 %s（device=%s，HF_HOME=%s）...",
                MODEL_NAME, device, os.environ["HF_HOME"])
    model = SentenceTransformer(MODEL_NAME, device=device)
    _model = model
    # sentence-transformers 5.x 已将方法重命名，兼容新旧两种
    dim_fn = (getattr(model, "get_embedding_dimension", None)
              or model.get_sentence_embedding_dimension)
    _model_dim = int(dim_fn())
    logger.info("模型加载完成，向量维度=%d", _model_dim)


def _encode(texts: list[str]) -> list[list[float]]:
    """归一化编码（normalize_embeddings=True，与检索侧余弦语义一致）。"""
    with _encode_lock:
        vecs = _model.encode(texts, normalize_embeddings=True,
                             convert_to_numpy=True)
    return [v.astype("float32").tolist() for v in vecs]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认逐请求日志
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/health", "/v1/health"):
            ready = _model is not None
            self._send_json(200, {
                "status": "ready" if ready else "loading",
                "model": MODEL_NAME, "dim": _model_dim})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/embeddings", "/v1/embeddings"):
            self._send_json(404, {"error": {"message": "not found"}})
            return
        if _model is None:
            self._send_json(503, {"error": {"message": "模型加载中，请稍候"}})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw or b"{}")
        except (ValueError, OSError) as e:
            self._send_json(400, {"error": {"message": f"请求体解析失败：{e}"}})
            return

        model_id = req.get("model") or MODEL_NAME
        inp = req.get("input")
        if inp is None:
            self._send_json(400, {"error": {"message": "缺少 input 字段"}})
            return
        texts = [inp] if isinstance(inp, str) else list(inp)
        if not texts:
            self._send_json(400, {"error": {"message": "input 为空"}})
            return
        try:
            vecs = _encode(texts)
        except Exception as e:  # noqa: BLE001
            logger.exception("编码失败")
            self._send_json(500, {"error": {"message": f"编码失败：{e}"}})
            return

        self._send_json(200, {
            "object": "list",
            "model": model_id,
            "data": [{"object": "embedding", "index": i, "embedding": v}
                     for i, v in enumerate(vecs)],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        })


def main() -> None:
    parser = argparse.ArgumentParser(description="本地 BGE-M3 Embedding 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    _load_model()  # 服务开始监听前完成预热，之后 /health 即 ready
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("Embedding 服务已就绪：http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断，正在关闭 ...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
