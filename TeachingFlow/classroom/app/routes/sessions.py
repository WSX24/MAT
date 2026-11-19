"""会话（设计文档 §4「会话」）。

三条产品口径都在这一个文件里落地：

* **续学**：`POST /sessions` 遇到同 (profile, unit) 还有 active/paused 的会话就复用它，
  返回 `resumed=true` 与上次位置。一个单元同时只该有一条"在上"的会话 ——
  否则学生每次刷新页面都会多一条新会话，课后小结里的时长就开始撒谎。
* **断开 = 暂停**：真正把状态置成 ended 的只有 `POST /sessions/{id}/end`
  与"课上完了"这两处（前者走这里，后者在运行器里）。
* **recap 可重放**：`GET /sessions/{id}/recap` 与 `POST /sessions/{id}/end` 返回同一份，
  且结束那一刻会把这份快照写一条 `recap` 事件 —— 服务重启后还能原样读回来，
  不会因为"运行器不在内存里了"就变成一堆 0。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from store import repo

from .. import deps, ws
from ..errors import bad_request, not_found

log = logging.getLogger("classroom.app")

router = APIRouter(prefix="/api/v1", tags=["sessions"])


class SessionIn(BaseModel):
    unit_id: str = ""
    profile_id: str = ""
    #: 大纲页的「从这里开始」：给了就从这一页开课。
    #: 类型写成 Any 是**故意的**：非法值要"忽略"而不是 422 ——
    #: 前端传个空字符串或越界页码时，学生该看到的是"课照上"，不是一条红色报错。
    start_slide: Any = None


def _clean_start_slide(value: Any, unit_row: dict) -> int | None:
    """把 start_slide 收成一个合法页码；不合法（空/非数字/越界）返回 None。"""
    if value is None or value == "":
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None
    slides = int(unit_row.get("slides") or 0)
    if slides and index >= slides:
        return None
    return index


def _position(row: dict, *, from_progress: bool | None = None) -> dict:
    """会话位置。`position_unit` 就是页内第几个讲解单元（= 状态机的 seg_index）。

    `from_progress` 只在 `POST /sessions` 的响应里出现：前端据此区分
    "从上次进度接上"和"从第一页开始"（小结页的文案不一样）。GET 会话不带它 ——
    一个会话自己的位置不可能是"来自进度"，说了反而要多解释一句。
    """
    position = {"slide": int(row.get("position_slide") or 0),
                "unit_index": int(row.get("position_unit") or 0)}
    if from_progress is not None:
        position["from_progress"] = bool(from_progress)
    return position


def _recap_from_db(conn, row: dict) -> dict:
    """按库里的数据拼 recap。

    优先读运行器写的 `recap` 事件（那是真数据：覆盖页数、讲过的单元、话题）；
    没有就按会话行推一个诚实的近似值，并说明未知项为 0 —— 编一个数字比显示 0 更坏。
    """
    event = repo.latest_event(conn, row["id"], "recap")
    if event and isinstance(event.get("payload"), dict) and event["payload"]:
        return event["payload"]
    unit = repo.get_unit(conn, row["unit_id"]) or {}
    slides_total = int(unit.get("slides") or 0)
    slide = int(row.get("position_slide") or 0)
    covered = min(slides_total, slide + 1) if slides_total else (slide + 1 if slide else 0)
    progress_ev = repo.latest_event(conn, row["id"], "progress") or {}
    return {
        "elapsed_sec": round(float(row.get("elapsed_sec") or 0.0), 1),
        "slides_covered": covered,
        "units_given": int((progress_ev.get("payload") or {}).get("units_given") or 0),
        "qa_count": int(row.get("qa_count") or 0),
        "topics": list((progress_ev.get("payload") or {}).get("topics") or []),
    }


def _write_progress(conn, row: dict, unit: dict) -> None:
    """把会话的最终位置写进 progress（续学的唯一依据）。

    percent 只增不减（`repo.upsert_progress` 用 MAX）：这里按"讲到第几页"估算，
    学生回看前面几页时不会把"学了多少"打回去。
    """
    slide = int(row.get("position_slide") or 0)
    seg = int(row.get("position_unit") or 0)
    total = int(unit.get("slides") or 0)
    percent = round(min(100.0, 100.0 * (slide + 1) / total), 1) if total else 0.0
    repo.upsert_progress(conn, row["profile_id"], row["unit_id"], percent=percent,
                         last_slide=slide, last_unit=seg,
                         study_sec=float(row.get("elapsed_sec") or 0.0))


@router.post("/sessions")
def create_session(body: SessionIn, conn=Depends(deps.get_conn)) -> dict:
    unit_id = (body.unit_id or "").strip()
    if not unit_id:
        raise bad_request("缺少 unit_id：开课必须指明是哪个单元")
    unit = repo.get_unit(conn, unit_id)
    if unit is None:
        raise not_found("这个单元")
    start = _clean_start_slide(body.start_slide, unit)
    pid = (body.profile_id or "").strip() or repo.active_profile_id(conn)
    if not pid:
        raise bad_request("还没有档案，请先创建一个档案再开课")
    if repo.get_profile(conn, pid) is None:
        raise not_found("这个档案")

    prev = repo.resumable_session(conn, pid, unit_id)
    if prev is not None:
        if start is not None and int(prev["position_slide"] or 0) != start:
            # 「从这里开始」= 把这条会话挪到那一页，而不是另开一条：
            # 另开的话一个单元会同时有两条"在上"的会话，课后时长会重复计。
            repo.update_session(conn, prev["id"], position_slide=start, position_unit=0)
            prev = repo.get_session(conn, prev["id"]) or prev
        log.info("复用会话 session=%s unit=%s profile=%s status=%s slide=%s",
                 prev["id"], unit_id, pid, prev["status"], prev["position_slide"])
        return {"session_id": prev["id"], "resumed": True,
                "position": _position(prev, from_progress=False)}

    # 没有未结束的会话 —— 但学生上一轮学到哪儿了，记在 progress 里（设计文档 §6：
    # 课程 → 单元 → 自学 → **进度与续学**）。下一轮必须从那儿接着上，而不是从第 1 页重来：
    # "每点一次单元就重听一遍"是自学产品最直接的劝退点。
    progress = repo.get_progress(conn, pid, unit_id) or {}
    if start is not None:
        start_slide, start_unit, from_progress = start, 0, False
    else:
        start_slide = max(0, int(progress.get("last_slide") or 0))
        start_unit = max(0, int(progress.get("last_unit") or 0))
        from_progress = start_slide > 0
    row = repo.create_session(conn, unit_id, pid, position_slide=start_slide,
                              position_unit=start_unit)
    log.info("新建会话 session=%s unit=%s profile=%s slide=%s from_progress=%s",
             row["id"], unit_id, pid, start_slide, from_progress)
    return {"session_id": row["id"], "resumed": from_progress,
            "position": _position(row, from_progress=from_progress)}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str, conn=Depends(deps.get_conn)) -> dict:
    row = repo.get_session(conn, session_id)
    if row is None:
        raise not_found("这个会话")
    try:
        comprehension = json.loads(row.get("comprehension") or "{}")
    except (TypeError, ValueError):
        comprehension = {}
    return {
        "id": row["id"],
        "unit_id": row["unit_id"] or "",
        "status": row["status"] or "",
        "started_at": row["started_at"] or "",
        "elapsed_sec": round(float(row["elapsed_sec"] or 0.0), 1),
        "position": _position(row),
        "comprehension": comprehension,
        "qa_count": int(row["qa_count"] or 0),
    }


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str, conn=Depends(deps.get_conn)) -> dict:
    row = repo.get_session(conn, session_id)
    if row is None:
        raise not_found("这个会话")
    recap = None
    runner = ws.get_runner(session_id)
    if runner is not None:
        # 运行器还在：让它自己收尾（它知道讲过哪些单元、答了几个问题）
        recap = await runner.finish()
    if recap is None:
        if row["status"] != "ended":
            repo.update_session(conn, session_id, status="ended",
                                ended_at=datetime.now().isoformat(timespec="seconds"))
        row = repo.get_session(conn, session_id) or row
        recap = _recap_from_db(conn, row)
        repo.add_event(conn, session_id, "recap", recap)
    # 结束这一刻**必须**把最终位置写进 progress，不能只靠运行期的节流写：
    # 断线、刷新、服务重启都会让最后一小节落在库里之外，而"下次从哪儿接着上"
    # 完全依赖这一行 —— 少写一次，学生就会从更早的位置重听。
    final = repo.get_session(conn, session_id) or row
    _write_progress(conn, final, repo.get_unit(conn, final["unit_id"]) or {})
    log.info("结束会话 session=%s slide=%s recap=%s",
             session_id, final["position_slide"], recap)
    return {"ok": True, "recap": recap}


@router.get("/sessions/{session_id}/recap")
def session_recap(session_id: str, conn=Depends(deps.get_conn)) -> dict:
    row = repo.get_session(conn, session_id)
    if row is None:
        raise not_found("这个会话")
    return _recap_from_db(conn, row)
