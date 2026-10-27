"""让位话术库管理（开发者模式）与两级快速意图路由器（枚举直通 + 轻量小模型预判）。

对应需求：
1. 开发者模式展示并管理所有让位话术（支持增删改、切换默认话术、切换固定话术库/LLM生成、恢复默认）。
2. 快速意图识别探索：预先定义好课堂意图枚举量（`StudentIntent`），先通过毫秒级规则与轻量小模型
   判断学生意图；对于「我听懂了」「继续讲吧」等回应，老师直接继续讲课，省下大模型思考与生成的开销。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from state import QAction, QKind


# ===========================================================================
# 1. 让位话术库（PhraseBank）
# ===========================================================================

DEFAULT_PHRASE_CATEGORIES: list[dict[str, Any]] = [
    {
        "key": "ack",
        "label": "收到提问 · 立即回执话术",
        "phase": "ACKNOWLEDGING",
        "desc": "学生在讲课中途插话提问时，老师在段末给出的第一句回执（规范 I3：先回执再处理）。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "收到，我把这一段讲完就来回答你。",
            "好问题，等我把这一句收个尾马上讲。",
            "我记下你的问题了，这小节说完就为你解答。",
        ],
    },
    {
        "key": "probe_ask",
        "label": "页尾理解探针 · 提问话术",
        "phase": "PAUSING_AUDIO",
        "desc": "每页最后一个讲解单元结束时，老师主动询问学生是否听懂的话术（触发右下角浮窗变色等待）。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "到这里清楚吗？不清楚可以直接说没听懂。",
            "这一页的重点大家都跟上了吗？有疑问随时提出来。",
            "这部分听明白了吗？如果觉得快可以说再讲一遍。",
        ],
    },
    {
        "key": "yield_transition",
        "label": "段末让位 · 过渡话术",
        "phase": "YIELDING",
        "desc": "老师从主线讲课切换到答疑时的自然过渡话术。设为「固定话术直出」可省去一次 LLM 调用延迟。",
        "mode": "fixed",
        "supports_llm": True,
        "active": 0,
        "items": [
            "这个问题问得好，先停一下。",
            "问到点子上了，我们先看你刚才提的这个问题。",
            "好，我们先停下来把你刚才问的地方理清楚。",
        ],
    },
    {
        "key": "redirect_prev",
        "label": "回顾型提问 · 往前翻页话术",
        "phase": "REDIRECTING",
        "desc": "学生问到前面讲过的某页内容时，老师切回对应 PPT 页码时说的话术（支持 {page} 占位符）。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "这个我们得回第 {page} 页去看一眼，就是这一行。",
            "好，我们往回翻到第 {page} 页，对着课件再看一遍。",
        ],
    },
    {
        "key": "redirect_next",
        "label": "前瞻/跨页提问 · 往后翻页话术",
        "phase": "REDIRECTING",
        "desc": "学生问到后面某页内容且需要切页定位时说的话术（支持 {page} 占位符）。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "这个其实在第 {page} 页，我们先翻过去看一眼。",
            "我们翻到第 {page} 页来看一下对应的内容。",
        ],
    },
    {
        "key": "redirect_same",
        "label": "本页定位 · 同页指引话术",
        "phase": "REDIRECTING",
        "desc": "回顾位置就在当前页时说的话术。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "就在这一页，我们一起看这一行。",
            "好，目光回到当前这一页的重点行。",
        ],
    },
    {
        "key": "resume_prefix",
        "label": "三步重入 · 认知与定位前缀",
        "phase": "RESUMING",
        "desc": "答疑结束回到主线时的第 1~2 步（认知层+定位层，支持 {anchor} 锚点提示占位符）。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "好，我们回到刚才那里——{anchor}。",
            "清楚了之后，我们回到刚才讲到的——{anchor}。",
        ],
    },
    {
        "key": "resume_continuation",
        "label": "三步重入 · 续接主线话术",
        "phase": "RESUMING",
        "desc": "答疑结束回到主线时的第 3 步（续接层）。设为「固定话术直出」可省去一次 LLM 调用延迟。",
        "mode": "fixed",
        "supports_llm": True,
        "active": 0,
        "items": [
            "我们接着往下看。",
            "顺着刚才的思路，我们继续往下讲。",
            "接下来我们继续看这一页剩下的部分。",
        ],
    },
    {
        "key": "defer_1",
        "label": "重讲超限 · 第 1 次暂放安抚话术",
        "phase": "ANSWERING",
        "desc": "同一单元重讲达到上限（如 4 轮）时，记入「待回看」并安抚学生的话术。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "这个点确实有点绕，我们先把它放一放。后面的内容讲完我再回到这里，这不是你的问题，是这个知识点本身就要多绕两圈。",
        ],
    },
    {
        "key": "defer_2",
        "label": "重讲超限 · 第 2 次暂放承诺话术",
        "phase": "ANSWERING",
        "desc": "重讲达上限后学生第 2 次追问时，避免复读的递进承诺话术。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "这个点我记住了，已经排在最后一起讲。我们先往后走，不然今天这一节会卡在这里，回头我一定把它讲透。",
        ],
    },
    {
        "key": "defer_3",
        "label": "重讲超限 · 第 3 次及以上暂放话术",
        "phase": "ANSWERING",
        "desc": "重讲达上限后学生继续表示没懂时的课后复盘承诺话术。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "我一直记得这个点，等下课后我们一起把它过一遍。现在我们先把后面的看完，你把不懂的地方留着，到时直接问我。",
        ],
    },
    {
        "key": "answer_fallback",
        "label": "模型离线/超时 · 答疑兜底话术",
        "phase": "ANSWERING",
        "desc": "大模型服务不可用或超时时的答疑兜底话术，保证课堂永不静默卡死。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "这个问题老师暂时答不上来，我们先继续往下讲。",
            "这个问题我先记下来，我们先把主线内容往下推进。",
        ],
    },
    {
        "key": "rephrase_fallback",
        "label": "模型离线/超时 · 重讲兜底话术",
        "phase": "ANSWERING",
        "desc": "大模型服务不可用时的重讲兜底话术。",
        "mode": "fixed",
        "supports_llm": False,
        "active": 0,
        "items": [
            "我们换一个说法，再看一遍这一段。",
        ],
    },
]


class PhraseBank:
    """让位话术库：支持查看、新增、编辑、删除、切换激活项及持久化。"""

    def __init__(self, store_path: Path | None = None):
        self.store_path = store_path
        self._cats: dict[str, dict[str, Any]] = {}
        self.reset_to_defaults(save=False)
        if store_path and store_path.exists():
            self._load()

    def reset_to_defaults(self, *, save: bool = True) -> None:
        self._cats = {
            c["key"]: {
                "key": c["key"],
                "label": c["label"],
                "phase": c["phase"],
                "desc": c["desc"],
                "mode": c["mode"],
                "supports_llm": bool(c.get("supports_llm", False)),
                "active": int(c.get("active", 0)),
                "items": list(c["items"]),
            }
            for c in DEFAULT_PHRASE_CATEGORIES
        }
        if save:
            self._save()

    def _load(self) -> None:
        if not self.store_path or not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            for key, saved in (data.get("categories") or {}).items():
                if key in self._cats and isinstance(saved, dict):
                    items = [str(x).strip() for x in (saved.get("items") or []) if str(x).strip()]
                    if items:
                        self._cats[key]["items"] = items
                    active = int(saved.get("active", 0) or 0)
                    self._cats[key]["active"] = max(0, min(len(self._cats[key]["items"]) - 1, active))
                    if self._cats[key]["supports_llm"] and saved.get("mode") in ("fixed", "llm"):
                        self._cats[key]["mode"] = saved["mode"]
        except Exception:
            pass

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "categories": {
                    k: {
                        "mode": v["mode"],
                        "active": v["active"],
                        "items": v["items"],
                    }
                    for k, v in self._cats.items()
                }
            }
            self.store_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def list_all(self) -> list[dict[str, Any]]:
        return [
            {
                **c,
                "current": self.get(c["key"]),
            }
            for c in self._cats.values()
        ]

    def mode_of(self, key: str) -> str:
        c = self._cats.get(key)
        return str(c.get("mode", "fixed")) if c else "fixed"

    def get(self, key: str, **fmt: Any) -> str:
        c = self._cats.get(key)
        if not c or not c["items"]:
            return ""
        idx = max(0, min(len(c["items"]) - 1, int(c.get("active", 0))))
        raw = c["items"][idx]
        if fmt:
            try:
                return raw.format(**fmt)
            except Exception:
                return raw
        return raw

    def get_sentences(self, key: str) -> list[str]:
        from llm import split_sentences

        text = self.get(key)
        sents = split_sentences(text)
        return sents if sents else ([text] if text else [])

    def update_category(
        self,
        key: str,
        *,
        items: list[str] | None = None,
        active: int | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if key not in self._cats:
            raise KeyError(f"未知的话术分类：{key}")
        c = self._cats[key]
        if items is not None:
            cleaned = [str(x).strip() for x in items if str(x).strip()]
            if not cleaned:
                raise ValueError("每个分类至少保留一条话术")
            c["items"] = cleaned
        if active is not None:
            c["active"] = max(0, min(len(c["items"]) - 1, int(active)))
        else:
            c["active"] = max(0, min(len(c["items"]) - 1, int(c["active"])))
        if mode in ("fixed", "llm") and c.get("supports_llm"):
            c["mode"] = mode
        self._save()
        return {**c, "current": self.get(key)}


# 全局单例话术库（默认持久化在 prototype/.cache/phrases.json）
PHRASE_BANK = PhraseBank(Path(__file__).resolve().parent / ".cache" / "phrases.json")


# ===========================================================================
# 2. 预定义学生意图枚举 + 快速意图路由器（枚举直通 + 小模型快速分类）
# ===========================================================================

class StudentIntent(str, Enum):
    """预先定义好的课堂学生意图枚举量。

    分为两大类：
      A. 零大模型生成开销（直接改变课堂状态或继续讲课，无需调主模型回答）：
         - CONFIRM_UNDERSTOOD : 明确表示听懂了/可以继续（「我听懂了」「明白了继续吧」）
         - CMD_NEXT_PAGE      : 要求翻到下一页（「下一页」「翻页吧」）
         - CMD_PREV_PAGE      : 要求回到上一页（「回上一页」「看上一页」）
         - CMD_REPEAT_UNIT    : 要求重播当前这一段（「再放一遍」「刚才没听清」）
      B. 需要教师解答/重讲（直接给出分流枚举，省去主模型 classify 调用）：
         - REPHRASE_CONFUSED  : 没听懂、要求换讲法重讲（「没听懂」「还是不明白」）
         - REPHRASE_EXAMPLE   : 要求举个例子（「举个例」「能造个句吗」）
         - ASK_CLARIFY        : 针对当前知识点的澄清提问
         - ASK_RECALL         : 回顾前面某页的提问（可带 slide 页码）
         - ASK_FORWARD        : 针对后续内容的前瞻提问
         - ASK_META           : 学习方法/考试要求类元问题
         - EXPRESS_EMOTION    : 畏难/疲惫等情绪表达
    """

    CONFIRM_UNDERSTOOD = "confirm_understood"
    CMD_NEXT_PAGE = "cmd_next_page"
    CMD_PREV_PAGE = "cmd_prev_page"
    CMD_REPEAT_UNIT = "cmd_repeat_unit"
    REPHRASE_CONFUSED = "rephrase_confused"
    REPHRASE_EXAMPLE = "rephrase_example"
    ASK_CLARIFY = "ask_clarify"
    ASK_RECALL = "ask_recall"
    ASK_FORWARD = "ask_forward"
    ASK_META = "ask_meta"
    EXPRESS_EMOTION = "express_emotion"
    DELEGATE = "delegate"


INTENT_META: dict[str, dict[str, Any]] = {
    StudentIntent.CONFIRM_UNDERSTOOD.value: {
        "label": "确认听懂（直接继续讲课）",
        "direct": True,
        "qkind": None,
        "desc": "学生表示已听懂，老师直接记下掌握信号并继续讲课，省下全部大模型调用",
    },
    StudentIntent.CMD_NEXT_PAGE.value: {
        "label": "指令：翻到下一页",
        "direct": True,
        "qkind": None,
        "desc": "直接执行下一页切换，零大模型开销",
    },
    StudentIntent.CMD_PREV_PAGE.value: {
        "label": "指令：返回上一页",
        "direct": True,
        "qkind": None,
        "desc": "直接返回上一页重讲，零大模型开销",
    },
    StudentIntent.CMD_REPEAT_UNIT.value: {
        "label": "指令：重听本段",
        "direct": True,
        "qkind": None,
        "desc": "直接重播当前讲解单元，零大模型开销",
    },
    StudentIntent.REPHRASE_CONFUSED.value: {
        "label": "没听懂 · 换讲法重讲",
        "direct": False,
        "qkind": QKind.CLARIFY.value,
        "desc": "省去 LLM 分流调用，直接进入重讲链（换说法→举例子→拆步骤→暂放）",
    },
    StudentIntent.REPHRASE_EXAMPLE.value: {
        "label": "请求举例 · 举例重讲",
        "direct": False,
        "qkind": QKind.CLARIFY.value,
        "desc": "省去 LLM 分流调用，直接按举例请求解答",
    },
    StudentIntent.ASK_CLARIFY.value: {
        "label": "知识点澄清提问",
        "direct": False,
        "qkind": QKind.CLARIFY.value,
        "desc": "针对当前页知识点的具体疑问，进入段末让位答疑",
    },
    StudentIntent.ASK_RECALL.value: {
        "label": "跨页回顾提问",
        "direct": False,
        "qkind": QKind.RECALL.value,
        "desc": "指向前面某页的提问，自动切页定位并答疑",
    },
    StudentIntent.ASK_FORWARD.value: {
        "label": "超前/拓展提问",
        "direct": False,
        "qkind": QKind.FORWARD.value,
        "desc": "问到后面还没讲的内容，先埋钩子再归位",
    },
    StudentIntent.ASK_META.value: {
        "label": "学习方法/考纲元问题",
        "direct": False,
        "qkind": QKind.META.value,
        "desc": "问要不要背、考不考等元问题，简短回答后归位",
    },
    StudentIntent.EXPRESS_EMOTION.value: {
        "label": "畏难/疲惫情绪表达",
        "direct": False,
        "qkind": QKind.EMOTION.value,
        "desc": "先共情安抚学生情绪，再降门槛重讲",
    },
    StudentIntent.DELEGATE.value: {
        "label": "委派外脑 · 逃生舱兜底",
        "direct": False,
        "qkind": QKind.CLARIFY.value,
        "desc": "规则与小模型均未给出确定意图，状态机快答兜底并唤醒外置大脑补充",
    },
}


@dataclass
class IntentDecision:
    intent: StudentIntent
    stage: str                    # "rule" | "small_llm" | "fallback"
    latency_ms: float
    reason: str
    qkind: QKind = QKind.CLARIFY
    backtrack_to_slide: int | None = None
    llm_saved: bool = False       # 是否省下了大模型分流/回答调用
    raw_input: str = ""

    def to_dict(self) -> dict[str, Any]:
        meta = INTENT_META.get(self.intent.value, {})
        return {
            "intent": self.intent.value,
            "intent_label": meta.get("label", self.intent.value),
            "stage": self.stage,
            "latency_ms": round(self.latency_ms, 2),
            "reason": self.reason,
            "qkind": self.qkind.value if self.qkind else None,
            "backtrack_to_slide": self.backtrack_to_slide,
            "llm_saved": self.llm_saved,
            "direct": bool(meta.get("direct", False)),
            "raw_input": self.raw_input,
        }


# 规则级枚举匹配词表
_NEG_WORDS = (
    "没懂", "没听懂", "不懂", "不明白", "不理解", "不清楚", "不会",
    "不知道", "没跟上", "不太懂", "不太明白", "不太清楚", "还是不懂", "没搞懂",
)
_CONFIRM_PATTERNS = (
    "我听懂了", "听懂了", "听懂啦", "我已经听懂了", "完全听懂了",
    "我明白了", "听明白了", "明白了", "明白啦", "完全明白",
    "清楚了", "听清楚了", "很清楚", "搞懂了", "懂了", "会了",
    "没问题", "没有问题", "完全没问题", "ok", "okay", "get到了",
    "可以继续", "继续讲吧", "继续吧", "接着讲吧", "往下讲吧",
)
_QUESTION_WORDS = ("吗", "呢", "?", "？", "什么", "为什么", "怎么", "如何", "哪", "是不是", "能不能")
_EXAMPLE_WORDS = ("举个例", "举例子", "造个句", "给个例句", "举一个例")
_EMOTION_WORDS = ("好难", "太难了", "不想学", "好累", "记不住", "头大", "学不会", "太抽象了", "好烦")
_META_WORDS = ("要背吗", "需要背吗", "会考吗", "考试考吗", "有什么用", "学这个干嘛", "重要吗")
_FORWARD_WORDS = ("后面还会", "以后会", "下节课", "下一单元")
_RECALL_SLIDE_RE = re.compile(r"第\s*(\d+)\s*页")


def classify_by_fast_rules(text: str) -> IntentDecision | None:
    """第一级：亚毫秒级确定性枚举判定（< 1ms，零网络开销）。"""
    t0 = time.perf_counter()
    raw = (text or "").strip()
    if not raw:
        return None
    core = raw.strip("。！？!?…，,、~～ \t\r\n").lower()

    # 1. 显式翻页/重播口语指令（不带疑问词时直接映射为课堂控制命令）
    if core in ("下一页", "翻下一页", "看下一页", "翻页", "往后翻"):
        return IntentDecision(
            intent=StudentIntent.CMD_NEXT_PAGE, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="命中翻页指令枚举", llm_saved=True, raw_input=raw,
        )
    if core in ("上一页", "回上一页", "返回上一页", "看上一页", "往前翻"):
        return IntentDecision(
            intent=StudentIntent.CMD_PREV_PAGE, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="命中回退上一页指令枚举", llm_saved=True, raw_input=raw,
        )

    # 2. 否定/困惑优先判定（绝不能把「我还是没听懂」误判为「听懂了」）
    if any(neg in core for neg in _NEG_WORDS) or core in ("再说一遍", "重讲", "再讲一遍", "重来一遍"):
        return IntentDecision(
            intent=StudentIntent.REPHRASE_CONFUSED, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="命中重讲/未听懂枚举，直接进重讲链（省去 LLM 分流）",
            qkind=QKind.CLARIFY, llm_saved=True, raw_input=raw,
        )

    # 3. 确认听懂判定（支持「老师我听懂了，继续讲吧」等自然表达，上限放宽至 22 字）
    has_question = any(q in raw for q in _QUESTION_WORDS)
    if not has_question and len(core) <= 22:
        if any(pat in core for pat in _CONFIRM_PATTERNS):
            return IntentDecision(
                intent=StudentIntent.CONFIRM_UNDERSTOOD, stage="rule",
                latency_ms=(time.perf_counter() - t0) * 1000,
                reason="命中「听懂/明白/继续」确认枚举，直接继续讲课",
                llm_saved=True, raw_input=raw,
            )

    # 4. 举例请求
    if len(core) <= 16 and any(ex in core for ex in _EXAMPLE_WORDS):
        return IntentDecision(
            intent=StudentIntent.REPHRASE_EXAMPLE, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="命中举例请求枚举（省去 LLM 分流）",
            qkind=QKind.CLARIFY, llm_saved=True, raw_input=raw,
        )

    # 5. 情绪表达
    if any(em in core for em in _EMOTION_WORDS):
        return IntentDecision(
            intent=StudentIntent.EXPRESS_EMOTION, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="命中情绪表达枚举（省去 LLM 分流）",
            qkind=QKind.EMOTION, llm_saved=True, raw_input=raw,
        )

    # 6. 指明页码的回顾型提问（如「刚才第3页那个公式」）
    m_slide = _RECALL_SLIDE_RE.search(raw)
    if m_slide:
        return IntentDecision(
            intent=StudentIntent.ASK_RECALL, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason=f"命中跨页回顾枚举（第 {m_slide.group(1)} 页，省去 LLM 分流）",
            qkind=QKind.RECALL, backtrack_to_slide=int(m_slide.group(1)),
            llm_saved=True, raw_input=raw,
        )

    # 7. 元问题 / 前瞻问题
    if any(mt in core for mt in _META_WORDS):
        return IntentDecision(
            intent=StudentIntent.ASK_META, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="命中元问题枚举（省去 LLM 分流）",
            qkind=QKind.META, llm_saved=True, raw_input=raw,
        )
    if any(fw in core for fw in _FORWARD_WORDS):
        return IntentDecision(
            intent=StudentIntent.ASK_FORWARD, stage="rule",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="命中前瞻提问枚举（省去 LLM 分流）",
            qkind=QKind.FORWARD, llm_saved=True, raw_input=raw,
        )

    return None


SMALL_INTENT_SYSTEM_PROMPT = """你是课堂意图快速分类器。将学生的话归入以下固定枚举之一，只输出一行 JSON：
{"intent":"枚举值","slide":null,"reason":"10字内理由"}

