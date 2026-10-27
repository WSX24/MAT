"""Agent-C 的**路线**分支（skip / revisit / finish）：从"池里只可能有变体"到真的造出岔路。

为什么单独一个文件
------------------
`test_producer.py` 钉住的是"C 的一次唤醒对不对"（门卫 / 窗口 / 校验 / 幂等写池），
那里的产物只有变体。路线是另一种载荷（跨好几格：跳过 / 回头 / 收尾），它把三件事
同时推到了台前，而每一件都能单独坏掉又不报错：

  ① **结构**：fork / merge / steps / est_sec 必须一次给全且自洽（越界的路线一旦进池，
     J 会选中它，状态机就会走出课程）；
  ② **触发条件**：只能用 facts.py 已有的字段与算子（否则 J 那边没有对应的比较能力），
     而且"造它的条件"就是"J 重算的条件"（否则造出来永远选不中）；
  ③ **记账**：target_unit 必须是岔出格的**下一格**（candidates_for 的既有过滤才生效）、
     candidate_id 必须由结构派生（否则每次唤醒都造一条新货，CAP=5 被同一件事填满）。

逐条对应的验收（编号沿用任务书）：
  R1) 三种走向：给定事实 → 结构正确（fork / merge / steps / est_sec 符号 / when / target_unit / source）
  R2) 稳态：同一事实重复算出同一个 candidate_id；同 fork 只留一条（一把岔路一把锁），
      不同 fork 的路线互不作废（它们是**别的边界**的选项）
  R3) 路线与变体同池共存：CAP=5、先到先得（路线不许挤掉变体）、candidates_for 的过滤照旧
  R4) 越界 / 结构非法 / 坏模型输出：丢弃 + 落一条 metric，不抛异常、不污染既有候选
  R5) LLM 路径：假模型吐 {route: {...}} → 同一把尺子体检 → 写池（零网络）
  R6) 模型没给 when 时不许留空守卫（空 when 恒真 = 每个边界都被选中）

全部离线：零网络、假 client、零真实 sleep。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.producer import (  # noqa: E402
    ROUTE_FINISH_AFTER,
    ROUTE_WHEN,
    GateWatermark,
    TemplateProposer,
    plan_route,
    route_kind,
    route_proposal,
    run_tick,
)
from agent.proposer import LlmProposer, ProposalError, parse_proposals  # noqa: E402
from director.assembly import serve_agent_c  # noqa: E402
from director.decider import Director, RuleDecider  # noqa: E402
from director.facts import DecisionFacts  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.presets import preset_candidates  # noqa: E402
from director.route import derive_route_id  # noqa: E402
from eval import MetricsTracker  # noqa: E402
from state import ClassroomState  # noqa: E402
from tests.test_machine import make_settings  # noqa: E402
from tests.test_producer import make_lesson  # noqa: E402

#: 路线的岔出格 = **当前格**的序号。C 在第 3 格（下标 3）醒 → 路线从第 3 格岔出、
#: merge 落在第 4/6 格、target = 第 4 格；于是 J 在"第 3 格讲完"这个边界上就能看见它。
#: 改动前的口径是"上货窗口首格"（current + EARLIEST_OFFSET = 8）—— 那是**变体**的
#: 窗口，路线拿它当锚就会在 5 格之后才到期，而这 5 格里每一次唤醒都会把它顶掉
#: （实测见 tests/test_producer_route_timeline.py 与 prototype/_accept_timeline.py）。
FORK_AT_THIRD = 3


# ===========================================================================
# 夹具：一条唤醒的全部输入（课堂状态 + 课件 + 池子）
# ===========================================================================

def _stand_at(script, index: int) -> ClassroomState:
    """把状态摆到某一格的开头（真实课堂由 machine 推进，这里只摆位置）。"""
    state = ClassroomState(course_id=script.course_id)
    unit = script.units[index]
    state.slide_index = unit.slide
    state.seg_index = unit.index_in_slide
    return state


def _facts_at(script, index: int, *, comprehension: str = "", stuck: int = 0
              ) -> DecisionFacts:
    """某一格的决策事实（与 facts.facts_from_state 的派生口径一致）。

    只写"单元级"的那两个量：comprehension 按本单元 kp 记（facts 取最坏值），
    consecutive_confused 由 q_rounds 记（重讲轮次 + 未答提问，见 facts_from_state）。
    """
    return DecisionFacts(
        unit_id=script.units[index].unit_id,
        slide=script.units[index].slide,
        consecutive_confused=stuck,
        comprehension=comprehension,
        q_rounds=stuck,
    )


async def _tick_at(index: int, *, comprehension: str = "", stuck: int = 0,
                   proposer: object | None = None, pool: CandidatePool | None = None,
                   now: float = 100.0):
    """在第 index 格上摆好单元级事实，跑一次完整唤醒，返回（script, 结果, 池子）。

    门卫的开闸信号用 `deferred`（待回看列表增长）：它与"这一格的事实"是两条独立
    的消息源 —— 混用会让"给 skip 摆 confirmed"这种用例因为门卫关闸而测不到路线。
    """
    script, deck = make_lesson()
    state = _stand_at(script, index)
    state.deferred.append("Q-让门卫开闸")
    unit = script.units[index]
    if comprehension:
        state.comprehension[unit.kp[0]] = comprehension
    if stuck:
        state.q_rounds[unit.unit_id] = stuck
    pool = pool if pool is not None else CandidatePool()
    result = await run_tick(
        state=state, script=script, deck=deck, pool=pool,
        watermark=GateWatermark(), proposer=proposer or TemplateProposer(), now=now,
    )
    assert result.called is True, f"门卫必须开闸，否则测的不是路线：{result.reason}"
    return script, result, pool


def _route_of(pool: CandidatePool, candidate_id: str):
    """取池里的一条路线候选，顺手断言它真的是路线（variant 必须为 None）。"""
    cand = pool.get(candidate_id)
    assert cand is not None, f"{candidate_id} 没进池"
    assert cand.route is not None and cand.variant is None, "路线候选的载荷必须只有 route"
    return cand


def _variant_cand(unit_id: str) -> Candidate:
    """一条"别人造的"变体候选（用来占池子名额）。"""
    return Candidate(
        candidate_id=f"c-{unit_id}-v1", target_unit=unit_id,
        variant=UnitVariant(variant_id="v1", label="换说法", text="换一种讲法。"),
        when={}, source="preset", created_at=0.0, created_at_unit="S01-U01",
        expires_at_seq=99,
    )


# ===========================================================================
# 假模型：唯一允许的"LLM"（零网络）
# ===========================================================================

class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _Completions:
    """替掉 `llm.client.chat.completions.create`：本文件的网络边界。

    记下每次调用的 kwargs，好断言"提示词里真的写了路线的形状"。
    """

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.payload, Exception):
            raise self.payload
        return _Resp(str(self.payload))


class _Chat:
    def __init__(self, completions: _Completions) -> None:
        self.completions = completions


class _Client:
    def __init__(self, completions: _Completions) -> None:
        self.completions = completions
        self.chat = _Chat(completions)


def _llm_proposer(payload: object) -> tuple[LlmProposer, _Completions]:
    """造一个接了假 client 的真实 LlmProposer（settings 走离线夹具）。"""
    proposer = LlmProposer(replace(make_settings(), agent_c_enabled=True))
    completions = _Completions(payload)
    proposer.llm.client = _Client(completions)
    return proposer, completions


class RoutesOnlyProposer:
    """只造路线的 proposer（变体一条都不出）。

    为什么需要它：默认的 TemplateProposer 每次唤醒还造 2 条变体，而池子 CAP=5 ——
    连续几次唤醒之后变体就把名额占满，路线被拒。测"路线之间的时序"时那是噪音：
    要测的是"新路线会不会顶掉旧路线"，不是"变体有没有挤满池子"。
    它走的是与 TemplateProposer 完全相同的规则（plan_route → route_proposal），
    所以除了"不出变体"之外没有任何额外待遇。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, request):
        self.calls += 1
        route = plan_route(request.facts, request.units, fork_node=request.current_seq)
        if route is None:
            return []
        return [route_proposal(route, request.units)]


