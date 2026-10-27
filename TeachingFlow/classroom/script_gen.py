"""课堂脚本生成：真实 PPT → 讲解单元（LectureUnit）。

对应《课堂交互设计》§1 的完整形态：`PPT + 课程大纲 → 教师 Agent 生成课堂脚本`。
原型阶段一用手写脚本（`script.py`）替代，这里把"阶段一"的第一版做出来 ——
因为接入真实 PPT 之后，**没有人手写脚本了**。

两种规划方式，优先级不同
------------------------
1. **LLM 规划**（`plan_llm`）：让模型"备这一节课"。产出的是**老师会说的话**
   （口语、有过渡、贴近课件措辞），锚点指向真实元素/段落 id，并规划分步与指示。
   这是正式路线。
2. **规则兜底**（`plan_rule`）：把课件文字按 80~180 字切成单元，锚点=被覆盖的段落。
   它的正文**基本是照读课件**，教学效果明显更差 —— 但它是**确定性的、离线的**：
   没有模型服务时课堂仍能跑，测试也能跑，而且它给 LLM 规划提供了可对照的基线。

诚实提醒
--------
真实课件里"页"和"讲多久"完全不成比例（本项目的英语课件一页 300 字、要讲两分钟，
封面页 38 字、十秒就过）。所以脚本生成必须**按内容切**，不能按页平均分。
另外真实课件常把 音标/释义/用法/搭配/例句 全塞进一个文本框 ——
锚点要细到**段落**（`s04-sh02-p03`），否则只能整块高亮，讲解就没有指向性。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from deck.ops import DeckOp, validate_ops, check_ops_in_range, check_ops_text
from deck.schema import (
    KIND_PICTURE,
    KIND_TABLE,
    ROLE_DECOR,
    ROLE_TITLE,
    DeckDoc,
    SlideDoc,
)
from models import LessonScript, Slide, SlideElement
from state import Anchor, LectureUnit, clip_summary

#: 单元长度硬约束（§2.3）：建议 80~180 字，硬上限 250 字
MIN_CHARS = 40
TARGET_CHARS = 130
MAX_CHARS = 180
HARD_MAX_CHARS = 250


# ===========================================================================
# 计划数据结构
# ===========================================================================

@dataclass
class PlanUnit:
    slide: int
    text: str
    anchors: list[dict] = field(default_factory=list)   # Anchor 的 dict 形式
    ops: list[dict] = field(default_factory=list)       # DeckOp 的 dict 形式
    kp: list[str] = field(default_factory=list)
    must_teach: bool = True
    is_page_end: bool = False
    note: str = ""
    #: UnitVariant 的 dict 形式，v0 是主 text，见方案 §4.1/§4.2。
    #: 带默认值 —— 旧 plan.json 没有这个键，`LessonPlan.from_dict` 仍能构造。
    variants: list[dict] = field(default_factory=list)
    #: 走向图卡面上的一句话总结（≤30 字）。
    #: **故意保持可为空**：空 = 这份计划还没补过，回填（backfill_summaries）靠它挑活。
    #: 运行期 LectureUnit 会把空的兜底成正文首句，所以 /graph 拿到的永远非空。
    summary: str = ""

    def est_sec(self) -> float:
        return round(len(self.text) / 5.0, 1)


@dataclass
class LessonPlan:
    deck_id: str = ""
    source: str = "rule"            # rule | llm | manual
    model: str = ""
    session_goals: list[str] = field(default_factory=list)
    units: list[PlanUnit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""
    coverage: dict = field(default_factory=dict)

    def units_of(self, slide: int) -> list[PlanUnit]:
        return [u for u in self.units if u.slide == slide]

    def to_json(self, *, indent: int = 1) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=indent)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @staticmethod
    def from_dict(d: dict) -> "LessonPlan":
        d = dict(d or {})
        d["units"] = [PlanUnit(**u) for u in d.get("units", [])]
        return LessonPlan(**{k: v for k, v in d.items() if k in LessonPlan.__dataclass_fields__})

    @staticmethod
    def load(path: str | Path) -> "LessonPlan":
        return LessonPlan.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ===========================================================================
# 公共：把 DeckDoc 适配成 LessonScript
# ===========================================================================

def slide_from_deck(s: SlideDoc) -> Slide:
    """SlideDoc → models.Slide（给状态机、体检、LLM 上下文用）。

    形状与**段落**都注册成元素：段落元素带 `parent_id`，
    这样 `slide.element("s04-sh02-p03")` 也能查到，
    体检（锚点是否存在）与 `anchors_hint`（喂给 LLM 的可选锚点清单）都不用特判。
    """
    els: list[SlideElement] = []
    for e in s.elements:
        els.append(SlideElement(element_id=e.element_id, kind=e.kind,
                                text=e.plain_text[:200], order=e.z))
        for p in e.paragraphs:
            if p.text.strip():
                els.append(SlideElement(element_id=p.pid, kind="para",
                                        text=p.text[:200], order=e.z,
                                        parent_id=e.element_id))
    return Slide(title=s.title or f"第 {s.index + 1} 页", index=s.index, elements=els)


def anchors_hint_for(s: SlideDoc, *, max_items: int = 40, max_chars: int = 60) -> str:
    """本页可锚定的 id → 文本（喂给 LLM 的"只能从这里挑"清单）。"""
    lines: list[str] = []
    for e in s.elements:
        if e.role == ROLE_DECOR and not e.plain_text.strip():
            continue
        want = e.plain_text.strip() or (f"[{e.kind}]" if e.kind in (KIND_PICTURE, KIND_TABLE) else "")
        if not want:
            continue
        lines.append(f"  {e.element_id} = {want[:max_chars]}".replace("\n", " ⏎ "))
        for p in e.paragraphs:
            if p.text.strip():
                lines.append(f"    {p.pid} = {p.text.strip()[:max_chars]}")
        if len(lines) >= max_items:
            break
    return "\n".join(lines[:max_items])


def build_lesson_script(deck: DeckDoc, plan: LessonPlan) -> LessonScript:
    """计划 → 状态机能跑的 LessonScript（unit_id 在这里统一编号，稳定可复现）。"""
    slides = [slide_from_deck(s) for s in deck.slides]
    units: list[LectureUnit] = []
    for s in deck.slides:
        for i, pu in enumerate(plan.units_of(s.index)):
            units.append(LectureUnit(
                unit_id=f"S{s.index + 1:02d}-U{i + 1:02d}",
                slide=s.index,
                index_in_slide=i,
                text=pu.text,
                anchors=[Anchor(**a) for a in pu.anchors],
                reveal=[op.get("target", "") for op in pu.ops
                        if op.get("op") in ("reveal", "step") and op.get("target")],
                ops=DeckOp.parse_many(pu.ops),
                is_page_end=pu.is_page_end,
                must_teach=pu.must_teach,
                kp=list(pu.kp),
                variants=list(pu.variants),
                summary=pu.summary,
            ))
    return LessonScript(
        course_id=deck.deck_id,
        title=deck.title or deck.deck_id,
        session_goals=list(plan.session_goals),
        slides=slides,
        units=units,
    )


# ===========================================================================
# 逐句标记清单（翻到这一页就先算好，讲到哪句只亮哪一个）
# ===========================================================================

def plan_markers(units: list) -> list[dict]:
    """本页每个锚点落在第几句 —— 翻页时算一次，讲的时候只查表。

    以前是"整个单元一次性批量高亮"：一个单元的几个锚点在**第 1 句**就同时亮起来，
    后面每句学生都已经看到答案了，"老师现在指的是哪一行"就没了。
    要改成逐句亮，就得先知道锚点分别落在第几句；这份清单是纯算术，
    放在翻页时算（一次），不要放在句子边界上算（每句都要算一遍）。

    分配规则：第一个锚点落在第 1 句，之后每个比前一个晚一句 ——
    **同一句绝不塞两个**，同时亮两块等于没有"指哪看哪"。
    锚点数多于句数时，多出来的直接丢掉：它们没有能落脚的句子，
    而硬塞进末句就会回到"一次亮两个"的老问题。

    纯函数：不读状态、不调模型、不碰时钟，同样的输入永远同一个输出。
    """
    out: list[dict] = []
    for u in units:
        # 句数口径必须与状态机逐句下发时一致（split_sentences），
        # 差一句就会让某个锚点永远等不到属于它的那句话。
        n_sent = len(_split_for_ops(u.text))
        # 锚点在 LessonScript 里是 Anchor、在 PlanUnit 里是 dict：
        # 两种都收，调用方不必先转换一遍。
        ids = [a.get("element_id", "") if isinstance(a, dict) else getattr(a, "element_id", "")
               for a in (getattr(u, "anchors", None) or [])]
        for i, eid in enumerate([x for x in ids if x][:n_sent]):
            out.append({"unit_id": u.unit_id, "sentence": i + 1, "element_id": str(eid)})
    return out


# ===========================================================================
# 规则兜底规划（离线、确定性）
# ===========================================================================

#: 真实课件里"这一页是目录/封面"的常见特征
_TOC_HINTS = ("目录", "contents", "content", "大纲", "学习目标", "教学目标")


def _is_cover(s: SlideDoc) -> bool:
    return s.index == 0 or (len(s.elements) <= 4 and len(s.text_flat) < 80)


def _is_toc(s: SlideDoc) -> bool:
    head = (s.title or "") + s.text_flat[:40]
    return any(h in head.lower() or h in head for h in _TOC_HINTS)


def _text_blocks(s: SlideDoc) -> list[tuple[str, str, float]]:
    """本页的"可讲文本块"： (anchor_id, text, 视觉顺序权重)。

    规则：有文字的段落按阅读序；图片单独算一块（讲图也是讲）。
    """
    out: list[tuple[str, str, float]] = []
    for e in s.elements:
        if e.role == ROLE_DECOR:
            continue
        if e.kind == KIND_PICTURE:
            out.append((e.element_id, f"（配图：{e.name or '插图'}）", e.box[1]))
            continue
        for p in e.paragraphs:
            t = p.text.strip()
            if t:
                out.append((p.pid, t, e.box[1] + p.level * 0.01))
    out.sort(key=lambda x: x[2])
    return out


def plan_rule(deck: DeckDoc, *, max_chars: int = MAX_CHARS,
              slides: list[int] | None = None) -> LessonPlan:
    """把课件文字按长度约束切成讲解单元。

    **这是兜底，不是正式方案**：正文基本等于照读课件。
    它的价值是"没有模型也能跑通全链路"，以及给 LLM 规划提供对照基线。
    """
    plan = LessonPlan(deck_id=deck.deck_id, source="rule",
                      created_at=datetime.now().isoformat(timespec="seconds"))
    plan.session_goals = _goals_from_deck(deck)
    for s in deck.slides:
        if slides is not None and s.index not in slides:
            continue
        blocks = _text_blocks(s)
        if not blocks:
            plan.warnings.append(f"⚠️ 第 {s.index + 1} 页没有任何可讲文字（只有装饰形状？）")
            continue
        groups = _group_blocks(blocks, max_chars=max_chars)
        for gi, group in enumerate(groups):
            text = "。".join(b[1].rstrip("。") for b in group) + "。"
            # 每个被覆盖的段落，在"它的文字被念到"的那一句做一次 step：
            # 这既是分步显示，也是"这段话对应课件哪里"的可视证据。
            #
            # 定位必须**在最终正文里按实际分块**算，不能拿各段长度去估：
            # 拼接会去掉多余的"。"、还会并进过短的句子，
            # 估出来的句号会超出实际句数 —— 那个 step 就永远不会执行，
            # 于是"该出现的内容一直不出现"。这一段曾经就是这么错的。
            sents = _split_for_ops(text)
            ops: list[dict] = [
                {"op": "step", "at_sentence": _sentence_index_at(text, _block_offset(group, i), sents),
                 "target": b[0]}
                for i, b in enumerate(group)
            ]
            anchors = [{"element_id": bid, "text_hint": btext[:24], "confidence": 1.0}
                       for bid, btext, _ in group]
            plan.units.append(PlanUnit(
                slide=s.index, text=text, anchors=anchors, ops=ops,
                kp=_kp_for(s, gi),
                must_teach=True,
                is_page_end=(gi == len(groups) - 1),
                note="规则兜底（照读课件）",
                summary=clip_summary(text),
            ))
    _fix_page_end(plan)
    plan.coverage = measure_plan(deck, plan)
    return plan


def _count_sentences(text: str) -> int:
    from llm import split_sentences

    return len(split_sentences(text))


def _split_for_ops(text: str) -> list[str]:
    """按**实际播放分块**切句（= 状态机逐句下发时的口径）。"""
    from llm import split_sentences

    return split_sentences(text)


def _block_offset(group: list[tuple[str, str, float]], i: int) -> int:
    """第 i 个文本块在最终正文里的起始下标。

    正文是 `"。".join(块.rstrip("。"))`，所以第 i 块前面有
    sum(len(前面各块)) + i 个字符（i 个连接用的"。"）。
    """
    return sum(len(g[1].rstrip("。")) for g in group[:i]) + i


def _sentence_index_at(text: str, offset: int, sents: list[str]) -> int:
    """正文里的第 offset 个字符落在第几句（1 起算）。

    分块是原文的连续子串（只可能被 strip 掉首尾空白），所以顺序 `find` 就能对上；
    对不上时退化成"夹在前后两块之间"的位置判断，绝不返回越界值。
    """
    if not sents:
        return 1
    pos = 0
    for i, s in enumerate(sents):
        j = text.find(s, pos)
        if j < 0:                      # 理论上不会发生，兜底别跑飞
            j = pos
        if offset < j + len(s):
            return i + 1
        pos = j + len(s)
    return len(sents)


def _group_blocks(blocks: list[tuple[str, str, float]], *, max_chars: int) -> list[list]:
    """按长度与语义把文本块分组成单元。

    长度不是唯一判据：真实课件里"音标/释义/用法/搭配/例句"这种并列项，
    一条就是一次完整的讲解起落。所以**遇到明显的并列结构就断单元**，
    只要当前单元已经够长（≥ MIN_CHARS）就切，不硬凑到 180 字。
    """
    groups: list[list] = []
    cur: list = []
    cur_len = 0
    for b in blocks:
        blen = len(b[1])
        if cur and cur_len + blen > max_chars:
            groups.append(cur); cur = []; cur_len = 0
        cur.append(b); cur_len += blen
        labels = ("音标：", "词性：", "释义：", "用法：", "搭配：", "例句：",
                  "动词", "名词", "形容词", "副词", "练习", "答案", "解析")
        if cur_len >= MIN_CHARS and any(l in b[1][:12] for l in labels):
            groups.append(cur); cur = []; cur_len = 0
    if cur:
        groups.append(cur)
    return groups


def _kp_for(s: SlideDoc, gi: int) -> list[str]:
    """页级知识点 id。真实课件没有 kp 字段，用 页号 + 标题片段 派生，稳定可复现。"""
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "", (s.title or ""))[:12]
    return [f"P{s.index + 1:02d}" + (f"-{slug}" if slug else "") + (f"-{gi + 1}" if gi else "")]


def _goals_from_deck(deck: DeckDoc) -> list[str]:
    """课堂目标：优先从目录页取，取不到就用"页标题前几条"。"""
    for s in deck.slides[:6]:
        if _is_toc(s):
            items = [p.strip() for p in s.text_flat.split("\n") if len(p.strip()) >= 4
                     and not any(h in p.lower() for h in _TOC_HINTS)]
            items = [re.sub(r"^\d+[\.、]?\s*", "", x) for x in items]
            if items:
                return items[:6]
    titles = [s.title for s in deck.slides if s.title and not _is_cover(s)]
    return titles[:5] or ["讲完本课件的主要内容"]


def _fix_page_end(plan: LessonPlan) -> None:
    """把 is_page_end 修正为"每页最后一条为 True，其余 False"（体检按这个判）。"""
    by_slide: dict[int, list[PlanUnit]] = {}
    for u in plan.units:
        by_slide.setdefault(u.slide, []).append(u)
    for us in by_slide.values():
        for i, u in enumerate(us):
            u.is_page_end = (i == len(us) - 1)


# ===========================================================================
# 度量（§7.6 / §8 V7：锚点生成命中率）
# ===========================================================================

def measure_plan(deck: DeckDoc, plan: LessonPlan) -> dict:
    """锚点命中率 —— 文档 §8 的 V7 一直是"需要真实 PPT 才能测"，这里把它测出来。"""
    index = deck.anchor_index()
    n_units = len(plan.units)
    units_with_anchor = 0
    n_anchors = 0
    hit = 0
    para_level = 0
    low_conf = 0
    op_total = 0
    op_bad = 0
    for u in plan.units:
        if u.anchors:
            units_with_anchor += 1
        for a in u.anchors:
            n_anchors += 1
            aid = a.get("element_id", "")
            if aid in index:
                hit += 1
                if "-p" in aid:
                    para_level += 1
            if float(a.get("confidence", 1.0)) < 0.6:
                low_conf += 1
        op_total += len(u.ops)
        op_bad += len(validate_ops(DeckOp.parse_many(u.ops), set(index.keys())))
        # 只把"❌ 永远不会发生"算作非法；⚠️ 近似定位只是提醒（`check_ops_text` 里区分）
        op_bad += sum(1 for m in check_ops_text(DeckOp.parse_many(u.ops), u.text,
                                                unit_id=f"P{u.slide + 1:02d}")
                      if m.startswith("❌"))
    chars = sum(len(u.text) for u in plan.units)
    est = sum(u.est_sec() for u in plan.units)
    longest = max((u.est_sec() for u in plan.units), default=0.0)
    over = sum(1 for u in plan.units if len(u.text) > HARD_MAX_CHARS)
    return {
        "slides": deck.slide_count,
        "units": n_units,
        "chars": chars,
        "est_sec": round(est, 1),
        "longest_unit_sec": longest,
        "units_over_hard_max": over,
        "units_with_anchor": units_with_anchor,
        "anchors": n_anchors,
        "anchor_hit": hit,
        "anchor_hit_rate": round(hit / max(1, n_anchors), 3),
        "para_level_anchors": para_level,
        "low_confidence_anchors": low_conf,
        "ops": op_total,
        "ops_invalid": op_bad,
    }


# ===========================================================================
# LLM 规划
# ===========================================================================

PROMPT_HEAD = """你是一位中学老师的**备课助手**。老师要对一份真实的 PPT 课件讲课，
你要把这一页拆成若干"讲解单元"。

