"""状态定义：Phase / QKind / QAction / 课堂状态数据结构。

对应文档：
  - 《课堂交互设计》§3.2 状态定义、§3.3 状态数据结构、§4.2 提问分流
  - 《课堂状态机规范》§1 状态枚举、§3 数据结构、§6 提问分流枚举
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


def now() -> float:
    return time.monotonic()


def new_id(prefix: str = "id") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Phase(str, Enum):
    """课堂状态（状态唯一：同一时刻只有一个 phase）。"""

    LECTURING = "LECTURING"          # 讲课，播讲解单元
    PAUSING_AUDIO = "PAUSING_AUDIO"  # 页尾探针提问话术正在播出（过渡态，不参与等待）
    PAUSING = "PAUSING"              # 探针等待，学生思考中（可被打断 —— 这正是它存在的意义）
    ACKNOWLEDGING = "ACKNOWLEDGING"  # 已收到提问，播回执话术
    YIELDING = "YIELDING"            # 讲完单元，播过渡，准备让位
    REDIRECTING = "REDIRECTING"      # 回顾型：切页/定位后作答
    ANSWERING = "ANSWERING"          # 正在回答
    RESUMING = "RESUMING"            # 三步重入，回到被打断的位置


class QKind(str, Enum):
    """提问分流结果。"""

    CLARIFY = "clarify"    # 这里没懂 / 再说一遍   → 原地简短解答，不打断主线
    RECALL = "recall"      # 刚才第 3 页那个公式   → 切页再答
    FORWARD = "forward"    # 那二次函数呢 / 会考吗 → 埋钩子或划界
    META = "meta"          # 学这个有啥用 / 要不要背 → 固定话术
    EMOTION = "emotion"    # 好难啊 / 不想学了     → 先共情再拉回


class QAction(str, Enum):
    ANSWER_INLINE = "answer_inline"
    BACKTRACK_PAGE = "backtrack_page"
    HOOK = "hook"
    FIXED_REPLY = "fixed_reply"
    BRIEF_EMPATHY = "brief_empathy"


class QStatus(str, Enum):
    QUEUED = "queued"
    ANSWERING = "answering"
    ANSWERED = "answered"
    DEFERRED = "deferred"


class Comprehension(str, Enum):
    CONFIRMED = "confirmed"  # 强信号
    VAGUE = "vague"          # 弱信号，不可作掌握度证据
    CONFUSED = "confused"    # 强信号


@dataclass
class Anchor:
    element_id: str
    text_hint: str = ""
    confidence: float = 1.0


@dataclass
class LectureUnit:
    """讲解单元 —— 让位、音频、锚定、探针、翻页共用的最小单位。"""

    unit_id: str
    slide: int
    index_in_slide: int
    text: str
    anchors: list[Anchor] = field(default_factory=list)
    reveal: list[str] = field(default_factory=list)
    pause: dict = field(default_factory=dict)  # {"after_sentence": int, "sec": float}
    is_page_end: bool = False
    must_teach: bool = True
    kp: list[str] = field(default_factory=list)
    #: 课件操作（DeckOp，见 deck/ops.py）：在第几句开始时藏/显/指/聚焦/缩放。
    #: 类型写成 list 而不是 list[DeckOp] 是为了让 state 不依赖 deck 层（可脱网单测）。
    ops: list = field(default_factory=list)
    #: 单元变体（UnitVariant 的 dict 形式，v0 是主 text）：换说法 / 举例子。
    #: 类型写成 list 而不是 list[UnitVariant] 是为了让 state 不依赖上层模块（可脱网单测）。
    variants: list = field(default_factory=list)
    #: 走向图卡面上的那句话总结（≤30 字，备课期产出）。
    #: 空 = 这份计划里没有（旧 plan.json / 还没回填），由 __post_init__ 用正文兜底。
    summary: str = ""

    def __post_init__(self):
        """空 summary 在这里兜底 —— 而不是在 /graph 的载荷里。

        计划层（PlanUnit）的空 summary 是**有意义的信号**："这段还没补过"，
        回填就是照着它挑活的，所以 PlanUnit 保持可空。但运行期的单元是给图、
        给详情面板看的，"没有总结"对读者不成立 —— 那就该显示正文首句。
        兜在这一层，所有构造点（备课链路 / 回填 / 测试夹具）一视同仁，
        图只是读者，不必判空，也不必为两种来源各写一套。
        """
        if not str(self.summary or "").strip():
            self.summary = clip_summary(self.text)

    # -- 派生 -------------------------------------------------------------
    @property
    def sentences(self) -> list[str]:
        from llm import split_sentences

        return split_sentences(self.text)

    @property
    def est_sec(self) -> float:
        return estimate_seconds(self.text)


def estimate_seconds(text: str, chars_per_sec: float = 5.0) -> float:
    """中文约 5 字/秒（≈300 字/分钟，接近课堂语速）。"""
    return round(len(text) / chars_per_sec, 1)


#: 走向图卡面上那句话总结的长度上限（产品口径：≤30 字）。
SUMMARY_MAX_CHARS = 30


def clip_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """把正文压成"一句话总结"：首句放得下就用首句，放不下才硬截到 limit。

    为什么首句优先而不是一律取前 N 字：讲解单元的第一句几乎就是"这一格在讲什么"
    （"好，我们来看 pack 的两个搭配"），展开在它后面。一律等长切会把一句话切成半句，
    卡面上读到"好，我们来看 pack 的两个搭"，比短一点的首句更难看懂。

    换行压成单空格、首尾去空：这段字会进 plan.json、进 JSON 载荷、再进 HTML 卡面，
    带换行的"总结"在固定高度的 .gsum 里会撑成两行。
    截断时补"…"，总长仍不超过 limit（上限是字面的 30 个字，多一个省略号就超了）。
    """
    from llm import split_sentences

    t = " ".join(str(text or "").split())
    if not t:
        return ""
    head = (split_sentences(t) or [t])[0].strip()
    if len(head) <= limit:
        return head
    return t[:max(0, limit - 1)] + "…"


@dataclass
class Covered:
    """已讲台账（只增不改）。"""

    kp: str
    slide: int
    unit_id: str
    at: float = field(default_factory=now)


@dataclass
class Question:
    raw: str
    kind: QKind = QKind.CLARIFY
    at_unit: str = ""          # 提问时正在播的单元 —— 稀疏输入可被理解的根本
    at_slide: int = 0
    status: QStatus = QStatus.QUEUED
    action: QAction = QAction.ANSWER_INLINE
    backtrack_to_slide: int | None = None
    answer: str = ""
    rounds: int = 0
    qid: str = field(default_factory=lambda: new_id("q"))
    asked_at: float = field(default_factory=now)   # = 学生按下发送的时刻
    yielded_at: float | None = None                # = 段末让位、开始作答的时刻


@dataclass
class Interrupt:
    """让位点。"""

    unit_id: str
    at_sentence: int
    sentence_complete: bool
    raw: str
    slide: int = 0


@dataclass
class Probe:
    """一次理解探针。"""

    slide: int
    unit_id: str
    opened_at: float
    closed_at: float | None = None
    result: str = ""  # confirmed | interrupted | timeout


@dataclass
class Utterance:
    """课堂说过的一句话（台账 / 上下文 / 记录用）。"""

    speaker: str   # teacher | student | system
    text: str
    at: float = field(default_factory=now)
    unit_id: str = ""
    slide: int = 0


@dataclass
class ClassroomState:
    """课堂状态。"""

    course_id: str = "demo_math_g8"
    student_id: str = "S001"

    # 位置
    slide_index: int = 0
    seg_index: int = 0
    phase: Phase = Phase.LECTURING

    # 目标与进度
    session_goals: list[str] = field(default_factory=list)
    covered: list[Covered] = field(default_factory=list)
    resume_point: Interrupt | None = None

    # 当前单元
    unit_progress: dict[str, int] = field(default_factory=dict)  # unit_id -> 已播句数

    # 提问
    pending_questions: list[Question] = field(default_factory=list)
    active: Question | None = None                               # 正在处理的那一条
    q_rounds: dict[str, int] = field(default_factory=dict)       # kp -> 重讲轮次
    deferred: list[str] = field(default_factory=list)            # 暂放待回
    deferred_too: int = 0          # 到顶后学生又说了几次"没懂"（计数，不重复nag）

    # 理解信号
    comprehension: dict[str, str] = field(default_factory=dict)
    probe_log: list[Probe] = field(default_factory=list)

    # 记录
    transcript: list[Utterance] = field(default_factory=list)
    qa_history: list[dict[str, str]] = field(default_factory=list)
    #: 走过的路线（skip 跳讲 / revisit 补回头 / finish 提前收尾），只增不改。
    #: 为什么留在 state 而不是只记 metrics：metrics 是时间序列，"这节课到底走了哪几条路、
    #: 每条从第几格岔出、汇回到第几格、跳过了谁"是**课堂结构**本身 —— 学生端回看、
    #: 课后复盘都要按路线而不是按事件读它。只留 metrics 的话每条路线都得靠 join 拼回来。
    #: 类型写成 list[dict] 而不是 Route 对象：state 不依赖 director 层（与单元变体同一个
    #: 理由），于是这一页仍然可以脱网单测。
    route_log: list[dict] = field(default_factory=list)

    # 运行期
    clarify_asks: int = 0
    user_advanced: bool = False
    seq: int = 0

    def emit(self, speaker: str, text: str, unit_id: str = "", slide: int | None = None) -> None:
        self.seq += 1
        self.transcript.append(
            Utterance(
                speaker=speaker,
                text=text,
                unit_id=unit_id,
                slide=self.slide_index if slide is None else slide,
            )
        )
