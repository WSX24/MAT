"""课堂状态机 —— 纯逻辑，无 I/O。

对应文档：
  - 《课堂交互设计》§3 状态机、§4 交互运行时、§5 翻页判定
  - 《课堂状态机规范》§2 转移表、§4 事件接口、§5 不变量

设计要点
--------
1. **状态唯一**：同一时刻只有一个 phase。个人自学场景不需要并行状态，
   唯一状态保证"老师现在在干什么"永远可答。
2. **归位由结构保证**：`ANSWERING` 没有通向 `LECTURING` 的边 —— 任何答疑都必须
   经过 `RESUMING`。这就是文档里的 I5，上课态靠结构守住，不靠 Prompt 自觉。
3. **本模块不合成音频、不播放**：它只通过 `next_speech()` 请求"现在该说哪句话"，
   由外层（web / speech）决定怎么生成、怎么合成、怎么播放。因此可以脱网单测。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from director.decider import Director
    from mcp_server import McpBrainServer

from config import Settings
#: 理解信号的强弱口径直接复用 J 的事实派生（director/facts.py 里的 _COMPREHENSION_RANK）。
#: 不另写一张表：两处各留一份，迟早一边把 confused 当"比 vague 强"、另一边当"差不多"，
#: 于是同一节课里状态机在做降级、J 却在读升级 —— 这种错不报错，只让课后结论反过来。
from director.facts import _COMPREHENSION_RANK as COMPREHENSION_RANK
from director.facts import facts_from_state
from director.presets import load_route_presets, load_unit_presets
from director.route import Route
from eval import MetricsTracker
from llm import Ctx, LLM, action_for, build_ctx, split_sentences
from models import LessonScript
from phrases import INTENT_ROUTER, PHRASE_BANK, StudentIntent, classify_by_fast_rules
from state import (
    Anchor,
    ClassroomState,
    Comprehension,
    Covered,
    Interrupt,
    Phase,
    Probe,
    QAction,
    QKind,
    QStatus,
    Question,
    Utterance,
    now,
)

# ===========================================================================
# 回调
# ===========================================================================

@dataclass
class SpeechSeg:
    """一段要"说出来"的话。"""

    text: str
    kind: str                      # unit | ack | yield | redirect | answer | rephrase | resume | ask
    unit_id: str = ""
    anchors: list = field(default_factory=list)
    pause_after_ms: int = 0
    raise_hand: bool = False       # 告诉前端：这是提问，等学生回应
    #: 课件操作（DeckOp 对象列表）。与这段话**同时生效** —— 说到哪、指到哪是同一件事。
    ops: list = field(default_factory=list)


@dataclass
class PhaseHandlers:
    speak: Callable[[SpeechSeg], Awaitable[None]]
    notify: Callable[[str, str], Awaitable[None]]          # (level, text) 仅 UI，不出声
    phase_changed: Callable[[ClassroomState], Awaitable[None]]
    state_changed: Callable[[], Awaitable[None]]           # 同步完整快照给前端
    transcript: Callable[[Utterance], Awaitable[None]]
    page_changed: Callable[[int], Awaitable[None]]
    highlight: Callable[[list], Awaitable[None]]
    #: (在思考?, 是否显示秒表) —— 仅 UI。**只表示"在思考"这个状态，不含思考内容。**
    #: 有默认值，所以老构造点不受影响。
    thinking: Callable[[bool, bool], Awaitable[None]] | None = None

    @staticmethod
    def noop() -> "PhaseHandlers":
        """测试用：什么都不做的回调。"""
        async def _noop(*_a, **_kw) -> None:
            return None

        return PhaseHandlers(
            speak=_noop, notify=_noop, phase_changed=_noop,
            state_changed=_noop, transcript=_noop, page_changed=_noop, highlight=_noop,
            thinking=_noop,
        )


#: 触发"换讲法重讲"的学生表达（对应 §4.7 重讲链）
REPHRASE_TRIGGERS = (
    "没懂", "没听懂", "不懂", "不明白", "不理解", "不知道在讲什么",
    "再说一遍", "重讲", "再讲一遍", "没跟上",
)

#: 明确表示"懂了" —— 探针窗口里收到它就直接确认（强信号）
CONFIRM_TRIGGERS = ("懂了", "明白", "清楚", "会了", "继续", "可以", "没问题的")

#: 否定式：这些字面上命中 CONFIRM_TRIGGERS，意思却**完全相反**
#: （"不明白"里有"明白"，"不清楚"里有"清楚"）。
#: 直接复用重讲触发词 —— 它们本来就是"我没懂"的表达 —— 再补两个纯否定。
#: 不单独判它们的话，探针会把"不明白"记成听懂了并当场翻页：
#: 学生最需要重讲的那一刻，恰好被跳过去。
DENY_TRIGGERS = REPHRASE_TRIGGERS + ("不清楚", "不会")

#: 句子里出现这些 → 学生是在**问**老师，不是在回答（"明白了吗""为什么会这样"）
_ASK_MARKS = ("吗", "呢", "?", "？", "什么", "为什么", "怎么", "如何", "哪")

#: 纯确认的长度上限：超过它，一定还夹带了别的内容，不该替学生判成"我听懂了"
_CONFIRM_MAX_LEN = 12


# ---------------------------------------------------------------------------
# 路线执行的防死循环上限（skip 跳讲 / revisit 补回头 / finish 提前收尾）
# ---------------------------------------------------------------------------
# 这三个数不是性能参数，是**收敛条件**：revisit 是唯一能让课堂"往回走"的东西，
# 一旦它被反复触发或者互相嵌套，课的结束时间就不再由课表长度决定 —— 学生会永远
# 上不完这节课。三条闸各挡一种跑飞方式，缺一条都能构造出无限循环：
#
#: ① 同一条路线（route_id）整节课最多走几次。
#:    为什么要按 route_id 而不按 candidate_id：C 会反复重算同一条走向，
#:    derive_route_id（director/route.py:85）保证同一走向的名字稳定 —— 正因如此，
#:    "学生一直卡在这一段"每次都算出同一个 id，不去重就会每次边界都跳回同一格重讲。
ROUTE_MAX_PER_ID = 1
#: ② 一节课最多执行几条**不同**的路线。C 能一直造新 id，①挡不住这条。
#:    8 = 池子 CAP(5) 的一轮半余量：够 C 在一节课里调整几次走向，
#:    又不至于把每个单元都重讲一遍（那等于这节课没上）。
ROUTE_MAX_ROUTES = 8
#: ③ 同时允许几条路线叠着走。1 = 不许嵌套。
#:    嵌套的代价是**层数相乘**：每层都能把课带回前面，两层就已经不收敛，
#:    所以在补回头重讲期间，边界上选出来的新路线一律不走（只记一条 route_refused）。
ROUTE_MAX_DEPTH = 1


def classify_reply(text: str) -> str | None:
    """把学生的一句话判成 `"confirm"` / `"deny"` / `None`。

    「明白了」和「不明白」只差一个字，却分别意味着"往下讲"和"重讲" ——
    子串匹配分不开这两件事（旧实现 `k in text` 两边都判成确认），
    所以这里按**整句话**判，并且只认"整句话就是这个意思"的短句：

    * 太长（> `_CONFIRM_MAX_LEN`）→ None：夹带了别的内容，交回答疑/重讲链；
    * 带疑问词（"吗""为什么"）→ None：那是在问老师，不是回答；
    * 命中 `DENY_TRIGGERS` → `"deny"`（**否定优先**；「没问题」例外，
      它是确认不是否定，所以先显式放行）；
    * 命中 `CONFIRM_TRIGGERS` → `"confirm"`；
    * 其余 → None（普通提问）。

    宁可漏判（当成普通提问走答疑），也不要误判：把"没听懂"记成"听懂了"，
    学生就会看到老师跳过自己唯一没懂的地方。
    """
    t = (text or "").strip()
    if not t:
        return None
    dec = classify_by_fast_rules(t)
    if dec is not None:
        if dec.intent is StudentIntent.REPHRASE_CONFUSED:
            return "deny"
        if dec.intent is StudentIntent.CONFIRM_UNDERSTOOD:
            return "confirm"
    core = t.strip("。！？!?…，,、~～ \t\r\n")
    if not core or len(core) > _CONFIRM_MAX_LEN:
        return None
    if any(q in t for q in _ASK_MARKS):
        return None
    if "没问题" in core:                      # 以"没"开头，但意思是确认
        return "confirm"
    if any(d in core for d in DENY_TRIGGERS):
        return "deny"
    if any(k in core for k in CONFIRM_TRIGGERS):
        return "confirm"
    return None


def _norm(s: str) -> str:
    """归一化：只留中日韩文字和数字，用于判断"是不是在复读"。"""
    return "".join(ch for ch in (s or "") if ch.isalnum())


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar(a: str, b: str) -> float:
    """极简 bigram 相似度，够用且无依赖。"""
    if not a or not b:
        return 0.0
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))

ACK_TEXT = "收到，我把这一段讲完就来回答你。"


@dataclass
class ActiveRun:
    """单一状态对象：收拢作答生成周期状态（对应 §13.1 立单一状态对象）。"""

    gen: int = 0
    task: asyncio.Task | None = None
    done: bool = True
    prepared: bool = False
    waiting: bool = False
    cycle: int = 0

    def reset(self) -> None:
        self.gen += 1
        self.done = False
        self.prepared = False
        self.waiting = False
        self.cycle = 0
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None


class Machine:
    """课堂状态机。"""

    def __init__(self, script: LessonScript, state: ClassroomState, settings: Settings,
                 llm: LLM, h: PhaseHandlers, metrics: MetricsTracker | None = None,
                 mcp: "McpBrainServer | None" = None,
                 director: "Director | None" = None):
        self.script = script
        self.state = state
        self.settings = settings
        self.llm = llm
        self.h = h
        self.metrics = metrics or MetricsTracker()
        self.mcp: "McpBrainServer | None" = None
        #: 决策层（§9.1 第 4~6 项）。None = 这套东西整个不存在：不预取、不消费、不换讲法，
        #: 课堂回到"脚本 + 状态机自驱"。§11 最后一行是硬门槛，所以这里连一次判断都不该多花。
        self.director: "Director | None" = director
        #: 位置纪元（§6.5）。课堂位置每跳一次 +1，用来让"一格前算出的决策"作废。
        #: 不能拿 state.seq 当位置（见 _pos_key）：单元播放期间每吐一句它都会变。
        self._pos_epoch = 0
        #: 已经换上变体的单元 id。同一单元整节课只换一次 —— 换讲法会改掉 unit.text，
        #: 再进"第一句"时不能又换一次，否则同一段内容会被切成碎片。
        self._variant_switched: set[str] = set()
        #: 目标 unit_id → 在这个边界上为它选定的讲法（含**决策那一刻**的事实快照）。
        #: 键是**下一格**，不是当前格：J 是选路者（§0.1 / §6.4），选的永远是"接下来那一格
        #: 怎么讲"。在单元 N 的边界写入（_route_next_unit），在 N+1 开播时装上
        #: （_seg_unit 里 done==0）。纠偏之前这里挂的是"当前单元"，于是学生在 N 上说一次
        #: 没懂，N 会被讲三遍（原讲 → 既有重讲 → 变体又整段重播）。
        self._variant_for: dict[str, dict] = {}
        #: unit_id → 这次变体重播的观测起点（§12 阶段 1 验收④）。
        #: _apply_variant_to 写入、单元边界处消费并记 variant_replay_end：
        #: "切换前后 probe_result" 只有两处各记一次才成对 —— 只在切换那一刻记快照，
        #: 课后就永远看不到"换了讲法之后学生怎么样了"。
        self._variant_replaying: dict[str, dict] = {}
        #: 上一个边界为"下一格"选定的**路线**（跨格走向：跳讲 / 补回头 / 提前收尾）。
        #: 与 _variant_for 分开存：路线不改任何一段正文，它改的是"下一步去哪"，
        #: 所以它的执行点不在"装上变体"那一刻，而在推进出口（_advance → _take_route）。
        #: 为什么只存一条：一个边界只有一次选择的机会，存多条就意味着要在这里再定一次
        #: 优先级 —— 而优先级已经在池子里定过了（§8.2）。
        self._route_pending: dict | None = None
        #: 正在走的补回头（revisit）重讲，None = 没有。字段：route_id / merge_node /
        #: last_unit（汇回点前面那一格，讲完它就汇回）。它同时是"重讲期间不选路"的依据
        #: （见 _route_next_unit 与 _take_route 的 depth 闸）。
        self._route_replay: dict | None = None
        #: route_id → 已经走过几次（ROUTE_MAX_PER_ID）。按 id 而不是按 candidate_id 记账：
        #: C 重算同一条走向会换 candidate_id，但 route_id 是稳定幂等键。
        self._route_runs: dict[str, int] = {}

        self._lock = asyncio.Lock()
        self._busy = False
        self._finished = asyncio.Event()

        self._first_ack_pending = False
        self._probe_opened = False
        self._probe_spoken = 0
        self._ack_emits = 0
        self._pumps_started = 0
        self._turns = 0
        self._prep_task: asyncio.Task | None = None
        self._recover_task: asyncio.Task | None = None   # 降级中的后台恢复探测
        self._empty_warned = False   # "模型一句话都没产出"只提醒一次，别刷屏
        self.active_run = ActiveRun()
        self._rephrase_rounds: dict[str, int] = {}   # unit_id → 已重讲轮数

        # 说话缓冲
        self._rephrase_buf: list[str] = []
        self._answer_buf: list[str] = []
        self._cur_unit_sents: list[str] = []
        self._yield_phrase: str | None = None
        self._resume_phrase: str | None = None
        #: 外置大脑（MCP say）待播话术队列 —— 只在让位点播出（铁律 #3）
        self.agent_speech_queue: list[dict[str, Any]] = []

        # 指标
        self._t_ack_start: float | None = None
        self._t_answer_start: float | None = None

        if mcp is not None:
            self.attach_mcp(mcp)

    # -- ActiveRun 兼容属性（收拢到单一对象，保持内部读写语义一致） -----------
    @property
    def _gen_task(self) -> asyncio.Task | None:
        return self.active_run.task

    @_gen_task.setter
    def _gen_task(self, v: asyncio.Task | None) -> None:
        self.active_run.task = v

    @property
    def _gen_gen(self) -> int:
        return self.active_run.gen

    @_gen_gen.setter
    def _gen_gen(self, v: int) -> None:
        self.active_run.gen = v

    @property
    def _gen_done(self) -> bool:
        return self.active_run.done

    @_gen_done.setter
    def _gen_done(self, v: bool) -> None:
        self.active_run.done = v

    @property
    def _prepared(self) -> bool:
        return self.active_run.prepared

    @_prepared.setter
    def _prepared(self, v: bool) -> None:
        self.active_run.prepared = v

    @property
    def _gen_wait(self) -> bool:
        return self.active_run.waiting

    @_gen_wait.setter
    def _gen_wait(self, v: bool) -> None:
        self.active_run.waiting = v

    @property
    def _cycle(self) -> int:
        return self.active_run.cycle

    @_cycle.setter
    def _cycle(self, v: int) -> None:
        self.active_run.cycle = v

    def attach_mcp(self, mcp: "McpBrainServer | None") -> None:
        """挂载或卸载外置大脑 MCP Server。"""
        self.mcp = mcp
        if mcp is not None and mcp.machine is not self:
            mcp._machine = self

    def enqueue_agent_speech(
        self,
        text: str,
        *,
        seq: int = 0,
        from_version: dict[str, Any] | None = None,
        version_check: str = "ok",
    ) -> None:
        """外置大脑通过 say() 入队待播话术（非阻塞，不抢锁，由让位点消费）。"""
        sents = split_sentences(text) or [text]
        self.agent_speech_queue.append(
            {
                "seq": seq,
                "text": text,
                "sentences": sents,
                "from_version": from_version,
                "version_check": version_check,
            }
        )
        # 若课堂已启动且主循环暂时停下，唤醒主循环以便在让位点播出
        try:
            loop = asyncio.get_running_loop()
            if self._pumps_started > 0 and not self._busy and not self._finished.is_set():
                loop.create_task(self._kick())
        except RuntimeError:
            pass

    def _drain_agent_speech_into_answer_buf(self) -> int:
        """将外脑排队的补充话术倒入 _answer_buf（仅在安全让位边界调用）。"""
        added = 0
        while self.agent_speech_queue:
            item = self.agent_speech_queue.pop(0)
            for s in item.get("sentences") or []:
                if s.strip():
                    self._answer_buf.append(s.strip())
                    added += 1
        return added

    # ==================================================================
    # 对外 API
    # ==================================================================

    async def start(self) -> None:
        self.state.session_goals = list(self.script.session_goals)
        self._first_ack_pending = True     # 第一条输入必须立刻回执（I3）
        await self.h.notify("info", f"开始上课：《{self.script.title}》")
        await self._run()

    async def wait_finished(self) -> None:
        await self._finished.wait()

    async def handle_input(self, text: str, *, via: str = "text") -> None:
        """学生任何输入的唯一入口（对应规范 §4 on_user_input）。"""
        text = (text or "").strip()
        if not text:
            return
        # 让位延迟从**输入到达**算起（等锁、排队都算学生的等待），
        # 这样页面上显示的等待时长和学生实际感受一致。
        arrived = now()

        async with self._lock:
            st = self.state
            st.emit("student", text)
            await self.h.transcript(st.transcript[-1])
            self.metrics.event("input", raw=text, phase=st.phase.value)
            self.metrics.mark("first_input")

            # 外脑失联/回归边沿检测（只在状态边沿播报一次，§3.3）
            if self.mcp is not None:
                edge = self.mcp.mode.check_edge_transition()
                if edge is not None:
                    await self.h.notify(edge[0], edge[1])

            # 快速意图预判（枚举规则 <1ms + 可选轻量小模型）：
            # 对于「我听懂了」「继续讲吧」等确认或直接翻页口令，老师直接继续讲课，省下大模型开销
            fast_dec = classify_by_fast_rules(text)
            sm_dec = None
            if fast_dec is not None:
                INTENT_ROUTER._record(fast_dec)
                if fast_dec.intent is StudentIntent.CMD_NEXT_PAGE:
                    await self.h.notify("info", f"快速意图[{fast_dec.intent.value}]：直接翻到下一页（0 大模型开销）")
                    await self._goto_slide(st.slide_index + 1)
                    asyncio.create_task(self._kick())
                    return
                if fast_dec.intent is StudentIntent.CMD_PREV_PAGE and st.slide_index > 0:
                    await self.h.notify("info", f"快速意图[{fast_dec.intent.value}]：直接返回上一页（0 大模型开销）")
                    st.slide_index = max(0, st.slide_index - 1)
                    st.seg_index = 0
                    await self.h.page_changed(st.slide_index)
                    if self.mcp is not None:
                        self.mcp.emit_classroom_event("page_changed", slide=st.slide_index + 1)
                    await self._back_to_lecturing(apply_resume=False)
                    asyncio.create_task(self._kick())
                    return

            reply = classify_reply(text)

            # 若规则未直接命中，且学生在探针等待（或口语短句）中回应，可走轻量小模型快速判定枚举：
            # 一旦小模型判定为 confirm_understood，老师直接继续讲课，省下主模型思考与回答开销。
            if (
                reply is None
                and fast_dec is None
                and INTENT_ROUTER.enabled
                and INTENT_ROUTER.use_small_llm
                and INTENT_ROUTER.small_model
                and len(text) <= 28
                and not any(q in text for q in _ASK_MARKS)
            ):
                slide_obj = self.script.slide_at(st.slide_index)
                sm_dec = await INTENT_ROUTER.route(
                    text, self.settings, slide_obj.title if slide_obj else ""
                )
                if sm_dec.intent is StudentIntent.CONFIRM_UNDERSTOOD:
                    reply = "confirm"
                    await self.h.notify(
                        "info",
                        f"小模型快速意图[{sm_dec.intent.value} · {sm_dec.latency_ms:.0f}ms]：确认听懂，直接继续讲课",
                    )
                elif sm_dec.intent is StudentIntent.REPHRASE_CONFUSED:
                    reply = "deny"

            # 逃生舱（§1.2 G1, §2 D5, §10.2）：当规则与小模型都未能判定（stage="fallback"）时，
            # 记录 DELEGATE 并立即推入 urgent query 事件唤醒外置大脑，同时状态机照常 4s 快答兜底（D4）。
            triage_dec = fast_dec or sm_dec
            if triage_dec is None and reply is None:
                from phrases import IntentDecision

                triage_dec = INTENT_ROUTER._record(
                    IntentDecision(
                        intent=StudentIntent.DELEGATE,
                        stage="fallback",
                        latency_ms=0.1,
                        reason="规则未命中，小模型也未给出 qkind",
                        qkind=QKind.CLARIFY,
                        llm_saved=False,
                        raw_input=text,
                    )
                )
            if triage_dec is not None and triage_dec.stage == "fallback" and self.mcp is not None:
                handled_by = None if getattr(self.llm, "degraded", False) else "state_machine"
                self.mcp.emit_student_query(
                    text,
                    via=via,
                    triage_stage="fallback",
                    triage_verdict="unknown",
                    triage_reason=triage_dec.reason,
                    llm_saved=triage_dec.llm_saved,
                    handled_by=handled_by,
                )

            # 已经过了让位点 → 只入队，不打断当前回答（§4.5）
            if st.phase in (Phase.ACKNOWLEDGING, Phase.YIELDING, Phase.REDIRECTING,
                            Phase.ANSWERING, Phase.RESUMING):
                if reply == "confirm":
                    # 答疑/重讲途中说"懂了"：这是对**上一轮解释**的收尾，收下就行。
                    # 不入队 —— 否则老师会去"回答"一句"明白了"。
                    await self.h.notify("info", "收到，清楚就好")
                    await self.h.state_changed()
                    return
                self._enqueue(text, arrived)
                await self.h.notify("info", "已记下，老师答完这一条就来")
                await self.h.state_changed()
                return

            # 探针等待中被回应 —— 这正是 PAUSING 存在的意义
            if st.phase in (Phase.PAUSING_AUDIO, Phase.PAUSING):
                self._close_probe("interrupted")
                if reply == "confirm":
                    # 学生明确说懂了：记为强信号，直接接着往下讲
                    st.comprehension[self._cur_unit_id()] = Comprehension.CONFIRMED.value
                    await self.h.notify("info", "收到，清楚就好")
                    await self._advance_after_probe()
                    return
                self._enqueue(text, arrived)
                await self._set_phase(Phase.ACKNOWLEDGING)
                return

            # LECTURING 途中说"懂了" → 记下强信号就**继续讲**：不入队、不回执、不打断。
            # 讲课途中的"明白了"是对当前内容的确认，不是新问题；
            # 老师为它停下来"回答"，学生会以为自己说错了什么。
            if reply == "confirm":
                st.comprehension[self._cur_unit_id()] = Comprehension.CONFIRMED.value
                await self.h.notify("info", "收到，清楚就好")
                await self.h.state_changed()
                return

            # LECTURING：**只回执提示，不改状态** —— 一定要等这一段讲完再让位。
            # （回执话术由 on_speech_end 在段末发出，见 _yield_now）
            self._enqueue(text, arrived)
            await self.h.notify("info", "收到，老师讲完这一段就回答你")
            await self.h.state_changed()
            return

    async def handle_command(self, cmd: str) -> None:
        """显式控制权（对应《课堂交互设计》§5.5、§5.7）。"""
        if cmd not in ("next", "prev", "repeat"):
            return

        async with self._lock:
            st = self.state
            self.metrics.event("command", cmd=cmd, slide=st.slide_index)

            if cmd == "repeat":
                st.unit_progress.pop(self._cur_unit_id(), None)
                st.q_rounds.pop(self._cur_unit_id(), None)
                await self.h.notify("info", "重放当前这一段")
                await self._back_to_lecturing(apply_resume=False)
                asyncio.create_task(self._kick())
                return

            if cmd == "prev":
                # 往回翻 = 重讲信号（§5.7）
                if st.slide_index <= 0 and st.seg_index == 0:
                    await self.h.notify("warn", "已经是第一页了")
                    return
                self._append_resume_point(self._prev_unit_id())
                st.q_rounds.pop(self._cur_unit_id(), None)
                st.slide_index = max(0, st.slide_index - 1)
                st.seg_index = 0
                await self.h.page_changed(st.slide_index)
                if self.mcp is not None:
                    self.mcp.emit_classroom_event("page_changed", slide=st.slide_index + 1)
                await self.h.notify("info", f"回到第 {st.slide_index + 1} 页重讲")
                await self._back_to_lecturing(apply_resume=False, keep_resume=True)
                asyncio.create_task(self._kick())
                return

            # cmd == "next" ——「我懂了，继续」
            skipped = self.script.units_of(st.slide_index)[st.seg_index:]
            for u in skipped:
                for kp in u.kp:
                    if st.comprehension.get(u.unit_id) != Comprehension.CONFIRMED.value:
                        st.deferred.append(kp)
                        if self.mcp is not None:
                            self.mcp.emit_classroom_event("deferred_added", kp=kp, slide=st.slide_index + 1)
                            self.mcp.memory.record_signal_async(st.student_id, weak_kp=[kp])
            st.user_advanced = True
            self.metrics.advance_manual += 1
            await self._goto_slide(st.slide_index + 1)
            asyncio.create_task(self._kick())
            return

    async def handle_dev_action(self, action: str, **kwargs) -> None:
        """开发者模式管理操作：跳页、清空待处理提问、清空待回看、重置重讲轮次等。"""
        async with self._lock:
            st = self.state
            if action == "goto_slide":
                idx = max(0, min(self.script.slide_count - 1, int(kwargs.get("slide", 0))))
                await self._goto_slide(idx)
                await self.h.notify("info", f"[开发者] 已跳转至第 {idx + 1} 页")
                asyncio.create_task(self._kick())
            elif action == "clear_pending":
                st.pending_questions.clear()
                await self.h.notify("info", "[开发者] 已清空待处理提问队列")
                await self.h.state_changed()
            elif action == "clear_deferred":
                st.deferred.clear()
                st.deferred_too = 0
                await self.h.notify("info", "[开发者] 已清空待回看知识点列表")
                await self.h.state_changed()
            elif action == "reset_rounds":
                st.q_rounds.clear()
                self._rephrase_rounds.clear()
                await self.h.notify("info", "[开发者] 已重置所有单元重讲轮次")
                await self.h.state_changed()

    # ==================================================================
    # 主循环
    # ==================================================================

    async def _kick(self) -> None:
        """在学生输入 / 指令之后唤醒主循环（不阻塞调用方）。

        只在主循环确实停下来时才重新拉起 —— 讲解进行中时它自己会继续，
        重复拉起会让同一句话被播两遍。
        """
        if self._busy or self._finished.is_set():
            return

        async def _later() -> None:
            while self._busy:
                await asyncio.sleep(0.03)
            if not self._finished.is_set():
                await self._run()

        asyncio.create_task(_later())

    async def _run(self) -> None:
        if self._busy:
            _trace("！_run 在 _busy=True 时被调用（并发 pump 风险）")
            return
        self._busy = True
        self._pumps_started += 1
        mine = self._pumps_started
        if mine > 1:
            _trace(f"！第 {mine} 个 pump 启动（正常只应该有 1 个）")
        try:
            await self._pump()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 主循环抛异常必须**说出来**。以前它就这样静默终结了整个课堂：
            # 前端看到的只是"老师突然不说话了"，服务端日志里一行都没有，
            # 只能靠猜（实测就是这么卡住的 —— 翻页那一行抛错，课堂从此安静）。
            # 打印 traceback 是为了能直接定位；notify 是为了学生知道自己没做错。
            import traceback

            tb = traceback.format_exc()
            print(f"[machine] ❌ 课堂循环异常，已停止：\n{tb}", flush=True)
            self.metrics.event("fatal", err=f"{type(e).__name__}: {e}",
                               slide=self.state.slide_index,
                               phase=self.state.phase.value)
            try:
                await self.h.notify(
                    "error",
                    f"课堂循环出错，已停下来：{type(e).__name__}: {e}"
                    f"（服务端日志有完整堆栈；这是程序问题，不是你的操作问题）")
            except Exception:
                pass
            self._finished.set()
        finally:
            self._busy = False
            self._maybe_finished()

    async def _pump(self) -> None:
        """主循环。

        `next_speech()` 返回 None 表示**过渡完成**（例如探针超时后翻页、回答说完转入重入），
        不代表课堂结束 —— 所以循环必须继续。课堂的真正结束由 `self._finished` 标示。

        这里曾经把 None 当成"收工"，导致课堂在第一次探针超时后就静默停住。
        """
        turns = 0
        while not self._finished.is_set():
            seg = await self.next_speech()
            self._turns += 1
            if _TRACE_ON and self._turns % 5 == 0:
                _trace(f"pump#{self._turns} pump={self._pumps_started} "
                       f"phase={self.state.phase.value} "
                       f"ack_pending={self._first_ack_pending} "
                       f"pending={len(self.state.pending_questions)} "
                       f"sync={type(self._lock).__name__}")
            if seg is None:
                if self._gen_wait:
                    # 正在生成、只是还没有成句 → 等它，这不是卡住。
                    # 注意 sleep 必须在锁外（next_speech 已经返回），
                    # 否则生成任务/其它输入会被一起挡住。
                    await asyncio.sleep(0.05)
                    turns = 0
                    continue
                # 兜底：连续大量"无话可说"却没有任何位置推进，说明状态卡住了
                turns += 1
                if turns > 64:
                    await self.h.notify("error", "课堂推进异常：状态未继续变化，已停止。")
                    self.metrics.event("stuck", slide=self.state.slide_index,
                                       phase=self.state.phase.value)
                    self._finished.set()
                    return
                continue
            turns = 0
            await self.h.speak(seg)

    async def next_speech(self) -> SpeechSeg | None:
        """状态机对外请求：现在该说哪句话？

        决策完全由当前 phase 决定 —— 不由 LLM 自由裁量。
        """
        async with self._lock:
            phase = self.state.phase

            if phase in (Phase.PAUSING_AUDIO, Phase.PAUSING):
                return await self._seg_probe()
            if phase is Phase.ACKNOWLEDGING:
                return await self._seg_ack()
            if phase is Phase.YIELDING:
                return await self._seg_yield()
            if phase is Phase.REDIRECTING:
                return await self._seg_redirect()
            if phase is Phase.ANSWERING:
                return await self._seg_answering()
            if phase is Phase.RESUMING:
                return await self._seg_resume()
            return await self._seg_unit()

    # ==================================================================
    # 各状态产出
    # ==================================================================

    async def _seg_unit(self) -> SpeechSeg | None:
        st = self.state
        unit = self.script.unit_of(st.slide_index, st.seg_index)
        if unit is None:
            await self._finish_all()
            return None

        done = st.unit_progress.get(unit.unit_id, 0)
        # 变体生效（§9.1 第 6 项）：上一个单元的边界为**这一格**选好了讲法，开播时就换上。
        # 必须排在 split_sentences 之前 —— 句子表要按变体的 text 重算，晚一步就是按旧句数
        # 切句、按旧句子定位打 op，字幕与课件操作立刻错位（见 _invalidate_unit_caches）。
        if done == 0:
            self._apply_variant_to(unit)
        sents = split_sentences(unit.text)
        self._cur_unit_sents = sents
        if done >= len(sents):
            await self._unit_exhausted(unit)
            return None

        # 一格前瞻（§6.4 ① / §9.1 第 5 项）：单元第一句开始时就把**为下一格**的选路请求
        # 发出去，不 await。播放这段时间足够它算完，边界上只补一点点宽限（grace_ms）。
        if done == 0:
            self._prefetch_decision(unit)

        text = sents[done]
        st.unit_progress[unit.unit_id] = done + 1
        self._log_utterance("teacher", text, unit.unit_id)

        pause = 0
        if unit.pause and unit.pause.get("after_sentence") == done + 1:
            pause = int(float(unit.pause.get("sec", 0)) * 1000)

        return SpeechSeg(
            text=text, kind="unit", unit_id=unit.unit_id,
            anchors=list(unit.anchors) if done == 0 else [],
            pause_after_ms=pause,
            ops=self._ops_for(unit, done + 1),
        )

    #: unit_id → 已按句号解析好的 op 列表（`at_text` 只解析一次，
    #: 否则同一个操作可能在多句上重复命中）
    _ops_cache: dict = {}
    #: 锚点 id → 文字（解析 op 的定位时，用目标元素自己的文字兜底）
    _anchor_text: dict | None = None

    def _ops_for(self, unit, sentence_no: int) -> list:
        """本单元在第 sentence_no 句（1 起算）开始时该做的课件操作。

        与 `pause` 同一个约定（都是逐句产出时判定）—— 一句一个动作，
        这样"说什么"和"指哪里"天然对齐。

        两种定位：`at_sentence`（规则兜底算出来的，准）与 `at_text`
        （LLM 规划的，按"讲到含这段文字的那一句"解析）—— 见 deck/ops.py。
        坏数据不在这里报错：审核阶段 `deck.ops.check_*` 会把它挑出来。
        """
        raw = getattr(unit, "ops", None) or []
        if not raw:
            return []
        try:
            from deck.ops import DeckOp, ops_due, resolve_ops

            key = (unit.unit_id, len(raw), unit.text)
            resolved = self._ops_cache.get(key)
            if resolved is None:
                sents = self._cur_unit_sents or []
                resolved = resolve_ops(DeckOp.parse_many(raw), sents,
                                       anchor_text=self._anchor_texts())
                self._ops_cache[key] = resolved
            return ops_due(resolved, sentence_no)
        except Exception:
            return []

    def _anchor_texts(self) -> dict:
        """脚本里所有锚点的文字（段落锚点给的是那一段的原文）。"""
        if self._anchor_text is None:
            out: dict[str, str] = {}
            try:
                for slide in self.script.slides:
                    for el in slide.elements:
                        if el.text:
                            out[el.element_id] = el.text
            except Exception:
                pass
            self._anchor_text = out
        return self._anchor_text

    # ==================================================================
    # 决策层接线：位置键 / 变体生效（§6.5 / §9.1 第 4~6 项）
    # ==================================================================

    def _next_unit(self, unit):
        """本单元之后**要讲的那一格**：本页后面还有单元就是它，页尾就是下一页第一个单元。

        为什么不能用 seg_index+1：播放期间 seg_index 是"本页第几个单元"，页尾之后那个
        下标并不存在（下一页从 0 重新开始），而学生听到的下一段话是跨页连续的。
        为什么最后一格返回 None：那时没有下一格可备，任何决策都没有读者 ——
        调用方据此**什么都不做**（不装载、不预取、不消费），别在课后留下一批没人看的候选。
        """
        if unit is None:
            return None
        return self.script.next_after(unit.unit_id)

    def _prefetch_decision(self, unit) -> None:
        """为**下一格**发一次选路请求（§6.4 ①），不 await。

        三个发出点共用这一个入口：
        ① 单元第一句开播时（_seg_unit）—— 播放这段时间足够决策算完，边界上只补宽限；
        ② 重讲链起步时（_gen_rephrase）—— 事实（confused / q_rounds）那一刻才齐；
        ③ 归位之后（_back_to_lecturing）—— 这是真正生效的那一次：归位会 bump 位置纪元，
           在它之前发出的票（①②）在边界上必然被判 stale（预取与消费用的是同一把 _pos_key），
           所以重讲之后必须在这里**重发一次**，"他刚说没懂"才真的反映到对下一格的选择上。
           幂等：Director.prefetch 覆盖槽位、候选池按候选 id 覆盖，重发只是换一张新票。

        为什么"下一格已经有讲法了"就不再预取：一次装载就是一次池子重置（CAP=5），
        对同一格反复备货只会把别人的货挤掉，而且决策已经在 _variant_for 里了。

        `load_unit_presets` 必须排在 `prefetch` 之前：预取读的是**此刻**的池子快照
        （Director.prefetch 里当场筛候选），先发请求再装池子，下一格的候选就永远少那两条。
        装的是**下一格**的变体（不是本单元的、更不是整页的）：CAP 只有 5，装一整页立刻满，
        后面的单元再也装不进来（理由见 load_unit_presets 的上货纪律）。

        为什么候选的 target_unit 写的是**下一格的真实 unit_id**（不是空串）：空串在 §4.4 里
        是"任意单元"，一旦 C 上线（池子里会同时有给 N+5、N+8 备的货），J 就可能挑中一条
        本来给远期某一格备的变体、再被装到下一格上 —— 学生听到的是另一段内容，
        而且不会报错。事实来源（本单元）与候选目标（下一格）在 Director 上是两个参数：
        前者给 facts，后者给筛选键。

        **同一个装载点还要装一次预置路线**（备课期 plan.json 的 routes，见
        presets.load_route_presets）：路线的窗口是"岔出格 == 正在讲的这一格"，不是"下一格"，
        因为 J 正是在听完这一格之后才决定"下一格往哪走"。两批货进同一个池子、同一套纪律，
        所以它们必须挤在同一个装载点里装完 —— 装到别处就等于让 J 有时看得见路线、
        有时看不见，而"为什么这一条从来没被走"在账上查不出来。
        """
        if self.director is None:
            return
        target = self._next_unit(unit)
        if target is None:               # 最后一格：没有下一格可备
            return
        if (target.unit_id in self._variant_for
                or target.unit_id in self._variant_switched):
            return                       # 下一格已经有讲法了，别再重装一遍池子
        st = self.state
        #: 这里传的 **必须是单元序号**（§4.4 的"单元序号超过 expires_at_seq 即作废"），
        #: 不能传 st.seq。两套时钟长得很像、混用的后果却完全不可见：
        #:   · st.seq 是**句子计数器** —— 每吐一句 +1（_log_utterance → emit），
        #:     一次"没懂"的重讲链就能让它涨 6；
        #:   · expires_at_seq 是**单元序号** —— C 按 `ordinals[target] + 余量` 记账
        #:     （agent/producer.py），单位是"第几格"。
        #: 混用之后 C 给"当前+5"备的货，其 expires 值是个位数，而走到那格之前每次装载
        #: 都会把它和几十上百的句子计数器比一次 —— 于是**每一条 C 的候选都会在到货前
        #: 被判过期**，C 写得再对也永远到不了 J 手里，而且账上只表现为"后面没换过讲法"。
        #: presets 用同一把尺子也完全等价：每次装载的目标都往后推一格，旧货照旧在下一次
        #: 装载时被淘汰（"只服务于眼前这一格"这条容量纪律不变）。
        seq = max(0, self.script.order_index(target.unit_id))
        now = time.monotonic()
        load_unit_presets(self.director.pool, target, seq=seq, now=now,
                          target_unit=target.unit_id)
        # 预置路线：只在"岔出格 == 正在讲的这一格"时装（窗口纪律见 presets.route_candidates）。
        # 必须排在 prefetch 之前，理由与上一条相同：预取读的是**此刻**的池子快照，晚一步，
        # 这个边界就看不见那条路线，预置分支在真实课堂里一次都不会被选中。
        # 装的是 fork_node = **本单元**的格号（不是 target 的）：两条线差一格，
        # 混用不会报错，只会"永远装不上"或者"装上了却不是这个边界"。
        load_route_presets(self.director.pool, self.script.routes,
                           fork_node=self.script.order_index(unit.unit_id),
                           fork_unit=unit.unit_id,
                           target_unit=target.unit_id, now=now)
        self.director.prefetch(unit, st, target_unit=target.unit_id,
                               position=self._pos_key)

    async def _route_next_unit(self, unit) -> None:
        """单元边界：为**下一格**选一条讲法，记进 `_variant_for`（§9.1 第 4 项）。

        这是 J 的正确位置：**选路者，不是补救者**。学生说没懂时"当场换讲法重讲"是
        `_gen_rephrase` 的活（既有机制，4 轮递进），J 不该去抢；J 管的是"接下来那几格
        怎么讲"—— §4.4 的 `Candidate.target_unit` 指向具体某一个单元、§4.4 的
        `expires_at_seq` 按单元序号作废、§7.4 的生产窗口 `[+5,+15]` 全都在说同一件事：
        候选是给**未来那几格**备的货。所以这里一个字的正文都不改、不重播、
        不碰 unit_progress —— 本单元已经讲完，学生听到的下一段话才该是新讲法。

        为什么消费点必须在"推进到下一单元"之前：晚一行，改的就不是下一格了
        （位置键取自此刻的 (页, 段)，与预取时用的是同一把 _pos_key）。

        成功时记下的快照（probe_before / facts_before）是**做决定这一刻**的事实 ——
        也就是单元 N 的表现。variant_applied 在 N+1 开播时才发出，快照跟着逻辑走，
        于是"这次换讲法之前学生什么状态"不会因为一个页尾探针而漂移。

        **两种载荷**在这里分道：变体进 `_variant_for`（下一格开播时装上），
        路线进 `_route_pending`（由推进出口 `_advance` → `_take_route` 执行）。
        分道的原因：变体只换一段正文，落地点是"这一格开始播"；路线换的是"下一步去哪"，
        落地点是"这一格讲完之后往哪走" —— 后者只能由推进的**那一个**出口来执行。
        """
        if self.director is None:
            return
        if self._route_replay is not None:
            # 补回头重讲期间**不选路**，两个理由都在这里：
            # ① 重讲的意义就是"照原样再讲一遍"，此刻换上另一种说法等于不是重讲了；
            # ② 再选出一条路线就会与正在走的那条叠在一起 —— 那正是 depth 闸要挡的
            #    不收敛（见 ROUTE_MAX_DEPTH）。挡在"发请求"之前而不是之后：接过来了
            #    却不走，那条候选已经是 consumed 的，等于白白销毁一条路线。
            return
        target = self._next_unit(unit)
        if target is None:               # 最后一格：没有下一格可讲
            return
        if (target.unit_id in self._variant_for
                or target.unit_id in self._variant_switched):
            return                       # 已经为下一格定过货 / 它已经换过讲法
        applied = await self.director.apply_at_boundary(
            unit, self.state, target_unit=target.unit_id, position=self._pos_key)
        if applied is None:
            return                       # 没选 / 超时 / 位置变了 → 下一格照常用 v0（§8.3）
        if applied.get("payload") == "route":
            # 路线：结构（fork/steps/merge/est_sec/reason）已经在交接单里一次给全，
            # 这里只存下来 + 记一条"边界上选定了路线"，**不在这里执行**。
            # 为什么不能在这里执行：位置还没往前推进（推进发生在 _advance 里），
            # 在这个位置跳会跳两次 —— 而"跳两格"这种错不会报错，只会少讲一段课。
            self._route_pending = applied
            self.metrics.event(
                "route_routed", site=unit.unit_id,
                candidate_id=applied.get("candidate_id", ""),
                route_id=applied.get("route_id", ""),
                route_kind=applied.get("kind", ""),
                fork=applied.get("fork_node", 0),
                merge=applied.get("merge_node", 0),
                position=self._pos_key,
            )
            return
        probe_before, facts_before = self._facts_snapshot(unit)
        self._variant_for[target.unit_id] = {
            **applied,
            "target_unit": target.unit_id,
            "decided_at": unit.unit_id,
            "probe_before": probe_before,
            "facts_before": facts_before,
        }
        self.metrics.event(
            "variant_routed", site=unit.unit_id, target=target.unit_id,
            candidate_id=applied.get("candidate_id", ""),
            variant_id=applied.get("variant_id", ""),
            position=self._pos_key,
        )

    @property
    def _pos_key(self) -> tuple[int, int, int]:
        """课堂位置键：(页, 段, 位置纪元)。预取与消费必须用**同一个**它（§6.5）。

        为什么不用 state.seq（方案 §6.5 写的就是它）：seq 是"课堂上说过几句话"的计数，
        `_log_utterance` → `state.emit`（state.py:212）每吐一句就 +1，而 `_seg_unit`
        每吐一句都调一次 `_log_utterance`。单元开始时预取、单元结束时消费，两端 seq
        必然差了这个单元的句数 —— 于是**每个单元都会被判成位置变了**，J 永远退回 v0，
        整套东西安静地失效（不报错，只是从不生效）。

        所以位置由状态机自己定义：只有换页 / 换段 / 让位归位 / 手动翻页之后才算"位置变了"，
        单元逐句播放期间它保持不变。纪元（`_pos_epoch`）就是这些跳转的累计值 ——
        页与段回到原值时纪元不会跟着回去，于是"让位 → 归位后又回到同一格"也能被识破。
        """
        return (self.state.slide_index, self.state.seg_index, self._pos_epoch)

    def _bump_pos_epoch(self) -> None:
        """课堂位置跳转（让位 / 归位 / 翻页 / 回退）时调用：让一格前算出的决策作废。

        director 不在场时连计数器都不动：这套东西全拆掉时，状态机必须与改动前逐字等价
        （§11 最后一行）。
        """
        if self.director is not None:
            self._pos_epoch += 1
            # 跳转同时结束一次变体重播（翻页 / 让位 / 归位都可能发生在重播途中）。
            # 不在这里收账，那条观测就永远留在 dict 里：课后还挂着"正在重播"，
            # 而 §12 验收④要的正是每一次重播的去向。
            for unit_id in list(self._variant_replaying):
                unit = self.script.unit(unit_id)
                if unit is None:
                    self._variant_replaying.pop(unit_id, None)
                else:
                    self._record_variant_replay_end(unit, "jump")

    def _invalidate_unit_caches(self, unit_id: str) -> None:
        """把按旧 text 算出来的缓存全部作废（§9.1 第 6 项）。

        `_cur_unit_sents` 是 `_ops_for` 解析 `at_text` 用的句子表：换了文本还留着它，
        op 就按旧句数 / 旧句子定位打点，字幕与课件操作立刻错位。
        `_ops_cache` 的键里虽然带 `unit.text`（所以新文本不会命中旧条目），但旧条目仍会
        长期占着，且"键里带 text"这件事是缓存自己的约定 —— 换文本时明确清掉本单元的条目，
        就不必依赖那条约定继续成立。
        `_anchor_text` 不动：它来自脚本里的元素文字，变体不改变课件（§0.4 铁律 1）。
        """
        self._cur_unit_sents = []
        for key in [k for k in self._ops_cache if isinstance(k, tuple) and k[:1] == (unit_id,)]:
            self._ops_cache.pop(key, None)

    # -- 观测：切换前后的 probe_result（§12 阶段 1 验收④） -----------------

    def _facts_snapshot(self, unit) -> tuple[str, str]:
        """(最近一次有结论的探针, ≤100 字事实块) —— **只给观测用**。

        为什么整段 try/except：它是观测，不是课堂逻辑。一次事实派生把边界拖进异常路径，
        代价是整节课停在这个单元上；而它算不出来最多只丢一行课后数据 —— 两件事不对等，
        所以这里宁可返回两个空串，也绝不让观测有机会影响上课。
        为什么复用 facts_from_state 而不是自己遍历一遍 probe_log：观测记的事实必须与 J
        看到的事实出自同一个算法，否则课后对着两条对不上的数只能得出相反的结论。
        """
        try:
            facts = facts_from_state(self.state, unit)
            return facts.probe_result, facts.to_prompt_block()
        except Exception:
            return "", ""

    def _record_variant_replay_end(self, unit, reason: str) -> bool:
        """记一次变体重播的结束，返回是否真的有一条在记（§12 阶段 1 验收④）。

        为什么在边界上就记、而不是只在"正常讲完"时记：被学生提问打断的那几次恰恰是
        最该看的样本（学生的反应就是答案），只在整段播完时记，等于把最有信息量的几条删掉。
        reason 只说"怎么结束的"：done 讲完 / yield 让位 / jump 位置跳转，
        三种都不许静默 —— 一条重播只有去向明确，切换前后的对比才说得清。
        """
        info = self._variant_replaying.pop(unit.unit_id, None)
        if info is None:
            return False
        probe_after, facts_after = self._facts_snapshot(unit)
        self.metrics.event(
            "variant_replay_end", unit=unit.unit_id, reason=reason,
            label=info.get("label", ""), variant_id=info.get("variant_id", ""),
            # 前一并带上：一条事件就能读出"这一格换过讲法之后学生怎么样"，
            # 不必再去和 variant_applied 做一次 join。
            probe_before=info.get("probe_before", ""), probe_after=probe_after,
            facts_before=info.get("facts_before", ""), facts_after=facts_after,
            sec=round(time.monotonic() - float(info.get("t") or 0.0), 3),
        )
        return True

    def _apply_variant_to(self, unit) -> bool:
        """把为**这一格**选定的讲法装上（§9.1 第 6 项），返回是否真的装上了。

        只由 `_seg_unit` 在 `done == 0` 时调用 —— 也就是说这一格**还没开过口**：
        所以这是"从头照另一条讲法讲"，不是"重播"。不动 seg_index、也不清 unit_progress
        （它本来就是 0，这里用断言把它钉住），学生不会听到同一段话被讲第二遍。

        三件事必须一起做完，缺一件就是课堂上的错位：
        1. 换 text；anchors / ops **空 = 沿用主单元**（§4.1），非空才替换。
        2. 依赖 text / ops / sentences 的缓存全部作废（见 _invalidate_unit_caches）——
           而且调用方必须在本函数**之后**才切句，句子表要按新文本重算。
        3. 记下观测起点：variant_applied（这一刻）+ variant_replay_end（这一格讲完/被打断）。
        """
        applied = self._variant_for.pop(unit.unit_id, None)
        if applied is None:
            return False
        st = self.state
        # 断言的是**结构保证**，不是用户输入：本函数只可能在本单元第一句之前被调用。
        # 有人把它挪到"讲到一半"去，代价是学生听到"另一种说法的后半段"，
        # 那比一条断言响得多，所以宁可在这里炸。
        assert st.unit_progress.get(unit.unit_id, 0) == 0, \
            f"{unit.unit_id} 已经有进度，变体必须从头讲（§12 验收③）"
        text = str(applied.get("text") or "")
        if text.strip():                 # 空文本的变体等于没装：P 侧体检会拦，这里只保证不静音
            unit.text = text
        anchors = applied.get("anchors") or []
        if anchors:
            # 变体的锚点是 dict 形式（§4.1），状态机与前端用的是 Anchor 对象；
            # 不转，前端取 anchors[0].element_id 时就会在"变体自带锚点"的单元上炸掉。
            unit.anchors = [
                a if isinstance(a, Anchor) else Anchor(
                    element_id=str(a.get("element_id") or ""),
                    text_hint=str(a.get("text_hint") or ""),
                )
                for a in anchors
            ]
        ops = applied.get("ops") or []
        if ops:
            unit.ops = list(ops)
            # reveal 是 build_lesson_script 从 ops 派生的（script_gen.py:160），web.py 靠它
            # 决定这一页哪些元素到"讲到"才出现；换了 ops 不重算，分步显示就还按主单元的走。
            try:
                from deck.ops import DeckOp

                unit.reveal = [op.target for op in DeckOp.parse_many(unit.ops)
                               if op.op in ("reveal", "step") and op.target]
            except Exception:
                pass                     # 坏 ops 不该把课堂拖进异常路径（审核阶段会报出来）
        self._variant_switched.add(unit.unit_id)
        self._invalidate_unit_caches(unit.unit_id)

        # 观测（§12 验收④）：这是课后唯一能回答"哪些单元被切过、切成了哪个变体"的证据。
        # probe_before / facts_before 是**做决定那一刻**（上一格的表现）的快照，由
        # _route_next_unit 一并带过来：在这里重算就变成"本格开播时"的事实，页尾探针
        # 一超时就能把它从 "" 改成 timeout，验收④要的那条基线随即漂移。
        probe_before = applied.get("probe_before", "")
        facts_before = applied.get("facts_before", "")
        self.metrics.event(
            "variant_applied", unit=unit.unit_id,
            variant_id=applied.get("variant_id", ""),
            label=applied.get("label", ""),
            candidate_id=applied.get("candidate_id", ""),
            # 这次选路是在哪个单元的边界上定下来的 —— 事件里带上，课后不必自己推。
            decided_at=applied.get("decided_at", ""),
            position=self._pos_key,
            probe_before=probe_before, facts_before=facts_before,
        )
        # 重播记账：边界上（on_speech_end）消费它并记 variant_replay_end。
        # 与 _variant_switched 分工不同 —— 那个防"同一单元被反复切"，这个只观测这一次重播。
        self._variant_replaying[unit.unit_id] = {
            "label": applied.get("label", ""),
            "variant_id": applied.get("variant_id", ""),
            "probe_before": probe_before,
            "facts_before": facts_before,
            "t": time.monotonic(),
        }
        if self.mcp is not None:
            self.mcp.emit_classroom_event(
                "variant_applied", slide=st.slide_index + 1, unit_id=unit.unit_id,
                variant_id=applied.get("variant_id", ""),
                label=applied.get("label", ""),
                candidate_id=applied.get("candidate_id", ""),
            )
        return True

    async def _seg_ack(self) -> SpeechSeg | None:
        self._ack_emits += 1
        if self._ack_emits > 3:
            _trace(f"！回执被重复产出第 {self._ack_emits} 次 "
                   f"pending={len(self.state.pending_questions)} "
                   f"busy={self._busy} pumps={self._pumps_started}")
        if self._first_ack_pending:
            self._first_ack_pending = False
        self._t_ack_start = now()
        ack_txt = PHRASE_BANK.get("ack") or ACK_TEXT
        self._log_utterance("teacher", ack_txt, self._cur_unit_id())
        return SpeechSeg(text=ack_txt, kind="ack")

    async def _seg_yield(self) -> SpeechSeg | None:
        q = self.state.pending_questions[0] if self.state.pending_questions else None
        # 让位话术在这里第一次被需要。它和"分流 + 生成回答"是**并行**的
        # （见 _yield_now 起的后台任务），所以正常情况下已经好了，不用等。
        await self._await_prep()
        if self._yield_phrase is None:
            ctx = build_ctx(self.script, self.state)
            self._yield_phrase = await self.llm.yield_phrase(ctx, q.raw if q else "")
        self._log_utterance("teacher", self._yield_phrase, self._cur_unit_id())
        return SpeechSeg(text=self._yield_phrase, kind="yield")

    async def _seg_redirect(self) -> SpeechSeg | None:
        """回顾型：把画面切回那一页，但**让位点保持在被打断的那一页**，
        这样回答完还能带着学生回到主线上（§4.2 recall + §4.6 归位）。"""
        st = self.state
        if st.resume_point is None:
            await self._set_phase(Phase.ANSWERING)
            return None
        target = st.resume_point.slide
        if target != st.slide_index:
            if target < st.slide_index:
                phrase = (
                    PHRASE_BANK.get("redirect_prev", page=target + 1)
                    or f"这个我们得回第 {target + 1} 页去看一眼，就是这一行。"
                )
            else:
                phrase = (
                    PHRASE_BANK.get("redirect_next", page=target + 1)
                    or f"这个其实在第 {target + 1} 页，我们先翻过去看一眼。"
                )
            st.slide_index = target          # 只动视觉位置
            st.seg_index = 0
            await self.h.page_changed(target)
        else:
            phrase = PHRASE_BANK.get("redirect_same") or "就在这一页，我们一起看这一行。"
        await self._set_phase(Phase.ANSWERING)
        self._log_utterance("teacher", phrase, self._cur_unit_id())
        return SpeechSeg(text=phrase, kind="redirect")

    async def _seg_answering(self) -> SpeechSeg | None:
        """ANSWERING 的统一出口：先判定走"回答"还是"重讲链"。

        一个作答周期**只生成一次**（`_prepared` 为周期门）：学生说没懂就升一轮重讲，
        这一轮没播完之前不会再去生成第二遍 —— 否则一个周期会被拆成两轮，
        重讲上限（4 轮）会被话轮数而不是学生次数烧完。
        """
        # 生成是后台任务（边生成边播），这里只保证它被起过一次
        if not self._prepared:
            await self._prepare()          # 分流 + 起生成任务（流式切句）

        # 生成还没结束、但当下没有现成的句子 → 让 pump 稍等，不要提前收尾
        if not self._answer_buf and not self._rephrase_buf and not self._gen_done:
            self._gen_wait = True
            return None
        self._gen_wait = False

        if self._rephrase_buf:             # 重讲链（"没听懂"）
            text = self._rephrase_buf[0]
            if self.state.active is not None:
                self.state.active.answer = (f"{self.state.active.answer} {text}").strip()
            self._log_utterance("teacher", text, self._cur_unit_id())
            return SpeechSeg(text=text, kind="rephrase", unit_id=self._cur_unit_id())

        # 若内脑本轮回答已播完，检查外置大脑是否有补充话术排队，在进入 RESUMING 前一并播出（§3.2 & §9.3）
        if not self._answer_buf and self.agent_speech_queue:
            self._drain_agent_speech_into_answer_buf()

        if self._answer_buf:
            text = self._answer_buf[0]
            if self.state.active is not None:
                self.state.active.answer = (f"{self.state.active.answer} {text}").strip()
            self._log_utterance("teacher", text, self._cur_unit_id())
            return SpeechSeg(text=text, kind="answer", unit_id=self._cur_unit_id())

        await self._set_phase(Phase.RESUMING)
        return None

    async def _collect_clean(self, stream, question: str = "") -> list[str]:
        """收流 → 切句 → 去掉"复读"。

        小模型在被要求"不要重讲已讲过的内容"时，反而会把刚讲过的那句话**原样抄一遍**。
        这不是状态机的问题，但学生听到的就是复读 —— 所以在这里用确定性的方式挡掉：
          - 和刚刚讲过的教师台词高度重合的句子（≥0.7）
          - 和学生的原话高度重合的句子（模型把问题抄回来当回答）
        """
        sents = await _collect_sentences(stream)
        # 最近 8 条教师台词（含刚播完的讲解句）—— 复读的判定基准
        recent = [u.text for u in self.state.transcript if u.speaker == "teacher"][-8:]
        out: list[str] = []
        for s in sents:
            ns = _norm(s)
            if len(ns) >= 6 and any(_similar(ns, _norm(r)) >= 0.7 for r in recent if r):
                _trace(f"丢掉复读句：{s[:36]}")
                continue
            if question and len(ns) >= 6 and _similar(ns, _norm(question)) >= 0.8:
                _trace(f"丢掉抄问题句：{s[:36]}")
                continue
            out.append(s)
        return out or sents[:1]

    async def _seg_resume(self) -> SpeechSeg | None:
        st = self.state
        rp = st.resume_point
        if rp is None:
            await self._back_to_lecturing()
            return None

        unit = self.script.unit(rp.unit_id)
        anchor_hint = unit.anchors[0].text_hint if (unit and unit.anchors) else "刚才的位置"
        if self._resume_phrase is None:
            ctx = build_ctx(self.script, st)
            last_q = next((u for u in reversed(st.transcript) if u.speaker == "student"), None)
            self._resume_phrase = await self.llm.resume_phrase(ctx, last_q.text if last_q else "")

        # 认知层 + 定位层（状态机给定，可在开发者话术库配置） / 续接层
        prefix = (
            PHRASE_BANK.get("resume_prefix", anchor=anchor_hint)
            or f"好，我们回到刚才那里——{anchor_hint}。"
        )
        text = f"{prefix}{self._resume_phrase}"
        self._log_utterance("teacher", text, rp.unit_id)
        return SpeechSeg(text=text, kind="resume", unit_id=rp.unit_id,
                         anchors=list(unit.anchors) if unit else [])

    async def _seg_probe(self) -> SpeechSeg | None:
        """页尾探针。先进 PAUSING_AUDIO 播提问话术，播完才开等待窗口。"""
        st = self.state
        _trace(f"探针 slide={st.slide_index} phase={st.phase.value} "
               f"opened={self._probe_opened} seg={st.seg_index}")

        if st.phase is Phase.PAUSING_AUDIO:
            await self._set_phase(Phase.PAUSING)
            unit = self.script.unit_of(st.slide_index, st.seg_index)
            self._probe_spoken += 1
            _trace(f"探针话术 #{self._probe_spoken} slide={st.slide_index} "
                   f"seg={st.seg_index} unit={unit.unit_id if unit else None}")
            probe_txt = (
                PHRASE_BANK.get("probe_ask")
                or "到这里清楚吗？不清楚可以直接说没听懂。"
            )
            self._log_utterance("teacher", probe_txt, unit.unit_id if unit else "")
            return SpeechSeg(
                text=probe_txt,
                kind="ask",
                unit_id=unit.unit_id if unit else "",
                raise_hand=True,
            )

        # PAUSING：等待窗口
        if not self._probe_opened:
            self._probe_opened = True
            self.metrics.on_probe_open()
            st.probe_log.append(Probe(slide=st.slide_index, unit_id=self._cur_unit_id(),
                                      opened_at=now()))
            if self.mcp is not None:
                self.mcp.emit_classroom_event(
                    "probe_opened", slide=st.slide_index + 1, unit_id=self._cur_unit_id()
                )

        _trace(f"探针窗口打开 {self.settings.probe_window}s "
               f"slide={st.slide_index} unit={self._cur_unit_id()}")
        try:
            await asyncio.wait_for(asyncio.shield(self._wait_student()),
                                   timeout=self.settings.probe_window)
        except asyncio.TimeoutError:
            pass

        self._probe_opened = False
        _trace(f"探针窗口结束 phase={st.phase.value} "
               f"pending={len(st.pending_questions)}")

        # 若探针等待窗口期间外脑有补充话术入队，则在让位窗口播出
        if st.phase is Phase.PAUSING and not st.pending_questions and self.agent_speech_queue:
            self._close_probe("interrupted")
            await self._yield_for_agent()
            return None

        # 窗口因学生输入而结束 → 让位链路会接手
        if st.phase is not Phase.PAUSING or st.pending_questions:
            return None

        # 超时无响应：记弱信号，继续推进（§6.3、I8）
        self._close_probe("timeout")
        self._note_comprehension(self._cur_unit_id(), Comprehension.VAGUE.value)
        if self.mcp is not None:
            self.mcp.emit_classroom_event(
                "probe_timeout", slide=st.slide_index + 1, unit_id=self._cur_unit_id()
            )
        _trace(f"探针超时 → 自动翻页 {st.slide_index} → {st.slide_index + 1}")
        await self._goto_slide(st.slide_index + 1)
        _trace(f"翻页完成 phase={st.phase.value} slide={st.slide_index}")
        return None

    # ==================================================================
    # 事件：一段话说完了
    # ==================================================================

    async def on_speech_end(self, kind: str) -> None:
        async with self._lock:
            st = self.state
            if self.mcp is not None:
                self.mcp.emit_classroom_event(
                    "speech_finished", seg_kind=kind, unit_id=self._cur_unit_id()
                )
                edge = self.mcp.mode.check_edge_transition()
                if edge is not None:
                    await self.h.notify(edge[0], edge[1])

            if kind == "unit":
                unit = self.script.unit(self._cur_unit_id())
                if unit is None:
                    return
                sents = self._cur_unit_sents or split_sentences(unit.text)
                done = st.unit_progress.get(unit.unit_id, 0)
                if done < len(sents):
                    return                       # 本单元还有句子，继续
                for kp in unit.kp:
                    st.covered.append(Covered(kp=kp, slide=unit.slide, unit_id=unit.unit_id))
                self.metrics.event("unit_done", unit=unit.unit_id, sec=unit.est_sec)
                if self.mcp is not None:
                    self.mcp.emit_classroom_event(
                        "unit_finished", unit_id=unit.unit_id, slide=unit.slide + 1
                    )

                # 观测（§12 阶段 1 验收④）：被换成变体的那一格讲到这个边界就是一次结束。
                # 必须排在让位判断**之前**：被学生提问打断的那一次同样是一次结束，
                # 而且按 §9.1 的顺序，学生提问优先于"再选一次讲法"。
                self._record_variant_replay_end(
                    unit, "yield" if st.pending_questions else "done")

                if st.pending_questions:          # 这一段讲完了 → 让位
                    await self._yield_now()
                    return

                # 决策点（§9.1 第 4 项）：**在这个单元的边界上决定下一格怎么讲**。
                # 顺序是**学生提问 > 外脑插播 > 变体选择**，所以它排在让位判断之后。
                # 它必须落在"推进到下一单元"之前：晚一行，位置键就已经是下一格的了。
                # 注意它**不再替换本单元**：本单元已经讲完，学生一个字的重复都不该再听到
                # （见 _route_next_unit 的说明）。
                await self._route_next_unit(unit)

                if self.agent_speech_queue:       # 外脑有排队补充话术 → 在让位点插播（§7.1, §10.2）
                    await self._yield_for_agent()
                    return
                # 页尾探针 / 下一格 / 路线（skip、revisit、finish）全都在**同一个**推进出口里
                # 判一次（见 _advance）：这一处与 _unit_exhausted、_advance_after_probe 共用它。
                await self._advance(unit)
                return

            if kind == "ack":
                if self._t_ack_start:
                    self.metrics.on_speech(self._t_ack_start)
                self._first_ack_pending = False      # 回执已经给过了
                _trace(f"on_speech_end(ack) pending={len(st.pending_questions)} "
                       f"phase={st.phase.value}")
                if st.pending_questions:
                    await self._set_phase(Phase.YIELDING)
                else:
                    await self._back_to_lecturing()
                return

            if kind == "yield":
                self._yield_phrase = None
                await self._dispatch()
                return

            if kind == "redirect":
                await self._set_phase(Phase.ANSWERING)
                return

            if kind == "answer":
                self._answer_buf.pop(0)
                if self._answer_buf:
                    return                        # 回答还没说完（§4.5：不打断）
                if self.agent_speech_queue:       # 外脑刚追加了补充话术，继续说完
                    self._drain_agent_speech_into_answer_buf()
                    if self._answer_buf:
                        return
                if st.active is not None:
                    st.active.status = QStatus.ANSWERED
                    if st.active.raw and st.active.answer:
                        st.qa_history.append({"q": st.active.raw, "a": st.active.answer})
                        if len(st.qa_history) > 10:
                            st.qa_history = st.qa_history[-10:]
                await self._set_phase(Phase.RESUMING)
                return

            if kind == "rephrase":
                self._rephrase_buf.pop(0)
                if self._rephrase_buf:
                    return
                if st.active is not None:
                    st.active.status = QStatus.ANSWERED
                    if st.active.raw and st.active.answer:
                        st.qa_history.append({"q": st.active.raw, "a": st.active.answer})
                        if len(st.qa_history) > 10:
                            st.qa_history = st.qa_history[-10:]
                # **这里没有决策点**，而且不该有。
                # 曾经在这里加过第二个决策点：学生刚说没懂、老师刚重讲完，于是当场换一种
                # 讲法把这一段再讲一遍。实测的后果是同一段被讲三遍 —— 原讲 4 句 →
                # 既有机制的重讲（这是对的）→ 归位的变体版又整段 4 句（只是多了个
                # "说白了，"前缀）。根因不是时机，是**决策对象选错了**：J 是选路者，
                # 它拿的是"下一格怎么讲"（§4.4 target_unit / §7.4 [+5,+15] 都在说这件事），
                # 而"学生没懂要当场换讲法重讲"是 _gen_rephrase 的职责，4 轮递进，一个字不改。
                # 所以重讲结束就照原样回 RESUMING → 归位，不碰任何变体。
                # 注意：**不在这里清 q_rounds**。
                # 重讲链是"按单元升级"的（换说法 → 举例 → 拆步骤 → 暂放），
                # 只有换页或换话题才重置；否则每轮都被清空，上限永远到不了。
                await self._set_phase(Phase.RESUMING)
                return

            if kind == "resume":
                self.metrics.on_resume(ok=True)
                await self._back_to_lecturing()   # 里面会把"为下一格"的选路请求重发一次
                return

            if kind == "ask":
                return                            # 等待窗口由 next_speech 打开

    # ==================================================================
    # 让位与归位
    # ==================================================================

    async def _yield_for_agent(self) -> None:
        """在让位点（sentence_complete=True）插播外置大脑排队的补充话术，播完经 RESUMING 归位（§7.1, §9.3）。"""
        self._bump_pos_epoch()
        st = self.state
        uid = self._cur_unit_id()
        st.resume_point = Interrupt(
            unit_id=uid,
            at_sentence=st.unit_progress.get(uid, 0),
            sentence_complete=True,
            raw="[mcp_agent]",
            slide=st.slide_index,
        )
        self._clear_buffers()
        self._drain_agent_speech_into_answer_buf()
        self._prepared = True
        self._gen_done = True
        await self._set_phase(Phase.ANSWERING)

    async def _yield_now(self) -> None:
        """记下让位点，播过渡话术。"""
        self._bump_pos_epoch()
        st = self.state
        st.resume_point = Interrupt(
            unit_id=self._cur_unit_id(),
            at_sentence=st.unit_progress.get(self._cur_unit_id(), 0),
            sentence_complete=True,      # 段末让位：永远停在完整句子之后
            raw=st.pending_questions[0].raw if st.pending_questions else "",
            slide=st.slide_index,
        )
        self._yield_phrase = None
        self._resume_phrase = None
        # 让位话术不依赖分流结果 —— 提前起一个后台任务把"过渡话术 + 分流"一起跑掉，
        # 等轮到播让位话术时通常已经好了，学生少等一段。
        self._spawn_prep()
        self.metrics.event("yield_point", unit=st.resume_point.unit_id,
                           sent=st.resume_point.at_sentence)
        # 学生还没听过回执 → 先在段末补一句回执，再进入让位
        # （回执必须在段末发，不能抢在那一段讲完之前）
        if self._first_ack_pending:
            await self._set_phase(Phase.ACKNOWLEDGING)
        else:
            await self._set_phase(Phase.YIELDING)

    async def _dispatch(self) -> None:
        """让位之后决定怎么答。

        注意：这里**不await LLM 分流** —— 否则让位过渡话术之后会出现一段空等。
        分流前置到 `_prepare`，与生成回答串在一条流里。
        """
        st = self.state
        q = st.pending_questions.pop(0) if st.pending_questions else None
        if q is None:
            await self._back_to_lecturing()
            return

        st.active = q
        q.status = QStatus.ANSWERING
        self.metrics.on_answer_start(q)       # 让位延迟就记在这里

        # 明确的回顾型且知道页 → 先切页（§4.2 recall）
        if q.action is QAction.BACKTRACK_PAGE and q.backtrack_to_slide:
            target = max(0, min(self.script.slide_count - 1, q.backtrack_to_slide - 1))
            if target != st.slide_index:
                st.resume_point = Interrupt(unit_id=q.at_unit, at_sentence=0,
                                            sentence_complete=False, raw=q.raw,
                                            slide=target)
                await self._set_phase(Phase.REDIRECTING)
                return
        await self._set_phase(Phase.ANSWERING)

    async def _back_to_lecturing(
        self, *, apply_resume: bool = True, keep_resume: bool = False
    ) -> None:
        """归位 —— 唯一一条回到 LECTURING 的路径（不变量 I5，P0-2 控制流收口）。"""
        # 归位可能把位置带回同一格（页/段都没变），也可能跳到别处；两种都算"位置动过"，
        # 所以纪元必须无条件 +1 —— 否则"让位→归位回到原处"会让一格前的决策看起来仍然有效。
        self._bump_pos_epoch()
        st = self.state
        # 是不是刚结束一个作答周期？（答疑 / 重讲都算）—— 是的话下面的刷新预取才有意义：
        # 学生刚插过话，"这格卡了"这件事到这一刻才写进事实。
        # 判 st.active 而不是 resume_point：后者在"上一页/下一页"里也会被挂上，
        # 而那些是**位置跳转**，正在飞的那条决策本来就该被判 stale（§6.5），
        # 在这里替它刷新一次，等于把跳转的作废机制悄悄绕过去了。
        answered = st.active is not None
        rp = st.resume_point
        if apply_resume and rp is not None:
            st.slide_index = rp.slide
            us = self.script.units_of(st.slide_index)
            idx = next((i for i, u in enumerate(us) if u.unit_id == rp.unit_id), None)
            if idx is not None:
                st.seg_index = idx
            self.metrics.event("resume", unit=rp.unit_id, slide=rp.slide)
        if not keep_resume:
            st.resume_point = None
        st.active = None
        self._clear_buffers()
        self._resume_phrase = None
        self._yield_phrase = None
        if self._prep_task is not None and not self._prep_task.done():
            self._prep_task.cancel()          # 上一轮的准备工作作废
        self._prep_task = None
        await self._set_phase(Phase.LECTURING)
        # 归位是**唯一**能回到 LECTURING 的入口，所以"把刚才那次插话反映到下一格"也挂在这里 ——
        # 一处覆盖全部归位出口（resume 段结束 / 没有 resume_point 时的兜底 / 翻页 / 收尾）。
        # 为什么必须排在 _bump_pos_epoch 之后：预取与消费用的是同一把 _pos_key，
        # 早于纪元 +1 发出的那张票在边界上必然被判 stale，等于发了一张注定作废的请求。
        # 为什么是"重发"而不是第一次：单元开播时发出的那次，事实还停在"学生什么都没说"，
        # v1/v2 一条都匹配不上；重讲把 confused / q_rounds 写进去之后，这一格才有的可选。
        if answered and self.director is not None:
            self._prefetch_decision(self.script.unit_of(st.slide_index, st.seg_index))

    # ==================================================================
    # 生成（只负责把 LLM 流变成句子，不关心播放）
    # ==================================================================

    def _spawn_prep(self) -> None:
        """让位那一刻就把"过渡话术 + 分流"派出去跑。

        这两件事都不依赖播放 —— 让位话术播的是音频时间，正好用来掩盖
        分流和生成回答的延迟。这里曾经是先播过渡话术、再串行分流、再生成回答，
        学生实测要等 20 秒。
        """
        if self._prep_task is not None and not self._prep_task.done():
            return
        # 降级中 → 派一个后台恢复探测：让位话术和讲解还在播，这段时间够它跑完。
        # 作答前会等它（见 _await_recover）—— 但不在**这里**等，
        # 因为首次请求可能正在等模型加载进内存（实测 17.8 秒）。
        if getattr(self.llm, "degraded", False):
            self._recover_task = asyncio.create_task(self._recover_provider())
        self._prep_task = asyncio.create_task(self._prep_async())

    async def _recover_provider(self) -> None:
        if await self.llm.recover_if_possible():
            await self.h.notify("info", "模型服务恢复了，答疑已恢复正常。")

    async def _await_recover(self) -> None:
        """要开始生成了：等恢复探测收尾，别在服务已经好的情况下还念兜底话术。

        探测通常早就跑完了（让位话术 + 讲解播放要好几十秒），所以这里几乎不等。
        """
        if self._recover_task is not None and not self._recover_task.done():
            try:
                await asyncio.shield(self._recover_task)
            except Exception:
                pass
        self._recover_task = None

    async def _prep_async(self) -> None:
        st = self.state
        q = st.active
        if q is None:
            return
        ctx = build_ctx(self.script, st)
        # 注意：恢复探测**不在这里 await** —— 它由 _spawn_prep 派成后台任务。
        # 首次请求可能正在等模型加载，await 它会让学生多等将近 20 秒。
        # 探测成功的话，这一轮之后的生成（以及下一轮）自然就正常了。
        # 过渡话术与分流并行（两者互不依赖）
        phrase_task = asyncio.create_task(
            self.llm.yield_phrase(ctx, q.raw if q else ""))
        try:
            if q.rounds == 0 and not self._wants_rephrase(q.raw):
                # 优先检查快速意图预判是否已经命中确定性枚举（省去主模型 classify 调用）
                fast_dec = classify_by_fast_rules(q.raw) if INTENT_ROUTER.enabled else None
                if fast_dec is not None and fast_dec.qkind is not None:
                    q.kind = fast_dec.qkind
                    q.action = action_for(q.kind)
                    q.backtrack_to_slide = fast_dec.backtrack_to_slide
                    await self.h.notify(
                        "info",
                        f"快速意图分流[{fast_dec.intent.value}]：{q.kind.value}（{fast_dec.reason} · {fast_dec.latency_ms:.1f}ms）",
                    )
                else:
                    info = await self.llm.classify(q.raw, ctx)
                    try:
                        q.kind = QKind(info["kind"])
                    except Exception:
                        q.kind = QKind.CLARIFY
                    q.action = action_for(q.kind)
                    q.backtrack_to_slide = info.get("backtrack_to_slide")
                    await self.h.notify("info", f"分流：{q.kind.value}（{info.get('reason', '')}）")
        except Exception as e:
            await self.h.notify("warn", f"分流失败，按澄清处理：{e}")
            q.kind = QKind.CLARIFY
            q.action = action_for(q.kind)
        finally:
            try:
                self._yield_phrase = await phrase_task
            except Exception:
                self._yield_phrase = None

    async def _await_prep(self) -> None:
        if self._prep_task is None:
            return
        try:
            await asyncio.shield(self._prep_task)
        except Exception as e:
            _trace(f"prep 任务异常：{e!r}")
        finally:
            self._prep_task = None

    async def _prepare(self) -> None:
        """一个作答周期只走一次：分流 + 起生成任务（边生成边播）。"""
        # 周期门关在入口 —— 这里是"本周期已生成过"唯一可靠的判定点。
        # （曾经只在 _gen_rephrase 里关，普通回答那条路就漏了：next_speech 反复
        #   冲进 _prepare，每次都起一个新任务并把上一个作废，学生永远等不到回答。）
        self._prepared = True
        # 要开始生成了：等恢复探测收个尾（通常早就跑完了）
        await self._await_recover()
        st = self.state
        q = st.active
        await self._await_prep()          # 分流已经在让位那一刻起跑了
        ctx = build_ctx(self.script, st)

        # 情绪型 / 重讲 → 换讲法（§4.7），不是重复原话
        if q is not None and (q.kind is QKind.EMOTION or self._wants_rephrase(q.raw)):
            _trace(f"prepare → 重讲链 q={q.raw!r} rounds={q.rounds} "
                   f"q_rounds={st.q_rounds.get(self._cur_unit_id(), 0)}")
            await self._gen_rephrase()
            return
        _trace(f"prepare → 回答 q={q.raw if q else None!r} kind={q.kind.value if q else '-'}")

        stream = self.llm.answer_stream(
            q.raw if q else "", q.kind.value if q else "clarify", ctx,
            on_think=self._on_think)
        self._gen_done = False            # 同上：一创建就宣告进行中
        ans_fb = PHRASE_BANK.get_sentences("answer_fallback") or [
            "这个问题老师暂时答不上来，我们先继续往下讲。"
        ]
        self._gen_task = asyncio.create_task(
            self._fill(self._answer_buf,
                       _aiter(self._collect_clean(stream, q.raw if q else "")),
                       fallback=ans_fb))

    async def _fill(self, buf: list[str], sentences, fallback: list[str]) -> None:
        """把生成出来的句子**一句一句**倒进播放缓冲。

        这是"边生成边播"的关键：以前是等整段回答生成完才开始播，
        实测学生要空等 16 秒。现在只要第一句好了就能开口说话，
        后面几句的生成时间和播放时间是重叠的。

        **失败了也必须有话说**：连不上模型时不能只弹一条通知就静默 ——
        学生刚看见回执和让位话术，然后课堂就没声了，比"说一句抱歉"难理解得多。
        """
        self._gen_done = False
        self._gen_gen += 1
        mine = self._gen_gen
        got = 0
        broke = False
        try:
            async for s in sentences:
                if mine != self._gen_gen:      # 已经被作废（学生又插话了）
                    return
                if not s.strip():
                    continue
                buf.append(s)
                got += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            broke = True
            await self.h.notify("error", _explain_llm_failure(e))
        if mine != self._gen_gen:
            return
        if got == 0:
            buf.extend(fallback)
            if not broke:
                # 模型连接正常、也没抛错，但**一句话都没生成出来** ——
                # 最典型的原因是"思考型"模型（如 qwen3.5:9b）把输出全放在
                # reasoning 字段里，而正文 content 一直是空的。
                # 这种"看着能跑、其实一句真话都没说"最坑人，所以要说破。
                if not getattr(self, "_empty_warned", False):
                    self._empty_warned = True
                    await self.h.notify(
                        "warn",
                        "模型连接正常，但这次一句话都没生成出来（不是网络问题）。\n"
                        "  最常见的坑：用的是「思考型」模型（如 qwen3.5:9b）。\n"
                        "  现在有开关了：.env 里 `LLM_THINKING=answer` 会关掉短话术、\n"
                        "  只让答疑走思考（注意思考很慢，本机实测约 30 秒/答）；\n"
                        "  或者 `LLM_THINKING=off` 完全不思考。改完重启课堂生效。")
        elif broke:
            # 说了一半就断了 → 收个尾，别让字幕停在半句话上
            buf.append("这一段我先说到这里，回头再补。")
        self._gen_done = True

    async def _gen_rephrase(self) -> None:
        """按轮次换讲法（§4.7）：1 换说法 → 2 举例子 → 3 拆步骤 → 4 暂放 + 承诺。

        轮次**逐单元累计**：每真正开口重讲一次升一轮。
        学生在一个单元上连点五次"没懂"，得到的是逐级递进的四轮，而不是复读同一句话。
        已经到顶的单元再喊"没懂"不会升轮（否则上限会被推着走），
        但仍然会重新开口 —— 只是换成"已经排进待回看"的说法，绝不原地重播。
        """
        st = self.state
        unit_id = self._cur_unit_id()
        n = self._rephrase_rounds.get(unit_id, 0) + 1
        round_no = min(self.settings.max_rephrase, n)
        st.q_rounds[unit_id] = round_no
        # 学生**明说**没懂 → confused（§4.6 的强信号，与 vague 的弱信号分开）。
        # q_rounds 记「讲了几遍」，comprehension 记「学生什么态度」，两件事必须分开记：
        # 以前只有探针超时写 vague、确认听懂写 confirmed，confused 一处都不写 ——
        # 于是 v1（comprehension ∈ {vague, confused}）在真实课堂永远不命中。
        # 说一次没懂，先换说法（v1）；连说两次（q_rounds=2）才轮到大白话举例（v2）。
        # vague 的既有语义不动：它仍然是"探针没人回应"的弱信号。
        self._note_comprehension(unit_id, Comprehension.CONFUSED.value)
        if n <= self.settings.max_rephrase:
            self._rephrase_rounds[unit_id] = n
        if st.active is not None:
            st.active.rounds = round_no
        ctx = build_ctx(self.script, st)
        self._rephrase_rounds[unit_id] = min(n, self.settings.max_rephrase)
        _trace(f"gen_rephrase unit={unit_id} round={round_no} n={n} "
               f"pending={self._pending_for(unit_id)}")
        # 本轮到这里就算生成过了 —— 无论走 LLM 还是走"暂放"话术。
        # （周期门不能只在 LLM 分支关：暂放分支不关的话，下一轮 next_speech
        #   会看到"没生成过"又冲进来一次，变成活锁。）
        # 本轮到这里就算生成过了 —— 无论走 LLM 还是走"暂放"话术。
        # （周期门不能只在 LLM 分支关：暂放分支不关的话，下一轮 next_speech
        #   会看到"没生成过"又冲进来一次，变成活锁。）
        self._gen_done = True

        if round_no >= self.settings.max_rephrase:
            # 最后一轮：暂放 + 承诺，**不再重复解释**。
            # 必须有这一轮，否则"不懂→重讲→还不懂"会把一页卡十分钟。
            unit = self.script.unit(unit_id)
            for kp in (unit.kp if unit else []):
                if kp not in st.deferred:
                    st.deferred.append(kp)
                    if self.mcp is not None:
                        self.mcp.emit_classroom_event("deferred_added", kp=kp, slide=st.slide_index + 1)
                        self.mcp.memory.record_signal_async(st.student_id, weak_kp=[kp])
            if st.deferred_too == 0:
                self._rephrase_buf = PHRASE_BANK.get_sentences("defer_1") or [
                    "这个点确实有点绕，我们先把它放一放。",
                    "后面的内容讲完我再回到这里，这不是你的问题，是这个知识点本身就要多绕两圈。",
                ]
            elif st.deferred_too == 1:
                # 学生又一次没懂 —— 不能重播同一句话，要交代"已经记住了、排进去了"
                self._rephrase_buf = PHRASE_BANK.get_sentences("defer_2") or [
                    "这个点我记住了，已经排在最后一起讲。",
                    "我们先往后走，不然今天这一节会卡在这里，回头我一定把它讲透。",
                ]
            else:
                self._rephrase_buf = PHRASE_BANK.get_sentences("defer_3") or [
                    "我一直记得这个点，等下课后我们一起把它过一遍。",
                    "现在我们先把后面的看完，你把不懂的地方留着，到时直接问我。",
                ]
            st.deferred_too += 1
            await self.h.notify("warn", "重讲轮次达上限，已记入待回看")
            return

        # 为**下一格**再发一次选路（§6.4 ①）：重讲链要开口了，而事实（confused + q_rounds）
        # 到这一刻才齐 —— 再早一步（入队 / 让位那一刻）那两项还没写，v1/v2 一条都匹配不上。
        # 它是一次"能赶上就赶上"的预取，不是决策：归位会 bump 位置纪元，于是这条票在边界上
        # 多半会被判 stale，再由归位之后的_那次刷新覆盖（见 _back_to_lecturing）。
        # 留着它的理由：这里一个字都不能阻塞学生（铁律 2），而重讲链本身的行为一个字没改。
        self._prefetch_decision(self.script.unit(unit_id))

        stream = self.llm.rephrase_stream(st.active.raw if st.active else "", round_no, ctx,
                                          on_think=self._on_think)
        # 任务一创建就宣告"正在进行" —— 不能等它被调度才置位。
        # 事件循环可能先回到 next_speech（快节奏驱动/真实播放结束），
        # 那时 _gen_done 还是上一轮的 True，会被误判成"生成完了没内容"，
        # 直接把刚起的任务取消掉（学生永远听不到重讲）。
        self._gen_done = False
        rep_fb = PHRASE_BANK.get_sentences("rephrase_fallback") or [
            "我们换一个说法，再看一遍这一段。"
        ]
        self._gen_task = asyncio.create_task(
            self._fill(self._rephrase_buf,
                       _aiter(_collect_sentences(stream)),
                       fallback=rep_fb))

    # ==================================================================
    # 辅助
    # ==================================================================

    def _enqueue(self, text: str, asked_at: float | None = None) -> None:
        st = self.state
        unit_id = self._cur_unit_id()
        asked = now() if asked_at is None else asked_at
        _trace(f"enqueue unit={unit_id} text={text!r} pending={self._pending_for(unit_id)} "
               f"q_rounds={st.q_rounds.get(unit_id, 0)} phase={st.phase.value}")
        if self._wants_rephrase(text):
            # 重讲链的升级**不在这里**做 —— 这里只登记"又没懂"。
            # 升几轮由 _gen_rephrase 在真正要开口时决定（一个作答周期升一轮），
            # 否则学生连点几下，轮次就被话轮数推着烧完了。
            st.pending_questions.append(Question(
                raw=text, kind=QKind.CLARIFY, action=QAction.ANSWER_INLINE,
                at_unit=unit_id, at_slide=st.slide_index, asked_at=asked,
            ))
            return
        st.pending_questions.append(Question(
            raw=text, at_unit=unit_id, at_slide=st.slide_index, asked_at=asked,
        ))

    def _pending_for(self, unit_id: str) -> int:
        """这个单元上已经排着几条提问（同一周期的重复输入）。"""
        return sum(1 for q in self.state.pending_questions if q.at_unit == unit_id)

    def _chain_active(self, unit_id: str) -> bool:
        """这个单元的重讲链是否已经在跑（在播 / 在答 / 还排在队列里）。"""
        if self._rephrase_buf:
            return True
        st = self.state
        if st.active is not None and st.active.at_unit == unit_id \
                and st.active.rounds > 0 and st.active.status is QStatus.ANSWERING:
            return True
        return self._pending_for(unit_id) > 0

    def _wants_rephrase(self, text: str) -> bool:
        t = text.strip().rstrip("。！？!?…，,")
        return any(k in text or k in t for k in REPHRASE_TRIGGERS)

    # 「是不是听懂了」一律走模块级的 classify_reply()：
    # 它要做的事比"查子串"多（否定优先、疑问句排除、长度上限），
    # 放在这里当方法只会诱使人再写一遍子串判断 —— 那个判断是错的。

    async def _set_phase(self, phase: Phase) -> None:
        st = self.state
        if st.phase is phase:
            return
        prev, st.phase = st.phase, phase
        self.metrics.event("phase", frm=prev.value, to=phase.value)
        await self.h.phase_changed(st)
        await self.h.state_changed()

    # ==================================================================
    # 推进：唯一的出口 + 路线执行（skip / revisit / finish）
    # ==================================================================

    async def _advance(self, unit, *, after_probe: bool = False) -> None:
        """唯一的推进出口：决定"下一步去哪"，并真的把指针挪过去。

        为什么必须是**一个**函数：指针原来在三处各自往前挪 —— ① on_speech_end("unit")
        这一段正常讲完；② _unit_exhausted 迟到的边界（让位回来时它早就讲完了，真实课堂
        最常走这条）；③ _advance_after_probe 探针窗口里被回应之后。每加一种新的走向都要在
        这三处各接一次，漏掉任何一处都不报错，只表现为"路线被选中了却没走"或者
        "跳完又被推进了一格"。收敛成一个出口之后，路线只需要在这里接一次。

        after_probe=True 就是③：页尾该**跨页**，而不是再开一个探针（那会开在一个
        没人会回答的页面上）。

        判断顺序是有意的：路线 > 补回头汇回 > 页尾探针 > 下一格。路线排第一，因为它
        可能把课堂整个带到别处（甚至直接下课），后面那几步全都作废。
        """
        st = self.state
        if await self._take_route(unit):
            return                        # 路线已经把课堂带走（或直接下课）
        if await self._merge_replay(unit):
            return                        # 补回头讲完 → 汇回 merge_node
        if unit is not None and unit.is_page_end:
            if after_probe:
                await self._goto_slide(st.slide_index + 1)
            else:
                await self._set_phase(Phase.PAUSING_AUDIO)
            return
        st.seg_index += 1
        if after_probe:
            await self._back_to_lecturing(apply_resume=False)

    async def _take_route(self, unit) -> bool:
        """走掉上一个边界定下的路线，返回 True = 推进已经处理完（调用方立刻返回）。

        三种走向唯一的一处执行点：
          · skip    → 直接到 merge_node 那一格的起点；中间几格一个字都不说（也就不会
                      碰课件操作），跨页照走 _goto_slide；
          · revisit → 回到 steps[0] 那一格重讲（清掉 steps 的进度让它们重播），
                      讲完由 _merge_replay 汇回 merge_node；
          · finish  → _finish_all()（唯一收尾入口，不变量 I5），学生听到的是"讲完了"。

        **任何失败都退回默认 v0**（照常讲下一格）+ 记一条 route_error，绝不抛异常：
        一条路线写错了最多让课堂少跳一格；抛出去整节课就停在边界上，而边界是异步
        链路里最不该抛的地方（没人 await 它，异常会变成一条没人看的告警）。
        """
        handoff = self._route_pending
        self._route_pending = None        # 一次边界只有一条路线，取走即清
        if handoff is None:
            return False
        # 岔出格对不上 = 这条路线是给**别的边界**定的（中途被让位 / 翻页打断过）。
        # 拿它去跳，跳的是"上一格的决定"，学生听到的顺序会整体错位。
        fork_unit = str(handoff.get("unit_id") or "")
        if fork_unit and (unit is None or fork_unit != unit.unit_id):
            self._route_refused(handoff, "stale")
            return False
        try:
            route = Route.from_dict(handoff)
        except Exception as e:            # kind 非法 = 不知道怎么走（见 route.py）
            self._route_failed(handoff, f"结构非法：{e}")
            return False
        rid = route.route_id
        if self._route_runs.get(rid, 0) >= ROUTE_MAX_PER_ID:
            self._route_refused(handoff, "dup")        # 同一条路线整节课只走一次
            return False
        if self._route_depth >= ROUTE_MAX_DEPTH:
            self._route_refused(handoff, "depth")      # 补回头重讲期间不许再叠一层
            return False
        if len(self._route_runs) >= ROUTE_MAX_ROUTES:
            self._route_refused(handoff, "cap")        # 这节课走的路线够多了
            return False
        try:
            route.validate(len(self.script.units))
        except Exception as e:            # 越界 / revisit 没有 steps / finish 汇回点不对
            self._route_failed(handoff, f"结构非法：{e}")
            return False

        st = self.state
        if route.kind == "finish":
            self._count_route(rid)
            self._record_route(handoff, route, to_unit="")
            await self._finish_all()      # 唯一收尾入口（不变量 I5）
            return True

        if route.kind == "revisit":
            start_node = self.script.order_index(route.steps[0].unit_id)
            target = self._unit_at_node(start_node)
            if target is None or start_node >= route.merge_node:
                self._route_failed(
                    handoff, f"重讲起点不可用：{route.steps[0].unit_id!r}")
                return False
            for step in route.steps:      # 让它们重播：进度清零（讲完是"从头讲一遍"）
                step_unit = self.script.unit(step.unit_id)
                if step_unit is not None:
                    st.unit_progress[step_unit.unit_id] = 0
            if not await self._jump_to_unit(target, reset_progress=True):
                self._route_failed(handoff, f"重讲起点跳不过去：{target.unit_id}")
                return False
            last = self._unit_at_node(route.merge_node - 1)
            self._route_replay = {
                "route_id": rid,
                "merge_node": route.merge_node,
                "last_unit": last.unit_id if last is not None else "",
            }
            self._count_route(rid)
            self._record_route(handoff, route, to_unit=target.unit_id)
            return True

        # skip：直接到 merge_node 那一格的起点。被跳过的格子一个字的字幕都不发，
        # 因而也不会触发它们自己的课件操作 —— "没讲"就该是"课件上什么都没发生"。
        target = self._unit_at_node(route.merge_node)
        if target is None:
            if route.merge_node >= len(self.script.units):
                # 合并点 == 课尾（合同允许 merge <= total）：后面已经没有格子可讲，
                # 等于提前收尾。走同一个收尾入口，不另开一个下课出口。
                self._count_route(rid)
                self._record_route(handoff, route, to_unit="")
                await self._finish_all()
                return True
            self._route_failed(handoff, f"merge_node 越界：{route.merge_node}")
            return False
        skipped = tuple(
            u.unit_id for u in self.script.units[
                max(0, route.fork_node + 1):max(0, route.merge_node)])
        if not await self._jump_to_unit(target, reset_progress=True):
            self._route_failed(handoff, f"merge 目标跳不过去：{target.unit_id}")
            return False
        self._count_route(rid)
        self._record_route(handoff, route, to_unit=target.unit_id, skipped=skipped)
        return True

    async def _merge_replay(self, unit) -> bool:
        """补回头重讲走到了最后一格：汇回 merge_node，返回 True = 推进已处理完。

        为什么要显式跳、而不是"接着往下讲自然就到"：merge_node 是冻结契约给出的汇回点，
        它未必是下一页的第一个单元。靠自然推进，落在哪取决于页尾探针走哪条路（探针还会
        先开一个等待窗口），于是"汇回"变成一件看运气的事 —— 而它一旦落错格，补回头就
        变成了"回前面讲完再乱走"。
        """
        replay = self._route_replay
        if replay is None or unit is None:
            return False
        merge = int(replay.get("merge_node") or 0)
        if self.script.order_index(unit.unit_id) >= merge:
            self._route_replay = None    # 已经越过汇回点：这条路线的重讲到此为止
            return False
        if unit.unit_id != replay.get("last_unit"):
            return False
        self._route_replay = None
        target = self._unit_at_node(merge)
        if target is None:
            # 汇回点 == 课尾：重讲完了这节课也就讲完了。走唯一收尾入口。
            self.metrics.event("route_merged", route_id=replay.get("route_id", ""),
                               unit=unit.unit_id, to="", position=self._pos_key)
            await self._finish_all()
            return True
        if not await self._jump_to_unit(target, reset_progress=False):
            self.metrics.event("route_error", route_id=replay.get("route_id", ""),
                               reason=f"汇回点跳不过去：{target.unit_id}")
            return False
        self.metrics.event("route_merged", route_id=replay.get("route_id", ""),
                           unit=unit.unit_id, to=target.unit_id,
                           position=self._pos_key)
        return True

    async def _jump_to_unit(self, unit, *, reset_progress: bool) -> bool:
        """把课堂指针搬到 unit 的起点 —— 全机唯一一处"跨格跳"。

        页内直接换段；**跨页一律经 _goto_slide**：page_changed 事件、换页才重置的
        q_rounds（§4.7）、"自动翻页"计数与前端快照都在那里。绕过去等于自己再写一份
        翻页纪律 —— 两份纪律迟早只修一份，表现是前端停在上页、或者重讲轮次没归零。
        """
        st = self.state
        if unit is None:
            return False
        idx = self._seg_index_of(unit)
        if idx < 0:
            return False
        if unit.slide != st.slide_index:
            await self._goto_slide(unit.slide)   # 里面会把 seg_index 归零并归位
            st.seg_index = idx
        else:
            st.seg_index = idx
            # 页内跳同样是"位置跳转"：一格前算出的决策必须作废（§6.5 的纪元语义）。
            self._bump_pos_epoch()
            self._cur_unit_sents = []
        if reset_progress:
            st.unit_progress[unit.unit_id] = 0   # 从这一格的**起点**讲（路线语义）
        # 跳完课堂必须停在"可以继续讲"的相位：探针窗口若还开着（学生在窗口里回了一句
        # "懂了"），学生会看到老师在等他回答一个已经被跳过去的问题。
        if st.phase is not Phase.LECTURING:
            await self._back_to_lecturing(apply_resume=False)
        return True

    # -- 路线的簿记 -------------------------------------------------------
    def _unit_at_node(self, node: int):
        """路线节点序号 → 单元；越界返回 None（路线是 C 现造的，越界必须当失败处理）。

        节点序号就是 script.units 里的下标（= Route.validate 的 total_nodes 口径）。
        """
        if 0 <= int(node) < len(self.script.units):
            return self.script.units[int(node)]
        return None

    def _seg_index_of(self, unit) -> int:
        """单元在本页里的下标；不在本页返回 -1。"""
        for i, u in enumerate(self.script.units_of(unit.slide)):
            if u.unit_id == unit.unit_id:
                return i
        return -1

    @property
    def _route_depth(self) -> int:
        """当前叠着几条路线：补回头重讲算一层，其余走向都是"一步到位"（0）。"""
        return 1 if self._route_replay is not None else 0

    def _count_route(self, route_id: str) -> None:
        self._route_runs[route_id] = self._route_runs.get(route_id, 0) + 1

    def _route_refused(self, handoff: dict, reason: str) -> None:
        """选出来了却没走（dup 重复 / depth 嵌套 / cap 到顶 / stale 岔出格对不上）。

        这些都不是错误 —— 课堂照常讲下一格，所以记 route_refused 而不是 route_error。
        但必须留痕：一条路线"被选中却没走"与"根本没被选中"在效果上一样，
        在原因上完全不同，复盘时只能靠这条事件分开。
        """
        self.metrics.event(
            "route_refused", reason=reason,
            route_id=str(handoff.get("route_id", "")),
            candidate_id=str(handoff.get("candidate_id", "")),
            unit=handoff.get("unit_id", ""),
        )

    def _route_failed(self, handoff: dict, reason: str) -> None:
        """路线走不通（结构非法 / 指向的单元不存在）：退回 v0 + 记一条 error 级事件。"""
        self.metrics.event(
            "route_error", reason=reason,
            route_id=str(handoff.get("route_id", "")),
            candidate_id=str(handoff.get("candidate_id", "")),
            unit=handoff.get("unit_id", ""),
        )

    def _record_route(self, handoff: dict, route, *, to_unit: str,
                      skipped: tuple = ()) -> None:
        """记一条可复盘的路线记录：state 台账（只增不改）+ 一条 metrics 事件。

        两处都记的理由不同：state.route_log 回答"这节课走过哪几条路、从第几格岔出、
        汇回到第几格、跳过了谁"（结构问题），metrics 回答"第几句话时走的"（时序问题）。
        只留一处，复盘时要么没有顺序、要么得自己把结构从事件里拼回来。
        """
        st = self.state
        frm = self.script.unit(str(handoff.get("unit_id") or ""))
        st.route_log.append({
            "route_id": route.route_id,
            "kind": route.kind,
            "fork_node": route.fork_node,
            "merge_node": route.merge_node,
            "from_unit": handoff.get("unit_id", ""),
            "from_slide": frm.slide if frm is not None else st.slide_index,
            "to_unit": to_unit,
            "slide": st.slide_index,
            "steps": [str(step.unit_id) for step in route.steps],
            "skipped": list(skipped),
            "candidate_id": handoff.get("candidate_id", ""),
            "reason": route.reason,
            "est_sec": route.est_sec,
            "seq": st.seq,
        })
        self.metrics.event(
            # 事件自己的 kind 是第一个位置参数（eval.MetricsTracker.event），
            # 所以路线走向只能写 route_kind —— 写成 kind 会撞参数名。
            "route_applied", route_id=route.route_id, route_kind=route.kind,
            frm=handoff.get("unit_id", ""), to=to_unit,
            fork=route.fork_node, merge=route.merge_node,
            skipped=list(skipped),
            candidate_id=handoff.get("candidate_id", ""),
            position=self._pos_key,
        )

    async def _unit_exhausted(self, unit) -> None:
        """progress 已满但事件还没到（兜底推进，避免卡死）。"""
        st = self.state
        if st.pending_questions:
            await self._yield_now()
            return
        # 一次**迟到**的边界（§9.1 第 4 项）：本单元的边界被"让位去回答"占走了，
        # 归位回来时它早就讲完了 —— 这里补的正是那次没能发生的"为下一格选路"。
        # 真实课堂最常走的就是这条路（学生说没懂 → 让位 → 重讲 → 归位 → 这一格已讲完），
        # 只在 on_speech_end("unit") 里决策的话，真实课件上一次都不会切。
        await self._route_next_unit(unit)
        if self.agent_speech_queue:
            await self._yield_for_agent()   # 外脑插播占走这个边界，回来后再推进
            return
        # 往前推进走**同一个**出口（_advance）：迟到的边界与正常讲完必须走同一条路，
        # 否则"真实课堂最常走的这条"就成了唯一没有路线执行器的那一条。
        await self._advance(unit)

    async def _advance_after_probe(self) -> None:
        """探针在窗口内被回应后，从页尾继续推进到下一页。

        推进本身交给唯一的出口（_advance）：这条路上同样可能有路线等着执行
        （学生正是"在这里说懂了，那跳过后面几格吧"的那个时刻）。after_probe=True
        是唯一需要告诉出口的东西 —— 页尾该跨页，而不是再开一个探针。
        """
        st = self.state
        await self._advance(self.script.unit_of(st.slide_index, st.seg_index),
                            after_probe=True)

    async def _goto_slide(self, slide_index: int) -> None:
        st = self.state
        if slide_index >= self.script.slide_count:
            await self._finish_all()
            return
        st.slide_index = slide_index
        st.seg_index = 0
        st.user_advanced = False
        self._cur_unit_sents = []
        # 换页 → 重讲轮次重置（§4.7）
        for u in self.script.units_of(slide_index):
            st.q_rounds.pop(u.unit_id, None)
        self.metrics.advance_auto += 1
        await self.h.page_changed(slide_index)
        if self.mcp is not None:
            self.mcp.emit_classroom_event("page_changed", slide=slide_index + 1)
        await self._back_to_lecturing(apply_resume=False)

    async def _finish_all(self) -> None:
        st = self.state
        await self.h.notify("info", "这节课的内容讲完了。")
        if st.deferred:
            await self.h.notify(
                "warn", "还有几个点没确认，已记入待回看：" + "、".join(dict.fromkeys(st.deferred))
            )
        self.metrics.event("class_end")
        # 收尾把相位收回 LECTURING。理由是探针的等待窗口靠"相位离开 PAUSING"退出：
        # 停在 PAUSING 收工，学生端看到的是"老师还在等你回答"，而课其实已经结束了。
        # 选 LECTURING 作终态 —— 它是本状态机的中性态（ANSWERING / RESUMING / YIELDING
        # 都表示流程还在走，PAUSING 表示还在等回应，只有 LECTURING 不承诺下一步）。
        # 而且必须走 _back_to_lecturing 这个**唯一入口**（不变量 I5；test_mcp 的 P0-2 会数
        # 这个调用点，收尾也不该另开第二个回 LECTURING 的口子）。
        await self._back_to_lecturing(apply_resume=False)
        self._close_director()
        self._finished.set()

    def _maybe_finished(self) -> None:
        st = self.state
        if (st.phase is Phase.LECTURING
                and st.slide_index >= self.script.slide_count
                and not st.pending_questions
                and not self.agent_speech_queue):
            self._close_director()
            self._finished.set()

    def _close_director(self) -> None:
        """下课收尾：取消还在飞的预取，别让一次没人等的模型调用活到进程后面去。

        为什么值得单独一个方法：收尾出口有 _finish_all 与 _maybe_finished 两处，
        两处都会 set(_finished)，漏掉任何一处就会出现"课结束了还有一次调用在飞"。
        那不只是内存泄漏 —— 退出时那条 "Task was destroyed but it is pending" 告警
        会把真正的错误淹掉，这才是要修的。
        Director.close() 本身幂等（清槽位 + 取消未完成 task），两个出口都调没有风险；
        director 不在场（§11 最后一行）时这里什么都不做。
        """
        if self.director is not None:
            self.director.close()

    async def _wait_student(self) -> None:
        """等待学生输入或外置大脑插话。PAUSING 期间任何输入都有效 —— 这正是探针存在的意义。

        为什么还要判 _finished：这个循环原先只认"相位离开 PAUSING"这一种退出方式，
        而课堂有若干条路径是**相位停在 PAUSING 就收工**的（_goto_slide 走到最后一页之后
        是先 _finish_all 再归位的，探针窗口本身又是 shield 包着的、超时不取消内层）。
        于是它会每 50ms 醒一次，一直活到事件循环关闭 —— 退出时那条
        "Task was destroyed but it is pending" 会把真正的错误淹掉。
        把"课已结束"写进条件，是唯一能覆盖全部出口的收法（也与仓库别处靠 _finished 收尾一致）；
        只去补某一条出口（比如提前归位、或取消内层 task）都只能治那一条路径。
        """
        while (not self._finished.is_set()
               and self.state.phase is Phase.PAUSING
               and not self.agent_speech_queue):
            await asyncio.sleep(0.05)

    def _note_comprehension(self, unit_id: str, signal: str) -> None:
        """记一条理解信号：**只允许升级，不允许降级**（§12 阶段 1 验收④ 的数据前提）。

        为什么需要这道闸：页尾探针超时写的是弱信号 vague，而它偏偏发生在"学生刚在这一页
        说过没懂（confused）"之后 —— 一覆盖，那条强信号就没了。而验收④ 正是靠"切换前后
        probe_result / 理解信号"判断这次换讲法有没有用：强信号被弱信号抹掉，课后对比会
        得出相反的结论（"换完更糊涂了" vs "换完清楚了"）。

        探针自己的结论不受影响：`_close_probe` 照旧把 probe_log 记成 timeout —— 那是
        "这个探针没人回应"的事实，与本单元学生的态度是两件事，两者都该留在台账里。

        口径复用 COMPREHENSION_RANK（confused 3 > vague 2 > confirmed 1 > 空 0）。
        相等时照写：所以"单元还没有任何信号 → 探针超时记 vague"这条既有行为一字不变。
        """
        cur = self.state.comprehension.get(unit_id, "")
        if COMPREHENSION_RANK.get(signal, 0) < COMPREHENSION_RANK.get(cur, 0):
            return
        self.state.comprehension[unit_id] = signal

    def _close_probe(self, result: str) -> None:
        for p in reversed(self.state.probe_log):
            if p.closed_at is None:
                p.closed_at = now()
                p.result = result
                break
        self.metrics.on_probe_close(result)

    async def _on_think(self, on: bool) -> None:
        """模型开始/结束"思考"时，把状态告诉前端。

        为什么值得单独做：开了思考以后，学生提问到听见第一句话之间会出现
        一段**只有等待、什么也没发生**的空窗（本机实测约 30 秒）。
        没有指示，学生只会以为课堂卡死了 —— 而"等待"本身不违反段末让位设计，
        "看不出在等"才违反。所以这里只暴露**状态**，不暴露**内容**：
        模型的自言自语不是老师要说的话，摆出来会毁掉这个角色。
        """
        if self.h.thinking is None:
            return
        try:
            await self.h.thinking(on, on)
        except Exception:
            pass          # 指示灯坏了不该影响上课

    def _cur_unit_id(self) -> str:
        st = self.state
        if st.phase in (Phase.ANSWERING, Phase.RESUMING) and st.active is not None and st.active.at_unit:
            return st.active.at_unit
        unit = self.script.unit_of(st.slide_index, st.seg_index)
        return unit.unit_id if unit else ""

    def _prev_unit_id(self) -> str:
        units = self.script.units_of(max(0, self.state.slide_index - 1))
        return units[0].unit_id if units else ""

    def _append_resume_point(self, unit_id: str) -> None:
        self.state.resume_point = Interrupt(
            unit_id=unit_id, at_sentence=0, sentence_complete=True,
            raw="", slide=self.state.slide_index,
        )

    def _log_utterance(self, speaker: str, text: str, unit_id: str = "") -> None:
        if not text:
            return
        st = self.state
        st.emit(speaker, text, unit_id=unit_id)
        asyncio.create_task(self.h.transcript(st.transcript[-1]))

    def _clear_buffers(self) -> None:
        self._rephrase_buf = []
        self._answer_buf = []
        # 作废还在跑的生成任务：已经产出的句子直接丢掉，不能再往清空的缓冲里塞
        self.active_run.reset()


_TRACE_ON = bool(os.environ.get("CLASSROOM_TRACE"))


def _explain_llm_failure(e: Exception) -> str:
    """把 LLM 调用失败翻译成"学生/演示者能照着修"的话。

    只报 `APIConnectionError: Connection error.` 是不够的 —— 演示现场最常见的
    原因就是 ollama 服务没起来，而错误里完全看不出这一点。
    """
    text = f"{type(e).__name__}: {e}"
    low = text.lower()
    if "预算吃光" in text:
        # 开了思考但预算不够。**服务是好的**，所以绝不能报成"连不上模型" ——
        # 那会把人带去查 ollama，而真正要改的是预算。
        return (
            f"{e}\n"
            "  · 这不是连不上模型：模型连着，只是**思考**把 token 预算吃完了。\n"
            "  · 想更稳：把 .env 里 LLM_THINK_TOKEN_BUDGET 调大（例：2048 → 4096）\n"
            "  · 想更快：把 LLM_THINKING 设回 off，回到「不思考、直接答」。")
    if "已确认连不上" in text:
        # 预检/上一次连接失败已经判定服务不可用，这一次根本没去连 ——
        # 不要再说"检查 base_url"，那会把人带到错的方向
        return ("模型服务当前不可用，答疑先用预置话术顶着。\n"
                "  · ollama 没在运行 → `ollama serve`（模型目录不在系统盘还要设 OLLAMA_MODELS）\n"
                "  · 起好服务后**不用重启课堂**，下一句提问会自动恢复正常。")
    if "connection" in low or "connect" in low or "timeout" in low:
        return (
            f"连不上模型服务（{text}）。最可能的原因：\n"
            "  · ollama 没在运行 —— 先执行 `ollama serve`（或启动 Ollama 桌面端）\n"
            "  · 模型目录不在系统盘时，还要设 OLLAMA_MODELS（例：E:\\AImodel）\n"
            "  · .env 的 OLLAMA_BASE_URL / 模型名与实际不一致\n"
            "本次回答暂时跳过，课堂继续。（改完不用重启课堂，下一句会自动恢复）"
        )
    if "401" in text or "403" in text or "api key" in low or "unauthorized" in low:
        return f"模型服务拒绝了这个 key（{text}）。检查 .env 里的 API Key。"
    if "404" in text or "not found" in low or "model" in low:
        return (f"模型名或接口路径不对（{text}）。检查 .env 里的模型名"
                f"（`ollama list` 看实际名字）。")
    return f"生成失败：{text}"


def _trace(msg: str) -> None:
    """临时排查开关：CLASSROOM_TRACE=1 时打印状态流转。"""
    if _TRACE_ON:
        print(f"[trace] {msg}", flush=True)


async def _aiter(coro):
    """把一个"返回 list 的协程"包成异步迭代器。

    `_collect_clean` / `_collect_sentences` 都是**一次性**收集完整段的协程，
    不能直接 `async for`（那是在迭代协程对象，会报 never awaited）。
    """
    for s in await coro:
        yield s


async def _collect_sentences(stream) -> list[str]:
    """把 LLM 的流式输出切成句子 —— 句级切分是句级流式合成的前提。"""
    out: list[str] = []
    buf = ""
    async for tok in stream:
        buf += tok
        while True:
            idx = _first_sentence_end(buf)
            if idx < 0:
                break
            out.append(buf[:idx + 1].strip())
            buf = buf[idx + 1:]
    tail = buf.strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def _first_sentence_end(buf: str, min_len: int = 8) -> int:
    """返回第一个可切分句子的下标（-1 表示还没有完整句）。"""
    for i, ch in enumerate(buf):
        if ch in "。！？!?…" and i + 1 >= min_len:
            return i
        if ch in "；;" and i + 1 >= 20:
            return i
    return -1
