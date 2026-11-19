"""单元（设计文档 §4「单元」）。

三个接口的分工：`GET` 是行数据、`outline` 是"这单元讲什么"（来自 plan.json，
W3 的 `studio.outline`）、`prepare` 是"把课件备好"（解析 + 备课，W3 的 `studio.materialize`）。

`graph` 是**旁路**：它只读（不写库、不建运行器、不推进状态机），把主线节点、预置路线与
当前进度指针一次给前端画「课堂走向图」—— 所以它会被轮询，形状错了比慢一点严重得多。

`prepare` 里那句 `mark_unit_prepared` 不能省：备课是**副作用在磁盘上**的事
（deck.json / plan.json），不落库的话下一次打开列表看到还是"未准备"，
学生会以为按钮没生效 —— 而它其实已经备好了。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, Response

from store import repo

from .. import deps
from ..errors import not_found

log = logging.getLogger("classroom.app")

router = APIRouter(prefix="/api/v1", tags=["units"])

#: 返回的字段（顺序即文档顺序）。
#: `slides` / `est_sec` 是**追加**的两个数值字段：前端拿它们显示"几页 / 讲多久"，
#: §4 的五个字段里没有它们 —— 少了前端只能再补一次 GET /units/{id}。
_PREPARE_FIELDS = ("ok", "deck_ready", "plan_ready", "plan_source", "degraded",
                   "warnings", "slides", "est_sec")


def _unit_or_404(conn, unit_id: str) -> dict:
    row = repo.get_unit(conn, unit_id)
    if row is None:
        raise not_found("这个单元")
    return row


@router.get("/units/{unit_id}")
def unit_detail(unit_id: str, profile_id: str = "", conn=Depends(deps.get_conn)) -> dict:
    row = _unit_or_404(conn, unit_id)
    pid = (profile_id or "").strip() or repo.active_profile_id(conn)
    return {
        "id": row["id"],
        "course_id": row["course_id"] or "",
        "title": row["title"] or "",
        "slides": int(row["slides"] or 0),
        "est_sec": round(float(row["est_sec"] or 0.0), 1),
        "ready": deps.unit_ready(row),
        "plan_source": row["plan_source"] or "",
        "progress": repo.unit_progress_view(conn, pid, row["id"]),
    }


@router.get("/units/{unit_id}/outline")
def unit_outline(unit_id: str, conn=Depends(deps.get_conn)) -> dict:
    row = _unit_or_404(conn, unit_id)
    return deps.build_outline(row)


@router.get("/units/{unit_id}/graph")
def unit_graph(unit_id: str, profile_id: str = "", conn=Depends(deps.get_conn)) -> dict:
    """课堂走向图：主线节点 + 预置路线 + 当前进度指针（只读，前端轮询它）。

    为什么惰性 import `studio.graph`：那一步会把课件装配（含状态机/模型依赖）拉起来，
    而单元列表与 `/api/health` 跟走向图一点关系都没有 —— 它们不该为这一条接口承担
    启动成本（deps._w3 对内容管线用的是同一条理由，这里只是换个方向）。
    """
    row = _unit_or_404(conn, unit_id)
    from studio import graph

    course, reason = graph.load_unit_course(row, deps.get_settings())
    payload = graph.build_unit_graph(
        course, live=_live_snapshot(), conn=conn,
        profile_id=(profile_id or "").strip(), unit_id=unit_id,
        title=str(row.get("title") or ""), reason=reason,
    )
    log.info("走向图 unit=%s ok=%s nodes=%s routes=%s live=%s",
             unit_id, payload["ok"], payload["totals"]["nodes"],
             payload["totals"]["routes"], payload["live"]["source"])
    return payload


def _live_snapshot() -> dict:
    """本进程正在跑的会话（LIVE 的浅拷贝）。

    拷贝而不是直接递过去：同步路由跑在线程池里，而 WS 协程随时在增删 LIVE。
    读不到就当一个都没在跑 —— 一张没有指针的图，比一个 500 好得多。
    """
    try:
        from .. import ws

        return dict(ws.LIVE)
    except Exception:
        return {}


@router.get("/units/{unit_id}/media/{name}")
def unit_media(unit_id: str, name: str, conn=Depends(deps.get_conn)) -> Response:
    """单元课件里的图片/媒体（page 帧的 `media_base` 指向这里）。

    设计文档 §4 漏了这条，但 16:9 重绘必须有它 —— 前端拿到的元素里有媒体文件名，
    没有取图的地址就画不出来。名字直接来自浏览器，所以**只允许单层文件名**：
    分隔符、`..`、点开头的隐藏文件一律 404，不做"试试看能不能解析"。
    """
    row = _unit_or_404(conn, unit_id)
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise not_found("这个文件")
    deck_dir = deps.deck_dir_of(row)
    if deck_dir is None:
        raise not_found("这个单元的课件")
    base = (Path(deck_dir) / "media").resolve()
    path = (base / name).resolve()
    if base != path and base not in path.parents:
        raise not_found("这个文件")
    if not path.is_file():
        raise not_found("这个文件")
    # Content-Type 由扩展名推断（FileResponse 的默认行为），不需要手写映射表
    return FileResponse(path)


@router.post("/units/{unit_id}/prepare")
def unit_prepare(unit_id: str, conn=Depends(deps.get_conn)) -> dict:
    row = _unit_or_404(conn, unit_id)
    result = deps.prepare_unit(row)
    if result.get("ok") or result.get("deck_ready"):
        repo.mark_unit_prepared(
            conn, unit_id,
            slides=result.get("slides"),
            est_sec=result.get("est_sec"),
            plan_source=result.get("plan_source") or "",
        )
    log.info("准备单元 unit=%s ok=%s deck=%s plan=%s warnings=%s",
             unit_id, result.get("ok"), result.get("deck_ready"),
             result.get("plan_ready"), len(result.get("warnings") or []))
    return {key: result.get(key) for key in _PREPARE_FIELDS}