#: 一份"模型给的路线"（前端 JSON）：结构由它自己给，target_unit 故意写错一格 ——
#: 生产者必须按冻结口径从 fork_node 重算它（见 _to_route_candidate）。
LLM_ROUTE_JSON = (
    '{"proposals": [{"target_unit": "S01-U04", "variant": {},'
    ' "route": {"kind": "skip", "fork_node": 3, "merge_node": 6, "steps": [],'
    ' "est_sec": -12.0, "reason": "这一段已经确认听懂，跳过两格"},'
    ' "when": {"comprehension": {"==": "confirmed"}}, "reason": "假模型造的路线"}]}'
)


# ===========================================================================
# R1 · 三种走向：给定事实 → 结构正确
# ===========================================================================

async def test_skip_route_from_confirmed_facts():
    """skip：理解信号 confirmed 且一次没卡 → 跳过两格汇回，时间代价是**负数**。

    断言的是整条链：事实 → 结构（fork/merge/steps）→ 记账（target/source/when/expires）。
    只断结构会漏掉"target 挂错格"这种错 —— 那时 J 永远选不到它，而池里看起来一切正常。
    """
    script, result, pool = await _tick_at(3, comprehension="confirmed")
    units = script.units
    route_id = derive_route_id("skip", FORK_AT_THIRD, FORK_AT_THIRD + 3)

    assert route_id in result.written, f"skip 路线没进池：{result.written}"
    cand = _route_of(pool, route_id)
    route = cand.route

    # ① 结构：跳过 2 格（fork+1、fork+2），在第 fork+3 格汇回
    assert route.kind == "skip"
    assert (route.fork_node, route.merge_node) == (FORK_AT_THIRD, FORK_AT_THIRD + 3)
    assert route.steps == [], "skip 的 steps 必须是空的（冻结契约）"
    skipped = units[FORK_AT_THIRD + 1: FORK_AT_THIRD + 3]
    assert route.est_sec == -round(sum(u.est_sec for u in skipped), 1)
    assert route.est_sec < 0, "省时间必须是负数（Route 的符号约定）"
    assert route.validate(len(units)) is None

    # ② 记账：target 是岔出格的**下一格**（candidates_for 的既有过滤才生效）
    assert cand.target_unit == units[FORK_AT_THIRD + 1].unit_id
    assert cand.source == "agent_c"
    assert cand.variant is None
    assert cand.when == ROUTE_WHEN["skip"]
    # expires 与变体同一把尺子：目标格序号 + 余量
    assert cand.expires_at_seq == FORK_AT_THIRD + 1 + 2