讲解单元 = 老师**一口气讲完的一段完整的话**，会让位给学生提问。
这是最重要的约束：一段话 80~180 字（约 15~30 秒语音），硬上限 250 字。
超过这个长度，学生等老师讲完再提问会等到忘记自己要问什么。

规则（必须遵守）：
1. 你写的是**老师开口说的话**，不是课件摘要。要口语、自然，像真人在讲课。
   不要写 Markdown、不要写小标题、不要出现"本页""以下""如图所示"这类书面语。
2. **术语与符号必须和课件一致**（课件写 pack up，你就说 pack up）。
   学生要能在屏幕上找到你在说的那一行。
3. `anchors` 只能从下面给出的 id 清单里挑，**优先挑段落 id**（以 -pNN 结尾的），
   因为段落级锚点才能指到"例句那一行"。每句话锚 1~3 个。
4. `ops` 是课件操作，用 `at_text` 说明"讲到哪句话时做"：
   **`at_text` 要填你自己正文里出现过的一小段原文（6~15 个字），系统靠它对齐语音。**
   不要用 `at_sentence`（系统切句规则你猜不准，数句子必然出错）。
   - `step`：让学生**此刻才看到**这块内容（+其余压暗）。用于"先别急着看答案"的场景，
     比如讲单词时先放音标与释义，讲到例句时再放例句。
     **凡是被 step/reveal 指到的内容，学生在此前是看不到的**，所以只有当内容确实不该提前出现时才用。
   - `highlight`：高亮（讲解焦点已经显而易见时用）。
   - `point`：指向配图/某个元素（讲图时用）。
   - `focus`：把其余内容压暗，只留这一块亮着 —— 用于"别的先别管，只看这一行"，
     比如一行里要抠单个词、或一句例句里要挑出搭配。
   - `zoom`：放大某块（文字密集或图表时用）。
   - 复位：`unfocus`（取消 focus）、`reset_zoom`（取消 zoom）。
     **只在你真的做过 focus/zoom 之后才写复位**；没放大过就写复位 = 空操作，
     系统会把它清掉（清理记录会出现在体检报告里）。
   每个单元最多 4 个 op，够用就行。不要每句话都 highlight —— 一直闪反而没重点。
   例（**格式示意，文字必须换成你自己正文里的原话**）：
   {"op":"step","target":"s04-sh02-p06","at_text":"<你正文里的 6~15 字>"}
