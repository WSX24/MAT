"""路线分支测试 —— 钉住"一次写入把结构给全"的每一条边界。

路线是跨格走向，它进池子之后会被状态机真的执行掉，所以结构错了不会表现为
"这条候选没被选中"，而是表现为"课走岔了"。这里逐条钉住：
1. kind 只认 skip / revisit / finish（构造与 from_dict 两条入口都拒）
2. validate 的四类结构非法：fork=merge / merge 越界 / revisit steps 为空 /
   finish 的汇回点必须正好是课尾
3. 摘要 ≤30 字，且"走向 + 真跳过格数 + 时间代价"在前、reason 在后
4. 序列化往返不丢信息；脏数据只给默认值，不抛异常（kind 除外）
5. route_id 缺省按冻结的命名约定派生 —— 没有幂等键，同一条路线会被当成多条新货
6. RouteStep 的空字段 = 用该单元主 text
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from director.route import ROUTE_KINDS, Route, RouteStep  # noqa: E402


def _route(kind: str = "skip", *, fork: int = 3, merge: int = 6,
           steps: list[RouteStep] | None = None, est_sec: float = -42.0,
           reason: str = "后三格是同一道例题的重复") -> Route:
    return Route(
        route_id=f"r-{kind}-{fork}",
        kind=kind,
        fork_node=fork,
        merge_node=merge,
        steps=[] if steps is None else steps,
        est_sec=est_sec,
        reason=reason,
    )


def test_route_step_defaults_and_round_trip() -> None:
    """步骤的空字段 = 用该单元主 text；脏字段给默认值；往返不丢信息。"""
    bare = RouteStep(unit_id="S01-U02")
    assert (bare.variant_id, bare.label, bare.text, bare.est_sec) == ("", "", "", 0.0), \
        "空 variant_id / text 的含义是『用该单元主 text』，所以默认值必须是空串而不是编出来的"

    full = RouteStep.from_dict({"unit_id": "S02-U01", "variant_id": "v2", "label": "举例子",
                                "text": "三斤苹果加两斤梨。", "est_sec": 3.5})
    assert (full.variant_id, full.label, full.est_sec) == ("v2", "举例子", 3.5)
    assert RouteStep.from_dict(full.to_dict()) == full, "往返不能丢信息"

    dirty = RouteStep.from_dict({"est_sec": "很久", "unit_id": None})
    assert (dirty.unit_id, dirty.est_sec) == ("", 0.0), "脏数字兜底成 0，不许抛异常"
    print("[R1] RouteStep 空字段语义 / 脏数据兜底 / 往返")


def test_kind_guard_on_both_entries() -> None:
    """kind 是唯一"非法就该炸"的字段：怎么走没定义，任何兜底都是猜。"""
    assert ROUTE_KINDS == ("skip", "revisit", "finish")
    for bad in ("jump", "", "SKIP", "skip "):
        with pytest.raises(ValueError):
            _route(kind=bad)
        with pytest.raises(ValueError):
            Route.from_dict({"kind": bad, "fork_node": 1, "merge_node": 2})
    print("[R2] kind 非法在构造与 from_dict 两条入口都炸")


def test_validate_shared_structure() -> None:
    """三种走向共用的结构边界：0 <= fork < merge <= total。"""
    total = 10
    for kind in ROUTE_KINDS:
        steps = [RouteStep(unit_id="S01-U02")] if kind == "revisit" else []
        merge = total if kind == "finish" else 6
        _route(kind, fork=3, merge=merge, steps=steps).validate(total)   # 合法不抛

    with pytest.raises(ValueError):          # fork == merge：岔出即汇回，等于没走
        _route(fork=4, merge=4).validate(total)
    with pytest.raises(ValueError):          # fork > merge：汇回点在岔出点之前
        _route(fork=6, merge=5).validate(total)
    with pytest.raises(ValueError):          # fork 负数
        _route(fork=-1, merge=5).validate(total)
    with pytest.raises(ValueError):          # merge 越界（课只有 10 格）
        _route(fork=8, merge=11).validate(total)
    _route(fork=8, merge=10).validate(total)  # 汇回最后一格是合法的
    print("[R3] validate 共用结构：fork<merge、fork>=0、merge<=total")


def test_validate_per_kind_rules() -> None:
    """每种走向多一条自己的规则：revisit 必须有重讲格，finish 必须收到课尾。"""
    total = 10
    with pytest.raises(ValueError):
        _route("revisit", fork=3, merge=6, steps=[]).validate(total)
    _route("revisit", fork=3, merge=6,
           steps=[RouteStep(unit_id="S01-U02")]).validate(total)

    with pytest.raises(ValueError):           # 提前收尾却"汇回"到第 7 格 = 不是收尾
        _route("finish", fork=3, merge=7, est_sec=-30.0).validate(total)
    _route("finish", fork=3, merge=total, est_sec=-30.0).validate(total)

    # skip 的"merge 必须大于 fork"就是共用规则那条：这里再钉一次 skip 自己的合法形状
    _route("skip", fork=3, merge=6, est_sec=-42.0).validate(total)
    with pytest.raises(ValueError):
        _route("skip", fork=3, merge=3, est_sec=-42.0).validate(total)
    print("[R4] validate 分行走向：revisit 非空 steps / finish 收到课尾")


def test_summary_format_and_length() -> None:
    """摘要：走向 + **真跳过格数** + 时间代价在前，reason 在后，整体 ≤30 字。

    逐字断言里的格数从"跨度（merge - fork）"改成了"真跳过格数（merge - fork - 1）"：
    fork 与 merge 都是"格子"，从 #3 岔出、在 #6 汇回，中间真正没讲的是 #4 #5 两格 ——
    按跨度写会把每一格都多算一格（#3 fork / #6 merge → 说"跳过 3 格"）。这是口径错误，
    不是文案偏好：J 会拿这个数比"划不划算"，多算一格等于把每条跳讲都说得比实际更省。
    """
    skip = _route("skip", fork=3, merge=6, est_sec=-42.0, reason="后三格重复")
    assert skip.summary() == "跳讲：跳过 2 格 · 省 42 秒：后三格重复", skip.summary()

    rev = _route("revisit", fork=4, merge=7, est_sec=15.0, reason="第一次用了公式",
                 steps=[RouteStep(unit_id="S01-U02"), RouteStep(unit_id="S01-U03")])
    assert rev.summary() == "补回头：重讲 2 格 · 多 15 秒：第一次用了公式", rev.summary()

    # finish 同一条公式：merge 就是课尾，所以跳过的是 total - fork - 1 = 5 格（不是 6）
    fin = _route("finish", fork=4, merge=10, est_sec=-90.0, reason="时间不够")
    assert fin.summary() == "提前收尾：跳过 5 格 · 省 90 秒：时间不够", fin.summary()

    # 边界：跨度只有一格时真跳过 0 格（不是 -0 也不是 1）
    assert _route("skip", fork=3, merge=4, est_sec=0.0,
                  reason="").summary() == "跳讲：跳过 0 格 · 省 0 秒"

    # 符号约定：省下的时间是负数，摘要里必须说"省"；多花的说"多"
    assert "省 42 秒" in skip.summary() and "省 90 秒" in fin.summary()
    assert "多 15 秒" in rev.summary()

    long_reason = _route("skip", reason="这一条理由写得很长，长到必然要把摘要顶破才行，否则这份上界就是假的")
    s = long_reason.summary()
    assert len(s) == 30 and s.endswith("…"), (len(s), s)
    assert s.startswith("跳讲：跳过 2 格 · 省 42 秒："), s

    for r in (skip, rev, fin, long_reason, _route("skip", reason="",
                                                  fork=0, merge=1, est_sec=-1.0)):
        assert len(r.summary()) <= 30, r.summary()
        assert len(r.summary(max_chars=12)) <= 12, r.summary(max_chars=12)
    empty = _route("skip", reason="", fork=0, merge=1, est_sec=-1.0)
    assert not empty.summary().endswith("："), "没理由就不留一个空冒号"
    print("[R5] summary 格式 / ≤30 字 / 符号约定 / 没理由不留空冒号")


def test_dirty_dict_never_raises_and_derives_id() -> None:
    """脏数据只给默认值；route_id 缺省按冻结约定派生（否则同一条路线会反复覆盖别人）。"""
    r = Route.from_dict({"kind": "skip", "fork_node": "3", "merge_node": None,
                         "steps": "不是列表", "est_sec": None, "reason": None})
    assert (r.route_id, r.fork_node, r.merge_node) == ("r-skip-3-0", 3, 0)
    assert (r.steps, r.est_sec, r.reason) == ([], 0.0, "")
    assert Route.from_dict({"kind": "revisit", "fork_node": 4, "steps": [{"unit_id": "S01-U02"}]}) \
        .route_id == "r-revisit-4"
    assert Route.from_dict({"kind": "finish", "fork_node": 7}).route_id == "r-finish-7"
    # 键里带的那个数是**跨度**（merge - fork = 3），不是跳过格数（跳过 2 格）：
    # 它是幂等键的区分位（同 fork 不同汇回点），改口径就等于让同一条走向算出新名字。
    # 人话口径的"跳过几格"在 Route.summary 里，两处不必一致、也不该互相追着改。
    assert Route.from_dict({"kind": "skip", "fork_node": 2, "merge_node": 5}).route_id \
        == "r-skip-2-3", "skip 的键里带跨度：第 2 格汇回第 5 格与第 6 格是两条路线"

    full = _route("revisit", steps=[RouteStep(unit_id="S01-U02", label="换说法")])
    assert Route.from_dict(full.to_dict()) == full, "路线往返不能丢信息"
    print("[R6] from_dict 脏数据兜底 / route_id 派生 / 往返")
