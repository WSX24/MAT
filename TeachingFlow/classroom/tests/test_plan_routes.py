"""备课期**预置路线分支**的测试：难度分档 / 结构自洽 / 幂等 / LLM 润色与降级 / 通道。

为什么单开一个文件
------------------
这一棒新加的是一条**流水线**而不是一个函数，它有三段会各自静默坏掉：

  ① **铺哪儿**（难度分档）：规则太松会铺满整份课件（决策层被淹），太紧则一条都没有
     （课上永远不换路线，而账上只表现为"这节课没走岔路"）。所以每个判据都要有
     "该铺"与"不该铺"两种构造用例，而不只是"跑通不报错"。
  ② **结构**：fork / merge / steps 越界或指向不存在的格时，状态机会走出课程 ——
     这种错不报错，只表现为"跳过的是别的内容"。所以每条都要真的过 Route.validate。
  ③ **通道**：状态机拿到的只有 LessonScript（web.py 起 Machine 时只传它），
     plan.json 读不到就等于没落地。旧计划（没有 routes 键）必须照常工作。

全部离线：零网络、假 llm、不读写 decks/ 下的真实数据（真实课件那条用例只读）。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import course as course_mod  # noqa: E402
from agent import routes as routes_mod  # noqa: E402
from agent.producer import ROUTE_WHEN  # noqa: E402
from agent.routes import FINISH_WHY_NOT, plan_routes, route_entry, when_for  # noqa: E402
from course import load_course, parse_plan_routes  # noqa: E402
from director.route import ROUTE_KINDS, Route, derive_route_id  # noqa: E402
from models import LessonScript  # noqa: E402
from script import build_script  # noqa: E402
from script_gen import LessonPlan, PlanUnit, build_lesson_script  # noqa: E402
from state import estimate_seconds  # noqa: E402
from tests.fixtures import make_deck  # noqa: E402
from tests.test_machine import make_settings  # noqa: E402

# ===========================================================================
# 夹具：合成"计划 + 课件"
# ===========================================================================

def _unit(slide: int, text: str, kp: list[str] | None = None) -> PlanUnit:
    return PlanUnit(slide=slide, text=text, anchors=[], ops=[], kp=list(kp or []))


def _deck(*titles: str):
    """只要页序与页标题：plan_routes 对课件的要求就这两样（其余都从计划里读）。"""
    return SimpleNamespace(
        title="合成课件",
        slides=[SimpleNamespace(index=i, title=t) for i, t in enumerate(titles)],
    )


def _plan(*units: PlanUnit) -> LessonPlan:
    return LessonPlan(deck_id="syn", source="rule", units=list(units))


def _text(chars: int, head: str = "") -> str:
    """造一段正好 chars 字的正文（5 字/秒 → 时长可算准）。"""
    body = (head + "判别式决定方程的实数根个数，我们先算再判。" * 40)[:chars]
    return body if len(body) >= chars else body + "好" * (chars - len(body))


def _ids(routes) -> list[str]:
    return [r.route_id for r in routes]


class _FakePolisher:
    """同步可调用替身：记下 prompt，回一段固定原文（零网络）。"""

    def __init__(self, payload: str):
        self.payload = payload
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.payload


# ===========================================================================
# ① 难度分档：revisit（这一格讲得重）
# ===========================================================================

def test_revisit_only_on_the_heaviest_unit_of_a_page():
    """一页里"明显高于中位数"的那一格才铺补回头；均匀的页、只有 1~2 格的页都不铺。"""
    deck = _deck("讲解页一", "讲解页二", "讲解页三")
    plan = _plan(
        # 第 1 页：10 / 40 / 10 秒 —— 中间那格是本页最重的
        _unit(0, _text(50), ["K1"]),
        _unit(0, _text(200), ["K2"]),
        _unit(0, _text(50), ["K3"]),
        # 第 2 页：三格一样长、kp 各不相同、没有例句线索 —— 不该铺
        _unit(1, _text(100), ["K4"]),
        _unit(1, _text(100), ["K5"]),
        _unit(1, _text(100), ["K6"]),
        # 第 3 页：只有 2 格（就算一格很长，也没有"本页最重"可言）
        _unit(2, _text(50), ["K7"]),
        _unit(2, _text(200), ["K8"]),
    )
    routes = plan_routes(plan, deck)
    revisit = [r for r in routes if r.kind == "revisit"]
    assert len(revisit) == 1, f"只有第 1 页该铺补回头：{_ids(routes)}"
    route = revisit[0]
    assert (route.fork_node, route.merge_node) == (1, 2), "岔出点必须是本页最重的那一格"
    assert route.est_sec > 0, "补回头是多花时间，est_sec 必须为正（符号约定）"
    assert [s.unit_id for s in route.steps] == ["S01-U02"], \
        "steps 要重讲的正是那一格（unit_id 必须与 build_lesson_script 的编号一致）"
    assert route.steps[0].text == "" and route.steps[0].variant_id == "", \
        "文案空 = 用该单元主 text（结构不该凭空编一段新讲法）"
    route.validate(len(plan.units))       # 每条都真的过一遍
    print(f"[分档·revisit] {route.route_id} fork={route.fork_node} est={route.est_sec}：{route.reason}")


def test_revisit_score_needs_length_plus_one_more_signal():
    """打分门槛：时长偏重是必要条件，kp 多 / 像例句各加一分 —— 两条凑够 2 才铺。

    为什么这条要单独钉住：真实课件里"带例句"的格子满地都是（177 格里 31 格），
    单凭它就能入选的话闸门等于没有 —— 那样一份课件会铺出几十条补回头。
    """
    deck = _deck("讲解页一", "讲解页二")
    plan = _plan(
        # 第 1 页：三格一样长，但最后一格同时"讲两个知识点 + 带例句" → 2 分，入选
        _unit(0, _text(100), ["K1"]),
        _unit(0, _text(100), ["K1"]),
        _unit(0, _text(100, "例如Δ等于零的时候，"), ["K1", "K2"]),
        # 第 2 页：只有"带例句"这一条（1 分）→ 不入选
        _unit(1, _text(100), ["K3"]),
        _unit(1, _text(100, "例如"), ["K4"]),
        _unit(1, _text(100), ["K5"]),
    )
    routes = plan_routes(plan, deck)
    revisit = [r for r in routes if r.kind == "revisit"]
    assert len(revisit) == 1, f"只有凑够 2 分的那一格该铺：{_ids(routes)}"
    assert revisit[0].fork_node == 2, "岔出点应该是同时踩到两条信号的那一格"


# ===========================================================================
# ② 难度分档：skip（连续同质 / 整页同质）
# ===========================================================================

def test_skip_on_a_homogeneous_run_inside_a_page():
    """连续 2 格以上讲同一件事 → 从段前一格岔出、段后一格汇回（整段跳过）。"""
    deck = _deck("讲解页")
    plan = _plan(
        _unit(0, _text(100), ["K1"]),
        _unit(0, _text(100), ["K2"]),
        _unit(0, _text(100), ["K2"]),      # 与上一格同 kp：连续同质段 (1,3)
        _unit(0, _text(100), ["K2"]),
    )
    routes = plan_routes(plan, deck)
    assert len(routes) == 1, f"这一段同质内容该铺一条跳讲：{_ids(routes)}"
    route = routes[0]
    assert route.kind == "skip"
    assert (route.fork_node, route.merge_node) == (0, 4), \
        "跳过的是 fork+1..merge-1（也就是那 3 格同质内容）"
    assert route.steps == [], "skip 没有要重讲的格，steps 必须为空"
    route.validate(len(plan.units))
    assert route.est_sec < 0, "跳讲是省时间，est_sec 必须为负（符号约定）"
    assert route.est_sec == pytest.approx(-(20.0 * 3), abs=0.2), \
        f"省下的应该正好是这 3 格的时长：{route.est_sec}"
    print(f"[分档·skip] {route.route_id} fork={route.fork_node} est={route.est_sec}：{route.reason}")


def test_skip_run_must_start_inside_the_page():
    """"连续同质"段必须从本页第 2 格起：否则 fork 落到上一页最后一格，决策点落在翻页缝上。

    放开这条的后果是实测过的：真实课件会从 10 条跳到 21 条 skip（42 页里一半都有），
    而"这一页开头可以整块不讲"在备课期没有任何证据 —— 学生还没见过这一页。
    """
    deck = _deck("讲解页")
    plan = _plan(
        _unit(0, _text(100), ["K1"]),
        _unit(0, _text(100), ["K1"]),      # 段 (0,1)：起点在页首，不铺
        _unit(0, _text(100), ["K2"]),
        _unit(0, _text(100), ["K3"]),
    )
    assert plan_routes(plan, deck) == [], "页首就开始的同质段不该铺跳讲"


def test_practice_page_is_skipped_whole():
    """整页同质（页标题像练习/答案）且够长 → 整页跳过；讲解页不铺。"""
    deck = _deck("讲解页", "二.词汇练习填空题", "三.讲解页")
    plan = _plan(
        _unit(0, _text(100), ["K1"]),
        # 练习页三格：kp 各不相同（kp 判据看不出来），只有页标题能说明"整页同一类内容"
        _unit(1, _text(100), ["K2"]),
        _unit(1, _text(100), ["K3"]),
        _unit(1, _text(100), ["K4"]),
        # 另一页三格：页标题不像练习，kp 也各不相同 → 不铺
        _unit(2, _text(100), ["K5"]),
        _unit(2, _text(100), ["K6"]),
        _unit(2, _text(100), ["K7"]),
    )
    routes = plan_routes(plan, deck)
    assert len(routes) == 1, f"只有练习页该整页跳过：{_ids(routes)}"
    route = routes[0]
    assert (route.kind, route.fork_node, route.merge_node) == ("skip", 0, 4), \
        "整页跳过 = 从该页前一格岔出、到该页后一格汇回：" \
        f"{(route.fork_node, route.merge_node)}"
    assert route.est_sec == pytest.approx(-60.0, abs=0.2)
    route.validate(len(plan.units))
    print(f"[分档·skip-整页] {route.route_id}：{route.reason}")


def test_finish_is_never_preset():
    """提前收尾不在备课期预置：它看的是"当下还剩多少时间"，备课期无从得知。"""
    deck = _deck("讲解页", "练习页", "讲解页二")
    plan = _plan(
        _unit(0, _text(100), ["K1"]),
        *[_unit(1, _text(100), ["K2"]) for _ in range(4)],        # 练习页 → 整页跳讲
        _unit(2, _text(50), ["K3"]),                              # 本页最重的一格 → 补回头
        _unit(2, _text(200), ["K4"]),
        _unit(2, _text(50), ["K5"]),
    )
    routes = plan_routes(plan, deck)
    assert routes, "这份构造至少该铺出一条，否则这条断言是空转"
    assert all(r.kind in ("skip", "revisit") for r in routes), \
        f"备课期只预置 skip / revisit，实测出现：{[r.kind for r in routes]}"
    assert "finish" not in {r.kind for r in routes}
    with pytest.raises(ValueError):
        when_for("finish")
    assert "备课期无从得知" in FINISH_WHY_NOT
    print(f"[分档·finish] 三条走向里只预置两种：{[r.kind for r in routes]}")


# ===========================================================================
# ③ 结构自洽 / 非法丢弃 / 幂等
# ===========================================================================

def _mixed_plan_and_deck():
    return _deck("讲解页一", "练习页", "讲解页二"), _plan(
        _unit(0, _text(120), ["K1"]),
        _unit(0, _text(240), ["K2", "K3"]),          # 本页最重 + kp 多 → revisit
        _unit(0, _text(120), ["K4"]),
        _unit(1, _text(150), ["P1"]),                # 练习页 → 整页 skip
        _unit(1, _text(150), ["P2"]),
        _unit(1, _text(150), ["P3"]),
        _unit(2, _text(150), ["K5"]),
        _unit(2, _text(150), ["K6"]),
        _unit(2, _text(150), ["K6"]),                # 连续同质 → 段内 skip
        _unit(2, _text(150), ["K6"]),
    )


def test_every_route_passes_validate_and_points_at_real_units():
    """结构自洽：0 <= fork < merge <= 总格数；revisit 的 steps 非空且指向存在的单元。"""
    deck, plan = _mixed_plan_and_deck()
    total = len(plan.units)
    routes = plan_routes(plan, deck)
    assert len(routes) >= 3, f"这份构造该铺出 revisit + 两条 skip：{_ids(routes)}"
    by_slide: dict[int, int] = {}
    for u in plan.units:
        by_slide[u.slide] = by_slide.get(u.slide, 0) + 1
    unit_ids = {routes_mod._unit_id(slide, i)
                for slide, count in by_slide.items() for i in range(count)}
    for route in routes:
        route.validate(total)                      # 每一条都真的跑一遍
        assert route.kind in ROUTE_KINDS
        assert 0 <= route.fork_node < route.merge_node <= total
        if route.kind == "revisit":
            assert route.steps, "revisit 必须给出要重讲的格子"
            for step in route.steps:
                assert step.unit_id in unit_ids, f"{step.unit_id} 指向不存在的格"
        else:
            assert route.steps == [], f"{route.kind} 的 steps 必须为空"
    print(f"[结构] {len(routes)} 条全部通过 Route.validate：{_ids(routes)}")


def test_illegal_draft_is_dropped_and_counted():
    """规则也会算出结构不合法的（第一页整页同质 → 岔出点成了 -1）：丢弃并计数，不抛。"""
    deck = _deck("答案解析")                       # 第 1 页就是练习页 → fork = -1
    plan = _plan(_unit(0, _text(150), ["K1"]),
                 _unit(0, _text(150), ["K2"]),
                 _unit(0, _text(150), ["K3"]))
    routes = plan_routes(plan, deck)
    assert routes == [], "第 0 格之前没法岔出，这条必须被丢掉"
    assert any("丢弃 1 条" in w for w in plan.warnings), \
        f"丢弃要计数并记账（plan.warnings）：{plan.warnings}"
    print(f"[结构·丢弃] {[w for w in plan.warnings if '[routes]' in w]}")


def test_route_ids_are_idempotent_and_prefixed():
    """幂等：反复算得到同一串 id，且一律是 p- + derive_route_id（与运行期的 r-… 分开）。"""
    deck, plan = _mixed_plan_and_deck()
    first = plan_routes(plan, deck)
    second = plan_routes(plan, deck)
    assert _ids(first) == _ids(second), "同一份计划两次算出的 id 必须完全一致"
    for route in first:
        assert route.route_id.startswith("p-"), "预置路线必须以 p- 开头"
        assert route.route_id == "p-" + derive_route_id(route.kind, route.fork_node,
                                                        route.merge_node), \
            "id 必须是 p- 加上冻结约定的 derive_route_id 结果"
        runtime_id = derive_route_id(route.kind, route.fork_node, route.merge_node)
        assert route.route_id != runtime_id, "与运行期 C 造的 id 撞了会互相覆盖"
    assert len(set(_ids(first))) == len(first), "同一次产出里不许有重复 id"


def test_when_matches_the_runtime_table_and_is_structured():
    """when 与 agent/producer.py 的 ROUTE_WHEN 是同一张表，且永远不为空。"""
    assert when_for("skip") == ROUTE_WHEN["skip"]
    assert when_for("revisit") == ROUTE_WHEN["revisit"]
    copied = when_for("revisit")
    copied["comprehension"]["in"].append("x")
    assert when_for("revisit") == ROUTE_WHEN["revisit"], "返回的必须是深拷贝，改它不许污染这张表"
    deck, plan = _mixed_plan_and_deck()
    for route in plan_routes(plan, deck):
        entry = route_entry(route)
        assert entry["when"], f"{route.route_id} 的 when 为空 → 它会在每个边界被选中"
        assert set(entry) == {"route_id", "kind", "fork_node", "merge_node",
                              "steps", "est_sec", "reason", "when"}, \
            "plan.json 里每条路线 = Route.to_dict() 逐字一致 + 一个 when"
    print("[when] 与 ROUTE_WHEN 同表，且非空")


# ===========================================================================
# ④ LLM 润色：成功 / 结构不许改 / 两条降级路径
# ===========================================================================

def test_llm_polish_fills_reason_and_step_text():
    """润色成功路径：reason 与 steps 的 label/text 用模型的，时长按新文案重算。"""
    deck, plan = _mixed_plan_and_deck()
    routes = plan_routes(plan, deck)
    target = next(r for r in routes if r.kind == "revisit")
    step_text = "我们把这一格再走一遍：先写 Δ 等于 b 平方减 4ac，再看它和零的大小关系。"
    payload = json.dumps({"routes": [{
        "route_id": target.route_id,
        "reason": "两个长格连着讲，学生容易在这里掉队",
        "steps": [{"unit_id": target.steps[0].unit_id, "label": "再讲一遍", "text": step_text}],
    }]}, ensure_ascii=False)
    fake = _FakePolisher(payload)
    polished = {r.route_id: r for r in plan_routes(plan, deck, llm=fake)}

    route = polished[target.route_id]
    assert route.reason == "两个长格连着讲，学生容易在这里掉队"
    assert route.steps[0].label == "再讲一遍"
    assert route.steps[0].text == step_text
    assert route.steps[0].est_sec == estimate_seconds(step_text), "时长要跟着新文案重算"
    assert route.est_sec == pytest.approx(route.steps[0].est_sec, abs=0.05)
    assert target.route_id in fake.prompts[0], "prompt 里必须带上 route_id 让模型原样抄回"
    assert not any("[routes]" in w for w in plan.warnings), "成功路径不该留降级告警"
    print(f"[润色·成功] {route.route_id} reason={route.reason!r} est={route.est_sec}")


def test_llm_cannot_change_structure():
    """模型给了不同的 fork/merge/kind、还想加一格 —— 一律无视（结构不归它管）。"""
    deck, plan = _mixed_plan_and_deck()
    before = {r.route_id: (r.kind, r.fork_node, r.merge_node, len(r.steps))
              for r in plan_routes(plan, deck)}
    payload = json.dumps({"routes": [{
        "route_id": rid,
        "kind": "finish", "fork_node": 99, "merge_node": 100,
        "reason": "换个理由",
        "steps": [{"unit_id": "S99-U99", "label": "不存在", "text": "不存在的格"}],
    } for rid in before]}, ensure_ascii=False)
    after = plan_routes(plan, deck, llm=_FakePolisher(payload))
    for route in after:
        assert (route.kind, route.fork_node, route.merge_node, len(route.steps)) == before[route.route_id], \
            f"{route.route_id} 的结构被模型改掉了：{(route.kind, route.fork_node, route.merge_node)}"
        assert route.reason == "换个理由", "文案该用模型的（它确实只改了文案）"
    print("[润色·结构] 模型的 fork/merge/kind/steps 全部被无视")


def test_llm_failure_falls_back_to_rule_reason_with_one_warning():
    """模型调用失败 → 规则文案 + 一条 warning，绝不抛（备课比文案重要）。"""
    deck, plan = _mixed_plan_and_deck()
    rule = plan_routes(plan, deck)

    def boom(prompt: str) -> str:
        raise RuntimeError("模型 502")

    got = plan_routes(plan, deck, llm=boom)
    assert _ids(got) == _ids(rule), "降级不该改变铺出来的条数与顺序"
    assert [r.reason for r in got] == [r.reason for r in rule], "降级后必须是规则文案"
    warns = [w for w in plan.warnings if "[routes]" in w]
    assert len(warns) == 1 and "LLM 润色不可用" in warns[0], f"要恰好一条降级告警：{warns}"
    print(f"[润色·降级-异常] {warns[0]}")


def test_llm_garbage_falls_back_to_rule_reason_with_one_warning():
    """模型返回垃圾 → 同样降级；另外超长文案也不许漏进来（§5.4 的 250 字上限）。"""
    deck, plan = _mixed_plan_and_deck()
    rule = plan_routes(plan, deck)

    got = plan_routes(plan, deck, llm=_FakePolisher("抱歉，这个我做不到。"))
    assert [r.reason for r in got] == [r.reason for r in rule]
    warns = [w for w in plan.warnings if "[routes]" in w]
    assert len(warns) == 1 and "不是合法 JSON" in warns[0], f"{warns}"

    # 超长文案：模型把一条 reason 写成 200 字、把 step 文本写成 900 字 → 逐条丢弃
    target = next(r for r in rule if r.kind == "revisit")
    plan2 = _plan(*[dataclasses.replace(u) for u in plan.units])
    payload = json.dumps({"routes": [{
        "route_id": target.route_id,
        "reason": "长" * 200,
        "steps": [{"unit_id": target.steps[0].unit_id, "label": "长" * 40, "text": "长" * 900}],
    }]}, ensure_ascii=False)
    got2 = plan_routes(plan2, deck, llm=_FakePolisher(payload))
    kept = next(r for r in got2 if r.route_id == target.route_id)
    assert kept.reason == target.reason, "超长 reason 该退回规则文案"
    assert kept.steps[0].text == "" and kept.steps[0].label == "", "超长文案一格都不许漏进来"
    print(f"[润色·降级-垃圾/超长] {warns[0]}")


# ===========================================================================
# ⑤ 通道：plan.json → LessonScript.routes
# ===========================================================================

def _write_plan(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _load_course(tmp_path: Path, monkeypatch, doc: dict):
    """离线装配一节课：课件用合成夹具，plan.json 用临时文件（不碰 decks/）。"""
    deck = make_deck()
    pj = _write_plan(tmp_path, doc)
    monkeypatch.setattr(course_mod, "load_deck", lambda deck_id: deck)
    monkeypatch.setattr(course_mod, "plan_json", lambda deck_id: pj)
    settings = dataclasses.replace(make_settings(), deck_id="syn-deck", pptx_file="")
    return load_course(settings), deck


def _plan_doc(deck, *, units: int = 3, routes=None) -> dict:
    from script_gen import plan_rule

    plan = plan_rule(deck)
    doc = json.loads(plan.to_json())
    if routes is not None:
        doc["routes"] = routes
    return doc


def test_lesson_script_defaults_to_empty_routes():
    """没有预置时 script.routes 是空列表（不是 None），而且每份脚本各拿一份。"""
    script = build_script()
    assert script.routes == []
    other = build_script()
    assert other.routes is not script.routes, "空列表不能是同一个对象（谁 append 一下就会串味）"
    assert LessonScript(course_id="x", title="y").routes == []


def test_plan_json_routes_land_on_the_script_in_order(tmp_path, monkeypatch):
    """从磁盘 plan.json 加载 → LessonScript.routes 有值、类型是 Route、顺序与文件一致。"""
    deck = make_deck()
    doc = _plan_doc(deck)
    total = sum(1 for _ in doc["units"])
    assert total >= 3, "夹具前提：规则兜底至少切出 3 格"
    entries = [
        {"route_id": "p-r-revisit-0", "kind": "revisit", "fork_node": 0, "merge_node": 1,
         "steps": [{"unit_id": "S01-U01", "variant_id": "", "label": "", "text": "",
                    "est_sec": 12.0}],
         "est_sec": 12.0, "reason": "这一格讲得重", "when": {"comprehension": {"in": ["vague"]}}},
        {"route_id": "p-r-skip-1-2", "kind": "skip", "fork_node": 1, "merge_node": 3,
         "steps": [], "est_sec": -40.0, "reason": "这两格是同一件事",
         "when": {"comprehension": {"==": "confirmed"}}},
    ]
    doc["routes"] = entries
    course, _deck = _load_course(tmp_path, monkeypatch, doc)

    assert [type(r) for r in course.script.routes] == [Route, Route]
    assert [r.to_dict() for r in course.script.routes] == [
        Route.from_dict(e).to_dict() for e in entries], "顺序与字段必须与 plan.json 一致"
    for route in course.script.routes:
        route.validate(len(course.script.units))
    assert any("预置路线 2 条" in n for n in course.notes), course.notes
    print(f"[通道] script.routes = {_ids(course.script.routes)}")


def test_plan_with_routes_key_does_not_backfill_variants(tmp_path, monkeypatch):
    """有 routes 键的新计划：就地变体已停产 → 一格都不许在加载时长出来。"""
    deck = make_deck()
    doc = _plan_doc(deck)
    assert all(not u.get("variants") for u in doc["units"]), "夹具前提：这份计划本来就没有变体"
    doc["routes"] = []
    course, _deck = _load_course(tmp_path, monkeypatch, doc)
    assert course.script.routes == [], "routes 是空数组 → 空列表（不是 None）"
    assert all(not u.variants for u in course.script.units), \
        "带 routes 键的计划不该再补挂就地变体（停产要在运行期也是真的）"
    assert all(not u.variants for u in course.plan.units)
    assert any("不再补挂" in n for n in course.notes), course.notes
    print("[通道·停产] 有 routes 键 → variants 全空")


def test_plan_without_routes_key_keeps_working(tmp_path, monkeypatch):
    """旧 plan.json（没有 routes 键）照常工作：routes 为空，课堂行为与现在一致。"""
    deck = make_deck()
    doc = _plan_doc(deck)
    assert "routes" not in doc
    course, _deck = _load_course(tmp_path, monkeypatch, doc)
    assert course.mode == "deck"
    assert course.script.routes == [], "旧计划没有预置路线，但也必须能起课上"
    assert course.script.units, "旧计划的单元照旧"
    assert any(u.variants for u in course.script.units), \
        "旧计划沿用内存补挂变体（这条兼容路径是有意保留的）"
    assert any("变体已在内存补齐" in n for n in course.notes), course.notes
    assert any("没有 routes 键" in n for n in course.notes), course.notes
    print(f"[通道·向后兼容] 旧计划起课正常，单元 {len(course.script.units)} 个，routes=[]")


def test_dirty_routes_are_dropped_not_fatal(tmp_path, monkeypatch):
    """脏数据 / 非法结构一律丢弃并计数：一条坏路线不许让上课启动失败。"""
    deck = make_deck()
    doc = _plan_doc(deck)
    good = {"route_id": "p-r-skip-1-2", "kind": "skip", "fork_node": 1, "merge_node": 3,
            "steps": [], "est_sec": -30.0, "reason": "可用的那一条"}
    doc["routes"] = [
        good,
        "不是对象",
        {**good, "route_id": "p-r-revisit-0", "kind": "jump"},          # kind 非法
        {**good, "route_id": "p-r-revisit-1", "fork_node": 2, "merge_node": 1},   # fork >= merge
        {**good, "route_id": "p-r-revisit-2", "kind": "revisit", "steps": []},    # revisit 没 steps
        {**good, "route_id": "r-skip-1-2"},                              # 没 p- 前缀（运行期命名）
        dict(good),                                                      # 与第一条重复
    ]
    course, _deck = _load_course(tmp_path, monkeypatch, doc)
    assert _ids(course.script.routes) == ["p-r-skip-1-2"], \
        f"只有那一条合法：{_ids(course.script.routes)}"
    assert any("丢弃 6 条非法" in n for n in course.notes), course.notes
    print(f"[通道·脏数据] 通过 1 / 丢弃 6 → {course.script.routes[0].route_id}")


def test_parse_plan_routes_total_nodes_is_enforced():
    """merge 越过课尾同样算非法（总格数只有调用方知道，所以这条只能在写入口判）。"""
    routes, dropped = parse_plan_routes(
        [{"route_id": "p-r-skip-8-10", "kind": "skip", "fork_node": 8, "merge_node": 10,
          "steps": [], "est_sec": -10.0, "reason": "越界"}], 9)
    assert (routes, dropped) == ([], 1)


# ===========================================================================
# ⑥ 真实课件：稀疏程度与真实性（只读 decks/，不写一个字节）
# ===========================================================================

def _real_pair():
    from deck.store import list_decks, load_deck, plan_json
    from script_gen import LessonPlan

    for row in list_decks():
        deck_id = str(row.get("deck_id") or "")
        if not deck_id or not row.get("has_plan"):
            continue
        try:
            deck = load_deck(deck_id)
            plan = LessonPlan.load(plan_json(deck_id))
        except Exception:
            continue
        if plan.units:
            return deck, plan
    pytest.skip("decks/ 下没有可用的真实课件，跳过实测")


def test_real_deck_routes_are_sparse_and_aligned_with_the_script():
    """真实课件（42 页 / 177 格）：铺出来的条数必须稀疏，且每一步都指向脚本里真实的格。"""
    deck, plan = _real_pair()
    routes = plan_routes(plan, deck)
    total = len(plan.units)
    script = build_lesson_script(deck, plan)
    assert len(script.units) == total, "格序列必须与 build_lesson_script 一一对应"

    assert 0 < len(routes) <= 60, f"闸门太松或太紧：{len(routes)} 条（{total * 2} 种可能）"
    density = len(routes) / (total * 2)
    assert density <= 0.10, f"稀疏度 {density:.1%} 已经不算稀疏了"
    kinds = {r.kind for r in routes}
    assert kinds == {"skip", "revisit"}, f"真实课件上两种走向都该铺出来：{kinds}"

    for route in routes:
        route.validate(total)
        assert script.units[route.fork_node] is not None
        if route.kind == "revisit":
            assert script.units[route.fork_node].unit_id == route.steps[0].unit_id, \
                "补回头的岔出格就是要重讲的那一格"
        for step in route.steps:
            assert step.unit_id in {u.unit_id for u in script.units}
    assert _ids(plan_routes(plan, deck)) == _ids(routes), "真实课件上同样要幂等"
    print(f"[真实课件] {deck.slide_count} 页 / {total} 格 → 预置 {len(routes)} 条"
          f"（{density:.2%} of {total * 2}）")
    for route in routes[:3]:
        print("  " + json.dumps(route_entry(route), ensure_ascii=False))


# ===========================================================================
# ⑦ 写盘：plan.json 的 routes / variants 与 variants 子命令的停产声明
# ===========================================================================

def test_save_plan_writes_routes_and_drops_empty_variants(tmp_path):
    """写盘形状：顶层有 routes，单元里不再写空的 variants（停产也要在文件里看得出来）。"""
    import run as run_mod
    from script_gen import plan_rule

    deck = make_deck()
    plan = plan_rule(deck)
    entries = [route_entry(r) for r in plan_routes(plan, deck)]
    path = tmp_path / "plan.json"
    run_mod._save_plan(path, plan, entries)

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["routes"] == entries, "routes 必须与内存里那批逐字一致（含 when）"
    assert all("variants" not in u for u in doc["units"]), "空 variants 不该再写进文件"
    assert all(entry["when"] for entry in doc["routes"]), "每条路线的 when 都不许为空"

    run_mod._save_plan(path, plan, None)          # 旧路径：不谈 routes 就保持原样
    doc2 = json.loads(path.read_text(encoding="utf-8"))
    assert "routes" not in doc2, "没有 routes 这回事时不许凭空造一个空数组（会让旧计划被升级）"


def test_fix_ops_does_not_wipe_existing_routes(tmp_path, monkeypatch):
    """--fix-ops 只清"空操作"，不许顺手把已备好的 routes 抹掉（plan.save() 的老陷阱）。"""
    import deck.store as deck_store
    import run as run_mod
    from script_gen import plan_rule

    deck = make_deck()
    plan = plan_rule(deck)
    doc = json.loads(plan.to_json())
    entries = [route_entry(r) for r in plan_routes(plan, deck)]
    doc["routes"] = entries
    pj = _write_plan(tmp_path, doc)
    monkeypatch.setattr(deck_store, "load_deck", lambda deck_id: deck)
    monkeypatch.setattr(deck_store, "plan_json", lambda deck_id: pj)

    assert run_mod.cmd_plan([deck.deck_id, "--fix-ops"]) == 0
    after = json.loads(pj.read_text(encoding="utf-8"))
    assert after.get("routes") == entries, "清理空操作不该动 routes（格号与正文都没变）"


def test_variants_command_still_runs_but_says_it_is_discontinued(tmp_path, monkeypatch, capsys):
    """variants 子命令：保留、行为不变，但先打一句"已停产"。"""
    import deck.store as deck_store
    import run as run_mod
    from script_gen import plan_rule

    deck = make_deck()
    pj = tmp_path / "plan.json"
    plan_rule(deck).save(pj)
    monkeypatch.setattr(deck_store, "load_deck", lambda deck_id: deck)
    monkeypatch.setattr(deck_store, "plan_json", lambda deck_id: pj)

    rc = run_mod.cmd_variants([deck.deck_id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已停产：就地变体与实时重讲冲突，改由 routes 承载分支" in out
    assert "已写回" in out, "行为不变：照旧把变体写回 plan.json"
    doc = json.loads(pj.read_text(encoding="utf-8"))
    assert any(u.get("variants") for u in doc["units"]), "老流程仍然要能跑"
    print("[variants] 停产声明 + 老行为都在")