async def test_revisit_route_from_confused_facts():
    """revisit：这格没听懂 → 回头把前面一格重讲再汇回，时间代价是**正数**、steps 非空。"""
    script, result, pool = await _tick_at(3, comprehension="confused")
    units = script.units
    route_id = derive_route_id("revisit", FORK_AT_THIRD, FORK_AT_THIRD + 1)

    assert route_id in result.written, f"revisit 路线没进池：{result.written}"
    cand = _route_of(pool, route_id)
    route = cand.route

    assert route.kind == "revisit"
    assert (route.fork_node, route.merge_node) == (FORK_AT_THIRD, FORK_AT_THIRD + 1)
    assert [s.unit_id for s in route.steps] == [units[FORK_AT_THIRD - 1].unit_id]
    assert route.est_sec == round(units[FORK_AT_THIRD - 1].est_sec, 1)
    assert route.est_sec > 0, "多花的时间必须是正数（Route 的符号约定）"
    # 步骤只给 unit_id + est_sec：空 text = 用该单元主 text（冻结契约）
    assert route.steps[0].text == "" and route.steps[0].variant_id == ""
    assert route.validate(len(units)) is None

    assert cand.target_unit == units[FORK_AT_THIRD + 1].unit_id
    assert cand.source == "agent_c" and cand.variant is None
    assert cand.when == ROUTE_WHEN["revisit"]


async def test_finish_route_from_stuck_facts_in_the_back_half():
    """finish：后段 + 连续卡壳三次 → 直接跳到课尾，merge 必须等于总格数、时间代价为负。"""
    index = 24                                   # 岔出格 = 当前格 24，刚好在后段（40 × 0.6）
    script, result, pool = await _tick_at(index, stuck=3)
    units = script.units
    fork = index
    assert fork >= len(units) * ROUTE_FINISH_AFTER
    route_id = derive_route_id("finish", fork, len(units))

    assert route_id in result.written, f"finish 路线没进池：{result.written}"
    cand = _route_of(pool, route_id)
    route = cand.route

    assert route.kind == "finish"
    assert route.fork_node == fork
    assert route.merge_node == len(units), "finish 的 merge 必须等于课程总格数"
    assert route.steps == [], "finish 的 steps 必须是空的（冻结契约）"
    left = units[fork + 1:]
    assert route.est_sec == -round(sum(u.est_sec for u in left), 1)
    assert route.est_sec < 0
    assert route.validate(len(units)) is None

    assert cand.target_unit == units[fork + 1].unit_id
    assert cand.source == "agent_c" and cand.variant is None
    assert cand.when == ROUTE_WHEN["finish"]


