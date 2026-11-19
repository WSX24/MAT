"""课件操作（DeckOp）测试 —— 让"AI 真正操作 PPT"这件事可执行地被验证。

要证明的三件事：
1. 操作模型本身不丢信息（序列化往返、target/targets 合并、句号合法化）
2. 非法操作**能被审核挑出来**（指向不存在的元素、句号超出范围、op 名打错）
3. 操作在**句边界**下发（状态机逐句产出时，第 N 句带出 at_sentence=N 的操作）
   —— 这是"说什么"和"指哪里"对齐的唯一机制，错位了就等于老师指着别处讲课。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deck.ops import (  # noqa: E402
    DeckOp,
    check_ops_in_range,
    clear,
    focus,
    highlight,
    ops_at_sentence,
    point,
    reveal,
    unfocus,
    validate_ops,
    zoom,
)
from machine import Machine, PhaseHandlers  # noqa: E402
from state import ClassroomState  # noqa: E402
from tests.fixtures import make_script_with_ops  # noqa: E402
from tests.test_machine import FakeLLM, make_settings  # noqa: E402


# ===========================================================================
# 1. 操作模型
# ===========================================================================

def test_op_round_trip() -> None:
    op = DeckOp(op="step", at_sentence=2, target="s02-sh02-p03", scale=1.7, note="放例句")
    d = op.to_dict()
    again = DeckOp.from_dict(d)
    assert again.op == "step" and again.at_sentence == 2
    assert again.target == "s02-sh02-p03" and again.scale == 1.7
    # 空字段不该落盘（plan.json 要给人看）
    assert "note" not in DeckOp(op="clear").to_dict()
    # 容错：模型可能给 targets 而不是 target，或者给个字符串
    assert DeckOp.from_dict({"op": "highlight", "targets": ["a", "b"]}).target == "a"
    assert DeckOp.from_dict({"op": "highlight", "targets": "a"}).anchor_ids == ["a"]
    assert DeckOp.from_dict({"op": "x", "at_sentence": "abc"}).at_sentence == 1
    assert DeckOp.from_dict({"op": "x", "at_sentence": -3}).at_sentence == 1
    print("[O1] 操作序列化往返与容错 OK")


def test_op_helpers_and_selection() -> None:
    ops = [reveal(["e1"], 1), point("e1", 2), highlight(["e2"], 2), unfocus(3)]
    assert [o.op for o in ops] == ["step", "point", "highlight", "unfocus"]
    assert [o.op for o in ops_at_sentence(ops, 2)] == ["point", "highlight"]
    assert ops_at_sentence(ops, 1)[0].anchor_ids == ["e1"]
    assert focus(["e1"], 1).op == "focus"
    assert zoom("e1", 1).scale == 1.7
    assert clear(1).op == "clear"
    print("[O2] 组合构造与按句选取 OK")


def test_validation_catches_real_mistakes() -> None:
    known = {"s01-sh01", "s01-sh01-p01", "s01-sh02"}
    bad = [
        DeckOp(op="teleport", target="s01-sh01"),          # op 名不存在
        DeckOp(op="highlight", target=""),                  # 没有 target
        DeckOp(op="highlight", target="s09-sh99"),          # 目标不在本页
        DeckOp(op="reveal", target="s01-sh01", at_sentence=0),  # 句号非法
    ]
    msgs = validate_ops(bad, known, unit_id="S01-U01")
    assert len(msgs) == 4, msgs
    assert any("不认识的操作" in m for m in msgs)
    assert any("没有 target" in m for m in msgs)
    assert any("在本页不存在" in m for m in msgs)
    assert any("从 1 起算" in m for m in msgs)
    # 合法的一批不该报任何问题
    assert validate_ops([highlight(["s01-sh01"], 1)], known) == []
    # 句号超出单元句数 = 这个操作永远不会发生，必须报出来
    assert check_ops_in_range([reveal(["a"], 9)], 3, unit_id="U")
    assert check_ops_in_range([reveal(["a"], 3)], 3) == []
    print("[O3] 非法操作被确定性挑出（含「永远不会发生」的那一类）")

# ===========================================================================
# 2. 状态机：操作在句边界下发
# ===========================================================================

class Driver:
    """无头驱动：逐句取段、记录每段带出的操作。"""

    def __init__(self):
        self.script = make_script_with_ops()
        self.state = ClassroomState(course_id="fixture")
        self.machine = Machine(self.script, self.state, make_settings(),
                               FakeLLM(), PhaseHandlers.noop())

    async def drain(self) -> list:
        """一直取段直到课程结束（每句都当成"立刻播完"）。"""
        out = []
        for _ in range(200):
            seg = await self.machine.next_speech()
            if seg is None:
                break
            out.append(seg)
            await self.machine.on_speech_end(seg.kind)
            if seg.kind == "ask":          # 探针提问后等窗口，直接跳过
                continue
        return out


def test_ops_are_emitted_at_sentence_boundaries() -> None:
    """操作的"句号"= 实际播放分块号。

    注意：这里的"句"不是语法句。`llm.split_sentences` 会把过短的句子
    **并进下一句**（≥8 字才在句末标点处断），所以
    "这是第一句话。这是第二句话，要放出一段内容。" 是**同一句**。
    这正是 `at_text` 存在的理由：让模型去数这种分块必然数错。
    """
    segs = asyncio.run(Driver().drain())
    unit_segs = [s for s in segs if s.kind == "unit" and s.unit_id == "S01-U01"]
    assert len(unit_segs) == 2, [s.text for s in unit_segs]

    def ops_of(i):
        return [(o.op, o.target) for o in unit_segs[i].ops]

    # 第 1 句：藏 + 放（两个 op 落在同一句上是有意的 —— 先藏后放不会闪，
    # 因为隐藏发生在渲染时、放出发生在第一句）
    assert ops_of(0) == [("hide", "s01-sh01-p01"), ("step", "s01-sh01-p01"),
                         ("point", "s01-sh01-p01")], ops_of(0)
    assert ops_of(1) == [("highlight", "s01-sh02")], ops_of(1)

    # 锚点仍然只在单元首句下发（老行为不能变）
    assert [len(s.anchors) for s in unit_segs] == [1, 0]

    # 句号超出单元句数的 op **不执行**：不隐藏、不报错，由审核去报
    last = [s for s in segs if s.kind == "unit" and s.unit_id == "S02-U01"]
    assert all(not s.ops for s in last)
    print("[O4] 操作落在正确的句边界，超范围操作不误执行")


def test_ops_by_text_resolve_to_the_right_chunk() -> None:
    """按文字定位：LLM 只要抄对一小段原文，操作就能对齐到正确的那一句。

    这是给模型用的定位方式 —— 数句子它一定数错，抄文字它抄得对。
    """
    from deck.ops import DeckOp, resolve_ops
    from llm import split_sentences

    text = "第一句讲音标，读作 pack。第二句讲搭配，pack up 是收拾行李。第三句看例句。"
    sents = split_sentences(text)
    ops = [DeckOp(op="highlight", target="x", at_text="pack up"),
           DeckOp(op="step", target="y", at_text="看例句"),   # 原文是"看例句"，这里少一个字
           DeckOp(op="point", target="z", at_text="这句正文里根本没有")]
    resolved = resolve_ops(ops, sents)
    assert resolved[0].at_text == "" and resolved[0].at_sentence >= 1
    # "pack up" 落在含它的那一句上
    assert "pack up" in sents[resolved[0].at_sentence - 1]
    # "看例句" 与原文"第三句看例句"近似匹配（模型改写措辞时靠这条救回来）
    assert "看例句" in sents[resolved[1].at_sentence - 1]
    # 完全对不上 → 退回 at_sentence，并由 check_ops_text 在审核时报 ❌
    assert resolved[2].at_sentence == 1
    from deck.ops import check_ops_text

    msgs = check_ops_text(ops, text, unit_id="U1")
    assert len(msgs) == 1 and msgs[0].startswith("❌") and "根本没有" in msgs[0], msgs
    # 近似匹配（改了措辞但明显指同一句）要报 ⚠️ 而不是 ❌
    near = check_ops_text([DeckOp(op="step", target="y", at_text="pack up 用来收拾行李")], text)
    assert near and near[0].startswith("⚠️"), near
    print("[O6] at_text 定位：原文/近似两档解析，失败会被审核报出")


def test_ops_fall_back_to_target_text() -> None:
    """`at_text` 编错了（模型改写措辞）时，用**目标元素自己的文字**救回来。

    模型很爱写"后接物品名词"这种自己造的措辞，但它指的往往是课件上某一段 ——
    而那段文字就在正文里。这一级兜底比"退回第 1 句"准得多：
    至少操作落在"正在讲那段"的时候，而不是一开口就全放出来。
    """
    from deck.ops import DeckOp, resolve_ops
    from llm import split_sentences

    text = "先看音标，读作 pack。再看搭配，pack up 是收拾行李。最后看例句，I need to pack my clothes。"
    sents = split_sentences(text)
    ops = [DeckOp(op="step", target="s02-sh02-p05", at_text="后接物品名词")]   # 正文里没有
    resolved = resolve_ops(ops, sents, anchor_text={"s02-sh02-p05": "pack up 是收拾行李。"})
    idx = resolved[0].at_sentence
    assert "pack up 是收拾行李" in sents[idx - 1], (
        f"应落到「句含目标文字」的那一句，实际第 {idx} 句：{sents[idx - 1]}")
    print("[O7] at_text 编错时按目标元素文字兜底定位")


def test_manual_commands_still_work_with_ops() -> None:
    """加了操作通道之后，手动翻页/重复不能坏（它们是学生按钮的直接入口）。"""

    async def go():
        d = Driver()
        seg = await d.machine.next_speech()
        assert seg is not None and seg.kind == "unit"
        await d.machine.handle_command("next")
        assert d.state.slide_index == 1, d.state.slide_index
        await d.machine.handle_command("prev")
        assert d.state.slide_index == 0
        return d

    asyncio.run(go())
    print("[O5] 手动翻页不受操作通道影响")


ALL = [
    test_op_round_trip,
    test_op_helpers_and_selection,
    test_validation_catches_real_mistakes,
    test_ops_are_emitted_at_sentence_boundaries,
    test_manual_commands_still_work_with_ops,
    test_ops_by_text_resolve_to_the_right_chunk,
    test_ops_fall_back_to_target_text,
]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
    print(f"\n课件操作结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
