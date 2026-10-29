"""课程规划层：需求 / 大纲 / 单元课程安排设计 的规整与互转（纯函数，不碰库、不碰网）。

为什么单独一层
--------------
这一层要同时服务四件事：

1. **模型给的 JSON 不可信**：字段可能缺、类型可能错、章节可能一个都没有。这里把
   任何输入都规整成同一个形状，让下游（蓝图、渲染、落库）只面对一种数据。
2. **TeachingFlow 的蓝图契约是硬的**：``blueprint.unit_variables`` 按下标取
   ``ch["units"][i]``、``naming.deliverable_path`` 读 ``chapter_order``/``unit_index``。
   所以「结构化大纲」必须能无损翻译成蓝图，而不是另起一套。
3. **单元的教学设计对用户不可见**：它只用来渲染 ``03_unit_<uid>.result.md``，
   也就是 PPT 链路 S4 的输入。渲染必须是确定性的（同一份设计永远得到同一份 md），
   否则「同一节课两次出片内容不一样」这种事会没法解释。
4. **没有模型时也要能走完**：离线兜底（``offline_*``）让链路在没有 key 的机器上
   照样产出结构正确的大纲与设计 —— 那是测试与演示的地基，不是第二套产品。

口径上的三个名字，全文统一：
    requirements  课程需求（用户确认过的：主题/程度/基础/周期/重点）
    outline       结构化课程大纲（章 → 单元，文件 outline.json）
    design        单元课程安排设计（知识点/教学流程/例题/练习，文件 unit_design_<uid>.json）
"""

from __future__ import annotations

import re
from typing import Any

# ===========================================================================
# 常量（与 TeachingFlow/core/scope.py 同一口径，改这里要连着改那边）
# ===========================================================================

STAGES: tuple[str, ...] = ("小学", "初中", "高中", "大学", "考研")
#: ``teacher_unit`` 模板认的五个水平档（也是 flow.py --level 的取值）
LEVELS: tuple[str, ...] = ("入门", "基础", "中等", "进阶", "竞赛")

#: 章 / 单元的数量边界：LLM 很容易一口气排 20 章，那不是课程的粒度
MAX_CHAPTERS = 8
MAX_UNITS_PER_CHAPTER = 6
MIN_CHAPTERS = 1
MIN_UNITS_PER_CHAPTER = 1

#: 周期与课时的合理区间（蓝图契约是 total_weeks < 16，这里留一点余量给长期课）
MAX_WEEKS = 24
MAX_HOURS_PER_WEEK = 12

#: 单元名 / 章名里不许出现的字符（与 core/naming.py 的 _BAD_CHARS 同口径）
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')

_STAGE_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("小学", re.compile(r"小学|[一二三四五六]年级")),
    ("初中", re.compile(r"初中|初一|初二|初三|七年级|八年级|九年级")),
    ("高中", re.compile(r"高中|高一|高二|高三|高考")),
    ("考研", re.compile(r"考研|研究生|硕士")),
    ("大学", re.compile(r"大学|本科|大[一二三四]|高校|专业课|期末")),
)

_LEVEL_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("入门", re.compile(r"零基础|小白|完全没学过|从零|入门|初学")),
    ("进阶", re.compile(r"进阶|提高|拔高|深入|有基础想进阶")),
    ("竞赛", re.compile(r"竞赛|奥赛|比赛")),
    ("中等", re.compile(r"中等|中级|学过一点|有点基础")),
    ("基础", re.compile(r"基础|普通|一般")),
)

_WEEK_RE = re.compile(r"(\d{1,2})\s*(?:周|个星期|星期)")
#: 中文数字的周期（「两周」「三个星期」「一个月」）—— 用户口语里比阿拉伯数字常见得多
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_WEEK_CN_RE = re.compile(r"([一二两三四五六七八九十]{1,2})\s*个?\s*(?:周|星期)")
_MONTH_CN_RE = re.compile(r"([一二两三四五六七八九十\d]{1,2})\s*个?\s*月")
_DAY_RE = re.compile(r"(\d{1,2})\s*天")
_HOUR_RE = re.compile(r"(?:每周|一周)\s*(\d{1,2})\s*(?:课时|小时|节课)")