def test_route_kind_priority_and_degradation():
    """入选规则的优先级与降级：finish > revisit > skip，结构不成立就往下让。

    阈值与"为什么是这个阈值"写在 producer.ROUTE_WHEN / route_kind 的注释里，
    这里只把它钉成可执行的行为：临界值附近的两侧各断言一次。
    """
    script, _ = make_lesson()
    total = len(script.units)

    # finish：连续卡壳 3 次 + 后段（岔出格 = 当前格，24 正好是 40 × 0.6 的界线）。
    assert route_kind(_facts_at(script, 24, stuck=3), fork_node=24, total_nodes=total) == "finish"
    # 同样的信号在前段 → 降级成 revisit（课还长，回头补得起，收尾太早）。
    assert route_kind(_facts_at(script, 3, stuck=3, comprehension="confused"),
                      fork_node=FORK_AT_THIRD, total_nodes=total) == "revisit"
    # 卡了三次但学生一个字没说没懂（comprehension 为空）：既没有回头可补的理由，
    # 也还没到收尾的份上 → 不造路线，变体照造。降级只降到"下一个守卫也成立"为止。
    assert route_kind(_facts_at(script, 3, stuck=3),
                      fork_node=FORK_AT_THIRD, total_nodes=total) == ""

    # revisit：理解信号偏弱（vague/confused），与 P 的 v1「换说法」同一条信号。
    for signal in ("vague", "confused"):
        assert route_kind(_facts_at(script, 3, comprehension=signal),
                          fork_node=FORK_AT_THIRD, total_nodes=total) == "revisit"

    # skip：确认听懂 + 一次没卡。卡过一次（stuck=1）就不再乐观。
    assert route_kind(_facts_at(script, 3, comprehension="confirmed"),
                      fork_node=FORK_AT_THIRD, total_nodes=total) == "skip"
    assert route_kind(_facts_at(script, 3, comprehension="confirmed", stuck=1),
                      fork_node=FORK_AT_THIRD, total_nodes=total) == ""

    # 结构不成立：fork 之后没有汇回点 / 前面没有回头路 / 还在前段 → 一律不造。
    assert route_kind(_facts_at(script, 3, comprehension="confirmed"),
                      fork_node=total - 1, total_nodes=total) == ""
    assert route_kind(_facts_at(script, 3, comprehension="confused"),
                      fork_node=0, total_nodes=total) == ""

    # 什么信号都没有 → 不造路线（只造变体）。
    assert route_kind(_facts_at(script, 3),
                      fork_node=FORK_AT_THIRD, total_nodes=total) == ""
    assert plan_route(_facts_at(script, 3), script.units, fork_node=FORK_AT_THIRD) is None


def test_route_proposal_carries_target_and_guard():
    """提案的形状：target_unit + route + when + reason（复用变体那条通道）。"""
    script, _ = make_lesson()
    route = plan_route(_facts_at(script, 3, comprehension="confirmed"),
                       script.units, fork_node=FORK_AT_THIRD)
    proposal = route_proposal(route, script.units)

    assert proposal["target_unit"] == script.units[FORK_AT_THIRD + 1].unit_id
    assert proposal["route"] == route.to_dict()
    assert proposal["when"] == ROUTE_WHEN["skip"]
    assert proposal["reason"] == route.reason
    assert "variant" not in proposal, "一条提案只给一种载荷（路线不该带 variant 键）"


# ===========================================================================
# R2 · 稳态：幂等键 + 同一时刻只有一条路线
# ===========================================================================

async def test_same_route_is_idempotent_across_wakes():
    """同一事实连醒 3 次：candidate_id 不变、alive 不增长、version 递增（池按 id 覆盖）。

    这是 §7.2 ⑥ 那条纪律在路线上的样子：C 每 60s 醒一次，同一条走向会反复被算出来。
    带上时间戳/序号就每次都是一条新货，5 次之后 CAP=5 被同一条走向填满。
    """
    script, _ = make_lesson()
    pool = CandidatePool()
    written: list[tuple[str, ...]] = []
    for n in range(3):
        _, result, _ = await _tick_at(3, comprehension="confused", pool=pool, now=float(n))
        written.append(result.written)

    route_id = derive_route_id("revisit", FORK_AT_THIRD, FORK_AT_THIRD + 1)
    assert all(route_id in ids for ids in written), written
    assert len({tuple(sorted(ids)) for ids in written}) == 1, "同一事实必须映射到同一批 candidate_id"
    assert pool.get(route_id).version == 3, "覆盖分支由池子数版本：写 3 次 → version=3"
    assert sum(1 for c in pool.items() if c.route is not None and c.status == "alive") == 1


