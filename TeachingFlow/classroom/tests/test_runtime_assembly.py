"""装配层的验收 —— 把 J 与 C 真的装进"一节完整的课"里。

为什么单独一个文件
------------------
decider / pool / presets / producer 各自的行为已经由 test_director_*.py 与 test_producer.py
钉住了，但"它们同时在场、又装在真实的状态机循环上"这件事没人证明过。§12 阶段 3 的三条
验收（①调模型次数 ②消费率 ③C 挂掉无感）说的都不是某个函数对不对，而是"装起来之后
课堂是什么样" —— 那一层只能靠跑完整一节课来看。

逐条对应（编号沿用任务书）：
  a) 默认关：wire 什么都不做、machine.director 仍是 None，课堂逐字不变（§11 最后一行）
  b) 开关打开：Director 挂上，预置变体真的被状态机灌池并发生切换（§12 阶段 2 验收）
  c) serve_agent_c 可启可停：跑完一节完整课，tick > 0，课后 0 悬挂 task（§7.1 生命周期）
  d) §12 阶段 3 验收③：proposer 全程抛异常，课堂照常跑完、异常不逃出循环
  e) §12 阶段 3 验收②：consumed/(consumed+expired) 可算、分母非零（具体值只报告）
  f) §7.3 门卫在真实课上确实挡掉大部分唤醒（tick 次数 ≫ 真调 proposer 次数）

全部离线：零网络、零真实 TTS、零 60s 等待 —— interval 一律取 0.01 级。
"课"用的是 test_variant_e2e 那套无头课堂（真状态机 + 假 LLM），
因为要证明的正是"接线接在状态机上"，而不是"函数返回值对不对"。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.producer import TemplateProposer  # noqa: E402
from director.assembly import serve_agent_c, wire  # noqa: E402
from director.decider import Director  # noqa: E402
from tests.fixtures import make_deck  # noqa: E402
from tests.test_machine import make_settings  # noqa: E402
from tests.test_variant_e2e import (  # noqa: E402
    Classroom,
    make_variant_script,
    seed_confusion,
)

#: C 的唤醒周期。用 0.01 而不是 60：这节课在测试里只活几百毫秒，
#: 60s 的周期会让"跑完一整节课"和"一次都没醒"变成同一件事。
INTERVAL_S = 0.01


# ===========================================================================
# 夹具
# ===========================================================================

def settings_off():
    """默认口径：**不改任何东西**，直接用夹具那份 Settings。

    特意不做 `replace(..., director_enabled=False)`：要证明的恰恰是"新字段的默认值
    就是关"，而手写 False 会把这个默认值从断言里偷走。
    """
    return make_settings()


def settings_on():
    """显式开：生产里对应 CLASSROOM_DIRECTOR=1。"""
    return replace(make_settings(), director_enabled=True)


class CountingProposer:
    """把真实现的调用次数记下来（§7.3 的门卫效果只能靠这个数看）。"""

    def __init__(self, inner=None):
        self.inner = inner
        self.calls = 0

    async def propose(self, request):
        self.calls += 1
        if self.inner is None:
            return []
        return await self.inner.propose(request)


class ExplodingProposer:
    """每次调用都抛 —— §11 "C 整个挂掉"的最坏形态，但只挂 C。

    刻意不在外层再包 try：要证明的是**循环宿主**吞掉了它，而不是测试自己兜住了它。
    """

    def __init__(self):
        self.calls = 0

    async def propose(self, request):
        self.calls += 1
        raise RuntimeError("模拟 C 的模型服务整个挂掉")


async def run_with_agent_c(classroom, *, proposer, interval_s: float = INTERVAL_S,
                           deck=None, on_tick=None) -> int:
    """把 C 的循环挂在课堂旁边跑完整一节课，返回 loop 自己结算出来的 tick 次数。

    先 `sleep(interval_s)` 再开课，不是为了等时间，是为了让**第一轮唤醒确定地落在
    开课位置**（当前 = 第一个单元、剩余 5 个 ≥ MIN_REMAINING_UNITS）：门卫只有在那里
    才可能开闸。不这么写，"门卫开过闸吗"就取决于调度顺序，用例会时灵时不灵。
    """
    task = asyncio.create_task(serve_agent_c(
        classroom.machine, proposer=proposer, deck=deck or make_deck(),
        interval_s=interval_s, on_tick=on_tick,
    ))
    await asyncio.sleep(interval_s)
    try:
        await classroom.run()
        # 课堂结束后循环必须**自己**退出：等不到就是悬挂，用例直接失败（不是替它取消）。
        return await asyncio.wait_for(task, timeout=10.0)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def assert_no_dangling_tasks(*, grace: float = 0.2) -> None:
    """课后不许有悬挂 task（与 test_variant_e2e 的 j) 同一口径）。

    Director 的预取 task 由状态机的收尾出口取消，C 的循环靠 `_finished.wait()` 退出。
    这两件事都"本来该自己收干净" —— 所以这里给它们一个 tick 的时间，
    而不是靠进程退出收尸。
    """
    for _ in range(3):
        await asyncio.sleep(0)
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=grace)
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert others == [], f"课后不许有悬挂 task：{others}"


def words(classroom) -> list[tuple[str, str]]:
    """课堂上说过的每一句话（speaker, text）—— "逐字一致"要按这个比。"""
    return [(u.speaker, u.text) for u in classroom.state.transcript]


# ===========================================================================
# a) 默认关：wire 什么都不做，课堂逐字不变（§11 最后一行）
# ===========================================================================

async def test_a_default_off_wires_nothing_and_keeps_the_class_verbatim():
    assert settings_off().director_enabled is False, \
        "开关的默认值必须是关：这是'整套拆掉还能上课'的第一道保险"

    # 对照组：从头到尾没提过装配这回事的一节课
    plain = await Classroom(make_variant_script()).run()

    wired = Classroom(make_variant_script())
    assert wire(wired.machine, settings=settings_off()) is None, "关着的时候 wire 必须返回 None"
    assert wired.machine.director is None, "关着的时候不许挂上 Director"
    await wired.run()

    script = wired.script
    assert sum(len(u.variants) for u in script.units) > 0, \
        "夹具前提：脚本里必须真的有变体，否则这条对照是空的"

    assert words(wired) == words(plain), "默认关时课堂必须与未装配时逐字一致"
    assert wired.state.seq == plain.state.seq, "连句数都必须一样"
    # 接线是真的没走到，不是"走到了但恰好没生效"
    assert wired.machine._variant_switched == set(), "没有 director 就不该有单元被标记成已切换"
    assert wired.machine._pos_epoch == 0, "没有 director 就不该动位置纪元"
    for uid in [u.unit_id for u in plain.script.units]:
        assert wired.script.unit(uid).text == plain.script.unit(uid).text, f"{uid} 的正文被改过"

    print(f"[a] 默认关：{len(words(wired))} 句逐字一致，机器上 director=None，"
          f"变体一句都没播出")


# ===========================================================================
# b) 开关打开：Director 挂上，预置变体真的被灌池并切换
# ===========================================================================

async def test_b_enabled_wires_a_director_and_presets_really_switch():
    script = make_variant_script()
    classroom = Classroom(script)          # 先建机器（此时 director=None）
    director = wire(classroom.machine, settings=settings_on())   # 装配发生在建机器之后

    assert isinstance(director, Director), "开关打开后 wire 必须返回 Director"
    assert classroom.machine.director is director, "必须真的挂到机器上，而不是只返回给调用方"

    # 与 test_variant_e2e 同一手法：造出"这几个单元没讲明白"的事实，
    # 预置变体的触发条件（v1：vague/confused；v2：连续没懂 ≥2）才会命中。
    targets = ["S01-U01", "S02-U01", "S02-U02", "S02-U03"]
    seed_confusion(classroom.state, targets)
    await classroom.run()

    applied = classroom.applied_units()
    assert applied, "开关打开后预置变体必须真的被灌池并切换（否则等于没装上）"
    assert len(set(applied)) >= 3, f"切换要发生在不同单元上，否则只是同一段被反复改写：{applied}"
    # 纠偏后 J 选的是"下一格"：被换的是困惑过那一格的**后继**，困惑过的那几格只讲一遍
    successors = {script.next_after(t).unit_id for t in targets}
    assert set(applied) == successors, (
        f"切换必须落在'困惑过那一格的下一格'上（J 是选路者）：{applied} vs {sorted(successors)}")
    assert director.stats["applied"] == len(applied), \
        f"J 的账必须与课堂上真的播出过的次数一致：{director.stats} vs {applied}"
    assert director.pool.status()["consumed"] == len(applied), "被选中的候选必须立刻消费掉"
    assert classroom.machine._finished.is_set(), "课堂必须正常收尾，不能停在半路"

    print(f"[b] 开关打开：{len(applied)} 次切换 / {len(set(applied))} 个单元 {sorted(set(applied))}，"
          f"池子 {director.pool.status()}，stats {director.stats}")


# ===========================================================================
# c) serve_agent_c 可启可停：tick > 0，课后 0 悬挂 task
# ===========================================================================

async def test_c_serve_agent_c_ticks_a_full_class_and_exits_cleanly():
    script = make_variant_script()
    classroom = Classroom(script)
    director = wire(classroom.machine, settings=settings_on())
    seed_confusion(classroom.state, ["S01-U01"])
    proposer = TemplateProposer()

    ticks = await run_with_agent_c(classroom, proposer=proposer)

    assert ticks > 0, "C 在整节课里一次都没醒，这个宿主就没有被证明"
    assert classroom.machine._finished.is_set(), "课堂必须正常跑完"
    assert classroom.state.slide_index == script.slide_count - 1
    # 循环是**自己**退出的（run_with_agent_c 等到了它的返回值），不是被取消或超时
    assert not [e for e in classroom.metrics.events if e["kind"] == "agent_c_dead"]
    await assert_no_dangling_tasks()

    print(f"[c] tick {ticks} 次后自行退出，悬挂 task 0，池子 {director.pool.status()}")


# ===========================================================================
# d) §12 阶段 3 验收③：C 全程挂掉，课堂无感
# ===========================================================================

async def test_d_c_dead_the_whole_class_is_unaffected():
    script = make_variant_script()
    classroom = Classroom(script)
    director = wire(classroom.machine, settings=settings_on())
    seed_confusion(classroom.state, ["S01-U01", "S02-U01"])
    proposer = ExplodingProposer()

    ticks = await run_with_agent_c(classroom, proposer=proposer)

    assert proposer.calls > 0, "门卫至少开过一次闸，否则这条用例没走到异常路径"
    assert ticks > proposer.calls, "一次失败之后循环必须继续（§11：下个周期重试）"
    # 课堂照常跑完，且**异常没有逃出循环**（逃出去的话 run_with_agent_c 会把它抛出来）
    assert classroom.machine._finished.is_set(), "C 挂掉不许把课堂停在半路"
    assert classroom.state.slide_index == script.slide_count - 1, "课堂必须照常讲到最后一页"
    # J 与 P 的链路一点没受影响：预置变体照常被灌池、照常切换
    applied = classroom.applied_units()
    assert applied, "C 挂掉之后预置变体必须照常切换（§11：退回现状）"
    assert director.stats["error"] == 0, f"J 的账不该被 C 的失败污染：{director.stats}"
    await assert_no_dangling_tasks()

    # 失败必须落进 metrics：课堂上没人看得见 C，只有这几个数能复盘它为什么没上货
    failed = [e for e in classroom.metrics.events
              if e["kind"] == "agent_c_tick_failed" and e.get("reason") == "proposer_error"]
    dead = [e for e in classroom.metrics.events if e["kind"] == "agent_c_dead"]
    assert failed, f"C 的每次失败都要记进 metrics：{classroom.metrics.events[-5:]}"
    assert not dead, "单次失败不算'整个挂掉'（§11：超时 → 下个周期重试）"

    print(f"[d] proposer 抛异常 {proposer.calls} 次（tick {ticks} 次），课堂讲完 "
          f"{script.slide_count} 页、切换 {len(applied)} 次、悬挂 task 0")


# ===========================================================================
# e) §12 阶段 3 验收②：consumed/(consumed+expired) 可算
# ===========================================================================

async def test_e_consumption_ratio_is_computable():
    script = make_variant_script()
    classroom = Classroom(script)
    director = wire(classroom.machine, settings=settings_on())
    seed_confusion(classroom.state, ["S01-U01", "S02-U01", "S02-U02", "S02-U03"])
    proposer = CountingProposer(TemplateProposer())

    ticks = await run_with_agent_c(classroom, proposer=proposer)

    status = director.pool.status()
    consumed, expired = status["consumed"], status["expired"]
    denominator = consumed + expired
    assert denominator > 0, f"分母必须非零，否则§12 验收②根本算不出来：{status}"
    ratio = consumed / denominator
    assert 0.0 <= ratio <= 1.0, f"消费率必须落在 [0,1]：{ratio}"

    # 具体阈值（≥30%）只在报告里给出，不断言：这个夹具只有 6 个单元、几秒钟就上完，
    # 拿它去卡一条 40 分钟课堂的参数，只会让它变成一条"改夹具就红"的脆弱断言。
    # 这一行是 §12 阶段 3 的完整实测面：唤醒次数 / 真调模型次数 / 切换次数 / 池子 / 悬挂 task
    applied = [e for e in classroom.metrics.events if e["kind"] == "variant_applied"]
    await assert_no_dangling_tasks()
    print(f"[e] tick {ticks} 次 / 真调 proposer {proposer.calls} 次 / "
          f"variant_applied {len(applied)} 次 / 池子 {status}（consumed={consumed}, "
          f"expired={expired}）→ consumed/(consumed+expired)={ratio:.1%} / 课后悬挂 task 0"
          f"（§12 验收②的 ≥30% 只报告不断言）")


# ===========================================================================
# f) §7.3 门卫：真调 proposer 的次数必须远小于 tick 次数
# ===========================================================================

async def test_f_gate_blocks_most_wakes():
    script = make_variant_script()
    classroom = Classroom(script)
    director = wire(classroom.machine, settings=settings_on())
    seed_confusion(classroom.state, ["S01-U01"])
    proposer = CountingProposer(TemplateProposer())
    results: list = []

    ticks = await run_with_agent_c(classroom, proposer=proposer, on_tick=results.append)

    assert ticks > 0
    assert proposer.calls > 0, "门卫一次都没开闸，那这个用例证明的是'永远不开闸'，不是'挡掉大部分'"
    assert proposer.calls < ticks, \
        f"§7.3：门卫必须挡掉大部分唤醒，实际 tick {ticks} 次 / 调模型 {proposer.calls} 次"
    assert director is not None
    await assert_no_dangling_tasks()

    # 一行看完 §12 阶段 3 的全部实测数据（tick / 真调模型 / 切换 / 池子 / 悬挂 task）
    status = director.pool.status()
    applied = [e for e in classroom.metrics.events if e["kind"] == "variant_applied"]
    # 追加（补覆盖缺口）：这个夹具只有 6 个单元，而 C 的上货窗口是 [当前+5, 当前+15]
    # （§7.4）—— 除了课首那一刹那，窗口恒为空，所以**一次都写不进池子**。以前这条用例
    # 只看 tick 次数，"C 其实什么都没造"就藏在这条缝里。真正证明"C 写得进池、并活到
    # 目标单元边界"的用例在 tests/test_proposer.py（那里用 ≥16 单元的脚本）。
    assert results, "一次唤醒都没记到，下面的断言就没有依据"
    written = [r for r in results if r.written]
    assert written == [], (
        f"6 单元夹具不该有货写进池：窗口 [+5,+15] 在这里恒为空，实际写了 {written}。"
        f"如果夹具被加长了，请改用 test_proposer.py 的长课脚本口径")

    print(f"[f] tick {ticks} 次 / 真调 proposer {proposer.calls} 次"
          f"（挡住 {ticks - proposer.calls} 次，{1 - proposer.calls / ticks:.0%} 的唤醒没有花钱）"
          f" / variant_applied {len(applied)} 次 / 池子 {status}"
          f" / 写池的唤醒 {len(written)} 次（6 单元夹具窗口恒空）/ 课后悬挂 task 0")