def _cn_int(text: str) -> int | None:
    """把「两 / 三 / 十二」这类中文数字换成整数（认不出返回 None）。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    head, tail = text[0], text[1:]
    if head == "十":
        return 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)
    if tail.startswith("十"):
        return _CN_DIGITS.get(head, 0) * 10 + (_CN_DIGITS.get(tail[1:], 0) if tail[1:] else 0)
    return None


class PlanError(ValueError):
    """规整失败（内容为空、结构不可修复）。message 直接能给人看。"""


# ===========================================================================
# 小工具
# ===========================================================================

def _text(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()[:limit]


def _clean_name(value: Any, fallback: str = "") -> str:
    s = _BAD_CHARS.sub("", _text(value, 120)).strip().rstrip(". ")
    return s or fallback


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = default
    return max(low, min(high, n))


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any, limit: int = 24) -> list[str]:
    """把模型的「字符串 / 字符串数组 / [{name: …}]」一律压成字符串数组。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or item.get("point") or ""
        text = _text(item, 200)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _extract_json(raw: str) -> str:
    """从可能带 ``` 围栏 / 前后寒暄的回复里取出 JSON 主体。"""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        return fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _looks_like_outline(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("chapters"), list)


# ===========================================================================
# 需求
# ===========================================================================

def infer_stage(text: str, default: str = "高中") -> str:
    for stage, rx in _STAGE_HINTS:
        if rx.search(text or ""):
            return stage
    return default


def infer_level(text: str, default: str = "基础") -> str:
    for level, rx in _LEVEL_HINTS:
        if rx.search(text or ""):
            return level
    return default


def infer_weeks(text: str, default: int = 6) -> int:
    raw = text or ""
    m = _WEEK_RE.search(raw)
    if m:
        return _as_int(m.group(1), default, 1, MAX_WEEKS)
    m = _WEEK_CN_RE.search(raw)
    if m:
        value = _cn_int(m.group(1))
        if value:
            return max(1, min(MAX_WEEKS, value))
    m = _MONTH_CN_RE.search(raw)
    if m:
        value = _cn_int(m.group(1))
        if value:
            return max(1, min(MAX_WEEKS, value * 4))
    m = _DAY_RE.search(raw)
    if m:
        return _as_int(round(int(m.group(1)) / 7) or 1, default, 1, MAX_WEEKS)
    return default


def normalize_requirements(raw: Any, fallback_text: str = "") -> dict:
    """把模型（或规则）整理出的需求压成唯一形状。

    缺的字段不编：``subject`` 空就写「通用」，``weeks``/``hours_per_week`` 给默认值，
    但 ``goal`` 与 ``course_title`` 一定非空 —— 它们是后来所有提示词的第一句。
    """
    data = raw if isinstance(raw, dict) else {}
    seed = _text(fallback_text, 400)

    title = _clean_name(data.get("course_title") or data.get("title"), "")
    if not title:
        title = _clean_name(seed, "未命名课程")[:40] or "未命名课程"

    stage = _text(data.get("stage"))
    if stage not in STAGES:
        stage = infer_stage(f"{seed} {title}")

    level = _text(data.get("student_level") or data.get("level"))
    if level not in LEVELS:
        level = infer_level(f"{seed} {_text(data.get('prerequisite'))}")

    subject = _clean_name(data.get("subject"), "") or "通用"
    goal = _text(data.get("goal") or data.get("learning_goal"), 600)
    if not goal:
        goal = f"系统掌握《{title}》的核心内容，能独立完成相关练习与典型问题。"

    weeks = _as_int(data.get("weeks") or data.get("total_weeks"),
                    infer_weeks(seed), 1, MAX_WEEKS)
    hours = _as_int(data.get("hours_per_week") or data.get("weekly_hours"),
                    _as_int(_hour_hint(seed), 3, 1, MAX_HOURS_PER_WEEK),
                    1, MAX_HOURS_PER_WEEK)

    key_topics = _str_list(data.get("key_topics") or data.get("topics"), limit=16)
    summary = _text(data.get("summary"), 400)
    if not summary:
        summary = goal

    return {
        "course_title": title,
        "subject": subject,
        "stage": stage,
        "goal": goal,
        "student_level": level,
        "prerequisite": _text(data.get("prerequisite") or data.get("foundation"), 400),
        "weeks": weeks,
        "hours_per_week": hours,
        "key_topics": key_topics,
        "summary": summary,
    }


def _hour_hint(text: str) -> int | None:
    m = _HOUR_RE.search(text or "")
    return int(m.group(1)) if m else None


# ===========================================================================
# 大纲
# ===========================================================================

