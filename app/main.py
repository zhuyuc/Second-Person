"""
FastAPI 应用入口（开发文档 §基础信息 / §八 错误码）。

- lifespan 启动/停机 AppContainer
- 统一响应封装 {code, message, trace_id, details}
- 挂载各域路由 + 静态前端
- SSE 流式 /chat/send
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from infrastructure.config_manager import ConfigError
from infrastructure.observability import get_trace_id, set_trace_id, setup_logging

from .container import AppContainer

logger = logging.getLogger("second_person.app")

_container: AppContainer | None = None


def get_container() -> AppContainer:
    assert _container is not None, "容器未初始化"
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

    @app.exception_handler(Exception)
    async def _err(request: Request, exc: Exception):
        logger.exception("未捕获异常")
        return JSONResponse(status_code=500, content={
            "code": 500, "message": str(exc), "trace_id": get_trace_id(),
            "details": None})

    # 路由注册
    from .routes import chat, memory, settings, soul, misc
    for mod in (chat, memory, settings, soul, misc):
        app.include_router(mod.router, prefix="/api")

    # 对话图片（用户消息携带的图片持久化目录，历史消息回看）
    img_dir = Path(data_dir) / "chat_images"
    img_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/chat-images", StaticFiles(directory=str(img_dir)),
              name="chat_images")

    # 静态前端（构建产物）
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir),
                  html=True), name="static")

    return app


def ok(data=None):
    return {"code": 200, "data": data}
