"""Agent-C（造分支）的一次唤醒流水线 —— 方案 §7.2 的 `tick()`。

C 是"上课中的造分支角色"（§7.1）：课堂正在实时进行，它每 60s 醒一次，判断
"这一刻值不值得花一次模型调用、给这个学生多造几条讲法"；值得就造、造完校验、
校验过才写进候选池（池子只有 J 读、只有 C 写）。

三条被写进代码形状的硬约束
--------------------------
1. **门卫在前，模型在后**（§7.3）：40 分钟 40 次唤醒，只有 8~15 次真调模型。
   门卫是纯 Python、≈0 成本，顺序反过来就等于先花掉那 25 次多余调用再判断。
2. **无私有记忆**（§7.5）：本模块不持有任何跨唤醒状态。唯一需要"上次"的地方
   （"相对上次新出现的困惑"）折中成调用方显式传入的 `GateWatermark` —— 它是
   **水位**（看到多少信号了），不是决策记录（上次造了什么、要不要造）。收益是
   可重启、可复现、可审计：同样的快照 + 同样的水位，输出必须一样。
3. **幂等写池**（§7.2 ⑥）：同一个提案在每 60s 一次的重放里必须映射到同一个
   candidate_id，于是 `CandidatePool.put` 走覆盖分支（版本 +1、alive 不涨）。
   否则池子每 60s 长一条，5 分钟撞满 CAP=5，门卫从此永远否决 —— 内容完全正确
   的候选，会因为记账方式把自己锁死在门外。

本模块只做流水线，不做真实模型接入：`ProposalProvider` 是注入的异步接口，
`TemplateProposer` 是零网络的规则实现，整条链路因此可以完全离线驱动。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

from deck.schema import DeckDoc
from director.facts import DecisionFacts, facts_from_state, matches
from director.pool import Candidate, CandidatePool, UnitVariant
from director.route import Route, RouteStep, derive_route_id
from models import LessonScript
from state import ClassroomState, LectureUnit

from .profiles.directing import DIRECTING
from .variants import make_variants, validate_variant

# ===========================================================================
# 参数（§10 参数表）
# ===========================================================================

#: 上货窗口：当前单元 +5 起、+15 止（§7.4）。+5 是容错窗口 —— C 连续失败 5 个
#: 单元（80~180 秒）内，J 手上仍有预置变体可用。
EARLIEST_OFFSET = 5
LATEST_OFFSET = 15

#: 停止期阈值（§7.1）：剩余单元不足 5 个时 C 不再上货，造了也来不及被选中。
MIN_REMAINING_UNITS = 5

#: 连续探针超时的开闸阈值（§7.3）：一次超时可能只是走神，连着两次才算"卡住"。
PROBE_TIMEOUT_STREAK = 2

#: 一次模型调用的时间预算。取 20s 而不是 60s：唤醒周期本身就是 60s，一次调用
#: 如果活到周期末尾，下一次唤醒会和它撞在一起（同一格被两个唤醒同时上货）。
#: 超时就放弃，下个周期重试 —— §11 的口径是"学生无感"。
PROPOSER_TIMEOUT_S = 20.0

#: 变体字数上限。与 P 的尺子同源（§5.4 / CLASSROOM_UNIT_CHAR_WARN）：
#: 两个角色用两把尺子，就会出现"P 合规、C 不合规"的变体在课堂上被 J 选中。
CHAR_WARN = 250

#: 候选的存活余量（单元数）：目标单元之后再留两格。
#: 留 0 会让候选在目标单元刚讲完就作废 —— 而学生"没懂"常常在下一格才表达出来；
#: 留太多则占着 alive 名额把池子撑满（门卫的 pool_full 否决），两格够用。
EXPIRE_MARGIN_UNITS = 2

#: when 允许的三种 key（§4.6：先跑稳再扩）与五种算子。
WHEN_KEYS: tuple[str, ...] = ("consecutive_confused", "probe_result", "comprehension")
WHEN_OPS: tuple[str, ...] = (">=", "<=", "==", "in", "not_in")

#: 候选来源一律记这个值：池子里"这条是谁造的"必须能一眼分开（§4.4）。
SOURCE = "agent_c"

#: 自包含输入的字数预算（§7.5 约 3000 token）。中文约 1 字 1 token，
#: 所以按字数卡预算即可；写死在模块里而不是交给调用方 —— 上界是硬约束。
PROMPT_CHAR_BUDGET = 3000

#: 窗口单元原文的截断长度。模型要的是"这一格讲什么"，不是全文；
#: 全文会把窗口里其余单元挤掉（窗口最多 11 个）。
_UNIT_TEXT_CHARS = 80


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


# ===========================================================================
# ① 快照（零副作用）
# ===========================================================================

@dataclass(frozen=True)
class Snapshot:
    """一次唤醒看到的课堂状态（§7.2 ①）。

    `frozen=True` 不是风格问题：C 无私有记忆（§7.5），它手上只该有"这一刻的
    一张照片"。可变快照会被下游悄悄当成缓存改，于是"可复现"这条就假了。

    `current_seq` 是**单元序号**（`script.units` 里的下标），不是
    `ClassroomState.seq`（那是"说了第几句话"的计数器）。§7.4 明确用单元序号算
    窗口：时间戳会漂移（学生插话、TTS 抖动、探针窗口可变），而"还有几格可用"是确定的。
    """

    current_unit: str                 # 当前正在播的 unit_id（"" = 位置没落在单元上）
    current_seq: int                  # 当前单元的序号；0 = 课首/找不到
    slide: int                        # 当前单元所在页（0 基）
    total_units: int                  # 全课单元数
    remaining_units: int              # 当前单元之后还剩几个单元
    confused_count: int               # 本课累计困惑信号数（= 被判"没懂"的知识点数）
    deferred: tuple[str, ...] = ()    # 待回看列表（快照里存不可变副本）
    probe_result: str = ""            # 最近一次**有结论**的探针结果
    probe_timeout_streak: int = 0     # 从最近往前数，连续超时了几次
    last_question: str = ""           # 最近一次学生提问原文
    previous_question: str = ""       # 再上一条学生提问原文（判"重复追问"用）

    @property
    def question_repeat(self) -> bool:
        """最近两条学生提问同文 = 重复追问（§7.3 的第四条信号）。

        为什么用"同文"这个最朴素的代理：判"追问"要么理解语义（那就要模型，
        而门卫的全部价值是零成本），要么比字符串。比字符串不会误判成"卡住"，
        最坏是漏判一次 —— 漏判的代价是少造一条候选，学生无感（§11）。
        """
        return bool(self.last_question) and self.last_question == self.previous_question


def snapshot_from(state: ClassroomState, script: LessonScript) -> Snapshot:
    """从课堂状态派生快照（只读，零副作用）。

    为什么只读 `get_state`、不消费 `get_events`（§7.7）：EventBus 是消费即消失的，
    C 拿走的那一条，J 就永远看不到 —— 两个消费者会互相偷事件。所以 C 只看
    幂等、可反复读的状态。

    各字段的派生口径与理由：
    - **当前单元**取 `unit_of(slide_index, seg_index)`；落在两页之间（翻页瞬间）时
      退回本页第一个单元 —— 快照宁可指向一个邻近单元，也不要变成空的：
      空快照会把窗口算到课首，等于给最早几格重复上货。
    - **confused_count** 数 `state.comprehension` 里值为 confused 的键数（与
      `facts._worst_comprehension` 同源）。同一知识点反复被判没懂不重复计数，
      但水位只需要"有没有变化"，重复不重复不影响门卫的判断。
    - **probe_result / probe_timeout_streak**：Probe 是"先 append 后 close"的
      （machine 的 `_close_probe`），所以 `probe_log[-1]` 可能是一条还没表态的
      空记录。未闭合的探针**跳过不算**（它不表示学生任何态度）；连续超时只数从
      最近往前数的一串 timeout。
    - **提问原文**取 transcript 里最近两条 student 发言：重复追问的"上一次"本来就在
      同一堂课的台账里，不需要 C 自己记（§7.5）。
    """
    unit = script.unit_of(state.slide_index, state.seg_index)
    if unit is None:
        unit = script.first_unit_of(state.slide_index)
    current_unit = unit.unit_id if unit is not None else ""

    current_seq = script.order_index(current_unit) if current_unit else -1
    if current_seq < 0:
        current_seq = 0          # 脚本里找不到：按课首算，窗口不会退化成越界

    total = len(script.units)
    remaining = max(0, total - (current_seq + 1))

    probe_result = ""
    streak = 0
    counting = True
    for probe in reversed(state.probe_log):
        if not probe.result:
            continue
        if not probe_result:
            probe_result = probe.result
        if counting:
            if probe.result == "timeout":
                streak += 1
            else:
                counting = False

    asked = [
        u.text.strip()
        for u in state.transcript
        if u.speaker == "student" and u.text.strip()
    ]

    return Snapshot(
        current_unit=current_unit,
        current_seq=current_seq,
        slide=unit.slide if unit is not None else state.slide_index,
        total_units=total,
        remaining_units=remaining,
        confused_count=sum(1 for v in state.comprehension.values() if v == "confused"),
        deferred=tuple(state.deferred),
        probe_result=probe_result,
        probe_timeout_streak=streak,
        last_question=asked[-1] if asked else "",
        previous_question=asked[-2] if len(asked) >= 2 else "",
    )


# ===========================================================================
# ② 规则门卫（§7.3）
# ===========================================================================

@dataclass
class GateWatermark:
    """门卫水位：调用方拥有，"上次已经消费到多少信号"。

    为什么它必须存在、又必须不在 producer 里（§7.5）："新出现的困惑"天然需要
    "相对上次"，而 C 不许有私有记忆。折中是把它做成**显式传入的水位**：只记计数
    （看到了几条困惑、几条待回看），不记任何"上次造了什么、这次要不要造"。
    前者是计量，后者才是决策记忆 —— producer 因此仍是纯函数式的：
    同样的快照 + 同样的水位 → 同样的结论。
    """

    confused_count: int = 0
    deferred_len: int = 0


#: 池满但仍可以挤掉一条远期备货时，开闸理由里带的那段标记（叠加在信号名之后）。
#: 它替掉的是改动前那句一律的 veto：池满不再等于"不许上货"，只等于"这次要挤"。
#: 信号名不被吞掉（理由串是拼接而不是替换）：指标里"为什么调用了模型"还要能分开统计。
REASON_POOL_FULL_EVICTABLE = "pool_full_evictable"


def boundary_need_expires(current_seq: int) -> int:
    """当期刚需的 expires 上界：**这个边界**的路线候选，即 fork + 1 + 余量。

    为什么"当期需求"取这一个数：C 一次唤醒里最靠当期的那条货就是路线（它锚在当前格、
    target 是下一格，见 _to_route_candidate ⑤）。上货窗口里的变体从"当前 + 5"起，
    expires 必然更大 —— 它们本身就是**未来备货**，拿它们去挤另一条未来备货只是把池子
    搅动一次，学生听到的东西一个字都没变（与 pool._eviction_victim 的判据互相印证）。

    于是"当期"与"备货"的分界线只有一条：expires 是否 ≤ 本边界的需求。判据仍旧只比
    expires_at_seq，不引入新时钟（§4.4 的记账尺子）。
    """
    return max(0, current_seq) + 1 + EXPIRE_MARGIN_UNITS


def gate_open(
    snapshot: Snapshot, pool: CandidatePool, watermark: GateWatermark
) -> tuple[bool, str]:
    """规则门卫（§7.3）：这次要不要调模型。返回（是否开闸，一句话理由）。

    理由串直接进指标（"pool_full" / "too_close_to_end" / "no_signal" /
    "new_confused" / ...）：课堂上没人看得见 C，只有这几个数能说明它为什么没干活。

    **池满不再一律否决（这是实测出来的那条）**：改动前池满即 `pool_full`，于是 C 连
    "写一条当期货"的机会都没有 —— 而池子恰恰是被 C 自己的远期备货占满的（每次唤醒给
    未来 5~15 格各备 2 条，三次唤醒就撞满 CAP=5）。结果是当期刚需全部饿死：给下一格装
    预置变体时写入 0 条，J 在一个边界上什么都看不见，连续七八格只能讲 v0。
    现在：池满但**挤得动**（存在比当期需求更远的活货，判据由池子给：pool.evictable_for）
    → 开闸，理由串带 REASON_POOL_FULL_EVICTABLE 标记；池满且全是当期货 → 仍旧 veto
    `pool_full`。于是"一次模型调用"仍然只花在真有地方放货的时候，而不会刷模型。

    顺序不是随意的：
    - 两条否决先判（池满只在**挤不动**时才算否决），且必须**在碰任何有副作用的读
      之前**判完 ——
      `pool.just_consumed()` 是读后清零的，先调它会把"刚腾出位置"这条信号吃掉。
    - 五条信号里 `just_consumed()` 放最后，理由同上；其余四条都是纯读，
      谁先谁后只影响理由串（固定顺序才能让指标可比较）。
    - 水位**只在开闸时**推进：关闸（池满 / 快下课）期间攒下的困惑信号要留到下次
      有机会时再兑现，否则那一次困惑就被"池子当时满了"永久吞掉了。
    """
    full_evictable = False
    if pool.alive_count() >= CandidatePool.CAP:
        # 池满：只有当"这个边界的当期货"真的挤得掉一条更远的活货时才放行。
        # 判据交给池子（pool.evictable_for 与 put 共用一把尺子）：门卫在这里说能挤，
        # 后面 put 就真的挤得进去 —— 否则这一次模型调用是白花的。
        if not pool.evictable_for(boundary_need_expires(snapshot.current_seq)):
            return False, "pool_full"
        full_evictable = True
    if snapshot.remaining_units < MIN_REMAINING_UNITS:
        return False, "too_close_to_end"

    reason = ""
    if snapshot.confused_count > watermark.confused_count:
        reason = "new_confused"
    elif len(snapshot.deferred) > watermark.deferred_len:
        reason = "deferred_grew"
    elif snapshot.probe_timeout_streak >= PROBE_TIMEOUT_STREAK:
        reason = "probe_timeout_streak"
    elif snapshot.question_repeat:
        reason = "repeated_question"
    elif pool.just_consumed():
        reason = "just_consumed"

    if not reason:
        return False, "no_signal"

    watermark.confused_count = snapshot.confused_count
    watermark.deferred_len = len(snapshot.deferred)
    if full_evictable:
        # 信号名 + 标记：两件事都要在账上（为什么调用了模型 / 这一次是不是要挤人）。
        return True, f"{reason}+{REASON_POOL_FULL_EVICTABLE}"
    return True, reason


# ===========================================================================
# ③ 上货窗口（§7.4）
# ===========================================================================

def target_window(
    current_seq: int,
    total: int,
    *,
    earliest: int = EARLIEST_OFFSET,
    latest: int = LATEST_OFFSET,
) -> range:
    """上货窗口（§7.4）：[当前+5, 当前+15] 与 [0, total) 的交集，单位是单元序号。

    为什么用序号不用时间戳：时间戳会漂移（学生插话、TTS 抖动、探针窗口可变），
    而"还有几格可用"是确定的。

    接近课末时窗口被 clamp 到 total；当前+5 已经越过课末时返回**空 range** ——
    空窗口 = 这次没有可上货的目标，调用方据此直接收工，不必再拼 prompt、更不必
    调模型。闭区间 [+5, +15] 共 11 格（对应 `units[seq+5 : seq+16]`）：
    §7.2 伪代码里写的 `units[seq+5 : seq+15]` 是同一意图的简写，少算了 +15 那一格，
    而 §7.4 自己的 `hi = current + LATEST_OFFSET + 1` 才是要表达的东西。
    """
    start = max(0, current_seq)
    lo = min(start + max(0, earliest), total)
    hi = min(start + max(0, latest) + 1, total)
    if hi < lo:
        hi = lo
    return range(lo, hi)


# ===========================================================================
# ④ 自包含输入（§7.2 ③ / §7.5）
# ===========================================================================

#: 输出契约。**改它之前先读完这段注释。**
#:
#: 为什么顶层是**对象**而不是数组
#: ------------------------------
#: `LlmProposer` 调用时带 `json_mode=True`，它落地成
#: `response_format={"type": "json_object"}` —— 在**协议层强制顶层是对象**。
#: 提示词若同时要求"只输出一个 JSON 数组"，模型就被夹在两个互相矛盾的要求之间，
#: 而它不会报错：实测（真实 key + 真实 prompt + 真实课件窗口）交出来的是把协议参数
#: **原样回显** —— `{"type": "json_object", "content": []}`（38 字），
#: `parse_proposals` 于是抛"对象里没有提案数组"，C 记成一次 bad_payload。
#:
#: 同一份 prompt 的三组对照（同一个模型）：
#:   · `json_mode=True` + 要数组 → 回显协议参数（上面那段垃圾）
#:   · `json_mode=False`（自由文本）→ 正常返回数组
#:   · `json_mode=True` + 要 `{"proposals": [...]}` → 正常返回，且产出通过
#:     `validate_variant` 体检
#: 结论：矛盾在提示词侧，不在模型侧。所以形状必须与 `json_mode` 一致，写成
#: `{"proposals": [...]}` —— 这恰好也落在 `parse_proposals` 已支持的对象包裹形态里
#: （另外两种是纯数组与 ```json 围栏）。
#:
#: 离线用例**抓不到**这类回归（假 client 根本不看提示词），只有真实模型才会现形，
#: 于是这条契约只能靠 tests/test_producer.py 反过来盯提示词本身。
#:
#: 为什么路线的形状也写在这一块里
#: ------------------------------
#: 路线是同一个 proposals 数组里的一条（载荷字段从 variant 换成 route），不是新的顶层
#: 键 —— 顶层再多一个键就等于把"一次唤醒返回什么"变成两套解析。所以第二段示例只换
#: 载荷，并且把四件模型最容易搞错的事写死：三选一的 kind、fork/merge 的包含关系、
#: revisit 的 steps 非空、以及 est_sec 的**符号**（省写负、花写正）。
#: 结构校验不指望提示词：越界与非法结构一律由 _to_route_candidate 挡在池子外面。
_PROMPT_TAIL = """
【你要输出的东西】
只输出一个 JSON 对象：顶层**只能有 proposals 这一个键**，值是提案数组。不要 markdown 代码块、不要任何解释，整个回复就是这个 JSON。形状（一条提案只给变体或路线其中一种）：
{"proposals": [{"target_unit": "上面窗口里出现过的某个 unit_id", "variant": {"variant_id": "c1", "label": "换个说法", "text": "能直接播出来的完整讲解，不超过 250 字", "anchors": [], "ops": []}, "when": {"consecutive_confused": {">=": 2}}, "reason": "为什么造这一条"}]}
要造**路线**（跨好几格的走向：跳过几格 / 回头补讲 / 提前收尾）时，variant 写空对象，route 一次给全：
{"proposals": [{"target_unit": "上面窗口里出现过的某个 unit_id", "variant": {}, "route": {"kind": "skip", "fork_node": 8, "merge_node": 11, "steps": [], "est_sec": -96.0, "reason": "这段已确认听懂，跳过两格直接接上"}, "when": {"comprehension": {"==": "confirmed"}}, "reason": "为什么造这一条"}]}
硬约束：顶层只能有 proposals 这一个键；when 的 key 只能取 consecutive_confused / probe_result / comprehension，算子只能取 >= <= == in not_in；target_unit 必须是上面窗口里出现过的 unit_id；路线的硬约束 —— kind 只能取 skip / revisit / finish，必须 0 <= fork_node < merge_node <= 课程总格数，revisit 的 steps 必须非空（要回头重讲的那几格，每格给 unit_id），skip / finish 的 steps 留空数组，merge_node 是汇回的那一格、finish 的 merge_node 必须等于总格数，est_sec 是时间代价、skip / finish 写负数（省下的秒）、revisit 写正数（多花的秒），fork_node 必须就是【课堂快照】里的当前单元序号（路线只服务**这个边界**：刚讲完这一格，接下来往哪走）、路线的 target_unit 必须是它后面那一格（= 当前单元的下一格）的 unit_id；variant.text 必须是这一单元的另一种讲法（讲同一个知识点），不是"你再讲一遍"这种指令。
"""


def build_prompt(
    snapshot: Snapshot, window: Sequence[LectureUnit], pool_status: dict
) -> str:
    """组装自包含输入（§7.2 ③）：固定系统提示 + 当前快照 + 窗口脚本 + 池子现状。

    为什么"自包含"是硬要求：C 没有会话历史（§7.5），每一次调用都必须只看这一个
    字符串就能判断。漏掉任何一块（尤其是窗口里的原文），模型只能靠猜，而猜出来的
    变体与目标单元讲的可能不是同一个知识点 —— 学生会听到"老师突然讲别的了"。

    字数按 §7.5 的 3000 token 量级裁剪，且**输出要求整块拼在裁剪之后**：如果先拼
    后裁，第一个被裁掉的恰好是"必须输出什么"，那时模型不会报错，只会返回一句解释。
    """
    lines: list[str] = [
        DIRECTING.system_prompt,
        "",
        "【课堂快照】",
        f"当前单元 {snapshot.current_unit or '（未开始）'}（第 {snapshot.slide + 1} 页），"
        f"序号 {snapshot.current_seq}/{snapshot.total_units}，剩余 {snapshot.remaining_units} 个单元",
        f"本课累计困惑 {snapshot.confused_count} 次 | 待回看 {len(snapshot.deferred)} 条"
        f" | 最近探针 {snapshot.probe_result or '无'}"
        f" | 连续超时 {snapshot.probe_timeout_streak}",
        f"最近学生提问：{snapshot.last_question or '（无）'}",
        "",
        "【可上货窗口】",
    ]
    for unit in window:
        kp = "、".join(unit.kp) or "无"
        text = _clip(" ".join(unit.text.split()), _UNIT_TEXT_CHARS)
        lines.append(f"- {unit.unit_id}（第 {unit.slide + 1} 页，知识点 {kp}）：{text}")

    lines.extend([
        "",
        "【候选池现状】",
        f"alive {pool_status.get('alive', 0)}/{CandidatePool.CAP}"
        f" | consumed {pool_status.get('consumed', 0)}"
        f" | expired {pool_status.get('expired', 0)}"
        f" —— 满 {CandidatePool.CAP} 条就停止上货，所以只造真正可能被用到的几条",
    ])

    body = "\n".join(lines)
    return _clip(body, PROMPT_CHAR_BUDGET - len(_PROMPT_TAIL)) + _PROMPT_TAIL


# ===========================================================================
# ⑤ 提案接口与离线实现
# ===========================================================================

@dataclass(frozen=True)
class ProposalRequest:
    """一次唤醒交给 proposer 的全部输入（自包含，无历史）。

    为什么 prompt 之外还要传结构化的东西：真模型只读 prompt，但离线的
    `TemplateProposer` 要按窗口单元的原文造变体 —— 让它去 prompt 里做正则解析，
    等于把"字符串格式"变成隐式契约，改一个字就会静默失效。

    路线把这份"结构化输入"的必要性推到了顶点：路线要 fork（岔出哪一格）、merge
    （汇回哪一格）、以及 revisit 的回头路 —— 这些是**课程结构**，不是窗口里那 11 格
    的原文能推出来的。所以下面三个字段补上：

    - `facts`：**当前这一格**的决策事实（unit_id / 连续卡壳 / 理解信号 / 重讲轮次）。
      "造哪一种走向"靠它判（见 route_kind），而快照里那几个数是课堂级的，判不了这个。
      必须是当前格而不是窗口里某一格：路线的 when 会在岔出边上被 J 重算一遍，
      两边拿的必须是同一个格子的信号 —— 否则 C 是拿 5 格前的状态去赌未来。
    - `units`：整份脚本的单元（order_index 序）。只给窗口装不下路线 —— revisit 要往
      **前**看、finish 要看到**课尾**。
    - `current_seq`：当前单元的 order_index，也就是路线的岔出格（route.plan_route 的
      fork_node）。路线是**这个边界**的选项：刚讲完第 N 格，接下来往哪走 —— 不是
      "给 5 格后备货"。锚在窗口首格（改动前的口径）会让每条路线在自己的边界到来
      之前就被下一次唤醒顶掉，学生在边界上永远看不见它（见 _make_room_for_route）。
    """

    prompt: str
    snapshot: Snapshot
    window: tuple[LectureUnit, ...]
    pool_status: dict
    facts: DecisionFacts | None = None
    units: tuple[LectureUnit, ...] = ()
    #: 当前单元的 order_index（= 路线的岔出格）。默认 -1 = 位置不明：plan_route 的
    #: `0 <= fork_node` 会挡掉它，于是"没摆位置"的调用点一个字的路线都不会造。
    current_seq: int = -1


class ProposalProvider(Protocol):
    """提案接口（可注入的模型调用）：给一份自包含输入，返回一批候选。

    返回类型故意写成 `object`：契约是 `{"proposals": [...]}` 对象（不是裸数组 —— `json_mode`
    在协议层强制顶层为对象，提示词要数组会让模型回显协议参数，见 `_PROMPT_TAIL` 的注释），
    但真模型会返回 None、纯字符串、
    甚至半截 JSON —— 那是**正常情况**，不是异常。所以逐条兜在 `run_tick` 里，
    而不是指望实现方保证形状。
    """

    async def propose(self, request: ProposalRequest) -> object:
        ...


class TemplateProposer:
    """离线的规则实现：零网络、零随机、零成本，让整条链路能跑通（§12 阶段 3）。

    为什么先有它：真模型接入是下一步的事，但门卫 / 窗口 / 校验 / 幂等写池这四件
    必须现在就能被测试驱动 —— 只有确定性的假模型，才能断言"这 40 次唤醒门卫开了
    几次""这条提案为什么被拒"。它造的变体来自 `make_variants`（拿课件原文换顺序），
    所以内容相关性与 P 的预置变体同一口径，不会凭空造句。

    变体按窗口顺序交替取"换说法 / 举例子"，`variant_id` 沿用 P 的 v1/v2 口径
    （同一个知识点的两档讲法）；candidate_id 再把单元带上，两段合起来才是
    "哪一格的哪一种讲法"。

    路线走同一份事实、同一套规则（`plan_route` → `route_proposal`），一次唤醒至多造
    **一条**：路线是"接下来往哪走"，同一时刻课堂上只可能执行一条岔路（理由见
    `_make_room_for_route`）。这也正是"离线可复现"那条验收落地的位置 —— 不调模型的
    路径能造出三种走向，才谈得上"规则写死且可解释"。
    """

    def __init__(self, *, per_tick: int = 2, when: dict | None = None,
                 routes: bool = True) -> None:
        #: 一次唤醒最多造几条变体。少造是设计（§4.5 的 CAP 与 J 的输入长度），
        #: 不是保守：候选多了 J 的选择输入就变长，边界上的等待跟着变长。
        self.per_tick = per_tick
        #: 条件固定成"连续没懂 ≥2 次"：这是 §7.3 里最常被触发的那一条，
        #: 也正好与"学生真的卡住了才换讲法"的教学口径一致。
        self.when = dict(when or {"consecutive_confused": {">=": 2}})
        #: 要不要顺带造一条路线（见 plan_route）。默认造 —— "路线候选从无到有"这条
        #: 验收要的是一条**默认就会发生**的链路，而不是一个要靠参数打开的开关。
        #: 关掉只留给"只想看变体"的对照实验。
        self.routes = bool(routes)

    async def propose(self, request: ProposalRequest) -> object:
        """变体在前、路线在后：池子按"先到先得"占名额（CAP=5 满了拒收新 id），
        顺序反过来就等于让一条路线挤掉一条变体 —— 而变体才是"这一格怎么讲"的常规货。

        路线只在窗口装得下时才造（`plan_route` 会自己判 fork 之后还有没有格子）：
        窗口只剩一格时它返回 None，这次就只上变体，而不是造一条没有汇回点的路线。
        """
        out: list[dict] = []
        for unit in request.window:
            variants = make_variants(unit.text, kp=list(unit.kp))
            if not variants:
                continue
            pick = variants[min(len(out), len(variants) - 1)]
            out.append({
                "target_unit": unit.unit_id,
                "variant": dict(pick),
                "when": dict(self.when),
                "reason": "窗口内单元按规则模板换一种讲法（离线实现）",
            })
            if len(out) >= self.per_tick:
                break
        if self.routes:
            # 岔出格 = **当前格**（ProposalRequest.current_seq），不是变体那个上货窗口的
            # 首格：路线是这个边界的选项，target 因此就是下一格（见 _to_route_candidate ⑤）。
            route = plan_route(request.facts, request.units,
                               fork_node=request.current_seq)
            if route is not None:
                out.append(route_proposal(route, request.units))
        return out


# ===========================================================================
# ⑤ 校验（与 P 同一把尺子）与 ⑥ 幂等写池
# ===========================================================================

#: `matches` 试算用的样本事实。只用来验证"这条 when 能被求值"，
#: 它的**取值**无关紧要 —— 校验不该依赖某一条候选是否命中。
_SAMPLE_FACTS = DecisionFacts(unit_id="sample", slide=0)


def candidate_id_for(target_unit: str, variant_id: str) -> str:
    """候选的幂等键（§7.2 ⑥）：`c-<unit_id>-<variant_id>`。

    为什么必须由"提案本身"派生、而不是由"第几次唤醒"派生：C 每 60s 醒一次，
    模型对同一个格子常常给出同一个提案。如果 id 带上时间戳或序号，`put()` 每次都走
    "新 id"分支，池子 5 分钟撞满 CAP=5，然后门卫开始否决 —— 一个内容上完全正确的
    候选，会因为记账方式把自己锁死在门外。

    为什么保持可读：课堂上出了问题，要能从一个 id 一眼看出"哪一格、哪一种讲法"。
    """
    return f"c-{target_unit}-{variant_id}"


def _check_when(when: object) -> list[str]:
    """when 只能是三种 key、五种算子（§4.6），并用 `matches` 试算一次兜底。

    为什么要两遍：白名单管"写错字"（`comprehensionn` 这种），`matches` 管"白名单
    之外的口径差异" —— 它是 J 真正用的那把尺子，两边必须一致。`matches` 对非法条件
    返回 False 而不抛异常（这是它的设计：不让一条脏候选把边界决策拖进异常路径），
    所以试算只能当兜底、不能替代白名单 —— 否则一个错字会让候选永远不匹配，
    而账上看起来一切正常。
    """
    if when is None:
        return []                    # 空条件 = 恒命中，允许（P 的预置就是这个形态）
    if not isinstance(when, dict):
        return [f"when 不是对象（{type(when).__name__}）"]

    problems: list[str] = []
    for key, cond in when.items():
        if key not in WHEN_KEYS:
            problems.append(
                f"when 的 key {key} 不合法（只认 consecutive_confused / probe_result / comprehension）"
            )
            continue
        if not isinstance(cond, dict):
            problems.append(f"when[{key}] 必须是「算子: 值」的对象")
            continue
        for op in cond:
            if op not in WHEN_OPS:
                problems.append(f"when[{key}] 的算子 {op} 不合法（只认 >= <= == in not_in）")
    if problems:
        return problems
    try:
        matches(when, _SAMPLE_FACTS)
    except Exception:
        problems.append("when 无法用 director.facts.matches 试算")
    return problems


# ===========================================================================
# ⑦ 路线分支（跨格走向）：skip / revisit / finish
# ===========================================================================
#
# 上面几节造的都是**变体**：就地换一种说法，讲完还在原地。路线是另一种东西 ——
# 它跨好几格：跳过几格再汇回、回头把前面几格重讲再汇回、或者直接跳到课尾收尾
# （结构本身在 director/route.py，这里只负责"造哪一条、什么时候造"）。
#
# 为什么入选规则和边界守卫是**同一张表**（ROUTE_WHEN）
# -----------------------------------------------------
# C 造一条路线时看到的是"此刻这个学生"，J 在岔出那一格的边界上看到的才是"他到那格
# 为止卡没卡" —— 两个时刻隔着 5~15 格，中间完全可能变好或变坏。所以"什么条件下该走
# 这条路"必须能被**重算一次**，而不是只在生产侧算一次。写成一张表、两边都查它，
# 两条纪律就不会漂移：改阈值只改一处，造与选同时跟着变。写成两份的后果很具体 ——
# "C 按 A 造、J 按 B 选"，而账上只表现为"这条路从来没被选中过"，与"根本没有这条路"
# 长得一模一样（这一点和 presets.py 里"为什么不能一律用空 when"是同一个道理）。
#
# 条件只能用 facts.py 已有的字段与算子（§4.6）：路线也要过 matches() 那把尺子，
# 多一个算子就得改 J 的实现，而 J 的延迟上界正是拿"条件退化成一次字典比较"换来的。
#
# 三种走向的阈值（为什么是这几个数，不是随手取的）
# ------------------------------------------------
# skip ← comprehension == confirmed 且 consecutive_confused <= 0：
#   跳讲会**漏掉**内容，代价不可逆（学生没听到的就是没听到），所以要求两个条件同时成立。
#   consecutive_confused 是"本单元已重讲轮次 + 本单元还没答完的提问"（见 facts_from_state），
#   取 <= 0 而不是 <= 1：卡过一次又被重讲救回来，恰恰说明这一格需要留在原位讲透，
#   把"救回来的"和"一次没卡过的"合并成一档，等于拿学生的注意力去赌。
# revisit ← comprehension ∈ {vague, confused}：
#   补回头要花的是**未来一整格**的时间，所以只认"这一格讲完之后学生的态度"。
#   它是 _worst_comprehension 的取值（有疑先按疑，见 facts.py）：一个单元覆盖几个
#   知识点时，只要有一个没懂就算没懂 —— 正是补回头该管的范围。
#   为什么不拿 consecutive_confused >= 2 触发：那个数数的是**已经发生过的重讲**，
#   用它触发"未来再花一格回头"，会让"其实只是有点含糊"的学生被拖回上一格 ——
#   那是课堂上最刺眼的一种抖动（presets.py 里 v1/v2 的分工就是同一口径）。
# finish ← consecutive_confused >= 3（且岔出格已在课程后段，见 ROUTE_FINISH_AFTER）：
#   比 revisit 高一档，因为它是三种走向里唯一不可挽回的 —— 后面的格子全都不讲了。
#   第二次卡住还值得补回头（revisit 会先被选中），到第三次说明这条路讲不下去；
#   后段那条结构条件只能放在生产侧：时间不是一个事实字段，"讲不完"只能靠位置估。
ROUTE_WHEN: dict[str, dict] = {
    "skip": {"comprehension": {"==": "confirmed"},
             "consecutive_confused": {"<=": 0}},
    "revisit": {"comprehension": {"in": ["vague", "confused"]}},
    "finish": {"consecutive_confused": {">=": 3}},
}

#: 同一时刻最多一条路线（池子 CAP=5 与"课堂只可能执行一条岔路"都这么要求），
#: 所以三种走向必须有确定的先后：finish（讲不下去了）> revisit（讲不透）> skip（讲得好）。
#: 越靠后越乐观，而乐观的选择在信息不足时最不该抢先。
ROUTE_PRIORITY: tuple[str, ...] = ("finish", "revisit", "skip")

#: alive 路线总数的上界（跨 fork 的总名额；"同 fork 至多一条"由 _make_room_for_route 保）。
#: 3 = 池子 CAP(5) 里留给路线的份数，**至少留 2 个名额给变体** —— 一格的 v1/v2 两档
#: 同时可用是常态（presets.py），而路线是加餐（§4.5 的先到先得纪律）。
#: 为什么不是越大越好、也不是 5：一条路线只对**一个**边界有意义（candidates_for 按
#: target_unit 过滤，别的 fork 的路线在那个边界上根本看不见），所以同时活着的路线
#: 超过"当前边界 + 刚过去的一两个边界"就等于给走不到的边界囤货 —— 那正是这次修的
#: 时序缺陷的成因。上限到了由新路线挤掉岔出点最早的那一条（_make_room_for_route），
#: 而不是拒收新的：拒收会让这个边界空手，而被挤掉的那条越过的边界最多、最不可能
#: 再被选中。
#: 3 还有一个巧合的好性质：一条路线的 expires_at_seq 是 fork + 1 + 2，也就是它在岔出
#: 之后还能活三个边界 —— 名额上限与"自然淘汰"同量级，于是被挤掉的那条本来也快过期了，
#: 两套机制不会互相打架。
ROUTE_ALIVE_CAP = 3

#: skip 的跨度：岔出格之后跳过 ROUTE_SKIP_GAP 格，在建回点（fork + GAP + 1）汇回。
#: 两格是"讲一段废话"的量级：跳一格既省不了多少时间，又会把"跳过去"这件事变成
#: 一次教学事故（学生听到的下一句接不上上一句）；三格以上就跨页了，而跨页汇回
#: 等于把这一页的结论整段丢掉。（跨度 3 格、真跳过 2 格：Route.summary 报的是后者，
#: derive_route_id 尾数存的是前者。）
ROUTE_SKIP_GAP = 2

#: revisit 回头重讲几格。1 格：补回头要的是"把刚才没讲透的那一版讲清楚"，
#: 拖两格以上就变成重上课，而课时是有限的。
ROUTE_REVISIT_SPAN = 1

#: "课程后段"的判据（fork 位置 / 总格数）。0.6 而不是 0.5：finish 是不可挽回的选择，
#: 只在前半程就用它，等于把"课还长、回头补得起"的那些机会全部扔掉；而 0.8 之后
#: 剩下的格子本来就快讲完了，提前收尾也省不下什么。
ROUTE_FINISH_AFTER = 0.6


def route_kind(
    facts: DecisionFacts | None, *, fork_node: int, total_nodes: int
) -> str:
    """这一轮该造哪一种走向；返回 "" = 这次不造路线（只造变体）。

    判定只用 matches() 跑 ROUTE_WHEN —— 也就是 J 在边界上要跑的那一把尺子，于是
    "C 为什么造了它"与"J 为什么会选中它"是同一句判断，不可能各说各话。

    三条结构条件只能在这里判、不能进 when：位置不是事实（§4.6 的白名单里没有它），
    而"回头路存不存在""课尾在不在"都是位置问题：
      · revisit 前面没有格子 → 没有可补的回头路；
      · skip 后面不足两格 → 跳过去就没有汇回点了；
      · finish 不在后段 → 见 ROUTE_FINISH_AFTER。
    判不过就往下一档降级（finish 退成 revisit），而不是直接放弃：学生的信号还在，
    只是这条路此刻不适合走。
    """
    if facts is None or total_nodes <= 1:
        return ""
    for kind in ROUTE_PRIORITY:
        if not matches(ROUTE_WHEN.get(kind), facts):
            continue
        if kind == "revisit" and fork_node <= 0:
            continue                       # 前面没有格子可补
        if kind == "skip" and fork_node + ROUTE_SKIP_GAP + 1 > total_nodes:
            continue                       # 跳过去就没有汇回点
        if kind == "finish" and (fork_node <= 0
                                 or fork_node < total_nodes * ROUTE_FINISH_AFTER):
            continue                       # 还不到后段：课还长，回头补得起
        return kind
    return ""


def plan_route(
    facts: DecisionFacts | None,
    units: Sequence[LectureUnit],
    *,
    fork_node: int,
) -> Route | None:
    """按事实与课程结构造一条**结构给全**的路线；None = 这次不造。

    fork_node 由调用方给（C 的**当前格**）：路线是"刚讲完这一格，接下来往哪走"，
    岔在哪一格只有知道课程进度的人能定。units 是整份脚本的单元（order_index 序）——
    路线要往前（revisit 的回头）和往后（skip / finish 的汇回）看，只给窗口装不下它。

    结构一次给全（fork / merge / steps / est_sec / reason 同一次写入），理由见
    route.py 顶部：中途那份半成品一旦被 J 选中，状态机就会在没有汇回点的情况下走出
    课程。所以这里绝不返回"先给 fork、稍后补 merge"的东西。

    est_sec 的符号按冻结约定（route.py）：skip / finish 省时间写负数，revisit 花时间
    写正数。数值直接取被跳过 / 被重讲那几格的 LectureUnit.est_sec（与 §4.1 的变体
    同一把尺子：中文约 5 字/秒），不是凭空估的常数 —— J 要拿它和变体比"划不划算"，
    两边必须用同一个口径量。
    """
    total = len(units)
    if facts is None or total < 3:
        return None
    if not 0 <= fork_node < total - 1:
        return None                       # 岔出格之后必须还有格子，否则汇不回来
    kind = route_kind(facts, fork_node=fork_node, total_nodes=total)
    if not kind:
        return None

    if kind == "skip":
        merge = min(fork_node + ROUTE_SKIP_GAP + 1, total)
        skipped = list(units[fork_node + 1: merge])
        if not skipped:
            return None
        return Route(
            route_id=derive_route_id("skip", fork_node, merge),
            kind="skip", fork_node=fork_node, merge_node=merge, steps=[],
            est_sec=-round(sum(u.est_sec for u in skipped), 1),
            reason=f"这段已确认听懂，跳过 {len(skipped)} 格直接接上",
        )

    if kind == "revisit":
        lo = max(0, fork_node - ROUTE_REVISIT_SPAN)
        back = list(units[lo:fork_node])
        if not back:
            return None
        # 步骤只填 unit_id + est_sec：variant_id / text 留空 = 用该单元的主 text
        # （route.py 的约定）—— "把这一格再讲一遍"本身就成立，不要求导演另编说法。
        steps = [RouteStep(unit_id=u.unit_id, est_sec=u.est_sec) for u in back]
        return Route(
            route_id=derive_route_id("revisit", fork_node, fork_node + 1),
            kind="revisit", fork_node=fork_node, merge_node=fork_node + 1,
            steps=steps,
            est_sec=round(sum(s.est_sec for s in steps), 1),
            reason=f"这格没讲透，回头把前面 {len(steps)} 格再讲一遍",
        )

    left = list(units[fork_node + 1: total])
    return Route(
        route_id=derive_route_id("finish", fork_node, total),
        kind="finish", fork_node=fork_node, merge_node=total, steps=[],
        est_sec=-round(sum(u.est_sec for u in left), 1),
        reason=f"反复讲不透，直接收尾到课末（剩 {len(left)} 格）",
    )


def route_target(route: Route, units: Sequence[LectureUnit]) -> str:
    """路线的 target_unit：**岔出格之后那一格**的 unit_id（冻结口径，见 route.py）。

    为什么是它、而不是 fork 自己：J 是选路者，它在"听完第 F 格"这个边界上决定
    "接下来往哪走"，而候选池按 candidates_for(下一格) 过滤 —— 所以一条路线必须挂在
    它**要接上的那一格**上，既有过滤才天然生效（decider.py 一个字都不用改）。
    越界返回 ""（由调用方当"这条路线用不了"处理）。
    """
    i = route.fork_node + 1
    if 0 <= route.fork_node and i < len(units):
        return units[i].unit_id
    return ""


def route_proposal(route: Route, units: Sequence[LectureUnit]) -> dict:
    """把一条路线包成与变体同形状的提案（target_unit + route + when + reason）。

    为什么复用同一个提案形状、而不给路线另开一条通道：run_tick 的五个环节
    （校验 → 幂等键 → 写池 → 记账 → 拒绝理由）对两种载荷是同一套纪律；另开通道
    等于把"路线能不能进池"的规则再写一遍，而两份规则必然漂移。

    when 取 ROUTE_WHEN[kind]：造它的那一条判断，就是 J 在边界上要重算的那一条
    （理由见 ROUTE_WHEN 的注释）。
    """
    return {
        "target_unit": route_target(route, units),
        "route": route.to_dict(),
        "when": dict(ROUTE_WHEN.get(route.kind) or {}),
        "reason": route.reason,
    }


def current_facts(state: ClassroomState, script: LessonScript) -> DecisionFacts | None:
    """当前这一格的决策事实（与边界上 J 用的是同一套派生，见 facts.facts_from_state）。

    为什么 C 也要这个：快照里那几个数是**课堂级**的（累计困惑数、待回看条数），
    它们回答"要不要造货"，不回答"造哪一种"；单元级的态度只在 DecisionFacts 里。
    取不到单元（位置掉在两页之间）→ None = 这次不造路线，变体照造。
    """
    unit = script.unit_of(state.slide_index, state.seg_index)
    if unit is None:
        unit = script.first_unit_of(state.slide_index)
    if unit is None:
        return None
    return facts_from_state(state, unit)


def _is_route(raw: object) -> bool:
    """提案是"路线"还是"变体"：只有 route 是一份**非空**对象时才算路线。

    为什么要判非空：模型为了满足 schema 常常把不要的字段写成空对象或 null
    （提示词里就要求"给路线时 variant 写空对象"），"有这个键"因此不能当判据。
    """
    if not isinstance(raw, dict):
        return False
    route = raw.get("route")
    return isinstance(route, dict) and bool(route)


def _to_route_candidate(
    raw: dict,
    snapshot: Snapshot,
    units: Sequence[LectureUnit],
    *,
    created_at: float,
) -> tuple[Candidate | None, list[str]]:
    """把一条路线提案变成池里的 Candidate；任何一项不过就返回（None, 原因列表）。

    校验顺序照 §7.2 ⑤ 的老规矩（便宜的先做），但**结构校验必须先于一切写池动作**：

      ① Route.from_dict：kind 非法当场炸（"怎么走"本身没定义），这里接住换成拒绝；
      ② route.validate(total_nodes)：0 <= fork < merge <= total / revisit 的 steps
         非空 / finish 的 merge 必须等于课尾 —— 越界的路线一旦进池，J 会选中它，
         状态机就会走出课程；
      ③ 汇回点存在（fork + 1 必须落在课程里，否则没有"接上"的那一格）；
      ④ steps 指向的单元必须真的存在（revisit 的全部内容就是它）；
      ⑤ 岔出格必须是**当前格**（fork_node == snapshot.current_seq），于是 target 必然
         就是当前格的下一格。这是路线与变体分道的那一条：变体是"给窗口里某一格备的
         货"（§7.4 的 [+5,+15]），路线是"这个边界接下来往哪走" —— 拿窗口去卡它，
         等于要求它去服务 5 格之后的边界，而那一刻的事实与它构造时依据的事实早已
         不是同一件事（也正因为如此，改动前的实现里每条路线都在自己的边界到来之前
         被下一次唤醒顶掉了）。模型给了别的 fork 一律拒：拒绝理由进 metrics，图上
         才分得清"这条路线为什么没进池"与"根本没有路线"。

    全过程不抛异常：C 是后台线程，一条脏路线最多是"这次少一条岔路"，而一个抛到
    run_tick 之外的异常会把整节课的造分支停掉（§11）。
    """
    route_raw = raw.get("route")
    if not isinstance(route_raw, dict) or not route_raw:
        return None, [f"route 不是对象（{type(route_raw).__name__}）"]
    try:
        route = Route.from_dict(route_raw)
    except (ValueError, TypeError) as e:
        return None, [f"路线结构非法：{e}"]

    total = len(units)
    try:
        route.validate(total)
    except ValueError as e:
        return None, [f"路线越界或结构不合法：{e}"]

    # 幂等键由结构派生，**不认提案里自带的 route_id**：同一条走向反复算出来必须命中
    # 同一个 key（池按 id 覆盖、version+1）。让写入方自己给 id，等于让每次唤醒都造一条
    # 新货，5 次之后 CAP=5 被同一条走向填满，门卫从此永远否决。
    route.route_id = derive_route_id(route.kind, route.fork_node, route.merge_node)

    target = route_target(route, units)
    if not target:
        return None, [f"fork_node {route.fork_node} 之后没有可汇回的格子（共 {total} 格）"]

    known = {u.unit_id for u in units}
    unknown = [s.unit_id for s in route.steps if s.unit_id not in known]
    if unknown:
        return None, [f"路线 steps 里的单元不存在：{unknown[:3]}"]

    # 路线的"上货窗口纪律"只有这一条：它必须锚在当前这一格上（理由见上面 ⑤）。
    # 顺序放在结构校验之后、when 之前：位置错是最便宜能判的那种错，而 when 的
    # 试算要跑一遍 matches()。
    if route.fork_node != snapshot.current_seq:
        return None, [
            f"路线的岔出格必须是当前这一格（第 {snapshot.current_seq} 格），"
            f"收到 fork_node={route.fork_node}"
        ]

    # 守卫**不许为空**：空 when 恒真（facts.matches），一条"补回头"会在每个边界上被
    # 选中。模型没给 when 时按走向补上 ROUTE_WHEN 那一档（造它的理由就是守卫本身）。
    problems = _check_when(raw.get("when"))
    if problems:
        return None, problems
    when = dict(raw.get("when") or ROUTE_WHEN.get(route.kind) or {})

    return Candidate(
        candidate_id=route.route_id,
        target_unit=target,
        route=route,
        when=when,
        source=SOURCE,
        created_at=created_at,
        created_at_unit=snapshot.current_unit,
        # 与变体同一把尺子（目标格序号 + 余量），但目标格是**当前格的下一格**，
        # 所以路线在岔出边界之后还能活 EXPIRE_MARGIN_UNITS 个边界 —— 学生"没懂"
        # 常常在下一格才表达出来（与 EXPIRE_MARGIN_UNITS 的理由同一个）。
        # 这里按绝对序号算而不是查窗口表：路线的 target 已经不在变体窗口里了。
        expires_at_seq=route.fork_node + 1 + EXPIRE_MARGIN_UNITS,
    ), []


def _route_fork(cand: Candidate) -> int:
    """路线候选的岔出格；Route 与 dict 两种形态都认（池里可能有 JSON 回填的那种）。

    读不出来给 0：它只用于"同 fork 替换"与"挤掉最旧"两处排序，读成 0 最坏是把
    一条脏路线当成最早的丢掉 —— 比让一次唤醒抛异常、把整节课的造分支停掉轻得多。
    """
    route = cand.route
    raw = route.get("fork_node") if isinstance(route, dict) else getattr(route, "fork_node", 0)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _make_room_for_route(pool: CandidatePool, cand: Candidate) -> int:
    """给一条**已经校验过**的路线腾位，返回作废条数。两条纪律：

    ① **一把岔路一把锁**：同 fork 的旧路线作废（新的顶替旧的）。同 fork 是"同一个
       边界往哪走"的两个答案，同时活着等于让 J 在同一个边界上二选一 —— 而它俩的
       汇回点互不相认。
    ② **总名额上限** ROUTE_ALIVE_CAP：超了就挤掉岔出点最早的那一条（同 fork 按新近）。
       为什么挤而不是拒：拒收会让这个边界空手，而被挤掉的那条越过的边界最多，
       是全部路线里最不可能再被选中的一条。

    **不再"新的一来就把别的路线全作废"**（改动前的口径）：路线是"这个边界的选项"，
    而 C 每推进一格醒一次 —— 全作废意味着每条路线都在自己那一格的边界到来之前被
    下一次唤醒顶掉。实测（prototype/_accept_timeline.py 的 [A]/[B]）：current=3 时
    造出的那条路线，再醒三次（current=4,5,6）就已经 expired，边界上什么都看不见；
    而它锚在窗口首格时"看得见自己那条"本来就不成立（target 在 +6 那一格）。
    账上这两种情形与"根本没有路线"长得一模一样，查不出来。别的 fork 的路线属于
    **别的边界**，不该被这一格的决定连坐。

    只碰路线：变体候选一条都不动（它们才是"这一格怎么讲"的常规货）。作废而不是删除：
    账上要留下"这条曾经存在过"（TOMBSTONE 语义与 expire_stale 一致，图上与复盘都靠它）。

    为什么必须在 put **之前**腾：池子满了 put 会拒收，而一条路线的净开销应当是
    "它自己那一个名额"。先腾后写，于是"顶掉旧货"与"新货进池"是同一件事 —— 既不会
    出现"旧的作废了、新的被拒了"这种空手，也不会让路线挤掉变体（腾完仍可能因为全是
    变体而写不进，那时一条路线都没被动过，见 run_tick）。
    """
    keep = cand.candidate_id
    fork = _route_fork(cand)
    dropped = 0
    for other in pool.items():
        if (other.status == "alive" and other.route is not None
                and other.candidate_id != keep and _route_fork(other) == fork):
            other.status = "expired"
            dropped += 1
    if pool.get(keep) is None:     # 同 id 重放走 put 的覆盖分支，不占新名额，也不必挤人
        alive = [c for c in pool.items() if c.status == "alive" and c.route is not None]
        while len(alive) >= ROUTE_ALIVE_CAP:
            oldest = min(alive, key=lambda c: (_route_fork(c), c.created_at))
            oldest.status = "expired"
            alive.remove(oldest)
            dropped += 1
    return dropped


def _to_candidate(
    raw: object,
    snapshot: Snapshot,
    window: dict[str, LectureUnit],
    ordinals: dict[str, int],
    deck: DeckDoc | None,
    *,
    created_at: float,
    char_warn: int,
) -> tuple[Candidate | None, list[str]]:
    """把一条原始提案变成池里的 Candidate；任何一项不过就返回（None, 原因列表）。

    校验顺序按"便宜的先做"：形状 → 目标单元 → 条件 → 文本体检。文本体检放最后，
    因为它要先取目标单元的 SlideDoc，而前面任何一项不过，那次取页就是白做的。

    文本体检用 `validate_variant`（§5.4）—— **和 P 完全同一把尺子**：两个角色用
    两把尺子，就会出现"P 合规、C 不合规"的变体在课堂上被 J 选中。
    """
    if not isinstance(raw, dict):
        return None, [f"提案不是对象（{type(raw).__name__}）"]

    problems: list[str] = []

    target = str(raw.get("target_unit") or "").strip()
    if not target:
        # 空串在池里的语义是"任意单元"，那是 P 批量预置用得上的形态；C 造的是
        # "这一格、这个学生"的讲法，指向空等于让窗口校验形同虚设。
        problems.append("target_unit 为空：C 必须指向窗口里一个具体单元")
    elif target not in window:
        problems.append(f"target_unit {target} 不在上货窗口内")

    variant: UnitVariant | None = None
    variant_raw = raw.get("variant")
    if not isinstance(variant_raw, dict):
        problems.append(f"variant 不是对象（{type(variant_raw).__name__}）")
    else:
        variant = UnitVariant.from_dict(variant_raw)
        if not variant.variant_id.strip():
            # 没有 variant_id 就没有幂等键：重放会把它当成新候选，池子照样被塞满。
            problems.append("variant.variant_id 为空：没有它就没有幂等键")

    problems.extend(_check_when(raw.get("when")))
    if problems or variant is None:
        return None, problems

    unit = window[target]
    if deck is None:
        return None, ["没有课件，无法用 P 的尺子体检（§5.4 的锚点与句数都要它）"]
    slide = deck.slide_at(unit.slide)
    if slide is None:
        return None, [f"课件里没有第 {unit.slide + 1} 页，无法体检"]

    problems = validate_variant(variant.to_dict(), slide, char_warn=char_warn)
    if problems:
        return None, problems

    return Candidate(
        candidate_id=candidate_id_for(target, variant.variant_id),
        target_unit=target,
        variant=variant,
        when=dict(raw.get("when") or {}),
        source=SOURCE,
        created_at=created_at,
        created_at_unit=snapshot.current_unit,
        expires_at_seq=ordinals[target] + EXPIRE_MARGIN_UNITS,
    ), []


# ===========================================================================
# 一次唤醒（§7.2 的 tick）
# ===========================================================================

@dataclass(frozen=True)
class Rejected:
    """一条被拒的提案：序号 + 目标单元 + 原因 + 它是哪一种载荷（进日志与指标）。

    `payload` 分开记路线与变体：路线上货是"同 fork 替换式"的（见 _make_room_for_route），
    它被丢掉时图上表现为"这一课没有岔路"，而那是**看不见**的 —— 所以路线的拒绝
    必须能在 metrics 里单独查（assembly.serve_agent_c 按它发 agent_c_route_rejected）。
    """

    index: int
    target_unit: str
    reasons: tuple[str, ...]
    payload: str = "variant"        # "variant" | "route"

    def summary(self) -> str:
        return f"#{self.index} {self.target_unit or '?'}：{'；'.join(self.reasons)}"


@dataclass
class TickResult:
    """一次唤醒的账（§7.2 的观测面）：调没调模型、为什么、写了什么、拒了什么。

    `called` 与 `ok` 必须分开：`called` 只回答"这次花没花钱"（§12 阶段 3 验收①
    数的是它），`ok` 回答"这次有没有出故障"。两者合成一个布尔值，就分不清
    "门卫否决"（好事）和"模型超时"（坏事）。
    """

    called: bool
    ok: bool
    reason: str
    prompt: str = ""
    window: tuple[str, ...] = ()
    written: tuple[str, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    snapshot: Snapshot | None = None
    #: 这一次唤醒挤掉了谁（candidate_id）。空 = 没驱逐。完整的账（谁被谁挤掉、expires）
    #: 在池子的 evictions() 里：这件事必须能复盘，否则"少了一条备货"永远查不出原因。
    evicted: tuple[str, ...] = ()

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def rejection_reasons(self) -> tuple[str, ...]:
        return tuple(r for item in self.rejected for r in item.reasons)


async def run_tick(
    *,
    state: ClassroomState,
    script: LessonScript,
    deck: DeckDoc | None,
    pool: CandidatePool,
    watermark: GateWatermark,
    proposer: ProposalProvider,
    now: float | None = None,
    timeout_s: float = PROPOSER_TIMEOUT_S,
    char_warn: int = CHAR_WARN,
) -> TickResult:
    """一次唤醒（§7.2）：读快照 → 门卫 → 组装 → 调模型 → 校验 → 幂等写池。

    任何一个环节出问题都收敛成一个 `TickResult`，不向上抛异常：C 是后台循环，
    异常逃出去的后果不是"这次没上货"，而是一整节课的造分支静默停摆 —— §11 的
    口径是"C 某次唤醒超时 → 60s 后重试，学生无感"，那就必须能重试，也就是不能死。
    （`asyncio.CancelledError` 例外：那是调用方要停，不是失败，照常向上传播。）

    `now` 可注入：`created_at` 参与池子的排序（同源按新近），测试要能钉死它。
    """
    snapshot = snapshot_from(state, script)

    # 清死货必须在门卫之前：CAP=5 是硬上限，而 expires_at_seq 已经过时的货在"满了"
    # 这件事上照样计数 —— 不清它们，池子会被**早就该作废**的备货永久冻住（每一次唤醒都
    # 撞在 pool_full 上，而满的那 5 条一条都用不了）。口径与 load_unit_presets 完全一致
    # （严格小于：等于 current_seq 的货还留给这一格），所以两处不会互相打架。
    pool.expire_stale(max(0, snapshot.current_seq))
    evicted_before = pool.eviction_count()

    opened, reason = gate_open(snapshot, pool, watermark)
    if not opened:
        # 门卫关：一次模型都不调（§7.3 的 8~15 次就是靠这条换来的）。
        return TickResult(called=False, ok=True, reason=reason, snapshot=snapshot)

    window_range = target_window(snapshot.current_seq, snapshot.total_units)
    window = tuple(script.units[i] for i in window_range)
    if not window:
        # 防御：门卫已经用 remaining_units 拦过空窗口，但窗口与门卫是两个常量，
        # 将来调了任何一个都可能让这里先空 —— 空窗口还调模型是纯浪费。
        return TickResult(called=False, ok=True, reason="empty_window", snapshot=snapshot)

    status = pool.status()
    units = tuple(script.units)
    request = ProposalRequest(
        prompt=build_prompt(snapshot, window, status),
        snapshot=snapshot,
        window=window,
        pool_status=status,
        # 路线的三件结构输入（理由见 ProposalRequest）：**当前这一格**的事实、整份脚本
        # 的单元、当前单元的序号（= 岔出格）。变体用不到它们，所以都有默认值 ——
        # 老调用点一个字都不用改。
        facts=current_facts(state, script),
        units=units,
        current_seq=snapshot.current_seq,
    )
    window_ids = tuple(u.unit_id for u in window)

    try:
        payload = await asyncio.wait_for(proposer.propose(request), timeout=max(0.0, timeout_s))
    except asyncio.TimeoutError:
        return TickResult(called=True, ok=False, reason="proposer_timeout",
                          prompt=request.prompt, window=window_ids, snapshot=snapshot)
    except Exception:
        # 异常类型不进结果串：它要进指标，而指标要的是"哪一类失败"，不是一段栈；
        # 栈在调用方自己的日志里（本模块不打日志，保持可重放）。
        return TickResult(called=True, ok=False, reason="proposer_error",
                          prompt=request.prompt, window=window_ids, snapshot=snapshot)

    if not isinstance(payload, list):
        # 契约是数组。单个对象也不认：多给一条和给错形状，在账上必须分得清。
        return TickResult(called=True, ok=False, reason="bad_payload",
                          prompt=request.prompt, window=window_ids, snapshot=snapshot)

    by_id = {u.unit_id: u for u in window}
    ordinals = {u.unit_id: i for i, u in zip(window_range, window)}
    created_at = time.monotonic() if now is None else float(now)

    written: list[str] = []
    rejected: list[Rejected] = []
    # 第一遍写不进去的候选：(提案序号, target, 候选)。驱逐名额只会从这里面挑一条。
    pending: list[tuple[int, str, Candidate]] = []
    # 处理顺序 = 池子的优先级：**变体先、路线后**。池子按"先到先得"占名额，先写路线就等于
    # 让一条路线挤掉一条变体 —— 而变体才是"这一格怎么讲"的常规货，路线是加餐。
    # 稳定排序保住 payload 内的原始次序，于是拒绝理由里的 index 仍然是提案在输出里的位置
    # （复盘时对得上账）。
    order = sorted(range(len(payload)), key=lambda i: (_is_route(payload[i]), i))
    for index in order:
        raw = payload[index]
        target = str(raw.get("target_unit") or "").strip() if isinstance(raw, dict) else ""
        route_payload = _is_route(raw)
        if route_payload:
            candidate, problems = _to_route_candidate(
                raw, snapshot, units, created_at=created_at,
            )
        else:
            candidate, problems = _to_candidate(
                raw, snapshot, by_id, ordinals, deck,
                created_at=created_at, char_warn=char_warn,
            )
        if candidate is None:
            rejected.append(Rejected(index=index, target_unit=target,
                                     reasons=tuple(problems),
                                     payload="route" if route_payload else "variant"))
            continue
        if candidate.route is not None:
            # 腾位（同 fork 替换 + 总名额上限，见 _make_room_for_route）：先腾再写，
            # 这样"顶掉旧货"与"新货进池"是同一件事，不会腾完反而被拒（空手）。
            _make_room_for_route(pool, candidate)
        # 第一遍只用**空闲名额**（allow_evict=False）：免费的位置照旧按上面的先后分，
        # 于是"变体先、路线后"这条优先级一个字都没变，驱逐名额也不会有第二个人来抢。
        if pool.put(candidate, allow_evict=False):
            written.append(candidate.candidate_id)
        else:
            pending.append((index, target, candidate))

    # 第二遍：整次唤醒只有**一次**驱逐名额 —— "一次唤醒只花一次调用、只换一条货"，账才对得上，
    # 也绝不会因为"挤进来了"就去再问模型要货。名额给**最靠当期**的那一条：expires 落在当期
    # 需求之内（= 这个边界的路线）的优先，那才是实测里被饿死的那一档；没有当期货时退一步给
    # 最近的那条备货（门卫既然是按"挤得动"开的闸，就让它至少换一条进来，否则这次调用白花）。
    # 无论走哪一支都只**尝试一次**：失败就是失败，绝不拿第二个候选再挤一次（那会变成刷池）。
    # 第一遍已经把免费名额分完，所以这里不改变"变体先、路线后"的既有优先级，只把本来进不来的
    # 那一条捞回来。
    need = boundary_need_expires(snapshot.current_seq)
    near = [item for item in pending if item[2].expires_at_seq <= need]
    attempt = near or pending
    if attempt:
        pick = min(attempt, key=lambda item: (item[2].expires_at_seq, item[0]))
        if pool.put(pick[2], allow_evict=True):
            written.append(pick[2].candidate_id)
            pending = [item for item in pending if item[0] != pick[0]]

    for index, target, candidate in pending:
        # 走到这里只有两种可能：池满且挤不动（没有比它更远的活货 / 期距超出上货窗口），
        # 或者挤得动但名额给了更靠当期的那一条。账上都记同一句：这一次没写成。
        rejected.append(Rejected(index=index, target_unit=target,
                                 reasons=("池子已满，未写入（§7.2 ⑥）",),
                                 payload="route" if candidate.route is not None else "variant"))

    return TickResult(
        called=True,
        ok=True,
        reason=reason,
        prompt=request.prompt,
        window=window_ids,
        written=tuple(written),
        rejected=tuple(rejected),
        snapshot=snapshot,
        evicted=tuple(e.candidate_id for e in pool.evictions()[evicted_before:]),
    )