def normalize_outline(raw: Any, requirements: dict) -> dict:
    """把模型给的章 / 单元压成结构化大纲。形状不合理就直接抛（不猜）。"""
    data = raw
    if isinstance(data, str):
        try:
            import json

            data = json.loads(_extract_json(data))
        except (TypeError, ValueError) as exc:
            raise PlanError(f"大纲不是合法 JSON：{exc}") from exc
    if not _looks_like_outline(data):
        raise PlanError("大纲缺少 chapters 数组")

    req = requirements or {}
    chapters_in = data.get("chapters") or []
    chapters: list[dict] = []
    for index, chapter in enumerate(chapters_in):
        if not isinstance(chapter, dict):
            continue
        ch_no = len(chapters) + 1
        ch_id = f"ch{ch_no:02d}"
        units_in = chapter.get("units") or []
        units: list[dict] = []
        for unit in units_in:
            if not isinstance(unit, dict):
                continue
            u_no = len(units) + 1
            title = _clean_name(unit.get("title") or unit.get("name"), "")
            if not title:
                continue
            points = _str_list(unit.get("knowledge_points") or unit.get("points"), 12)
            units.append({
                "id": f"{ch_id}-u{u_no}",
                "number": u_no,
                "title": title[:60],
                "type": _text(unit.get("type"), 12) or "新授",
                "hours": round(max(0.5, _as_float(unit.get("hours") or unit.get("estimated_hours"), 1.0)), 1),
                "summary": _text(unit.get("summary"), 300),
                "knowledge_points": points,
            })
            if len(units) >= MAX_UNITS_PER_CHAPTER:
                break
        if not units:
            continue
        chapters.append({
            "id": ch_id,
            "number": ch_no,
            "title": _clean_name(chapter.get("title"), f"第{ch_no}章")[:60],
            "summary": _text(chapter.get("summary"), 300),
            "units": units,
        })
        if len(chapters) >= MAX_CHAPTERS:
            break

    if not chapters:
        raise PlanError("大纲里没有可用的章 / 单元（每章至少要有一个带标题的单元）")

    outline = {
        "course_title": _clean_name(data.get("course_title"), "") or req.get("course_title") or "未命名课程",
        "subject": _clean_name(data.get("subject"), "") or req.get("subject") or "通用",
        "stage": _text(data.get("stage")) if _text(data.get("stage")) in STAGES else (req.get("stage") or "高中"),
        "goal": _text(data.get("goal"), 600) or req.get("goal", ""),
        "student_level": _text(data.get("student_level")) if _text(data.get("student_level")) in LEVELS
        else (req.get("student_level") or "基础"),
        "summary": _text(data.get("summary"), 600) or req.get("summary", ""),
        "weeks": _as_int(data.get("weeks") or data.get("total_weeks"),
                         _as_int(req.get("weeks"), 6, 1, MAX_WEEKS), 1, MAX_WEEKS),
        "hours_per_week": _as_int(data.get("hours_per_week") or data.get("weekly_hours"),
                                  _as_int(req.get("hours_per_week"), 3, 1, MAX_HOURS_PER_WEEK),
                                  1, MAX_HOURS_PER_WEEK),
        "chapters": chapters,
    }
    outline = balance_hours(outline)
    outline["chapter_count"] = len(outline["chapters"])
    outline["unit_count"] = sum(len(c["units"]) for c in outline["chapters"])
    return outline


def balance_hours(outline: dict) -> dict:
    """把单元课时摊到「周期 × 每周课时」上（差得太多才重排，差一点不动）。

    为什么必须做：单元课时是后面 S4a 唯一的时间锚（「本单元课时」进提示词），
    而模型排出来的课时合计经常和用户说的周期差一倍 —— 那会让一节课被要求
    「45 分钟讲 3 课时」。
    """
    weeks = _as_int(outline.get("weeks"), 6, 1, MAX_WEEKS)
    per_week = _as_int(outline.get("hours_per_week"), 3, 1, MAX_HOURS_PER_WEEK)
    budget = float(weeks * per_week)
    units = [u for c in outline.get("chapters") or [] for u in c.get("units") or []]
    if not units:
        return outline
    total = sum(float(u.get("hours") or 0) for u in units)
    if total <= 0:
        total = float(len(units))
    # 差 20% 以内不动：模型按内容排的课时比机械均分更合理
    if abs(total - budget) <= max(1.0, budget * 0.2):
        return outline
    share = round(max(0.5, budget / len(units)) * 2) / 2
    for unit in units:
        unit["hours"] = share
    outline["total_hours"] = round(share * len(units), 1)
    return outline


