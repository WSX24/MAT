"""候选池：J 能选的东西。

只有 J 读、只有 C 写（P 的预置变体在开课前批量灌入）。做成单一真相的好处是
"这一格为什么换了讲法"永远能在一个 dict 里复盘，而不是散在三个 agent 各自的记忆里
—— 课堂上没有时间对账，只有一个地方能查才有意义。

池里装两种载荷，恰好一个：**变体**（就地重讲这一格）与**路线**（跨几格的走向）。
为什么不给路线新开一个池：J 在单元边界上做的是"一次选择"，它的输入是**一个**
有序列表；两个池意味着两处排序规则、两处 CAP、两处幂等，而它们会各自漂移 ——
同一件事有两个记账处，最后没人说得清课堂到底听了谁的。所以纪律共用：
CAP=5、幂等覆盖、version 自增、alive/consumed/expired、摘要 ≤30 字。

**满了怎么办：当期刚需挤掉最远的未来备货。** 池满时一律拒收新 id 的后果是实测出来的：
C 每 60s 醒一次、每次给未来 5~15 格各备 2 条货（expires 是目标格 + 2，要约 7 格后才过期），
三次唤醒就把 CAP=5 占满，于是**当期**的刚需（下一格的重讲、这个边界的路线）全部饿死，
39 个边界的默认节奏里只有 1~2 个能选到路线（修复后实测 31 个），而账上只表现为
"后面再没换过讲法"。
判据只用已有的 expires_at_seq 做单调比较（不引入新时钟）：越靠当期 expires 越小，
所以"新候选的 expires 严格小于牺牲品"就等于"当期优先"，见 put 与 _eviction_victim。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .facts import DecisionFacts, matches
from .route import Route

#: 摘要上界（§6.2）：J 一次只看 5 条 × 30 字。它要选，不要读作文。
SUMMARY_CHARS = 30

#: 喂给 J 的候选条数硬上限（§6.2）。与 CAP 分开写：
#: 池子容量可以为了"多备几条"而调大，但 J 的输入长度不许跟着涨。
J_CANDIDATE_LIMIT = 5

#: 驱逐的**期距上界**（单元数）：新候选只许挤掉 expires 不超过"自己的 expires + 它"的货。
#: 为什么需要它：空出来的名额是给**这个边界**用的，所以被挤掉的必须是"这个边界本来会读到
#: 的那一档远期备货"—— C 的上货窗口是当前+5 ~ 当前+15（producer.LATEST_OFFSET），再加上
#: EXPIRE_MARGIN_UNITS 就是 17 格；比这更远的货不属于任何一次上货。
#: 代价：期距超过它的货挤不动（宁可让它自然过期）—— 现实里只有哨兵值（expires=99）会撞到
#: 这条线，而把它挤掉等于让夹具 / 回填的数据被静默作废。
#: 与 producer 的那两个常量必须一致（同步由 tests/test_pool_eviction.py 的期距那一条钉住）。
EVICT_HORIZON_UNITS = 17


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


@dataclass
class UnitVariant:
    """一个讲解单元的替代表述。v0 是主 text，其余都在这里（§4.1）。"""

    variant_id: str
    label: str
    text: str
    anchors: list[dict] = field(default_factory=list)   # 空 = 沿用主单元
    ops: list[dict] = field(default_factory=list)       # 空 = 沿用主单元
    est_sec: float = 0.0

    def __post_init__(self) -> None:
        """est_sec 缺省时按字数派生（§4.1）。

        放在这里而不是每个用它的地方：0 秒的变体是一句谎话 ——
        J 会拿它估代价、机器会拿它排时长。让它根本流不出去，比在每个调用处补默认值可靠。
        """
        if not self.est_sec:
            self.est_sec = round(len(self.text) / 5.0, 1)

    @staticmethod
    def from_dict(d: dict) -> UnitVariant:
        """从 plan.json / C 的提案里还原；缺字段一律给默认值，不为一条脏数据打断上课。"""
        return UnitVariant(
            variant_id=str(d.get("variant_id") or ""),
            label=str(d.get("label") or ""),
            text=str(d.get("text") or ""),
            anchors=list(d.get("anchors") or []),
            ops=list(d.get("ops") or []),
            est_sec=float(d.get("est_sec") or 0.0),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self, *, max_chars: int = SUMMARY_CHARS) -> str:
        """给 J 看的一行摘要（label + 冒号 + 文本截断，整体 ≤max_chars）。

        只要标签和开头几个字：J 的任务是"选哪个"，不是"读懂每个变体"。
        摘要写长了等于让它在课堂边界上读作文，延迟上界立刻失守。
        """
        label = _clip(self.label.strip() or self.variant_id, max_chars)
        body = " ".join(self.text.split())
        budget = max_chars - len(label) - 1        # 冒号占一格
        if budget <= 0:
            return label
        if len(body) > budget:
            body = _clip(body, budget)
        return f"{label}：{body}" if body else label


@dataclass(frozen=True)
class Eviction:
    """一条驱逐账：**谁**被挤掉、它本来活到第几格、**被谁**挤掉。

    为什么要有这笔账：被驱逐的候选是置 TOMBSTONE 的（不删除，见 put），但"作废"
    与"从来没造过"在观测面上长得一模一样 —— 复盘时最常问的那句"这次为什么丢了那条
    备货"只能靠这笔账回答。它按发生顺序追加，谁都不能改（frozen），于是它能直接进
    metric 而不会变成第二个可写的真相。
    """

    candidate_id: str
    expires_at_seq: int
    evicted_by: str
    evicted_by_expires: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    """候选池里的一项（§4.4）。candidate_id 是幂等键，version 由池维护。

    **两种载荷，恰好一个**：variant（就地重讲这一格）或 route（跨几格的走向）。
    为什么不在字段上写死"路线也只能是一个变体列表"：路线要带走向、岔出/汇回格、
    以及"省还是花时间"的符号，硬塞进 UnitVariant 的话，这些字段对变体全是空的，
    而空字段没人校验 —— 空字段不会报错，只会让状态机走错。
    """

    candidate_id: str
    target_unit: str           # 空串 = 任意单元（纯条件触发）
    when: dict                 # 结构化触发条件，见 facts.matches
    source: str                # "preset" | "agent_c"
    created_at: float
    created_at_unit: str
    expires_at_seq: int
    variant: UnitVariant | None = None
    route: Route | None = None
    status: str = "alive"      # "alive" | "consumed" | "expired"
    version: int = 1

    def __post_init__(self) -> None:
        """互斥在这里判死：两个都空 = 没什么可选的，两个都满 = 不知道选中的是什么。

        为什么在构造时炸而不是让 J / 状态机各自兜底：下游兜底的结果是"某一种载荷
        悄悄不生效"，而课堂上没人看得见 J —— 一条永远不被选中的候选和一条不存在的
        候选长得一模一样，只会表现为"这个单元又没换讲法"。
        """
        if (self.variant is None) == (self.route is None):
            raise ValueError(
                "候选的变体与路线恰好一个："
                f"{self.candidate_id!r} 收到 variant={self.variant!r}, route={self.route!r}"
            )

    def summary(self, *, max_chars: int = SUMMARY_CHARS) -> str:
        """给 J 看的一行摘要（§6.2）。按载荷分派，长度上界两种载荷共用同一个。

        为什么不让路线自带更长的摘要：J 的输入长度是它延迟上界的来源，
        5 条 × 30 字是一条整体纪律；某一种载荷"值得多写几个字"的话，
        这条纪律就变成"看类型而定"，而边界上的等待会跟着候选的种类走。
        """
        if self.variant is not None:
            return self.variant.summary(max_chars=max_chars)
        return self.route.summary(max_chars=max_chars)


class CandidatePool:
    """候选池（§4.5）：单一真相，J 的输入从这里来。"""

    #: alive 硬上限：不是省内存，是给 J 的输入长度封顶（§6.2 的 5 条 × 30 字）。
    CAP = 5

    def __init__(self) -> None:
        self._items: dict[str, Candidate] = {}
        self._consumed_since_read = False
        self._evictions: list[Eviction] = []

    # -- 写 ---------------------------------------------------------------
    def _eviction_victim(self, expires_at_seq: int) -> Candidate | None:
        """此刻写入一条 expires=expires_at_seq 的新候选时该驱逐谁；None = 不该驱逐。

        两条判据都是**单调比较**（不引入新时钟），因此"越靠当期越优先"是可以证明的：
        当期刚需的 expires 小、远期备货的 expires 大。

        ① **严格小于才换**：等于时不动 —— 两条货都要服务同一个边界，换谁都没意义，
           只是把账搅动一次。
        ② **只挤够得着的那一档**：牺牲品的期距不许超过 EVICT_HORIZON_UNITS。空出来的
           名额是给**这个边界**用的，而被挤掉的必须是"本来会被这个边界读到的远期备货"
           （C 的上货窗口 +5~+15 就在这条线以内）；比那更远的货不属于任何一次上货，
           挤它等于拿这一格去赌别的课程段 —— 宁可让它自然过期。
           这一条同时挡住了"哨兵值"造成的误驱逐：把 expires 写成 99 表示"永远不过期"的
           数据（夹具、回填）不会因为一条当期货进池而被静默作废。

        判据写在池子里、由 put 与门卫（evictable_for）**共用同一把尺子**：门卫说能挤、
        put 却拒收的话，那一次模型调用就白花了。
        """
        alive = [c for c in self._items.values() if c.status == "alive"]
        if len(alive) < self.CAP:
            return None
        victim = max(alive, key=lambda c: c.expires_at_seq)
        if not expires_at_seq < victim.expires_at_seq:
            return None
        if victim.expires_at_seq > expires_at_seq + EVICT_HORIZON_UNITS:
            return None
        return victim

    def evictable_for(self, expires_at_seq: int) -> bool:
        """门卫用：池满时，一条 expires=expires_at_seq 的"当期货"挤得进来吗。

        与 put 共用 _eviction_victim（理由见那里）：门卫只回答"值不值得为这次唤醒花一次
        模型调用"，而"挤不挤得进来"必须由池子自己说 —— 两边各写一份判据，迟早在
        "门卫放行、put 拒收"处漂移。
        """
        return self._eviction_victim(expires_at_seq) is not None

    def put(self, cand: Candidate, *, allow_evict: bool = True) -> bool:
        """幂等写入：同 id 覆盖、版本 +1；alive 已满且是新 id → 见下。

        版本号不让写入方给：C 会在后台反复重写同一个候选，写入方各自记版本必然错位，
        池是唯一真相，版本也该由它数。

        **满了不再一律拒收**：当期刚需可以挤掉一条最远的未来备货（判据见
        _eviction_victim）。被挤掉的那条置 expired（TOMBSTONE，不删除）并记一笔 Eviction，
        于是"这条备货是怎么丢的"能复盘 —— 不然课堂上只表现为"后面再没换过讲法"。
        alive 恒 ≤ CAP 不变：驱逐只是把"满了"的拒收换成"换一条更当期的进来"，
        单一容量、单一真相，不给任何载荷开分区或 reserve 名额。

        allow_evict=False：这一条不许驱逐。给调用方留的闸门 —— 状态机按"一次唤醒最多
        一次驱逐"写池时，名额要留给最靠当期的那一条（见 agent/producer.py 的两遍写池）。
        """
        old = self._items.get(cand.candidate_id)
        if old is not None:
            cand.version = (old.version or 1) + 1
            self._items[cand.candidate_id] = cand
            return True
        if self.alive_count() >= self.CAP:
            victim = self._eviction_victim(cand.expires_at_seq) if allow_evict else None
            if victim is None:
                return False
            victim.status = "expired"
            self._evictions.append(Eviction(
                candidate_id=victim.candidate_id,
                expires_at_seq=victim.expires_at_seq,
                evicted_by=cand.candidate_id,
                evicted_by_expires=cand.expires_at_seq,
            ))
        cand.version = 1
        self._items[cand.candidate_id] = cand
        return True

    def mark_consumed(self, candidate_id: str) -> None:
        """选中即消费：同一个候选不能对两个单元生效两次。"""
        cand = self._items.get(candidate_id)
        if cand is None:
            return
        cand.status = "consumed"
        self._consumed_since_read = True

    def expire_stale(self, current_seq: int) -> int:
        """单元序号越过 expires_at_seq 即作废，返回作废条数。

        用严格小于：等于 current_seq 的候选还留给"下一格"，多留一格比早作废安全 ——
        早作废的后果是这一格明明有更好的讲法却只能讲 v0。
        """
        n = 0
        for cand in self._items.values():
            if cand.status == "alive" and cand.expires_at_seq < current_seq:
                cand.status = "expired"
                n += 1
        return n

    # -- 读 ---------------------------------------------------------------
    def get(self, candidate_id: str) -> Candidate | None:
        return self._items.get(candidate_id)

    def items(self) -> list[Candidate]:
        """池内全部候选的快照（只给观测面用，调用方不得修改返回的对象）。

        为什么要有这个方法，而不是让观测面自己去摸私有字段：那个 dict 是这个类自己的
        记账结构，直读它的代码会随"池要不要多加一层索引"一起碎掉；更要紧的是，
        能摸到它的观测面顺手删掉一条，改的就是课堂正在用的那份单一真相 —— 一个
        查看页不该有这种权限。返回 list 只挡住"往列表里塞/删条目不回写池子"这一种
        改法，列表里的元素仍是池里的对象，所以"不得修改"是契约而不是保险。
        """
        return list(self._items.values())

    def alive_count(self) -> int:
        return sum(1 for c in self._items.values() if c.status == "alive")

    def candidates_for(self, unit_id: str, facts: DecisionFacts) -> list[Candidate]:
        """该单元此刻能用的候选（§8.2）：alive + 目标单元匹配 + 条件匹配，排序后硬截断 5 条。

        排序 agent_c 优先：它是照当下这个学生造的，比开课前批量灌的预置更贴题；
        同源按新近，因为后造的候选看到的状态更完整。
        末尾的截断用 J_CANDIDATE_LIMIT 而不是 CAP：两者是两道独立保险，
        池子容量为了多备几条而调大时，J 的输入长度依然不许涨。
        """
        alive = [
            c
            for c in self._items.values()
            if c.status == "alive"
            and (not c.target_unit or c.target_unit == unit_id)
            and matches(c.when, facts)
        ]
        alive.sort(key=lambda c: (c.source != "agent_c", -c.created_at))
        return alive[:J_CANDIDATE_LIMIT]

    def status(self) -> dict:
        """给门卫与日志看的三态计数。"""
        out = {"alive": 0, "consumed": 0, "expired": 0}
        for cand in self._items.values():
            if cand.status in out:
                out[cand.status] += 1
        return out

    def evictions(self) -> list[Eviction]:
        """驱逐账（只读快照）：谁被挤掉、本来活到第几格、被谁挤掉 —— 复盘用。

        返回 list 副本而不是内部那个列表：观测面（tick / metrics / 复盘脚本）顺手
        append 一条就等于伪造了一笔课堂没发生过的驱逐，而它看起来和真的一模一样。
        Eviction 本身是 frozen 的，所以副本里的元素改不动。
        """
        return list(self._evictions)

    def eviction_count(self) -> int:
        """驱逐过几条（单调递增）：tick 用它算"这一次唤醒驱逐了谁"。"""
        return len(self._evictions)

    def just_consumed(self) -> bool:
        """自上次读取以来是否发生过消费，读后清零（§7.3 门卫用）。

        读后清零而不是让调用方自己复位：门卫是每 60s 醒一次的循环，
        忘了复位就会永远以为"刚消费过"，于是再也不叫 C 上货。
        """
        flag = self._consumed_since_read
        self._consumed_since_read = False
        return flag