可选枚举值（严禁自造）：
- confirm_understood : 学生表示听懂了、学会了、没问题、可以继续讲（不需老师回答）
- cmd_next_page      : 学生明确要求翻到下一页
- cmd_prev_page      : 学生明确要求回到上一页
- rephrase_confused  : 学生表示没听懂、糊涂、要求再讲一遍或换个说法
- rephrase_example   : 学生要求举个例子或造句
- ask_clarify        : 针对当前所学知识点的具体提问
- ask_recall         : 询问前面某页讲过的内容（若提到第N页则 slide 填整数 N）
- ask_forward        : 询问后面还没讲的内容
- ask_meta           : 询问学习方法、要不要背、考不考等
- express_emotion    : 表达好难、好累、不想学等情绪"""


class FastIntentRouter:
    """两级意图路由器：先走亚毫秒规则枚举，未命中时调用轻量小模型（关闭思考）快速判定枚举。"""

    def __init__(self) -> None:
        self.enabled: bool = True
        self.use_small_llm: bool = True
        self.small_model: str = ""          # 空 = 自动复用当前服务商但强制关闭思考 + 极小 token 预算
        self.small_base_url: str = ""
        self.small_api_key: str = ""
        self.history: list[dict[str, Any]] = []
        self.stats = {
            "total": 0,
            "rule_hits": 0,
            "small_llm_hits": 0,
            "direct_continues": 0,
            "llm_calls_saved": 0,
        }

    def configure(self, *, enabled: bool | None = None, use_small_llm: bool | None = None,
                  small_model: str | None = None, small_base_url: str | None = None,
                  small_api_key: str | None = None) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if use_small_llm is not None:
            self.use_small_llm = bool(use_small_llm)
        if small_model is not None:
            self.small_model = small_model.strip()
        if small_base_url is not None:
            self.small_base_url = small_base_url.strip()
        if small_api_key is not None:
            self.small_api_key = small_api_key.strip()

    def _record(self, dec: IntentDecision) -> IntentDecision:
        self.stats["total"] += 1
        if dec.stage == "rule":
            self.stats["rule_hits"] += 1
        elif dec.stage == "small_llm":
            self.stats["small_llm_hits"] += 1
        if dec.intent in (
            StudentIntent.CONFIRM_UNDERSTOOD,
            StudentIntent.CMD_NEXT_PAGE,
            StudentIntent.CMD_PREV_PAGE,
            StudentIntent.CMD_REPEAT_UNIT,
        ):
            self.stats["direct_continues"] += 1
        if dec.llm_saved:
            self.stats["llm_calls_saved"] += 1
        item = dec.to_dict()
        self.history.append(item)
        if len(self.history) > 80:
            self.history = self.history[-80:]
        return dec

    async def route(self, text: str, settings=None, slide_title: str = "") -> IntentDecision:
        """对学生输入做快速意图判定。"""
        t0 = time.perf_counter()
        raw = (text or "").strip()

        # Stage 1: 毫秒级确定性枚举规则
        if self.enabled:
            rule_dec = classify_by_fast_rules(raw)
            if rule_dec is not None:
                return self._record(rule_dec)

        # Stage 2: 轻量小模型快速判定（强制关闭思考、极短 Prompt、限制 60 tokens）
        if self.enabled and self.use_small_llm and settings is not None:
            try:
                dec = await self._call_small_llm(raw, settings, slide_title, t0)
                if dec is not None:
                    return self._record(dec)
            except Exception:
                pass

        # Stage 3: 逃生舱兜底 —— 判不出来时不伪装成确定性澄清，改为 DELEGATE（修 G1）
        fallback_dec = IntentDecision(
            intent=StudentIntent.DELEGATE,
            stage="fallback",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason="规则未命中，小模型也未给出 qkind",
            qkind=QKind.CLARIFY,
            llm_saved=False,
            raw_input=raw,
        )
        return self._record(fallback_dec)

    async def _call_small_llm(
        self, raw: str, settings, slide_title: str, t0: float
    ) -> IntentDecision | None:
        prof = settings.active
        base_url = self.small_base_url or getattr(settings, "fast_intent_base_url", "") or prof.base_url
        api_key = self.small_api_key or getattr(settings, "fast_intent_api_key", "") or prof.api_key or "ollama"
        model = self.small_model or getattr(settings, "fast_intent_model", "") or prof.model

        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=min(6.0, getattr(settings, "timeout", 30.0)),
            default_headers=prof.headers if base_url == prof.base_url else None,
        )
        user_msg = f"当前课件页：{slide_title or '课堂讲授中'}\n学生说：{raw}"
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SMALL_INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.0,
            "max_tokens": 60,
            "reasoning_effort": "none",
        }
        try:
            r = await client.chat.completions.create(**kwargs)
        except Exception:
            kwargs.pop("reasoning_effort", None)
            r = await client.chat.completions.create(**kwargs)

        content = (r.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0) if m else content)
        intent_str = str(data.get("intent") or "").strip().lower()
        intent = StudentIntent(intent_str)
        meta = INTENT_META.get(intent.value, {})
        qkind_str = meta.get("qkind")
        qkind = QKind(qkind_str) if qkind_str else QKind.CLARIFY
        slide = data.get("slide")
        bt = int(slide) if isinstance(slide, int) and slide > 0 else None
        is_direct = bool(meta.get("direct", False))
        return IntentDecision(
            intent=intent,
            stage="small_llm",
            latency_ms=(time.perf_counter() - t0) * 1000,
            reason=f"小模型({model})：{str(data.get('reason') or meta.get('label', ''))[:28]}",
            qkind=qkind,
            backtrack_to_slide=bt,
            llm_saved=is_direct,
            raw_input=raw,
        )

    def status(self, settings=None) -> dict[str, Any]:
        prof = settings.active if settings is not None else None
        eff_model = (
            self.small_model
            or (getattr(settings, "fast_intent_model", "") if settings else "")
            or (prof.model if prof else "qwen2.5:1.5b")
        )
        return {
            "enabled": self.enabled,
            "use_small_llm": self.use_small_llm,
            "small_model": self.small_model,
            "effective_model": eff_model,
            "small_base_url": self.small_base_url,
            "enums": [
                {"intent": k, **v}
                for k, v in INTENT_META.items()
            ],
            "stats": dict(self.stats),
            "recent": list(reversed(self.history[-15:])),
        }


INTENT_ROUTER = FastIntentRouter()