def outline_total_hours(outline: dict) -> float:
    return round(sum(float(u.get("hours") or 0)
                     for c in outline.get("chapters") or []
                     for u in c.get("units") or []), 1)


def flatten_outline(outline: dict) -> list[dict]:
    """大纲 → 按章序 / 单元序排好的扁平单元表（含蓝图需要的位置信息）。"""
    out: list[dict] = []
    for ch_index, chapter in enumerate(outline.get("chapters") or []):
        for unit in chapter.get("units") or []:
            out.append({
                "chapter_id": chapter["id"],
                "chapter_order": chapter.get("number") or ch_index + 1,
                "chapter_title": chapter.get("title") or "",
                "unit_index": int(unit.get("number") or 1) - 1,
                "unit_id": unit["id"],
                "unit_title": unit.get("title") or "",
                "unit_type": unit.get("type") or "新授",
                "hours": float(unit.get("hours") or 1.0),
                "units_in_chapter": len(chapter.get("units") or []),
                "summary": unit.get("summary") or "",
                "knowledge_points": list(unit.get("knowledge_points") or []),
            })
    return out


# ===========================================================================
# 大纲 → TeachingFlow 蓝图
# ===========================================================================

def outline_to_blueprint(outline: dict) -> dict:
    """结构化大纲 → ``01_blueprint.yaml`` 的 dict（S3/S4/S5 认的那一份）。

    与 ``teacher_planner`` 的产出同形：course 元信息 + chapters[units[assessment]]。
    多写的几个字段（``unit_order`` / ``kp``）下游不读，但审计时能直接看出
    「这一章为什么这么排」，比回头看提示词便宜。
    """
    weeks = _as_int(outline.get("weeks"), 6, 1, MAX_WEEKS)
    per_week = _as_int(outline.get("hours_per_week"), 3, 1, MAX_HOURS_PER_WEEK)
    term = "短期" if weeks <= 3 else "长期"
    chapters_out: list[dict] = []
    prev_id = ""
    for chapter in outline.get("chapters") or []:
        units = chapter.get("units") or []
        ch_hours = round(sum(float(u.get("hours") or 0) for u in units), 1)
        unit_items = []
        for unit in units:
            unit_items.append({
                "id": unit["id"],
                "title": unit["title"],
                "type": unit.get("type") or "新授",
                "estimated_hours": float(unit.get("hours") or 1.0),
                "homework": {"count": 4, "difficulty": "基础"},
                "lesson_status": "pending",
                "kp": list(unit.get("knowledge_points") or []),
            })
        unlock = ({"type": "after_chapter", "chapter": prev_id, "min_score": 60}
                  if prev_id else {"type": "none"})
        chapters_out.append({
            "id": chapter["id"],
            "order": int(chapter.get("number") or len(chapters_out) + 1),
            "title": chapter.get("title") or "",
            "estimated_hours": ch_hours,
            "unlock": unlock,
            "units": unit_items,
            "assessment": {
                "type": "chapter_test",
                "difficulty": "基础",
                "covers": [u["id"] for u in units],
                "unlock_next": {"min_score": 60},
            },
        })
        prev_id = chapter["id"]
    return {
        "course": {
            "subject": outline.get("subject") or "通用",
            "goal": outline.get("goal") or "",
            "student_level": outline.get("student_level") or "基础",
            "total_weeks": weeks,
            "weekly_hours": per_week,
            "mode": "short_term" if term == "短期" else "long_term",
            "buffer_ratio": 0.15,
            "net_hour_budget": outline_total_hours(outline),
            "term": term,
            "weeks_source": "用户在对话中确认",
            "chapter_count": len(chapters_out),
        },
        "chapters": chapters_out,
    }


def blueprint_targets(outline: dict) -> list[dict]:
    """全量目标（``02_targets.json`` 的形状，与 S2 的 pick_targets 同字段）。"""
    targets: list[dict] = []
    for unit in flatten_outline(outline):
        targets.append({**unit, "unlocked": True, "unlock_reason": "由课程大纲生成"})
    return targets


# ===========================================================================
# 单元课程安排设计
# ===========================================================================

