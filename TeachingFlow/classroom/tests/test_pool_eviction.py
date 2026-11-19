"""候选池的驱逐：池满时**当期刚需优先**（实测缺陷的回归用例）。

为什么单开一个文件
------------------
实测的缺陷不是"某条候选写错了"，而是**池子满了之后的记账方式**：C 每 60s 醒一次、
每次给未来 5~15 格各备 2 条货（expires = 目标格 + 2，要约 7 格之后才过期），三次唤醒
就把 CAP=5 占满。于是池里全是 S01-U06/07/08 的**远期备货**，而这一格真正要用到的货
（下一格的 v1/v2、这个边界的路线）一条都进不来：

    唤醒#0 written=3 alive=3 → #1 written=2 alive=5 → #2..#5 called=False 'pool_full'
    给 S01-U02 装预置变体：写入 0 条 → J 在这个边界上看得见的候选 = 空 → 连续七八格只能讲 v0

这里钉住的是修复后的五条不变量（编号沿用任务书）：

  (a) 满池的远期备货里，一条**当期货**挤得进来：expires 最大的那条被置 expired，
      其余几条与既有的变体一条不动，alive 仍然是 CAP（单一容量、单一真相）
  (b) 满池且**全是当期货**时仍旧拒收：既有行为一个字节都没变（挤不动就别挤）
  (c) expires 相等 → 拒收（严格小于：两条都要服务同一个边界，换谁都没意义）
  (d) 同 id 覆盖走原有幂等路径：不驱逐、version + 1
  (e) 驱逐有账可查：谁被挤掉 / 它活到第几格 / 被谁挤掉（TOMBSTONE，不删除）
  (f) `load_unit_presets` 享受同一条规则 —— 这一条就是被实测打脸的那一格：
      满池远期备货时给下一格装 v1/v2，必须**真的进池**

全部离线、零网络；expires_at_seq 一律用单元序号（§4.4 的记账尺子，与 producer 同源）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.producer import EXPIRE_MARGIN_UNITS, LATEST_OFFSET  # noqa: E402
from director.pool import (  # noqa: E402
    EVICT_HORIZON_UNITS,
    Candidate,
    CandidatePool,
    UnitVariant,
)
from director.presets import load_unit_presets  # noqa: E402
from state import LectureUnit  # noqa: E402

#: 一条变体候选（形状无关紧要，驱逐只看 expires_at_seq —— 这也是"不引入新时钟"的意思）。
def _cand(candidate_id: str, expires: int, *, target: str = "", source: str = "agent_c",
          text: str = "换一种讲法。") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        target_unit=target or candidate_id,
        variant=UnitVariant(variant_id="v1", label="换说法", text=text),
        when={},
        source=source,
        created_at=0.0,
        created_at_unit="",
        expires_at_seq=expires,
    )


def _fill(pool: CandidatePool, expires: list[int], prefix: str = "c-far") -> list[Candidate]:
    """把池子填到 CAP，逐条给 expires（顺序即写入顺序）。"""
    out = [_cand(f"{prefix}{i}", exp) for i, exp in enumerate(expires)]
    for cand in out:
        assert pool.put(cand) is True, f"夹具前提：空池子写不进 {cand.candidate_id}"
    assert pool.alive_count() == CandidatePool.CAP
    return out


#: 满池的"远期备货"：五条都在**一个上货窗口的期距之内**（与真实节奏同量级，
#: 而不是随手写的哨兵值 —— 见 EVICT_HORIZON_UNITS 与下面那条期距用例）。
FAR_STOCK = [10, 12, 14, 16, 18]


# ===========================================================================
# (a) 当期刚需挤掉最远的那条备货
# ===========================================================================

def test_a_near_candidate_evicts_the_farthest_stock_and_alive_stays_at_cap() -> None:
    """满池远期备货 + 一条当期货 → 写入成功、expires 最大的那条作废，其余一条不动。

    这就是实测缺陷的正面：改动前 put 一律返回 False，于是**这个边界要用到的货**
    永远进不来；而"进不来"在账上与"没有这条货"长得一模一样。
    """
    pool = CandidatePool()
    stock = _fill(pool, FAR_STOCK)

    near = _cand("c-near", 6)               # 当期：这个边界的路线 / 下一格的预置变体
    assert pool.put(near) is True, "当期刚需必须挤得进来"

    assert pool.alive_count() == CandidatePool.CAP, "驱逐只换一条，不许把容量撑大"
    victim = max(stock, key=lambda c: c.expires_at_seq)
    assert pool.get(victim.candidate_id).status == "expired", \
        f"被驱逐的必须是 expires 最大的那条：{victim.candidate_id}"
    for cand in stock:
        if cand.candidate_id == victim.candidate_id:
            continue
        assert pool.get(cand.candidate_id).status == "alive", \
            f"{cand.candidate_id} 不该被动到"
    assert pool.get("c-near").status == "alive"
    assert pool.status()["expired"] == 1, "驱逐=置 expired（TOMBSTONE），不删除"

    # J 的输入长度上界不变：候选条数仍是 CAP，且当期那条真的能被读到
    from director.facts import DecisionFacts
    facts = DecisionFacts("", 0)
    ids = [c.candidate_id for c in pool.candidates_for("c-near", facts)]
    assert "c-near" in ids, f"当期那条必须真的进 J 的视野：{ids}"


def test_eviction_gives_the_slot_to_the_nearest_not_to_whoever_came_last() -> None:
    """两条都要挤时，池子按 expires 单调收：更靠当期的那条挤掉最远的那条。

    "越靠当期越优先"是可证明的，而不是靠写入顺序：擦掉一条远货之后，池里最远的
    那条仍然是"该让位"的那一条。
    """
    pool = CandidatePool()
    _fill(pool, FAR_STOCK)
    assert pool.put(_cand("c-near", 6)) is True
    assert pool.get("c-far4").status == "expired"       # expires=18

    assert pool.put(_cand("c-nearer", 5)) is True
    assert pool.get("c-far3").status == "expired"       # expires=16 现在最远
    assert pool.alive_count() == CandidatePool.CAP


# ===========================================================================
# (b) 满池全是当期货 → 仍旧拒收（既有行为不变）
# ===========================================================================

def test_a_full_pool_of_near_stock_still_rejects_the_far_candidate() -> None:
    """五条都在前、一条远期备货在后 → 拒收（挤不动就不挤，先到先得）。

    这条是"新行为不许扩大"的那一半：修复的是"当期饿死"，不是"后来的总能挤掉先来的"。
    """
    pool = CandidatePool()
    stock = _fill(pool, [3, 4, 5, 6, 7])
    before = [(c.candidate_id, c.status, c.version) for c in pool.items()]

    assert pool.put(_cand("c-far-late", 20)) is False, "远期备货不许挤掉任何当期刚需"
    assert pool.get("c-far-late") is None
    assert [(c.candidate_id, c.status, c.version) for c in pool.items()] == before, \
        "拒收是一个字节都不许动的：既有候选一条不作废、一条不被覆盖"
    assert pool.alive_count() == CandidatePool.CAP
    assert pool.eviction_count() == 0
    assert len(stock) == CandidatePool.CAP


# ===========================================================================
# (c) expires 相等 → 拒收（严格小于）
# ===========================================================================

def test_equal_expiry_is_rejected_by_the_strict_comparison() -> None:
    """expires 相等 → 不动：两条都要服务同一个边界，换谁都没意义，只是把账搅动一次。"""
    pool = CandidatePool()
    _fill(pool, FAR_STOCK)
    assert pool.put(_cand("c-same", FAR_STOCK[-1])) is False, "等于不许驱逐（严格小于）"
    assert pool.get("c-same") is None
    assert all(c.status == "alive" for c in pool.items()), pool.status()
    assert pool.eviction_count() == 0


# ===========================================================================
# (d) 同 id 覆盖：不触发驱逐、version + 1
# ===========================================================================

def test_same_id_overwrite_still_takes_the_idempotent_path() -> None:
    """同 id 覆盖不驱逐、不占新名额，version 照旧 +1（C 每 60s 重放的那条路径）。"""
    pool = CandidatePool()
    _fill(pool, FAR_STOCK)

    again = _cand("c-far4", FAR_STOCK[-1], text="换一种讲法（重写）。")
    again.version = 9
    assert pool.put(again) is True, "已存在的 id 走覆盖分支，不受 CAP 影响"
    got = pool.get("c-far4")
    assert (got.version, got.variant.text) == (2, "换一种讲法（重写）。")
    assert pool.eviction_count() == 0, "覆盖不是驱逐"
    assert pool.alive_count() == CandidatePool.CAP
    assert all(c.status == "alive" for c in pool.items())


# ===========================================================================
# (e) 驱逐有账：谁被挤掉 / 活到第几格 / 被谁挤掉
# ===========================================================================

def test_eviction_is_recorded_and_the_victim_stays_as_a_tombstone() -> None:
    """驱逐记一笔账（frozen 的 Eviction）：复盘时能回答"这次为什么丢了那条备货"。"""
    pool = CandidatePool()
    _fill(pool, FAR_STOCK)
    assert pool.eviction_count() == 0

    assert pool.put(_cand("c-near", 6)) is True
    log = pool.evictions()
    assert len(log) == 1, log
    entry = log[0]
    assert entry.candidate_id == "c-far4"
    assert entry.expires_at_seq == FAR_STOCK[-1]
    assert entry.evicted_by == "c-near" and entry.evicted_by_expires == 6
    assert entry.to_dict() == {
        "candidate_id": "c-far4", "expires_at_seq": FAR_STOCK[-1],
        "evicted_by": "c-near", "evicted_by_expires": 6,
    }
    assert pool.eviction_count() == 1

    # TOMBSTONE：账上留痕，不是删除（否则"作废"与"从没造过"分不开）
    assert pool.get("c-far4") is not None and pool.get("c-far4").status == "expired"
    assert "c-far4" in [c.candidate_id for c in pool.items()]

    # 账是只读快照：调用方顺手 append 一条，不许伪造出一笔没发生过的驱逐
    log.append("伪造的一笔")
    assert len(pool.evictions()) == 1 and pool.eviction_count() == 1


def test_allow_evict_false_is_the_per_write_brake() -> None:
    """allow_evict=False：这一条不许驱逐（"一次唤醒最多一次驱逐"的名额由调用方发）。

    池子这一层的语义必须能被关掉：否则"名额留给最靠当期的那一条"只能写在调用方注释里，
    而 put 自己随时可能替别人花掉它。
    """
    pool = CandidatePool()
    _fill(pool, FAR_STOCK)
    assert pool.put(_cand("c-near", 6), allow_evict=False) is False
    assert pool.eviction_count() == 0
    assert pool.alive_count() == CandidatePool.CAP
    # 同一个候选，开着闸就能进 —— 差别只有"谁批准了这次驱逐"
    assert pool.put(_cand("c-near", 6)) is True


def test_evict_horizon_matches_the_supply_window() -> None:
    """"够得着"的那条线必须与 producer 的上货窗口同源：两边各写一个数就会漂移。

    EVICT_HORIZON_UNITS 的口径 = 上货窗口的最大期距（LATEST_OFFSET）+ 候选的存活余量。
    漂移的后果很具体：调大窗口之后，最远的那一档备货突然变成"挤不动"，而当期货
    永远进不来 —— 又回到实测的那个缺陷，且账上只看得到 pool_full。
    """
    assert EVICT_HORIZON_UNITS == LATEST_OFFSET + EXPIRE_MARGIN_UNITS


def test_stock_beyond_the_horizon_is_left_alone() -> None:
    """期距超过上货窗口的"备货"挤不动（宁可让它自然过期）。

    现实里只有哨兵值（expires=99 表示"永远不过期"）会撞到这条线：把它挤掉等于让
    夹具 / 回填的数据被静默作废 —— 那不是"当期优先"，那是把别的课堂段偷过来。
    """
    pool = CandidatePool()
    _fill(pool, [96, 97, 98, 99, 99])
    assert pool.put(_cand("c-near", 6)) is False
    assert pool.eviction_count() == 0 and pool.alive_count() == CandidatePool.CAP


# ===========================================================================
# (f) 预置变体装载：被实测打脸的那一格
# ===========================================================================

def _next_unit(unit_id: str = "S01-U02") -> LectureUnit:
    """下一格的单元（带 v1/v2 两条预置变体，形状与 P 写进 plan.json 的一致）。"""
    unit = LectureUnit(
        unit_id=unit_id, slide=0, index_in_slide=1,
        text="这一格讲的是另一个知识点。它和上一格连在一起看才完整。",
        anchors=[], kp=["K2"],
    )
    unit.variants = [
        {"variant_id": "v1", "label": "换说法", "text": "换个说法：这一格讲的是另一个知识点。"},
        {"variant_id": "v2", "label": "举例子", "text": "举个例子：拿两个苹果讲这个知识点。"},
    ]
    return unit


def test_load_unit_presets_squeezes_into_a_pool_full_of_far_stock() -> None:
    """满池远期备货时给下一格装 v1/v2：**真的进池**（实测里是"写入 0 条"）。

    为什么这条最要紧：预置变体的 expires 是**下一格的序号**，也就是池子里最靠当期的那一档。
    改动前它被 C 的远期备货堵在门外 —— J 在这个边界上看得见的候选 = 空，于是学生连续
    七八格只能听到 v0，而账上只表现为"后面没再换过讲法"。
    """
    pool = CandidatePool()
    _fill(pool, [14, 16, 18, 20, 22])      # C 给未来几格备的货（期距在同一窗口内）
    unit = _next_unit()

    written = load_unit_presets(pool, unit, seq=12, now=100.0, target_unit=unit.unit_id)

    assert written == 2, f"v1/v2 必须都进池（实测缺陷里这里是 0）：{written}"
    assert pool.get("c-S01-U02-v1").status == "alive"
    assert pool.get("c-S01-U02-v2").status == "alive"
    assert pool.alive_count() == CandidatePool.CAP, "容量不变：挤掉两条，留下三条备货"
    assert pool.eviction_count() == 2, "两条预置各挤掉一条最远的备货，账上要能查到"
    assert {e.evicted_by for e in pool.evictions()} == {"c-S01-U02-v1", "c-S01-U02-v2"}
    assert pool.get("c-far4").status == "expired" and pool.get("c-far3").status == "expired"

    # 这两条正是 J 在下一格边界上要看的货（target_unit 匹配 + 条件匹配）
    from director.facts import DecisionFacts
    confused = DecisionFacts("S01-U02", 0, consecutive_confused=2, comprehension="confused")
    ids = [c.candidate_id for c in pool.candidates_for("S01-U02", confused)]
    assert {"c-S01-U02-v1", "c-S01-U02-v2"} <= set(ids), ids


def test_load_unit_presets_still_writes_nothing_when_nothing_can_be_evicted() -> None:
    """挤不动时照旧"少写"：满池全是**同一边界**的货（expires 都等于 seq）→ 一条也装不进来。

    预置变体的 expires 就是装载时传进来的 seq（= 下一格的序号），所以"挤不动"只有一种形状：
    池里那条最远的货也不比它更远。这时换上 v1/v2 没有任何意义 —— 两条都要服务同一个边界。
    """
    pool = CandidatePool()
    _fill(pool, [3, 3, 3, 3, 3])
    unit = _next_unit()

    assert load_unit_presets(pool, unit, seq=3, now=100.0, target_unit=unit.unit_id) == 0
    assert pool.eviction_count() == 0
    assert all(c.status == "alive" for c in pool.items())