5. 最后一个单元要收尾：`is_page_end` 为 true（其余为 false）。
6. `kp` 是知识点 id（英文或拼音短标签，如 `pack-usage`），一页 1~3 个，全课复用同一套命名。
7. 如果这一页是封面/目录/纯过渡页，就写 1 个简短单元（30~80 字），别硬凑。
8. 每个单元再带一句 `summary`（**≤30 字**）：这是给老师看的"这一格在讲什么"，
   不是念给学生听的话 —— 不要句末标点、不要换行、不要写"本单元""这一页"。
   例："pack 的三个搭配" / "用例句区分 pack 与 pack up"。
   走向图上这一格只显示这句话，写长了会被截掉。

只输出 JSON，不要解释、不要代码块围栏：
{"units":[{"text":"...","summary":"...","anchors":["id1"],"ops":[],"kp":["..."],
"is_page_end":true}]}
"""


def prompt_slide(deck: DeckDoc, s: SlideDoc, *, goals: list[str],
                 prev_tail: str = "") -> list[dict]:
    """给一页课件做备课的 prompt。"""
    body = [
        f"# 课件《{deck.title}》共 {deck.slide_count} 页",
        "## 课堂目标\n" + "；".join(goals),
    ]
    if prev_tail:
        body.append("## 上一页最后讲到这里（保持衔接，别重复）\n" + prev_tail)
    body.append(
        f"## 现在要备第 {s.index + 1} 页\n"
        f"标题：{s.title or '（无标题）'}\n"
        f"页面文字（按阅读序）：\n{s.text_flat[:1500]}\n\n"
        f"## 本页可用的锚点 id（只能用这些）\n{anchors_hint_for(s)}"
    )
    return [
        {"role": "system", "content": PROMPT_HEAD},
        {"role": "user", "content": "\n".join(body)},
    ]


def _fallback_anchor(s: SlideDoc) -> str:
    """模型一个锚点都没给时，兜底锚一个**整块**，而不是随便某一行。

    两件事都不对：

    1. 以前是 `next(iter(para_ids))` —— `para_ids` 是 **set**，所谓"第一个"是
       哈希顺序，**不确定**。同一页两次备课可能锚到不同的行。
    2. 更糟的是精确度：实测第 16 页一个收尾单元"好，关于 community 的用法就讲到这里，
       大家记一下这些搭配"，兜底锚到了 `s16-sh02-p05`（正好是"community center 社区中心"
       那一行）。学生会以为老师只让记这一条，而其实讲的是整块搭配。

    所以兜底选**正文里字数最多的那个元素**（形状级 id）：它一定覆盖了"这一页的主要内容"，
    粗但不会说错；低置信度标记仍然保留，体检里照样提醒人工确认。
    """
    best, best_len = "", -1
    for e in s.elements:
        n = sum(len(p.text or "") for p in (e.text.paras if e.text else []))
        if n > best_len:
            best, best_len = e.element_id, n
    if best:
        return best
    # 没有文本元素（纯图片页）：退回确定性最小 id，至少保证同一页每次结果一致
    ids = sorted(s.anchor_index().keys())
    return ids[0] if ids else ""


def parse_units_json(raw: str, s: SlideDoc, deck: DeckDoc) -> tuple[list[PlanUnit], list[str]]:
    """把模型输出变成 PlanUnit，并把"模型说得对但写得不对"的地方修好。

    修而不抛：本地小模型的 JSON 一定会有瑕疵，**能修的修，修不了的报出来**，
    绝不让一条坏数据把整节课带崩（与 `machine._collect_clean` 同一个思路）。
    """
    from llm import _extract_json

    warns: list[str] = []
    try:
        data = json.loads(_extract_json(raw))
    except Exception as e:
        return [], [f"❌ 第 {s.index + 1} 页：模型输出不是合法 JSON（{e}）"]
    raw_units = data.get("units") if isinstance(data, dict) else data
    if not isinstance(raw_units, list) or not raw_units:
        return [], [f"❌ 第 {s.index + 1} 页：模型没有给出 units"]

    valid_ids = set(s.anchor_index().keys())
    idx_of = s.anchor_index()
    para_ids = {p for p in valid_ids if "-p" in p}
    fallback_anchor = _fallback_anchor(s)
    out: list[PlanUnit] = []
    for i, ru in enumerate(raw_units):
        if not isinstance(ru, dict):
            warns.append(f"⚠️ 第 {s.index + 1} 页第 {i + 1} 个单元不是对象，已丢弃")
            continue
        text = str(ru.get("text") or "").strip()
        if not text:
            warns.append(f"⚠️ 第 {s.index + 1} 页第 {i + 1} 个单元正文为空，已丢弃")
            continue
        # 总结：模型没写、写超 30 字、写成一句话正文，都在这里收敛成同一种东西。
        # 宁可显示"正文首句"，也不能在走向图的卡面上留一格空白。
        summary = clip_summary(ru.get("summary") or "") or clip_summary(text)
        # 锚点：只保留存在的；全丢光就给一个兜底锚点（宁可粗，不可无）
        anchors: list[dict] = []
        for a in (ru.get("anchors") or []):
            aid = a if isinstance(a, str) else str(a.get("element_id") or "")
            aid = aid.strip()
            if not aid:
                continue
            if aid not in valid_ids:
                warns.append(f"⚠️ 第 {s.index + 1} 页：锚点 {aid} 不存在，已丢弃")
                continue
            anchors.append({"element_id": aid, "text_hint": idx_of.get(aid, "")[:24],
                            "confidence": 1.0})
        if not anchors and fallback_anchor:
            anchors.append({"element_id": fallback_anchor,
                            "text_hint": idx_of.get(fallback_anchor, "")[:24],
                            "confidence": 0.4})
            warns.append(f"⚠️ 第 {s.index + 1} 页第 {i + 1} 个单元没有有效锚点，"
                         f"已兜底到 {fallback_anchor}（低置信度）")
        ops_raw = ru.get("ops") or []
        ops = DeckOp.parse_many(ops_raw)
        n_sent = _count_sentences(text)
        for op in ops:                      # 句号超范围 → 夹到范围内（否则永远不会发生）
            if op.at_sentence > n_sent:
                warns.append(f"⚠️ 第 {s.index + 1} 页：op {op.op} 的 at_sentence="
                             f"{op.at_sentence} 超出 {n_sent} 句，已夹到末句")
                op.at_sentence = n_sent
        for m in validate_ops(ops, valid_ids):
            warns.append(f"⚠️ 第 {s.index + 1} 页：{m}")
        ops = [o for o in ops if not validate_ops([o], valid_ids)]
        kp = [str(k).strip() for k in (ru.get("kp") or []) if str(k).strip()][:3]
        out.append(PlanUnit(
            slide=s.index,
            text=text,
            anchors=anchors,
            ops=[o.to_dict() for o in ops],
            kp=kp or _kp_for(s, i),
            must_teach=bool(ru.get("must_teach", True)),
            is_page_end=bool(ru.get("is_page_end", False)),
            note="LLM",
            summary=summary,
        ))
    return out, warns


def _split_long_units(units: list[PlanUnit], char_warn: int = HARD_MAX_CHARS) -> list[PlanUnit]:
    """超硬上限的单元按句切开（否则"学生最坏等待时长"就失控了，§2.3）。"""
    from llm import split_sentences

    out: list[PlanUnit] = []
    for u in units:
        if len(u.text) <= char_warn:
            out.append(u); continue
        sents = split_sentences(u.text)
        cur: list[str] = []
        acc = 0

        def _seg() -> PlanUnit:
            """切出来的一段：正文与操作各自独立，**总结也必须按这一段重算**。

            不能照抄原单元的 summary —— 那说的是整段长话，切完就只有前半段还对得上，
            卡面上会出现"这一格讲了三件事"而实际只讲了一件（variants 当年就踩过这个坑：
            重建 PlanUnit 时漏传 = 字段静默丢掉）。
            """
            text = "".join(cur)
            return PlanUnit(slide=u.slide, text=text, anchors=list(u.anchors),
                            kp=list(u.kp), must_teach=u.must_teach,
                            note=u.note + "（超长已切分）",
                            summary=clip_summary(text))

        for s in sents:
            if cur and acc + len(s) > TARGET_CHARS:
                seg = _seg()
                seg.ops = [o for o in u.ops if o.get("at_sentence") == 1]
                out.append(seg)
                cur, acc = [], 0
            cur.append(s); acc += len(s)
        if cur:
            out.append(_seg())
    return out


async def plan_llm(deck: DeckDoc, llm, *, slides: list[int] | None = None,
                   on_progress=None, char_warn: int = HARD_MAX_CHARS) -> LessonPlan:
    """逐页让 LLM 备课。

    逐页而不是整份一起喂：本地小模型一次输出 42 页必然截断或跑偏；
    一页一次还有个好处 —— 某页失败了只有那页退化，不会毁掉整份脚本。
    """
    plan = LessonPlan(deck_id=deck.deck_id, source="llm",
                      model=(getattr(llm, "settings", None).describe()
                             if getattr(llm, "settings", None) else ""),
                      created_at=datetime.now().isoformat(timespec="seconds"))
    plan.session_goals = _goals_from_deck(deck)
    todo = [s for s in deck.slides if slides is None or s.index in slides]
    prev_tail = ""
    for n, s in enumerate(todo, 1):
        if on_progress:
            on_progress(n, len(todo), s)
        units: list[PlanUnit] = []
        # 失败重试一次：小模型偶发吐不出 JSON，重摇一次的性价比很高
        for attempt in (1, 2):
            try:
                raw = await llm.chat(prompt_slide(deck, s, goals=plan.session_goals,
                                                  prev_tail=prev_tail),
                                     max_tokens=1200, json_mode=True)
            except Exception as e:
                plan.warnings.append(f"❌ 第 {s.index + 1} 页：模型调用失败 {type(e).__name__}: {e}")
                break
            units, warns = parse_units_json(raw, s, deck)
            plan.warnings.extend(warns)
            if units:
                break
            plan.warnings.append(f"⚠️ 第 {s.index + 1} 页第 {attempt} 次尝试无有效单元")
        if not units:      # 这一页彻底失败 → 用规则兜底，别在课堂上留一个空洞
            fallback = plan_rule(deck, slides=[s.index])
            units = fallback.units
            plan.warnings.append(f"⚠️ 第 {s.index + 1} 页已回退为规则兜底")
        plan.units.extend(_split_long_units(units, char_warn))
        if units:
            prev_tail = units[-1].text[-80:]
    _fix_page_end(plan)
    plan.warnings.extend(prune_noop_ops(plan))
    plan.coverage = measure_plan(deck, plan)
    return plan


# ===========================================================================
# 总结回填（存量计划：只补 summary，正文一格都不动）
# ===========================================================================

PROMPT_SUMMARY_HEAD = """你是备课助手。下面按页给出**已经定稿**的讲解单元（老师要说的话），
给每个单元写一句 30 字以内的总结：这是老师在"走向图"上看到的一格标题，不是念给学生听的话。

