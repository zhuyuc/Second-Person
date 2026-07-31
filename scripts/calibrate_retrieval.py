"""检索阈值校准脚本（离线运维工具，随时可重跑）。

用途：用真实数据测"正样本（引用事件）/ 噪声样本（未引用轮次）"的余弦分布，
决定是否值得启用高置信短路及其阈值——开关和阈值由数据说话，不拍脑袋。

正样本：citation_events 的"用户提问 → 被引用记忆"余弦（检索必须放行）
噪声样本：未产生引用的用户提问 → 全库 top-1 余弦（期望拦截的近似集，
          注意其中混有"应命中但未被引用"的样本，拦截率仅作参考下界）

运行：python scripts/calibrate_retrieval.py
输出：data/temp/calib_result.txt（明细 + 分位数 + 阈值扫描表）

注意：门 2（上下文线索）上线后分数分布会整体改变，改动检索线索构造后应重跑。
"""
import io
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from memory.vector_store import deserialize_vector  # noqa: E402

DB_PATH = ROOT / "data" / "palace.db"
OUT_PATH = ROOT / "data" / "temp" / "calib_result.txt"
NOISE_LIMIT = 200          # 噪声查询采样上限
QUERY_MAX_CHARS = 2000     # 与 Retriever.EMBED_QUERY_MAX_CHARS 对齐
THRESHOLDS = [0.50, 0.55, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70, 0.72, 0.75]


def _embed_endpoint(db) -> tuple[str, str]:
    """从 model_assignment/providers 读 embedding 服务地址与模型名。"""
    row = db.execute(
        "SELECT p.base_url, p.model_id FROM model_assignment a "
        "JOIN providers p ON p.id=a.provider_id "
        "WHERE a.task_type='embedding'").fetchone()
    if not row:
        raise SystemExit("未配置 embedding 模型槽位，无法校准")
    return row["base_url"].rstrip("/") + "/v1/embeddings", row["model_id"]


def _embed(url: str, model: str, texts: list[str]) -> list[list[float]]:
    vecs = []
    for i in range(0, len(texts), 16):
        req = urllib.request.Request(
            url, data=json.dumps({"model": model,
                                  "input": texts[i:i + 16]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        vecs.extend([d["embedding"] for d in data["data"]])
    return vecs


def main() -> int:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    embed_url, embed_model = _embed_endpoint(db)

    # 全库记忆向量矩阵（归一化）
    mem_ids, mem_vecs = [], []
    for r in db.execute(
            "SELECT memory_id, embedding FROM vectors WHERE embedding IS NOT NULL"):
        v = deserialize_vector(r["embedding"])
        if v is not None and len(v):
            mem_ids.append(r["memory_id"])
            mem_vecs.append(v)
    if not mem_ids:
        raise SystemExit("vectors 表为空，无法校准")
    mat = np.asarray(mem_vecs, dtype=np.float32)
    mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
    id2idx = {m: i for i, m in enumerate(mem_ids)}
    titles = {r["id"]: r["title"]
              for r in db.execute("SELECT id,title FROM memories")}

    def _last_user_msg(session_id: str, before_id: int):
        return db.execute(
            "SELECT content FROM conversations WHERE session_id=? AND id<? "
            "AND role='user' ORDER BY id DESC LIMIT 1",
            (session_id, before_id)).fetchone()

    # 正样本：引用事件 → 当时的用户提问
    pos = []
    for r in db.execute(
            "SELECT memory_id, message_id, session_id FROM citation_events"):
        u = _last_user_msg(r["session_id"], r["message_id"])
        if u and r["memory_id"] in id2idx:
            pos.append((u["content"][:QUERY_MAX_CHARS], r["memory_id"]))

    # 噪声样本：未产生引用的 assistant 轮次对应的用户提问
    cited_msgs = {r[0] for r in db.execute(
        "SELECT DISTINCT message_id FROM citation_events")}
    noise = []
    for r in db.execute(
            "SELECT id, session_id FROM conversations WHERE role='assistant' ORDER BY id"):
        if r["id"] in cited_msgs:
            continue
        u = _last_user_msg(r["session_id"], r["id"])
        if u and len(u["content"].strip()) >= 4:
            noise.append(u["content"][:QUERY_MAX_CHARS])
    noise = list(dict.fromkeys(noise))[:NOISE_LIMIT]

    qmat = np.asarray(_embed(embed_url, embed_model,
                             [q for q, _ in pos] + noise), dtype=np.float32)
    qmat = qmat / (np.linalg.norm(qmat, axis=1, keepdims=True) + 1e-8)
    sims = qmat @ mat.T

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = io.open(OUT_PATH, "w", encoding="utf-8")
    out.write(f"记忆向量数={len(mem_ids)} 正样本对={len(pos)} 噪声查询数={len(noise)}\n\n")

    pos_cited, pos_top1 = [], []
    out.write("==== 正样本（检索必须放行）====\n")
    for i, (q, mid) in enumerate(pos):
        row = sims[i]
        cs, t1 = float(row[id2idx[mid]]), float(row.max())
        rank = int((row > cs).sum()) + 1
        pos_cited.append(cs)
        pos_top1.append(t1)
        out.write(f"cited={cs:.3f} top1={t1:.3f} rank={rank} "
                  f"[{titles.get(mid, '?')[:20]}] Q: {q[:60]!r}\n")

    noise_top1 = []
    out.write("\n==== 噪声样本 top-1 分布 ====\n")
    for j, q in enumerate(noise):
        row = sims[len(pos) + j]
        t1, ti = float(row.max()), int(row.argmax())
        noise_top1.append(t1)
        out.write(
            f"top1={t1:.3f} [{titles.get(mem_ids[ti], '?')[:20]}] Q: {q[:60]!r}\n")

    def _stats(name, arr):
        a = np.asarray(arr)
        if not len(a):
            return
        ps = np.percentile(a, [5, 25, 50, 75, 95])
        out.write(f"{name}: n={len(a)} min={a.min():.3f} p5={ps[0]:.3f} "
                  f"p25={ps[1]:.3f} p50={ps[2]:.3f} p75={ps[3]:.3f} "
                  f"p95={ps[4]:.3f} max={a.max():.3f}\n")

    out.write("\n==== 统计 ====\n")
    _stats("正样本 cited 余弦 ", pos_cited)
    _stats("正样本 top1 余弦  ", pos_top1)
    _stats("噪声   top1 余弦  ", noise_top1)

    out.write("\n==== 阈值扫描（按 top1 判定）====\n")
    for thr in THRESHOLDS:
        pp = sum(1 for s in pos_top1 if s >= thr) / max(len(pos_top1), 1)
        bn = sum(1 for s in noise_top1 if s < thr) / max(len(noise_top1), 1)
        out.write(f"thr={thr:.2f}  正样本放行率={pp:5.1%}  噪声拦截率={bn:5.1%}\n")
    out.close()
    print("校准完成 ->", OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
