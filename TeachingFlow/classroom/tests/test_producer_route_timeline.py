"""路线的时间线：路线锚在**当前格**，并且在自己的边界上仍然活着。

为什么单独一个文件
------------------
`test_producer_route.py` 钉的是"一条路线的结构对不对、进池的纪律对不对"；这个文件
钉的是**时间**：C 每推进一格醒一次、每醒一次造一条路线 —— 那些路线会不会在轮到
自己的边界之前被后来的唤醒顶掉。

改动前的实现里每次上货都把所有其它 alive 路线作废（`_retire_route_peers`），而
路线又锚在"上货窗口首格 = 当前 + 5"上，于是真实节奏下每条路线都在自己的边界到来
之前被下一次唤醒顶掉。`prototype/_accept_timeline.py` 实测过三个实验：

    [A] 只在 current=3 醒一次      → 池里有 fork=8 的路线，边界 F=8 上看得见
    [B] 再醒 3 次(4,5,6)           → 它变 expired，边界 F=8 上什么都看不见
    [C] 全程 39 个边界逐格唤醒      → 有路线可选的边界 = 1 / 39

结论是"链路只单测成立，真实课堂上几乎永远选不中"，而这与"根本没有路线"在账上一模
一样。这里把那三个实验固化成用例，钉的是**不变量**（不是好看的数字）：

  (a) 每个 fork 边界都看得见**该边界自己的**路线 —— 无论中途醒过多少次
  (b) 同 fork 的新路线替换旧的；**不同 fork 的路线不被作废**；alive 总数有上界
  (c) 路线不许挤掉变体：池满时路线被拒、既有变体一条不动
  (d) 门卫关闸时不造路线，且既有路线不被无故作废（**不许空手**）

全部离线：零网络、零真实 sleep，位置由 `stand` 摆出来（真实课堂由 machine 推进）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.producer import (  # noqa: E402
    EXPIRE_MARGIN_UNITS,
    MIN_REMAINING_UNITS,
    ROUTE_ALIVE_CAP,
    ROUTE_WHEN,
    GateWatermark,
    TemplateProposer,
    run_tick,
)
from director.facts import facts_from_state  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.route import Route, RouteStep, derive_route_id  # noqa: E402
from state import ClassroomState  # noqa: E402
from tests.test_producer import make_lesson  # noqa: E402
from tests.test_producer_route import RoutesOnlyProposer  # noqa: E402

#: 路线 test 的锚：C 在第 3 格醒 → 岔出格 3、汇回格 6（skip 跳两格）、target 第 4 格。
FORK = 3


# ===========================================================================
# 夹具：把一个边界摆出来（状态 + 唤醒 + "J 在这一格看得见什么"）
# ===========================================================================

def stand(script, index: int, *, comprehension: str = "confirmed",
          gate: bool = True) -> ClassroomState:
    """把课堂摆到第 index 格的开头。

    `gate` 控制门卫的开闸信号：待回看列表增长是五条信号里最独立的一条 —— 拿它开闸
    就不会与"这一格的决策事实"（comprehension / q_rounds）混在一起，于是"给 skip 摆
    confirmed"这类用例不会因为门卫关着而测不到路线。
    """
    state = ClassroomState(course_id=script.course_id)
    unit = script.units[index]
    state.slide_index = unit.slide
    state.seg_index = unit.index_in_slide
    if gate:
        state.deferred.append("Q-让门卫开闸")
    if comprehension:
        state.comprehension[unit.kp[0]] = comprehension
    return state


async def wake(script, deck, pool, index: int, *, proposer=None, now: float | None = None,
               comprehension: str = "confirmed"):
    """在第 index 格上醒一次（每次都是新的水位：与 C 的真实循环一致）。"""
    return await run_tick(
        state=stand(script, index, comprehension=comprehension), script=script, deck=deck,
        pool=pool, watermark=GateWatermark(), proposer=proposer or TemplateProposer(),
        now=100.0 + index if now is None else now,
    )


def visible_route_ids(pool: CandidatePool, script, fork: int) -> list[str]:
    """边界 fork：讲完第 fork 格之后，J 为"接上第 fork+1 格"看得见的路线。"""
    nxt = script.units[fork + 1].unit_id
    facts = facts_from_state(stand(script, fork), script.units[fork])
    return [c.candidate_id for c in pool.candidates_for(nxt, facts) if c.route is not None]


def alive_routes(pool: CandidatePool) -> list[Candidate]:
    return [c for c in pool.items() if c.route is not None and c.status == "alive"]


def _variant(unit_id: str) -> Candidate:
    """一条"别人造的"变体（占用池子名额，用来压出"路线挤不挤得掉变体"）。"""
    return Candidate(
        candidate_id=f"c-{unit_id}-v1", target_unit=unit_id,
        variant=UnitVariant(variant_id="v1", label="换说法", text="换一种讲法。"),
        when={}, source="preset", created_at=0.0, created_at_unit=unit_id,
        expires_at_seq=99,
    )


# ===========================================================================
# 锚：路线是这个边界的选项（不是给 5 格后备货）
# ===========================================================================

async def test_route_is_anchored_on_the_current_unit_not_the_window_start():
    """fork = 当前格、target = 下一格；expires 按绝对序号算，边界 3 上就看得见。

    改动前 fork = 当前 + EARLIEST_OFFSET(5)：路线永远在给 5 格之后的边界备货，
    而 when 拿的是**现在**这一格的学生状态去赌 5 格以后 —— 两件事都错在同一个地方。
    """
    script, deck = make_lesson()
    pool = CandidatePool()

    result = await wake(script, deck, pool, FORK, proposer=RoutesOnlyProposer())
    route_id = derive_route_id("skip", FORK, FORK + 3)

    assert result.called is True, result.reason
    assert result.written == (route_id,), f"只该写这一条路线：{result.written}"
    cand = pool.get(route_id)
    assert (cand.route.fork_node, cand.route.merge_node) == (FORK, FORK + 3)
    assert cand.target_unit == script.units[FORK + 1].unit_id, \
        "target 必须是当前格的下一格（否则 candidates_for 的既有过滤看不见它）"
    assert cand.expires_at_seq == FORK + 1 + EXPIRE_MARGIN_UNITS, \
        "过期用绝对序号（路线的 target 已经不在变体的上货窗口里）"

    assert visible_route_ids(pool, script, FORK) == [route_id], "自己的边界上必须看得见"
    assert visible_route_ids(pool, script, FORK + 5) == [], \
        "5 格之后那个边界不该看见它（路线的锚是当前格，不是窗口首格）"


# ===========================================================================
# (a) 每个 fork 边界都看得见该边界自己的路线
# ===========================================================================

async def test_every_fork_boundary_keeps_its_own_route_after_many_wakes():
    """[A]+[B]：连续在相邻几格唤醒之后，**每个**边界都还看得见自己那条路线。

    改动前：第 2 次唤醒就把第 1 条作废 —— 第 1 个边界就此空手，而账上只剩一条
    "expired"（与"根本没造"长得一模一样）。
    """
    script, deck = make_lesson()
    pool = CandidatePool()

    for cur in range(ROUTE_ALIVE_CAP):                     # 三个相邻边界
        result = await wake(script, deck, pool, cur, proposer=RoutesOnlyProposer())
        assert result.called is True, result.reason

    for fork in range(ROUTE_ALIVE_CAP):
        ids = visible_route_ids(pool, script, fork)
        assert ids == [derive_route_id("skip", fork, fork + 3)], \
            f"边界 {fork} 必须看见它自己的路线，池内现状：" \
            f"{[(c.candidate_id, c.status) for c in pool.items() if c.route is not None]}"
        assert pool.get(ids[0]).status == "alive"


async def test_walking_the_whole_lesson_never_loses_a_route_at_its_own_boundary():
    """[C]：逐格推进 39 个边界，**门卫开着的每个边界**都要看得见本格造出的路线。

    这是 `_accept_timeline.py` 第三个实验的离线等价物：改动前是 1/39。这里用
    routes-only proposer 把"变体占满池子"这个因素隔离掉（那是门卫的既有纪律，
    不是路线的时间线问题）；门卫开着却空手的边界数必须为 0 —— "不许因为前一条
    路线被顶掉而空手"（任务书第 4 条）。
    """
    script, deck = make_lesson()
    total = len(script.units)
    pool = CandidatePool()

    opened = seen = 0
    missed_at = []
    for cur in range(total - 1):
        result = await wake(script, deck, pool, cur, proposer=RoutesOnlyProposer())
        if not result.called:
            continue                                  # 门卫关（课末停止期）：允许没有路线
        opened += 1
        ids = visible_route_ids(pool, script, cur)
        if ids:
            seen += 1
            assert ids == [derive_route_id("skip", cur, cur + 3)], \
                f"边界 {cur} 看见的必须是它自己的路线：{ids}"
        else:
            missed_at.append((cur, result.reason, result.rejected))

    assert missed_at == [], f"门卫开了却没路线 = 空手：{missed_at}"
    assert opened == total - MIN_REMAINING_UNITS, \
        f"门卫在最后 {MIN_REMAINING_UNITS} 格关闭，其余每格都开：{opened}"
    assert seen == opened, f"每个开着闸的边界都必须有路线：{seen}/{opened}"


# ===========================================================================
# (a') 默认配置下的真实节奏：有路线可选的边界数
# ===========================================================================

async def test_default_proposer_keeps_a_route_visible_on_at_least_half_the_boundaries():
    """默认 TemplateProposer（2 变体 + 1 路线）逐格推进 39 个边界：路线还看得见吗。

    改动前实测 **2 / 39**（`prototype/_accept_timeline.py` 的 [C]，本用例复现到 1~2/39）：
    C 每次唤醒给未来 5~15 格各备 2 条货（expires 是目标格 + 2，要约 7 格才过期），
    三次唤醒就把 CAP=5 占满，于是**这个边界的路线那条永远被拒** —— 而账上只表现为
    "这次没有岔路"，与"根本没有路线"长得一模一样。

    修复（当期刚需挤掉最远的未来备货）之后钉的是三条不变量，不是好看的数字：
      · **造出来的路线在自己边界上必须看得见**（空手 = 0，任务书第 3 条）；
      · 有路线可选的边界至少**一半**（改动前连 10% 都不到）；
      · 抬升必须来自驱逐这条路（一次驱逐都没发生就说明是别的巧合）。
    """
    script, deck = make_lesson()
    total = len(script.units)
    pool = CandidatePool()

    opened = seen = written_routes = 0
    invisible = []
    for cur in range(total - 1):
        result = await wake(script, deck, pool, cur)
        if not result.called:
            continue                       # 门卫关（停止期 / 池满且挤不动）：允许没有路线
        opened += 1
        ids = visible_route_ids(pool, script, cur)
        if ids:
            seen += 1
        mine = [w for w in result.written if w.startswith("r-")]
        written_routes += len(mine)
        if mine and not set(mine) & set(ids):
            invisible.append((cur, mine, ids, result.reason, result.rejected))

    assert invisible == [], f"造出来的路线在自己边界上必须看得见：{invisible}"
    assert seen * 2 >= total - 1, \
        f"有路线可选的边界至少要一半：{seen}/{total - 1}（门卫开了 {opened} 次）"
    assert seen > 2, \
        f"改动前是 2/39（只在最早的几个边界上碰巧成立），修复后必须显著抬升：{seen}/{total - 1}"
    assert pool.eviction_count() > 0, "抬升必须来自驱逐：一次都没驱逐就说明机制没被走到"
    print(f"[D] 默认 proposer：{seen}/{total - 1} 个边界能选到路线"
          f"（门卫开了 {opened} 次，路线写了 {written_routes} 条，"
          f"驱逐 {pool.eviction_count()} 条）")


# ===========================================================================
# (b) 一把岔路一把锁 + alive 总数有界
# ===========================================================================

async def test_same_fork_replaces_but_other_forks_are_never_retired():
    """同 fork 换走向 → 旧的 expired（新的接替它）；不同 fork 的那条一条都不动。

    这是**一个**边界的两个答案 vs **两个**边界的各自答案的区别：前者同时活着会让
    J 在同一个边界上二选一，后者互不相干。
    """
    script, deck = make_lesson()
    pool = CandidatePool()

    # 边界 3 先造一条 revisit（学生"没讲透"），边界 4 再造一条 skip
    await wake(script, deck, pool, 3, proposer=RoutesOnlyProposer(), comprehension="confused")
    await wake(script, deck, pool, 4, proposer=RoutesOnlyProposer())
    revisit_id = derive_route_id("revisit", 3, 4)
    neighbour_id = derive_route_id("skip", 4, 7)
    assert (pool.get(revisit_id).status, pool.get(neighbour_id).status) == ("alive", "alive")

    # 边界 3 的学生状态变了（现在确认听懂）→ 同一个 fork 换成 skip
    second = await wake(script, deck, pool, 3, proposer=RoutesOnlyProposer())
    skip_id = derive_route_id("skip", 3, 6)
    assert second.written == (skip_id,), second.written

    assert pool.get(revisit_id).status == "expired", "同 fork 的旧路线必须被顶掉"
    assert pool.get(skip_id).status == "alive"
    assert pool.get(neighbour_id).status == "alive", "别的 fork 的路线不许被顺手作废"
    assert len(alive_routes(pool)) == 2, [(c.candidate_id, c.status) for c in alive_routes(pool)]


async def test_alive_routes_are_bounded_and_the_oldest_is_dropped():
    """alive 路线总数 ≤ ROUTE_ALIVE_CAP；超了挤掉岔出点最早的那一条，且留痕。

    上界的理由见 producer.ROUTE_ALIVE_CAP：一条路线只对一个边界有意义，池子 CAP=5
    里还要留两个名额给变体。挤掉最旧的而不是拒收新的 —— 拒收会让**这一格**空手，
    而被挤掉的那条越过的边界最多，是全部路线里最不可能再被选中的一条。
    """
    script, deck = make_lesson()
    pool = CandidatePool()

    for cur in range(ROUTE_ALIVE_CAP + 2):
        await wake(script, deck, pool, cur, proposer=RoutesOnlyProposer())
        assert len(alive_routes(pool)) <= ROUTE_ALIVE_CAP, \
            f"alive 路线不许随唤醒次数增长：{[(c.candidate_id) for c in alive_routes(pool)]}"

    forks = sorted(c.route.fork_node for c in alive_routes(pool))
    assert forks == list(range(2, ROUTE_ALIVE_CAP + 2)), f"只留最新的几条：{forks}"
    assert pool.get(derive_route_id("skip", 0, 3)).status == "expired", \
        "被挤掉的是最旧那条，且作废而不是删除（账上要留痕）"
    # 最新那条在自己的边界上仍然看得见
    assert visible_route_ids(pool, script, ROUTE_ALIVE_CAP + 1)


# ===========================================================================
# (c) 路线不许挤掉变体
# ===========================================================================

async def test_a_full_pool_of_variants_rejects_the_route_and_keeps_them_all():
    """池子塞满变体时：路线被拒（先到先得），四条变体一条不动、一条不作废。

    "先腾后写"只腾**同 fork 的旧路线**；变体不在可腾范围里，所以这条用例在改动前后
    都成立 —— 这里钉住它不要被新逻辑弄坏。
    """
    script, deck = make_lesson()
    pool = CandidatePool()
    keep = [_variant(u.unit_id) for u in script.units[:CandidatePool.CAP]]
    for cand in keep:
        assert pool.put(cand) is True

    result = await wake(script, deck, pool, FORK, proposer=RoutesOnlyProposer())

    assert (result.called, result.ok) == (False, True) and result.reason == "pool_full"
    assert result.written == ()
    assert all(pool.get(c.candidate_id).status == "alive" for c in keep), \
        "既有变体一条都不许被路线挤掉"


async def test_replacing_a_route_in_a_full_pool_still_lands_and_leaves_variants_alone():
    """池子在这一次唤醒里被变体填满之后，同 fork 换走向照样进池，变体一条不动。

    场景（真实节奏下最常见的那种）：池里已有 3 条变体 + 1 条老路线（alive 4，门卫
    还开着）→ 这次唤醒先写变体（只挤进 1 条，alive 到 CAP）→ 再写路线。此时若
    "先写后腾"，路线会被 CAP 拒收，而老路线已经在前面被作废了 —— 这个边界就空手了
    （缺陷一的形状）。"先腾后写"让它照样落地。
    """
    script, deck = make_lesson()
    units = script.units
    pool = CandidatePool()

    old = Route(route_id=derive_route_id("revisit", FORK, FORK + 1), kind="revisit",
                fork_node=FORK, merge_node=FORK + 1,
                steps=[RouteStep(unit_id=units[FORK - 1].unit_id)], est_sec=6.0,
                reason="补回头")
    assert pool.put(Candidate(
        candidate_id=old.route_id, target_unit=units[FORK + 1].unit_id, route=old,
        when=ROUTE_WHEN["revisit"], source="agent_c", created_at=1.0,
        created_at_unit=units[FORK].unit_id, expires_at_seq=FORK + 1 + EXPIRE_MARGIN_UNITS,
    )) is True
    keep = [_variant(u.unit_id) for u in units[:CandidatePool.CAP - 2]]
    for cand in keep:
        assert pool.put(cand) is True
    assert pool.alive_count() == CandidatePool.CAP - 1, "门卫还得是开的（alive < CAP）"

    # 这次唤醒：变体在前（挤进一条，池子到 CAP），路线在后（同 fork 换走向）
    result = await wake(script, deck, pool, FORK, proposer=TemplateProposer())
    new_id = derive_route_id("skip", FORK, FORK + 3)

    assert new_id in result.written, f"同 fork 换走向必须真的进池：{result.written}"
    assert pool.get(old.route_id).status == "expired", "老路线作废（腾出的名额给了新路线）"
    assert pool.get(new_id).status == "alive"
    assert all(pool.get(c.candidate_id).status == "alive" for c in keep), \
        "既有变体一条都不许被路线挤掉"
    assert pool.alive_count() == CandidatePool.CAP


# ===========================================================================
# (d) 门卫关闸：不造路线，也不动既有路线
# ===========================================================================

async def test_closed_gate_creates_nothing_and_retires_nothing():
    """没有开闸信号：一次模型都不调，池子一字不差（既有路线不许被无故作废）。

    任务书第 4 条：门卫关着 → 这个边界可以没有路线（J 退回变体或 v0 是正确默认），
    但已经造好的那些路线不许因为"这次没造"就被顺手清掉。
    """
    script, deck = make_lesson()
    pool = CandidatePool()
    await wake(script, deck, pool, FORK, proposer=RoutesOnlyProposer())
    before = [(c.candidate_id, c.status, c.version) for c in pool.items()]

    result = await run_tick(
        state=stand(script, FORK, gate=False), script=script, deck=deck, pool=pool,
        watermark=GateWatermark(), proposer=RoutesOnlyProposer(), now=9.0,
    )

    assert (result.called, result.reason) == (False, "no_signal")
    assert result.written == () and result.rejected == ()
    assert [(c.candidate_id, c.status, c.version) for c in pool.items()] == before, \
        "门卫关着的时候池子必须一字不差"
    assert visible_route_ids(pool, script, FORK) == [derive_route_id("skip", FORK, FORK + 3)], \
        "门卫关着不等于把上一条路线作废 —— 边界上照样看得见它"
