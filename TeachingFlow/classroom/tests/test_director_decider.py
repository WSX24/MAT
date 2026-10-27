"""决策层测试 —— 证明 Director 的五条行为都能在毫秒级、零真实等待下复现。

零阻塞是这套设计里最容易被改坏的一处：谁把"预取"改成 await，课堂就慢一截，
而慢在测试里不会报错，只会变成"感觉有点卡"。所以这里逐条钉住：
已就绪立即应用、未就绪超时回 v0、(unit_id, seq) 变了就作废、异常吞掉、
close() 之后不留悬挂 task，以及应用成功后池里那条候选确实变成 consumed。

时序一律用 asyncio.Event 或 5~20ms 的 grace_ms 控制，不睡真实等待。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import director  # noqa: E402
from director.decider import Director, RuleDecider  # noqa: E402
from director.facts import DecisionFacts  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.route import Route, RouteStep  # noqa: E402
from state import ClassroomState, LectureUnit  # noqa: E402


def _state() -> ClassroomState:
    return ClassroomState()


def _unit(unit_id: str = "S03-U02", slide: int = 2) -> LectureUnit:
    return LectureUnit(unit_id=unit_id, slide=slide, index_in_slide=0, text="……")


def _variant(variant_id: str = "c1", label: str = "更浅的讲法",
             text: str = "用天平打比方：左右两边一样重。") -> UnitVariant:
    return UnitVariant(variant_id=variant_id, label=label, text=text,
                       anchors=[{"element_id": "s03-sh01"}], ops=[{"op": "highlight"}])


def _cand(candidate_id: str, *, when: dict | None = None, source: str = "agent_c",
          created_at: float = 0.0, expires: int = 99, target: str = "",
          variant: UnitVariant | None = None) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        target_unit=target,
        variant=variant or _variant(candidate_id),
        when={} if when is None else when,
        source=source,
        created_at=created_at,
        created_at_unit="S03-U02",
        expires_at_seq=expires,
    )


def _route_cand(candidate_id: str, *, when: dict | None = None, source: str = "agent_c",
                created_at: float = 0.0, expires: int = 99, target: str = "",
                route: Route | None = None) -> Candidate:
    """一条路线候选：与变体候选同池，只是载荷换成了按一次写入给全的 Route。

    route 可换（默认是一条 skip）：三种走向的载荷形状必须都能原样交接，
    只测 skip 的话"steps 里的 RouteStep 能不能过一遍 from_dict"就没人守着。
    """
    return Candidate(
        candidate_id=candidate_id,
        target_unit=target,
        when={} if when is None else when,
        source=source,
        created_at=created_at,
        created_at_unit="S03-U02",
        expires_at_seq=expires,
        route=route or Route(route_id=f"r-{candidate_id}", kind="skip", fork_node=3,
                             merge_node=6, steps=[], est_sec=-42.0,
                             reason="后三格是同一道例题的重复"),
    )


class GateDecider:
    """可控决策器：gate 开 = 立刻给答案，gate 关 = 永远不算完。

    用 Event 而不是 sleep：要断言"超时"就必须能百分百确定"它确实还没算完"。
    """

    def __init__(self, result: str | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.seen: list[tuple] = []

    async def decide(self, facts: DecisionFacts, candidates: list[Candidate]) -> str | None:
        self.seen.append((facts, [c.candidate_id for c in candidates]))
        self.tasks.append(asyncio.current_task())
        self.entered.set()
        if self.error is not None:
            raise self.error
        await self.gate.wait()
        return self.result


def test_package_exports() -> None:
    """契约要求 __init__ 导出这些名字：接线方只 import director 一处。

    路线三件（Route / RouteStep / ROUTE_KINDS）必须一起导出：后面三棒（生成 / 执行 /
    渲染）拿到的必须是同一个类对象，而不是各自从不同路径 import 出来的一份副本 ——
    类型不同、"是不是路线候选"的判断就会在两处各写一遍，然后各自漂移。
    """
    for name in ("DecisionFacts", "matches", "facts_from_state", "UnitVariant", "Candidate",
                 "CandidatePool", "DecisionProvider", "RuleDecider", "Director",
                 "Route", "RouteStep", "ROUTE_KINDS"):
        assert hasattr(director, name), name
    from director.route import ROUTE_KINDS, Route, RouteStep
    assert (director.Route, director.RouteStep, director.ROUTE_KINDS) == \
        (Route, RouteStep, ROUTE_KINDS), "包导出与 director.route 必须是同一个对象"
    print("[D0] director 包导出完整")


def test_ready_decision_is_applied_immediately() -> None:
    """已就绪 → 边界上不等 grace，直接应用，并把候选消费掉。"""
    async def go():
        pool = CandidatePool()
        cand = _cand("c1", variant=_variant("c1", "更浅的讲法", "用天平打比方：两边一样重。"))
        pool.put(cand)
        dec = GateDecider(result="c1")
        dec.gate.set()
        d = Director(pool, dec, grace_ms=5)
        st, unit = _state(), _unit()
        d.prefetch(unit, st)
        assert d.stats["prefetched"] == 1
        await asyncio.sleep(0)                      # 只让预取跑完，不做任何等待
        assert dec.seen[-1][0].unit_id == "S03-U02", "预取要拿本单元的事实"
        assert dec.seen[-1][1] == ["c1"], "预取要拿池子筛过的候选"

        t0 = time.perf_counter()
        applied = await d.apply_at_boundary(unit, st)
        elapsed = time.perf_counter() - t0
        assert applied is not None
        # 契约 v3 多了 "target_unit"：不传 target_unit 时它等于 unit.unit_id（老语义不变）
        # 与当初加 "position" 是同一类改动：加字段就同步这张逐键断言，不留特例。
        assert set(applied) == {"variant_id", "label", "candidate_id", "unit_id",
                                "seq", "position", "text", "anchors", "ops", "est_sec",
                                "target_unit"}
        assert applied["position"] == st.seq
        assert applied["target_unit"] == unit.unit_id, \
            "没传 target_unit 时必须回落到 unit 自己（老路径语义不变）"
        assert applied["candidate_id"] == "c1" and applied["variant_id"] == "c1"
        assert applied["label"] == "更浅的讲法"
        assert (applied["unit_id"], applied["seq"]) == (unit.unit_id, st.seq)
        assert applied["text"] == cand.variant.text
        assert applied["anchors"] == cand.variant.anchors
        assert applied["ops"] == cand.variant.ops
        assert applied["est_sec"] == cand.variant.est_sec > 0
        assert elapsed < 0.05, f"已就绪的决策不该再等 grace，实际 {elapsed:.3f}s"
        assert pool.get("c1").status == "consumed"
        assert pool.just_consumed() is True
        assert d.stats == {"prefetched": 1, "applied": 1, "none": 0,
                           "timeout": 0, "stale": 0, "error": 0}
        # 同一个边界不能消费两次
        assert await d.apply_at_boundary(unit, st) is None
        assert d.stats["none"] == 1
        d.close()

    asyncio.run(go())
    print("[D1] 决策已就绪 → 立即应用（候选转 consumed）")


def test_slow_decision_times_out_to_default() -> None:
    """未就绪 → 最多等 grace_ms，超时回 v0 且不消费候选。"""
    async def go():
        pool = CandidatePool()
        pool.put(_cand("c1"))
        dec = GateDecider(result="c1")              # gate 不开 = 边界上还没算完
        d = Director(pool, dec, grace_ms=10)
        st, unit = _state(), _unit()
        d.prefetch(unit, st)
        await dec.entered.wait()

        t0 = time.perf_counter()
        assert await d.apply_at_boundary(unit, st) is None
        elapsed = time.perf_counter() - t0
        assert 0.008 <= elapsed < 0.5, f"应只等 grace_ms≈10ms，实际 {elapsed:.3f}s"
        assert d.stats["timeout"] == 1 and d.stats["applied"] == 0
        assert pool.get("c1").status == "alive", "超时不该消费候选，下一格还能用"
        d.close()

    asyncio.run(go())
    print("[D2] 未就绪 → 超时回 v0（只等 grace_ms）")


def test_decision_is_discarded_when_position_moved() -> None:
    """(unit_id, seq) 与当前不一致 → 丢弃并计 stale（§6.5）。"""
    async def go():
        pool = CandidatePool()
        pool.put(_cand("c1"))
        dec = GateDecider(result="c1")
        dec.gate.set()
        d = Director(pool, dec, grace_ms=5)
        st, unit = _state(), _unit()
        d.prefetch(unit, st)
        await asyncio.sleep(0)
        st.seq += 1                                  # 学生插话：位置发生了变化
        assert await d.apply_at_boundary(unit, st) is None
        assert d.stats["stale"] == 1 and d.stats["applied"] == 0
        assert pool.get("c1").status == "alive", "作废的决策不能动池子"

        d.prefetch(unit, st)                         # 让位后回到了别的单元
        await asyncio.sleep(0)
        assert await d.apply_at_boundary(_unit("S04-U01"), st) is None
        assert d.stats["stale"] == 2
        d.close()

    asyncio.run(go())
    print("[D3] (unit_id, seq) 变化 → 丢弃计 stale")


def test_decider_exception_is_swallowed() -> None:
    """decider 抛异常 → 吞掉、计 error、回 v0，绝不阻塞课堂。"""
    async def go():
        pool = CandidatePool()
        pool.put(_cand("c1"))
        dec = GateDecider(error=RuntimeError("模型超时"))
        d = Director(pool, dec, grace_ms=20)
        st, unit = _state(), _unit()
        d.prefetch(unit, st)
        await asyncio.sleep(0)
        assert await d.apply_at_boundary(unit, st) is None       # 没有异常逃出来
        assert d.stats["error"] == 1 and d.stats["applied"] == 0
        assert pool.get("c1").status == "alive"
        # 上一格炸了也不影响下一格边界安静地回 v0
        assert await d.apply_at_boundary(unit, st) is None
        assert d.stats["none"] == 1
        d.close()

    asyncio.run(go())
    print("[D4] decider 异常 → 吞掉计 error 回 v0")


def test_no_choice_falls_back_to_default() -> None:
    """decider 返回 None（候选池空 / 它选 none）→ 用 v0。"""
    async def go():
        dec = GateDecider(result=None)
        dec.gate.set()
        d = Director(CandidatePool(), dec, grace_ms=5)
        st, unit = _state(), _unit()
        d.prefetch(unit, st)
        await asyncio.sleep(0)
        assert await d.apply_at_boundary(unit, st) is None
        assert d.stats["none"] == 1 and d.stats["applied"] == 0
        d.close()

    asyncio.run(go())
    print("[D5] 无选择 → 回 v0")


def test_prefetch_overwrite_and_close_leave_no_task() -> None:
    """重复预取安全覆盖旧槽位；close() 之后不留悬挂 task。"""
    async def go():
        pool = CandidatePool()
        pool.put(_cand("c1"))
        dec = GateDecider(result="c1")               # gate 永远不开 → 两个 task 都挂起
        d = Director(pool, dec, grace_ms=5)
        st = _state()
        d.prefetch(_unit("S03-U02"), st)
        await dec.entered.wait()
        first = dec.tasks[0]
        d.prefetch(_unit("S04-U01"), st)             # 覆盖旧槽位
        for _ in range(3):
            await asyncio.sleep(0)
        assert first.cancelled() is True, "被覆盖的那一格不该继续跑"
        assert d.stats["prefetched"] == 2

        d.close()
        for _ in range(5):
            await asyncio.sleep(0)
        assert all(t.done() for t in dec.tasks), dec.tasks
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leftover == [], f"close() 后不应有悬挂 task：{leftover}"

    asyncio.run(go())
    print("[D6] 预取覆盖 + close() 不留悬挂 task")


def test_rule_decider_keeps_pool_order() -> None:
    """纯规则：按给定顺序取第一个命中，不重排（池子已经排过）。"""
    async def go():
        facts = DecisionFacts("S03-U02", 2, consecutive_confused=2)
        ordered = [
            _cand("preset-new", when={"consecutive_confused": {">=": 2}},
                  source="preset", created_at=5.0),
            _cand("agent-c", when={"consecutive_confused": {">=": 2}},
                  source="agent_c", created_at=1.0),
        ]
        rd = RuleDecider()
        assert await rd.decide(facts, ordered) == "preset-new", "不得自行重排"
        assert await rd.decide(facts, []) is None
        assert await rd.decide(facts, [_cand("no", when={"consecutive_confused": {">=": 9}})]) is None

    asyncio.run(go())
    print("[D7] RuleDecider 只按给定顺序取第一个命中")


def test_rule_decider_drives_director_end_to_end() -> None:
    """端到端：状态 → 事实 → 池子筛选 → RuleDecider → 边界应用。"""
    async def go():
        pool = CandidatePool()
        pool.put(_cand("p-only-if-confirmed", source="preset", created_at=5.0,
                       when={"probe_result": {"==": "confirmed"}}))
        pool.put(_cand("p-concrete", source="agent_c", created_at=1.0,
                       when={"consecutive_confused": {">=": 1}}))
        d = Director(pool, RuleDecider(), grace_ms=20)
        st, unit = _state(), _unit()
        st.q_rounds["S03-U02"] = 1                   # 学生已经卡了一轮
        d.prefetch(unit, st)
        await asyncio.sleep(0)
        applied = await d.apply_at_boundary(unit, st)
        assert applied is not None and applied["candidate_id"] == "p-concrete"
        assert pool.get("p-concrete").status == "consumed"
        assert pool.get("p-only-if-confirmed").status == "alive", "没命中的候选不该被动"
        assert d.stats["applied"] == 1
        d.close()

    asyncio.run(go())
    print("[D8] RuleDecider 端到端（条件决定池子筛选结果）")


def test_explicit_position_ignores_rising_seq() -> None:
    """显式位置键：单元播放期间逐句旁白把 state.seq 推高，也不许判 stale。

    这就是契约 v2 要修的那条：老语义拿 state.seq 当位置，而 machine 每吐一句旁白
    都会 emit 一次（_log_utterance → state.emit → seq += 1），于是"单元开始预取、
    单元结束消费"必然不等，每个单元的决策都白白作废。
    """
    async def go():
        pool = CandidatePool()
        pool.put(_cand("c1"))
        dec = GateDecider(result="c1")
        dec.gate.set()
        d = Director(pool, dec, grace_ms=5)
        st, unit = _state(), _unit()
        d.prefetch(unit, st, position="S03-U02#0")
        await asyncio.sleep(0)
        st.seq += 7                              # 模拟本单元说了 7 句旁白
        applied = await d.apply_at_boundary(unit, st, position="S03-U02#0")
        assert applied is not None, "显式位置没变，不该判 stale"
        assert applied["position"] == "S03-U02#0"
        assert applied["seq"] == st.seq, "seq 键仍然给当时的 state.seq（machine 侧还要用）"
        assert d.stats["stale"] == 0 and d.stats["applied"] == 1
        assert pool.get("c1").status == "consumed"
        d.close()

    asyncio.run(go())
    print("[D9] 显式 position：state.seq 上涨不影响应用")


def test_explicit_position_change_invalidates() -> None:
    """显式位置键变了 → 计 stale、回 v0，且不消费候选。"""
    async def go():
        pool = CandidatePool()
        pool.put(_cand("c1"))
        dec = GateDecider(result="c1")
        dec.gate.set()
        d = Director(pool, dec, grace_ms=5)
        st, unit = _state(), _unit()
        d.prefetch(unit, st, position=("S03-U02", 0))
        await asyncio.sleep(0)
        assert await d.apply_at_boundary(unit, st, position=("S03-U02", 1)) is None
        assert d.stats["stale"] == 1 and d.stats["applied"] == 0
        assert pool.get("c1").status == "alive", "作废的决策不能动池子"
        d.close()

    asyncio.run(go())
    print("[D10] 显式 position 变化 → 丢弃计 stale")

def test_route_only_pool_is_selected_then_handed_off() -> None:
    """候选里只有路线时：RuleDecider 照样按 when 命中，边界交出**结构给全**的交接单。

    **契约为什么变了（S3）**：上一棒这里断言的是"返回 None 且候选保持 alive"，
    因为那时没有路线执行器 —— 消费掉一条没人执行的路线等于把它静默销毁，观测面上
    只显示成"播过了"。现在状态机真的会按它走，于是"交出去"**就是**"用在课堂上了"：
    不消费反而会让同一条路线在每个边界上被反复选中、反复执行（revisit 会回头重讲，
    重复执行直接吃课堂时间）。所以这条用例改写为钉住三件事：
      · 交接单里 Route 的字段一个不少 —— 状态机能 Route.from_dict 原样还原；
      · 判别键 payload="route" 与变体 dict 分开（变体 dict 的形状零改动）；
      · 选中的那条被消费一次，没选中的那条一个字都不许动。
    """
    async def go():
        facts = DecisionFacts("S03-U02", 2, consecutive_confused=2)
        pool = CandidatePool()
        pool.put(_route_cand("only-route", when={"consecutive_confused": {">=": 2}}))
        pool.put(_route_cand("route-miss", when={"consecutive_confused": {">=": 9}}))
        ordered = pool.candidates_for("S03-U02", facts)
        assert [c.candidate_id for c in ordered] == ["only-route"], ordered
        assert ordered[0].variant is None and ordered[0].route is not None
        assert await RuleDecider().decide(facts, ordered) == "only-route", "路线候选照 when 命中"
        assert await RuleDecider().decide(facts, []) is None

        d = Director(pool, RuleDecider(), grace_ms=20)
        st, unit = _state(), _unit()
        st.q_rounds["S03-U02"] = 2
        d.prefetch(unit, st)
        await asyncio.sleep(0)
        out = await d.apply_at_boundary(unit, st)
        assert out is not None, "路线必须交给状态机，不能悄悄退回 v0"
        assert out["payload"] == "route", "判别键：变体 dict 没有 payload"
        assert Route.from_dict(out) == pool.get("only-route").route, \
            f"交接单必须能原样还原成 Route（结构一次给全）：{out}"
        assert (out["unit_id"], out["target_unit"]) == ("S03-U02", "S03-U02")
        assert pool.get("only-route").status == "consumed", "交出去 = 用在课堂上了"
        assert pool.get("route-miss").status == "alive", "没选中的路线不许被消费"
        assert d.stats == {"prefetched": 1, "applied": 1, "none": 0,
                           "timeout": 0, "stale": 0, "error": 0}, d.stats
        d.close()

    asyncio.run(go())
    print("[D11] 只有路线候选 → RuleDecider 命中、边界交出结构给全的路线交接单")


def test_route_handoff_round_trips_every_kind() -> None:
    """三种走向的载荷都能原样还原：steps 里的 RouteStep 也要过一遍 from_dict。

    为什么单独一条：交接单靠 `**route.to_dict()` 铺开，谁把 steps 换成"只有 unit_id
    的字符串列表"，skip / finish 照样能用 —— 只有 revisit 会在状态机里悄悄丢掉重讲
    的讲法与时长，而那时课堂已经开讲了。
    """
    async def go():
        cases = [
            Route(route_id="r-skip-3-3", kind="skip", fork_node=3, merge_node=6,
                  steps=[], est_sec=-42.0, reason="后三格重复"),
            Route(route_id="r-revisit-4", kind="revisit", fork_node=4, merge_node=7,
                  steps=[RouteStep(unit_id="S03-U02", variant_id="v2", label="举例子",
                                   text="换成天平打比方。", est_sec=18.5)],
                  est_sec=18.5, reason="第一次用了公式"),
            Route(route_id="r-finish-8", kind="finish", fork_node=8, merge_node=10,
                  steps=[], est_sec=-90.0, reason="时间不够"),
        ]
        for want in cases:
            pool = CandidatePool()
            pool.put(_route_cand(f"c-{want.kind}", when={}, route=want))
            d = Director(pool, RuleDecider(), grace_ms=20)
            st, unit = _state(), _unit()
            d.prefetch(unit, st)
            await asyncio.sleep(0)
            out = await d.apply_at_boundary(unit, st)
            assert out is not None and out["payload"] == "route", out
            assert Route.from_dict(out) == want, f"{want.kind} 的交接单还原不回去：{out}"
            d.close()

    asyncio.run(go())
    print("[D12] 三种走向的交接单都能 Route.from_dict 原样还原")


def test_route_handoff_consumes_the_candidate_exactly_once() -> None:
    """同一个候选只消费一次：下一个边界既选不到它，也不会被第二次交出去。

    为什么值得单独一条：revisit 会回头重讲，同一条路线被执行两次就是两倍的课堂时间；
    而"消费"在池里的表现只是 status 变了一个字符串 —— 谁都能悄悄改坏它。
    """
    async def go():
        pool = CandidatePool()
        pool.put(_route_cand("r1", when={}))
        d = Director(pool, RuleDecider(), grace_ms=20)
        st, unit = _state(), _unit()
        d.prefetch(unit, st)
        await asyncio.sleep(0)
        first = await d.apply_at_boundary(unit, st)
        assert first is not None and first["payload"] == "route", first
        assert pool.get("r1").status == "consumed"
        assert pool.just_consumed() is True, "消费必须被门卫看见一次（§7.3 才会叫 C 上货）"
        assert pool.just_consumed() is False, "读后清零：门卫不该永远以为刚刚消费过"

        d.prefetch(unit, st)                     # 下一个边界：同一格再来一次
        await asyncio.sleep(0)
        assert await d.apply_at_boundary(unit, st) is None, \
            "已消费的路线不许再交出去一次（revisit 被执行两遍就是两倍课堂时间）"
        assert d.stats["applied"] == 1 and d.stats["none"] == 1, d.stats
        assert pool.status() == {"alive": 0, "consumed": 1, "expired": 0}, pool.status()
        d.close()

    asyncio.run(go())
    print("[D13] 路线候选只被消费一次，第二个边界选不到它")


def test_route_and_variant_in_one_pool_do_not_interfere() -> None:
    """同池两种载荷：选中路线 → 变体原样 alive；选中变体 → 路线原样 alive。

    两个方向各走一次，因为单向只能证明一半：被路线"连坐"消费掉的变体，
    在一个只测了"路线能选出"的用例里长得和"排在后面没被选"一模一样。
    """
    async def go():
        st, unit = _state(), _unit()
        # ① 路线排在前面（agent_c 优先于 preset，§8.2）
        pool = CandidatePool()
        pool.put(_route_cand("r-skip", source="agent_c", when={}))
        pool.put(_cand("v-keep", source="preset", created_at=1.0))
        d = Director(pool, RuleDecider(), grace_ms=20)
        d.prefetch(unit, st)
        await asyncio.sleep(0)
        out = await d.apply_at_boundary(unit, st)
        assert out is not None and out["payload"] == "route", out
        assert pool.get("r-skip").status == "consumed"
        assert pool.get("v-keep").status == "alive", "变体被路线连坐消费掉了"
        d.close()

        # ② 变体排在前面：路线必须原样 alive，且变体 dict 里不许冒出 payload 键
        pool2 = CandidatePool()
        pool2.put(_route_cand("r-late", source="preset", created_at=1.0, when={}))
        pool2.put(_cand("v-first", source="agent_c", when={}))
        d2 = Director(pool2, RuleDecider(), grace_ms=20)
        d2.prefetch(unit, st)
        await asyncio.sleep(0)
        out2 = await d2.apply_at_boundary(unit, st)
        assert out2 is not None and "payload" not in out2, \
            f"变体 dict 的形状一个键都不许多：{out2}"
        assert out2["variant_id"] == "v-first", out2
        assert pool2.get("v-first").status == "consumed"
        assert pool2.get("r-late").status == "alive", "没选中的路线不许被消费"
        d2.close()

    asyncio.run(go())
    print("[D14] 同池两种载荷互不干扰（两个方向各一次）")

