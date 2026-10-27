"""决策层：J 的实现与三段式协调器。

铁律 2（§0.4）是"J 永远不能阻塞课堂"，所以这里没有任何一条路径会等一次模型往返：
预取在单元开始时就发出去，边界上最多补 300 ms，补不到就用 v0。
四种失败（无候选 / 超时 / 过期 / 异常）全部收敛到同一个默认值 ——
课堂上"这次换不换讲法"没有第三个答案值得让学生多等（§8.3）。

**事实来源与候选目标是两个维度**（架构纠偏）：J 在单元 N 的边界上读的是"这格学生卡没卡"
（事实来自 N），但选的是"接下来那一格怎么讲"（货是给 N+1 备的）。所以在 prefetch /
apply_at_boundary 上把两者分开：`unit` 是事实来源，`target_unit` 是这次选的是给哪个单元用的。
不传 `target_unit` 时两者合成一个（= 老契约，逐字等价），老调用点一个字都不用改。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from state import ClassroomState, LectureUnit

from .facts import DecisionFacts, facts_from_state, matches
from .pool import Candidate, CandidatePool


class DecisionProvider(Protocol):
    """选择器接口（§6.1）：返回 candidate_id；None = 用默认 v0。"""

    async def decide(self, facts: DecisionFacts, candidates: list[Candidate]) -> str | None:
        ...


class RuleDecider:
    """纯规则实现：不调模型，按候选已排好的顺序取第一个条件命中的。

    为什么不自作聪明重排：池子已经按"agent_c 优先、同源新近"排过（§8.2）。
    决策层再排一次，就会出现两处优先级规则互相打架，而且线上没人说得清是谁生效了。
    """

    async def decide(self, facts: DecisionFacts, candidates: list[Candidate]) -> str | None:
        for cand in candidates:
            if matches(cand.when, facts):
                return cand.candidate_id
        return None


@dataclass
class _Slot:
    """一格前瞻：属于哪个单元的哪个位置，以及后台算到哪了（§6.5 的校验依据）。

    位置键为什么必须能由调用方给：state.seq 是"课堂上说过几句话"的计数，
    单元播放期间每吐一句旁白都会 +1（machine.py 的 _log_utterance → state.emit），
    于是"单元开始时预取、单元结束时消费"必然被判成位置变了 —— 每个单元的决策
    都会白白作废，J 永久退化成 v0。而什么才算"位置"（slide / seg_index / 让位纪元）
    只有 machine 知道，Director 不该猜，所以 key 由调用方通过 position 传入。
    不传时回落到 state.seq，老调用点一个字都不用改。
    """

    unit_id: str                # 事实来源单元（这次决策看的是哪一格的表现）
    target_unit: str            # 候选的目标单元（这一条货是给哪一格备的）
    seq: int                    # 预取时的 state.seq：返回 dict 的 "seq"，也是缺省位置键
    position: object | None     # 调用方给的显式位置键；None = 用 state.seq 当位置键
    task: asyncio.Task
    consumed: bool = False

    @property
    def key(self) -> object:
        """位置键：有显式 position 就用它，否则回落到预取时的 state.seq。"""
        return self.seq if self.position is None else self.position


@dataclass
class _Outcome:
    """decide() 的落地形态：要么给出 candidate_id，要么"已经记过一次异常"。"""

    candidate_id: str | None = None
    error: bool = False


class Director:
    """三段式协调器（§6.4）：开始预取 → 播放期间在后台算 → 边界上消费。"""

    #: 每个边界恰好落进一个出口（§6.5）。"applied" 含**两种载荷**：变体装上了、
    #: 或者路线被交给了状态机 —— 对课堂来说两者都是"这个边界上落实了一条决策"。
    #: 不给路线另立一个计数器：观测面（web.py 的 live 快照 / test_script_tree）按这张
    #: 名单展示，多一个键就要跟着改那张名单，而"路线被选中"与"变体被装"在复盘里是同一件事。
    _STAT_KEYS = ("prefetched", "applied", "none", "timeout", "stale", "error")

    def __init__(
        self,
        pool: CandidatePool,
        decider: DecisionProvider,
        *,
        grace_ms: int = 300,
    ) -> None:
        self.pool = pool
        self.decider = decider
        #: 边界上补等的上限。它的存在只为一件事：让"单元播放期间学生插话"造成的
        #: 最后时刻修正有机会赶上来；再长就变成让学生等模型了。
        self.grace_ms = grace_ms
        self._slot: _Slot | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stats: dict[str, int] = {k: 0 for k in self._STAT_KEYS}

    # -- ① 预取 ------------------------------------------------------------
    def prefetch(
        self,
        unit: LectureUnit,
        state: ClassroomState,
        *,
        target_unit: str | None = None,
        position: object | None = None,
    ) -> None:
        """单元开始播放时调用：立刻发请求，不 await（§6.4 ①）。

        槽位可以被安全覆盖 —— 覆盖意味着"上一格已经翻篇"，旧 task 顺手取消：
        它算的是上一个单元的选择，留着只会白烧一次调用，还会一直挂在事件循环上。

        position 是调用方掌握的位置键（见 _Slot）：给了就以它为准，边界上完全不看
        state.seq；不给就沿用 state.seq 的老语义。两种调用点可以并存，因为
        老调用点（单元内没有逐句旁白计数时）本来就没有更好的位置概念。

        target_unit 是"这一批候选给哪一格用"：不传 = 就是 unit 自己（老契约）。
        传了就把两件事分开 —— **事实仍从 unit 派生**（"这格学生卡没卡"），
        **候选按 target_unit 筛**（"下一格用哪种讲法"）。这不是可选的方便参数：
        §4.4 里 target_unit 空串的含义是"任意单元"，池子里一旦同时有给 N+1 和给 N+5
        的货，空目标就会让 J 挑中一条本来给远期某一格备的变体，然后被装到下一格上
        —— 学生听到的是另一段内容的另一种说法，而且不报错。
        """
        want = unit.unit_id if target_unit is None else str(target_unit)
        facts = facts_from_state(state, unit)
        candidates = self.pool.candidates_for(want, facts)
        old = self._slot
        if old is not None and not old.task.done():
            old.task.cancel()
        task = asyncio.create_task(self._run_decide(facts, candidates))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._slot = _Slot(
            unit_id=unit.unit_id, target_unit=want, seq=state.seq,
            position=position, task=task,
        )
        self._stats["prefetched"] += 1

    async def _run_decide(
        self, facts: DecisionFacts, candidates: list[Candidate]
    ) -> _Outcome:
        """把 decider 的一切失败收敛成一个值。

        异常一旦逃出 task，就不只是"这次没换讲法"，而是一次没人观察的异步事故
        （事件循环只会在关掉时打一行告警，而那时课早就上完了）。
        """
        try:
            chosen = await self.decider.decide(facts, candidates)
        except Exception:
            self._stats["error"] += 1
            return _Outcome(error=True)
        return _Outcome(candidate_id=chosen or None)

    # -- ③ 边界消费 --------------------------------------------------------
    async def apply_at_boundary(
        self,
        unit: LectureUnit,
        state: ClassroomState,
        *,
        target_unit: str | None = None,
        position: object | None = None,
    ) -> dict | None:
        """单元边界调用：能应用就返回变体 dict，任何不确定都返回 None（= 用 v0）。

        每一次调用恰好落在 applied / none / timeout / stale 之一，
        异常另记 error（发生即记一次，不重复记）：计数是课后唯一能复盘 J 的证据，
        所以四个出口不能有两条路合成一个数。

        position 必须与 prefetch 时给的那个一致；给了它之后位置校验就完全不看
        state.seq（理由见 _Slot：逐句旁白会把 seq 推上去，那不是"位置变了"）。
        target_unit 也必须与 prefetch 时给的一致：不一致说明"下一格"已经换了一格
        （翻页 / 回退），那条货是给旧目标备的，装上去就是讲串 —— 判 stale，用 v0。
        返回 dict 里 "seq" 仍然是当时的 state.seq（machine 侧还要用），
        "position" 是这次判定用的位置键，"target_unit" 是这次选的是给哪一格用的货
        （不传时等于 unit.unit_id）。

        选中的若是**路线**候选（cand.route）：返回一张"路线交接单"，形状见下面
        那一段注释。变体 dict 的形状一个字都没改（零回归）—— 路线不挤进那个契约，
        因为变体 dict 一旦多出"也可能是一条路线"的分支，每个读者都要学会分辨。
        """
        want = unit.unit_id if target_unit is None else str(target_unit)
        slot = self._slot
        self._slot = None            # 一格只消费一次，失败也不留给下一个边界
        if slot is None or slot.consumed:
            self._stats["none"] += 1
            return None
        slot.consumed = True

        task = slot.task
        if not task.done():
            await asyncio.wait({task}, timeout=max(0.0, self.grace_ms / 1000.0))
        if not task.done():
            self._stats["timeout"] += 1
            return None

        try:
            outcome = task.result()
        except (asyncio.CancelledError, Exception):
            self._stats["error"] += 1
            return None
        if outcome.error:
            return None                  # 已在 _run_decide 里记过 error
        boundary_key = state.seq if position is None else position
        if slot.unit_id != unit.unit_id or slot.key != boundary_key:
            self._stats["stale"] += 1     # 位置变了（插话导致让位/归位），决策作废
            return None
        if slot.target_unit != want:
            # 目标单元变了（翻页 / 回退之后"下一格"已经不是当初备货的那一格）——
            # 照着旧目标换讲法，学生会听到另一段内容的另一种说法。作废 = 用 v0（§8.3）。
            self._stats["stale"] += 1
            return None
        if not outcome.candidate_id:
            self._stats["none"] += 1
            return None

        cand = self.pool.get(outcome.candidate_id)
        if cand is None or cand.status != "alive":
            self._stats["none"] += 1      # 池里被过期/被别人消费掉了
            return None
        route = cand.route
        if route is not None:
            # 路线候选（skip / revisit / finish）：它改的不是"这一格怎么讲"，而是
            # "下一步去哪" —— 这是一件变体 dict 表达不了的事，所以交接单里放的是
            # **Route 自己的全部字段**（route_id/kind/fork_node/merge_node/steps/
            # est_sec/reason，一次给全，见 director/route.py），状态机用
            # Route.from_dict 就能原样还原。
            #
            # 判别键 payload="route"：变体 dict 没有这个键，于是"这是路线还是变体"
            # 只有一个判据，不必去数哪个字段在不在。加一个键而不是改形状，是因为
            # 变体 dict 的读者（machine 的事件、web 的复盘、测试里的等号）一个都不许动。
            #
            # **consumed 的语义在这里变了，这是 S3 唯一的契约改动**：
            # S1 时这里是"返回 None 且保持 alive"，因为那时没有任何执行器 —— 消费掉
            # 等于把一条路线静默销毁，图上会显示"播过"而课堂什么都没发生。
            # 现在状态机真的会按它走：交接出去的那一刻它就是"已经用在课堂上了"；
            # 不消费反而会让同一条路线在每个边界上被反复选中、反复执行
            # （revisit 会回头重讲，重复执行直接吃课堂时间）。
            self.pool.mark_consumed(cand.candidate_id)
            self._stats["applied"] += 1
            return {
                "payload": "route",
                "candidate_id": cand.candidate_id,
                **route.to_dict(),
                # 与变体 dict 同名同位的那几个字段：交接单的读者（状态机的事件与台账）
                # 两条路走同一套日志口径，课后不必分辨"这次的 site 从哪来"。
                "unit_id": unit.unit_id,
                "target_unit": want,
                "seq": state.seq,
                "position": state.seq if position is None else position,
            }
        variant = cand.variant
        self.pool.mark_consumed(cand.candidate_id)
        self._stats["applied"] += 1
        out = {
            "variant_id": variant.variant_id,
            "label": variant.label,
            "candidate_id": cand.candidate_id,
            "unit_id": unit.unit_id,
            "seq": state.seq,
            "position": state.seq if position is None else position,
            "text": variant.text,
            "anchors": list(variant.anchors),
            "ops": list(variant.ops),
            "est_sec": variant.est_sec,
        }
        # "这条货是给谁备的"无条件写回：不传 target_unit 时它等于 unit.unit_id（老语义）。
        # 不做条件式加键 —— 返回形状随参数变化，所有读者（事件、日志、复盘脚本）都得写
        # d.get() 兜底，而那种分支没有测试守着。契约加字段时同步改契约测试，与当初加
        # position 是同一类改动、同一种做法。
        out["target_unit"] = want
        return out

    # -- 收尾与观测 --------------------------------------------------------
    def close(self) -> None:
        """下课或退出时调用：取消所有未完成的预取 task，不留悬挂 task。

        留着它们的后果不是内存泄漏，而是"课结束了还有一次模型调用在飞"，
        退出时那条告警会把真正的错误淹掉。
        """
        self._slot = None
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    @property
    def stats(self) -> dict:
        """计数的意义：课堂上没人看得见 J，只有这几个数能说明它到底有没有用。"""
        return dict(self._stats)
