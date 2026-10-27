"""状态机测试 —— 把《课堂状态机规范》的不变量变成可执行的断言。

无需网络、无需 LLM、无需 TTS：FakeLLM 与 Headless 驱动替代了真实服务商。
运行：  python -m pytest tests -q     （或 python tests\\test_machine.py）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings, ProviderProfile  # noqa: E402
from director.pool import CandidatePool  # noqa: E402
from director.route import Route, RouteStep, derive_route_id  # noqa: E402
from eval import MetricsTracker  # noqa: E402
from machine import Machine, PhaseHandlers, SpeechSeg  # noqa: E402
from script import build_script  # noqa: E402
from state import ClassroomState, Phase, QKind  # noqa: E402

# ===========================================================================
# 替身
# ===========================================================================

def make_settings(probe_window: float = 0.15) -> Settings:
    prof = ProviderProfile("fake", "http://localhost", "x", "fake-model")
    return Settings(
        provider="fake", providers={"fake": prof},
        temperature=0.0, timeout=5.0,
        tts_enabled=False, tts_voice="x", tts_rate="+0%",
        probe_window=probe_window, max_clarify_asks=2, max_rephrase=4,
        unit_char_warn=250, host="127.0.0.1", port=0,
        cache_dir=Path(".cache-test"),
    )


class FakeLLM:
    """按关键词分流，回答拆成两句 —— 够验证链路，且完全确定。"""

    def __init__(self):
        self.answer_calls = 0
        self.rephrase_calls = 0
        self.resume_calls = 0

    async def classify(self, q: str, ctx) -> dict:
        kind = QKind.CLARIFY.value
        if "没" in q or "不懂" in q:
            kind = QKind.EMOTION.value if "难" in q or "累" in q else QKind.CLARIFY.value
        if "刚才" in q or "前面" in q:
            kind = QKind.RECALL.value
        return {"kind": kind, "pause_lecture": True,
                "backtrack_to_slide": 1 if kind == QKind.RECALL.value else None,
                "reason": "fake"}

    async def answer_stream(self, q, kind, ctx, *, on_think=None):
        self.answer_calls += 1
        for part in ["这是回答的第一句。", "这是回答的第二句。"]:
            yield part

    async def rephrase_stream(self, q, round_no, ctx, *, max_sentences=4, on_think=None):
        self.rephrase_calls += 1
        yield f"换个说法（第{round_no}轮），我们再看一遍。"
        yield "这样是不是清楚一点了？"

    async def yield_phrase(self, ctx, q) -> str:
        return "这个问题问得好，先停一下。"

    async def resume_phrase(self, ctx, q) -> str:
        self.resume_calls += 1
        return "我们接着往下看。"


class Headless:
    """无头驱动：自动回 speech_done，并记录所有说过的话与状态转移。

    `speak_delay` 模拟真实播放耗时（秒/句）。默认 0 —— 只测状态机逻辑；
    需要测"让位延迟"这类与时间有关的量时才给它正数。
    """

    def __init__(self, probe_window: float = 0.15, auto_input=None,
                 speak_delay: float = 0.0, director=None):
        self.script = build_script()
        self.settings = make_settings(probe_window)
        self.state = ClassroomState(course_id=self.script.course_id)
        self.llm = FakeLLM()
        self.metrics = MetricsTracker()
        self.spoken: list[SpeechSeg] = []
        self.phases: list[str] = []
        self.pages: list[int] = []
        self.auto_input = auto_input
        self.speak_delay = speak_delay
        self.machine: Machine | None = None
        self._rephrase_sent: set[str] = set()
        #: 决策层替身（路线分支用）。None = 不接决策层（与既有用例逐字等价）。
        self.director = director

    def handlers(self) -> PhaseHandlers:
        async def speak(seg: SpeechSeg):
            # 与 web.py 的播放协议一致：先"播完"这一段，再通知状态机
            self.spoken.append(seg)
            if self.speak_delay:
                await asyncio.sleep(self.speak_delay)
            if self.machine is not None:
                await self.machine.on_speech_end(seg.kind)
            if self.auto_input is not None:
                await self.auto_input(self, seg)

        async def notify(level, text):
            return None

        async def phase_changed(state):
            self.phases.append(state.phase.value)

        async def state_changed():
            return None

        async def transcript(u):
            return None

        async def page_changed(idx):
            self.pages.append(idx)

        async def highlight(anchors):
            return None

        return PhaseHandlers(speak, notify, phase_changed, state_changed,
                             transcript, page_changed, highlight)

    async def run(self, _unused=None):
        self.machine = Machine(self.script, self.state, self.settings,
                               self.llm, self.handlers(), self.metrics,
                               director=self.director)
        await self.machine.start()
        try:
            await asyncio.wait_for(self.machine.wait_finished(), timeout=12)
        except asyncio.TimeoutError:
            pass
        return self

    # -- 断言辅助 ---------------------------------------------------------
    @property
    def kinds(self) -> list[str]:
        return [s.kind for s in self.spoken]

    def text_of(self, kind: str) -> list[str]:
        return [s.text for s in self.spoken if s.kind == kind]


# ===========================================================================
# T1 主链路：讲课 → 提问 → 段末让位 → 回答 → 三步重入 → 回到讲课
# ===========================================================================

async def test_yield_flow():
    hs = Headless(probe_window=0.15, speak_delay=0.05)
    fired = {"n": 0}

    async def auto_input(h: Headless, seg: SpeechSeg):
        # 在第 2 个单元的讲解中途提问 —— 应该等这一段讲完才让位。
        # 只发一次：重讲段落也带着 S1-U02 的 unit_id，不加这个开关会再问一次。
        if seg.kind == "unit" and seg.unit_id == "S1-U02" \
                and h.machine is not None and fired["n"] == 0:
            fired["n"] += 1
            await h.machine.handle_input("没懂")

    hs.auto_input = auto_input
    await hs.run(None)

    kinds = hs.kinds
    # 让位链路的顺序：回执 → 过渡 → 重讲 → 重入 → 回到讲课
    assert "ack" in kinds, "收到提问后必须先回执（I3）"
    assert "yield" in kinds, "段末让位必须播过渡话术"
    assert "rephrase" in kinds, "说没听懂应走重讲链，而不是普通回答"

    i_ack = kinds.index("ack")
    i_yield = kinds.index("yield")
    i_rephrase = kinds.index("rephrase")
    i_resume = kinds.index("resume")
    assert i_ack < i_yield < i_rephrase < i_resume, f"让位/归位顺序错乱: {kinds}"

    # 让位点必须落在完整句之后（段末让位）
    assert hs.state.resume_point is None, "归位后必须清空让位点"
    assert "ANSWERING" in hs.phases and "RESUMING" in hs.phases, "必须经过 RESUMING（I5）"

    # 让位延迟必须被采集，且在有真实播放耗时的情况下必须 > 0
    # —— 这个数就是段末让位的代价（V1/V2），0 说明根本没在测真实链路
    assert hs.metrics.yield_ms, "让位延迟必须被采集（§7.6）"
    assert hs.metrics.yield_ms[0] > 0, "有播放耗时却报 0ms，说明指标算错了"
    print(f"[T1] 让位延迟 {hs.metrics.yield_ms[0]:.0f}ms · "
          f"状态序列 {hs.phases}")


# ===========================================================================
# T2 不变量 I5：ANSWERING 没有直通 LECTURING 的边
# ===========================================================================

async def test_no_answering_to_lecturing_direct_edge():
    hs = Headless(probe_window=0.15)

    async def auto_input(h: Headless, seg: SpeechSeg):
        if seg.kind == "unit" and seg.unit_id == "S1-U01" and h.machine is not None:
            await h.machine.handle_input("这一步为什么这样做？")

    hs.auto_input = auto_input
    await hs.run(None)

    phases = hs.phases
    for i in range(len(phases) - 1):
        if phases[i] == "ANSWERING":
            assert phases[i + 1] != "LECTURING", (
                f"ANSWERING 之后直接到了 LECTURING（第 {i} 步）—— 违反 I5：{phases}"
            )
    assert "ANSWERING" in phases and "RESUMING" in phases, f"应当经过回答与重入: {phases}"
    assert "answer" in hs.kinds, "普通提问应走 answer 通道"
    print(f"[T2] I5 成立 · 状态序列 {phases}")


# ===========================================================================
# T3 §4.5：ANSWERING 期间的新输入只入队，不打断当前回答
# ===========================================================================

async def test_input_while_answering_queues():
    hs = Headless(probe_window=0.15)
    second_sent = {"v": False}

    async def auto_input(h: Headless, seg: SpeechSeg):
        if seg.kind == "unit" and seg.unit_id == "S1-U01" and h.machine is not None:
            await h.machine.handle_input("这一步为什么这样做？")
        if seg.kind == "answer" and not second_sent["v"] and h.machine is not None:
            second_sent["v"] = True
            # 正在回答时又提一个问题 —— 应只入队，且当前回答必须说完两句
            await h.machine.handle_input("那 Δ=0 呢？")

    hs.auto_input = auto_input
    await hs.run(None)

    answers = hs.text_of("answer")
    assert len(answers) >= 2, f"当前回答不该被打断，应说完两句: {answers}"
    # 两条提问都要有归属（I1：不丢输入）
    answered = hs.llm.answer_calls
    assert answered >= 1, "至少处理了一条提问"
    print(f"[T3] 回答 {len(answers)} 句 · LLM 回答次数 {answered} · 队列未丢输入")


# ===========================================================================
# T4 探针：超时 → 记 vague → 推进（沉默不等于理解）
# ===========================================================================

async def test_probe_timeout_marks_vague_and_advances():
    hs = Headless(probe_window=0.15)
    await hs.run(None)

    assert hs.metrics.probe_sent > 0, "页尾必须发出探针"
    assert hs.metrics.probe_timeout > 0, "无输入时应记录超时"
    assert any(v == "vague" for v in hs.state.comprehension.values()), \
        "超时必须记为 vague（弱信号），且不能记为 confirmed"
    assert len(hs.pages) >= 2, f"探针超时后必须自动翻页: pages={hs.pages}"
    assert "ask" in hs.kinds, "探针应以提问话术的形式发出"
    print(f"[T4] 探针 发出{hs.metrics.probe_sent} 超时{hs.metrics.probe_timeout} · "
          f"翻页 {hs.pages} · 理解信号 {hs.state.comprehension}")


# ===========================================================================
# T5 §5.5：手动"下一页"= 明确跳过，未确认项必须进 deferred（不静默丢弃）
# ===========================================================================

async def test_manual_next_defers_unconfirmed():
    hs = Headless(probe_window=5.0)          # 窗口够长，确保不会自动超时
    fired = {"v": False}

    async def auto_input(h: Headless, seg: SpeechSeg):
        # 第一个单元刚开始就手动翻页 —— 本页剩余单元全部未确认
        if seg.kind == "unit" and not fired["v"] and h.machine is not None:
            fired["v"] = True
            await h.machine.handle_command("next")

    hs.auto_input = auto_input
    await hs.run(None)

    assert hs.state.deferred, "手动跳过时未确认的知识点必须进 deferred，不能静默丢弃"
    assert hs.metrics.advance_manual >= 1, "手动翻页必须计数"
    assert 1 in hs.pages, f"应已切换到第 2 页: {hs.pages}"
    print(f"[T5] 手动翻页 → 待回看 {sorted(set(hs.state.deferred))}")


# ===========================================================================
# T6 §5.7：往回翻 = 重讲信号，且台账只增不改（I6）
# ===========================================================================

async def test_prev_triggers_rephrase_and_keeps_ledger():
    hs = Headless(probe_window=0.15)
    fired = {"v": False}

    async def auto_input(h: Headless, seg: SpeechSeg):
        if seg.kind == "unit" and seg.unit_id.startswith("S2-") and not fired["v"] \
                and h.machine is not None:
            fired["v"] = True
            await h.machine.handle_command("prev")

    hs.auto_input = auto_input
    await hs.run(None)

    assert 0 in hs.pages, f"应回到第 1 页: {hs.pages}"
    assert hs.state.covered, "台账只增不改，往回翻不得清空（I6）"
    print(f"[T6] 往回翻 → pages={hs.pages} · 台账 {len(hs.state.covered)} 条保留")


# ===========================================================================
# T7 重讲链轮次上限：到顶必须暂放，不能无限循环（§4.7）
# ===========================================================================

async def test_rephrase_has_ceiling():
    """学生在一个单元上反复说"没懂"：必须逐轮升级，到顶暂放，且**不重播同一句话**。"""
    hs = Headless(probe_window=0.15)
    st = {"n": 0}

    async def auto_input(h: Headless, seg: SpeechSeg):
        # 只在第一个单元的每一句话上说一次，共 6 次 —— 足够冲过上限（4 轮）
        if seg.kind not in ("unit", "rephrase") or h.machine is None:
            return
        if seg.kind == "unit" and seg.unit_id != "S1-U01":
            return
        if st["n"] >= 6:
            return
        st["n"] += 1
        await h.machine.handle_input("没懂")

    hs.auto_input = auto_input
    await hs.run(None)

    rephrase_texts = [s.text for s in hs.spoken if s.kind == "rephrase"]
    rounds_seen = sorted(set(hs.state.q_rounds.values()))

    assert hs.llm.rephrase_calls == hs.settings.max_rephrase - 1, (
        f"重讲链应恰好调用 LLM {hs.settings.max_rephrase - 1} 次（第 "
        f"{hs.settings.max_rephrase} 轮走暂放不调 LLM），实际 {hs.llm.rephrase_calls} 次"
    )
    assert hs.state.q_rounds.get("S1-U01") == hs.settings.max_rephrase, (
        f"轮次应停在 {hs.settings.max_rephrase}，实际 {hs.state.q_rounds}"
    )
    assert max(rounds_seen) <= hs.settings.max_rephrase, f"轮次超过上限: {rounds_seen}"
    assert hs.state.deferred, "轮到上限时必须记入待回看（暂放 + 承诺）"
    # 关键：到顶之后学生再喊没懂，老师不能原地重播同一段话
    assert len(set(rephrase_texts)) >= 4, (
        f"到顶后重复播放相同话术（学生听到的是复读机）: {rephrase_texts}"
    )
    print(f"[T7] 重讲 LLM 调用 {hs.llm.rephrase_calls} 次 · 轮次 {rounds_seen} · "
          f"话术 {len(set(rephrase_texts))} 种 · 待回看 {sorted(set(hs.state.deferred))} · "
          f"到顶后又喊 {hs.state.deferred_too - 1} 次")


# ===========================================================================
# T8 模型连不上：课堂必须"有话说"，不能静默卡死
# ===========================================================================

class BrokenLLM(FakeLLM):
    """模拟 ollama 没起来 —— 流式生成中途直接抛连接错误。"""

    def __init__(self, die_after: int = 0):
        super().__init__()
        self.die_after = die_after

    async def answer_stream(self, q, kind, ctx, *, on_think=None):
        self.answer_calls += 1
        for i in range(self.die_after):
            yield f"这是第{i + 1}句，然后连接断了。"
        raise RuntimeError("APIConnectionError: Connection error.")

    async def rephrase_stream(self, q, round_no, ctx, *, max_sentences=4, on_think=None):
        self.rephrase_calls += 1
        raise RuntimeError("APIConnectionError: Connection error.")
        yield  # pragma: no cover


async def test_llm_down_still_speaks():
    """连不上模型时，学生不能只等到一句"生成失败"。

    实测场景：学生提问 → 回执 → 让位话术 → **课堂没声了**。
    正确行为：说一句兜底话术（连一句都没生成），或者收个尾（说了一半断掉），
    然后照常重入、继续讲课。
    """
    for die_after, want in ((0, "一句都没生成"), (1, "说了一半断掉")):
        hs = Headless(probe_window=0.15)
        hs.llm = BrokenLLM(die_after=die_after)
        fired = {"n": 0}

        async def auto_input(h: Headless, seg: SpeechSeg):
            if seg.kind == "unit" and seg.unit_id == "S1-U01" \
                    and h.machine is not None and fired["n"] == 0:
                fired["n"] += 1
                await h.machine.handle_input("这一步为什么这样做？")

        hs.auto_input = auto_input
        await hs.run(None)

        kinds = hs.kinds
        answered = [s.text for s in hs.spoken if s.kind == "answer"]
        assert answered, f"[{want}] 模型挂了之后学生一个字的回答都没听到：{kinds}"
        assert "resume" in kinds, f"[{want}] 失败后没有重入，课堂没能回到主线：{kinds}"
        assert kinds[-1] in ("unit", "ask"), f"[{want}] 没有回到讲课：{kinds}"
    print("[T8] 模型连不上 · 两段路径都有兜底话术并成功归位")


# ===========================================================================
# T9 「明白了」是确认，不是提问：不入队、不被"回答"，老师直接往下讲
# ===========================================================================

async def test_confirm_reply_is_not_queued():
    """讲课途中说"明白了"：不能排进问题队列，也不能让老师去"回答"它。

    实测踩到的原样：学生回一句"明白了"，老师先说
    "收到，我把这一段讲完就来回答你"，然后真的去回答这句确认。
    确认是对当前内容的收尾，不是新问题 —— 它既不排队，也不该打断讲课。
    """
    hs = Headless(probe_window=0.15)
    fired = {"v": False}

    async def auto_input(h: Headless, seg: SpeechSeg):
        if seg.kind == "unit" and not fired["v"] and h.machine is not None:
            fired["v"] = True
            await h.machine.handle_input("明白了")

    hs.auto_input = auto_input
    await hs.run(None)

    assert fired["v"], "用例没跑起来：没有在讲课途中插话"
    assert hs.state.pending_questions == [], \
        f"「明白了」被排进了问题队列: {[q.raw for q in hs.state.pending_questions]}"
    assert hs.llm.answer_calls == 0, "老师不该去'回答'一句『明白了』"
    assert "ack" not in hs.kinds, f"讲课途中的确认不该触发回执话术: {hs.kinds}"
    assert "confirmed" in hs.state.comprehension.values(), \
        f"确认必须记为强信号: {hs.state.comprehension}"
    assert hs.kinds.count("unit") > 1, "老师应继续往下讲，不能停在那里"
    print(f"[T9] 「明白了」→ 不入队 · 无回执 · 无答疑 · 记为 confirmed · "
          f"继续讲了 {hs.kinds.count('unit')} 句")


# ===========================================================================
# T10 「不明白」里有"明白"，但它**不是**听懂了
# ===========================================================================

async def test_deny_reply_is_not_taken_as_confirm():
    """探针窗口里回"不明白"：必须走重讲链，绝不能记成 confirmed 并翻页。

    旧实现是 `k in text` 的子串判断 —— "不明白"命中"明白"，于是学生说
    "没懂"的那一刻，探针当场记 confirmed 并翻页，正好把唯一没懂的地方跳过去。
    否定必须先于肯定判定。
    """
    hs = Headless(probe_window=0.3)
    fired = {"v": False}

    async def auto_input(h: Headless, seg: SpeechSeg):
        # 在**提问话术还没播完**时回话：这正是真实时序 ——
        # 学生听到"到这里清楚吗"就立刻答了，此时状态已经是 PAUSING。
        if seg.kind == "ask" and not fired["v"] and h.machine is not None:
            fired["v"] = True
            await h.machine.handle_input("不明白")

    hs.auto_input = auto_input
    await hs.run(None)

    assert fired["v"], "用例没跑起来：没有在探针窗口里回话"
    assert "confirmed" not in hs.state.comprehension.values(), \
        f"『不明白』被记成了听懂了: {hs.state.comprehension}"
    assert hs.llm.rephrase_calls > 0, "『不明白』必须触发重讲链（换说法再讲一遍）"
    print(f"[T10] 「不明白」→ 理解信号 {sorted(set(hs.state.comprehension.values()))} · "
          f"重讲 {hs.llm.rephrase_calls} 次（没有被当成听懂了）")


# ===========================================================================
# T11~T19 路线分支：状态机真的按它走（skip / revisit / finish / 失败兜底）
# ===========================================================================
#
# 路线与"就地重讲"的分工（这一组用例的判据）：v1/v2 是同一个格子换一种说法，讲完还在原地；
# 路线是**跨格**的走向 —— 跳过几格、回前面重讲再汇回、或者直接下课。所以这里断言的全是
# "位置"与"播出序列"，不是文本。选择（谁能被选中）由 test_director_decider.py 钉住，
# 真实 Director 的接线由 test_variant_e2e.py 的端到端用例证明；这一组只钉状态机怎么走。

def route_handoff(route: Route, *, unit_id: str, candidate_id: str = "c-route") -> dict:
    """把一条 Route 包成 decider 会交出来的那张交接单。

    形状与 director/decider.py 的 apply_at_boundary 一致：Route 自己的字段全都在
    （**route.to_dict()），外加判别键 payload="route"。这里直接把 to_dict 铺开，
    而不是再抄一遍字段名 —— 抄一遍之后 route.py 加字段，状态机这边就悄悄少一个。
    """
    return {"payload": "route", "candidate_id": candidate_id, "unit_id": unit_id,
            "target_unit": unit_id, "seq": 0, "position": None, **route.to_dict()}


class StubDirector:
    """最小决策层替身：在**指定的那一格**边界上交出预先定好的一条路线。

    为什么不用真 Director：这一组用例钉的是"状态机怎么走"（跳到哪一格、什么时候汇回、
    什么时候下课），不是"J 怎么选"。替身让每个用例只动一处变量，也不带候选池与预取的噪音。
    它只实现状态机会调的那三个方法，签名与真 Director 一致（多一个方法就说明状态机越界了）。
    """

    def __init__(self, handoff: dict | None = None, *, at_unit: str | None = None,
                 times: int = 1):
        self.pool = CandidatePool()
        self.handoff = handoff
        self.at_unit = at_unit      # None = 任何边界都交；否则只在某一格的边界上交
        self.times = times          # 最多交几次（超过之后交给状态机自己去挡）
        self.boundaries = 0         # 被问过几次"下一格怎么讲"
        self.served = 0             # 真的交出去几次
        self.closed = 0

    def prefetch(self, unit, state, *, target_unit=None, position=None):
        return None

    async def apply_at_boundary(self, unit, state, *, target_unit=None, position=None):
        self.boundaries += 1
        if self.at_unit is not None and unit.unit_id != self.at_unit:
            return None
        if self.served >= self.times:
            return None
        self.served += 1
        return self.handoff

    def close(self):
        self.closed += 1


def build_machine(*, director=None, probe_window: float = 0.05) -> Machine:
    """一个**不跑 pump 循环**的 Machine：只驱动单个出口或单步 next_speech 时用。

    需要整节课跑完的用例走 Headless（它自带 pump 与自动 speech_done）；
    需要精确到"到底是哪一个出口做的"的用例走这里 —— 只有单步可辨。
    """
    script = build_script()
    st = ClassroomState(course_id=script.course_id)
    return Machine(script, st, make_settings(probe_window), FakeLLM(),
                   PhaseHandlers.noop(), MetricsTracker(), director=director)


def metric_kinds(m: Machine) -> list[str]:
    return [e["kind"] for e in m.metrics.events]


def unit_runs(hs: Headless) -> list[str]:
    """把播出序列压成"一段一段"的单元 id（同一单元连续的几句合成一段）。

    比逐句数句子稳：句数由 split_sentences 决定，而这里要证的是**顺序与次数**
    （"回前面又讲了一遍"只有按段看才看得清）。
    """
    runs: list[str] = []
    for seg in hs.spoken:
        if seg.kind != "unit":
            continue
        if not runs or runs[-1] != seg.unit_id:
            runs.append(seg.unit_id)
    return runs


async def test_skip_route_jumps_to_merge_and_never_speaks_the_middle() -> None:
    """skip：直接到 merge_node 那一格的起点，中间几格一个字都不念。

    为什么"一句都没念"要单独断言：把被跳过的格子当作"讲完了"是最省事的实现
    （进度直接写满），它同样能把指针送到 merge，区别只在学生会听到一段没打算讲的课。
    """
    hs = Headless(probe_window=0.05)
    fork = hs.script.order_index("S1-U01")
    merge = hs.script.order_index("S1-U03")
    skipped = [u.unit_id for u in hs.script.units[fork + 1:merge]]
    route = Route(route_id=derive_route_id("skip", fork, merge), kind="skip",
                  fork_node=fork, merge_node=merge, steps=[], est_sec=-12.0,
                  reason="后两格是同一道例题的重复")
    hs.director = StubDirector(route_handoff(route, unit_id="S1-U01"))
    await hs.run()

    assert skipped, "用例没造出被跳过的格子"
    runs = unit_runs(hs)
    for uid in skipped:
        assert uid not in runs, f"被跳过的 {uid} 一句都不该念：{runs}"
    assert runs[:2] == ["S1-U01", "S1-U03"], f"skip 之后下一段必须是汇回目标：{runs}"

    log = hs.state.route_log
    assert len(log) == 1 and log[0]["kind"] == "skip", log
    assert (log[0]["to_unit"], log[0]["skipped"]) == ("S1-U03", skipped), log[0]
    assert "route_applied" in metric_kinds(hs.machine)
    assert hs.machine._finished.is_set(), "跳讲之后课堂必须照常上完（不许卡在边界上）"
    print(f"[T11] skip：跳过 {skipped} → 直接讲 S1-U03（台账 {log[0]["route_id"]}）")


async def test_skip_route_across_pages_goes_through_goto_slide() -> None:
    """跨页 skip 必须经 _goto_slide：page_changed 与换页才有的 q_rounds 重置。

    绕过 _goto_slide 也能把指针挪到下一页，区别只在"前端不知道翻页了"和"重讲轮次没归零"
    —— 两者都不报错，只会在真实课堂上变成"老师已经讲到第二页、学生端还停在第 1 页"。
    """
    hs = Headless(probe_window=0.05)
    fork = hs.script.order_index("S1-U01")
    merge = hs.script.order_index("S2-U02")
    target = hs.script.unit("S2-U02")
    hs.state.q_rounds["S2-U02"] = 3          # 换页必须把它清掉（§4.7）
    route = Route(route_id=derive_route_id("skip", fork, merge), kind="skip",
                  fork_node=fork, merge_node=merge, steps=[], est_sec=-60.0,
                  reason="这一页都是复习")
    hs.director = StubDirector(route_handoff(route, unit_id="S1-U01"))
    await hs.run()

    runs = unit_runs(hs)
    assert runs[:2] == ["S1-U01", "S2-U02"], f"跨页 skip 的落点必须是 merge 目标：{runs}"
    for uid in ("S1-U02", "S1-U03", "S1-U04", "S2-U01"):
        assert uid not in runs, f"被跳过的 {uid} 不该念：{runs}"
    assert hs.pages[:1] == [target.slide], f"跨页跳必须发 page_changed：{hs.pages}"
    assert "S2-U02" not in hs.state.q_rounds, \
        f"翻页必须重置重讲轮次（§4.7）：{hs.state.q_rounds}"
    assert hs.metrics.advance_auto >= 1, "跨页跳要计入自动翻页"
    print(f"[T12] 跨页 skip：page_changed={hs.pages[:1]} · q_rounds 已重置")


async def test_revisit_route_replays_the_steps_then_merges() -> None:
    """revisit：回到 steps[0] 重讲（逐字重播）→ 讲完汇回 merge_node。

    这段序列同时钉住两个设计选择：
      · 只有 steps 里的格子被清进度 —— 岔出那一格（S1-U03）已经讲完，再重讲一遍等于
        让学生把同一段听三遍（旧的"换讲法整段重播"就是这么来的）；
      · "汇回"由 merge_node 决定，不是"最后一步之后自然推进"—— 否则落点取决于页尾探针
        走哪条路，而那是看运气的事。
    """
    hs = Headless(probe_window=0.05)
    fork = hs.script.order_index("S1-U03")
    merge = hs.script.order_index("S1-U04")
    route = Route(route_id=derive_route_id("revisit", fork, merge), kind="revisit",
                  fork_node=fork, merge_node=merge,
                  steps=[RouteStep(unit_id="S1-U01"), RouteStep(unit_id="S1-U02")],
                  est_sec=30.0, reason="第一次用了公式，回去重讲")
    hs.director = StubDirector(route_handoff(route, unit_id="S1-U03"), at_unit="S1-U03")
    await hs.run()

    runs = unit_runs(hs)
    assert runs[:6] == ["S1-U01", "S1-U02", "S1-U03", "S1-U01", "S1-U02", "S1-U04"], \
        f"补回头序列不对（应回到 S1-U01 重讲再汇回 S1-U04）：{runs}"
    first_pass = [s.text for s in hs.spoken
                  if s.kind == "unit" and s.unit_id == "S1-U01"]
    half = len(first_pass) // 2
    assert len(first_pass) % 2 == 0 and first_pass[:half] == first_pass[half:], \
        f"补回头必须逐字重讲同一段（不是换一种说法）：{first_pass}"

    log = hs.state.route_log
    assert log[-1]["kind"] == "revisit" and log[-1]["to_unit"] == "S1-U01", log[-1]
    assert log[-1]["steps"] == ["S1-U01", "S1-U02"], log[-1]
    kinds = metric_kinds(hs.machine)
    assert "route_applied" in kinds and "route_merged" in kinds, kinds
    assert len([e for e in hs.machine.metrics.events
                if e["kind"] == "route_applied"]) == 1, "同一条路线只执行一次"
    assert hs.machine._finished.is_set()
    print(f"[T13] revisit：{runs[:6]} → 汇回 S1-U04 并上完课")


async def test_finish_route_ends_the_class_through_finish_all() -> None:
    """finish：走 _finish_all 正常收尾 —— 学生看到的是"讲完了"，不是卡住。

    class_end 这条 events 只在 _finish_all 里发（唯一收尾入口，不变量 I5），
    所以"有 class_end"就是"走的是那个入口"的直接证据。
    """
    hs = Headless(probe_window=0.05)
    total = len(hs.script.units)
    fork = hs.script.order_index("S1-U01")
    route = Route(route_id=derive_route_id("finish", fork, total), kind="finish",
                  fork_node=fork, merge_node=total, steps=[], est_sec=-300.0,
                  reason="时间不够，提前收尾")
    hs.director = StubDirector(route_handoff(route, unit_id="S1-U01"))
    await hs.run()

    assert set(unit_runs(hs)) == {"S1-U01"}, \
        f"提前收尾之后不该再讲任何一格：{unit_runs(hs)}"
    assert hs.machine._finished.is_set(), "finish 必须真的下课（_finished 置位）"
    assert "class_end" in metric_kinds(hs.machine), "收尾必须走 _finish_all（唯一出口）"
    assert hs.state.route_log[-1]["kind"] == "finish"
    assert hs.state.phase is Phase.LECTURING, "收尾把相位收回 LECTURING（不是停在等待）"
    print(f"[T14] finish：讲到 S1-U01 收尾 · class_end 已记 · 相位 {hs.state.phase.value}")


async def test_broken_route_falls_back_to_v0_and_records_error() -> None:
    """越界 / kind 不认识 / 重讲起点不存在 → 退回默认 v0 并记 error，绝不抛异常。

    三种坏法是 C 现造的结构真的会给出来的（它是模型，不是校验器）。共同要求只有一条：
    **课堂照常往前走** —— 一条路线写错最多少跳一格，抛出去整节课停在边界上。
    """
    cases = [
        ({"kind": "skip", "fork_node": 0, "merge_node": 999, "steps": []}, "合并点越界"),
        ({"kind": "teleport", "fork_node": 0, "merge_node": 1, "steps": []}, "kind 不认识"),
        ({"kind": "revisit", "fork_node": 2, "merge_node": 3,
          "steps": [{"unit_id": "S9-U99"}]}, "重讲起点不存在"),
    ]
    for bad, why in cases:
        hs = Headless(probe_window=0.05)
        handoff = {"payload": "route", "candidate_id": "c-bad", "unit_id": "S1-U01",
                   "target_unit": "S1-U01", "seq": 0, "position": None,
                   "route_id": "r-bad", "est_sec": 0.0, "reason": ""}
        handoff.update(bad)
        hs.director = StubDirector(handoff)
        await hs.run()

        kinds = metric_kinds(hs.machine)
        assert "route_error" in kinds, f"[{why}] 坏路线必须记一条 error：{kinds}"
        assert not hs.state.route_log, \
            f"[{why}] 没走成的路线不该进台账：{hs.state.route_log}"
        assert unit_runs(hs)[:2] == ["S1-U01", "S1-U02"], \
            f"[{why}] 必须照常讲下一格（v0）：{unit_runs(hs)}"
        assert hs.machine._finished.is_set(), f"[{why}] 课堂照常上完"
    print("[T15] 三种坏路线 → 都退回 v0 并记 route_error，课堂照常上完")


async def test_late_boundary_also_executes_routes() -> None:
    """迟到边界（_unit_exhausted，真实课堂最常走的那条）同样执行路线。

    为什么专门钉这条出口：学生说没懂 → 让位 → 重讲 → 归位时这一格早就讲完了，
    于是"为下一格选路"是在 _unit_exhausted 里补的。只把路线接在正常讲完那条路上，
    真实课件上一次都不会走 —— 而它不会报错，只会"路线被选中了却没跳"。
    """
    m = build_machine(probe_window=0.05)
    unit = m.script.unit("S1-U01")
    fork, merge = m.script.order_index("S1-U01"), m.script.order_index("S1-U03")
    route = Route(route_id=derive_route_id("skip", fork, merge), kind="skip",
                  fork_node=fork, merge_node=merge, steps=[], est_sec=-9.0, reason="")
    m.director = StubDirector(route_handoff(route, unit_id="S1-U01"))
    m.state.unit_progress["S1-U01"] = 99          # 早就讲完了：这是一次迟到的边界

    await m._unit_exhausted(unit)

    assert m.state.slide_index == 0
    assert m.state.seg_index == m._seg_index_of(m.script.unit("S1-U03")), \
        f"迟到边界必须同样走路线：seg={m.state.seg_index}"
    assert "route_applied" in metric_kinds(m)
    print("[T16] 迟到边界（_unit_exhausted）→ 路线照常执行")


async def test_after_probe_exit_also_executes_routes() -> None:
    """探针窗口里被回应之后的推进出口，同样执行路线。

    桌面上的场景：老师在页尾问"到这里清楚吗"，学生答"懂了，后面几页跳过吧"。
    这条出口如果自己写一份页尾判断，路线就会在这里被悄悄丢回"照常翻页"。
    """
    m = build_machine(probe_window=0.05)
    st = m.state
    page_end = m.script.units_of(0)[-1]
    fork = m.script.order_index(page_end.unit_id)
    merge = m.script.order_index("S2-U02")
    st.slide_index = 0
    st.seg_index = len(m.script.units_of(0)) - 1
    st.unit_progress[page_end.unit_id] = 99
    await m._set_phase(Phase.PAUSING)             # 探针窗口正开着
    route = Route(route_id=derive_route_id("skip", fork, merge), kind="skip",
                  fork_node=fork, merge_node=merge, steps=[], est_sec=-40.0, reason="")
    m._route_pending = route_handoff(route, unit_id=page_end.unit_id)

    await m._advance_after_probe()

    assert (st.slide_index, st.seg_index) == (1, 1), \
        f"必须落在 merge 目标（S2-U02）上：{(st.slide_index, st.seg_index)}"
    assert st.phase is Phase.LECTURING, \
        "跳完之后不许停在探针相位（学生会以为老师在等他回答一个已跳过的问题）"
    assert "route_applied" in metric_kinds(m)
    print("[T17] 探针之后的出口（_advance_after_probe）→ 路线照常执行")


async def test_same_route_id_is_executed_only_once() -> None:
    """同一条路线（route_id）只执行一次：第二次交进来必须被挡住（dup）。

    为什么按 id 而不按 candidate_id 记账：C 会反复重算同一条走向，route_id 是稳定幂等键
    （derive_route_id），而 candidate_id 会变。不去重，学生卡住的那一段就会被无限重讲
    —— 这是补回头唯一能把课上不完的方式。
    """
    m = build_machine(director=StubDirector())
    unit = m.script.unit("S1-U01")
    fork, merge = m.script.order_index("S1-U01"), m.script.order_index("S1-U03")
    route = Route(route_id=derive_route_id("skip", fork, merge), kind="skip",
                  fork_node=fork, merge_node=merge, steps=[], est_sec=-9.0, reason="")

    m._route_pending = route_handoff(route, unit_id="S1-U01")
    assert await m._take_route(unit) is True, "第一次必须真的走"
    assert m.state.seg_index == m._seg_index_of(m.script.unit("S1-U03"))

    m.state.seg_index = 0                         # 回到岔出格，同一个 route_id 再来一次
    m._route_pending = route_handoff(route, unit_id="S1-U01", candidate_id="c-again")
    assert await m._take_route(unit) is False, "同一条路线不许走第二遍"
    refused = [e for e in m.metrics.events if e["kind"] == "route_refused"]
    assert [e.get("reason") for e in refused] == ["dup"], refused
    applied = [e for e in m.metrics.events if e["kind"] == "route_applied"]
    assert len(applied) == 1 and len(m.state.route_log) == 1, (applied, m.state.route_log)
    print("[T18] 同一 route_id 只执行一次 · 第二次记 route_refused:dup")


async def test_revisit_blocks_route_selection_while_replaying() -> None:
    """补回头重讲期间不选路：一个请求都不发给决策层。

    挡在"发请求之前"而不是发完再拒：接过来了却不用，那条候选已经是 consumed 的，
    等于白白销毁一条路线；而且重讲时换上另一种说法就不再是"补回头"了。
    """
    director = StubDirector({"payload": "route"})
    m = build_machine(director=director)
    m._route_replay = {"route_id": "r-revisit-1", "merge_node": 3, "last_unit": "S1-U02"}

    await m._route_next_unit(m.script.unit("S1-U01"))

    assert director.boundaries == 0, "重讲期间不该向决策层问路"
    assert m._route_pending is None
    print("[T19] 补回头重讲期间：0 次选路请求")


# ===========================================================================
# 入口
# ===========================================================================

ALL = [test_yield_flow, test_no_answering_to_lecturing_direct_edge,
       test_input_while_answering_queues, test_probe_timeout_marks_vague_and_advances,
       test_manual_next_defers_unconfirmed, test_prev_triggers_rephrase_and_keeps_ledger,
       test_rephrase_has_ceiling, test_llm_down_still_speaks,
       test_confirm_reply_is_not_queued, test_deny_reply_is_not_taken_as_confirm,
       test_skip_route_jumps_to_merge_and_never_speaks_the_middle,
       test_skip_route_across_pages_goes_through_goto_slide,
       test_revisit_route_replays_the_steps_then_merges,
       test_finish_route_ends_the_class_through_finish_all,
       test_broken_route_falls_back_to_v0_and_records_error,
       test_late_boundary_also_executes_routes,
       test_after_probe_exit_also_executes_routes,
       test_same_route_id_is_executed_only_once,
       test_revisit_blocks_route_selection_while_replaying]


async def _main() -> int:
    failed = 0
    for fn in ALL:
        try:
            await fn()
            print(f"  ✅ {fn.__name__}\n")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}\n")
        except Exception as e:
            failed += 1
            print(f"  💥 {fn.__name__}\n     {type(e).__name__}: {e}\n")
    total = len(ALL)
    print(f"结果：{total - failed}/{total} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
