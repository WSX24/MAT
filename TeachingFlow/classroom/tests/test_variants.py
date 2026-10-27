"""变体生产测试 —— 规则模板的确定性、§5.4 的同一把尺子、以及向后兼容。

三件事必须成立，否则"课堂上换个说法"就是假的：
1. **确定性**：同样输入跑两次得到同样的变体（不联网、不用随机数、不用 `hash()`）。
2. **同一把尺子**：变体过与 v0 完全相同的体检 —— §5.4 点名的"v0 合规、v1 却是
   600 字巨兽"必须被拦住，否则选中它学生就干等两分钟。
3. **向后兼容**：没有 `variants` 键的旧 plan.json 仍能加载、仍能上课。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import variants as variants_mod  # noqa: E402
from agent.variants import (  # noqa: E402
    VARIANT_LABELS,
    attach_variants,
    make_variants,
    validate_variant,
)
from script_gen import (  # noqa: E402
    LessonPlan,
    PlanUnit,
    audit_plan,
    build_lesson_script,
    plan_rule,
)
from tests.fixtures import make_deck  # noqa: E402

#: 干净的两句话（每句都 ≥8 字，`llm.split_sentences` 才认它是两句）
SAMPLE = "这个词表示把东西收拾进箱子。它既可以单独用，也可以和 up 连用。"


def _variant(text: str = "这是一句合规的变体文本。", **kw) -> dict:
    v = {"variant_id": "v1", "label": "换说法", "text": text,
         "anchors": [], "ops": [], "est_sec": round(len(text) / 5.0, 1)}
    v.update(kw)
    return v


# ===========================================================================
# make_variants：确定性 + 非空 + 与原意相关
# ===========================================================================

def test_make_variants_is_deterministic() -> None:
    """同一输入两次调用结果完全相同（AC3）。"""
    first = make_variants(SAMPLE)
    second = make_variants(SAMPLE)
    assert first == second, "同样输入必须同样输出（不允许随机数/时钟/hash()）"
    assert [v["variant_id"] for v in first] == ["v1", "v2"]
    assert [v["label"] for v in first] == list(VARIANT_LABELS)
    assert all(v["text"].strip() for v in first), "变体不得是空串"


def _run_in_fresh_process(text: str, hash_seed: str) -> str:
    proto = Path(__file__).resolve().parent.parent
    code = ("import sys; sys.path.insert(0, '.');"
            "from agent.variants import make_variants;"
            f"print([v['text'] for v in make_variants({text!r})])")
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, "-c", code], cwd=str(proto), env=env,
                          capture_output=True, text=True, encoding="utf-8", check=True)
    return done.stdout.strip()


def test_make_variants_does_not_depend_on_hash_seed() -> None:
    """换一个 PYTHONHASHSEED 跑，输出必须逐字相同 —— 这条专抓误用内置 hash()。"""
    assert _run_in_fresh_process(SAMPLE, "0") == _run_in_fresh_process(SAMPLE, "12345")


def test_make_variants_returns_empty_for_too_short_text() -> None:
    """文本为空或不足一句 → 没有可变的内容，返回空列表。"""
    assert make_variants("") == []
    assert make_variants("   ") == []
    assert make_variants("目录。") == []          # 3 字，连一句都构不成
    assert make_variants("好") == []


def test_make_variants_envelope_matches_unitvariant() -> None:
    """dict 的键与 §4.1 的 UnitVariant 逐字段对应；anchors/ops 留空表示沿用主单元。"""
    v1, v2 = make_variants(SAMPLE)
    for v in (v1, v2):
        assert set(v) == {"variant_id", "label", "text", "anchors", "ops", "est_sec"}
        assert v["anchors"] == [] and v["ops"] == []
        assert v["est_sec"] == pytest.approx(len(v["text"]) / 5.0, abs=0.06)


def test_make_variants_stays_related_to_source() -> None:
    """变体是原文句子重排，不是胡乱拼接：原文两句话都还在里面。"""
    sents = [s for s in SAMPLE.split("。") if s]
    for v in make_variants(SAMPLE):
        for s in sents:
            assert s[:12] in v["text"], f"变体丢了原句：{s}"


def test_make_variants_v2_leads_with_example_sentence() -> None:
    """v2（举例子）把像例句的那句提到最前 —— 先给例子，再回来说明。"""
    text = "例句：I need to pack my clothes。释义：打包；收拾。"
    v2 = make_variants(text)[1]
    assert v2["label"] == "举例子"
    assert v2["text"].index("例句") < v2["text"].index("释义")


def test_make_variants_respects_char_budget_for_long_source() -> None:
    """原文顶到硬上限时，变体仍要落在 250 字以内（生成侧自己守线，不靠事后被拒）。"""
    long_text = "这个词表示把东西收拾进箱子，常和 up 连用。" * 12
    assert len(long_text) > 250
    for v in make_variants(long_text):
        assert 0 < len(v["text"]) <= 250


# ===========================================================================
# validate_variant：与 v0 同一把尺子（§5.4）
# ===========================================================================

def test_validate_variant_accepts_generated_variants() -> None:
    """规则模板产出的变体必须能过体检，否则 `attach_variants` 一个都挂不上。"""
    deck = make_deck()
    slide = deck.slide_at(1)
    for u in plan_rule(deck).units_of(1):
        for v in make_variants(u.text, kp=list(u.kp)):
            assert validate_variant(v, slide) == [], v


def test_validate_variant_rejects_giant_variant_that_v0_would_pass() -> None:
    """§5.4 点名的场景：v0 合规（≤250 字），某个变体却是 600 字的巨兽 → 必须被拒。"""
    deck = make_deck()
    slide = deck.slide_at(1)
    v0_text = "pack 表示打包，也常和 up 连用。" * 12
    assert len(v0_text) <= 250, "这个用例的前提是 v0 自己合规"
    v0_plan = LessonPlan(units=[PlanUnit(slide=1, text=v0_text,
                                         anchors=[{"element_id": "s02-sh02-p03"}])])
    v0_msgs = audit_plan(deck, v0_plan)
    assert not any("❌ 超出硬上限" in m for m in v0_msgs), v0_msgs

    giant_text = "巨兽" * 300
    assert len(giant_text) == 600
    msgs = validate_variant(_variant(giant_text), slide)
    assert any("600" in m and "超出上限 250" in m for m in msgs), msgs
    # 同一把尺子：同样的 600 字当成 v0 喂给 audit_plan，也必须是"超出硬上限"
    giant_as_v0 = LessonPlan(units=[PlanUnit(slide=1, text=giant_text)])
    assert any("❌ 超出硬上限" in m for m in audit_plan(deck, giant_as_v0))


def test_validate_variant_rejects_empty_text() -> None:
    msgs = validate_variant(_variant("   "), make_deck().slide_at(1))
    assert any("文本为空" in m for m in msgs), msgs


def test_validate_variant_rejects_anchor_missing_on_page() -> None:
    """锚点必须是**本页**存在的元素（跨页的也不行）。"""
    deck = make_deck()
    slide = deck.slide_at(1)
    msgs = validate_variant(_variant(anchors=[{"element_id": "s02-sh99"}]), slide)
    assert any("s02-sh99" in m for m in msgs), msgs
    # s01-sh01 是第 1 页的元素：在本页不存在 → 同样要拒
    msgs = validate_variant(_variant(anchors=[{"element_id": "s01-sh01"}]), slide)
    assert any("s01-sh01" in m for m in msgs), msgs


def test_validate_variant_accepts_paragraph_anchor() -> None:
    """段落 pid 也算本页元素（`slide_from_deck` 把段落也注册成元素）。"""
    slide = make_deck().slide_at(1)
    assert slide.find_anchor("s02-sh02-p03") is not None
    assert validate_variant(_variant(anchors=[{"element_id": "s02-sh02-p03"}]), slide) == []


def test_validate_variant_rejects_ops_beyond_sentence_count() -> None:
    """ops 的 at_sentence 不得超过**本变体**的句数（不是 v0 的句数）。"""
    slide = make_deck().slide_at(1)
    text = "这是第一句讲解的内容。这是第二句讲解的内容。这是第三句讲解的内容。"
    bad = _variant(text, ops=[{"op": "point", "at_sentence": 9, "target": "s02-sh02"}])
    msgs = validate_variant(bad, slide)
    assert any("第 9 句" in m for m in msgs), msgs
    ok = _variant(text, ops=[{"op": "point", "at_sentence": 3, "target": "s02-sh02"}])
    assert validate_variant(ok, slide) == []


def test_validate_variant_honours_char_warn_argument() -> None:
    """尺子的阈值可传：收紧到 100 字，120 字的变体就该被拒。"""
    slide = make_deck().slide_at(1)
    v = _variant("字" * 120)
    assert validate_variant(v, slide, char_warn=100)
    assert validate_variant(v, slide, char_warn=250) == []


# ===========================================================================
# attach_variants：只挂通过校验的，被拒原因进 warnings
# ===========================================================================

def test_attach_variants_attaches_only_valid_and_warns(monkeypatch) -> None:
    """只挂通过校验的变体；被拒原因进 plan.warnings；返回值 = 实际挂上的数量（AC5）。"""
    deck = make_deck()
    plan = plan_rule(deck)
    good = _variant("这是一句合规的变体文本。")
    bad = _variant("巨兽" * 300, variant_id="v2", label="举例子")
    calls: list[str] = []

    def fake_make_variants(text, *, kp=None):
        calls.append(text)
        return [dict(good), dict(bad)]

    monkeypatch.setattr(variants_mod, "make_variants", fake_make_variants)
    attached = variants_mod.attach_variants(plan, deck)

    assert len(calls) == len(plan.units), "每个单元都该试着生成一次"
    assert attached == len(plan.units) == sum(len(u.variants) for u in plan.units)
    assert all([v["variant_id"] for v in u.variants] == ["v1"] for u in plan.units)
    assert len(plan.warnings) == len(plan.units), plan.warnings
    assert all("超出上限" in w for w in plan.warnings), plan.warnings


def test_attach_variants_on_rule_plan_returns_real_count() -> None:
    deck = make_deck()
    plan = plan_rule(deck)
    attached = attach_variants(plan, deck)
    assert attached == sum(len(u.variants) for u in plan.units)
    assert attached > 0
    assert plan.warnings == [], plan.warnings
    assert all(len(u.variants) == 2 for u in plan.units if len(u.text) >= 8)


def test_attach_variants_warns_when_page_missing_from_deck() -> None:
    """计划里的页在课件里找不到 → 跳过并留痕，而不是抛异常。"""
    plan = LessonPlan(units=[PlanUnit(slide=99, text=SAMPLE)])
    assert attach_variants(plan, make_deck()) == 0
    assert plan.units[0].variants == []
    assert any("99" in w or "100" in w for w in plan.warnings), plan.warnings


# ===========================================================================
# 向后兼容：旧 plan.json 没有 variants 键
# ===========================================================================

def test_old_plan_json_without_variants_still_loads_and_builds(tmp_path) -> None:
    """旧 plan.json（无 variants 键）能加载，产出的 LectureUnit.variants 是空列表（AC1）。"""
    deck = make_deck()
    raw = json.loads(plan_rule(deck).to_json())
    for u in raw["units"]:
        u.pop("variants", None)
    assert all("variants" not in u for u in raw["units"])
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    old = LessonPlan.load(path)
    assert old.units
    assert all(u.variants == [] for u in old.units)

    script = build_lesson_script(deck, old)
    assert script.units
    assert all(u.variants == [] for u in script.units)


def test_plan_json_round_trip_keeps_variants(tmp_path) -> None:
    """带 variants 的 plan.json 存盘再读回来，变体一字不差（阶段 1 的产物形态）。"""
    deck = make_deck()
    plan = plan_rule(deck)
    assert attach_variants(plan, deck) > 0
    path = plan.save(tmp_path / "plan.json")
    back = LessonPlan.load(path)
    assert [u.variants for u in back.units] == [u.variants for u in plan.units]
    assert any(u.variants for u in back.units)


def test_build_lesson_script_passes_variants_through() -> None:
    """带 variants 的计划经 build_lesson_script 后内容一致（透传无损，AC2）。"""
    deck = make_deck()
    plan = plan_rule(deck)
    assert attach_variants(plan, deck) > 0

    def expected() -> dict[str, list]:
        out: dict[str, list] = {}
        for s in deck.slides:
            for i, pu in enumerate(plan.units_of(s.index)):
                out[f"S{s.index + 1:02d}-U{i + 1:02d}"] = pu.variants
        return out

    want = expected()
    script = build_lesson_script(deck, plan)
    assert [u.unit_id for u in script.units] == list(want)
    for u in script.units:
        assert u.variants == want[u.unit_id]

    # 手工改过的变体同样要原样透传（不是只对生成器的产物生效）
    hand = {"variant_id": "c9", "label": "手工", "text": "手工改的讲法。",
            "anchors": [{"element_id": "s02-sh02-p03"}], "ops": [], "est_sec": 1.4}
    plan.units_of(1)[0].variants = [hand]
    rebuilt = {u.unit_id: u.variants for u in build_lesson_script(deck, plan).units}
    assert rebuilt["S02-U01"] == [hand]
