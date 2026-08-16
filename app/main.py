"""
FastAPI 应用入口（开发文档 §基础信息 / §八 错误码）。

- lifespan 启动/停机 AppContainer
- 统一响应封装 {code, message, trace_id, details}
- 挂载各域路由 + 静态前端
- SSE 流式 /chat/send
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from infrastructure.config_manager import ConfigError
from infrastructure.observability import get_trace_id, set_trace_id, setup_logging

from .container import AppContainer

logger = logging.getLogger("second_person.app")

_container: AppContainer | None = None


def get_container() -> AppContainer:
    if _container is None:
        raise RuntimeError("容器未初始化")
    return _container


def create_app(data_dir: str | Path) -> FastAPI:
    global _container
    setup_logging("INFO")
    _container = AppContainer(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _container.startup()
        yield
        await _container.shutdown()

    app = FastAPI(title="Second Person", version="1.0.0", lifespan=lifespan)

    cors_origins = [
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def trace_mw(request: Request, call_next):
        set_trace_id()
        return await call_next(request)

    @app.exception_handler(ConfigError)
    async def _config_err(request: Request, exc: ConfigError):
        return JSONResponse(status_code=400, content={
            "code": 400, "message": str(exc), "trace_id": get_trace_id(),
            "details": {"field": exc.field, "received": exc.received,
                        "expected": exc.expected}})

    @app.exception_handler(HTTPException)
    async def _http_err(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={
            "code": exc.status_code, "message": str(exc.detail),
            "trace_id": get_trace_id(), "details": None})

    @app.exception_handler(RequestValidationError)
    async def _validation_err(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={
            "code": 422, "message": "请求参数校验失败",
            "trace_id": get_trace_id(), "details": exc.errors()})

    @app.exception_handler(KeyError)
    async def _key_err(request: Request, exc: KeyError):
        # 请求体缺必填字段（body["x"]）统一返 400，避免变成 500；
        # 同时记日志便于区分“缺字段”与内部逻辑 KeyError
        tid = get_trace_id()
        logger.warning("请求缺字段或 KeyError trace_id=%s key=%s", tid, exc)
        return JSONResponse(status_code=400, content={
            "code": 400, "message": f"请求缺少必填字段：{exc}",
            "trace_id": tid, "details": None})

    @app.exception_handler(Exception)
    async def _err(request: Request, exc: Exception):
        # 不向客户端回显 str(exc)（防泄露内部细节/路径/堆栈）；
        # 完整堆栈只写服务端日志，前端凭 trace_id 排查
        tid = get_trace_id()
        logger.exception("未捕获异常 trace_id=%s", tid)
        return JSONResponse(status_code=500, content={
            "code": 500, "message": "服务内部错误，请稍后重试",
            "trace_id": tid, "details": None})

    # 路由注册
    from .routes import chat, memory, settings, soul, misc, elicitations
    for mod in (chat, memory, settings, soul, misc, elicitations):
        app.include_router(mod.router, prefix="/api")

    # 对话图片（用户消息携带的图片持久化目录，历史消息回看）
    img_dir = Path(data_dir) / "chat_images"
    img_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/chat-images", StaticFiles(directory=str(img_dir)),
              name="chat_images")

    # 浏览器默认探测 /favicon.ico，统一重定向到 favicon.svg，避免 404 噪音
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return RedirectResponse(url="/favicon.svg")

    # 静态前端（构建产物）
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir),
                  html=True), name="static")

    return app
