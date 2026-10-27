"""Agent-C 一次唤醒流水线的测试 —— 门卫、窗口、校验、幂等写池、失败不逃逸。

为什么这些断言值得写：C 的失败在课堂上**完全不可见**（§11：C 挂掉学生无感），
所以"它到底有没有干活、干错了什么"只能靠这里的账。五件事必须成立：
1. 门卫关闸时一次模型都不许调（§7.3 的 8~15 次是拿这个换来的）；
2. 一节 40 分钟课（40 次唤醒）里开闸 8~15 次（§12 阶段 3 验收①的离线口径）；
3. 校验不过的提案一条都不许进池（§11）；
4. 60s 重放不许把池子塞满（§7.2 ⑥）；
5. 模型抛异常 / 超时 / 返回垃圾时，唤醒不许把异常抛出去、不许污染池子。

全程零网络、零真实 sleep（"超时"用一个永不返回的协程 + 极小的预算验证）。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.producer import (  # noqa: E402
    EXPIRE_MARGIN_UNITS,
    PROMPT_CHAR_BUDGET,
    REASON_POOL_FULL_EVICTABLE,
    GateWatermark,
    ProposalRequest,
    Snapshot,
    TemplateProposer,
    boundary_need_expires,
    build_prompt,
    candidate_id_for,
    gate_open,
    run_tick,
    snapshot_from,
    target_window,
)
from deck.schema import (  # noqa: E402
    KIND_TEXT,
    ROLE_TITLE,
    DeckDoc,
    Element,
    Paragraph,
    RunStyle,
    SlideDoc,
    TextBody,
)
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.route import derive_route_id  # noqa: E402
from models import LessonScript, Slide, SlideElement  # noqa: E402
from state import ClassroomState, LectureUnit, Probe  # noqa: E402

# ===========================================================================
# 合成一节 40 单元的课
# ===========================================================================

#: 每页单元数与全课单元数。40 个单元 ≈ 一节 40 分钟的课（每格约 1 分钟），
#: 也正好是 §7.3 "40 分钟 = 40 次唤醒"的口径 —— 只有单元数确定，
#: "门卫开了几次"才能被断言成一个数字。
PER_SLIDE = 10
TOTAL_UNITS = 40

#: 一个"单元原文"：两句、每句都 ≥8 字（llm.split_sentences 才认它是两句），
#: 且不超过 250 字 —— 这样 make_variants 造得出变体、validate_variant 过得去。
UNIT_TEXT = "第{n}个单元讲的是这一点。它和上一格连在一起看才完整。"


def make_lesson(total: int = TOTAL_UNITS, per_slide: int = PER_SLIDE) -> tuple[LessonScript, DeckDoc]:
    """合成课件 + 脚本：每页 per_slide 个单元，每单元一句可念的原文。

    为什么不用 script.build_script()：那是真实课件（页数、单元数都不定），
    而 §12 阶段 3 验收要的是"40 分钟课 ≈ 40 个单元"这个确定口径。
    单元锚点留空（= 沿用主单元），因为这里验的是流水线，不是锚点生成。
    """
    n_slides = (total + per_slide - 1) // per_slide
    deck_slides: list[SlideDoc] = []
    script_slides: list[Slide] = []
    for s in range(n_slides):
        title = f"第{s + 1}页"
        eid = f"s{s + 1:02d}-sh01"
        pid = f"{eid}-p01"
        deck_slides.append(SlideDoc(
            index=s, title=title,
            elements=[Element(
                element_id=eid, kind=KIND_TEXT, role=ROLE_TITLE,
                text=TextBody(paras=[Paragraph(pid=pid, text=title,
                                               runs=[RunStyle(text=title)])]),
            )],
        ))
        script_slides.append(Slide(
            index=s, title=title,
            elements=[SlideElement(eid, "title", title),
                      SlideElement(pid, "para", title, parent_id=eid)],
        ))

    units: list[LectureUnit] = []
    for i in range(total):
        s, k = divmod(i, per_slide)
        units.append(LectureUnit(
            unit_id=f"S{s + 1:02d}-U{k + 1:02d}", slide=s, index_in_slide=k,
            text=UNIT_TEXT.format(n=i + 1), anchors=[], kp=[f"K{i + 1}"],
        ))

    script = LessonScript(course_id="producer-test", title="40 分钟合成课",
                          session_goals=["验证 C 的流水线"],
                          slides=script_slides, units=units)
    deck = DeckDoc(deck_id="producer-test", title=script.title, slides=deck_slides)
    return script, deck


def _stand_at(state: ClassroomState, unit: LectureUnit) -> ClassroomState:
    """把状态摆到某个单元开头（真实课堂由 machine 推进，这里只摆位置）。"""
    state.slide_index = unit.slide
    state.seg_index = unit.index_in_slide
    state.seq += 1
    return state


def _state_at(script: LessonScript, index: int) -> ClassroomState:
    state = ClassroomState(course_id=script.course_id)
    return _stand_at(state, script.units[index])


def _snapshot(
    script: LessonScript,
    index: int,
    *,
    confused: int = 0,
    deferred: int = 0,
    timeouts: int = 0,
    questions: tuple[str, ...] = (),
) -> Snapshot:
    """在某个单元上摆出一组信号，然后取快照（门卫的输入）。"""
    state = _state_at(script, index)
    for i in range(confused):
        state.comprehension[f"K{i + 1}"] = "confused"
    for i in range(deferred):
        state.deferred.append(f"Q{i + 1}")
    for i in range(timeouts):
        state.probe_log.append(Probe(slide=0, unit_id="probe", opened_at=float(i),
                                     closed_at=float(i) + 1.0, result="timeout"))
    for q in questions:
        state.emit("student", q)
    return snapshot_from(state, script)


def _candidate(unit_id: str, variant_id: str = "v1", *, expires: int = 99) -> Candidate:
    """往池子里塞一条"别人造的"候选（用来把池子填满）。

    expires 默认 99（改动前就是这么写的哨兵值）：既有的门卫用例因此一个字都不用改；
    而"远期备货"的新用例按真实节奏给 12~16 这种量级（= 目标格的序号 + 余量，见 producer）。
    """
    return Candidate(
        candidate_id=candidate_id_for(unit_id, variant_id), target_unit=unit_id,
        variant=UnitVariant(variant_id=variant_id, label="换说法", text="换一种讲法。"),
        when={}, source="preset", created_at=0.0, created_at_unit="S01-U01",
        expires_at_seq=expires,
    )


def _proposal(
    target_unit: str,
    *,
    text: str = "换个说法讲一遍这个知识点。它讲的是同一个意思。",
    variant_id: str = "v1",
    when: dict | None = None,
    anchors: list | None = None,
) -> dict:
    """一条形状正确的提案（各条测试只改一个字段来构造非法）。"""
    return {
        "target_unit": target_unit,
        "variant": {"variant_id": variant_id, "label": "换说法", "text": text,
                    "anchors": list(anchors or []), "ops": []},
        "when": {"consecutive_confused": {">=": 2}} if when is None else when,
        "reason": "测试用",
    }


# ===========================================================================
# 替身（全部离线）
# ===========================================================================

#: 区分"没给 payload"和"给的就是 None（垃圾数据）"——后者必须真的走坏数据分支。
_UNSET = object()


class CountingProposer:
    """计数假模型：证明"关闸时一次都没被调用"，并交出预置 payload 或内层实现。"""

    def __init__(self, payload: object = _UNSET, *, inner: object | None = None) -> None:
        self.payload = [] if payload is _UNSET else payload
        self.inner = inner
        self.calls = 0
        self.requests: list[ProposalRequest] = []

    async def propose(self, request: ProposalRequest) -> object:
        self.calls += 1
        self.requests.append(request)
        if self.inner is not None:
            return await self.inner.propose(request)
        return self.payload


class BoomProposer:
    """抛异常的模型（§11：C 某次唤醒失败 → 60s 后重试，学生无感）。"""

    async def propose(self, request: ProposalRequest) -> object:
        raise RuntimeError("provider 挂了")


class HangProposer:
    """永不返回的模型：占住一个永不 set 的 Event，不消耗真实时间。"""

    async def propose(self, request: ProposalRequest) -> object:
        await asyncio.Event().wait()
        return []


async def _tick(
    state: ClassroomState,
    script: LessonScript,
    deck: DeckDoc | None,
    pool: CandidatePool,
    watermark: GateWatermark,
    proposer: object,
    **kwargs: object,
) -> object:
    kwargs.setdefault("now", 1000.0)
    return await run_tick(state=state, script=script, deck=deck, pool=pool,
                          watermark=watermark, proposer=proposer, **kwargs)


# ===========================================================================
# AC1 · 门卫的两条否决
# ===========================================================================

def test_gate_veto_pool_full() -> None:
    """否决①：池子 alive 满 CAP 即关闸（§7.3），理由进指标。"""
    script, _ = make_lesson()
    pool = CandidatePool()
    for i in range(CandidatePool.CAP):
        pool.put(_candidate(f"S01-U{i + 1:02d}"))
    assert pool.alive_count() == CandidatePool.CAP

    snap = _snapshot(script, 3, confused=1)     # 有信号，但池满优先
    assert gate_open(snap, pool, GateWatermark()) == (False, "pool_full")


def test_gate_opens_when_the_pool_is_full_but_holds_evictable_stock() -> None:
    """池满但**挤得动**时门卫放行：理由串换成 pool_full_evictable（这是实测缺陷的正面）。

    改动前池满即 veto，于是 C 连"写一条当期货"的机会都没有 —— 而池子恰恰是被它自己的
    远期备货占满的。放行的前提只有一个：池里存在**比当期需求更远**、且期距够得着的活货
    （判据由池子给，与 put 共用一把尺子，见 pool._eviction_victim）。
    """
    script, _ = make_lesson()
    pool = CandidatePool()
    for i, expires in enumerate((12, 13, 14, 15, 16)):
        pool.put(_candidate(f"S01-U{i + 9:02d}", expires=expires))
    assert pool.alive_count() == CandidatePool.CAP

    snap = _snapshot(script, 3, confused=1)
    opened, reason = gate_open(snap, pool, GateWatermark())

    assert opened is True, reason
    assert reason == f"new_confused+{REASON_POOL_FULL_EVICTABLE}", \
        f"池满但可驱逐：放行，且理由串里要能一眼看出这次要挤人：{reason}"
    assert pool.alive_count() == CandidatePool.CAP, "门卫只读池子：放行不等于已经驱逐"
    assert pool.eviction_count() == 0, "驱逐发生在写池那一步，门卫不许先动手"


def test_gate_still_vetoes_a_full_pool_that_cannot_be_evicted() -> None:
    """池满且**挤不动**（全是当期货）时仍旧 veto pool_full：一次模型调用都不许花。

    这是"新行为不许扩大"的那一半：放行的判据是"存在比当期需求更远的活货"，而当期货的
    expires 都 ≤ 当期需求时，池子里根本没有该让位的那一条 —— 花一次调用换回来一堆
    "池子已满"，账上看起来像 C 干了活，实际上一个字节都没写。
    """
    script, _ = make_lesson()
    pool = CandidatePool()
    need = boundary_need_expires(3)
    for i, expires in enumerate(range(need - 4, need + 1)):      # 全都 ≤ 当期需求
        pool.put(_candidate(f"S01-U{i + 1:02d}", expires=expires))

    assert gate_open(_snapshot(script, 3, confused=1), pool, GateWatermark()) == (False, "pool_full")


def test_gate_veto_too_close_to_end() -> None:
    """否决②：剩余单元 < 5 即关闸（§7.3 停止期）—— 造了也来不及被选中。"""
    script, _ = make_lesson()
    pool = CandidatePool()

    snap = _snapshot(script, 35, confused=1)    # 40 个单元的第 36 个：剩 4 个
    assert snap.remaining_units == 4
    assert gate_open(snap, pool, GateWatermark()) == (False, "too_close_to_end")

    snap_ok = _snapshot(script, 34, confused=1)  # 剩 5 个：还允许上货
    assert snap_ok.remaining_units == 5
    assert gate_open(snap_ok, pool, GateWatermark())[0] is True


# ===========================================================================
# AC1 · 门卫的五条信号
# ===========================================================================

def test_gate_signal_new_confused() -> None:
    """信号①：相对水位新出现的困惑（§7.3）。"""
    script, _ = make_lesson()
    pool = CandidatePool()
    watermark = GateWatermark()

    assert gate_open(_snapshot(script, 3), pool, watermark) == (False, "no_signal")
    assert gate_open(_snapshot(script, 3, confused=1), pool, watermark) == (True, "new_confused")
    # 水位已推进：同一个困惑信号不会重复开闸（否则 60s 一次会一直开）。
    assert gate_open(_snapshot(script, 3, confused=1), pool, watermark) == (False, "no_signal")
    assert gate_open(_snapshot(script, 3, confused=2), pool, watermark) == (True, "new_confused")


def test_gate_signal_deferred_grew() -> None:
    """信号②：待回看列表变长（§7.3）。"""
    script, _ = make_lesson()
    pool = CandidatePool()
    watermark = GateWatermark()

    assert gate_open(_snapshot(script, 3, deferred=1), pool, watermark) == (True, "deferred_grew")
    assert gate_open(_snapshot(script, 3, deferred=1), pool, watermark) == (False, "no_signal")
    assert gate_open(_snapshot(script, 3, deferred=2), pool, watermark) == (True, "deferred_grew")


def test_gate_signal_probe_timeout_streak() -> None:
    """信号③：连续探针超时 ≥ 2（一次超时只是走神，连着两次才算卡住）。"""
    script, _ = make_lesson()
    pool = CandidatePool()

    assert gate_open(_snapshot(script, 3, timeouts=1), pool, GateWatermark()) == (False, "no_signal")
    assert gate_open(_snapshot(script, 3, timeouts=2), pool, GateWatermark()) == (True, "probe_timeout_streak")


def test_gate_signal_repeated_question() -> None:
    """信号④：最近两条学生提问同文 = 重复追问（§7.3）。"""
    script, _ = make_lesson()
    pool = CandidatePool()

    one = _snapshot(script, 3, questions=("老师这里没懂",))
    assert (one.last_question, one.question_repeat) == ("老师这里没懂", False)
    assert gate_open(one, pool, GateWatermark()) == (False, "no_signal")

    twice = _snapshot(script, 3, questions=("老师这里没懂", "老师这里没懂"))
    assert twice.question_repeat is True
    assert gate_open(twice, pool, GateWatermark()) == (True, "repeated_question")

    changed = _snapshot(script, 3, questions=("老师这里没懂", "刚才那个再说一遍"))
    assert gate_open(changed, pool, GateWatermark()) == (False, "no_signal")


def test_gate_signal_just_consumed() -> None:
    """信号⑤：J 刚消费掉一条（腾出位置）—— 读后清零，只开一次闸。"""
    script, _ = make_lesson()
    pool = CandidatePool()
    pool.put(_candidate("S01-U05"))
    pool.mark_consumed(candidate_id_for("S01-U05", "v1"))

    snap = _snapshot(script, 3)
    assert gate_open(snap, pool, GateWatermark()) == (True, "just_consumed")
    # just_consumed() 是读后清零的：没新的消费就不该再开闸，否则门卫形同常开。
    assert gate_open(snap, pool, GateWatermark()) == (False, "no_signal")


def test_gate_watermark_survives_veto() -> None:
    """关闸期间攒下的信号不许被吞（水位只在开闸时推进）。

    池满时学生又没懂了一次：如果那次关闸顺手推进了水位，这条困惑就永久消失了 ——
    等池子腾出位置，C 反而不会上货。这条测试钉的就是这个顺序。
    """
    script, _ = make_lesson()
    pool = CandidatePool()
    watermark = GateWatermark()

    for i in range(CandidatePool.CAP):
        pool.put(_candidate(f"S01-U{i + 1:02d}"))
    assert gate_open(_snapshot(script, 3, confused=1), pool, watermark) == (False, "pool_full")

    pool.mark_consumed(candidate_id_for("S01-U01", "v1"))   # 腾出一个位置
    assert pool.alive_count() == CandidatePool.CAP - 1
    opened, reason = gate_open(_snapshot(script, 3, confused=1), pool, watermark)
    assert (opened, reason) == (True, "new_confused"), "关闸期间攒下的困惑必须还在"


async def test_a_full_pool_of_far_stock_still_lets_the_route_land_once() -> None:
    """池满远期备货时，这个边界的路线**真的进池**，且整次唤醒只驱逐一条。

    这是门卫放行的兑现：改动前门卫在这里直接 veto，路线一条都造不出来；改后路线挤掉
    最远的那条备货落地，而两条变体照旧被拒 —— 一次唤醒只换一条货（不刷模型、不刷池子），
    账上三件事齐了：called=True、written 只有路线、evicted 只有那条最远的备货。
    """
    script, deck = make_lesson()
    pool = CandidatePool()
    for i, expires in enumerate((12, 13, 14, 15, 16)):
        # variant_id 用 x1：默认的 v1 会和 TemplateProposer 给窗口单元造的候选撞 id，
        # 撞上就走幂等覆盖（alive 不涨），那测的就不是驱逐了。
        assert pool.put(_candidate(f"S01-U{i + 9:02d}", "x1", expires=expires)) is True

    state = _state_at(script, 3)
    state.deferred.append("Q-让门卫开闸")
    state.comprehension["K4"] = "confirmed"        # 确认听懂 → 这个边界该走 skip
    result = await _tick(state, script, deck, pool, GateWatermark(), TemplateProposer())

    route_id = derive_route_id("skip", 3, 6)
    assert (result.called, result.ok) == (True, True)
    assert REASON_POOL_FULL_EVICTABLE in result.reason, result.reason
    assert result.written == (route_id,), f"只该换进来这条路线：{result.written}"
    assert result.evicted == ("c-S01-U13-x1",), \
        f"被驱逐的必须是 expires 最大的那条（16）：{result.evicted}"
    assert pool.get("c-S01-U13-x1").status == "expired"
    assert pool.alive_count() == CandidatePool.CAP, "容量不变：换一条，不扩池"
    assert [r.payload for r in result.rejected] == ["variant", "variant"], \
        f"两条变体照旧被拒（整次唤醒只换一条货）：{result.rejected}"
    assert pool.eviction_count() == 1, "一次唤醒最多一次驱逐"


# ===========================================================================
# AC1 · 关闸时一次模型都不调
# ===========================================================================

async def test_gate_closed_never_calls_proposer() -> None:
    """关闸时 proposer 的调用次数必须是 0（§7.3 的 8~15 次全靠这条）。"""
    script, deck = make_lesson()
    pool = CandidatePool()
    watermark = GateWatermark()
    state = _state_at(script, 3)

    counting = CountingProposer([_proposal("S01-U09")])

    # ① 无信号
    res = await _tick(state, script, deck, pool, watermark, counting)
    assert (res.called, res.ok, res.reason) == (False, True, "no_signal")
    assert counting.calls == 0
    assert pool.alive_count() == 0

    # ② 池满（即便有信号）
    for i in range(CandidatePool.CAP):
        pool.put(_candidate(f"S01-U{i + 1:02d}"))
    state.comprehension["K9"] = "confused"
    res = await _tick(state, script, deck, pool, watermark, counting)
    assert (res.called, res.reason) == (False, "pool_full")
    assert counting.calls == 0

    # ③ 快下课（即便有信号）
    pool2 = CandidatePool()
    late = _state_at(script, 36)
    late.comprehension["K9"] = "confused"
    res = await _tick(late, script, deck, pool2, GateWatermark(), counting)
    assert (res.called, res.reason) == (False, "too_close_to_end")
    assert counting.calls == 0


# ===========================================================================
# AC2 · 一节 40 分钟课：门卫实际开闸 8~15 次
# ===========================================================================

#: 学生侧信号的时间表（全部写死：开闸次数必须是可复现的数字，不能靠随机）。
#: 口径是"每格约 1 分钟"的 40 分钟课：一节课里真正卡住四五次、被追同一句话一两次、
#: 两次探针没人应 —— 这是设计上的常态（§7.3 "40 次唤醒只有 8~15 次真调模型"）。
_CONFUSED_AT = frozenset({3, 11, 20, 28})
_DEFERRED_AT = frozenset({7, 24, 33})
_TIMEOUT_AT = frozenset({15, 16})
_ASK_AT = {18: "老师，这里没懂", 19: "老师，这里没懂", 20: "刚才那个再说一遍",
           26: "这个和上一页有关吗"}
_CONSUME_AT = frozenset({4, 12, 21})


async def test_full_lesson_gate_opens_between_8_and_15() -> None:
    """离线模拟一节 40 分钟课：门卫实际开闸次数落在 8~15（§12 阶段 3 验收①）。

    这是"验收①"的离线口径：真调模型的那一次（called=True）才计数，
    门卫否决（called=False）不计数。整个过程零网络 —— 模型是 TemplateProposer。
    """
    script, deck = make_lesson()
    state = ClassroomState(course_id=script.course_id)
    pool = CandidatePool()
    watermark = GateWatermark()
    proposer = CountingProposer(inner=TemplateProposer(per_tick=1))

    opened: list[str] = []
    closed: dict[str, int] = {}
    alive_ids: list[str] = []

    for i, unit in enumerate(script.units):
        _stand_at(state, unit)
        if i in _CONFUSED_AT:
            state.comprehension[f"K{i + 1}"] = "confused"
        if i in _DEFERRED_AT:
            state.deferred.append(f"Q{i}")
        state.probe_log.append(Probe(
            slide=unit.slide, unit_id=unit.unit_id,
            opened_at=float(i), closed_at=float(i) + 1.0,
            result="timeout" if i in _TIMEOUT_AT else "confirmed",
        ))
        if i in _ASK_AT:
            state.emit("student", _ASK_AT[i], unit.unit_id)

        result = await _tick(state, script, deck, pool, watermark, proposer, now=float(i))
        if result.called:
            opened.append(result.reason)
            alive_ids.extend(result.written)
        else:
            closed[result.reason] = closed.get(result.reason, 0) + 1

        pool.expire_stale(i)                    # 单元推进时作废过期候选（§8）
        if i in _CONSUME_AT:                    # J 在边界上真的用掉了一条
            for cid in list(alive_ids):
                cand = pool.get(cid)
                if cand is not None and cand.status == "alive":
                    pool.mark_consumed(cid)
                    alive_ids.remove(cid)
                    break

    assert proposer.calls == len(opened), "called=True 的次数必须等于真实模型调用次数"
    assert 8 <= len(opened) <= 15, f"§12 阶段 3 验收①：开闸 {len(opened)} 次，应落在 8~15（{opened}）"
    # 一节课不可能全程开闸：至少有一半唤醒是被门卫挡下的（省钱的那一半）。
    assert len(opened) < len(script.units) / 2
    # 课末必须进入停止期。
    assert closed.get("too_close_to_end", 0) > 0
    # 五条信号都必须真的在一节课里出现过，否则这条测试只验证了"少调模型"。
    assert set(opened) == {"new_confused", "deferred_grew", "probe_timeout_streak",
                           "repeated_question", "just_consumed"}, f"实测：{sorted(set(opened))}"


# ===========================================================================
# AC3 · 上货窗口
# ===========================================================================

def test_target_window_basic() -> None:
    """窗口 = [当前+5, 当前+15]（§7.4）。"""
    assert target_window(0, 40) == range(5, 16)
    assert target_window(10, 40) == range(15, 26)


def test_target_window_clamped_near_end() -> None:
    """接近课末时窗口被 clamp 到总单元数（不越界）。"""
    assert target_window(30, 40) == range(35, 40)
    assert target_window(34, 40) == range(39, 40)     # 只剩最后 1 个可上货


def test_target_window_empty_when_beyond_end() -> None:
    """current+5 越过总单元数时返回空 range（这次没有可上货的目标）。"""
    assert target_window(35, 40) == range(0)
    assert target_window(36, 40) == range(0)
    assert target_window(39, 40) == range(0)
    assert target_window(0, 3) == range(0)            # 课太短，连窗口都没有


def test_target_window_matches_script_slice() -> None:
    """窗口就是 script.units 的一个切片：[+5, +15] 闭区间共 11 格，课末自动夹断。"""
    script, _ = make_lesson()
    for seq in range(len(script.units) + 3):
        window = target_window(seq, len(script.units))
        assert [script.units[i].unit_id for i in window] == [
            u.unit_id for u in script.units[seq + 5: seq + 16]
        ]
    assert len(target_window(0, len(script.units))) == 11


# ===========================================================================
# AC4 · 校验不过一律不写池
# ===========================================================================

async def _tick_with(payload: object, *, index: int = 3) -> tuple[object, CandidatePool, CountingProposer]:
    """在一个"门卫会开闸"的位置跑一次唤醒，返回（结果，池子，假模型）。"""
    script, deck = make_lesson()
    pool = CandidatePool()
    state = _state_at(script, index)
    state.comprehension["K1"] = "confused"       # 让门卫开闸
    proposer = CountingProposer(payload)
    result = await _tick(state, script, deck, pool, GateWatermark(), proposer)
    assert result.called is True
    return result, pool, proposer


async def test_rejects_overlong_text() -> None:
    """超字数（§5.4）：300 字的变体与 v0 的尺子不合，丢弃且不写池。"""
    result, pool, _ = await _tick_with([_proposal("S01-U09", text="很长的讲法。" * 60)])

    assert result.written == ()
    assert result.rejected_count == 1
    assert "超出上限" in "；".join(result.rejection_reasons)
    assert pool.alive_count() == 0


async def test_rejects_anchor_not_on_slide() -> None:
    """锚点不在本页（§5.4）：锚点集合取 SlideDoc.anchor_index()，不存在的 id 一律拒。"""
    result, pool, _ = await _tick_with(
        [_proposal("S01-U09", anchors=[{"element_id": "s09-sh09"}])]
    )

    assert result.written == ()
    assert "锚点 s09-sh09 在本页不存在" in "；".join(result.rejection_reasons)
    assert pool.alive_count() == 0


async def test_rejects_illegal_when_key() -> None:
    """when 用了非法 key（§4.6 只认三种）：这条候选在 J 那边永远不匹配，必须拒。"""
    result, pool, _ = await _tick_with([_proposal("S01-U09", when={"mood": {"==": "bored"}})])

    assert result.written == ()
    assert "key mood 不合法" in "；".join(result.rejection_reasons)
    assert pool.alive_count() == 0


async def test_rejects_illegal_when_operator() -> None:
    """when 的算子不合法（只认 >= <= == in not_in）。"""
    result, pool, _ = await _tick_with(
        [_proposal("S01-U09", when={"consecutive_confused": {"~=": 2}})]
    )

    assert result.written == ()
    assert "算子 ~= 不合法" in "；".join(result.rejection_reasons)
    assert pool.alive_count() == 0


async def test_rejects_target_unit_outside_window() -> None:
    """target_unit 必须是窗口内真实存在的 unit_id（§7.2 ⑤ 的第二道校验）。"""
    result, pool, _ = await _tick_with([_proposal("S99-U99")])

    assert result.written == ()
    assert "不在上货窗口内" in "；".join(result.rejection_reasons)
    assert pool.alive_count() == 0

    # 当前是 3 号单元，窗口 = 序号 [8, 19)：S01-U09 在窗口内，S01-U08 刚过去。
    result2, pool2, _ = await _tick_with([_proposal("S01-U09"), _proposal("S01-U08")])
    assert len(result2.written) == 1 and result2.rejected_count == 1
    assert pool2.alive_count() == 1


async def test_rejects_empty_target_and_variant_id() -> None:
    """空 target_unit 与空 variant_id 都拒：前者让窗口校验失效，后者让幂等失效。"""
    result, pool, _ = await _tick_with([
        _proposal(""),
        _proposal("S01-U09", variant_id=""),
        "这不是提案",
    ])

    reasons = "；".join(result.rejection_reasons)
    assert result.written == ()
    assert result.rejected_count == 3
    assert "target_unit 为空" in reasons
    assert "variant_id 为空" in reasons
    assert "不是对象" in reasons
    assert pool.alive_count() == 0


async def test_rejection_detail_is_in_tick_result() -> None:
    """被拒的条数与原因必须出现在 TickResult 里（否则课堂上没人知道为什么没上货）。"""
    result, pool, _ = await _tick_with([
        _proposal("S01-U09"),                                    # 好
        _proposal("S01-U10", text="很长的讲法。" * 60),           # 超字数
        _proposal("S01-U11", when={"mood": {"==": "bored"}}),     # 非法 key
        _proposal("S77-U77"),                                     # 窗口外
    ])

    assert len(result.written) == 1
    assert result.rejected_count == 3
    assert [r.index for r in result.rejected] == [1, 2, 3]
    assert all(r.reasons for r in result.rejected)
    assert pool.alive_count() == 1


# ===========================================================================
# AC5 · 幂等写池（60s 重放不许塞满池子）
# ===========================================================================

async def test_replay_is_idempotent() -> None:
    """同一次 tick 重放 5 次：alive 不增长，同一个候选的 version 递增（§7.2 ⑥）。

    每次重放都让状态多一个"没懂"的知识点 —— 门卫因此每次都开闸（真实场景里学生
    一直在卡），而模型给的提案一模一样。要验证的正是"同一提案不会把池子塞满"。
    """
    script, deck = make_lesson()
    pool = CandidatePool()
    watermark = GateWatermark()
    state = _state_at(script, 3)
    payload = [_proposal("S01-U09"), _proposal("S01-U10", variant_id="v2")]
    proposer = CountingProposer(payload)

    written_each: list[tuple[str, ...]] = []
    for n in range(5):
        state.comprehension[f"K{n + 1}"] = "confused"    # 学生还在卡 → 门卫还开
        result = await _tick(state, script, deck, pool, watermark, proposer, now=float(n))
        assert result.called is True, "重放时门卫必须开闸（否则测的不是幂等）"
        written_each.append(result.written)

    assert proposer.calls == 5
    assert pool.alive_count() == 2, "重放不许让 alive 增长（否则 5 次就撞满 CAP）"
    assert len(set(written_each)) == 1, "同一提案必须映射到同一 candidate_id"

    first = pool.get(written_each[0][0])
    assert first is not None
    assert first.version == 5, "覆盖分支由池子数版本：写 5 次 → version=5"


async def test_candidate_metadata() -> None:
    """候选的记账字段：source / created_at / created_at_unit / expires_at_seq。"""
    script, deck = make_lesson()
    pool = CandidatePool()
    state = _state_at(script, 3)
    state.comprehension["K1"] = "confused"
    proposer = CountingProposer([_proposal("S01-U09")])

    result = await _tick(state, script, deck, pool, GateWatermark(), proposer, now=1234.5)

    cand = pool.get(result.written[0])
    assert cand is not None
    assert cand.source == "agent_c"
    assert cand.created_at == 1234.5                       # 用注入的 now，可断言
    assert cand.created_at_unit == "S01-U04"               # 当前单元
    assert cand.target_unit == "S01-U09"
    assert cand.candidate_id == "c-S01-U09-v1"
    # 目标单元序号 8 + 余量：过了两格还没被用上就该作废，否则白占 alive 名额。
    assert cand.expires_at_seq == 8 + EXPIRE_MARGIN_UNITS
    assert cand.when == {"consecutive_confused": {">=": 2}}


# ===========================================================================
# AC6 · 失败不逃逸、不污染池子
# ===========================================================================

async def test_proposer_exception_is_contained() -> None:
    """模型抛异常：返回说明原因的 TickResult，池子干净（§11）。"""
    script, deck = make_lesson()
    pool = CandidatePool()
    state = _state_at(script, 3)
    state.comprehension["K1"] = "confused"

    result = await _tick(state, script, deck, pool, GateWatermark(), BoomProposer())

    assert (result.called, result.ok, result.reason) == (True, False, "proposer_error")
    assert pool.status() == {"alive": 0, "consumed": 0, "expired": 0}


async def test_proposer_timeout_is_contained() -> None:
    """模型超时：60s 后重试，学生无感（§11）—— 不抛异常、不写池。

    "超时"用一个永不返回的协程 + 1ms 预算验证：零真实 sleep，毫秒级。
    """
    script, deck = make_lesson()
    pool = CandidatePool()
    state = _state_at(script, 3)
    state.comprehension["K1"] = "confused"

    result = await _tick(state, script, deck, pool, GateWatermark(), HangProposer(),
                         timeout_s=0.001)

    assert (result.called, result.ok, result.reason) == (True, False, "proposer_timeout")
    assert result.prompt, "超时也要留下这次问了什么，否则复盘时无从下手"
    assert pool.status() == {"alive": 0, "consumed": 0, "expired": 0}


async def test_proposer_garbage_is_contained() -> None:
    """模型返回垃圾（不是数组）：不抛异常，说明失败原因，池子干净。"""
    for garbage in ("这不是数组", None, {"target_unit": "S01-U09"}, 42):
        result, pool, _ = await _tick_with(garbage)
        assert (result.called, result.ok, result.reason) == (True, False, "bad_payload")
        assert pool.status() == {"alive": 0, "consumed": 0, "expired": 0}


async def test_payload_of_non_dicts_is_rejected_not_fatal() -> None:
    """数组里混进非对象：逐条拒，不影响同一批里合法的那条。"""
    result, pool, _ = await _tick_with([None, _proposal("S01-U09"), 42])

    assert len(result.written) == 1
    assert result.rejected_count == 2
    assert pool.alive_count() == 1


# ===========================================================================
# 快照与自包含输入
# ===========================================================================

def test_snapshot_is_zero_side_effect() -> None:
    """读快照不许推进状态机、不许改任何字段（§7.7：只读 get_state）。"""
    script, _ = make_lesson()
    state = _state_at(script, 3)
    state.comprehension["K1"] = "confused"
    state.deferred.append("Q1")
    state.probe_log.append(Probe(slide=0, unit_id="S01-U04", opened_at=0.0,
                                 closed_at=1.0, result="timeout"))
    state.emit("student", "老师这里没懂", "S01-U04")

    before = (
        state.seq, state.slide_index, state.seg_index, state.phase,
        len(state.transcript), len(state.deferred), len(state.probe_log),
        dict(state.comprehension), len(state.pending_questions),
    )
    first = snapshot_from(state, script)
    second = snapshot_from(state, script)
    after = (
        state.seq, state.slide_index, state.seg_index, state.phase,
        len(state.transcript), len(state.deferred), len(state.probe_log),
        dict(state.comprehension), len(state.pending_questions),
    )

    assert first == second, "同一状态两次快照必须完全一样（可复现）"
    assert before == after, "快照不许产生任何副作用"
    assert first.current_unit == "S01-U04"
    assert first.current_seq == 3
    assert first.slide == 0
    assert first.total_units == TOTAL_UNITS
    assert first.remaining_units == TOTAL_UNITS - 4
    assert first.confused_count == 1
    assert first.deferred == ("Q1",)
    assert first.probe_result == "timeout"
    assert first.probe_timeout_streak == 1
    assert first.last_question == "老师这里没懂"


def test_snapshot_deferred_is_a_copy() -> None:
    """快照持有不可变副本：状态之后再变，快照不许跟着变（否则水位会漂）。"""
    script, _ = make_lesson()
    state = _state_at(script, 3)
    state.deferred.append("Q1")
    snap = snapshot_from(state, script)

    state.deferred.append("Q2")
    assert snap.deferred == ("Q1",)


def test_snapshot_skips_unclosed_probe() -> None:
    """未闭合的探针不算表态（Probe 是先 append 后 close 的）。"""
    script, _ = make_lesson()
    state = _state_at(script, 3)
    state.probe_log.append(Probe(slide=0, unit_id="u", opened_at=0.0, result=""))
    state.probe_log.append(Probe(slide=0, unit_id="u", opened_at=1.0, closed_at=2.0,
                                 result="timeout"))
    state.probe_log.append(Probe(slide=0, unit_id="u", opened_at=3.0, result=""))

    snap = snapshot_from(state, script)
    assert snap.probe_result == "timeout"
    assert snap.probe_timeout_streak == 1


async def test_prompt_is_self_contained() -> None:
    """组装出的输入必须自包含：快照 + 窗口脚本 + 池子现状 + 固定系统提示（§7.5）。"""
    script, deck = make_lesson()
    pool = CandidatePool()
    state = _state_at(script, 3)
    state.comprehension["K1"] = "confused"
    proposer = CountingProposer([_proposal("S01-U09")])

    result = await _tick(state, script, deck, pool, GateWatermark(), proposer)

    prompt = result.prompt
    assert result.snapshot is not None
    assert result.snapshot.current_unit in prompt          # 当前快照
    assert all(uid in prompt for uid in result.window)     # 窗口脚本（每个单元都在）
    assert "候选池现状" in prompt and "alive 0/5" in prompt  # 池子现状
    assert "你是上课中的造分支角色" in prompt                # 固定系统提示（DIRECTING）
    assert "consecutive_confused" in prompt                # 输出契约（when 的 key）
    assert "proposals" in prompt                           # 输出契约（顶层键，见下一条用例）
    assert len(prompt) <= PROMPT_CHAR_BUDGET, "上界是硬约束（§7.5 约 3000 token）"
    assert len(result.window) == 11                         # [+5, +15] 共 11 格


def test_prompt_demands_an_object_with_the_proposals_key() -> None:
    """输出契约必须是**对象**，与 `json_mode=True` 的 `response_format` 同形（§7.2 ④）。

    为什么这条必须反过来盯提示词本身：契约写反（要数组）时**所有离线用例都是绿的** ——
    假 client 根本不看提示词。只有真实模型才会现形，而且不是"拒绝"，是把协议参数
    原样回显：`{"type": "json_object", "content": []}`（真实 key + 真实 prompt +
    真实课件窗口实测）。所以这里断言两件事：形状是 `{"proposals": [...]}`，
    且"只输出一个 JSON 数组"这种与 `response_format` 打架的说法**不能再出现**。
    """
    script, _ = make_lesson()
    snap = _snapshot(script, 0)
    window = tuple(script.units[:3])
    prompt = build_prompt(snap, window, {"alive": 0, "consumed": 0, "expired": 0})

    # ① 形状：顶层是一个带 proposals 键的对象
    assert '{"proposals": [' in prompt, f"顶层必须是 proposals 包裹的对象：{prompt[-400:]!r}"
    assert "顶层只能有 proposals 这一个键" in prompt, \
        "只有一个键这件事必须写死，否则模型会自己套壳（对象里塞别的东西就解析不出来）"

    # ② 不许再出现与 json_mode 矛盾的说法：两个要求并存 = 让模型在两个矛盾里猜
    assert "只输出一个 JSON 数组" not in prompt, (
        "提示词要数组、response_format 要对象 —— 模型会卡在这个矛盾里，"
        '实测返回的是 {"type": "json_object", "content": []}')
    assert "不要 markdown 代码块" in prompt and "不要任何解释" in prompt, \
        "围栏与解释会白占 budget，且容易被当成正文"

    # ③ 原有硬约束一条不少：改形状不等于放松口径
    assert "consecutive_confused / probe_result / comprehension" in prompt
    assert ">= <= == in not_in" in prompt
    assert "target_unit 必须是上面窗口里出现过的 unit_id" in prompt
    assert "不超过 250 字" in prompt
    assert "讲同一个知识点" in prompt

    print("提示词契约：{\"proposals\": [...]} 且与 json_mode 不再矛盾；五条硬约束全部保留")


async def test_prompt_survives_budget_clip() -> None:
    """窗口超长时 prompt 被裁，但"要输出什么"整块必须在（否则模型只会回一句解释）。

    构造一个 40 格 × 200 字的窗口把预算顶破 —— 这正是 §7.5 "上界是硬约束"要防的
    情形：任何一个字段被灌进长文本，都不许把输出契约挤出 prompt。
    """
    script, _ = make_lesson()
    fat = replace(script.units[0], text="很长的单元原文，用来把预算顶破。" * 14)
    window = tuple(fat for _ in range(40))
    snap = _snapshot(script, 0)
    prompt = build_prompt(snap, window, {"alive": 0, "consumed": 0, "expired": 0})

    assert len(prompt) <= PROMPT_CHAR_BUDGET
    assert "【你要输出的东西】" in prompt
    assert "target_unit 必须是上面窗口里出现过的 unit_id" in prompt
    assert prompt.rstrip().endswith("这种指令。"), "输出契约必须在裁剪之后拼上（尾部不被裁）"


async def test_template_proposer_is_deterministic_offline() -> None:
    """离线的规则实现：同一输入两次跑出完全相同的提案与写入（可复现）。"""
    script, deck = make_lesson()

    async def one_run() -> tuple[tuple[str, ...], str]:
        pool = CandidatePool()
        state = _state_at(script, 3)
        state.comprehension["K1"] = "confused"
        result = await _tick(state, script, deck, pool, GateWatermark(),
                             TemplateProposer(), now=7.0)
        return result.written, result.prompt

    first = await one_run()
    second = await one_run()

    assert first == second
    assert len(first[0]) == 2, "per_tick=2：一次唤醒造两条"
    assert first[0] == ("c-S01-U09-v1", "c-S01-U10-v2")
