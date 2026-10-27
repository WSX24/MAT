"""候选池测试 —— 池子是 J 的唯一输入源，它的五条语义错一条就会换错讲法。

要证明的五件事：
1. CAP=5 是硬上限：满了拒收新 id，且不因为覆盖而偷偷变多
2. 同 id 幂等覆盖、版本由池维护（写入方给的 version 不算数）
3. candidates_for 的三重过滤（alive / 目标单元 / 条件）+ 排序（agent_c 优先、同源新近）+ 截断 5 条
4. mark_consumed / expire_stale / status 的三态记账
5. just_consumed 读后清零（门卫忘了复位就再也不叫 C 上货）
6. 路线候选与变体候选**同池同纪律**：双载荷互斥、摘要共用 30 字上界、
   CAP / 幂等 / 三态一条不少 —— 给路线另开一个池就等于有两处排序与两处上限
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from director.facts import DecisionFacts  # noqa: E402
from director.pool import SUMMARY_CHARS, Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.route import Route, RouteStep  # noqa: E402


def _variant(variant_id: str = "v1", label: str = "换说法",
             text: str = "换一种说法，用天平打比方。") -> UnitVariant:
    return UnitVariant(variant_id=variant_id, label=label, text=text)


def _cand(candidate_id: str, *, target: str = "", when: dict | None = None,
          source: str = "preset", created_at: float = 0.0, expires: int = 99,
          variant: UnitVariant | None = None, unit: str = "S03-U02") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        target_unit=target,
        variant=variant or _variant(candidate_id),
        when={} if when is None else when,
        source=source,
        created_at=created_at,
        created_at_unit=unit,
        expires_at_seq=expires,
    )


def _route(kind: str = "skip", *, fork: int = 3, merge: int = 6,
           steps: list[RouteStep] | None = None, est_sec: float = -42.0,
           reason: str = "后三格是同一道例题的重复") -> Route:
    return Route(route_id=f"r-{kind}-{fork}", kind=kind, fork_node=fork,
                 merge_node=merge, steps=[] if steps is None else steps,
                 est_sec=est_sec, reason=reason)


def _route_cand(candidate_id: str, *, target: str = "", when: dict | None = None,
                source: str = "agent_c", created_at: float = 0.0, expires: int = 99,
                unit: str = "S03-U02", route: Route | None = None) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        target_unit=target,
        when={} if when is None else when,
        source=source,
        created_at=created_at,
        created_at_unit=unit,
        expires_at_seq=expires,
        route=route or _route(),
    )


def test_variant_round_trip_and_summary() -> None:
    """变体：空 anchors/ops = 沿主单元、est_sec 由字数派生、摘要 ≤30 字。"""
    d = {
        "variant_id": "v2",
        "label": "举例子",
        "text": "我们举一个具体数字的例子：三斤苹果加两斤梨。" * 3,
        "anchors": [{"element_id": "s02-sh01"}],
        "ops": [],
    }
    v = UnitVariant.from_dict(d)
    assert (v.variant_id, v.label) == ("v2", "举例子")
    assert v.anchors == [{"element_id": "s02-sh01"}] and v.ops == []
    assert v.est_sec == round(len(v.text) / 5.0, 1) > 0, "est_sec 缺省要由字数派生"
    assert UnitVariant.from_dict(v.to_dict()) == v, "序列化往返不能丢信息"

    short = UnitVariant.from_dict({"variant_id": "v1", "label": "换说法", "text": "用天平打比方。"})
    assert short.summary() == "换说法：用天平打比方。"

    long = UnitVariant.from_dict(
        {"variant_id": "c1", "label": "更浅的讲法", "text": "很长的替代表述" * 40})
    s = long.summary()
    assert len(s) == SUMMARY_CHARS, (len(s), s)
    assert s.startswith("更浅的讲法：") and s.endswith("…")

    # label 自己就超标时只能留 label，不许靠拼正文把长度撑破
    huge_label = UnitVariant.from_dict({"variant_id": "x", "label": "很长的标签" * 20, "text": "正文"})
    assert len(huge_label.summary()) <= SUMMARY_CHARS
    print("[P1] UnitVariant 往返 / est_sec 派生 / 摘要 ≤30 字")


def test_pool_cap_and_idempotent_overwrite() -> None:
    """CAP=5 硬上限 + 同 id 覆盖（版本自增、不新增）。"""
    pool = CandidatePool()
    for i in range(CandidatePool.CAP):
        assert pool.put(_cand(f"p{i}")) is True
    assert CandidatePool.CAP == 5
    assert pool.put(_cand("p5")) is False, "alive 满 5 条后新 id 必须拒收"
    assert pool.get("p5") is None
    assert pool.status() == {"alive": 5, "consumed": 0, "expired": 0}

    cand = _cand("p0", source="agent_c", when={"consecutive_confused": {">=": 1}})
    cand.version = 9
    assert pool.put(cand) is True
    got = pool.get("p0")
    assert (got.version, got.source) == (2, "agent_c"), "版本由池维护，传入的 version 不算数"
    assert pool.status()["alive"] == 5, "覆盖不该新增一条"

    assert pool.put(_cand("p0")) is True
    assert pool.get("p0").version == 3, "满额时仍可覆盖已有 id（它不是新 id）"
    print("[P2] CAP=5 硬上限 + 同 id 幂等覆盖 / version 自增")


def test_candidates_for_triple_filter_and_order() -> None:
    """三重过滤 + 排序：alive、目标单元（空串=任意）、条件；agent_c 优先、同源新近。"""
    hit = {"consecutive_confused": {">=": 2}}
    facts = DecisionFacts("S03-U02", 2, consecutive_confused=2, probe_result="timeout")
    pool = CandidatePool()
    pool.put(_cand("p-old", when=hit, created_at=1.0))
    pool.put(_cand("p-new", when=hit, created_at=5.0))
    pool.put(_cand("c-new", when=hit, source="agent_c", created_at=2.0))
    pool.put(_cand("other-unit", target="S09-U09", when=hit, created_at=9.0))
    pool.put(_cand("no-match", when={"probe_result": {"==": "confirmed"}}, created_at=9.0))
    got = [c.candidate_id for c in pool.candidates_for("S03-U02", facts)]
    assert got == ["c-new", "p-new", "p-old"], got

    pool2 = CandidatePool()
    pool2.put(_cand("any", created_at=1.0))
    pool2.put(_cand("mine", target="S03-U02", created_at=2.0))
    pool2.put(_cand("elsewhere", target="S09-U09", created_at=3.0))
    pool2.put(_cand("dead", created_at=4.0))
    pool2.mark_consumed("dead")
    assert [c.candidate_id for c in pool2.candidates_for("S03-U02", facts)] == ["mine", "any"]
    assert [c.candidate_id for c in pool2.candidates_for("S01-U01", facts)] == ["any"], \
        "target_unit 空串 = 任意单元"
    print("[P3] candidates_for 三重过滤 + agent_c 优先 / 同源新近")


class _WidePool(CandidatePool):
    """只为验证截断：CAP 调大之后，J 也只许看 5 条。"""

    CAP = 8


def test_candidates_for_truncates_at_five() -> None:
    """[:5] 与 CAP 是两道独立保险：池子容量变了也不能让 J 的输入变长。"""
    facts = DecisionFacts("S03-U02", 2)
    pool = _WidePool()
    for i in range(8):
        assert pool.put(_cand(f"c{i}", created_at=float(i))) is True
    got = pool.candidates_for("S03-U02", facts)
    assert len(got) == 5, [c.candidate_id for c in got]
    assert [c.candidate_id for c in got] == ["c7", "c6", "c5", "c4", "c3"], "截断前先按新近排序"
    print("[P4] candidates_for 硬截断前 5 条")


def test_lifecycle_consume_expire_status() -> None:
    """三态记账：消费即消失、过期按 seq 边界、重复过期不计数、未知 id 不炸。"""
    pool = CandidatePool()
    pool.put(_cand("a", expires=5))
    pool.put(_cand("b", expires=5))
    pool.put(_cand("c", expires=6))
    pool.mark_consumed("a")
    assert pool.status() == {"alive": 2, "consumed": 1, "expired": 0}
    assert pool.expire_stale(5) == 0, "expires_at_seq == current_seq 还留给下一格"
    assert pool.expire_stale(6) == 1, "只作废 b"
    assert pool.expire_stale(7) == 1, "c 越过一格后作废"
    assert pool.status() == {"alive": 0, "consumed": 1, "expired": 2}
    assert pool.get("b").status == "expired"
    assert pool.expire_stale(999) == 0, "已经作废的不重复计数"
    assert pool.mark_consumed("不存在") is None
    assert pool.status()["consumed"] == 1
    print("[P5] mark_consumed / expire_stale / status 三态记账")


def test_just_consumed_reads_and_clears() -> None:
    """门卫门用：读后清零，没真消费不置位。"""
    pool = CandidatePool()
    pool.put(_cand("a"))
    assert pool.just_consumed() is False
    pool.mark_consumed("a")
    assert pool.just_consumed() is True
    assert pool.just_consumed() is False, "读后必须清零"
    pool.put(_cand("b"))
    pool.mark_consumed("unknown")
    assert pool.just_consumed() is False, "没有真消费不能置位"
    print("[P6] just_consumed 读后清零")
def test_candidate_payload_is_exactly_one() -> None:
    """两种载荷恰好一个：都空 = 没什么可选，都满 = 不知道选中的是什么。"""
    ok_variant = Candidate(candidate_id="c-v", target_unit="", when={}, source="preset",
                           created_at=0.0, created_at_unit="", expires_at_seq=9,
                           variant=_variant("v1"))
    ok_route = _route_cand("c-r")
    assert ok_variant.route is None and ok_route.variant is None

    docs = dict(target_unit="", when={}, source="agent_c", created_at=0.0,
                created_at_unit="", expires_at_seq=9)
    with pytest.raises(ValueError) as exc:
        Candidate(candidate_id="c-none", **docs)
    assert "变体与路线恰好一个" in str(exc.value), str(exc.value)

    with pytest.raises(ValueError) as exc:
        Candidate(candidate_id="c-both", variant=_variant("v1"), route=_route(), **docs)
    assert "变体与路线恰好一个" in str(exc.value), str(exc.value)
    print("[P7] Candidate 双载荷互斥（都空 / 都满都拒）")


def test_candidate_summary_covers_both_payloads() -> None:
    """摘要按载荷分派，但两种载荷共用同一个 30 字上界（J 的输入长度不许涨）。"""
    variant = _variant("c1", "更浅的讲法", "很长的替代表述" * 40)
    cand_v = Candidate(candidate_id="c-v", target_unit="", when={}, source="agent_c",
                       created_at=0.0, created_at_unit="", expires_at_seq=9,
                       variant=variant)
    assert cand_v.summary() == variant.summary(), "变体载荷必须逐字走 UnitVariant.summary"
    assert len(cand_v.summary()) <= SUMMARY_CHARS

    long_route = _route(reason="这一条理由写得很长，长到必然要把摘要顶破才行，否则这份上界就是假的")
    cand_r = _route_cand("c-r", route=long_route)
    assert cand_r.summary() == long_route.summary(), "路线载荷必须逐字走 Route.summary"
    assert len(cand_r.summary()) <= SUMMARY_CHARS, cand_r.summary()
    assert cand_r.summary().startswith("跳讲："), cand_r.summary()

    for cand in (cand_v, cand_r):
        assert len(cand.summary(max_chars=12)) <= 12, cand.summary(max_chars=12)
    print("[P8] Candidate.summary 两种载荷都 ≤30 字（上界共用）")


def test_route_candidate_idempotent_overwrite_and_cap() -> None:
    """路线候选与变体候选挤同一个 CAP：新 id 满额拒收、同 id 覆盖版本 +1。"""
    pool = CandidatePool()
    for i in range(4):
        assert pool.put(_cand(f"v{i}")) is True
    assert pool.put(_route_cand("r-skip-3-3")) is True
    assert pool.status() == {"alive": 5, "consumed": 0, "expired": 0}, "路线也占 CAP 一格"

    assert pool.put(_route_cand("r-finish-8", route=_route("finish", fork=8, merge=10,
                                                          est_sec=-30.0))) is False, \
        "alive 满 5 条后新的路线候选同样被拒收"
    assert pool.get("r-finish-8") is None

    again = _route_cand("r-skip-3-3", route=_route(est_sec=-50.0, reason="改主意了"))
    again.version = 9
    assert pool.put(again) is True
    got = pool.get("r-skip-3-3")
    assert (got.version, got.route.est_sec, got.route.reason) == (2, -50.0, "改主意了"), \
        "版本由池维护，载荷被新的一次写入整体替换"
    assert pool.status()["alive"] == 5, "覆盖不该新增一条"
    assert pool.get("v0").version == 1, "别条候选的版本不受影响"
    print("[P9] 路线候选同池：CAP 拒收 + 幂等覆盖 version+1")


def test_route_candidate_lifecycle_and_filtering() -> None:
    """路线候选进池后走同一套筛选 / 排序 / 三态记账，不享受任何特权。"""
    hit = {"consecutive_confused": {">=": 2}}
    facts = DecisionFacts("S03-U02", 2, consecutive_confused=2)
    pool = CandidatePool()
    pool.put(_cand("v-new", when=hit, created_at=5.0))
    pool.put(_route_cand("r-hit", when=hit, created_at=9.0))
    pool.put(_route_cand("r-miss", when={"consecutive_confused": {">=": 9}}, created_at=9.5))
    pool.put(_route_cand("r-elsewhere", target="S09-U09", when=hit, created_at=9.9))
    got = [c.candidate_id for c in pool.candidates_for("S03-U02", facts)]
    assert got == ["r-hit", "v-new"], got
    assert pool.candidates_for("S03-U02", facts)[0].route is not None

    pool.mark_consumed("r-hit")
    assert pool.get("r-hit").status == "consumed" and pool.just_consumed() is True
    assert "r-hit" not in [c.candidate_id for c in pool.candidates_for("S03-U02", facts)]

    assert pool.expire_stale(100) == 3, "路线候选也按 expires_at_seq 过期（已消费的不重复计）"
    assert pool.status() == {"alive": 0, "consumed": 1, "expired": 3}, pool.status()
    assert pool.get("r-miss").status == "expired"
    print("[P10] 路线候选的筛选 / 消费 / 过期三态记账")