async def test_a_new_route_at_the_same_fork_replaces_the_old_one():
    """同一个边界换走向（revisit → skip）：旧的作废、新的进池 —— 一把岔路一把锁。

    同一个 fork 的两条路线是"这个边界往哪走"的两个答案，同时活着等于让 J 在**一个**
    边界上二选一，而它俩的汇回点互不相认。所以替换只发生在同 fork 内部。
    """
    script, _ = make_lesson()
    pool = CandidatePool()

    _, first, _ = await _tick_at(3, comprehension="confused", pool=pool, now=1.0)
    old_id = derive_route_id("revisit", FORK_AT_THIRD, FORK_AT_THIRD + 1)
    assert old_id in first.written

    # 同一格的学生状态变了（没讲透 → 确认听懂）：同一个 fork 换成 skip，汇回点也变了
    _, second, _ = await _tick_at(3, comprehension="confirmed", pool=pool, now=2.0)
    new_id = derive_route_id("skip", FORK_AT_THIRD, FORK_AT_THIRD + 3)
    assert new_id in second.written

    assert pool.get(old_id).status == "expired", "同 fork 的旧路线必须被顶掉（作废而不是删除）"
    assert pool.get(new_id).status == "alive"
    alive_routes = [c for c in pool.items() if c.route is not None and c.status == "alive"]
    assert len(alive_routes) == 1, f"同一个 fork 只许一条路线 alive：{alive_routes}"


async def test_a_new_route_at_another_fork_keeps_the_old_one():
    """不同 fork 的路线互不作废：C 推进一格造下一条，上一条属于**上一个边界**，仍活着。

    改动前这里是"一条新的把所有旧的都作废" —— 于是真实节奏（逐格推进、C 反复唤醒）
    下每条路线都在自己那一格的边界到来之前被顶掉，学生在边界上永远看不见路线。
    别的 fork 的路线是别的边界的选项，不该被这一格的决定连坐。
    """
    script, _ = make_lesson()
    pool = CandidatePool()

    _, first, _ = await _tick_at(3, comprehension="confused", pool=pool, now=1.0)
    old_id = derive_route_id("revisit", FORK_AT_THIRD, FORK_AT_THIRD + 1)
    assert old_id in first.written

    # 只造路线：默认 proposer 的两条变体会把 CAP=5 占满，那是另一个因素（见 (c)）。
    _, second, _ = await _tick_at(4, comprehension="confused", pool=pool, now=2.0,
                                  proposer=RoutesOnlyProposer())
    new_id = derive_route_id("revisit", FORK_AT_THIRD + 1, FORK_AT_THIRD + 2)
    assert new_id in second.written

    assert pool.get(old_id).status == "alive", "别的 fork 的路线不许被顺手作废"
    assert pool.get(new_id).status == "alive"
    alive_routes = [c for c in pool.items() if c.route is not None and c.status == "alive"]
    assert len(alive_routes) == 2, f"两个边界各一条：{alive_routes}"


async def test_two_routes_in_one_payload_keep_only_the_last():
    """同一批里给两条**同 fork** 的路线：只留最后一条，前一条作废 —— 同 fork 只许一条。

    模型完全可能一次给两条（"跳过前面这几格" + "回头补一格"），而两条都锚在同一个
    边界上。它们同时留在池里，J 在**一个**边界上就要二选一 —— 而两条的汇回点互不
    相认。不同 fork 的路线不受影响（见 test_a_new_route_at_another_fork_keeps_the_old_one）。
    """
    payload = [
        {"target_unit": "S01-U04", "when": ROUTE_WHEN["skip"],
         "route": {"kind": "skip", "fork_node": FORK_AT_THIRD,
                   "merge_node": FORK_AT_THIRD + 3, "steps": [], "est_sec": -11.0,
                   "reason": "跳两格"}},
        {"target_unit": "S01-U04", "when": ROUTE_WHEN["revisit"],
         "route": {"kind": "revisit", "fork_node": FORK_AT_THIRD,
                   "merge_node": FORK_AT_THIRD + 1,
                   "steps": [{"unit_id": "S01-U03"}], "est_sec": 5.2,
                   "reason": "补回头"}},
    ]
    _, result, pool = await _tick_at(3, comprehension="confused",
                                     proposer=_FixedProposer(payload))
    skip_id = derive_route_id("skip", FORK_AT_THIRD, FORK_AT_THIRD + 3)
    revisit_id = derive_route_id("revisit", FORK_AT_THIRD, FORK_AT_THIRD + 1)

    assert set(result.written) == {skip_id, revisit_id}
    assert pool.get(skip_id).status == "expired"
    assert pool.get(revisit_id).status == "alive"
    assert sum(1 for c in pool.items() if c.route is not None and c.status == "alive") == 1


# ===========================================================================
# R3 · 路线与变体同池共存：CAP、先到先得、candidates_for 的过滤
# ===========================================================================