def normalize_design(raw: Any, unit: dict, chapter: dict, outline: dict) -> dict:
    """把模型给的单元设计压成唯一形状（缺的字段留空，不编造具体知识内容）。"""
    data = raw if isinstance(raw, dict) else {}
    points_in = data.get("knowledge_points") or []
    kps: list[dict] = []
    for item in points_in:
        if isinstance(item, dict):
            name = _clean_name(item.get("name") or item.get("point") or item.get("title"), "")
            if not name:
                continue
            kps.append({
                "name": name[:60],
                "explain": _text(item.get("explain") or item.get("detail") or item.get("desc"), 900),
                "difficulty": _text(item.get("difficulty"), 12) or "基础",
            })
        else:
            name = _clean_name(item, "")
            if name:
                kps.append({"name": name[:60], "explain": "", "difficulty": "基础"})
    if not kps:
        # 大纲里本来就写了知识点（用户看过的那份）：以它为底，别让设计变空
        kps = [{"name": n, "explain": "", "difficulty": "基础"}
               for n in (unit.get("knowledge_points") or [])]

    slides_in = data.get("slides") or []
    slides: list[dict] = []
    for item in slides_in:
        if isinstance(item, dict):
            title = _clean_name(item.get("title") or item.get("heading"), "")
            if not title:
                continue
            slides.append({"title": title[:50], "points": _str_list(item.get("points") or item.get("bullets"), 8)})
    if not slides:
        slides = _slides_from_points(unit.get("title") or "", kps)

    exercises_in = data.get("exercises") or []
    exercises: list[dict] = []
    for item in exercises_in:
        if isinstance(item, dict):
            q = _text(item.get("q") or item.get("question") or item.get("stem"), 600)
            if not q:
                continue
            exercises.append({
                "q": q,
                "answer": _text(item.get("answer") or item.get("a"), 900),
                "kind": _text(item.get("kind") or item.get("difficulty"), 12) or "基础",
            })
        else:
            q = _text(item, 600)
            if q:
                exercises.append({"q": q, "answer": "", "kind": "基础"})

    flow_in = data.get("lesson_flow") or []
    lesson_flow: list[dict] = []
    for item in flow_in:
        if isinstance(item, dict):
            phase = _clean_name(item.get("phase") or item.get("stage"), "")
            if not phase:
                continue
            lesson_flow.append({
                "phase": phase[:20],
                "minutes": _as_int(item.get("minutes"), 5, 1, 120),
                "activity": _text(item.get("activity") or item.get("what"), 400),
            })
    if not lesson_flow:
        lesson_flow = _default_flow(unit)

    examples = _str_list(data.get("examples") or data.get("typical_examples"), 6)
    mistakes = _str_list(data.get("common_mistakes") or data.get("mistakes"), 6)

    return {
        "unit_id": unit["id"],
        "chapter_id": chapter["id"],
        "chapter_title": chapter.get("title") or "",
        "title": unit.get("title") or "",
        "type": unit.get("type") or "新授",
        "hours": float(unit.get("hours") or 1.0),
        "teaching_goals": _str_list(data.get("teaching_goals") or data.get("goals"), 8)
        or [f"能说清「{k['name']}」的关键结论" for k in kps[:3]],
        "knowledge_points": kps,
        "prerequisites": _str_list(data.get("prerequisites") or data.get("prior"), 8),
        "lesson_flow": lesson_flow,
        "examples": examples,
        "exercises": exercises,
        "common_mistakes": mistakes,
        "summary": _text(data.get("summary"), 900) or (unit.get("summary") or ""),
        "slides": slides,
        "offline": False,
    }


def _slides_from_points(title: str, points: list[dict]) -> list[dict]:
    """没有显式页设计时，一页一个知识点（「一页一个教学动作」的朴素版）。"""
    slides = [{"title": f"{title}：本课目标", "points": [p["name"] for p in points[:4]] or [title]}]
    for point in points[:8]:
        bullets = [point["name"]]
        if point.get("explain"):
            bullets.append(point["explain"][:60])
        slides.append({"title": point["name"], "points": bullets})
    slides.append({"title": "小结与作业", "points": ["回顾本课知识点", "完成课后练习"]})
    return slides


def _default_flow(unit: dict) -> list[dict]:
    hours = float(unit.get("hours") or 1.0)
    total = max(20, int(round(hours * 45)))
    return [
        {"phase": "导入", "minutes": max(3, round(total * 0.1)), "activity": "用一个具体问题引出本课主题"},
        {"phase": "讲解", "minutes": round(total * 0.45), "activity": "按知识点顺序讲解，每点配一个小例子"},
        {"phase": "例题", "minutes": round(total * 0.2), "activity": "演示一道典型题，讲清每一步的依据"},
        {"phase": "练习", "minutes": round(total * 0.15), "activity": "学生独立完成练习，当堂订正"},
        {"phase": "小结", "minutes": max(2, round(total * 0.1)), "activity": "回顾要点并布置作业"},
    ]


