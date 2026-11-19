"""课堂脚本生成测试 —— 从真实课件到"能上课的脚本"。

三件事必须成立，否则"AI 操作 PPT"就是假的：
1. **锚点是真锚点**：每个单元的锚点都在课件里存在，且尽量细到段落
   （`s02-sh02-p03` 这种）。锚点错了 = 老师在指着别的地方讲。
2. **单元长度受控**：文档 §2.3 的 40~180 字（硬上限 250）决定了
   "学生最坏等待时长"，这是让位机制的前提。
3. **模型输出不可信也要能用**：JSON 烂了、锚点编造了、句号超范围了，
   都必须被修好或丢掉并留下告警 —— 绝不能让学生听到一串 raw JSON。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deck.schema import deck_overview  # noqa: E402
from script_gen import (  # noqa: E402
    HARD_MAX_CHARS,
    MAX_CHARS,
    MIN_CHARS,
    LessonPlan,
    PlanUnit,
    _fallback_anchor,
    audit_plan,
    build_lesson_script,
    measure_plan,
    plan_markers,
    plan_rule,
    parse_units_json,
    prompt_slide,
    prune_noop_ops,
)
from state import Anchor, LectureUnit  # noqa: E402
from tests.fixtures import make_deck  # noqa: E402


def test_rule_plan_respects_length_and_anchors() -> None:
    deck = make_deck()
    plan = plan_rule(deck)
    assert plan.units, "合成课件应该能规划出单元"
    cov = plan.coverage or measure_plan(deck, plan)
    assert cov["anchor_hit_rate"] == 1.0, cov
    assert cov["ops_invalid"] == 0, cov
    index = deck.anchor_index()
    # 每页可讲字数（决定这页"允许短"还是必须凑够长度）
    page_chars = {s.index: sum(len(e.plain_text) for e in s.elements) for s in deck.slides}
    for n, u in enumerate(plan.units):
        tag = f"P{u.slide + 1:02d}#{n}"
        # 页面上本来就没几个字的（封面），允许短 —— 没东西可合
        if page_chars.get(u.slide, 0) >= MIN_CHARS + 10:
            assert MIN_CHARS <= len(u.text), f"{tag} 太短：{len(u.text)}"
        assert len(u.text) <= HARD_MAX_CHARS, f"{tag} 超硬上限：{len(u.text)}"
        assert u.anchors, f"{tag} 没有锚点"
        for a in u.anchors:
            assert a["element_id"] in index, a
        assert any("-p" in a["element_id"] for a in u.anchors), \
            f"{tag} 没有任何段落级锚点 —— 真实 PPT 的讲解粒度就是段落"
        assert u.ops, f"{tag} 没有课件操作（分步显示就没了）"
    # 每页最后一个单元必须标成页尾 —— 翻页门控靠它
    for s in deck.slides:
        us = plan.units_of(s.index)
        if us:
            assert us[-1].is_page_end and not any(u.is_page_end for u in us[:-1]), \
                f"第 {s.index + 1} 页的页尾标记不对"
    print(f"[G1] 规则兜底：{cov['units']} 个单元 · 锚点命中 {cov['anchor_hit_rate'] * 100:.0f}%"
          f" · 段落级 {cov['para_level_anchors']} · 最长单元 {cov['longest_unit_sec']:.1f}s")


def test_ops_at_sentence_stay_in_range() -> None:
    """规则生成的 op 句号必须落在单元句数之内 —— 否则内容永远不出现。"""
    from llm import split_sentences

    deck = make_deck()
    plan = plan_rule(deck)
    for n, u in enumerate(plan.units):
        cnt = len(split_sentences(u.text))
        for op in u.ops:
            assert 1 <= op["at_sentence"] <= cnt, \
                f"P{u.slide + 1:02d}#{n} 的 op 落在第 {op['at_sentence']} 句，但只有 {cnt} 句"
    print("[G2] 规则生成的课件操作句号全部在范围内")


def test_lesson_script_is_playable() -> None:
    """生成结果必须能直接喂给状态机（字段齐全、id 对得上）。"""
    deck = make_deck()
    plan = plan_rule(deck)
    script = build_lesson_script(deck, plan)
    assert script.units and script.slides
    # 单元里的锚点必须能在页面上找到（含段落级）
    for u in script.units:
        page = script.slide_at(u.slide)
        assert page is not None, u.unit_id
        for a in u.anchors:
            assert page.element(a.element_id) is not None, \
                f"{u.unit_id} 的锚点 {a.element_id} 不在第 {u.slide + 1} 页上"
            assert a.text_hint, "锚点要带文字提示，否则高亮处没有可读的说明"
    # 段落级锚点：形状 id 与其段落 id 都在
    word_page = script.slide_at(1)
    assert word_page.element("s02-sh02") is not None
    assert word_page.element("s02-sh02-p05") is not None, \
        "段落级锚点没注册进页面（高亮不到具体的行）"
    assert word_page.parent_of("s02-sh02-p05") == "s02-sh02", "段落与形状的父子关系要建立"
    # 单元长度/时长字段可用
    for u in script.units:
        assert u.est_sec > 0
        assert u.ops, "脚本里要带上课件操作"
    print(f"[G3] 生成脚本可用于上课：{len(script.slides)} 页 / {len(script.units)} 单元"
          f" / 预计 {script.total_est_sec / 60:.1f} 分钟")


def test_parse_units_json_repairs_bad_model_output() -> None:
    """模型返回的 JSON 脏得离谱也要能用 —— 这是最常出问题的一环。"""
    deck = make_deck()
    s = deck.slides[1]                     # 单词页
    raw = json.dumps({
        "units": [
            {   # 正常单元，但锚点里混了一个不存在的 + 一个段落级
                "text": "p-a-c-k，pack，动词，表示打包、收拾。注意它是可数名词时读作 a pack of。",
                "anchors": ["s02-sh02-p01", "s02-sh99-p77", "s02-sh03"],
                "ops": [{"op": "step", "target": "s02-sh02-p06", "at_text": "注意它是可数"},
                        {"op": "teleport", "target": "s02-sh02-p06"}],
            },
            {   # 没有 text：整条丢掉
                "anchors": ["s02-sh02-p02"],
            },
            {   # 句号越界：clamp 到范围内
                "text": "接下来看搭配，pack up 表示收拾行李，pack for 表示为……打包。",
                "anchors": ["s02-sh02-p05"],
                "ops": [{"op": "highlight", "target": "s02-sh02-p05", "at_sentence": 999}],
            },
        ]
    }, ensure_ascii=False)
    units, warns = parse_units_json(raw, s, deck)
    assert len(units) == 2, [u.text for u in units]
    # 不存在的锚点被丢掉，不存在的 op 被丢掉，并且留下了告警
    assert all(a["element_id"] != "s02-sh99-p77" for a in units[0].anchors)
    assert [a["element_id"] for a in units[0].anchors] == ["s02-sh02-p01", "s02-sh03"]
    assert [o["op"] for o in units[0].ops] == ["step"]
    assert units[1].ops[0]["at_sentence"] <= 3
    assert warns, "修过的东西必须留告警（审核页要显示）"
    # 完全不是 JSON 时不能抛异常
    units2, warns2 = parse_units_json("模型今天心情不好，不输出 JSON", s, deck)
    assert units2 == [] and warns2
    print(f"[G4] 脏 JSON 被修复并留告警（{len(warns)} 条）："
          f"丢虚构锚点 / 丢非法操作 / 句号收敛")


def test_audit_reports_the_promise_metric() -> None:
    """体检报告要给出 V7 口径的数字 —— 这是文档里承诺过的可测指标。"""
    deck = make_deck()
    plan = plan_rule(deck)
    lines = audit_plan(deck, plan, HARD_MAX_CHARS)
    text = "\n".join(lines)
    assert "锚点命中率" in text and "最长单元" in text, text
    assert "❌" not in text, "规则兜底的输出不该有硬错误：\n" + text
    print("[G5] 体检报告包含 V7 锚点命中率与最长单元")


def test_prompt_includes_slide_text_and_anchors() -> None:
    """提示词必须同时给出**原文**和**可用锚点 id** ——
    只给原文，模型会编造 id；只给 id，模型不知道在讲什么。"""
    deck = make_deck()
    s = deck.slides[1]
    p = "\n".join(m["content"] for m in prompt_slide(deck, s, goals=["掌握 pack 的用法"]))
    assert "s02-sh02-p01" in p and "s02-sh02-p06" in p, "锚点 id 没进提示词"
    assert "音标" in p and "pack" in p, "原文没进提示词"
    assert "at_text" in p, "没告诉模型怎么对齐语音"
    assert "第 2 页" in p, "没告诉模型这是第几页"
    print("[G6] 提示词同时包含原文与可用锚点")


def test_deck_overview_is_readable() -> None:
    """`deck_overview` 是解析完给人看的第一眼，必须说得清"解析到了什么"。"""
    deck = make_deck()
    out = deck_overview(deck, limit=3)
    assert "fixture_deck" in out and "1280" in out
    assert "第 1 页" in out or "1." in out
    print("[G7] 课件概览可读")


def test_noop_restore_ops_are_pruned() -> None:
    """"复原"类操作没有前置状态时是空操作，必须被清掉。

    真实备课计划里就出现过 3 个这样的 `reset_zoom`（整份计划里根本没有 `zoom`）：
    发下去什么都不会发生，却照样计入"课件操作 下发 N"，把真正没命中的操作淹掉。
    同时确认**正常的复原不会被误删**（先放大再复原 = 有意义）。
    """
    deck = make_deck()
    plan = LessonPlan(deck_id=deck.deck_id, units=[
        PlanUnit(slide=0, text="第一页讲一句。", ops=[
            {"op": "reset_zoom"},                       # 空操作 → 删
            {"op": "unfocus"},                          # 空操作 → 删
            {"op": "highlight", "target": "s01-sh01"},
        ]),
        PlanUnit(slide=1, text="第二页先放大再复原。", ops=[
            {"op": "zoom", "target": "s02-sh02-p01", "scale": 1.8},
            {"op": "reset_zoom"},                       # 有意义 → 留
        ]),
    ])
    dropped = prune_noop_ops(plan)
    assert len(dropped) == 2, f"应该删掉 2 个空操作，实际 {len(dropped)}：{dropped}"
    first = plan.units_of(0)[0].ops
    assert [o["op"] for o in first] == ["highlight"], f"第一页剩下 {first}"
    second = [o["op"] for o in plan.units_of(1)[0].ops]
    assert second == ["zoom", "reset_zoom"], f"第二页的成对操作被误删：{second}"
    assert prune_noop_ops(plan) == [], "再跑一次不该还有可删的（幂等）"
    print("[G8] 空操作被清掉，成对的复原操作保留")


def test_audit_recomputes_stale_coverage() -> None:
    """计划被改动过之后，体检里的数字必须重算，不能照着旧快照报。

    踩过的样子：`--fix-ops` 清掉 3 个空操作后，报告里还写着"课件操作 163 个" ——
    报的和实际的不一致，比不报更坏。判据是单元数对账（计划一变，单元数几乎必变）。
    """
    deck = make_deck()
    plan = plan_rule(deck)
    plan.coverage = {"units": 999, "ops": 999, "anchors": 999, "anchor_hit": 999,
                     "est_sec": 0, "longest_unit_sec": 0, "units_over_hard_max": 0,
                     "para_level_anchors": 0, "low_confidence_anchors": 0,
                     "ops_invalid": 0, "anchor_hit_rate": 0}
    text = "\n".join(audit_plan(deck, plan, HARD_MAX_CHARS))
    assert "已重算" in text, "没有发现计划已改动、还在照旧快照报数：\n" + text
    assert f"单元数 {len(plan.units)}" in text, text
    assert "课件操作 999" not in text, text
    print("[G9] 计划改动后体检数字会重算（不照旧快照报）")


def test_audit_reports_op_mix() -> None:
    """体检要印出**操作类型分布**：只看总数看不出"备课只用了一种操作"。

    真实课件第一版计划：160 个操作全是 step/highlight，一个 focus/zoom 都没有 ——
    没有分布这一行，这个缺口根本不会被人发现。
    """
    deck = make_deck()
    plan = plan_rule(deck)
    text = "\n".join(audit_plan(deck, plan, HARD_MAX_CHARS))
    assert "课件操作" in text, text
    if plan.units and any(u.ops for u in plan.units):
        assert "（" in text.split("课件操作")[1].split("\n")[0], \
            "操作类型分布没印出来：\n" + text
    print("[G10] 体检报告印出操作类型分布")


def test_fallback_anchor_is_coarse_and_deterministic() -> None:
    """模型一个锚点都没给时，兜底要锚**整块**，而且必须**每次都一样**。

    之前兜底写的是 `next(iter(para_ids))` —— `para_ids` 是 set，"第一个"是哈希顺序，
    同一页两次备课可能锚到不同的行；实测还正好锚到了"community center 社区中心"那一行，
    而老师说的是"记一下这些搭配"。学生只会以为要记这一条。
    """
    deck = make_deck()
    s = deck.slides[1]
    got = {_fallback_anchor(s) for _ in range(8)}
    assert len(got) == 1, f"兜底锚点不确定：{got}"
    aid = got.pop()
    assert aid in {e.element_id for e in s.elements}, \
        f"兜底应该给形状级 id（整块），实际给了 {aid}"
    print("[G11] 兜底锚点确定、且指向整块（不锚某一行）")


def test_markers_follow_unit_and_anchor_order() -> None:
    """标记清单的顺序 = 单元顺序 → 单元内锚点顺序。

    前端按这份顺序逐句点灯；顺序一乱就会出现"讲第 3 单元时亮了第 1 单元的行"。
    """
    from llm import split_sentences

    deck = make_deck()
    script = build_lesson_script(deck, plan_rule(deck))
    markers = plan_markers(script.units)
    want = [(u.unit_id, a.element_id)
            for u in script.units
            for a in u.anchors[:len(split_sentences(u.text))]]
    assert markers, "真实规划出来的脚本应该能排出标记"
    assert [(m["unit_id"], m["element_id"]) for m in markers] == want
    print(f"[G12] 标记清单按 单元→锚点 顺序展开（{len(markers)} 个标记 / "
          f"{len(script.units)} 个单元）")


def test_marker_sentences_are_monotonic_and_in_range() -> None:
    """同一单元内句号从 1 起、严格递增、不超句数 —— 这就是"一次只亮一个"的保证。

    同一句塞两个锚点，等于整单元批量高亮又回来了；句号超出句数，
    那个锚点永远等不到属于自己的那句话，学生就一直看不到那块内容被点亮。
    """
    from llm import split_sentences

    deck = make_deck()
    script = build_lesson_script(deck, plan_rule(deck))
    markers = plan_markers(script.units)
    for u in script.units:
        n = len(split_sentences(u.text))
        sents = [m["sentence"] for m in markers if m["unit_id"] == u.unit_id]
        assert sents == list(range(1, len(sents) + 1)), \
            f"{u.unit_id} 的标记句号不是从 1 起的严格递增序列：{sents}"
        assert all(1 <= s <= n for s in sents), \
            f"{u.unit_id} 的标记句号超出 {n} 句：{sents}"
    print(f"[G13] 单元内标记句号从 1 起严格递增且不超句数（{len(markers)} 个标记）")


def test_extra_anchors_beyond_sentence_count_are_dropped() -> None:
    """锚点比句子多时，超额的丢掉 —— 不减到"每句一个"就会一句亮两块。"""
    from llm import split_sentences

    text = "先看音标，这一段凑够八个字。再看释义，这一段也够长。最后一句收尾，同样够长。"
    unit = LectureUnit(unit_id="S09-U01", slide=8, index_in_slide=0, text=text,
                       anchors=[Anchor(element_id=f"s09-sh01-p{i:02d}")
                                for i in range(1, 7)])
    n = len(split_sentences(text))
    markers = plan_markers([unit])
    assert n == 3, f"测试文本的句数变了（{n} 句），下面的断言要跟着改"
    assert len(markers) == n, f"应该正好留下 {n} 个标记，实际 {len(markers)}：{markers}"
    assert [m["sentence"] for m in markers] == [1, 2, 3]
    assert [m["element_id"] for m in markers] == [f"s09-sh01-p{i:02d}" for i in (1, 2, 3)]
    print("[G14] 6 个锚点 / 3 句 → 超额 3 个被丢弃（一句只亮一个）")


def test_markers_empty_input_is_empty() -> None:
    """没有单元、或单元没有锚点时给空清单 —— 课堂不该因为没东西可亮就停下。"""
    assert plan_markers([]) == []
    bare = LectureUnit(unit_id="S01-U01", slide=0, index_in_slide=0,
                       text="这一单元其实一个锚点都没有。")
    assert plan_markers([bare]) == []
    print("[G15] 空输入 / 无锚点单元 → 空清单")


ALL = [
    test_rule_plan_respects_length_and_anchors,
    test_ops_at_sentence_stay_in_range,
    test_lesson_script_is_playable,
    test_parse_units_json_repairs_bad_model_output,
    test_audit_reports_the_promise_metric,
    test_prompt_includes_slide_text_and_anchors,
    test_deck_overview_is_readable,
    test_noop_restore_ops_are_pruned,
    test_audit_recomputes_stale_coverage,
    test_audit_reports_op_mix,
    test_fallback_anchor_is_coarse_and_deterministic,
    test_markers_follow_unit_and_anchor_order,
    test_marker_sentences_are_monotonic_and_in_range,
    test_extra_anchors_beyond_sentence_count_are_dropped,
    test_markers_empty_input_is_empty,
]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
    print(f"\n脚本生成结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