async def test_route_and_variants_share_the_pool():
    """一次唤醒写 2 条变体 + 1 条路线：三种载荷同池，路线的过滤与变体一样按 target/when 走。"""
    script, result, pool = await _tick_at(3, comprehension="confirmed")
    units = script.units
    route_id = derive_route_id("skip", FORK_AT_THIRD, FORK_AT_THIRD + 3)

    assert len(result.written) == 3, f"2 变体 + 1 路线：{result.written}"
    assert pool.alive_count() == 3
    assert sum(1 for c in pool.items() if c.route is not None) == 1

    target = units[FORK_AT_THIRD + 1].unit_id
    hit = _facts_at(script, FORK_AT_THIRD, comprehension="confirmed")
    hit_ids = [c.candidate_id for c in pool.candidates_for(target, hit)]
    assert route_id in hit_ids, "岔出格的下一格必须看得见这条路线"

    miss = _facts_at(script, FORK_AT_THIRD, comprehension="confused")
    assert route_id not in [c.candidate_id for c in pool.candidates_for(target, miss)], \
        "条件不成立时路线必须不匹配（否则 J 会照着旧判断岔路）"

    other = units[FORK_AT_THIRD].unit_id
    assert route_id not in [c.candidate_id for c in pool.candidates_for(other, hit)], \
        "路线只能挂在岔出格之后那一格上"


def test_j_picks_the_route_over_preset_variants_at_the_fork_boundary():
    """岔出格的边界上，路线先于预置变体被选中（agent_c 优先，pool.candidates_for 的既有排序）。

    这条用例回答"路线造出来真的有人选吗"：池子里同时躺着预置 v1/v2（两个条件都命中）
    与这条路线，J 拿到的第一个就是路线 —— 于是状态机真的会在那一格岔出去。
    排序本身是冻结契约（pool.py），这里只钉住"路线凭 source=agent_c 排在最前"这一点。
    """
    script, _ = make_lesson()
    units = script.units
    fork = FORK_AT_THIRD
    target = units[fork + 1]
    target.variants = [{"variant_id": "v1", "label": "换说法", "text": "换句话说，这一格讲的是这一点."},
                       {"variant_id": "v2", "label": "举例子", "text": "举个例子，这一格讲的是这一点."}]
    facts = _facts_at(script, fork, comprehension="confused", stuck=2)

    pool = CandidatePool()
    for cand in preset_candidates(target, seq=fork + 1, now=999.0, target_unit=target.unit_id):
        pool.put(cand)
    route = plan_route(facts, units, fork_node=fork)
    cand = Candidate(
        candidate_id=route.route_id, target_unit=target.unit_id, route=route,
        when=ROUTE_WHEN[route.kind], source="agent_c", created_at=1.0,
        created_at_unit=units[fork].unit_id, expires_at_seq=fork + 3,
    )
    assert pool.put(cand) is True

    order = [c.candidate_id for c in pool.candidates_for(target.unit_id, facts)]
    assert order[0] == route.route_id, f"路线必须排在预置变体之前：{order}"
    assert asyncio.run(RuleDecider().decide(facts, pool.candidates_for(target.unit_id, facts))) \
        == route.route_id


async def test_route_never_squeezes_variants_out_of_the_pool():
    """池子只剩一个名额时：变体先占、路线被拒（先到先得，路线是加餐）。

    顺序反过来就等于让一条路线挤掉一条变体 —— 而变体才是"这一格怎么讲"的常规货。
    这条用例把"优先级"钉成可执行的行为，而不是写在注释里的一句口号。
    """
    script, _ = make_lesson()
    pool = CandidatePool()
    keep = [_variant_cand(u.unit_id) for u in script.units[:4]]
    for cand in keep:
        assert pool.put(cand) is True
    assert pool.alive_count() == CandidatePool.CAP - 1

    _, result, _ = await _tick_at(3, comprehension="confirmed", pool=pool)

    assert len(result.written) == 1, f"只该有一个名额：{result.written}"
    assert [r.payload for r in result.rejected] == ["variant", "route"], \
        f"被拒的先后必须与处理顺序一致（变体先、路线后）：{result.rejected}"
    assert "池子已满" in "；".join(result.rejected[1].reasons)
    assert all(pool.get(c.candidate_id).status == "alive" for c in keep), \
        "既有变体一条都不许被路线挤掉"
    assert pool.alive_count() == CandidatePool.CAP


async def test_pool_full_veto_means_no_route_is_even_produced():
    """池子满了门卫就关闸：一次模型都不调，路线自然也不会被造（§7.3）。"""
    script, deck = make_lesson()
    pool = CandidatePool()
    for unit in script.units[:CandidatePool.CAP]:
        pool.put(_variant_cand(unit.unit_id))
    state = _stand_at(script, 3)
    state.comprehension["K4"] = "confirmed"

    result = await run_tick(state=state, script=script, deck=deck, pool=pool,
                            watermark=GateWatermark(), proposer=TemplateProposer(),
                            now=5.0)

    assert (result.called, result.reason) == (False, "pool_full")
    assert result.written == () and result.rejected == ()


