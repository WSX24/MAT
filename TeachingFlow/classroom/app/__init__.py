"""课堂正式版 FastAPI 应用（设计文档 §4 的全部 HTTP / WS 契约）。

装配顺序是有意的：`/api/health` → 业务路由 → WS → **静态兜底最后**。
SPA 的 `/{path}` 兜底如果排在前面，任何写错的接口地址都会变成一张首页，
排查时看到的是 200 + HTML，而不是 404 —— 这条比"少一行 include"贵得多。
"""

from __future__ import annotations

import contextlib
import logging
import os

from fastapi import FastAPI

from . import deps, ws
from .errors import install_error_handlers
from .routes import courses, dev, health, profiles, sessions, static, units

log = logging.getLogger("classroom.app")


def _setup_logging() -> None:
    """结构化日志（key=value 写法），只走 stderr。

    不 print 到 stdout 的理由很实在：uvicorn 把 stdout 当作访问日志通道，
    混进去的业务日志会和请求行交织成谁也读不懂的东西。
    """
    logger = logging.getLogger("classroom")
    if logger.handlers:
        return
    handler = logging.StreamHandler()          # 默认就是 stderr
    handler.setFormatter(logging.Formatter(
        "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("CLASSROOM_LOG_LEVEL", "INFO").upper())
    logger.propagate = False


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    deps.boot()
    yield
    log.info("课堂服务停止")


def create_app() -> FastAPI:
    _setup_logging()
    api = FastAPI(title="课堂正式版", version=deps.APP_VERSION, lifespan=_lifespan)
    install_error_handlers(api)
    api.include_router(health.router)          # /api/health（不带 v1 前缀）
    api.include_router(profiles.router)
    api.include_router(courses.router)
    api.include_router(units.router)
    api.include_router(sessions.router)
    api.include_router(dev.router)              # 默认 404，见 routes/dev.py
    api.add_api_websocket_route("/ws/classroom/{session_id}", ws.classroom_ws)
    api.include_router(static.router)           # 必须最后
    return api


app = create_app()