规则：
1. 只说"这一格在讲什么"，像"pack 的三个搭配""用例句区分 pack 与 pack up"。
2. ≤30 字；不要句末标点、不要换行、不要写"本页""本单元""通过……"。
3. 术语照课件写（课件写 pack up 就写 pack up），别换成同义词。
4. 一个单元配一句，i 与下面的单元序号对齐，不要合并、不要漏。

只输出 JSON，不要解释、不要代码块围栏：
{"summaries":[{"i":1,"summary":"..."}]}
"""


def prompt_summaries(deck: DeckDoc, s: SlideDoc, units: list[PlanUnit]) -> list[dict]:
    """一页的总结 prompt：只喂正文。

    不喂锚点清单与操作 —— 写一句总结用不到它们，而那份清单一页就有几十行，
    白占的正是小模型最紧张的上下文。
    """
    body = [f"# 课件《{deck.title}》第 {s.index + 1} 页" + (f"：{s.title}" if s.title else "")]
    for i, u in enumerate(units, 1):
        body.append(f"## 单元 {i}（{len(u.text)} 字）\n{u.text}")
    return [
        {"role": "system", "content": PROMPT_SUMMARY_HEAD},
        {"role": "user", "content": "\n\n".join(body)},
    ]


def _parse_summaries(raw: str, count: int) -> dict[int, str]:
    """模型输出 → {序号（1 起）: 总结}。坏数据只丢不抛。

    回填是给**存量计划**用的，跑一次要动几百个单元 —— 这里抛一次，
    调用方就得为"模型今天心情不好"再设计一套回滚。所以宽容：认不出的条目直接跳过，
    缺的那一格由调用方按正文截断兜底。
    """
    from llm import _extract_json

    try:
        data = json.loads(_extract_json(raw))
    except Exception:
        return {}
    if isinstance(data, dict):
        items = data.get("summaries") or data.get("units") or []
    else:
        items = data
    if not isinstance(items, list):
        return {}
    out: dict[int, str] = {}
    for k, it in enumerate(items, 1):
        if isinstance(it, str):
            i, text = k, it
        elif isinstance(it, dict):
            try:
                i = int(it.get("i") or it.get("index") or k)
            except (TypeError, ValueError):
                i = k
            text = it.get("summary") or it.get("text") or ""
        else:
            continue
        summary = clip_summary(str(text))
        if summary and 1 <= i <= count:
            out.setdefault(i, summary)
    return out


async def backfill_summaries(deck: DeckDoc, plan: LessonPlan, llm, *,
                             slides: list[int] | None = None,
                             on_progress=None) -> dict:
    """给**已有**计划补 summary，正文 / 锚点 / 操作一格都不动。

    为什么单独一条路：plan.json 是花钱备出来的（真实的 42 页课件要几十分钟），
    而"走向图卡面上要一句话"是后来才有的需求 —— 为了这一行字再备一次课毫无道理。
    这里只写 `units[*].summary`，所以对一份上过课的定稿计划也是安全的。

    只补**空的**：已有总结的单元原样保留（模型润色过的那句不能被规则截断顶掉）。
    要全补由调用方把 summary 清空 —— "补哪些"因此只有一个判据，两种口径不会打架。

    逐页一次调用，且**只对真有活要干的页调**：存量课件通常只差几页，
    为一页缺就整份重跑一遍模型，时间和额度都白花。

    返回 {"updated": 补上的个数, "skipped": 本来就有/被 --slides 挡掉的个数,
    "warnings": [...]}；模型挂了就回落正文截断 —— 回填永远不许失败。
    """
    picked = [u for u in plan.units if slides is None or u.slide in set(slides)]
    targets = [u for u in picked if not str(u.summary or "").strip()]
    warns: list[str] = []
    updated = 0
    pages = [s for s in deck.slides if any(u.slide == s.index for u in targets)]
    for n, s in enumerate(pages, 1):
        pending = [u for u in targets if u.slide == s.index]
        if on_progress:
            on_progress(n, len(pages), s)
        got: dict[int, str] = {}
        try:
            raw = await llm.chat(prompt_summaries(deck, s, pending),
                                 max_tokens=800, json_mode=True)
            got = _parse_summaries(raw, len(pending))
            if not got:
                warns.append(f"⚠️ 第 {s.index + 1} 页：模型没给出可用的总结，已回落正文截断")
        except Exception as e:
            warns.append(f"⚠️ 第 {s.index + 1} 页：总结模型调用失败 {type(e).__name__}: {e}"
                         "，已回落正文截断")
        for i, u in enumerate(pending, 1):
            u.summary = got.get(i) or clip_summary(u.text)
            updated += 1
    # 脏计划里的单元（页号在课件里根本不存在）也照样补上：/graph 里没有空的卡面
    for u in targets:
        if not str(u.summary or "").strip():
            u.summary = clip_summary(u.text)
            updated += 1
            warns.append(f"⚠️ 第 {u.slide + 1} 页有单元不在课件里，已按正文截断补 summary")
    return {"updated": updated, "skipped": len(picked) - updated, "warnings": warns}


#: "复原类"操作 → 它需要的前置状态。没有前置状态时，这个操作**什么都不会发生**。
_NOOP_NEEDS = {"reset_zoom": "zoom", "unfocus": "focus"}


def prune_noop_ops(plan: LessonPlan) -> list[str]:
    """按页清掉"没有前置状态可复原"的操作。

    为什么值得清：模型很爱写"讲完复原"，但那一页**根本没有放大/压暗过** ——
    这种操作发下去什么都不会发生，却会照常计入 "课件操作 下发 N"，
    把真正没命中的操作淹掉（§8 的指标本来是拿来判断备课质量的）。
    实测真实课件的 163 个操作里就有 3 个这样的空 `reset_zoom`。

    只按**同页**判定：翻页时前端会把压暗、高亮、缩放一起重置
    （deck.js `render → resetOps`），所以上一页的 zoom 不会带到下一页，
    复原操作的作用域就是本页。反过来说，这里**不补**"页面结束时的收尾操作" ——
    翻页本来就会重置，补上去只会多一个空操作。

    返回被删掉的说明（调用方写进 warnings / 审核页）。
    """
    dropped: list[str] = []
    for slide in sorted({u.slide for u in plan.units}):
        seen: set[str] = set()
        for u in plan.units_of(slide):
            kept: list[dict] = []
            for op in u.ops or []:
                kind = str(op.get("op") or "")
                need = _NOOP_NEEDS.get(kind)
                if need and need not in seen:
                    dropped.append(
                        f"第 {slide + 1} 页：{kind} 前面没有 {need}，是空操作，已去掉")
                    continue
                kept.append(op)
                if kind in ("zoom", "focus"):
                    seen.add(kind)
            u.ops = kept
    return dropped


# ===========================================================================
# 体检（§2.3 长度 · §2.7 可审核性 · V7 锚点命中率）
# ===========================================================================

def audit_plan(deck: DeckDoc, plan: LessonPlan, char_warn: int = HARD_MAX_CHARS) -> list[str]:
    msgs: list[str] = []
    index = deck.anchor_index()
    for u in plan.units:
        n = len(u.text)
        flag = ""
        if n > char_warn:
            flag = "  ❌ 超出硬上限"
        elif n > MAX_CHARS:
            flag = "  ⚠️  超过建议区间(80~180)"
        elif n < MIN_CHARS:
            flag = "  ⚠️  偏短(<40字)"
        msgs.append(f"[P{u.slide + 1:02d}] {n:>4} 字 · 约 {u.est_sec():>5.1f} 秒{flag}")
        if not u.anchors:
            msgs.append(f"    ⚠️  没有锚点 —— 语音无法与课件元素对应")
        for a in u.anchors:
            aid = a.get("element_id", "")
            if aid not in index:
                msgs.append(f"    ❌ 锚点 {aid} 在课件第 {u.slide + 1} 页上不存在")
            elif float(a.get("confidence", 1.0)) < 0.6:
                msgs.append(f"    ⚠️  低置信度锚点 {aid}（{index[aid][:24]}）—— 建议人工确认")
        ops = DeckOp.parse_many(u.ops)
        msgs.extend("    " + m for m in validate_ops(ops, set(index.keys()),
                                                    unit_id=f"P{u.slide + 1:02d}"))
        # 按文字定位的 op：文字对不上 = 永远不执行，比"句号写错"更隐蔽
        msgs.extend("    " + m for m in check_ops_text(ops, u.text,
                                                       unit_id=f"P{u.slide + 1:02d}"))
        # 按句号定位的 op：句数按**实际播放分块**算（不是数句号）
        n_sent = _count_sentences(u.text)
        msgs.extend("    " + m for m in check_ops_in_range(
            ops, n_sent, unit_id=f"P{u.slide + 1:02d}"))

    # 计划里存的 coverage 是**上一次体检时**快照下来的（为了省掉重算）。
    # 只要计划被改过（清理空操作、部分重备、手工编辑），它就过期了 ——
    # 过期还照报，就会出现"删掉 3 个操作、报告里还是 163 个"这种自相矛盾。
    # 所以这里对一下账：单元数对不上就重算，并说明一句。
    stale = bool(plan.coverage) and plan.coverage.get("units") != len(plan.units)
    cov = measure_plan(deck, plan) if (stale or not plan.coverage) else plan.coverage
    msgs.append("")
    if stale:
        msgs.append(f"（计划被改动过，体检数字已重算：旧记录 {plan.coverage.get('units')} 个单元 "
                    f"→ 现在 {len(plan.units)} 个）")
    msgs.append(f"来源 {plan.source} · 页数 {deck.slide_count} · 单元数 {cov['units']} · "
                f"总时长约 {cov['est_sec']:.0f} 秒（{cov['est_sec'] / 60:.1f} 分钟）")
    msgs.append(f"最长单元 {cov['longest_unit_sec']:.1f} 秒 —— 即学生最坏等待时长"
                + ("  ❌ 过长" if cov["longest_unit_sec"] > 45 else ""))
    msgs.append(f"超硬上限单元 {cov['units_over_hard_max']} 个")
    msgs.append(f"**锚点命中率 {cov['anchor_hit']}/{cov['anchors']} = "
                f"{cov['anchor_hit_rate'] * 100:.1f}%**（其中段落级 {cov['para_level_anchors']} 个，"
                f"低置信度 {cov['low_confidence_anchors']} 个）")
    # 操作类型分布要一起印出来：只看总数看不出"备课其实只用了一种操作"
    # （实测第一版全课 160 个操作里只有 step/highlight，一个 focus/zoom 都没有 ——
    #  而它们是"指哪看哪"最该用的两个，不看分布就发现不了）
    kinds: dict[str, int] = {}
    for u in plan.units:
        for op in u.ops or []:
            k = str(op.get("op") or "")
            kinds[k] = kinds.get(k, 0) + 1
    mix = " / ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1]))
    msgs.append(f"课件操作 {cov['ops']} 个 · 非法 {cov['ops_invalid']} 个"
                + (f"（{mix}）" if mix else ""))
    covered = {u.slide for u in plan.units}
    missing = [s.index + 1 for s in deck.slides if s.index not in covered]
    if missing:
        msgs.append(f"⚠️ 这些页没有任何讲解单元：{missing}")
    if plan.warnings:
        msgs.append("")
        msgs.append(f"生成过程中的告警 {len(plan.warnings)} 条（前 12 条）：")
        msgs.extend("  " + w for w in plan.warnings[:12])
    return msgs
