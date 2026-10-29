"""出片（设计文档 §4「生成」的 HTTP 面）。

两条纪律：

1. **路由里不干活**：发起、查询、取消都转发给 generation.py，HTTP 层只把
   GenerationError 翻译成 400/404/409 —— 状态码口径只能有一处出处；
2. **GET 只做一次 SELECT**：页面 1 秒轮询一次，任何"顺手扫一下 runs 打个总数"
   都会把轮询变成磁盘压力，而出片期间磁盘正是最忙的时候（渲染在写 PPTX）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends

import generation

from .. import deps
from ..errors import ApiError

log = logging.getLogger("classroom.app")

router = APIRouter(prefix="/api/v1", tags=["generations"])

#: GenerationError.code → HTTP 状态（code 本身就是契约词表，别在这里另造一套）
_HTTP_STATUS = {"bad_request": 400, "not_found": 404, "conflict": 409}


def _http(exc: generation.GenerationError) -> ApiError:
    return ApiError(_HTTP_STATUS.get(exc.code, 400), exc.message, exc.code)


#: /generations/config 必须排在 /generations/{gen_id} 之前：FastAPI 按声明顺序匹配，
#: 顺序反了 "config" 会被当成一个任务 id，页面拿到的是 404 而不是本机状态。
@router.get("/generations/config")
def generations_config() -> dict:
    """能不能出片、以及为什么（ready=false 时 reason 就是给用户看的那句话）。"""
    return generation.config_view()


@router.get("/generations")
def list_generations(limit: int = 10, conn=Depends(deps.get_conn)) -> dict:
    """最近的出片任务 + 当前正在跑的那一条（没有就是 null）。"""
    return generation.list_recent(conn, limit)


@router.post("/generations", status_code=202)
async def create_generation(payload: dict | None = Body(None),
                            conn=Depends(deps.get_conn)) -> dict:
    """发起一次出片。202 = 已受理（真正的进度靠 GET 轮询）。"""
    body = payload if isinstance(payload, dict) else {}
    try:
        return await generation.create(
            conn,
            goal=str(body.get("goal") or ""),
            stage=str(body.get("stage") or ""),
            subject=str(body.get("subject") or ""),
            term=str(body.get("term") or ""),
            level=str(body.get("level") or ""),
        )
    except generation.GenerationError as exc:
        raise _http(exc) from exc


@router.get("/generations/{gen_id}")
def get_generation(gen_id: str, conn=Depends(deps.get_conn)) -> dict:
    snap = generation.get(conn, gen_id)
    if snap is None:
        raise ApiError(404, "找不到这条出片任务", "not_found")
    return snap


@router.post("/generations/{gen_id}/cancel")
async def cancel_generation(gen_id: str, conn=Depends(deps.get_conn)) -> dict:
    """取消 = 杀掉整棵进程树。已经结束的任务不能再取消（409）。"""
    try:
        return await generation.cancel(conn, gen_id)
    except generation.GenerationError as exc:
        raise _http(exc) from exc
