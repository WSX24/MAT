"""诊断接口（设计文档 §4「/dev」，`CLASSROOM_DEV=1` 才存在）。

默认关的时候**必须和"没有这个路由"一模一样**（404），不是 403、不是空数组：
"存在但没权限"本身就是一条信息，产品面不该有它。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends

from store import repo

from .. import deps
from ..errors import not_found

log = logging.getLogger("classroom.app")

router = APIRouter(prefix="/dev", tags=["dev"])


def require_dev() -> None:
    if not deps.dev_enabled():
        raise not_found("页面")


@router.get("/units/{unit_id}/plan", dependencies=[Depends(require_dev)])
def dev_plan(unit_id: str, conn=Depends(deps.get_conn)) -> dict:
    unit = repo.get_unit(conn, unit_id)
    if unit is None:
        raise not_found("这个单元")
    deck_id = deps.deck_id_of(unit)
    if not deck_id:
        raise not_found("这个单元的课件")
    try:
        from deck.store import plan_json

        path = plan_json(deck_id)
    except Exception as e:
        raise not_found(f"这个单元的 plan.json（{e}）")
    if not path.is_file():
        raise not_found("plan.json（这个单元还没备课）")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise not_found(f"读不出来的 plan.json（{e}）")
    return {"unit_id": unit_id, "deck_id": deck_id, "plan": plan}


@router.get("/sessions/{session_id}/events", dependencies=[Depends(require_dev)])
def dev_events(session_id: str, limit: int = 200, conn=Depends(deps.get_conn)) -> dict:
    if repo.get_session(conn, session_id) is None:
        raise not_found("这个会话")
    capped = max(1, min(int(limit or 200), repo.EVENTS_KEEP))
    return {
        "session_id": session_id,
        "count": repo.count_events(conn, session_id),
        "items": repo.list_events(conn, session_id, capped),
    }
