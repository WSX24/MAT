"""决策事实测试 —— 把 §4.6 / §8.1 的条件匹配与 100 字事实块变成可执行的断言。

三件要证明的事：
1. 五种算子 + 隐式 AND 就是全部匹配语义：写错 key、写错算子、类型对不上，一律不匹配。
   错的条件不会报错，只会让课堂安静地选错讲法 —— 所以必须由测试兜住。
2. 事实块的上界是硬的：字段被灌满长文本也不会超过 100 字。
3. facts_from_state 的派生规则与 state 的真实字段对得上，包括键到底是 unit_id 还是 kp。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from director.facts import MAX_PROMPT_CHARS, DecisionFacts, facts_from_state, matches  # noqa: E402
from state import ClassroomState, LectureUnit, Probe, QStatus, Question  # noqa: E402


def _unit(unit_id: str = "S03-U02", slide: int = 2, kp: tuple[str, ...] = ()) -> LectureUnit:
    return LectureUnit(unit_id=unit_id, slide=slide, index_in_slide=0, text="……", kp=list(kp))


def test_matches_covers_five_ops_and_implicit_and() -> None:
    """匹配退化成一次字典比较：五种算子 + 隐式 AND 之外没有别的语义。"""
    facts = DecisionFacts(
        unit_id="S03-U02", slide=2, consecutive_confused=2,
        probe_result="timeout", comprehension="confused", q_rounds=1,
    )
    # 空条件恒 True —— "无条件 = 随时可用"是预置变体最常见的形态
    assert matches({}, facts) is True
    assert matches(None, facts) is True
    # 五种算子
    assert matches({"consecutive_confused": {">=": 2}}, facts) is True
    assert matches({"consecutive_confused": {">=": 3}}, facts) is False
    assert matches({"consecutive_confused": {"<=": 2}}, facts) is True
    assert matches({"probe_result": {"==": "timeout"}}, facts) is True
    assert matches({"probe_result": {"in": ["timeout", "interrupted"]}}, facts) is True
    assert matches({"probe_result": {"not_in": ["confirmed"]}}, facts) is True
    # 隐式 AND：一条不满足即整条不匹配
    when = {
        "consecutive_confused": {">=": 2},
        "probe_result": {"in": ["timeout"]},
        "comprehension": {"==": "confused"},
    }
    assert matches(when, facts) is True
    assert matches({**when, "q_rounds": {">=": 2}}, facts) is False
    print("[F1] 五种算子 + 隐式 AND")


def test_matches_rejects_typos_instead_of_guessing() -> None:
    """错的条件必须不匹配，而不是被 getattr(None) 兜成"意外命中"。"""
    facts = DecisionFacts(unit_id="S03-U02", slide=2, consecutive_confused=2, probe_result="timeout")
    assert matches({"consecutive_confused": {"=>": 2}}, facts) is False      # 算子写错
    assert matches({"confusion": {">=": 1}}, facts) is False                 # key 写错
    assert matches({"confusion": {"==": None}}, facts) is False              # 写错 key 也不许靠比 None 命中
    assert matches({"probe_result": {">=": 2}}, facts) is False              # str 与 int 比大小
    assert matches({"consecutive_confused": 2}, facts) is False              # 条件不是 {算子: 值}
    assert matches(["consecutive_confused"], facts) is False                 # when 根本不是 dict
    print("[F2] 未知 key / 未知算子 / 类型不符一律不匹配")


def test_prompt_block_stays_within_hard_cap() -> None:
    """事实块 ≤100 字是硬约束：任何字段被灌长文本都不许顶破（§4.7）。"""
    normal = DecisionFacts("S03-U02", 2, 2, "timeout", "confused", 1)
    block = normal.to_prompt_block()
    assert len(block) <= MAX_PROMPT_CHARS == 100, block
    for token in ("S03-U02", "第2页", "连续没懂2次", "timeout", "confused", "重讲1轮"):
        assert token in block, (token, block)
    for stuffed in (
        DecisionFacts("U" * 500, 99, 10 ** 9, "x" * 500, "y" * 500, 10 ** 9),
        DecisionFacts("单元" * 300, 2 ** 31 - 1, 999999, "timeout", "confused", 999999),
        DecisionFacts("S01-U01", 0),
    ):
        rendered = stuffed.to_prompt_block()
        assert len(rendered) <= MAX_PROMPT_CHARS, rendered
    print("[F3] 事实块严格 ≤100 字（含灌满长文本的极端值）")


def test_facts_from_state_derivation() -> None:
    """派生规则：键两种都认、理解信号取最坏、连续没懂 = 重讲轮次 + 未答提问、探针取最后有结论的。"""
    unit = _unit(kp=("kp-A",))
    # ① 空状态：一切归零，不抛异常（课堂第一格就该能预取）
    st = ClassroomState()
    f = facts_from_state(st, unit)
    assert (f.unit_id, f.slide) == ("S03-U02", 2)
    assert (f.consecutive_confused, f.probe_result, f.comprehension, f.q_rounds) == (0, "", "", 0)

    # ② machine.py 里 q_rounds / comprehension 的键是 unit_id（state.py 注释写的是 kp）
    st = ClassroomState()
    st.q_rounds["S03-U02"] = 2
    st.comprehension["S03-U02"] = "vague"
    st.pending_questions = [
        Question(raw="没懂", at_unit="S03-U02"),
        Question(raw="还是没懂", at_unit="S03-U02", status=QStatus.ANSWERED),
        Question(raw="刚才那个公式", at_unit="S01-U01"),
    ]
    # 探针是先 append 后 close：最后一条还开着（无结论）时应取前一条有结论的
    st.probe_log = [
        Probe(slide=1, unit_id="S02-U01", opened_at=0.0, closed_at=1.0, result="confirmed"),
        Probe(slide=2, unit_id="S03-U02", opened_at=2.0),
    ]
    f = facts_from_state(st, unit)
    assert f.q_rounds == 2
    assert f.consecutive_confused == 3, "重讲 2 轮 + 本单元未答完的 1 条"
    assert f.probe_result == "confirmed"
    assert f.comprehension == "vague"

    # ③ kp 键也认（state.py 注释的约定），多个 kp 取最坏信号
    st = ClassroomState()
    st.q_rounds["kp-A"] = 1
    st.comprehension["kp-A"] = "confirmed"
    st.comprehension["kp-B"] = "confused"
    f = facts_from_state(st, _unit(kp=("kp-A", "kp-B")))
    assert (f.q_rounds, f.comprehension) == (1, "confused")
    print("[F4] facts_from_state 派生规则（unit_id / kp 双键、最坏信号、pending 计数、探针取已结论）")