# ===========================================================================
# 设计 → S4 的输入（03_unit_<uid>.result.md 的 10 模块）
# ===========================================================================
#
# 这个渲染是**严格确定性**的：同一份设计永远得到同一份 md。
# 模块名与顺序与 PromptTemplate/unit_bridge.py 的 UNIT_ALL_SECTIONS 逐字一致 ——
# 少了任何一个，unit_output_to_ppt_content 就会退回"整篇原样传"，PPT 里会混进作业与解锁提示。

def render_unit_markdown(design: dict, *, chapter_index: int, prev_title: str = "",
                         next_title: str = "") -> str:
    """设计 → ``03_unit_<uid>.result.md``（10 个模块，供 S4 消费）。"""
    title = design.get("title") or "本单元"
    kps = design.get("knowledge_points") or []
    flow = design.get("lesson_flow") or []
    exercises = design.get("exercises") or []
    examples = design.get("examples") or []
    mistakes = design.get("common_mistakes") or []
    goals = design.get("teaching_goals") or []
    total_min = sum(int(item.get("minutes") or 0) for item in flow) or int(
        round(float(design.get("hours") or 1.0) * 45))

    lines: list[str] = []

    # 1. 本单元定位
    position = [f"第{chapter_index + 1}章「{design.get('chapter_title') or ''}」中的「{title}」，"
                f"预计 {float(design.get('hours') or 1.0):g} 课时（约 {total_min} 分钟）。"]
    if prev_title:
        position.append(f"承接上一单元「{prev_title}」；")
    else:
        position.append("是本课程的开篇单元；")
    if next_title:
        position.append(f"为下一单元「{next_title}」做准备。")
    lines.append(_module(1, "本单元定位", "\n".join(position)))

    # 2. 难度调整说明
    prereq = design.get("prerequisites") or []
    diff = [f"按「{design.get('type') or '新授'}」课型与既定学生水平输出。"]
    if prereq:
        diff.append("本课假设学生已具备：" + "、".join(prereq) + "。")
    else:
        diff.append("本课不假设额外的先修知识，从零开始建立概念。")
    lines.append(_module(2, "难度调整说明", "\n".join(diff)))

    # 3. 本课目标
    lines.append(_module(3, "本课目标",
                         "\n".join(f"- {g}" for g in goals) if goals else f"- 掌握「{title}」的核心内容"))

    # 4. 知识讲解
    body: list[str] = []
    for index, point in enumerate(kps):
        explain = (point.get("explain") or "").strip()
        body.append(f"要点 {index + 1}：{point.get('name')}")
        body.append(explain or f"{point.get('name')} 的定义、作用与使用条件，"
                               f"结合一个具体例子说明它在什么时候用、为什么这么用。")
        body.append("")
    lines.append(_module(4, "知识讲解", "\n".join(body).strip() or f"讲解「{title}」的核心内容。"))

    # 5. 典型例题
    if examples:
        lines.append(_module(5, "典型例题", "\n\n".join(examples)))
    else:
        lines.append(_module(5, "典型例题",
                             f"例题：结合本课第 1 个知识点设计一道小题，要求写出完整依据。\n"
                             f"解析：先读题定位考点，再按「条件 → 结论 → 依据」三步作答。"))

    # 6. 易错提醒
    lines.append(_module(6, "易错提醒",
                         "\n".join(f"- {m}" for m in mistakes) if mistakes
                         else f"- 只记结论不写依据，是本课最常见的失分点。"))

    # 7. 小结
    lines.append(_module(7, "小结", design.get("summary")
                         or f"本课围绕{title}讲了 {len(kps)} 个要点：" +
                            "、".join(p.get("name") or "" for p in kps) + "。"))

    # 8. 课件大纲（页骨架）
    slides = design.get("slides") or _slides_from_points(title, kps)
    page_lines = []
    for index, slide in enumerate(slides):
        points = slide.get("points") or []
        suffix = f" — {'；'.join(points)}" if points else ""
        page_lines.append(f"P{index + 1} {slide.get('title') or title}{suffix}")
    lines.append(_module(8, "课件大纲", "\n".join(page_lines)))

    # 9. 单元作业
    if exercises:
        items = []
        for index, item in enumerate(exercises):
            items.append(f"题{index + 1}（{item.get('kind') or '基础'}）：{item.get('q')}")
        lines.append(_module(9, "单元作业", "\n".join(items)))
    else:
        lines.append(_module(9, "单元作业", "完成本课配套练习 4 题，答案与解析略。"))

    # 10. 完成后解锁
    tail = (f"完成本单元作业后进入下一单元「{next_title}」。" if next_title
            else "本单元是本课程最后一个单元，完成后可进入总复习。")
    lines.append(_module(10, "完成后解锁", tail))
    return "\n".join(lines) + "\n"


