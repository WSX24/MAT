"""课程来源：这节课是"手写演示课件"还是"真实 pptx 课件"。

为什么要这一层
--------------
原型原本在 `web.py` 模块级直接 `script = build_script()` —— 课件是写死的。
接入真实 PPT 之后，课件变成一个**可配置的输入**：
    DECK_ID=<deck_id>           用 decks/<deck_id>/ 下已解析好的真实课件
    PPTX_FILE=<path>            直接指定 pptx（没解析过就现场解析一次）
两者都没有 → 退回原型自带的 3 页手写课件（保证旧行为不坏）。

课堂脚本（讲解单元）也来自课件目录下的 `plan.json`：
    - 有 plan.json → 用它（LLM 备课的成果）
    - 没有          → 用规则兜底现场生成一份，并**明确告诉使用者质量更差**
                      （`python run.py plan` 可以生成正式的那份）
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agent.variants import attach_variants
from config import ROOT, Settings
from deck.schema import DeckDoc
from deck.store import deck_dir, deck_json, load_deck, media_dir, plan_json
from director.route import Route
from models import LessonScript
from script import build_script
from script_gen import LessonPlan, build_lesson_script, measure_plan, plan_rule


@dataclass
class Course:
    """一节课的全部输入。"""

    script: LessonScript
    mode: str = "simple"                 # simple（手写） | deck（真实 pptx）
    deck: DeckDoc | None = None
    plan: LessonPlan | None = None
    notes: list[str] = field(default_factory=list)

    # -- 便捷 ---------------------------------------------------------------
    @property
    def deck_id(self) -> str:
        return self.deck.deck_id if self.deck else ""

    @property
    def anchor_report(self) -> dict:
        if self.deck is None or self.plan is None:
            return {}
        return self.plan.coverage or measure_plan(self.deck, self.plan)

    def media_path(self, name: str) -> Path | None:
        if self.deck is None:
            return None
        p = (media_dir(self.deck.deck_id) / name).resolve()
        # 只允许取课件目录下的文件（前端传来的名字不可信）
        base = media_dir(self.deck.deck_id).resolve()
        if base not in p.parents or not p.is_file():
            return None
        return p

    def describe(self) -> str:
        if self.deck is None:
            return f"手写演示课件（{self.script.slide_count} 页 / {len(self.script.units)} 单元）"
        src = self.plan.source if self.plan else "?"
        return (f"真实课件《{self.deck.title}》（{self.deck.slide_count} 页 / "
                f"{len(self.script.units)} 单元 / 脚本来源 {src}）")


def _ensure_deck(settings: Settings, notes: list[str]) -> DeckDoc | None:
    """按配置拿到 DeckDoc：优先 deck_id，其次 pptx_file（必要时现场解析）。"""
    if settings.deck_id:
        try:
            return load_deck(settings.deck_id)
        except FileNotFoundError as e:
            notes.append(f"❌ {e}")
            return None
    if not settings.pptx_file:
        return None
    src = Path(settings.pptx_file)
    if not src.is_file():
        notes.append(f"❌ PPTX_FILE 指向的文件不存在：{src}")
        return None
    from deck.pptx_ingest import ingest_pptx   # 懒导入：没接真实课件时不必依赖 python-pptx
    from deck.store import make_deck_id

    deck_id = make_deck_id(src)
    if deck_json(deck_id).exists():
        deck = load_deck(deck_id)
        if deck.source_sha1 == _sha1(src):
            notes.append(f"✅ 复用已解析的课件 {deck_id}（deck.json 已是最新）")
            return deck
        notes.append(f"↻ 课件 {src.name} 已改动，重新解析…")
    if not src.is_file():
        notes.append(f"❌ 找不到 pptx：{src}")
        return None
    notes.append(f"⏳ 正在解析 {src.name}（首次启动需要几秒）…")
    deck = ingest_pptx(src, deck_id=deck_id)
    return deck


def _sha1(p: Path) -> str:
    from deck.schema import sha1_file

    try:
        return sha1_file(p)
    except Exception:
        return ""


def parse_plan_routes(raw, total_nodes: int) -> tuple[list[Route], int]:
    """plan.json 顶层的 routes 数组 → Route 对象；返回 (通过的, 丢弃的条数)。

    为什么一律丢弃而不是抛：这是一份**人可编辑**的产物（备课期写盘、人工审核、可能手改）。
    一条脏路线不该让整节课起不来 —— 课堂起不来的代价比少两条岔路大得多。
    判非法的四类（都在这里一刀切掉，和 Route.validate 同一个口径）：
      · 不是对象 / kind 不在三种走向里（Route 构造就会抛）
      · 结构越界（fork/merge/total 对不上、revisit 没给 steps）
      · route_id 不带 p- 前缀（那是运行期 C 的 r-… 命名，两边撞 id 会互相覆盖，
        而且「这条是预置的」这个信息就丢了）
      · route_id 重复（同一条走向出现两次，J 在边界上会看见两个一模一样的选项）
    """
    if not isinstance(raw, (list, tuple)):
        return [], 0
    out: list[Route] = []
    seen: set[str] = set()
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        try:
            route = Route.from_dict(item)
            route.validate(total_nodes)
        except Exception:            # kind 非法 / 结构越界 / 字段读不出来
            dropped += 1
            continue
        if not route.route_id.startswith("p-") or route.route_id in seen:
            dropped += 1
            continue
        seen.add(route.route_id)
        out.append(route)
    return out, dropped


def _attach_missing_variants(plan: LessonPlan, deck: DeckDoc, settings: Settings,
                             notes: list[str]) -> None:
    """**旧计划专用**：给 plan 里没有变体的单元在内存里补挂变体（§4.6 / §5.5）。

    就地变体（v1 换说法 / v2 举例子）已**停产**：它与"学生没懂时状态机实时调模型重讲"
    冲突，而且按产品口径它从来就不该算分支。备课期现在产出的是**预置路线分支**
    （plan.json 的 routes，见 agent/routes.py）。

    那为什么这段还留着：**旧 plan.json 没有 routes 键**，那些课件一旦不再补挂变体，
    候选池就空了（J 一次都不会切换），而既有验收里"真实课件 177 个单元都有变体"这条
    是按这份旧计划写的。所以这里只对"没有 routes 键的旧计划"开放，新计划一律不走这条路
    （load_course 里的判断就是这一条），停产对新产出的计划是真的停了。

    为什么在加载时补，而不是要求重新备一次课：真实课件的 plan.json 是 LLM 生成的
    （重新生成要花钱、要等几十分钟），而变体是确定性规则模板（`agent/variants.py`）。
    不补的后果不是"少几个选项"，而是候选池**永远是空的** —— J 一次都不会切换，
    整条「双脑三体」链路在真实课件上静默失效（实测：一整节课 0 次切换）。
    补挂只改内存里的 plan：plan.json 一个字节都不写，老课件不改文件也能立即生效。

    为什么保留已有的变体：阶段 1.5 会把规则模板换成 LLM 生成并写进 plan.json，
    那时缺的只是新加的几个单元 —— 无差别重挂会把 LLM 写得更好的那些覆盖成模板。
    """
    #: 先记下已经有的（plan.json 里 LLM 生成的），重挂后原样放回。
    #: 用 id(u) 当键：plan 阶段还没有 unit_id（它在 build_lesson_script 里才编号）。
    keep = {id(u): list(u.variants) for u in plan.units if u.variants}
    attach_variants(plan, deck, char_warn=settings.unit_char_warn)
    for u in plan.units:
        if keep.get(id(u)):
            u.variants = keep[id(u)]
    covered = sum(1 for u in plan.units if u.variants)
    total = sum(len(u.variants) for u in plan.units)
    notes.append(f"✅ 变体已在内存补齐：{total} 个变体 / {covered} 个单元"
                 f"（plan.json 未改动；`python run.py variants {deck.deck_id}` 可就地写盘）")


def load_course(settings: Settings, *, allow_rule_fallback: bool = True) -> Course:
    """装配这节课。任何一步失败都**降级而不是崩** —— 课堂起不来比讲得差更糟。"""
    notes: list[str] = []
    deck = _ensure_deck(settings, notes)

    if deck is None:
        if settings.deck_id or settings.pptx_file:
            notes.append("⚠️ 已退回原型自带的手写演示课件")
        return Course(script=build_script(), mode="simple", notes=notes)

    plan: LessonPlan | None = None
    # plan.json 的**原始 dict**：routes 与 units 同级，而 LessonPlan 这个 dataclass 没有装它。
    # 读一次、两处用（LessonPlan.from_dict 只取它认识的键，多余的键它自己会忽略）。
    plan_doc: dict = {}
    #: plan.json 顶层的 routes：None = 这份计划**没有这个键**（旧计划），[] = 有键但没分支。
    #: 两者必须分开：新计划（有键）不再补挂就地变体，旧计划沿用（见下面的判断）。
    preset_routes = None
    pj = plan_json(deck.deck_id)
    if pj.exists():
        try:
            plan_doc = json.loads(pj.read_text(encoding="utf-8"))
            plan = LessonPlan.from_dict(plan_doc)
            notes.append(f"✅ 课堂脚本来源 {plan.source}（{len(plan.units)} 个讲解单元）")
        except Exception as e:
            notes.append(f"⚠️ plan.json 读取失败（{type(e).__name__}: {e}），改用规则兜底")
            plan_doc = {}
    if isinstance(plan_doc, dict) and isinstance(plan_doc.get("routes"), list):
        preset_routes = plan_doc["routes"]
    if plan is None and allow_rule_fallback:
        slide_filter = None
        if settings.pptx_max_slides:
            slide_filter = [s.index for s in deck.slides[:settings.pptx_max_slides]]
        plan = plan_rule(deck, slides=slide_filter)
        notes.append("⚠️ 没有 plan.json，已用**规则兜底**脚本（正文基本是照读课件）。"
                     f"跑 `python run.py plan {deck.deck_id}` 可以让模型备一版真正像老师讲的话。")
    if plan is None:
        return Course(script=build_script(), mode="simple", notes=notes)

    # 就地变体已停产：新计划（带 routes 键）**一律不补挂**，分支由预置路线承载。
    # 只有"没有 routes 键"的旧计划还走这一条（不补就是候选池空着，见 _attach_missing_variants）。
    if preset_routes is None and any(not u.variants for u in plan.units):
        _attach_missing_variants(plan, deck, settings, notes)

    script = build_lesson_script(deck, plan)
    if not script.units:
        notes.append("❌ 脚本里没有任何讲解单元，退回手写演示课件")
        return Course(script=build_script(), mode="simple", notes=notes)

    # 预置路线：plan.json → LessonScript 的**唯一**通道（状态机只拿得到 script）。
    # 脏数据/非法结构在这里被丢弃并计数 —— 上课起不来比少两条岔路糟得多。
    script.routes, dropped = parse_plan_routes(preset_routes, len(script.units))
    if preset_routes is not None:
        notes.append(f"✅ 预置路线 {len(script.routes)} 条（plan.json 的 routes，"
                     f"已逐条过 Route.validate）"
                     + (f"，丢弃 {dropped} 条非法" if dropped else "")
                     + "；就地变体已停产，不再补挂")
    elif any(u.variants for u in plan.units):
        notes.append(f"ℹ️ 这份 plan.json **没有 routes 键**（旧计划）：沿用内存补挂的变体 "
                     f"{sum(len(u.variants) for u in plan.units)} 个；"
                     f"重跑 python run.py plan {deck.deck_id} 会产出预置路线分支")
    return Course(script=script, mode="deck", deck=deck, plan=plan, notes=notes)


def print_course_notes(course: Course) -> None:
    for n in course.notes:
        print(f"[course] {n}")
    print(f"[course] 本节课件：{course.describe()}")
    if course.deck is not None and course.plan is not None:
        r = course.anchor_report
        print(f"[course] 锚点命中率 {r.get('anchor_hit', 0)}/{r.get('anchors', 0)}"
              f" = {r.get('anchor_hit_rate', 0) * 100:.1f}%（段落级 {r.get('para_level_anchors', 0)}）"
              f" · 课件操作 {r.get('ops', 0)} 个")
    sys.stdout.flush()
