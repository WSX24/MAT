"""预置路线（备课期 plan.json 的 routes）进池、在单元边界被选中、由状态机执行。

这一棒要证明的是一条**接线**：script.routes 里的路线不再只是数据，它在"岔出格 == 当前
正在讲的格"那个边界上被装进**同一个**候选池（source="preset"），被 J 按 when 选中，
最后由既有的 machine._take_route 执行。逐条对应的验收（编号沿用任务书）：

  a) fork=K 的路线在第 K 格边界装载后，candidates_for(第 K+1 格) 看得见它，
     且 source=="preset"、when 非空（when 必须来自 ROUTE_WHEN 那张唯一的表）
  b) 窗口纪律：fork≠当前格的路线在别的边界一条都装不上（不抢跑、不白占 CAP=5），
     装上的那条在**别的目标格**上也看不见（candidates_for 按 target_unit 筛）
  c) 端到端：真 Director + RuleDecider + 状态机 —— 预置 skip 路线被选中后中间那几格
     **一句都没念**、下一句落在 merge 目标；预置 revisit 路线重讲 steps 后显式汇回；
     同一份数据跑两节课，行为逐字一致（幂等）
  d) 池满时预置路线不挤掉当期变体（既有的驱逐纪律原样生效，不许为预置开洞）
  e) 旧 plan（script.routes == []）时装载是彻底的空转，课堂一句不多一句不少

为什么这些断言必须走真实的 machine 调度循环：能装进池 ≠ 课堂上真的走了这条路线。
中间隔着一串只存在于状态机里的判断 —— 装载点（必须排在 prefetch 之前）、事实来源
（facts 取自岔出格）与候选目标的分离（货写给下一格）、边界上的位置校验、以及唯一的
推进出口 _advance → _take_route。§验收 2(c) 全部落在这条缝里，所以 c) 那一组一律走
next_speech → speak → on_speech_end 的真实链路，只把 LLM 与音频换成确定性替身。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.producer import EXPIRE_MARGIN_UNITS, ROUTE_WHEN  # noqa: E402
from director.facts import DecisionFacts  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.presets import load_route_presets, route_candidates, route_when  # noqa: E402
from director.route import Route, RouteStep  # noqa: E402
from llm import split_sentences  # noqa: E402
from script import build_script  # noqa: E402
from tests.test_variant_e2e import (  # noqa: E402
    Classroom,
    make_director,
    make_variant_script,
    seed_confusion,
)

#: 夹具脚本：6 格的真实 rule 计划（格号 0..5 = S01-U01 / S02-U01..U04 / S03-U01）。
#: 均不带变体 —— 就地变体已停产，池子里只该有预置路线这一种预置货。
NODES = ("S01-U01", "S02-U01", "S02-U02", "S02-U03", "S02-U04", "S03-U01")


def base_script():
    """6 格、无变体的真实脚本。状态机、翻页缝、页尾探针都按生产口径走。"""
    return make_variant_script(with_variants=False)


def skip_route(fork: int = 1, merge: int = 3) -> Route:
    """预置 skip：从第 fork 格岔出、在第 merge 格汇回（中间几格一个字都不讲）。"""
    return Route(route_id=f"p-skip-{fork}-{max(0, merge - fork)}", kind="skip",
                 fork_node=fork, merge_node=merge, steps=[], est_sec=-9.0,
                 reason="中间两格是同一道例题的重复")


def revisit_route(fork: int = 1, merge: int = 2) -> Route:
    """预置 revisit：把第 fork 格本身重讲一遍，然后汇回第 merge 格。"""
    units = base_script().units
    return Route(route_id=f"p-revisit-{fork}", kind="revisit", fork_node=fork,
                 merge_node=merge, steps=[RouteStep(units[fork].unit_id)],
                 est_sec=8.0, reason="这一格没讲透，补回头")


def confirmed(script, node: int) -> DecisionFacts:
    """第 node 格"学生听懂了"那一刻的事实（skip 的触发条件）。"""
    return DecisionFacts(unit_id=script.units[node].unit_id, slide=script.units[node].slide,
                         comprehension="confirmed")


def route_events(classroom, kind: str) -> list[dict]:
    return [e for e in classroom.metrics.events if e["kind"] == kind]


def unit_visits(classroom) -> list[str]:
    """课堂上每一次"开讲"落在哪一格（按播出顺序）。

    逐句播出，所以一格会产出好几条 seg —— 取 progress == 1 的那一条就是一次开讲：
    正常推进一格一次，补回头重讲则同一格**再开一次**（两次都留着，这正是要看见的差别）。
    """
    return [s["unit_id"] for s in classroom.segs
            if s["kind"] == "unit" and s["progress"] == 1]


# ===========================================================================
# a) 装载：fork=K 的路线在第 K 格边界进池，货上写着第 K+1 格
# ===========================================================================

def test_fork_route_is_loaded_at_its_own_boundary_with_a_real_target():
    """第 K 格边界装载 → candidates_for(第 K+1 格) 看得见它，且 source/when/expires 都对。

    三个字段各有各的坑：
      · target_unit 必须是**第 K+1 格的真实 unit_id**（空串 = "任意单元"，会让 J 把一条
        本给别的格备的路线从这个边界岔出去）；
      · when 必须非空、且就是 ROUTE_WHEN 那一档 —— 空 when 恒真，等于每个边界都被选中；
      · expires 与运行期路线同一把尺子（fork + 1 + EXPIRE_MARGIN_UNITS），
        两处各写一份就会有一个先过期，而账上只表现为"这条再也没被选中过"。
    """
    script = base_script()
    units = script.units
    route = skip_route(1, 3)
    script.routes = [route]
    pool = CandidatePool()

    assert load_route_presets(pool, script.routes, fork_node=1, fork_unit=units[1].unit_id,
                              target_unit=units[2].unit_id, now=1.0) == 1
    cand = pool.get(route.route_id)
    assert cand is not None, "第 K 格边界必须把 fork=K 的路线装进池子"
    assert cand.source == "preset", f"预置货的来源必须是 preset：{cand.source!r}"
    assert cand.route is route, "装进去的就是脚本上那条路线本身（结构已经一次给全）"
    assert cand.variant is None, "两种载荷互斥：路线候选不许同时挂着变体"
    assert cand.target_unit == units[2].unit_id, "货写给下一格，不是留给任意单元"
    assert cand.created_at_unit == units[1].unit_id, "记账的岔出格就是当前这一格"
    assert cand.expires_at_seq == 1 + 1 + EXPIRE_MARGIN_UNITS, \
        f"过期口径必须与运行期路线一致：{cand.expires_at_seq}"
    assert cand.when, "when 不许为空（空条件恒真 = 每个边界都被选中）"
    assert cand.when == ROUTE_WHEN["skip"], f"when 必须取那张唯一的表：{cand.when}"

    facts = confirmed(script, 1)
    assert [c.candidate_id for c in pool.candidates_for(units[2].unit_id, facts)] \
        == [route.route_id], "第 K+1 格边界必须看得见它（J 的输入就是这把过滤）"
    # 目标不是它的格：看不见（否则同一节课会在好几个边界上被同一条路线岔开）
    assert pool.candidates_for(units[3].unit_id, facts) == []


def test_route_when_comes_from_the_plan_or_the_shared_table():
    """when 的两档来源：plan 侧透传（Route 的附加属性）优先，否则取 ROUTE_WHEN 那张表。

    为什么不给 Route 加一个 when 字段：Route 的字段是被建池、状态机、渲染三处对齐的
    冻结契约，为一个"只在装载时读一次"的条件动它，等于让每个读者都多一个可能为空的
    字段。走附加属性则两边都不欠：plan 侧挂上就用，没挂就取那张表 —— 而那张表正是
    plan.json 里每一条 when 的来源（agent/routes.py 的 when_for，tests/test_plan_routes.py
    钉住两边相等），所以今天两条路给出的是同一份条件。
    """
    script = base_script()
    route = skip_route(1, 3)
    assert route_when(route) == ROUTE_WHEN["skip"], "没挂 when 时取那张唯一的表"

    route.when = {"comprehension": {"in": ["vague", "confused"]}}
    got = route_when(route)
    assert got == {"comprehension": {"in": ["vague", "confused"]}}, "plan 侧给了 when 就用它"
    got["comprehension"]["in"].append("污染")
    assert route.when["comprehension"]["in"] == ["vague", "confused"], \
        "返回的必须是深拷贝：改它不许污染路线上挂的那份条件"

    class _UnknownKind:
        """Route.__post_init__ 会拦掉非法 kind，所以"认不出来"只能这样造。"""

        route_id = "p-teleport-1"
        kind = "teleport"
        fork_node = 1

    assert route_when(_UnknownKind()) == {}, "认不出来的走向没有条件可查"
    assert route_candidates([_UnknownKind()], fork_node=1, fork_unit=script.units[1].unit_id,
                            target_unit=script.units[2].unit_id, now=1.0) == [], \
        "认不出来就整条不装 —— 装一条空条件的路线等于每个边界都乱岔"


# ===========================================================================
# b) 窗口纪律：只有"岔出格 == 当前格"的边界装得进东西
# ===========================================================================

def test_window_only_the_fork_boundary_can_load():
    """别的边界一条都不许装：fork 还没到的装不上，fork 已经过去的也不补装。

    后一半是"不抢跑"那条：一条岔出格已经过去的路线此刻进池也选不中（J 的候选按
    target_unit 筛，而它们的 target 是已经讲过的格子），占着 CAP=5 的一格只会把当期
    刚需挤走 —— 而账上只会表现为"这个边界什么都没得选"。
    """
    script = base_script()
    units = script.units
    script.routes = [skip_route(1, 3), revisit_route(4, 5)]

    for wrong in (0, 2, 3, 5):
        pool = CandidatePool()
        assert load_route_presets(pool, script.routes, fork_node=wrong,
                                  fork_unit=units[wrong].unit_id,
                                  target_unit=units[min(wrong + 1, 5)].unit_id,
                                  now=1.0) == 0, f"第 {wrong} 格不是任何一条路线的岔出格"
        assert pool.items() == [], f"窗口外的边界一条都不许落池：{[c.candidate_id for c in pool.items()]}"

    pool = CandidatePool()
    assert load_route_presets(pool, script.routes, fork_node=4, fork_unit=units[4].unit_id,
                              target_unit=units[5].unit_id, now=1.0) == 1
    assert [c.candidate_id for c in pool.items()] == ["p-revisit-4"], \
        "到达岔出格时只装这一条（其余格子的路线此边界无关）"


def test_reloading_the_same_boundary_tops_up_instead_of_flooding():
    """同一个边界反复装载：命中同一个 candidate_id、version+1，池子不涨（补齐不是清空重灌）。

    为什么这条重要：_prefetch_decision 有三个发出点（单元第一句 / 重讲链起步 / 归位），
    归位那一次是真实课堂最常走的。若装载每次造一条新 id，CAP=5 会被同一条路线占满，
    J 再也没有第二个选项 —— 而账上只表现为"这节课只走了一条岔路"。
    """
    script = base_script()
    units = script.units
    pool = CandidatePool()
    # 池里已经有当期的一份预置变体货（旧 plan 会走到这条路径）：装载必须"补齐"，不许清空
    variant = Candidate(
        candidate_id="c-S02-U01-v1", target_unit=units[1].unit_id,
        variant=UnitVariant(variant_id="v1", label="换说法", text="换句话说，这里先算 Δ。"),
        when={"comprehension": {"in": ["vague", "confused"]}},
        source="preset", created_at=1.0, created_at_unit=units[1].unit_id,
        expires_at_seq=2,
    )
    assert pool.put(variant)

    load = lambda now: load_route_presets(  # noqa: E731
        pool, [skip_route(1, 3)], fork_node=1, fork_unit=units[1].unit_id,
        target_unit=units[2].unit_id, now=now)

    assert load(1.0) == 1
    first = pool.get("p-skip-1-2")
    assert first.version == 1 and first.created_at == 1.0
    alive = pool.alive_count()

    assert load(2.0) == 1, "重复装载必须幂等（命中同一个 id）"
    assert pool.alive_count() == alive, "重复装载不许把池子堆大"
    again = pool.get("p-skip-1-2")
    assert again.version == 2, "同 id 重写由池维护版本号"
    assert again.created_at == 2.0, "这一次装载刷新了 created_at（排新近用得到）"
    assert pool.get("c-S02-U01-v1") is not None, "装载是补齐：池里原有的货一条都不许丢"
    assert pool.get("c-S02-U01-v1").status == "alive"


# ===========================================================================
# d) 池满：预置路线走池子既有的那把尺子，不挤当期变体
# ===========================================================================

def test_preset_route_never_displaces_current_period_variants():
    """池满时只挤得掉**更远的未来备货**；全是当期货时路线自己装不进来（返回 0）。

    这条是"不许为预置另开通道"的可执行版：判据只有 expires 的单调性（pool._eviction_victim），
    预置路线与运行期路线、与变体共用同一把尺子。为预置加一句"路线优先"就等于有了第二份
    容量纪律，而两份纪律迟早只修一份 —— 表现是当期刚需在某个边界上莫名其妙地不见了。
    """
    script = base_script()
    units = script.units
    route_expires = 1 + 1 + EXPIRE_MARGIN_UNITS          # fork=1 的预置路线

    # ① 三条当期变体（expires = 本边界）+ 两条远期备货（expires = 更远）→ 只挤掉远期那条
    pool = CandidatePool()
    for i, uid in enumerate(("c-a", "c-b", "c-c")):
        assert pool.put(Candidate(
            candidate_id=uid, target_unit=units[2].unit_id,
            variant=UnitVariant(variant_id=uid, label=f"当期{i}", text="当期货。"),
            when={"comprehension": {"in": ["vague", "confused"]}}, source="preset",
            created_at=1.0 + i, created_at_unit=units[1].unit_id, expires_at_seq=2))
    for j, uid in enumerate(("c-far-1", "c-far-2")):
        assert pool.put(Candidate(
            candidate_id=uid, target_unit=units[5].unit_id,
            variant=UnitVariant(variant_id=uid, label=f"远期{j}", text="远期货。"),
            when={"comprehension": {"in": ["vague"]}}, source="agent_c",
            created_at=2.0 + j, created_at_unit=units[1].unit_id, expires_at_seq=9))
    assert load_route_presets(pool, [skip_route(1, 3)], fork_node=1,
                              fork_unit=units[1].unit_id, target_unit=units[2].unit_id,
                              now=3.0) == 1, "池满不该让预置路线一律饿死"
    assert pool.alive_count() == CandidatePool.CAP, "驱逐只换来一条更当期的货，上限不变"
    for uid in ("c-a", "c-b", "c-c"):
        assert pool.get(uid).status == "alive", f"当期变体 {uid} 被挤掉了（纪律被破坏）"
    assert pool.get("p-skip-1-2").status == "alive"
    assert [e.candidate_id for e in pool.evictions()] == ["c-far-1"], \
        f"被挤掉的必须是 expires 更远的备货：{[e.to_dict() for e in pool.evictions()]}"
    assert pool.evictions()[0].evicted_by_expires == route_expires

    # ② 池里全是当期货 → 路线自己装不进来（返回 0），五条一条不少
    pool2 = CandidatePool()
    for i in range(5):
        assert pool2.put(Candidate(
            candidate_id=f"c-now-{i}", target_unit=units[2].unit_id,
            variant=UnitVariant(variant_id=f"n{i}", label=f"当期{i}", text="当期货。"),
            when={"comprehension": {"in": ["vague", "confused"]}}, source="preset",
            created_at=1.0 + i, created_at_unit=units[1].unit_id, expires_at_seq=2))
    assert load_route_presets(pool2, [skip_route(1, 3)], fork_node=1,
                              fork_unit=units[1].unit_id, target_unit=units[2].unit_id,
                              now=9.0) == 0, "当期货更靠前：按纪律不许动它们，路线这一格就不装"
    assert pool2.alive_count() == 5 and pool2.eviction_count() == 0
    assert all(pool2.get(f"c-now-{i}").status == "alive" for i in range(5))


# ===========================================================================
# e) 旧 plan（routes == []）：装载是彻底的空转
# ===========================================================================

def test_empty_routes_loader_is_a_noop():
    """script.routes == [] 时装载一条都不写、一次驱逐都不记（"装载一次空转"）。

    这是零回归的判据：旧 plan.json（没有 routes 键）、手写演示脚本、规则兜底脚本全都走到
    这里，而它们必须与改动前**逐字**一致 —— 池子里多一条不存在的候选，J 的选择就会变。
    """
    pool = CandidatePool()
    assert load_route_presets(pool, [], fork_node=1, fork_unit="S02-U01",
                              target_unit="S02-U02", now=1.0) == 0
    assert pool.items() == [] and pool.alive_count() == 0 and pool.eviction_count() == 0

    assert build_script().routes == [], "手写演示课件没有预置分支（这里就是'没有 = 空列表'）"

    # 池里已有的货也不许被动过（空转不是"顺便扫一遍"）
    keep = Candidate(
        candidate_id="c-S02-U01-v1", target_unit="S02-U01",
        variant=UnitVariant(variant_id="v1", label="换说法", text="换句话说。"),
        when={"comprehension": {"in": ["vague"]}}, source="preset", created_at=1.0,
        created_at_unit="S02-U01", expires_at_seq=99)
    assert pool.put(keep)
    assert load_route_presets(pool, [], fork_node=1, fork_unit="S02-U01",
                              target_unit="S02-U02", now=5.0) == 0
    assert pool.alive_count() == 1 and pool.get("c-S02-U01-v1").status == "alive"
    assert pool.get("c-S02-U01-v1").version == 1, "空转连版本号都不该动"


async def test_classroom_without_routes_speaks_every_unit_exactly_once():
    """旧 plan 的课堂行为不变：六格按序各讲一遍，台账里没有任何路线事件。"""
    script = base_script()
    assert script.routes == []
    director = make_director()
    classroom = Classroom(script, director=director)
    await classroom.run()

    assert unit_visits(classroom) == list(NODES), "routes==[] 时一格都不许跳、也不许重讲"
    assert classroom.state.route_log == [], "没有预置路线，就不该有路线台账"
    for kind in ("route_routed", "route_applied", "route_refused", "route_error", "route_merged"):
        assert route_events(classroom, kind) == [], f"routes==[] 时不该有 {kind} 事件"
    assert director.stats["applied"] == 0, f"池子里没有货，J 一次都不该选中：{director.stats}"
    assert classroom.machine._finished.is_set()
    assert classroom.state.slide_index == script.slide_count - 1


# ===========================================================================
# c) 端到端：预置 skip 被选中 → 中间几格一句没念 → 落在 merge 目标
# ===========================================================================

async def test_preset_skip_route_is_selected_and_executed():
    """fork=1（S02-U01）的预置 skip 在它的边界上被选中，第 2 格一个字都没念。

    这条用例断言的是一条完整的链：装载（source=preset）→ 选中（route_routed）→
    执行（route_applied + state.route_log）→ 播出顺序（跳过第 2 格、下一句落在第 3 格）。
    任何一环断掉它都红：装不进池 → 没有 route_routed；装进了但 J 看不见（装载排在
    prefetch 之后）→ 也没有；选中了但执行器没接（_advance 没调 _take_route）→
    第 2 格照样被念出来。
    """
    script = base_script()
    units = script.units
    route = skip_route(1, 3)
    script.routes = [route]
    director = make_director()
    classroom = Classroom(script, director=director)

    #: 第 1 格第一句播出时池子里的预置货（装载点在 _prefetch_decision，必须早于这里）
    seen: dict = {}

    async def peek(c: Classroom, seg):
        if seg.kind == "unit" and seg.unit_id == units[1].unit_id and "pool" not in seen:
            seen["pool"] = [(x.candidate_id, x.source, bool(x.when)) for x in director.pool.items()]

    classroom.on_seg = peek
    seed_confusion(classroom.state, [units[1].unit_id], comprehension="confirmed")
    await classroom.run()

    assert seen.get("pool") == [("p-skip-1-2", "preset", True)], \
        f"第 1 格开播时池子里该有这条预置路线（source=preset、when 非空）：{seen.get('pool')}"

    # 被跳过的格子：一句都不许念（跨页也不许"顺手"讲）
    skipped = units[2].unit_id
    assert classroom.unit_segs(skipped) == [], f"{skipped} 被跳过了，却播出了 {classroom.unit_segs(skipped)}"
    # 下一句落在 merge 目标上
    assert unit_visits(classroom) == [NODES[0], NODES[1], NODES[3], NODES[4], NODES[5]], \
        f"跳过之后必须从汇回格继续：{unit_visits(classroom)}"

    routed = route_events(classroom, "route_routed")
    applied = route_events(classroom, "route_applied")
    assert [e["route_id"] for e in routed] == ["p-skip-1-2"], f"边界上必须选中这条预置路线：{routed}"
    assert [e["route_id"] for e in applied] == ["p-skip-1-2"], f"选中之后必须真的执行：{applied}"
    assert applied[0]["route_kind"] == "skip"
    assert applied[0]["frm"] == units[1].unit_id and applied[0]["to"] == units[3].unit_id
    assert applied[0]["skipped"] == [skipped]
    assert applied[0]["candidate_id"] == "p-skip-1-2", "执行的是池子里那条货（不是另找一条）"

    log = classroom.state.route_log
    assert len(log) == 1, f"整节课只走了一条路线：{log}"
    assert (log[0]["route_id"], log[0]["kind"], log[0]["from_unit"], log[0]["to_unit"]) \
        == ("p-skip-1-2", "skip", units[1].unit_id, units[3].unit_id)
    assert log[0]["skipped"] == [skipped]
    assert director.pool.get("p-skip-1-2").status == "consumed", \
        "选中即消费：同一条路线不许在两个边界上各走一次"

    assert classroom.machine._finished.is_set() and not route_events(classroom, "route_error")
    print(f"[P-c/skip] 装载 S02-U01 边界 → 选中 p-skip-1-2 → 跳过 {skipped} → 汇回 {units[3].unit_id}；"
          f"播出顺序 {unit_visits(classroom)}")


# ===========================================================================
# c) 端到端：预置 revisit 重讲 steps 之后显式汇回
# ===========================================================================

async def test_preset_revisit_route_replays_steps_then_merges():
    """fork=1 的预置 revisit：把 S02-U01 再讲一遍，讲完显式汇回 merge 目标 S02-U02。

    "重讲"与"跳讲"的区别就在这句断言上：skip 让中间几格一声不响，revisit 让 steps 里的格子
    出声**两遍**。汇回点则必须显式落到 merge_node（_merge_replay），靠自然推进的话落在哪
    取决于页尾探针走哪条路 —— 那正是"补回头"变成看运气的来源。
    """
    script = base_script()
    units = script.units
    route = revisit_route(1, 2)
    script.routes = [route]
    director = make_director()
    classroom = Classroom(script, director=director)
    seed_confusion(classroom.state, [units[1].unit_id], comprehension="confused")
    await classroom.run()

    sents = split_sentences(units[1].text)
    said = [s["text"] for s in classroom.unit_segs(units[1].unit_id)]
    assert said == sents + sents, f"补回头就是照原样再讲一遍（{len(sents)} 句 × 2）：{said}"

    assert unit_visits(classroom) == [NODES[0], NODES[1], NODES[1], NODES[2], NODES[3],
                                      NODES[4], NODES[5]], \
        f"重讲之后必须显式汇回 merge 目标，再照常往下讲：{unit_visits(classroom)}"

    routed = route_events(classroom, "route_routed")
    applied = route_events(classroom, "route_applied")
    merged = route_events(classroom, "route_merged")
    assert [e["route_id"] for e in routed] == ["p-revisit-1"]
    assert [e["route_id"] for e in applied] == ["p-revisit-1"]
    assert applied[0]["route_kind"] == "revisit" and applied[0]["to"] == units[1].unit_id
    assert [e["to"] for e in merged] == [units[2].unit_id], f"汇回点必须是 merge_node：{merged}"
    assert len(classroom.state.route_log) == 1
    assert classroom.state.route_log[0]["steps"] == [units[1].unit_id]
    assert classroom.machine._route_replay is None, "汇回之后重讲状态必须清掉"
    assert classroom.machine._finished.is_set() and not route_events(classroom, "route_error")

    print(f"[P-c/revisit] 装载 S02-U01 边界 → 选中 p-revisit-1 → 重讲 {units[1].unit_id} → "
          f"汇回 {units[2].unit_id}；播出顺序 {unit_visits(classroom)}")


# ===========================================================================
# c) 幂等：同一份数据跑两节课，行为逐字一致
# ===========================================================================

async def test_two_classes_on_the_same_preset_data_behave_identically():
    """同一份 script（含同一条预置路线）跑两节课：台词、路线台账、池子的账全都一致。

    幂等键取 route_id 的意义在这里落地：两节课各自一个池子，装载都从 version 1 起，
    走出来的路线、跳过的格子、汇回的落点一字不差。若 candidate_id 里带上时间戳或序号，
    第二节的池子就会是另一副样子 —— 而"课堂行为能不能复现"正是这条用例要守的东西。
    """
    script = base_script()
    units = script.units
    script.routes = [skip_route(1, 3)]

    runs = []
    for _ in range(2):
        director = make_director()
        classroom = Classroom(script, director=director)
        seed_confusion(classroom.state, [units[1].unit_id], comprehension="confirmed")
        await classroom.run()
        runs.append((classroom, director))

    first, second = runs
    words = lambda c: [(u.speaker, u.text) for u in c.state.transcript]  # noqa: E731
    assert words(first[0]) == words(second[0]), "两节课的台词必须逐字一致"
    assert first[0].state.route_log == second[0].state.route_log, "路线台账必须一致"
    assert unit_visits(first[0]) == unit_visits(second[0])
    for classroom, director in runs:
        cand = director.pool.get("p-skip-1-2")
        assert (cand.status, cand.version) == ("consumed", 1), \
            "每节课的装载都从 version 1 起、选中即消费（幂等键不随课堂状态漂移）"
        assert director.stats["applied"] == 1

    print(f"[P-c/idempotent] 两节课 {len(words(first[0]))} 句逐字一致，"
          f"路线 {first[0].state.route_log[0]['route_id']} 两次都跳过 "
          f"{first[0].state.route_log[0]['skipped']}")
