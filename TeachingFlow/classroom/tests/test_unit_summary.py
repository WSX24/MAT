"""unit.summary：备课期产出 / 落盘 / 回填 / 传到走向图。

三个环节各自会**静默**坏掉，这才是这个文件存在的理由：

  ① 产出：模型没给、给超 30 字、或者规则兜底备课 —— 卡面上都不许出现空白；
  ② 落盘：空的 summary 一旦写进 plan.json，"还没补过"与"补过了但是空的"就分不开了，
     回填从此不干活，而文件看上去完全正常；
  ③ 传导：PlanUnit.summary 只加在计划层是**传不到 /graph** 的 ——
     build_lesson_script 逐字段构造 LectureUnit，漏传就等于这个字段不存在（variants 踩过）。

全部离线：假 llm、不读写 decks/ 下的真实数据、不用那份 16MB 真实课件。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deck.store as deck_store  # noqa: E402
import llm as llm_mod  # noqa: E402
import run as run_mod  # noqa: E402
import script_gen as sg  # noqa: E402
from script_gen import (  # noqa: E402
    LessonPlan,
    PlanUnit,
    backfill_summaries,
    build_lesson_script,
    parse_units_json,
    plan_rule,
)
from state import SUMMARY_MAX_CHARS, clip_summary  # noqa: E402
from tests.fixtures import make_deck  # noqa: E402

#: 手搭的 3 页合成课件（封面 / 单词讲解页 / 目录页），逐字段可控。
DECK = make_deck()


class FakeLLM:
    """只记账、按剧本回话的假模型 —— 这条链路上唯一要与外部打交道的地方。"""

    def __init__(self, replies=None, *, raises: Exception | None = None):
        self.replies = list(replies or [])
        self.raises = raises
        self.calls: list[dict] = []

    async def chat(self, messages, *, json_mode=False, max_tokens=0, **kw):
        self.calls.append({"messages": messages, "json_mode": json_mode,
                           "max_tokens": max_tokens})
        if self.raises:
            raise self.raises
        return self.replies.pop(0) if self.replies else "{}"


def _raw(units: list[dict]) -> str:
    return json.dumps({"units": units}, ensure_ascii=False)


def _plan_two_pages() -> LessonPlan:
    """第 0 页两格（一格已有总结）/ 第 1 页一格（没有）：三种情况一次摆齐。"""
    return LessonPlan(units=[
        PlanUnit(slide=0, text="第一页第一格：先讲封面标题。", summary="封面标题"),
        PlanUnit(slide=0, text="第一页第二格：再讲副标题。"),
        PlanUnit(slide=1, text="第二页：讲 pack 的两个搭配。"),
    ])


def _snapshot(plan: LessonPlan) -> list[tuple]:
    """回填**不许动**的那些字段（正文/锚点/操作/页边界/知识点）。"""
    return [(u.text, list(u.anchors), list(u.ops), u.is_page_end, u.must_teach, list(u.kp))
            for u in plan.units]


# ===========================================================================
# ① 规则截断：卡面上那句话的形状
# ===========================================================================

def test_clip_summary_shape():
    """首句优先、换行压平、硬上限 30 字、空输入给空串。

    每一条都是卡面上的**可见**后果：带换行会撑破固定高度的 .gsum，超过 30 字会被 CSS
    的 line-clamp 吃掉后半句（而省略号是我们自己补的，等于告诉老师"还有下文"）。
    """
    assert clip_summary("  第一句。第二句。  ") == "第一句。第二句。", "首尾空白要去掉"
    assert clip_summary("这一格讲了三件事\n第二行东西") == "这一格讲了三件事 第二行东西", \
        "换行压成单空格：总结是一行字，不是一段话"
    long = "这个单元开头就是一句特别特别长的话没有任何停顿一直说到超过三十个字的长度才停。后面还有"
    got = clip_summary(long)
    assert len(got) == SUMMARY_MAX_CHARS and got.endswith("…"), \
        "超长就截到字面上限 30 个字（省略号也在这 30 里）"
    assert clip_summary("") == "" and clip_summary(None) == ""


def test_clip_summary_prefers_first_sentence():
    """首句够短就用首句 —— 讲解单元的第一句本来就在说"这一格讲什么"。"""
    text = "好，我们来看 pack 的两个搭配。第一个是 pack sth.，第二个是 pack up。"
    assert clip_summary(text) == "好，我们来看 pack 的两个搭配。"


# ===========================================================================
# ② 产出：同一次 per-page 调用里要回来，规则备课也要有
# ===========================================================================

def test_prompt_contract_asks_for_summary_in_the_same_output():
    """契约：summary 写在**同一份** JSON 输出里（不新增请求），并同步到 schema。"""
    assert '"summary":"..."' in sg.PROMPT_HEAD, "输出示例里必须出现 summary"
    assert "30 字" in sg.PROMPT_HEAD, "上限要写在 prompt 里，模型才知道该多短"

    from agent.profiles.planning import UNIT_SCHEMA

    props = UNIT_SCHEMA["properties"]
    assert props["summary"]["maxLength"] == SUMMARY_MAX_CHARS, \
        "工具侧的 schema 与 prompt 是同一份契约的两个出口，不能各说各的上限"


def test_plan_llm_gets_summary_without_extra_request():
    """一页一次调用，summary 与单元一起回来 —— 调用次数必须还是页数。"""
    llm = FakeLLM([_raw([{"text": "这一格讲 pack 的搭配。", "summary": "pack 的搭配"}])] * 3)
    plan = asyncio.run(sg.plan_llm(DECK, llm))
    assert len(llm.calls) == DECK.slide_count, "summary 不许额外多发一个请求"
    assert all(u.summary for u in plan.units)
    assert any(u.summary == "pack 的搭配" for u in plan.units), "模型给了就照用"


def test_parse_units_takes_model_summary_and_falls_back():
    """模型给了照用；给空白/超长就收敛成同一种东西（正文首句 / 截断）。"""
    slide = DECK.slides[1]
    units, _ = parse_units_json(_raw([
        {"text": "好，我们来看 pack 的两个搭配。", "summary": "pack 的两个搭配"},
        {"text": "第二格：先看音标，再看例句。", "summary": "   "},
        {"text": "第三格：讲 I need to pack my clothes 这句。",
         "summary": "这一格故意写得非常非常长完全超过三十个字的硬上限用来验证截断是否正确"},
    ]), slide, DECK)
    assert len(units) == 3
    assert units[0].summary == "pack 的两个搭配", "给了就逐字用（≤30 字不改写）"
    assert units[1].summary == clip_summary(units[1].text), "空白 summary 回落正文首句"
    assert len(units[2].summary) == SUMMARY_MAX_CHARS and units[2].summary.endswith("…")


def test_plan_rule_gives_every_unit_a_summary():
    """规则备课（plan_rule）没有模型，但卡面上照样要有一行字。"""
    plan = plan_rule(DECK)
    assert plan.units
    for u in plan.units:
        assert u.summary and len(u.summary) <= SUMMARY_MAX_CHARS, \
            f"第 {u.slide + 1} 页的规则单元没有总结"


def test_split_long_units_regenerates_summary_per_segment():
    """超长单元切开后，每段按**自己的**正文重算总结（不调模型）。

    照抄原单元的 summary 是最容易犯的错：那句话说的是整段长话，切完就对不上了 ——
    而卡面上看不出任何异常，只是"这一格讲了三件事"其实只讲了一件。
    """
    text = "这是一句用来把单元撑到硬上限以外的长话，讲的是同一个知识点。" * 12
    unit = PlanUnit(slide=0, text=text,
                    anchors=[{"element_id": "s01-sh01", "text_hint": "标题", "confidence": 1.0}],
                    kp=["K1"], summary="原始的一句话总结")
    segs = sg._split_long_units([unit])
    assert len(segs) > 1, "夹具前提：这段话确实超了硬上限"
    assert "".join(s.text for s in segs) == text, "切分只分段，不加字也不删字"
    for seg in segs:
        assert seg.summary == clip_summary(seg.text), "每段按自己的正文重算"
        assert seg.summary != "原始的一句话总结"
        assert seg.anchors == unit.anchors and seg.kp == ["K1"] and seg.must_teach


# ===========================================================================
# ③ 传导：计划层 → 运行期单元
# ===========================================================================

def test_build_lesson_script_carries_summary_to_runtime_units():
    """漏传 = 字段不存在：PlanUnit 加了 summary，LectureUnit 那边没人替它补。"""
    plan = LessonPlan(units=[
        PlanUnit(slide=0, text="第一格：讲封面标题。", summary="封面标题"),
        PlanUnit(slide=0, text="第二格：旧计划没有总结。"),
    ])
    units = build_lesson_script(DECK, plan).units
    assert units[0].summary == "封面标题", "计划里有什么，运行期就得是什么（逐字）"
    assert units[1].summary == clip_summary("第二格：旧计划没有总结。"), \
        "计划里没有就由运行期兜底（不是前端补）"


def test_runtime_unit_summary_is_verbatim_and_never_empty():
    """运行期单元的 summary：有就逐字透传，没有也绝不返空串。

    "永远非空"是前端不判空这个决定的前提：卡面上只有一行字，空串等于那一格看上去没内容，
    而这一页看起来一切正常。（旧 UI 的 /graph 载荷已随 web.py 删除，判据落在它的数据源——
    运行期单元本身，载荷只是把这两个字段读出来。）
    """
    plan = LessonPlan(units=[
        PlanUnit(slide=0, text="旧计划：这一格没有总结。"),
        PlanUnit(slide=0, text="这一格有：讲 pack 的两个搭配。", summary="pack 的两个搭配"),
    ])
    units = build_lesson_script(DECK, plan).units
    assert units[0].summary == clip_summary("旧计划：这一格没有总结。"), "回落正文截断"
    assert units[0].summary, "旧计划也必须有总结"
    assert units[1].summary == "pack 的两个搭配", "有 summary 就逐字等于 plan.json 里的值"


def test_runtime_unit_fills_blank_summary_on_construction():
    """LectureUnit 自己兜底：任何构造点（备课/回填/夹具）都不产出空 summary。"""
    from state import Anchor, LectureUnit

    u = LectureUnit(unit_id="S01-U01", slide=0, index_in_slide=0,
                    text="这一格讲 pack 的搭配。", anchors=[Anchor("s01-sh01")])
    assert u.summary == clip_summary(u.text) and u.summary


# ===========================================================================
# ④ 落盘：空的 summary 不写，旧计划照常读
# ===========================================================================

def test_save_plan_omits_empty_summary(tmp_path):
    """空 summary 不写盘：写进去就等于宣称"补过了，是空的"。"""
    plan = LessonPlan(units=[
        PlanUnit(slide=0, text="有总结的单元。", summary="有总结"),
        PlanUnit(slide=0, text="没总结的单元。"),
    ])
    path = tmp_path / "plan.json"
    run_mod._save_plan(path, plan, None)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["units"][0]["summary"] == "有总结"
    assert "summary" not in doc["units"][1], "空 summary 不许写盘（回填的判据就在这）"

    # 向后兼容：旧 plan.json 没有这个键照样 load，缺键 = 空 = 待补
    doc["units"][0].pop("summary")
    old = LessonPlan.from_dict(doc)
    assert [u.summary for u in old.units] == ["", ""]


# ===========================================================================
# ⑤ 回填：只补空的，正文一格都不动，模型挂了也不许失败
# ===========================================================================

def test_backfill_only_fills_empty_and_only_calls_pages_with_work():
    plan = _plan_two_pages()
    llm = FakeLLM(['{"summaries":[{"i":1,"summary":"封面副标题"}]}',
                   '{"summaries":[{"i":1,"summary":"pack 的两个搭配"}]}'])
    res = asyncio.run(backfill_summaries(DECK, plan, llm))
    assert [c["json_mode"] for c in llm.calls] == [True, True], "与备课同一条 JSON 契约"
    assert len(llm.calls) == 2, "只对真有活要干的页调模型（第 0 页那格已有总结）"
    assert plan.units[0].summary == "封面标题", "已有的总结原样保留，不许被规则截断顶掉"
    assert plan.units[1].summary == "封面副标题"
    assert plan.units[2].summary == "pack 的两个搭配"
    assert res == {"updated": 2, "skipped": 1, "warnings": []}


def test_backfill_never_touches_text_anchors_ops_or_page_end():
    """回填只写 summary —— 对一份已经定稿、上过课的计划必须绝对安全。"""
    plan = _plan_two_pages()
    before = _snapshot(plan)
    asyncio.run(backfill_summaries(DECK, plan, FakeLLM([
        '{"summaries":[{"i":1,"summary":"a"}]}',
        '{"summaries":[{"i":1,"summary":"b"}]}',
    ])))
    assert _snapshot(plan) == before, "正文/锚点/操作/页边界/知识点一个字都不许动"
    assert [u.summary for u in plan.units] == ["封面标题", "a", "b"]


def test_backfill_is_idempotent():
    """全都有总结之后不再调模型、也不再写盘（第二次跑同一条命令应该零成本）。"""
    plan = _plan_two_pages()
    llm = FakeLLM(['{"summaries":[{"i":1,"summary":"a"}]}',
                   '{"summaries":[{"i":1,"summary":"b"}]}'])
    asyncio.run(backfill_summaries(DECK, plan, llm))
    calls = len(llm.calls)
    again = asyncio.run(backfill_summaries(DECK, plan, llm))
    assert len(llm.calls) == calls, "没有待补的单元就不该再花一次额度"
    assert again == {"updated": 0, "skipped": 3, "warnings": []}


def test_backfill_survives_model_failure_and_bad_json():
    """模型挂了 / 吐了一堆非 JSON：这一页回落正文截断，回填本身不许失败。"""
    plan = _plan_two_pages()
    res = asyncio.run(backfill_summaries(DECK, plan, FakeLLM(raises=RuntimeError("ollama 挂了"))))
    assert res["updated"] == 2 and plan.units[1].summary
    assert plan.units[1].summary == clip_summary(plan.units[1].text)
    assert any("回落正文截断" in w for w in res["warnings"])

    plan2 = _plan_two_pages()
    res2 = asyncio.run(backfill_summaries(DECK, plan2, FakeLLM(["这不是 JSON"] * 3)))
    assert plan2.units[1].summary == clip_summary(plan2.units[1].text)
    assert any("没给出可用的总结" in w for w in res2["warnings"])


def test_backfill_slides_filter_leaves_other_pages_alone():
    plan = _plan_two_pages()
    llm = FakeLLM(['{"summaries":[{"i":1,"summary":"只补第 1 页"}]}'])
    res = asyncio.run(backfill_summaries(DECK, plan, llm, slides=[0]))
    assert len(llm.calls) == 1, "--slides 之外的页一次都不许调"
    assert plan.units[1].summary == "只补第 1 页"
    assert plan.units[2].summary == "", "--slides 之外的单元一个字都不许动"
    assert res == {"updated": 1, "skipped": 1, "warnings": []}


def test_force_is_just_clearing_summaries_first():
    """--force 的实现口径：清空后再补 —— "补哪些"只有一个判据，不是两套规则。"""
    plan = _plan_two_pages()
    for u in plan.units:
        u.summary = ""
    llm = FakeLLM(['{"summaries":[{"i":1,"summary":"重写一"},{"i":2,"summary":"重写二"}]}',
                   '{"summaries":[{"i":1,"summary":"重写三"}]}'])
    res = asyncio.run(backfill_summaries(DECK, plan, llm))
    assert [u.summary for u in plan.units] == ["重写一", "重写二", "重写三"]
    assert res == {"updated": 3, "skipped": 0, "warnings": []}


# ===========================================================================
# ⑥ CLI：summarize 子命令（dry-run 不调模型、不写盘）
# ===========================================================================

def _boom(*a, **kw):
    raise AssertionError("--dry-run 不许调模型")


def test_summarize_dry_run_counts_without_model_or_write(tmp_path, monkeypatch, capsys):
    plan = plan_rule(DECK)
    for u in plan.units[::2]:
        u.summary = ""
    pj = tmp_path / "plan.json"
    plan.save(pj)
    before = pj.read_bytes()
    monkeypatch.setattr(deck_store, "load_deck", lambda deck_id: DECK)
    monkeypatch.setattr(deck_store, "plan_json", lambda deck_id: pj)
    monkeypatch.setattr(run_mod.asyncio, "run", _boom)

    assert run_mod.cmd_summarize([DECK.deck_id, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "待补" in out and "没有调模型、没有写盘" in out
    assert pj.read_bytes() == before, "--dry-run 不许写盘"


def test_summarize_writes_back_and_keeps_routes(tmp_path, monkeypatch):
    """真跑一次：总结落盘、空的不写键、顶层 routes 不许被 plan.save() 抹掉。"""
    plan = plan_rule(DECK)
    for u in plan.units:
        u.summary = ""
    pj = tmp_path / "plan.json"
    doc = json.loads(plan.to_json())
    doc["routes"] = [{"route_id": "r1"}]
    pj.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(deck_store, "load_deck", lambda deck_id: DECK)
    monkeypatch.setattr(deck_store, "plan_json", lambda deck_id: pj)
    monkeypatch.setattr(llm_mod, "LLM", lambda s: FakeLLM())

    assert run_mod.cmd_summarize([DECK.deck_id]) == 0
    after = json.loads(pj.read_text(encoding="utf-8"))
    assert after["routes"] == [{"route_id": "r1"}], "routes 键必须原样带回去"
    assert all(u.get("summary") for u in after["units"]), "每个单元都要有总结（模型空了就截断）"