# ===========================================================================
# R4 · 坏路线：丢弃 + 落 metric，不抛异常、不污染既有候选
# ===========================================================================

#: 一份份"结构上就不行的路线"（每条的失败点不同）。越界、空 steps、kind 拼错、
#: 指向不存在的单元、汇回格不在窗口 —— 这些都必须被挡在池子外面。
BAD_ROUTES: tuple[tuple[str, dict], ...] = (
    ("kind 拼错", {"kind": "teleport", "fork_node": 8, "merge_node": 11}),
    ("merge 越过课尾", {"kind": "skip", "fork_node": 38, "merge_node": 45}),
    ("revisit 没给 steps", {"kind": "revisit", "fork_node": 8, "merge_node": 9}),
    ("finish 不落在课尾", {"kind": "finish", "fork_node": 8, "merge_node": 20}),
    ("steps 指向不存在的单元",
     {"kind": "revisit", "fork_node": 8, "merge_node": 9,
      "steps": [{"unit_id": "S99-U99"}]}),
    # 改动前这里卡的是"汇回格不在上货窗口"：路线拿变体的 [+5,+15] 窗口当锚，
    # 于是它永远在给 5 格之后的边界备货（然后在到达之前被下一次唤醒顶掉）。
    # 现在路线必须锚在**当前格**（第 3 格），指向别处一律拒。
    ("岔出格不是当前格", {"kind": "skip", "fork_node": 0, "merge_node": 3}),
)


async def test_bad_routes_are_rejected_without_raising():
    """六种坏路线：一条都不进池、原因逐条落账，且同批的好变体照写（互不影响）。"""
    payload = [{"target_unit": "S01-U09", "route": dict(raw)} for _, raw in BAD_ROUTES]
    proposer = _FixedProposer(payload)
    script, result, pool = await _tick_at(3, comprehension="confirmed", proposer=proposer)

    assert result.ok is True, "坏路线不许把整次唤醒打成失败"
    assert result.written == (), f"一条坏路线都不许进池：{result.written}"
    assert [r.payload for r in result.rejected] == ["route"] * len(BAD_ROUTES)
    reasons = [r.reasons[0] for r in result.rejected]
    assert "路线结构非法" in reasons[0], reasons[0]
    assert "路线越界或结构不合法" in reasons[1], reasons[1]
    assert "revisit 必须给出要重讲的格子" in reasons[2], reasons[2]
    assert "finish 的 merge_node" in reasons[3], reasons[3]
    assert "steps 里的单元不存在" in reasons[4], reasons[4]
    assert "岔出格必须是当前这一格" in reasons[5], reasons[5]
    assert pool.status() == {"alive": 0, "consumed": 0, "expired": 0}


async def test_bad_route_does_not_disturb_a_good_variant():
    """同一批里"坏路线 + 好变体"：变体照写、路线照拒（两套校验互不连坐）。"""
    payload = [
        {"target_unit": "S01-U09",
         "variant": {"variant_id": "c9", "label": "换说法",
                     "text": "换个说法讲这一格。它讲的是同一个知识点。"},
         "when": {}, "reason": "好变体"},
        {"target_unit": "S01-U09",
         "route": {"kind": "skip", "fork_node": 38, "merge_node": 45}},
    ]
    script, result, pool = await _tick_at(3, comprehension="confirmed",
                                          proposer=_FixedProposer(payload))

    assert result.written == ("c-S01-U09-c9",), result.written
    assert [r.payload for r in result.rejected] == ["route"]
    assert pool.alive_count() == 1


class _StubMachine:
    """只长 serve_agent_c 真正读的那五个属性（见 director/assembly.py）。"""

    def __init__(self, script: object, state: ClassroomState, pool: CandidatePool) -> None:
        self.script = script
        self.state = state
        self._finished = asyncio.Event()
        self.metrics = MetricsTracker()
        self.director = Director(pool, RuleDecider())


