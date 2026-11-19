# -*- coding: utf-8 -*-
"""课堂走向图：把一节课摊成前端画得出来的一页 JSON（GET /units/{id}/graph）。

三份事实，一个都不新造
----------------------
① **主线节点** = `script.units` 的顺序。它同时就是 `Route.fork_node / merge_node` 用的
   那套下标（见 director/route.py），所以"图上第 n 格"与状态机"第 n 格"是同一件事。
   另起一套编号的代价是图上的指针指着 A、课堂在讲 B —— 这种错不会抛异常，
   只会在真实课堂上被学生看出来。
② **预置路线** = `script.routes`（plan.json 的 routes，load_course 已经逐条过 validate）。
   这里再 validate 一次不是重复劳动：它挡的是"课程数据结构在内存里被别处改过"这一档。
   一条脏路线只丢它自己，整页照旧 200 —— 前端在轮询它，一个 500 会让图纸整块变白。
③ **当前进度指针** = 本进程真在上的那节课（app.ws.LIVE）→ 库里最近一条会话 → 都没有。
   三档都只读：不建 runner、不推进状态机、不写库、不碰模型。

为什么装配写在这个文件里
------------------------
装配只有一条路（`studio/runner._build_course`：unit 行的 pptx → deck_id → replace(Settings)
→ load_course）。`load_unit_course` 逐行照抄它，连"deck.json 不存在就退回 pptx 让
load_course 现场解析"这条兜底也照抄 —— 另发明一套装配的下场是
"课堂讲到第 12 格，图上指着第 9 格"，而这种分叉只在真实课件上才现形。
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Mapping

#: 三种走向的中文名。**借用 director.route 里那张表**（Route.summary 用的同一张）：
#: 自己再抄一份的话，"跳讲"和"跳过"这类字眼迟早会在摘要与图例上分叉。
from director.route import _KIND_LABELS as KIND_LABELS
#: 卡面那句总结的上限（产品口径 ≤30 字）。从 state 借而不是再写一个 30：
#: 备课期截一次、图上再截一次，两处只要差一个字，同一句话就会在两边长得不一样。
from state import SUMMARY_MAX_CHARS

log = logging.getLogger("classroom.graph")

#: 详情面板的正文首段（与 studio.outline.PREVIEW_CHARS 同一个观感口径）
PREVIEW_CHARS = 60
#: 路线那一行中文摘要（Route.summary 的参数）
ROUTE_SUMMARY_CHARS = 40
#: hint 的上限：它要在图上占一行，超过一行就不再是"一句人话"
HINT_CHARS = 80
#: notes 一行的上限（降级原因会被拼进 hint）
NOTE_CHARS = 60

#: 理解信号 → 0~1 的理解度（图上给指针那一格上色用）。
#: 三档来自 state.Comprehension：confirmed 强信号 / vague 弱信号 / confused 强负信号。
#: vague 给 0.5 而不是 0：弱信号是"没确认"，不是"没懂"（COMPREHENSION_RANK 里它也排在
#: confirmed 之上），给 0 会把"没反应"和"明确说难"画成同一个颜色。null = 这一格还没有信号。
COMPREHENSION_SCORE = {"confirmed": 1.0, "vague": 0.5, "confused": 0.0}

#: notes 行首的进度标记（❌ / ⚠️ / ✅ / ℹ️ …）。它们是给命令行看的，不该出现在 hint 里。
_MARKS = re.compile(r"^[\s❌⚠️✅ℹ️·•—\-]+")


# ===========================================================================
# 装配：单元行 → Course（配方照抄 studio/runner._build_course）
# ===========================================================================
def load_unit_course(unit_row: dict, settings) -> tuple[Any, str]:
    """单元行 → (Course, "")；装不起来时 (None, 一句人话原因)。

    原因不抛出去而是变成 hint：走向图是**只读的旁路**，课件装不起来时前端要看到的
    是一句"先去点准备课件"，而不是一张 500。
    """
    if settings is None:
        return None, "服务还没读到可用的配置（.env 缺失或写坏了），课件装配不起来。"
    pptx = str((unit_row or {}).get("pptx_path") or "")
    if not pptx:
        return None, "这个单元还没登记课件文件。"
    deck_id, ready = "", False
    try:
        from deck.store import deck_json, make_deck_id

        deck_id = make_deck_id(pptx)
        ready = deck_json(deck_id).is_file()
    except Exception:
        deck_id, ready = "", False
    try:
        # deck_id 指向不存在的 deck.json 时 load_course 会退回示例课件，
        # 所以要像 runner 那样把 pptx_file 交回去让它现场解析真正的课件。
        replaced = dataclasses.replace(settings,
                                       deck_id=deck_id if ready else "",
                                       pptx_file="" if ready else pptx)
        from course import load_course

        return load_course(replaced), ""
    except Exception as e:
        log.warning("走向图装配课件失败 unit=%s: %s: %s",
                    (unit_row or {}).get("id"), type(e).__name__, e)
        return None, f"课件装配失败（{type(e).__name__}）。"


# ===========================================================================
# 载荷
# ===========================================================================
def build_unit_graph(course, *, live: Mapping[str, Any] | None = None, conn=None,
                     profile_id: str = "", unit_id: str = "", title: str = "",
                     reason: str = "") -> dict:
    """Course → 响应体。**绝不抛**：脏数据只丢它自己，整页照旧 200。"""
    script = getattr(course, "script", None) if course is not None else None
    mode = str(getattr(course, "mode", "") or "simple") if course is not None else "simple"
    nodes: list[dict] = []
    routes: list[dict] = []
    totals = {"nodes": 0, "routes": 0, "slides": 0, "est_sec": 0.0}
    hint = ""
    if script is not None and mode == "deck" and _has_plan(course):
        try:
            nodes = build_nodes(script)
            routes = build_routes(script, len(nodes))
            totals = {
                "nodes": len(nodes),
                "routes": len(routes),
                "slides": _int(getattr(script, "slide_count", 0)),
                "est_sec": round(sum(n["est_sec"] for n in nodes), 1),
            }
        except Exception:
            log.exception("走向图构建失败 unit=%s", unit_id)
            nodes, routes = [], []
            totals = {"nodes": 0, "routes": 0, "slides": 0, "est_sec": 0.0}
            hint = "这节课的脚本读不出来（数据结构不完整），走向图暂时打不开。"
    else:
        hint = _degrade_hint(course, reason)
    return {
        "ok": not hint,
        "mode": mode,
        "hint": hint,
        "unit": {"id": str(unit_id or ""), "title": str(title or "")},
        "totals": totals,
        "nodes": nodes,
        "routes": routes,
        # 降级时也照给：一节还没画出主线的课，同样可能正讲到第 3 页。
        "live": resolve_live(unit_id, nodes, live=live, conn=conn, profile_id=profile_id),
    }


def build_nodes(script) -> list[dict]:
    """`script.units` → 主线节点。`n` 就是它在 units 里的下标，别处一律按它对齐。"""
    titles = _page_titles(script)
    nodes: list[dict] = []
    for n, unit in enumerate(getattr(script, "units", None) or []):
        slide = _int(getattr(unit, "slide", 0))
        nodes.append({
            "n": n,
            "unit_id": str(getattr(unit, "unit_id", "") or ""),
            "slide": slide,
            "page": slide + 1,
            "page_title": titles.get(slide) or f"第 {slide + 1} 页",
            "summary": _clip(str(getattr(unit, "summary", "") or ""), SUMMARY_MAX_CHARS),
            "preview": _first_para(str(getattr(unit, "text", "") or "")),
            "est_sec": round(_num(getattr(unit, "est_sec", 0.0)), 1),
            "kp": [str(k) for k in (getattr(unit, "kp", None) or []) if str(k).strip()],
            "page_end": bool(getattr(unit, "is_page_end", False)),
        })
    return nodes


def build_routes(script, total_nodes: int) -> list[dict]:
    """`script.routes` → 预置路线。一条不合结构就丢它自己（整页不跟着塌）。"""
    out: list[dict] = []
    for route in getattr(script, "routes", None) or []:
        try:
            route.validate(total_nodes)
            out.append(_route_view(route))
        except Exception as e:
            log.info("走向图丢弃一条非法路线：%s: %s", type(e).__name__, e)
    return out


# ===========================================================================
# 进度指针（三档，按序，全程只读）
# ===========================================================================
def resolve_live(unit_id: str, nodes: list[dict], *,
                 live: Mapping[str, Any] | None = None, conn=None,
                 profile_id: str = "") -> dict:
    """当前指针：本进程正在上的那节课 → 库里最近一条会话 → 都没有。

    为什么先看 LIVE 而不是先读库：位置是**节流落库**的（runner 每 2 秒一次），
    只读库的话图上的指针会稳定落后半格到一格，而它恰恰是这张图最该准的东西。
    """
    runner = _live_runner(unit_id, live or {})
    if runner is not None:
        return _live_view(runner, nodes)
    row = _session_row(conn, profile_id, unit_id)
    if row is not None:
        slide = _int(row.get("position_slide"))
        return {
            "running": False,
            "source": "progress",
            "session_id": str(row.get("id") or ""),
            # position_unit 存的是**段序号**（这一页的第几段），不是格号 ——
            # 拿它当节点会指到别处去，所以只认页：这一页的第一格就是上次停的地方。
            "node": _first_node_on(slide, nodes),
            "slide": slide,
            "phase": str(row.get("status") or ""),
            "comprehension": None,
        }
    return _no_live()


def _no_live() -> dict:
    return {"running": False, "source": "none", "session_id": "", "node": None,
            "slide": None, "phase": "", "comprehension": None}


def _live_runner(unit_id: str, live: Mapping[str, Any]) -> Any | None:
    """LIVE 里正在上这个单元的那个运行器。

    LIVE 会被 WS 协程随时增删，所以先拷一份再遍历；单个运行器读不动就跳过它
    （它的属性属于另一条协程，取值时可能正好在收尾）。
    """
    for runner in list(live.values()):
        try:
            session = getattr(runner, "session", None) or {}
            if str(session.get("unit_id") or "") != unit_id:
                continue
            if str(session.get("status") or "active") != "active":
                continue
            if getattr(runner, "_ended", False) or getattr(runner, "_closing", False):
                continue
            return runner
        except Exception:
            continue
    return None


def _live_view(runner, nodes: list[dict]) -> dict:
    state = getattr(runner, "state", None)
    node = _node_from_transcript(state, nodes)
    if node is None:
        # transcript 最后一句可能是学生提问（没有 unit_id）：退一步问状态机自己在哪一格。
        node = _node_of(_machine_unit_id(runner), nodes)
    return {
        "running": True,
        "source": "live",
        "session_id": str(getattr(runner, "session_id", "") or ""),
        "node": node,
        "slide": _int(getattr(state, "slide_index", 0)),
        "phase": str(getattr(getattr(state, "phase", None), "value", "") or ""),
        "comprehension": _comprehension(state, node, nodes),
    }


def _node_from_transcript(state, nodes: list[dict]):
    """最后一句 utterance 讲的哪一格 —— 学生耳朵里的正是这一格。"""
    transcript = getattr(state, "transcript", None) or []
    if not transcript:
        return None
    return _node_of(str(getattr(transcript[-1], "unit_id", "") or ""), nodes)


def _machine_unit_id(runner) -> str:
    """状态机当前那一格（`Machine._cur_unit_id`：作答/重入时跟的是被打断的格）。"""
    try:
        return str(runner.machine._cur_unit_id() or "")
    except Exception:
        return ""


def _comprehension(state, node, nodes: list[dict]):
    """指针那一格自己的理解度（0~1）；没有信号就 null。

    为什么折成一个数而不是原样给 confirmed/vague/confused：这个字段是给图上色的，
    数值才能画渐变；而"哪种措辞算几分"是产品口径，只该有一处定义（见 COMPREHENSION_SCORE）。
    为什么只给当前这一格：信号本来就是按格记的，图上要画的是"这一格学生懂了没"，
    折成全课平均数反而没地方画，也解释不清。
    """
    signals = getattr(state, "comprehension", None) or {}
    if node is None or not (0 <= node < len(nodes)):
        return None
    return COMPREHENSION_SCORE.get(str(signals.get(nodes[node]["unit_id"]) or ""))


def _session_row(conn, profile_id: str, unit_id: str):
    """库里这个档案在这个单元上的最近一条会话；读不出来当没有。"""
    if conn is None or not unit_id:
        return None
    try:
        from store import repo

        pid = str(profile_id or "").strip() or repo.active_profile_id(conn)
        if not pid:
            return None
        return repo.latest_session(conn, pid, unit_id)
    except Exception as e:
        log.warning("走向图读会话失败 unit=%s: %s: %s", unit_id, type(e).__name__, e)
        return None


# ===========================================================================
# 降级 hint
# ===========================================================================
def _has_plan(course) -> bool:
    """这节课有没有一份真正备过课的 plan.json。

    只看 `course.plan` 不够：plan.json 缺失时 load_course 会用规则兜一版，plan 照样
    不是 None —— 那种脚本画出来是一屏没有总结的空白卡片。判据回到磁盘上那份文件。
    """
    deck = getattr(course, "deck", None)
    deck_id = str(getattr(deck, "deck_id", "") or "")
    if not deck_id:
        return False
    try:
        from deck.store import plan_json

        return plan_json(deck_id).is_file()
    except Exception:
        return False


def _degrade_hint(course, reason: str) -> str:
    """降级理由 → 一句人话（哪一步没成 + 下一步点了什么）。

    复用 notes 里已经写好的原因而不是重新猜一遍：装配时它就知道是"文件不在"
    还是"plan.json 读不出来"，而这两档对用户意味着不同的下一步。
    """
    if course is None:
        return _clip(reason or "这个单元的课件还没准备好，走向图暂时打不开。", HINT_CHARS)
    if str(getattr(course, "mode", "") or "") != "deck":
        why = _note_text(getattr(course, "notes", None) or []) or reason
        head = "这份课件还没解析成课堂脚本"
        text = f"{head}（{why}）" if why else head
        return _clip(f"{text}：先在单元页点一次「准备课件」再回来。", HINT_CHARS)
    return ("这节课只有规则兜底的讲稿（没有 plan.json），走向图暂时打不开："
            "先在单元页点一次「准备课件」再回来。")


def _note_text(notes) -> str:
    """notes 里第一条"出问题了"的原因 → 人话（去标记、压掉本机路径、截一行）。"""
    for note in notes:
        text = str(note or "")
        if text.lstrip().startswith(("❌", "⚠")):
            return _plain(text)
    return ""


def _plain(note: str) -> str:
    text = _MARKS.sub("", str(note or "")).strip()
    try:
        # 复用 runner 的路径压缩：notes 里的"文件不存在：C:\…"会被原样拼进 hint，
        # 而这句话是给学生看的（deps 里已经复用过一次同一份实现）。
        from studio.runner import shorten_paths

        text = shorten_paths(text)
    except Exception:
        pass
    return _clip(text, NOTE_CHARS)


# ===========================================================================
# 小工具
# ===========================================================================
def _route_view(route) -> dict:
    kind = str(getattr(route, "kind", "") or "")
    fork = _int(getattr(route, "fork_node", 0))
    merge = _int(getattr(route, "merge_node", 0))
    span = max(0, merge - fork)
    revisit = kind == "revisit"
    steps = [_step_view(s) for s in (getattr(route, "steps", None) or [])]
    return {
        "route_id": str(getattr(route, "route_id", "") or ""),
        "kind": kind,
        "label": KIND_LABELS.get(kind, kind),
        "fork_node": fork,
        "merge_node": merge,
        "span": span,
        # skip / finish 真正被跳过的是 fork 与 merge 之间那几格（不含两端），
        # revisit 一格都不跳 —— 这两个数是"这条线划过去发生了什么"的全部依据。
        "skipped": 0 if revisit else max(0, span - 1),
        "repeats": len(steps) if revisit else 0,
        "est_sec": round(_num(getattr(route, "est_sec", 0.0)), 1),
        "summary": route.summary(max_chars=ROUTE_SUMMARY_CHARS),
        "reason": str(getattr(route, "reason", "") or ""),
        "steps": steps,
    }


def _step_view(step) -> dict:
    return {
        "unit_id": str(getattr(step, "unit_id", "") or ""),
        "label": str(getattr(step, "label", "") or ""),
        "text": str(getattr(step, "text", "") or ""),
        "est_sec": round(_num(getattr(step, "est_sec", 0.0)), 1),
    }


def _page_titles(script) -> dict:
    """页序 → 页标题。取 `Slide.index`（与单元自己的 slide 同一套编号），没有才用位次。"""
    out: dict[int, str] = {}
    for pos, slide in enumerate(getattr(script, "slides", None) or []):
        try:
            idx = int(getattr(slide, "index", pos))
        except (TypeError, ValueError):
            idx = pos
        out.setdefault(idx, " ".join(str(getattr(slide, "title", "") or "").split()))
    return out


def _node_of(unit_id: str, nodes: list[dict]):
    if not unit_id:
        return None
    for node in nodes:
        if node["unit_id"] == unit_id:
            return node["n"]
    return None


def _first_node_on(slide: int, nodes: list[dict]):
    for node in nodes:
        if node["slide"] == slide:
            return node["n"]
    return None


def _first_para(text: str) -> str:
    for line in str(text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return _clip(line, PREVIEW_CHARS)
    return ""


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    try:
        return int(_num(value, float(default)))
    except (TypeError, ValueError):
        return default