def _module(number: int, name: str, body: str) -> str:
    return f"{number}. 【{name}】\n{(body or '').strip()}\n"


def render_outline_markdown(outline: dict) -> str:
    """大纲的人类可读版（``outline.md``，不是前端用的那份 JSON）。"""
    lines = [
        f"# {outline.get('course_title') or '未命名课程'}",
        "",
        f"- 学科：{outline.get('subject') or '—'}｜学段：{outline.get('stage') or '—'}"
        f"｜水平：{outline.get('student_level') or '—'}",
        f"- 周期：{outline.get('weeks')} 周 × {outline.get('hours_per_week')} 课时/周"
        f"（约 {outline_total_hours(outline):g} 课时）",
        f"- 目标：{outline.get('goal') or '—'}",
        "",
    ]
    for chapter in outline.get("chapters") or []:
        lines.append(f"## 第{chapter.get('number')}章 {chapter.get('title')}")
        if chapter.get("summary"):
            lines.append(chapter["summary"])
        for unit in chapter.get("units") or []:
            points = "、".join(unit.get("knowledge_points") or [])
            lines.append(f"- {chapter.get('number')}.{unit.get('number')} {unit.get('title')}"
                         f"（{float(unit.get('hours') or 0):g} 课时）"
                         + (f" —— {points}" if points else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ===========================================================================
# 离线兜底（没有模型时也要走完，供 mock / 无 key 的机器使用）
# ===========================================================================

def offline_questions(requirements: dict, asked: set[str]) -> list[dict]:
    """规规矩矩把「程度 / 基础 / 周期 / 重点」问一遍（问过的不再问）。"""
    bank = [
        {
            "id": "depth",
            "header": "想学到什么程度",
            "question": f"你希望{requirements.get('course_title') or '这门课'}学到什么程度？",
            "multiple": False,
            "options": [
                {"label": "了解与入门", "description": "能听懂、能说清基本概念"},
                {"label": "掌握并能做题", "description": "概念清楚，常见题会做"},
                {"label": "能独立应用", "description": "能解决综合问题、能解释给别人听"},
            ],
        },
        {
            "id": "foundation",
            "header": "你现在的基础",
            "question": "你在这块内容上的基础大概是？",
            "multiple": False,
            "options": [
                {"label": "零基础", "description": "没接触过，需要从头讲"},
                {"label": "学过一点", "description": "知道名词，但不成体系"},
                {"label": "有基础", "description": "想补薄弱点或提高"},
            ],
        },
        {
            "id": "period",
            "header": "预计学习周期",
            "question": "你打算用多长时间学完？",
            "multiple": False,
            "options": [
                {"label": "2 周内速成", "description": "时间紧，只抓重点"},
                {"label": "1 个月左右", "description": "每周几小时，稳步推进"},
                {"label": "2~3 个月", "description": "系统学，配套练习"},
            ],
        },
        {
            "id": "focus",
            "header": "重点想学什么",
            "question": "有没有特别想覆盖或特别想跳过的内容？",
            "multiple": True,
            "options": [
                {"label": "按标准课程顺序走", "description": "从基础到综合，不特别指定"},
                {"label": "重点在概念理解", "description": "先把「是什么、为什么」讲透"},
                {"label": "重点在练习与考试", "description": "多讲题型与解法"},
            ],
        },
    ]
    return [item for item in bank if item["id"] not in asked]


def offline_outline(requirements: dict) -> dict:
    """没有模型时按需求排一份结构正确的 3~4 章大纲（内容朴素但可用）。"""
    req = requirements or {}
    title = req.get("course_title") or "未命名课程"
    topics = [t for t in (req.get("key_topics") or []) if t]
    if not topics:
        topics = [f"{title}的基本概念", f"{title}的核心方法", f"{title}的综合应用"]
    weeks = _as_int(req.get("weeks"), 6, 1, MAX_WEEKS)
    per_week = _as_int(req.get("hours_per_week"), 3, 1, MAX_HOURS_PER_WEEK)
    budget = max(len(topics), int(round(weeks * per_week)))
    chapters = []
    for index, topic in enumerate(topics[:MAX_CHAPTERS]):
        ch_no = index + 1
        units = [
            {"title": f"{topic}：概念与表示", "type": "新授",
             "knowledge_points": [f"{topic}的定义", f"{topic}的表示方法"]},
            {"title": f"{topic}：方法与练习", "type": "巩固",
             "knowledge_points": [f"{topic}的常用方法", f"{topic}的典型题"]},
        ]
        chapters.append({"title": str(topic), "summary": "", "units": [
            {**u, "hours": 1.0, "summary": ""} for u in units
        ]})
    raw = {
        "course_title": title,
        "subject": req.get("subject") or "通用",
        "stage": req.get("stage") or "高中",
        "goal": req.get("goal") or "",
        "student_level": req.get("student_level") or "基础",
        "summary": req.get("summary") or "",
        "weeks": weeks,
        "hours_per_week": per_week,
        "chapters": chapters,
    }
    outline = normalize_outline(raw, req)
    outline["total_hours"] = budget
    outline["offline"] = True
    return outline


def offline_design(unit: dict, chapter: dict, outline: dict, *, prev_title: str = "",
                   next_title: str = "") -> dict:
    """没有模型时按大纲知识点生成一份结构完整的单元设计。"""
    kps = unit.get("knowledge_points") or [unit.get("title") or "本课要点"]
    raw = {
        "teaching_goals": [f"能说清「{name}」的含义与作用" for name in kps[:3]],
        "knowledge_points": [
            {"name": name, "difficulty": "基础",
             "explain": f"{name}：先说明它解决什么问题，再给出定义与一个具体例子，"
                        f"最后指出使用时的条件与常见误用。"}
            for name in kps
        ],
        "prerequisites": [f"上一单元「{prev_title}」的要点"] if prev_title else [],
        "lesson_flow": _default_flow(unit),
        "examples": [],
        "exercises": [
            {"q": f"围绕「{name}」做一道基础题，要求写出判断依据。", "kind": "基础", "answer": ""}
            for name in kps[:3]
        ],
        "common_mistakes": ["只记结论不写依据", "把相近概念混用"],
        "summary": f"本课围绕{unit.get('title') or ''}讲了 {len(kps)} 个要点。",
        "slides": _slides_from_points(unit.get("title") or "", [
            {"name": name, "explain": ""} for name in kps
        ]),
    }
    design = normalize_design(raw, unit, chapter, outline)
    design["offline"] = True
    design["prerequisites"] = design["prerequisites"] or ([f"上一单元「{prev_title}」"] if prev_title else [])
    return design


# ===========================================================================
# 校验（编辑后的结构是否能拿去排课）
# ===========================================================================

def validate_outline(outline: Any) -> list[str]:
    """返回问题列表（空 = 可以拿去生成单元设计）。"""
    problems: list[str] = []
    if not isinstance(outline, dict):
        return ["大纲不是对象"]
    chapters = outline.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        return ["至少要有一个章"]
    if len(chapters) > MAX_CHAPTERS:
        problems.append(f"章太多了（{len(chapters)} > {MAX_CHAPTERS}）")
    total_units = 0
    seen_ids: set[str] = set()
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            problems.append(f"第 {index + 1} 章不是对象")
            continue
        if not _text(chapter.get("title")):
            problems.append(f"第 {index + 1} 章缺标题")
        units = chapter.get("units")
        if not isinstance(units, list) or not units:
            problems.append(f"第 {index + 1} 章没有单元")
            continue
        if len(units) > MAX_UNITS_PER_CHAPTER:
            problems.append(f"第 {index + 1} 章单元太多（{len(units)} > {MAX_UNITS_PER_CHAPTER}）")
        for unit in units:
            total_units += 1
            if not isinstance(unit, dict) or not _text(unit.get("title")):
                problems.append(f"第 {index + 1} 章里有单元缺标题")
                continue
            uid = _text(unit.get("id"))
            if uid:
                if uid in seen_ids:
                    problems.append(f"单元 id 重复：{uid}")
                seen_ids.add(uid)
    if total_units == 0:
        problems.append("至少要有一个单元")
    return problems