async def test_rejected_route_lands_in_metrics():
    """被丢的路线必须留一条 metric：图上"没有岔路"与"路线被拒"长得一模一样。

    变体的拒绝只进 TickResult（照旧用 v0，无害）；路线被拒是"这节课不会岔路"，
    属于必须能事后复盘的一类（agent_c_route_rejected）。
    """
    script, _ = make_lesson()
    state = _stand_at(script, 3)
    state.deferred.append("Q-让门卫开闸")
    pool = CandidatePool()
    machine = _StubMachine(script, state, pool)
    bad = [{"route": {"kind": "skip", "fork_node": 38, "merge_node": 45}}]

    def on_tick(_result) -> None:
        machine._finished.set()                 # 醒一次就下课，别让循环真的等 60s

    ticks = await serve_agent_c(machine, proposer=_FixedProposer(bad),
                                interval_s=0.001, on_tick=on_tick)

    assert ticks == 1
    rejected = [e for e in machine.metrics.events if e["kind"] == "agent_c_route_rejected"]
    assert rejected, f"路线被拒必须落 metric：{machine.metrics.events}"
    assert "路线越界或结构不合法" in "；".join(rejected[0]["reasons"])
    assert pool.alive_count() == 0


# ===========================================================================
# R5 · LLM 路径：假模型吐路线 → 同一把尺子体检 → 写池
# ===========================================================================

async def test_llm_route_payload_is_parsed_and_written():
    """真模型形状的路线提案（variant 空对象 + route 结构）能一路走到池子里。

    这条用例盯两件事：① 提示词真的告诉了模型路线怎么写（否则真模型永远给不出结构）；
    ② 解析出来的路线走的是**同一套**校验与记账（target 由 fork 重算，不信任模型）。
    """
    proposer, completions = _llm_proposer(LLM_ROUTE_JSON)
    script, result, pool = await _tick_at(3, comprehension="confirmed", proposer=proposer)

    prompt = completions.calls[0]["messages"][0]["content"]
    assert "fork_node" in prompt and "merge_node" in prompt, "提示词必须写清路线的字段"
    assert "skip / revisit / finish" in prompt, "三种走向必须写进输出契约"
    assert "est_sec 是时间代价" in prompt, "符号约定必须写进提示词（否则 J 比不了划不划算）"

    route_id = derive_route_id("skip", FORK_AT_THIRD, FORK_AT_THIRD + 3)
    assert route_id in result.written, f"模型给的路线没进池：{result.written}"
    cand = _route_of(pool, route_id)
    assert cand.target_unit == script.units[FORK_AT_THIRD + 1].unit_id, \
        "target 必须按冻结口径从 fork 重算，不信任模型写的那个"
    assert cand.when == {"comprehension": {"==": "confirmed"}}


async def test_llm_route_without_when_gets_the_frozen_guard():
    """模型没给 when → 按走向补上守卫，绝不留空（空 when 恒真 = 每个边界都被选中）。"""
    payload = ('{"proposals": [{"target_unit": "S01-U04", "variant": {},'
               ' "route": {"kind": "revisit", "fork_node": 3, "merge_node": 4,'
               ' "steps": [{"unit_id": "S01-U03"}], "est_sec": 5.0, "reason": "补回头"}}]}')
    proposer, _ = _llm_proposer(payload)
    _, result, pool = await _tick_at(3, comprehension="confused", proposer=proposer)

    route_id = derive_route_id("revisit", FORK_AT_THIRD, FORK_AT_THIRD + 1)
    assert route_id in result.written, result.written
    cand = _route_of(pool, route_id)
    assert cand.when == ROUTE_WHEN["revisit"], "空守卫必须被补上（否则恒真）"


def test_empty_or_garbage_model_output_raises_the_same_proposal_error():
    """空 / 坏输出照旧抛 ProposalError（不许吞成空数组）：解析纪律一个字都没松。"""
    for garbage in ("", "   ", "这不是 JSON", '{"proposals": "不是数组"}'):
        with pytest.raises(ProposalError):
            parse_proposals(garbage)


async def test_llm_garbage_produces_no_candidate_and_does_not_raise():
    """模型整段不合格 → 唤醒收敛成 proposer_error，池子干净，异常不出 run_tick。

    LLM 的解析失败有两层兜底（json_mode 重试一次），两次都是垃圾才抛；
    抛出来之后由 run_tick 记成 proposer_error —— 学生无感（§11）。
    """
    proposer, completions = _llm_proposer("模型今天不想输出 JSON")
    script, deck = make_lesson()
    state = _stand_at(script, 3)
    state.deferred.append("Q-让门卫开闸")
    pool = CandidatePool()

    result = await run_tick(state=state, script=script, deck=deck, pool=pool,
                            watermark=GateWatermark(), proposer=proposer, now=9.0)

    assert (result.called, result.ok, result.reason) == (True, False, "proposer_error")
    assert len(completions.calls) == 2, "解析失败照旧降级重试一次（除 json_mode 外逐字一致）"
    assert pool.status() == {"alive": 0, "consumed": 0, "expired": 0}


class _FixedProposer:
    """原样交出预置 payload 的 proposer（用来精确控制模型说了什么）。"""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0

    async def propose(self, request):
        self.calls += 1
        return self.payload
