"""课堂正式版 FastAPI 应用（设计文档 §4 的全部 HTTP / WS 契约）。

装配顺序是有意的：`/api/health` → 业务路由 → WS → **静态兜底最后**。
SPA 的 `/{path}` 兜底如果排在前面，任何写错的接口地址都会变成一张首页，
排查时看到的是 200 + HTML，而不是 404 —— 这条比"少一行 include"贵得多。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI

import course_builder
import generation

from . import deps, ws
from .errors import install_error_handlers
from .routes import (
    builder,
    courses,
    dev,
    generations,
    health,
    model_providers,
    profiles,
    sessions,
    static,
    units,
)

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


def _quiet_connection_resets(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """压掉"浏览器中途断开"的拆连接噪声，其它循环异常照旧交给默认处理器。

    实测现象：前端播放/拖动首页那段 5.7 MB 的 poster 视频时，Windows 的 Proactor 事件循环
    会往 stderr 刷一段

        Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
        ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

    根因在 CPython 自己：`_call_connection_lost` 对端已经 RST 的 socket 又要
    `shutdown(SHUT_RDWR)`，那一步没有 try（`Lib/asyncio/proactor_events.py`，源码里就写着
    "XXX ... maybe using DisconnectEx() would be better"）。它**不是本服务的错**，
    服务照常在跑（同一段日志里后面的 206 一直正常返回）。

    留着它的代价很实在：满屏堆栈会把真正的故障埋掉，看日志的人第一反应是"课堂坏了"。
    所以只按**异常类型**放行这一种，其余一个都不拦 —— 把循环异常一律吞掉，等于关掉
    "事件循环里有没人处理的异常"这条本来有用的信号（tests/test_acceptance_gaps.py
    正是拿它当断言的）。
    """
    if isinstance(context.get("exception"), ConnectionResetError):
        log.debug("连接被对端重置（浏览器中途断开，正常）")
        return
    loop.default_exception_handler(context)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 先装过滤器再自检：连接噪声从第一个请求就可能出现
    asyncio.get_running_loop().set_exception_handler(_quiet_connection_resets)
    deps.boot()
    # 出片对账：上一个进程留下的 queued/running 一律标成 interrupted。库里留一条"看着还在跑"
    # 的行不只是显示问题 —— 它还占着全局单并发的名额，出片按钮会从此点不动。
    try:
        generation.reconcile()
    except Exception as e:          # 库坏了也不许连累启动：健康检查得先活着
        log.error("出片对账失败 err=%s: %s", type(e).__name__, e)
    # 课程生成 / 单课 PPT 的对账同理：库里停在"运行中"的行会变成永远不动的进度条，
    # 而且占着"一次只做一课"的名额 —— 用户从此点不动「生成下一课」。
    try:
        course_builder.reconcile()
    except Exception as e:
        log.error("课程生成对账失败 err=%s: %s", type(e).__name__, e)
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
    api.include_router(generations.router)
    api.include_router(builder.router)           # 交互式课程生成 + 渐进式单课 PPT
    api.include_router(model_providers.router)  # 服务商中枢（明文 key 不出 HTTP）
    api.include_router(dev.router)              # 默认 404，见 routes/dev.py
    api.add_api_websocket_route("/ws/classroom/{session_id}", ws.classroom_ws)
    api.include_router(static.router)           # 必须最后
    return api


app = create_app()
