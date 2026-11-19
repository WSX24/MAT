"""决策事实：J 在单元边界上能看到的全部信息。

为什么要把课堂压成一个定长小结构：J 的延迟上界是拿"输入有界"换来的（方案 §6.2），
所以不允许它去翻状态机 —— 状态机先把自己压成 100 字，再交给它。

为什么触发条件是结构化 dict 而不是自然语言（§4.6）：匹配退化成一次字典比较之后，
连状态机自己都会做，J 才有资格只输出一个枚举；而"理解一句话"没法承诺时延。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, fields

from state import ClassroomState, LectureUnit, QStatus

#: 事实块硬上界（§4.7）。写死在模块里而不是交给调用方：J 的 prefill 预算按它算，
#: 少一个字没人在意，多一个字就是课堂上的一次停顿。
MAX_PROMPT_CHARS = 100

_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


@dataclass
class DecisionFacts:
    unit_id: str
    slide: int
    consecutive_confused: int = 0
    probe_result: str = ""          # "" | confirmed | interrupted | timeout
    comprehension: str = ""         # "" | confirmed | vague | confused
    q_rounds: int = 0

    def to_prompt_block(self) -> str:
        """渲染成 ≤100 字的事实块（§4.7）。

        取舍：只写"会改变选择"的量 —— 位置、连续没懂几次、最近探针、理解信号、已重讲几轮；
        不写任何课件原文，因为原文对"这一格换不换讲法"没有信息量。
        自由字段先各自截断，最后整体再截一次：上界是硬约束，任何一个字段被灌进长文本
        都不能把它顶破（顶破了就是边界上一次没人预估过的等待）。
        """
        unit = _clip(self.unit_id, 20)
        probe = _clip(self.probe_result, 12) or "无"
        comp = _clip(self.comprehension, 10) or "无"
        block = (
            f"单元{unit}（第{self.slide}页）"
            f"|连续没懂{self.consecutive_confused}次"
            f"|探针{probe}|理解{comp}|重讲{self.q_rounds}轮"
        )
        return _clip(block, MAX_PROMPT_CHARS)


#: 条件里只允许引用真实字段。写错 key 的候选必须一律不匹配 ——
#: getattr 兜底成 None 之后，"== None" 之类的条件会让一个错字意外生效，
#: 而错的条件不会报错，只会让课堂选错讲法。
_FACT_KEYS = frozenset(f.name for f in fields(DecisionFacts))


def matches(when: dict | None, facts: DecisionFacts) -> bool:
    """扁平条件 + 隐式 AND（§8.1）：任一 key 不满足即 False，空条件恒 True。

    宁可"不匹配"也不抛异常：一个写错的条件最多让候选不被选中（课堂照旧用 v0），
    而抛异常会把边界上的决策拖进异常路径 —— 两件事的代价完全不同。
    """
    if not when:
        return True
    try:
        items = list(when.items())
    except AttributeError:      # 不是 dict：约定之外的东西一律当作不匹配
        return False
    for key, cond in items:
        if key not in _FACT_KEYS:
            return False
        actual = getattr(facts, key, None)
        try:
            cond_items = list(cond.items())
        except AttributeError:
            return False
        for op, want in cond_items:
            fn = _OPS.get(op)
            if fn is None:
                return False
            try:
                if not fn(actual, want):
                    return False
            except Exception:   # 类型对不上（如 str 与 int 比大小）
                return False
    return True


#: 越靠后越该保守：一个单元只要有一个知识点没懂，就当这格没懂。
_COMPREHENSION_RANK = {"": 0, "confirmed": 1, "vague": 2, "confused": 3}


def _worst_comprehension(signals: dict[str, str], keys: list[str]) -> str:
    worst = ""
    for key in keys:
        got = signals.get(key, "")
        if _COMPREHENSION_RANK.get(got, 0) > _COMPREHENSION_RANK[worst]:
            worst = got
    return worst


def facts_from_state(state: ClassroomState, unit: LectureUnit) -> DecisionFacts:
    """从课堂状态派生事实（只读，无副作用）。派生规则与理由：

    - **键两种都认**：state.py 的注释写 q_rounds / comprehension 以 kp 为键，
      但 machine.py 实际写的是 unit_id（st.q_rounds[unit_id] = round_no）。
      两处并存，只查一种必然查空，所以 unit_id 与本单元 kp 一起查、取最坏值。
    - comprehension **有疑先按疑处理**：confused > vague > confirmed。
      一个单元覆盖好几个知识点时，只要有一个点没懂，这一格就不该当"听懂了"。
    - consecutive_confused = 本单元已重讲轮次 + 本单元还没答完的提问数（§4.6）。
      轮次记"已经讲了几遍"，pending 记"刚打断还没兑现"，两者都算这格卡住了；
      只看其一就会漏掉另一种卡顿（学生一句话不发，轮次也会被推上去）。
    - probe_result 取最后一条**有结论**的探针：Probe 是先 append 后 close
      （machine.py 的 _close_probe），直接取 probe_log[-1].result 会读到空串，
      而那条空串只表示"探针刚开"，不表示学生任何态度。
    """
    keys = [unit.unit_id, *unit.kp]
    q_rounds = max((state.q_rounds.get(k, 0) for k in keys), default=0)
    pending = sum(
        1
        for q in state.pending_questions
        if q.at_unit == unit.unit_id and q.status is not QStatus.ANSWERED
    )
    probe_result = ""
    for probe in reversed(state.probe_log):
        if probe.result:
            probe_result = probe.result
            break
    return DecisionFacts(
        unit_id=unit.unit_id,
        slide=unit.slide,
        consecutive_confused=q_rounds + pending,
        probe_result=probe_result,
        comprehension=_worst_comprehension(state.comprehension, keys),
        q_rounds=q_rounds,
    )
