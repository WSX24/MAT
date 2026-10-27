"""装配：把一个已经建好的课堂接上决策层（J + 候选池）与 C 的唤醒循环。

为什么装配要独立成一个模块，而不是直接写进 web.py
--------------------------------------------------
因为这里其实是**三个可以分别开关的决定**（§11 的降级表就是按这三层写的）：
  ① 这节课要不要这套东西 —— `wire()`：同步、必成功、默认关（§11 最后一行是硬门槛）；
  ② J 怎么装上去        —— `attach()`：建池子 + 挂 Director，同步、必成功；
  ③ C 能不能干活        —— `serve_agent_c()`：长期跑、随时可能失败。
前两件失败等于课堂开不起来，第三件失败必须让学生**完全无感**（§11："C 整个挂掉 → 无"）。
合成一个开关的后果很具体：某天模型没配好 → C 挂 → 连 J 也不敢装配 → 预置变体一句都不换，
而 J 本身是零阻塞、不调模型的（RuleDecider），它没有任何理由跟着 C 一起死。

为什么这一层只做"接线"、不做逻辑：machine.py 已验收且不再改，装配只能从外面把它接起来
（`machine.director = ...`）。所以这里不许出现教学策略、不许改状态机行为。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

from agent.producer import GateWatermark, ProposalProvider, TickResult, run_tick
from deck.schema import DeckDoc
from director.decider import DecisionProvider, Director, RuleDecider
from director.pool import CandidatePool

if TYPE_CHECKING:
    from machine import Machine

#: J 在边界上补等的上限（§10 参数表：300ms，可调 100~500）。
#: 放成常量而不是写死在 `attach` 的默认参数里：它的含义是"学生最多多等多久"，
#: 是产品口径而不是实现细节，调它的人应该看见这个数字叫什么。
GRACE_MS = 300

#: C 连续失败几次之后认定"它已经挂了"（§11："C 进程整个挂掉 → agent_c_dead"）。
#: 用**连续**而不是累计：§11 的上一行写的是"某次唤醒超时 → 60s 后重试"，那正是
#: 我们"记一次错、下个周期接着来"的形状；只有每次都炸才值得判定它挂了。
DEAD_AFTER = 5

#: 唤醒间隔的下限。0（或负值）会让循环空转，把这层从"后台角色"变成"和课堂抢 CPU 的人" ——
#: 一个配错的值不该有能力拖慢正在上课的状态机，所以在这里夹一下，而不是信任调用点。
MIN_INTERVAL_S = 0.001


def attach(
    machine: "Machine",
    *,
    decider: DecisionProvider | None = None,
    grace_ms: int = GRACE_MS,
) -> Director:
    """建池子 + Director，挂到 machine 上，返回它（§9.1 第 4~6 项的装配面）。

    `decider` 缺省用 RuleDecider：本轮没有 J 的模型版（§12 阶段 2 用规则版验收），
    而它正好是异步接口的**确定性实现** —— 断言"到底换没换讲法"时不需要任何替身。

    池子在这里新建、不从外面传：池子的持有者是 Director（"只有 J 读、只有 C 写"，§4.5）。
    让调用方也留一份引用，就会出现"两个池子"的幻觉 —— C 往一个里写、J 从另一个里读，
    账面上一切正常，课堂上一次都不切换，而且两边都不报错。
    """
    director = Director(CandidatePool(), decider or RuleDecider(), grace_ms=grace_ms)
    machine.director = director
    return director


def wire(
    machine: "Machine",
    *,
    settings,
    proposer: ProposalProvider | None = None,
) -> Director | None:
    """按开关装配决策层：关 → 返回 None 且**什么都不做**；开 → attach 后返回 Director。

    为什么默认关（§11 最后一行）：现有课堂必须能在完全不认识这套东西的前提下照常跑。
    默认关、显式开，等于把"没接对"的爆炸半径限制在那次显式配置之后。

    为什么 web.py 已经挡过一次、这里还要再挡一次：wire 也会被脚本与测试直接调用，
    而它的承诺是"关着时绝对无副作用"。把判断放在**被承诺的那个函数里**，
    换个调用点也不会悄悄破坏这条承诺。

    `proposer` 只用来**说明 C 这次能不能干活**，wire 自己绝不起后台循环 ——
    起循环是调用方的事（理由见 serve_agent_c：装配必成功，生产可失败）。
    有 proposer 时这里只负责把它装上去；真正起循环的是 web.py 的 start()，
    取消循环的是 close()。两层分开，是为了让"C 挂了"永远只表现为少几条候选（§11）。

    proposer 为 None 有两种情况，日志必须分得开，否则"为什么这节课没上货"会变成猜谜：
      · C 的开关没开（`CLASSROOM_AGENT_C`，默认关）—— 这就是设计；
      · 配置写错 / 构造失败 —— 这要查。
    """
    if not getattr(settings, "director_enabled", False):
        return None
    director = attach(machine)
    if proposer is None:
        print("[director] 已装配决策层（J + 预置变体）；"
              "C 的后台循环未启动（没有可用的 proposer）", flush=True)
    else:
        print("[director] 已装配决策层（J + 预置变体 + C 的真实模型 proposer）", flush=True)
    return director


async def serve_agent_c(
    machine: "Machine",
    *,
    proposer: ProposalProvider,
    deck: DeckDoc | None = None,
    interval_s: float = 60.0,
    watermark: GateWatermark | None = None,
    on_tick: Callable[[TickResult], None] | None = None,
) -> int:
    """C 的宿主循环（§7.1 生命周期）：每 interval_s 醒一次，直到课堂结束；返回醒了几次。

    **任何异常都不许逃出去**（§11）。C 是后台角色，它挂掉唯一允许的后果是"这节课少了
    几条候选"，绝不允许是"这节课停了"。所以这里逐层吞：
      - `run_tick` 已经把提案方的超时 / 异常 / 坏 JSON 收敛成 TickResult（§7.2）；
      - 连它都抛出来（= 我们自己有 bug），记一次错，下个周期接着试 ——
        这正是 §11"C 某次唤醒超时 → 60s 后重试，学生无感"的形状；
      - 连续 DEAD_AFTER 次都抛，才判定 C 已经挂了：记 `agent_c_dead` 后退出。
        别让一个每周期必炸的循环把日志刷满；课堂照常上，走 §8.3 的默认路径。
    只有 `asyncio.CancelledError` 照常向上抛 —— 那是调用方要停，不是失败。

    为什么用 `_finished.wait()` 当睡眠：课堂一结束循环**立刻**退出，既不用白等一个周期，
    也不存在"还挂着一个正在 sleep 的 task"。本函数自己不起任何子 task，
    所以取消它就等于取消整条生产链（§11 的"不留悬挂 task"在这里是结构保证的）。
    """
    director = getattr(machine, "director", None)
    if director is None:
        # 没有决策层就没有池子可写（§4.5 的单一真相在 Director 手上）。这不是错误路径，
        # 而是"只装了一半的配置"：说清楚，然后安静收工，绝不自己偷偷建一个池子 ——
        # 那会造出一份没有任何读者的候选。
        machine.metrics.event("agent_c_skipped", reason="no_director")
        return 0

    pool = director.pool
    watermark = watermark or GateWatermark()
    ticks = 0
    failures = 0
    while not machine._finished.is_set():
        ticks += 1
        try:
            result = await run_tick(
                state=machine.state, script=machine.script, deck=deck,
                pool=pool, watermark=watermark, proposer=proposer,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failures += 1
            machine.metrics.event("agent_c_tick_error", err=f"{type(e).__name__}: {e}")
            if failures >= DEAD_AFTER:
                machine.metrics.event("agent_c_dead", ticks=ticks, failures=failures)
                print(f"[agent_c] 连续 {failures} 次唤醒都抛异常，判定 C 已挂："
                      f"{type(e).__name__}: {e}（课堂继续，按 §8.3 用 v0 / 预置变体）",
                      flush=True)
                break
        else:
            failures = 0
            if not result.ok:
                # 单次失败（超时 / 模型报错 / 坏 JSON）不算挂掉，但必须留下痕迹：
                # 课堂上没人看得见 C，"这节课为什么少了候选"事后只能从这几个事件复盘。
                machine.metrics.event("agent_c_tick_failed", reason=result.reason)
            for item in result.rejected:
                # **路线**被丢必须留痕：变体被丢只是"这一格少一种说法"（照旧用 v0，
                # 无害），而路线被丢是"这节课不会岔路"—— 它在图上表现为一片空白，
                # 而空白与"没有坏"长得一模一样。所以这一条单独记进 metrics，
                # 让"为什么这条路线上不了"事后能查（变体的拒绝仍只进 TickResult）。
                if item.payload != "route":
                    continue
                machine.metrics.event("agent_c_route_rejected",
                                      target=item.target_unit,
                                      reasons=list(item.reasons))
            if on_tick is not None:
                try:
                    on_tick(result)
                except Exception as e:
                    # 观测回调是调用方的代码。它炸了只是少一条观测，不该拖死生产。
                    machine.metrics.event("agent_c_callback_error",
                                          err=f"{type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(
                machine._finished.wait(),
                timeout=max(MIN_INTERVAL_S, float(interval_s)),
            )
            break                    # 课堂结束：连同这次等待一起收干净
        except asyncio.TimeoutError:
            continue
    return ticks
