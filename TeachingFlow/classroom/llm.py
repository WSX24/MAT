"""LLM Provider 层 + 课堂 Prompt。

两个服务商共用同一套 OpenAI 兼容实现，只换 base_url / key / model：
  - ollama   : 本地部署，零成本，离线可用
  - opencode : OpenCode Zen 网关（OpenAI 兼容，按量付费）
  - opencode_go : OpenCode Go 订阅套餐网关（OpenAI 兼容，端点 /zen/go/v1）

服务商在这里只承担四件事：提问分流、生成回答、换讲法重讲、课堂上下文装配。
状态机（machine.py）不依赖本模块的任何具体实现。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from config import Settings
from models import LessonScript
from state import QAction, QKind, Question

# ===========================================================================
# 文本工具
# ===========================================================================

#: 句末标点（不切断小数点：靠右侧必须是标点才算边界）
SENT_END = "。！？!?；;…"
SECONDARY_END = "，,、：:"


#: 小模型常把自己的"分层标题"一起吐出来，这些词必须整行丢掉
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.*)$")
_MD_BULLET = re.compile(r"^\s{0,3}(?:[-*+•]|\d+[.、)])\s+")
#: `**教师答**` / `__要点__` 这种独立成行的强调块
_MD_ONLY_MARK = re.compile(r"^\s*(?:\*\*|__|##+)\s*(.*?)\s*(?:\*\*|__|##+)\s*$")

#: 标题本身长这样时，"内容"其实在标题里（例如 `# 教师答：判别式……`）
_HEADING_KEEP = re.compile(
    r"^(?:教师答|老师答|回答|答|参考答案|教师回应|教师)\s*[:：]?\s*(.*)$"
)
#: 这些标题是模型在模仿 prompt 结构，整行丢掉
_HEADING_DROP = re.compile(
    r"^(?:约束|规则|要求|说明|学生问|学生说|问|题干|背景|补充|策略|任务|格式|"
    r"注意|注意点|知识点|要点|提示|输出|结论|总结)\s*[:：]?\s*$"
)
#: 这些是"主持串场"的套话，和状态机自己的过渡话术会撞车，删掉
#: 注意前缀要宽松：模型写的是"好的，我们接着看这一页。"，
#: 而只认"好，"的正则会把这一句整句放过去（实测泄漏过一次）。
_BOILERPLATE = re.compile(
    r"^(?:(?:好(?:的|吧|了)?|嗯|那|行)[，,]?\s*)?"
    r"(?:(?:我们|咱们|咱|大家)(?:就)?(?:先)?)?"
    r"(?:接着往下看|往下看|接着往下讲|往下讲|接着往下说|接着看这一页|接着看|"
    r"继续说|回到这一页|回到刚才的位置|回到主线|继续往下|往下)[，。！!]?$"
)
#: 包在整个句子外面的**直**引号（模型爱把回答包起来）；
#: 中文引号"" 是有意义的引用，不动。
_WRAPPED = re.compile(r'^["\']+|["\']+$')
#: 模型爱加的执行旁白，例如"(停顿)"。注意别误伤数学里的括号 —— 只删纯旁白。
_NARRATION = re.compile(r"[（(](?:停顿|稍等|学生|提问|注|备注|笑)[^）)]{0,12}[）)]")
#: prompt 自身的结构词被模型抄进正文 —— 这些行整句丢掉
#: （小模型会把"当前任务：……策略是「换说法」""关键 只针对卡住的那一小块"当成要念的内容）
_PROMPT_ECHO = re.compile(
    r"当前任务|本轮要求|策略是|策略[:：]|重讲[:：]|"
    r"只针对卡住|不要重讲整页|说完让学生|让学生有个着落|不要重复原来的措辞|"
    r"被打断的位置|你被打断的|当前讲的内容|学生刚才说的原话|直接输出你"
)


def sanitize_spoken(text: str) -> str:
    """把 LLM 输出净化成**可以念出来**的一句话。

    这一步是确定性的，不依赖模型自觉 —— 小模型几乎一定会往回答里塞
    标题、列表符号、复述 prompt 结构的内容，这些一旦进了 TTS 就是教学事故。

    处理：
      1. 丢 markdown 标题行；但 `# 教师答：xxx` 这种把内容写在标题里的，留下内容
      2. 去掉行首的项目符号 / 序号
      3. 丢掉空话式的"我们接着往下看"（状态机的重入话术会负责这件事）
      4. 丢掉 ``` 代码栅栏
    """
    if not text:
        return ""

    out: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        raw = line.rstrip()
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        m = _MD_HEADING.match(raw)
        inner = m.group(1).strip() if m else None
        if inner is None:
            m2 = _MD_ONLY_MARK.match(raw)
            if m2:
                inner = m2.group(1).strip()

        if inner is not None:
            if _HEADING_DROP.match(inner) or not inner:
                continue                     # 整行是结构标题 → 丢
            keep = _HEADING_KEEP.match(inner)
            raw = (keep.group(1).strip() if keep else inner)
            if not raw:
                continue

        raw = _MD_BULLET.sub("", raw).strip()
        if not raw:
            continue
        if _BOILERPLATE.match(raw) or _PROMPT_ECHO.search(raw):
            continue
        out.append(raw)

    joined = " ".join(out).strip()
    joined = _NARRATION.sub("", joined).strip()
    return _WRAPPED.sub("", joined).strip()


def drop_boilerplate(sentences: list[str]) -> list[str]:
    """切句之后再过一遍串场套话。

    为什么按行过滤不够：`sanitize_spoken` 会把多行拼成一行（"好，我们接着看这一页。
    判别式……"），整行不再是套话，`_BOILERPLATE` 的整行匹配就漏了。
    重入话术由状态机自己说，模型跟着说一遍学生听到的就是复读。
    """
    return [s for s in sentences if s.strip() and not _BOILERPLATE.match(s.strip())]


def clamp_spoken(text: str, max_chars: int) -> str:
    """把话术压到长度上限内（在句末标点处截断，宁可短不要念半句）。"""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"),
              head.rfind("；"), head.rfind("，"))
    return (head[:cut + 1] if cut >= max_chars * 0.4 else head).strip()


def split_sentences(text: str, max_len: int = 70) -> list[str]:
    """把讲解文本切成句 —— 音频切分、进度追踪、让位边界都以句为单位。

    过长的句子在次级标点处二次切分，保证没有任何一句长得无法作为让位点。
    """
    text = (text or "").strip()
    if not text:
        return []

    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENT_END and len(buf) >= 8:
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())

    out: list[str] = []
    for p in parts:
        if len(p) <= max_len:
            out.append(p)
            continue
        # 二次切分
        chunk = ""
        for ch in p:
            chunk += ch
            if ch in SECONDARY_END and len(chunk) >= max_len * 0.6:
                out.append(chunk.strip())
                chunk = ""
        if chunk.strip():
            out.append(chunk.strip())
    return [s for s in out if s]


# ===========================================================================
# 课堂上下文：喂给 LLM 与 MCP 外脑的"环境认知"（ContextSnapshot）
# ===========================================================================

@dataclass(init=False)
class ContextSnapshot:
    """精简课堂上下文快照（对应《MCP方案设计》§6.1）。

    已清除 5 个从不进入提示词的死字段：
      anchors_hint / next_unit_hint / goals / slide_index / unit_id
    新增连续性记忆字段（修复 G2 记忆不回灌、G3 学生状态零引用）：
      version / comprehension / deferred / recent_qa / q_rounds
    """

    version: dict[str, Any] = field(default_factory=dict)
    slide: dict[str, Any] = field(default_factory=dict)
    unit: dict[str, Any] = field(default_factory=dict)
    prev_unit_text: str = ""
    covered_kp: list[str] = field(default_factory=list)
    comprehension: dict[str, str] = field(default_factory=dict)
    deferred: list[str] = field(default_factory=list)
    recent_qa: list[dict[str, str]] = field(default_factory=list)
    q_rounds: int = 0

    def __init__(
        self,
        *,
        version: dict[str, Any] | None = None,
        slide: dict[str, Any] | None = None,
        unit: dict[str, Any] | None = None,
        prev_unit_text: str = "",
        covered_kp: list[str] | None = None,
        comprehension: dict[str, str] | None = None,
        deferred: list[str] | None = None,
        recent_qa: list[dict[str, str]] | None = None,
        q_rounds: int = 0,
        # 兼容旧测试构造点（传入后折算进 slide/unit/version，本身不作为死字段保留）
        script: LessonScript | None = None,
        slide_index: int = 0,
        slide_title: str = "",
        slide_text: str = "",
        unit_id: str = "",
        unit_text: str = "",
        covered: list[str] | None = None,
        **_ignored_legacy: Any,
    ) -> None:
        total_slides = script.slide_count if script is not None else 1
        self.slide = slide if slide is not None else {
            "index": slide_index,
            "total": total_slides,
            "title": slide_title,
            "text": slide_text,
        }
        self.unit = unit if unit is not None else {
            "id": unit_id,
            "text": unit_text,
        }
        self.version = version if version is not None else {
            "slide": int(self.slide.get("index", 0)) + 1,
            "unit": str(self.unit.get("id", "")),
            "seq": 0,
        }
        self.prev_unit_text = prev_unit_text
        base_cov = covered_kp if covered_kp is not None else (covered or [])
        self.covered_kp = list(base_cov)[-10:]
        self.comprehension = dict(comprehension or {})
        self.deferred = list(dict.fromkeys(deferred or []))
        self.recent_qa = list(recent_qa or [])[-3:]
        self.q_rounds = int(q_rounds or 0)

    @property
    def slide_title(self) -> str:
        return str(self.slide.get("title", ""))

    @property
    def slide_text(self) -> str:
        return str(self.slide.get("text", ""))

    @property
    def unit_text(self) -> str:
        return str(self.unit.get("text", ""))

    @property
    def slide_plan(self) -> str:
        idx = int(self.slide.get("index", 0)) + 1
        total = int(self.slide.get("total", 1))
        return f"第 {idx} 页 / 共 {total} 页"

    @property
    def covered(self) -> list[str]:
        return self.covered_kp

    def digest(self) -> str:
        """事件信封里的精简摘要（不塞完整快照，省 prefill）。"""
        page = int(self.slide.get("index", 0)) + 1
        uid = str(self.unit.get("id", ""))
        title = self.slide_title or self.unit_text[:20] or "课堂讲授"
        return f"第 {page} 页（{uid or '当前段'}）：{title[:28]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": dict(self.version),
            "slide": dict(self.slide),
            "unit": dict(self.unit),
            "prev_unit_text": self.prev_unit_text,
            "covered_kp": list(self.covered_kp),
            "comprehension": dict(self.comprehension),
            "deferred": list(self.deferred),
            "recent_qa": [dict(x) for x in self.recent_qa],
            "q_rounds": self.q_rounds,
        }


#: 向后兼容类型别名
Ctx = ContextSnapshot


def _extract_recent_qa(state) -> list[dict[str, str]]:
    """提取最近 3 轮「学生问 / 老师答的核心」（解自我矛盾与追问链断裂）。"""
    qa_hist = getattr(state, "qa_history", None)
    if qa_hist:
        return [{"q": str(x.get("q", ""))[:80], "a": str(x.get("a", ""))[:120]}
                for x in qa_hist[-3:] if x.get("q")]
    transcript = getattr(state, "transcript", None) or []
    pairs: list[dict[str, str]] = []
    cur_q = ""
    cur_a: list[str] = []
    for u in transcript:
        spk = getattr(u, "speaker", "")
        txt = (getattr(u, "text", "") or "").strip()
        if not txt:
            continue
        if spk == "student":
            if cur_q and cur_a:
                pairs.append({"q": cur_q[:80], "a": " ".join(cur_a)[:120]})
            cur_q = txt
            cur_a = []
        elif spk == "teacher" and cur_q:
            if txt.startswith(("收到，", "好问题，", "我记下你的问题", "到这里清楚吗")):
                continue
            cur_a.append(txt)
    if cur_q and cur_a:
        pairs.append({"q": cur_q[:80], "a": " ".join(cur_a)[:120]})
    return pairs[-3:]


def build_ctx(script: LessonScript, state, *, slide_override: int | None = None) -> ContextSnapshot:
    slide_idx = getattr(state, "slide_index", 0) if slide_override is None else slide_override
    seg_idx = getattr(state, "seg_index", 0) if slide_override is None else 0
    slide = script.slide_at(slide_idx)
    unit = script.unit_of(slide_idx, seg_idx)
    prev_u = script.previous_of(unit.unit_id) if unit else None

    slide_text = "\n".join(f"  - {e.text}" for e in slide.elements) if slide else ""
    covered_raw = getattr(state, "covered", None) or []
    covered_kp = [c.kp if hasattr(c, "kp") else str(c) for c in covered_raw][-10:]
    comp = dict(getattr(state, "comprehension", None) or {})
    deferred = list(dict.fromkeys(getattr(state, "deferred", None) or []))
    q_rounds_map = getattr(state, "q_rounds", None) or {}
    unit_id = unit.unit_id if unit else ""
    q_rounds = int(q_rounds_map.get(unit_id, 0)) if isinstance(q_rounds_map, dict) else 0
    seq = int(getattr(state, "seq", len(getattr(state, "transcript", None) or [])))

    return ContextSnapshot(
        version={
            "slide": slide_idx + 1,
            "unit": unit_id,
            "seq": seq,
        },
        slide={
            "index": slide_idx,
            "total": script.slide_count,
            "title": slide.title if slide else "",
            "text": slide_text,
        },
        unit={
            "id": unit_id,
            "text": unit.text if unit else "",
        },
        prev_unit_text=prev_u.text if prev_u else "",
        covered_kp=covered_kp,
        comprehension=comp,
        deferred=deferred,
        recent_qa=_extract_recent_qa(state),
        q_rounds=q_rounds,
    )


# ===========================================================================
# Prompt 模板
# ===========================================================================

TEACHER_PERSONA = """你是一位耐心的初中数学老师，正在给一名学生上课。

- 口语化，像真人讲课，不要书面腔、不要念稿
- 每次回答控制在 2~4 句话，因为你是"说"而不是"写"
- 不要说"综上所述""首先其次最后"这类结构词
- 如果学生问的内容和正在讲的内容一致，直接顺着讲
- 说错了要纠正，不要护短
- 只回答学生问的，不主动扩展新知识点
- **输出会被直接转成语音念出来**：只写你要说出口的话。
  不要写标题、不要写 markdown、不要写列表符号、不要复述别人的问题，
  不要出现"教师答""约束""学生问"这类字样 —— 它们会被一字不差地念出来。"""


def _format_memory_block(ctx: ContextSnapshot) -> str:
    """将 ContextSnapshot 的连续性记忆字段（deferred / comprehension / recent_qa / q_rounds）拼入提示词。"""
    lines: list[str] = []
    if ctx.deferred:
        lines.append(f"- 待回看知识点（前面暂放、欠学生的点）：{', '.join(ctx.deferred)}")
    if ctx.comprehension:
        comp_items = [f"{k}={v}" for k, v in list(ctx.comprehension.items())[-6:]]
        lines.append(f"- 学生理解信号：{', '.join(comp_items)}")
    if ctx.q_rounds > 0:
        lines.append(f"- 当前单元已重讲轮次：{ctx.q_rounds} 轮")
    if ctx.recent_qa:
        qa_lines = [f"  · 问：{item.get('q', '')} → 答：{item.get('a', '')}" for item in ctx.recent_qa[-3:]]
        lines.append("- 最近问答记录（保持前后一致，勿自我矛盾）：\n" + "\n".join(qa_lines))
    return "\n".join(lines)


def prompt_classify(q: str, ctx: Ctx) -> list[dict]:
    system = f"""{TEACHER_PERSONA}

# 你的任务
判断学生这句话属于哪一类，并决定怎么处理。

## 分类
- clarify：澄清型。没听懂、再说一遍、某句话没跟上的疑问。"这里没懂""为什么是减号"
- recall：回顾型。明确指向前面讲过的内容。"刚才第3页那个公式为什么成立""换页前说的那个"
- forward：前瞻型。问后面还没讲的内容。"那Δ<0呢""这个会考吗""后面还会讲吗"
- meta：元问题型。问学习价值、方法、要不要背。"学这个有什么用""要背下来吗"
- emotion：情绪型。表达感受而不是提问。"好难啊""不想学了""今天好累""总算明白了"

## 输出格式
只输出 JSON，不要任何其他文字：
{{"kind": "clarify|recall|forward|meta|emotion",
  "pause_lecture": true,
  "backtrack_to_slide": null,
  "reason": "一句话理由"}}

## 规则
- 只有 recall 且能确定是哪一页时，才填 backtrack_to_slide（用页码数字，从 1 开始）
- emotion 类 pause_lecture 一律为 true（共情必须被听见）
- reason 用中文，20 字以内"""

    mem = _format_memory_block(ctx)
    mem_sec = f"\n\n课堂记忆：\n{mem}" if mem else ""
    user = f"""当前页：{ctx.slide_title}｜正在讲：{ctx.unit_text[:80]}

刚讲过：{ctx.prev_unit_text or "（本页第一段）"}

这一页的要点：{", ".join(ctx.covered_kp[-8:]) or "（还没开始记）"}{mem_sec}

学生原话：{q}

只输出 JSON。"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def prompt_answer(q: str, kind: str, ctx: Ctx) -> list[dict]:
    style = {
        "clarify": "原地简短解答，解答完就说一句回到主线的话（例如「好，我们接着看这一页」），不要跑题。",
        "recall": "学生问的是前面讲过的内容。先用一句话把它和现在讲的内容连起来，再回答，最后接回当前页。",
        "forward": "学生问到了后面还没讲的内容。如果接下来就会讲到，就卖个关子：「这个我马上就讲」；"
                   "如果这节课不涉及，就明确说明「这节课不涉及，你先记住有这么回事」。不要提前展开讲。",
        "meta": "用一两句实在话回答，不要空泛地谈学习方法，也不要长篇大论。",
        "emotion": "学生表达的是感受，不是提问。先用一句真诚的话接住他的情绪，"
                   "再给一个门槛更低的具体下一步（例如「我们先把最简单的那种情况过一下」）。不要说教、不要喊口号。",
    }.get(kind, "简短回答，然后回到讲课。")

    mem = _format_memory_block(ctx)
    mem_constraint = f"\n{mem}" if mem else ""

    system = f"""{TEACHER_PERSONA}

# 当前任务
学生在听课时插话提问。回答要求：
{style}

# 重要约束
- 以下知识点你已经讲过，**不要当成新内容重新讲一遍**：{", ".join(ctx.covered_kp[-10:]) or "（暂无）"}
- 你被打断的位置：{ctx.unit_text[:80]}{mem_constraint}
- 回答完**不要**另起新话题，学生要回去继续听课。
- 直接输出你要说出口的那几句话，不要加任何标题、编号或格式。"""

    user = f"""课堂位置：{ctx.slide_plan}｜当前页：{ctx.slide_title}

本页内容：
{ctx.slide_text}

学生刚才说的原话是：
{q}

请直接说给这个学生听。"""

    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


REPHRASE_STRATEGY = {
    1: ("换说法", "换一个表达角度说明同一个意思。**不要重复原来的措辞**，"
                 "可以换一种说法、换个顺序、把术语换成大白话。"),
    2: ("举例子", "举一个具体、贴近生活的数字例子，让学生看到它真的能算。"),
    3: ("拆步骤", "把这一小点拆成更细的几步，一步只说一件事，每步之间停一下。"),
    4: ("暂放", "这个点确实有点绕。坦诚地说明我们先把它放一放，后面的内容讲完再回来处理，"
                "并让学生放心：这不是他的问题。"),
}


def prompt_rephrase(q: str, round_no: int, ctx: Ctx) -> list[dict]:
    name, style = REPHRASE_STRATEGY.get(round_no, REPHRASE_STRATEGY[4])
    mem = _format_memory_block(ctx)
    mem_sec = f"\n{mem}" if mem else ""
    system = f"""{TEACHER_PERSONA}

# 当前任务
学生说没听懂。这是第 {round_no} 轮重讲，策略是「{name}」。
{style}{mem_sec}

# 要求
- **绝不要说「我再说一遍」然后重复原话** —— 重复原话理解率提升极其有限
- 只针对卡住的那一小块，不要重讲整页
- 2~4 句话，说完让学生有个着落
- 直接输出你要说出口的那几句话，不要写标题、不要复述上面的要求"""

    user = f"""学生刚才说的原话是：{q or "（点了「不懂」，没有具体说明）"}

他卡住的这一段是：
{ctx.unit_text}

请用「{name}」的方式，直接说给他听。"""

    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def prompt_resume_yield(ctx: Ctx, q: str) -> str:
    """让位过渡话术（运行时生成，不在预生成脚本里）。"""
    return (
        f"你正在讲课，讲到一半学生插话提问。\n"
        f"请直接写出你要说出口的那一句话，用来准备停下来回答他。\n"
        f"要求：不超过 20 个字；不要重复学生的原话；不要说「好的我来回答」；"
        f"不要描述你的做法（例如「我会先回答你」），只写要念出来的话。\n"
        f"学生问的是：{q}\n"
        f"当前讲的内容：{ctx.unit_text[:50]}"
    )


def prompt_resume_back(ctx: Ctx, q: str) -> str:
    """三步重入中的第 3 层（续接层）。

    认知层（"好，我们回到刚才那里"）与定位层（锚点）由状态机给定，
    这里只生成把它们接到讲解上的那一句。
    """
    return (
        f"你刚回答完学生的提问：{q}\n"
        f"现在要接回被打断的那段讲解。\n"
        f"请直接写出你要说出口的那一句话，用来把话头接回去。\n"
        f"要求：不超过 20 个字；不要说「我们继续」这种空话；"
        f"不要描述你的做法（例如「我会把话题接回去」），只写要念出来的话。\n"
        f"被打断的位置：{ctx.unit_text}"
    )


# ===========================================================================
# Provider
# ===========================================================================

class LLMError(RuntimeError):
    pass


def _probe_log(msg: str) -> None:
    """恢复探测的诊断输出 —— 只在显式开启 trace 时打印，免得刷屏。"""
    if os.environ.get("CLASSROOM_TRACE"):
        print(msg, flush=True)


class LLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile = settings.active
        self.client = AsyncOpenAI(
            base_url=self.profile.base_url,
            api_key=self.profile.api_key or "not-set",
            timeout=settings.timeout,
            # 服务商要求的固定请求头（OpenCode Go 的 x-opencode-session 就在这里；
            # 没有它 Go 会直接 400，连"模型名对不对"都轮不到判断）
            default_headers=self.profile.headers or None,
        )
        #: 已经确认连不上（预检失败或连续失败）→ 不再去等注定失败的连接。
        #: 学生不会因为"等一个死掉的服务超时"而多等好几秒，而是立刻听到预置话术。
        self.degraded = False
        self.down_reason = ""
        #: 降级是不是开课预检时就判定出来的（用来把话说准：见 _explain_llm_failure）
        self.down_at_preflight = False
        self._last_probe = 0.0
        #: 思考统计（只用于诊断和指标；思考内容本身绝不外泄）
        self.last_reasoning_chars = 0        # 最近一次生成里思考了多少字
        self.last_text_at: float | None = None   # 第一个正文增量到达的时刻
        self._stream_t0 = 0.0                # 本次流式请求发出的时刻
        self.last_think_ms = 0               # 这次"光想不说"花了多久

    def finish_think_stats(self) -> None:
        """流结束时把"思考了多久"算出来。"""
        if self.last_text_at is not None and self._stream_t0:
            self.last_think_ms = int((self.last_text_at - self._stream_t0) * 1000)
        elif self.last_reasoning_chars:
            # 光思考、一个正文都没产出 —— 多半是预算被吃光了
            self.last_think_ms = int((time.monotonic() - self._stream_t0) * 1000)
        else:
            self.last_think_ms = 0

    def mark_down(self, reason: str = "", *, at_preflight: bool = False) -> None:
        self.degraded = True
        self.down_reason = reason
        self.down_at_preflight = at_preflight

    def mark_up(self) -> None:
        self.degraded = False
        self.down_reason = ""
        self.down_at_preflight = False

    @property
    def short_timeout(self) -> float:
        """短话术（过渡/重入/分流）超时。

        这几句是**学生正在等**的东西，一共十几个字，不该等满配置里的 180 秒。

        但也**不能太短**：第一次请求往往要等模型加载进内存（实测本地 9B 模型
        首次要十几秒），定 5 秒的话会把"服务其实是好的"误判成不可用 ——
        那一步会让学生直接听到兜底话术，比多等几秒糟得多。
        取配置超时的 1/3，下限 20 秒。
        """
        base = getattr(getattr(self, "settings", None), "timeout", 60.0)
        return max(20.0, base / 3)

    async def recover_if_possible(self) -> bool:
        """降级状态下试探一次服务是否回来了。

        调用点在"学生刚提问、马上就要作答"之前，所以这里**必须真的去试**
        （哪怕几十秒前刚失败过）—— 否则服务恢复了，学生还是听到预置的兜底话术。
        失败代价很小：用的是 `short_timeout`，最多几秒，而且这次尝试本来就要失败。

        绝对下限只防"同一瞬间并发调用打出一串连接"。
        """
        if not self.degraded:
            return True
        if time.monotonic() - self._last_probe < 1.0:
            return False
        self._last_probe = time.monotonic()
        # 探测用配置里的完整超时，不用 short_timeout：
        # 一个还在加载模型的 ollama 会先在短超时上失败，而"误判成不可用"
        # 的代价是学生听不到真正的回答 —— 宁可这次多等几秒。
        try:
            # 只是连通性探测，2 个 token —— 绝不能开思考（那要 30 秒，探测就没意义了）
            await self.chat([{"role": "user", "content": "好"}], max_tokens=2, think=False)
        except Exception as e:
            _probe_log(f"[recover] 探测失败：{type(e).__name__}: {e}")
            return False
        self.mark_up()
        return True

    # -- 思考模式 ---------------------------------------------------------
    def _think_for(self, role: str) -> bool:
        """这个角色要不要开思考。没有 settings 时（测试里的裸 LLM）一律不思考。"""
        st = getattr(self, "settings", None)
        if st is None:
            return False
        try:
            return bool(st.think_for(role))
        except Exception:
            return False

    def _think_kwargs(self, think: bool) -> dict:
        """把"要不要思考"翻成这家服务商认的参数。

        实测（Ollama 的 OpenAI 兼容接口，见 README 实测表）：
          - 只能靠 `reasoning_effort` 控制；
          - 值只有两端有效：`"none"` 关得掉，其余值（含 `low`/`medium`/`minimal`）
            都被当成"非 none"照样全量思考 —— 所以别指望用它们做"思考预算分级"；
          - `extra_body={"think": False}` 走兼容接口**无效**（要原生 /api/chat 才认）。
        """
        style = getattr(self.profile, "thinking_style", "reasoning_effort")
        if style != "reasoning_effort":
            return {}
        st = getattr(self, "settings", None)
        if think:
            v = getattr(st, "thinking_on_value", "")
            return {} if not v else {"reasoning_effort": v}
        v = getattr(st, "thinking_off_value", "none")
        return {"reasoning_effort": v} if v else {}

    def _budget(self, max_tokens: int, think: bool) -> int:
        """开思考时必须给思考留出预算。

        这不是优化，是**必需品**：思考会把 max_tokens 吃光，正文一个 token 都剩不下，
        表现就是"模型明明连着、却一句话都产不出来"。实测一次思考约 2900 字，
        给 1200 的预算就必然空手而归（finish_reason=length）。
        """
        if not think:
            return max_tokens
        extra = getattr(getattr(self, "settings", None), "think_token_budget", 2048)
        return max_tokens + extra

    @staticmethod
    def _reasoning_of(msg_or_delta) -> str:
        """取出"思考"字段 —— 各家的字段名不一样，都试一遍。

        注意：这个函数的返回值**永远不会**被念出来，只用于诊断和统计。
        """
        if msg_or_delta is None:
            return ""
        for name in ("reasoning", "reasoning_content", "thinking"):
            v = getattr(msg_or_delta, name, None)
            if isinstance(v, str) and v:
                return v
        extra = getattr(msg_or_delta, "model_extra", None) or {}
        for name in ("reasoning", "reasoning_content", "thinking"):
            v = extra.get(name)
            if isinstance(v, str) and v:
                return v
        return ""

    # -- 基础调用 ---------------------------------------------------------
    async def chat(self, messages: list[dict], *, json_mode: bool = False,
                   temperature: float | None = None, max_tokens: int = 700,
                   timeout: float | None = None, think: bool = False) -> str:
        kwargs: dict = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self._budget(max_tokens, think),
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        kwargs.update(self._think_kwargs(think))
        # 短话术（过渡/重入/分流）给它一个更短的超时：这几句是"让位时"
        # 学生正在等的东西，等它 180 秒毫无意义 —— 超时就该用预置话术顶上。
        # （实测：课上 ollama 挂掉后，让位话术等了 11 秒才出声，就是等这个超时。）
        call = self.client.chat.completions.create
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = await call(**kwargs)
        except Exception as e:  # 有些服务商不支持 response_format
            if not json_mode:
                raise LLMError(self._hint(e)) from e
            kwargs.pop("response_format", None)
            try:
                r = await call(**kwargs)
            except Exception as e2:
                raise LLMError(self._hint(e2)) from e2
        # 任何一次调用成功，就说明服务回来了 —— 自动解除降级，不用重启课堂
        self.mark_up()
        msg = r.choices[0].message
        self.note_reasoning(msg)
        return (msg.content or "").strip()

    def note_reasoning(self, msg_or_delta) -> None:
        """记录"这一次到底思考了没有、思考了多少"。

        思考内容本身**绝不返回给上层**（学生只该听到老师说的话，
        不该听到模型的自言自语）。这里只累计统计量，用来解释"为什么这次这么慢"
        以及诊断"思考把预算吃光了"。
        """
        r = self._reasoning_of(msg_or_delta)
        if r:
            self.last_reasoning_chars += len(r)

    async def stream(self, messages: list[dict], *, max_tokens: int = 500,
                     temperature: float | None = None, think: bool = False,
                     on_think=None) -> AsyncIterator[str]:
        """流式生成。

        `on_think` 是"在思考 / 想完了"的回调 `(thinking: bool) -> Awaitable`。
        之所以做成**每次调用传参**、而不是挂在 LLM 对象上：LLM 实例是跨连接共享的，
        挂对象上会让两个标签页互相点亮对方的"在思考"指示灯。

        无论思考内容有多少，**它都不会出现在产出里** —— 只有 `delta.content` 会被 yield。
        """
        kw: dict = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self._budget(max_tokens, think),
            "stream": True,
        }
        kw.update(self._think_kwargs(think))
        t0 = time.monotonic()
        self._stream_t0 = t0
        self.last_reasoning_chars = 0
        self.last_text_at = None
        self.last_think_ms = 0
        thinking_shown = False

        async def _set_thinking(on: bool) -> None:
            if on_think is None:
                return
            try:
                await on_think(on)
            except Exception:
                pass        # 指示灯坏了不该影响上课

        try:
            s = await self.client.chat.completions.create(**kw)
        except Exception as e:
            raise LLMError(self._hint(e)) from e

        try:
            async for chunk in s:
                if not chunk.choices:
                    continue
                d = chunk.choices[0].delta
                if d is None:
                    continue
                # **思考增量一律不产出**。这一行是"思考模式"能被安全启用的一半原因：
                # 模型的自言自语只能进统计、最多点亮一个"在思考"的指示灯，
                # 绝不能进字幕、更不能进 TTS。
                if self._reasoning_of(d):
                    if self.last_text_at is None:
                        if not thinking_shown:
                            thinking_shown = True
                            await _set_thinking(True)
                        self.note_reasoning(d)
                    continue
                if d.content:
                    if self.last_text_at is None:
                        self.last_text_at = time.monotonic()
                        if thinking_shown:
                            # 开口说话了 —— 思考阶段结束
                            thinking_shown = False
                            await _set_thinking(False)
                    yield d.content
        finally:
            self.finish_think_stats()
            if thinking_shown:
                # 光想不说就结束了（或中途异常）—— 指示灯不能留在"在思考"上
                await _set_thinking(False)

    def _hint(self, e: Exception) -> str:
        # 连接类失败 → 直接进降级。
        # 场景：课上到一半 ollama 被关了。如果只记着"预检时是好的"，
        # 接下来每一次提问/每句生成都要再等一个完整的超时，学生一节一节地干等。
        low = f"{type(e).__name__}: {e}".lower()
        if any(k in low for k in ("connection", "connect", "timeout", "timed out")):
            self.mark_down(str(e))
        return (
            f"[LLM] {self.profile.name} 调用失败：{type(e).__name__}: {e}\n"
            f"       检查 .env 里的 base_url / api_key / model：{self.profile.base_url} "
            f"({self.profile.model})"
        )

    # -- 课堂能力 ---------------------------------------------------------
    async def classify(self, q: str, ctx: Ctx) -> dict:
        """提问分流。失败时回落到 clarify —— 最保守、也最不会跑偏的一类。"""
        default = {"kind": QKind.CLARIFY.value, "pause_lecture": True,
                   "backtrack_to_slide": None, "reason": "分流失败，按澄清型处理"}
        if self.degraded:
            return default
        # 分流是"学生正在等十几个字"的一环，按短语策略走（默认不思考）
        try:
            raw = await self.chat(prompt_classify(q, ctx), json_mode=True,
                                  temperature=0.0, max_tokens=200,
                                  timeout=self.short_timeout,
                                  think=self._think_for("phrase"))
        except LLMError:
            return default

        try:
            data = json.loads(_extract_json(raw))
        except Exception:
            return default

        kind = str(data.get("kind", "")).strip().lower()
        if kind not in {k.value for k in QKind}:
            return default
        bt = data.get("backtrack_to_slide")
        return {
            "kind": kind,
            "pause_lecture": bool(data.get("pause_lecture", True)),
            "backtrack_to_slide": bt if isinstance(bt, int) else None,
            "reason": str(data.get("reason", ""))[:40],
        }

    async def answer_stream(self, q: str, kind: str, ctx: Ctx, *, on_think=None) -> AsyncIterator[str]:
        """注意：分句前先净化 —— 不能让 `# 教师答` 之类的标题进 TTS。

        这是"值得慢"的一环：答对比答快重要，所以按配置决定要不要思考
        （实测本机 9B 开思考约 30 秒/答，所以默认还是关，见 config.THINKING_MODES）。
        """
        if self.degraded:
            # 一句都不产出就报错 → 上层会用兜底话术，学生立刻听到声音而不是等超时
            raise LLMError("模型服务不可用（已确认连不上）")
        think = self._think_for("answer")
        buf = ""
        async for tok in self.stream(prompt_answer(q, kind, ctx), max_tokens=500,
                                     think=think, on_think=on_think):
            buf += tok
        # 思考把预算吃光时会走到这里：正文为空，但确实想了很久。
        # 必须和"模型没连上"区分开 —— 两者表现都是"没产出"，原因完全相反。
        if not buf.strip() and self.last_reasoning_chars:
            raise LLMError(
                f"模型思考了 {self.last_reasoning_chars} 字却没能产出正文 —— "
                f"思考把 token 预算吃光了。把 LLM_THINK_TOKEN_BUDGET 调大，"
                f"或把 LLM_THINKING 设回 off。")
        for s in drop_boilerplate(split_sentences(sanitize_spoken(buf))):
            yield s

    async def rephrase_stream(self, q: str, round_no: int, ctx: Ctx, *,
                              max_sentences: int = 4, on_think=None) -> AsyncIterator[str]:
        """换讲法重讲。硬性限制句数 —— 小模型很容易一口气讲八句。"""
        if self.degraded:
            raise LLMError("模型服务不可用（已确认连不上）")
        think = self._think_for("answer")
        buf = ""
        async for tok in self.stream(prompt_rephrase(q, round_no, ctx), max_tokens=420,
                                     think=think, on_think=on_think):
            buf += tok
        if not buf.strip() and self.last_reasoning_chars:
            raise LLMError(
                f"模型思考了 {self.last_reasoning_chars} 字却没能产出正文 —— "
                f"思考把 token 预算吃光了。")
        clean = drop_boilerplate(split_sentences(sanitize_spoken(buf)))
        for s in clean[:max_sentences]:
            yield s

    async def yield_phrase(self, ctx: Ctx, q: str) -> str:
        from phrases import PHRASE_BANK

        fb = PHRASE_BANK.get("yield_transition") or "这个问题问得好，先停一下。"
        has_settings = getattr(self, "settings", None) is not None
        if self.degraded or (has_settings and PHRASE_BANK.mode_of("yield_transition") == "fixed"):
            return fb
        return await self._one_liner(prompt_resume_yield(ctx, q),
                                     fallback=fb,
                                     max_chars=22, strict=True)

    async def resume_phrase(self, ctx: Ctx, q: str) -> str:
        from phrases import PHRASE_BANK

        # 重入话术只负责"续接"，定位由状态机给（"好，我们回到刚才那里——<锚点>"）
        fb = PHRASE_BANK.get("resume_continuation") or "我们接着往下看。"
        has_settings = getattr(self, "settings", None) is not None
        if self.degraded or (has_settings and PHRASE_BANK.mode_of("resume_continuation") == "fixed"):
            return fb
        return await self._one_liner(prompt_resume_back(ctx, q),
                                     fallback=fb, max_chars=22, strict=True)

    async def _one_liner(self, prompt: str, fallback: str, max_chars: int = 40,
                         strict: bool = False) -> str:
        """生成一句短话术。

        `strict=True`（过渡/重入话术）时，**宁可退回预置话术，也不放残句过去** ——
        这两种话术一共只说十几个字，一旦被截断听起来就是"老师说了半句话"，
        比用一句万能的固定话术糟糕得多。
        """
        try:
            t = await self.chat(
                [{"role": "system", "content": TEACHER_PERSONA},
                 {"role": "user", "content": prompt}],
                max_tokens=80, temperature=0.4, timeout=self.short_timeout,
                think=self._think_for("phrase"),
            )
        except LLMError:
            return fallback
        t = sanitize_spoken(t).strip().strip("「」\"'")
        if not t:
            return fallback
        # 只取第一句，防止话术变长
        for ch in SENT_END:
            if ch in t:
                t = t.split(ch)[0] + ch
                break
        else:
            # 结尾是未说完的半句（例如"a 不能等于零"这种残片）→ 退到最后一个完整句
            cut = max(t.rfind("。"), t.rfind("！"), t.rfind("？"), t.rfind("；"))
            if cut >= 0:
                t = t[:cut + 1]
        if strict:
            tail = t.rstrip()
            # 长度超限 / 停在逗号或悬空的连接词上 / 结尾没有句读 → 一律用预置话术
            if len(t) > max_chars or not tail.endswith(tuple(SENT_END)):
                return fallback
            if tail.endswith(("，", "、", "：", "的", "和", "是", "把", "在")) or \
                    tail[-1] in "+-*/=＝＋－×÷":
                return fallback
            if _looks_mangled(tail):
                return fallback
            return tail
        t = clamp_spoken(t, max_chars)
        return t if t.strip("。！？；，, ") else fallback


#: 成对符号。直引号单独处理（它自己跟自己配对，配对检查天然通过，见 _looks_mangled）
_OPEN_CLOSE = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】",
               "“": "”", "‘": "’"}
#: 所有收尾符号（判断"孤零零一个收尾符号"用）
_ALL_CLOSERS = set(_OPEN_CLOSE.values()) | {'"', "」", "』", "）", ")", "】", "”", "’"}
#: 结尾不该单独出现的字（半句话常见尾巴）
_DANGLING_TAIL = ("的", "和", "是", "把", "在", "与", "或", "而", "就", "都",
                  "为", "给", "对", "从", "向")


def _looks_mangled(t: str) -> bool:
    """判断一句话是不是"排版残片"。

    真实翻车样本（让位话术）：`Δ 不能等于零"。` —— 结尾把半个引号和一个句号
    粘在一起，读出来是"Δ 不能等于零引号"。这种必须退回预置话术。

    注意直引号 `"` 的特殊性：它没有左右之分，自己跟自己"配对成功"，
    所以配对检查挡不住它，必须单独看**它出现在哪个位置**。
    """
    if not t:
        return True
    # 1) 成对符号配不上（中文引号/括号）
    for op, cl in _OPEN_CLOSE.items():
        if t.count(op) != t.count(cl):
            return True
    # 2) 出现孤立的收尾符号：前面一个开符号都没有
    for cl in _ALL_CLOSERS:
        if cl in t and not any(op in t for op in _OPEN_CLOSE):
            return True
    # 3) 收尾符号紧贴着句读，且前面不是"整句引用"的收尾（实测样本就是这一条）
    if re.search(r'[」』）)\]】”’"]\s*[。！？；]', t):
        return True
    # 4) 标点连排（"。"、"，。"…）
    if re.search(r"[。！？；，、]{2,}", t):
        return True
    # 5) 逗号/顿号后面跟着收尾符号，说明是从中间截出来的
    if re.search(r"[，、]\s*[」』）)\]】”’\"]", t):
        return True
    # 6) markdown 残留
    if any(ch in t for ch in "*`#"):
        return True
    # 7) 结局悬空：句读前一个字是连接性虚词
    body = t[:-1].rstrip() if t[-1] in SENT_END else t.rstrip()
    if body and body[-1] in _DANGLING_TAIL:
        return True
    return False


def _extract_json(raw: str) -> str:
    """从可能带 markdown 围栏的输出里取出 JSON 主体。"""
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if m:
        return m.group(1).strip()
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        return raw[i:j + 1]
    return raw


def action_for(kind: QKind) -> QAction:
    return {
        QKind.CLARIFY: QAction.ANSWER_INLINE,
        QKind.RECALL: QAction.BACKTRACK_PAGE,
        QKind.FORWARD: QAction.HOOK,
        QKind.META: QAction.FIXED_REPLY,
        QKind.EMOTION: QAction.BRIEF_EMPATHY,
    }[kind]
