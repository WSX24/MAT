"""交互式课程生成 + 渐进式单课 PPT（设计文档 §4「生成」的新主线）。

两条纪律与 generations.py 一致：

1. **路由里不干活**：会话推进、后台任务、落盘、入库全在 ``course_builder``；
   HTTP 层只把 ``BuilderError`` 翻成 400/404/409 —— 状态码口径只有一处出处。
2. **GET 只读库**：页面 1 秒轮询一次，任何"顺手扫一下 runs"都会把轮询变成磁盘压力。

路径为什么都挂在 /course-builder 下
----------------------------------
它是**一条流程**（对话 → 确认 → 大纲 → 单元设计 → 入库），不是五个独立资源。
按流程挂在同一个前缀下，前端与排障都能一眼看出"这一步属于哪条线"。
唯一例外是渐进式 PPT 的三个口：它们属于**课程 / 单元**，用户是从课程页点进来的，
所以路径必须落在 /courses/{id}/ppt-plan 与 /units/{id}/ppt 上。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends

import course_builder

from .. import deps
from ..errors import ApiError

log = logging.getLogger("classroom.app")

router = APIRouter(prefix="/api/v1", tags=["course-builder"])

#: BuilderError.code → HTTP 状态（code 本身就是契约词表，别在这里另造一套）
_HTTP_STATUS = {"bad_request": 400, "not_found": 404, "conflict": 409}


def _http(exc: course_builder.BuilderError) -> ApiError:
    return ApiError(_HTTP_STATUS.get(exc.code, 400), exc.message, exc.code)


def _body(payload: dict | None) -> dict:
    return payload if isinstance(payload, dict) else {}


# ===========================================================================
# 会话
# ===========================================================================

@router.post("/course-builder/sessions", status_code=201)
async def create_session(payload: dict | None = Body(None),
                         conn=Depends(deps.get_conn)) -> dict:
    """第一句话进来 → 建会话 → 直接跑一轮模型（所以返回的已经是"模型在提问"的状态）。"""
    try:
        return await course_builder.create_session(conn, str(_body(payload).get("message") or ""))
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.get("/course-builder/sessions")
def list_sessions(limit: int = 20, conn=Depends(deps.get_conn)) -> dict:
    return course_builder.list_sessions(conn, limit)


@router.get("/course-builder/sessions/{session_id}")
def get_session(session_id: str, conn=Depends(deps.get_conn)) -> dict:
    try:
        return course_builder.get_session(conn, session_id)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.post("/course-builder/sessions/{session_id}/messages")
async def send_message(session_id: str, payload: dict | None = Body(None),
                       conn=Depends(deps.get_conn)) -> dict:
    try:
        return await course_builder.send_message(
            conn, session_id, str(_body(payload).get("text") or ""))
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.post("/course-builder/sessions/{session_id}/confirm-requirements")
async def confirm_requirements(session_id: str, payload: dict | None = Body(None),
                               conn=Depends(deps.get_conn)) -> dict:
    """用户对"整理出来的需求"表态。

    accepted=true  → 开始生成课程大纲（后台任务，靠 GET 轮询进度）
    accepted=false → 回到对话继续细化；feedback 会被当成用户的一句话记进去
    """
    body = _body(payload)
    accepted = body.get("accepted")
    try:
        return await course_builder.confirm_requirements(
            conn, session_id,
            accepted=bool(accepted),
            requirements=body.get("requirements") if isinstance(body.get("requirements"), dict) else None,
            feedback=str(body.get("feedback") or ""),
        )
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.post("/course-builder/sessions/{session_id}/outline")
def save_outline(session_id: str, payload: dict | None = Body(None),
                 conn=Depends(deps.get_conn)) -> dict:
    """保存用户手动编辑过的大纲（规整后入库；结构不可用直接 400）。"""
    body = _body(payload)
    outline = body.get("outline")
    if not isinstance(outline, dict):
        raise ApiError(400, "outline 必须是一个对象", "bad_request")
    try:
        return course_builder.save_outline(conn, session_id, outline)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.post("/course-builder/sessions/{session_id}/outline/confirm")
async def confirm_outline(session_id: str, payload: dict | None = Body(None),
                          conn=Depends(deps.get_conn)) -> dict:
    """用户确认大纲 → 生成每个单元的课程安排设计 → 存入课程库。

    可以在同一个请求里带上最终版 outline：前端改完直接确认，少一次往返，
    也避免"保存成功、确认时用的却是旧版"。
    """
    body = _body(payload)
    outline = body.get("outline") if isinstance(body.get("outline"), dict) else None
    try:
        return await course_builder.start_unit_designs(conn, session_id, outline)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.delete("/course-builder/sessions/{session_id}")
def delete_session(session_id: str, conn=Depends(deps.get_conn)) -> dict:
    try:
        return course_builder.delete_session(conn, session_id)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


# ===========================================================================
# 渐进式 PPT（按课生成）
# ===========================================================================

@router.get("/courses/{course_id}/ppt-plan")
def course_ppt_plan(course_id: str, conn=Depends(deps.get_conn)) -> dict:
    """"这门课哪些单元已经有 PPT、下一课是谁"。课程详情页与我的课堂都读它。"""
    try:
        return course_builder.ppt_plan(conn, course_id)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.post("/courses/{course_id}/ppt/next", status_code=202)
async def generate_next_ppt(course_id: str, conn=Depends(deps.get_conn)) -> dict:
    """「生成下一课 PPT」：顺序取第一个还没有 PPT 的单元。"""
    try:
        return await course_builder.start_next_ppt(conn, course_id)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.get("/units/{unit_id}/ppt")
def unit_ppt(unit_id: str, conn=Depends(deps.get_conn)) -> dict:
    try:
        return course_builder.get_unit_ppt(conn, unit_id)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.post("/units/{unit_id}/ppt", status_code=202)
async def generate_unit_ppt(unit_id: str, conn=Depends(deps.get_conn)) -> dict:
    """单独生成某一课的 PPT（用户直接点某个单元时用）。"""
    try:
        return await course_builder.start_unit_ppt(conn, unit_id)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc


@router.post("/units/{unit_id}/ppt/cancel")
async def cancel_unit_ppt(unit_id: str, conn=Depends(deps.get_conn)) -> dict:
    try:
        return await course_builder.cancel_unit_ppt(conn, unit_id)
    except course_builder.BuilderError as exc:
        raise _http(exc) from exc
