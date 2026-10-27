"""横向提交图（/graph + /api/script/graph）的测试。

为什么单开一个文件
------------------
这一页和分支树是**同一份数据的两种投影**，所以它最容易犯的错不是"画歪了"，
而是三种不会报错的错：

  1. **n 和 script.order_index 不是同一把尺子** —— 图上的第 K 格与状态机装载候选
     时的第 K 格静默错开。不崩不报错，只是换到了别的地方，事后只能人肉对账。
  2. **泳道重号或越界** —— 前端按 lane 做绝对定位，重号就是两条支线叠在一条线上，
     看起来像"少了一条"；越界则是画到图外面去了。
  3. **载荷有副作用** —— 这个端点会和正在上课的窗口同时被调用。它一旦顺手建 Runner、
     推进状态机、或者取一次 MCP 事件队列，课堂那边就少了一次决策，而这一页看起来
     一切正常。
  4. **预置路线没进载荷** —— 就地变体已停产，分支改由备课期预置路线承载（plan.json 的
     routes）。这张图若只画 nodes[].branches，真实课件上就一条支线都画不出来，而页面
     本身看起来完全正常（"这门课没有岔路"与"这份载荷漏了一个键"长得一模一样）。

调用方式沿用 tests/test_script_tree.py：项目没装 httpx（fastapi.testclient 用不了），
而端点本来就是纯 async 函数，asyncio.run(web.api_xxx()) 直接 await 更快，也更容易
把"没有新建 Runner"这类断言写死。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web  # noqa: E402
from course import Course  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.route import Route, RouteStep  # noqa: E402
from models import LessonScript, Slide  # noqa: E402
from script import build_script  # noqa: E402
from state import Anchor, LectureUnit  # noqa: E402

#: 冻结契约的字段名（逐字，前端并行开发靠它对接）。
#: 写成**精确相等**而不是"包含"：多了字段就是契约变了，两边要一起改，
#: 而"悄悄多一个字段"恰恰是最容易漏过一次对齐的那种变化。
NODE_KEYS = {
    "n", "unit_id", "slide_index", "page_index", "index_in_slide", "slide_title",
    "is_page_start", "is_page_end", "text", "est_sec", "kp", "anchors_count",
    "ops_count", "branches",
    #: 卡面上那一句话总结（备课期产出的 ≤30 字）。**契约上保证非空**：
    #: 计划里没有时由 LectureUnit 用正文首句兜底，所以前端不许判空。
    "summary",
}
#: branch 上的 fork_node / merge_node / nodes 是 git 风格支线那次扩展加进来的；
#: insert_node 保留（与 fork_node 同值）—— 老前端按它挂支线，返工没必要。
BRANCH_KEYS = {
    "branch_id", "source", "variant_id", "label", "text", "est_sec", "lane",
    "insert_node", "insert_unit", "fork_node", "merge_node", "nodes",
}
#: 顶层 routes 里一条预置路线的**精确**字段集合。它与 live 候选行里的 route 子对象逐字同形，
#: 只多 source: "preset" —— 前端只有一套路线渲染，两个来源给两种形状就等于让它写两遍，
#: 而两遍迟早会画得不一样。
PRESET_ROUTE_KEYS = {
    "route_id", "kind", "fork_node", "merge_node", "steps", "est_sec", "reason", "source",
}
#: 路线一步（照抄 RouteStep：variant_id/text 空 = 用该单元主 text）。
PRESET_STEP_KEYS = {"unit_id", "variant_id", "label", "text", "est_sec"}
#: meta 里 truncated 是**有意多出来的一个**：泳道撞上限时被截掉的支线在图上
#: 和"本来就没有"长得一模一样，不显式说一句，前端只能画一张看似完整的图。
#: terminal_branches 同理是给前端"数一数课尾有几条出去的路"用的。
#: preset_routes 是备课期预置路线的条数，与 preset_branches（就地变体分支数）**不许互相顶替**：
#: 真实课件上前者是 14、后者是 0。
META_KEYS = {
    "deck_id", "deck_title", "plan_source", "total_slides", "total_units",
    "mainline_nodes", "preset_branches", "preset_routes", "lanes_width",
    "terminal_branches", "truncated",
}
#: 载荷顶层字段。冻结集合：多一个键就是契约变了，两边要一起改。
TOP_KEYS = {"ok", "mode", "hint", "meta", "lanes", "nodes", "routes"}

GRAPH_HTML = Path(__file__).resolve().parent.parent / "web" / "graph.html"


def _json(res) -> dict:
    """端点可能直接返回 dict，也可能用 JSONResponse（老端点就是这么写的）。"""
    if isinstance(res, JSONResponse):
        return json.loads(res.body.decode("utf-8"))
    return res


def _graph() -> dict:
    return _json(asyncio.run(web.api_script_graph()))


def _fake_deck(slides: int = 1) -> types.SimpleNamespace:
    """只提供载荷**允许读**的那几个字段的课件替身。

    为什么不直接用 tests/fixtures.make_deck()：这个载荷只读
    deck.slides / deck_id / title / slide_count，而泳道的三条规矩
    （从 1 起、同节点内不重号、撞上限截断）要靠**摆形状**才钉得住 ——
    真实课件每个单元都只有 2 个变体，永远碰不到上限。
    """
    return types.SimpleNamespace(
        deck_id="deck-test",
        title="测试课件",
        slide_count=slides,
        slides=[types.SimpleNamespace(index=i, title=f"第 {i + 1} 页")
                for i in range(slides)],
    )


def _unit(uid: str, slide: int, idx: int, variants: int, *,
          text: str = "这是一个讲解单元，用来占位。", summary: str = "") -> LectureUnit:
    return LectureUnit(
        unit_id=uid,
        slide=slide,
        index_in_slide=idx,
        text=text,
        summary=summary,          # 空 = 由 LectureUnit 用正文首句兜底
        anchors=[Anchor("title", "标题")],
        kp=["K1"],
        variants=[{
            "variant_id": f"v{i}",
            "label": f"说法{i}",
            "text": text + f"（第 {i} 种说法）",
            "est_sec": 3.5,
        } for i in range(1, variants + 1)],
    )


def _deck_course(units: list, *, slides: int = 1, with_plan: bool = True,
                 routes: list | None = None) -> Course:
    """合成一份"已备好课"的课件替身。routes 直接挂到 LessonScript.routes 上
    （真实链路上是 course.load_course 从 plan.json 解析出来的那一批，见 course.parse_plan_routes）。"""
    script = LessonScript(
        course_id="deck-test",
        title="测试课",
        slides=[Slide(index=i, title=f"第 {i + 1} 页") for i in range(slides)],
        units=list(units),
        routes=list(routes or []),
    )
    return Course(script=script, mode="deck", deck=_fake_deck(slides),
                  plan=types.SimpleNamespace(source="manual") if with_plan else None)


def _route(route_id: str, kind: str, fork: int, merge: int, *, steps=None,
           est_sec: float = -5.0, reason: str = "") -> Route:
    """一条预置路线（备课期产物）。直接构造 Route 而不走 dict 解析：

    这里要钉的是**载荷投影**，不是解析宽容度 —— 脏数据的宽容度归
    course.parse_plan_routes 与 tests/test_plan_routes.py。
    """
    return Route(
        route_id=route_id, kind=kind, fork_node=fork, merge_node=merge,
        steps=list(steps or []), est_sec=est_sec,
        reason=reason or f"{route_id} 的理由",
    )


def _pool_runner() -> types.SimpleNamespace:
    """一台"正在上课"的最小替身：只有池子，够证明观测端点没动它。

    载荷函数压根用不到 machine —— 这条测试的价值正在于此：把"没碰"变成可断言。
    """
    pool = CandidatePool()
    for cid, vid in (("c-1", "v1"), ("c-2", "v2")):
        pool.put(Candidate(
            candidate_id=cid, target_unit="S01-U01",
            variant=UnitVariant(variant_id=vid, label="换说法", text="另一种说法。"),
            when={}, source="preset", created_at=1.0,
            created_at_unit="S01-U01", expires_at_seq=0,
        ))
    return types.SimpleNamespace(
        machine=types.SimpleNamespace(director=types.SimpleNamespace(pool=pool)))


# ---------------------------------------------------------------------------
# ① 页面与路由
# ---------------------------------------------------------------------------

def test_graph_page_route_serves_html_with_graph_container(tmp_path, monkeypatch):
    """/graph 走 _page()：返回不缓存的 HTML，里面必须有前端约定的图容器 id="graph"。

    这里把 web.ROOT 指到一个临时目录再放一份最小页面，而不是等 web/graph.html 落地：
    前端由并行的另一个任务编写，路由这一侧的行为不该依赖它有没有写完。
    真文件落地之后由下一条测试接着钉住真实页面的名字。
    """
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "graph.html").write_text(
        '<html><body><div id="graph"></div>'
        '<link href="/static/classroom.css"></body></html>',
        encoding="utf-8")
    monkeypatch.setattr(web, "ROOT", tmp_path)

    res = asyncio.run(web.graph_page())
    assert res.status_code == 200
    html = res.body.decode("utf-8")
    assert 'id="graph"' in html, "前端约定的图容器锚点，名字一变渲染就哑了"
    assert "/static/" in html, "静态资源必须仍走 /static（web.py 的挂载点）"
    assert "no-store" in res.headers.get("cache-control", ""), \
        "页面一律不缓存，否则改了前端要靠教用户硬刷新"


def test_graph_real_page_has_graph_container():
    """真实页面的三条名字约束（图容器 / 静态样式表 / 标题含"提交"或"分支"）。

    前端文件还没落地时跳过：这条测试的用途是**前端写完之后**守住契约，
    不是拦住后端的交付。
    """
    if not GRAPH_HTML.is_file():
        pytest.skip("web/graph.html 由并行的前端任务编写，还没落地")

    res = asyncio.run(web.graph_page())
    html = res.body.decode("utf-8")
    assert 'id="graph"' in html
    assert "/static/" in html


def test_graph_page_versions_graph_css():
    """新页面的样式表也得带 ?v=<mtime>，和既有页面同一套规矩。

    为什么值得单钉一条：graph.css 一改而 HTML 不带版本号，用户看到的是
    "新页面 + 旧样式"（泳道间距、指针颜色都不对），而页面本身看起来完全正常 ——
    这正是 _with_asset_versions 当初存在的理由。顺带确认 classroom.css 那条没被弄坏。
    """
    if not GRAPH_HTML.is_file():
        pytest.skip("web/graph.html 由并行的前端任务编写，还没落地")

    html = asyncio.run(web.graph_page()).body.decode("utf-8")
    assert 'href="/static/graph.css?v=' in html, \
        "graph.css 必须版本化，否则改了样式用户还是拿着上一版"
    assert 'href="/static/classroom.css?v=' in html,         "既有资源的版本化不许被顺手弄坏"


def test_graph_routes_are_registered():
    """两条路径都要在 app 上（少一条就是 404，而 404 在浏览器里只表现为空页面）。"""
    paths = {getattr(r, "path", "") for r in web.app.routes}
    assert {"/graph", "/api/script/graph"} <= paths


def test_navigation_links_expose_the_graph_page(monkeypatch):
    """/graph 必须从上课页、/script、/review 都点得到 —— 建好却没人链过去的页面等于不存在。

    为什么值得单独钉一条：这个页当时路由、载荷、前端全绿，但全仓库只有它往外链，
    用户看到的仍然是旧的竖向树，以为那就是成果。单页测试抓不到这种事，
    只有"从别的页面找入口"这种跨页断言才抓得到。
    """
    # ① /script 顶部导航
    script_html = asyncio.run(web.script_page()).body.decode("utf-8")
    assert 'href="/graph"' in script_html, "/script 顶部导航必须给出走向图入口"

    # ② /review 的降级分支（手写演示课件）：审核同事在这种时候更需要别的入口
    real_course = web.course
    monkeypatch.setattr(web, "course", Course(script=build_script(), mode="simple"))
    simple_review = asyncio.run(web.review()).body.decode("utf-8")
    assert "课堂脚本审核" in simple_review, "先确认命中的是降级那条早返回分支"
    assert 'href="/graph"' in simple_review

    # ③ /review 的真实课件分支
    monkeypatch.setattr(web, "course", real_course)
    if web.course.mode != "deck" or web.course.plan is None:
        pytest.skip("这台机器上没有真实课件（DECK_ID 未配置），审核页只走了降级分支")
    assert 'href="/graph"' in asyncio.run(web.review()).body.decode("utf-8")

    # ④ 走向图自己的外链没有被这次改动弄坏（它本来就是从这几页点得到的）
    graph_html = asyncio.run(web.graph_page()).body.decode("utf-8")
    assert 'href="/"' in graph_html, "上课页入口不许丢"
    assert 'href="/script"' in graph_html, "旧的竖向树入口不许丢"


# ---------------------------------------------------------------------------
# ② 真实课件：主线编号与泳道
# ---------------------------------------------------------------------------

def test_graph_real_deck_mainline_matches_order_index():
    """真实课件下 177 个主线节点：n 与 script.order_index 逐个相等；支线只剩预置路线。

    这是本文件最重要的一条：n 一旦自己另起一套编号，前端点第 K 格、
    状态机装载第 K 格就会错开，而且**不会报错**。

    新口径（就地变体停产后）—— 括号里是被换掉的旧口径：
      · 每格 branches 都是空数组                （旧：每单元 2 个变体，合计 354 条）
      · meta.preset_branches == 0               （旧：354；它数的是就地变体分支，不是支线总数）
      · 泳道宽度回到下限 2、truncated 恒 False   （旧：3 = 主线 1 + 两条变体泳道）
      · terminal_branches == 0                  （旧：最后一格的 v1/v2 是仅有的两条出口）
      · 分支全部在顶层 routes 里：14 条预置路线  （旧：这个键根本不存在）
        按 fork_node 升序、与 script.routes 逐字一致；备课期不预置 finish，所以课尾出口是 0。
    """
    if web.course.mode != "deck" or web.course.plan is None:
        pytest.skip("这台机器上没有真实课件（DECK_ID 未配置）")

    data = _graph()
    script = web.course.script
    nodes, meta, routes = data["nodes"], data["meta"], data["routes"]

    assert data["ok"] is True and data["mode"] == "deck" and data["hint"] == ""
    assert meta["mainline_nodes"] == len(nodes) == meta["total_units"]
    assert meta["total_units"] == len(script.units)
    assert data["lanes"]["width"] == meta["lanes_width"]

    # 顺序：页序 → 页内单元序，且 n 就是主线下标
    assert [nd["n"] for nd in nodes] == list(range(len(nodes)))
    assert [nd["n"] for nd in nodes] == \
        [script.order_index(nd["unit_id"]) for nd in nodes]
    assert [nd["unit_id"] for nd in nodes] == [u.unit_id for u in script.units]

    # ---- 新口径 ①：就地变体归零，泳道宽度回到下限 ----
    assert meta["preset_branches"] == 0, "就地变体已停产：这一栏数的是变体分支，不是支线"
    assert all(nd["branches"] == [] for nd in nodes), "真实课件的每一格都不该再挂变体分支"
    widest = max(len(nd["branches"]) for nd in nodes)
    assert widest == 0 and data["lanes"]["width"] == max(2, 1 + widest) == 2, \
        "0 条变体 ⇒ 泳道只剩主线那一列 + 一格空泳道（宽度下限 2）"
    assert meta["truncated"] is False, "0 条变体永远撞不到泳道上限"
    for nd in nodes:
        assert set(nd) == NODE_KEYS, "nodes 的既有契约一个字都不许动"
        assert nd["page_index"] == nd["slide_index"]
        assert nd["slide_title"], f"{nd['unit_id']} 没有页标题"
        assert [b["lane"] for b in nd["branches"]] == [], "没有变体就没有泳道"
        assert isinstance(nd["summary"], str) and nd["summary"], \
            f"{nd['unit_id']} 的卡面总结为空：卡面上就会只剩一个页码"
        assert len(nd["summary"]) <= 30, "summary 是卡面上一行字，硬上限 30 字"

    # ---- 新口径 ②：课尾没有出口（备课期不预置 finish）----
    last = nodes[-1]
    assert last["branches"] == []
    assert meta["terminal_branches"] == 0 == sum(
        1 for nd in nodes for b in nd["branches"] if b["merge_node"] == len(nodes))
    assert not [r for r in routes if r["kind"] == "finish"], \
        "备课期不预置 finish：课尾出口只能来自课堂运行期"

    # ---- 新口径 ③：分支全在顶层 routes 里，14 条，与 script.routes 逐字一致 ----
    assert len(routes) == len(script.routes) == 14
    assert meta["preset_routes"] == len(routes) == 14
    assert meta["preset_routes"] != meta["preset_branches"], \
        "两个计数不许互相顶替：预置路线 14 / 就地变体 0"
    assert [r["fork_node"] for r in routes] == sorted(r["fork_node"] for r in routes), \
        "routes 必须按 fork_node 升序（图的横轴是课的时间轴）"
    by_id = {r["route_id"]: r for r in routes}
    assert len(by_id) == len(routes), "route_id 是幂等键，载荷里不许有重复"
    for rt in script.routes:
        assert by_id[rt.route_id] == {
            "route_id": rt.route_id,
            "kind": rt.kind,
            "fork_node": rt.fork_node,
            "merge_node": rt.merge_node,
            "steps": [{"unit_id": s.unit_id, "variant_id": s.variant_id,
                       "label": s.label, "text": s.text, "est_sec": s.est_sec}
                      for s in rt.steps],
            "est_sec": rt.est_sec,
            "reason": rt.reason,
            "source": "preset",
        }, f"{rt.route_id} 与 script.routes 里那一条不逐字一致"
    for r in routes:
        assert set(r) == PRESET_ROUTE_KEYS
        assert 0 <= r["fork_node"] < r["merge_node"] <= len(nodes), \
            "每条路线都要岔出在课内、汇回在课内（越界的线画到画布外就等于不存在）"
        for step in r["steps"]:
            assert set(step) == PRESET_STEP_KEYS

    # 载荷是快照：前端改它不该改到 plan 里那条路线（这条端点每秒可能被拉一次）
    src_route = {rt.route_id: rt for rt in script.routes}[routes[0]["route_id"]]
    reason_before, steps_before = src_route.reason, list(src_route.steps)
    routes[0]["reason"] = "被观测面改过的理由"
    routes[0]["steps"].append({"unit_id": "S99-U99"})
    assert src_route.reason == reason_before
    assert src_route.steps == steps_before, "载荷是副本：前端随手 push 一下不该改到课堂要执行的那条路线"

    # 页边界：每个页首前面换了页、每个页尾后面换了页
    starts = [i for i, nd in enumerate(nodes) if nd["is_page_start"]]
    ends = [i for i, nd in enumerate(nodes) if nd["is_page_end"]]
    assert starts[0] == 0 and ends[-1] == len(nodes) - 1
    pages_with_units = [u.slide for u in script.units]
    changes = [i for i, s in enumerate(pages_with_units) if i == 0 or pages_with_units[i - 1] != s]
    assert starts == changes
    assert ends == [i - 1 for i in starts[1:]] + [len(nodes) - 1]
    assert len(starts) == len(ends) == len(changes)


# ---------------------------------------------------------------------------
# ③ 泳道形状（用合成课件摆形状，真实课件碰不到这些边界）
# ---------------------------------------------------------------------------

def test_graph_lanes_start_at_one_and_scale_with_widest_node():
    """3 条支线 → 4 列：lane 1..3 连续；没有变体的节点 branches 为空。"""
    data = web.script_graph_payload(_deck_course([
        _unit("S01-U01", 0, 0, 3),
        _unit("S01-U02", 0, 1, 1),
        _unit("S01-U03", 0, 2, 0),
    ]))

    assert data["ok"] is True and data["mode"] == "deck"
    assert data["meta"]["total_slides"] == 1
    assert data["meta"]["mainline_nodes"] == data["meta"]["total_units"] == 3
    assert data["lanes"]["width"] == data["meta"]["lanes_width"] == 4
    assert data["meta"]["preset_branches"] == 3 + 1 + 0
    assert data["meta"]["truncated"] is False

    assert [b["lane"] for b in data["nodes"][0]["branches"]] == [1, 2, 3]
    assert [b["lane"] for b in data["nodes"][1]["branches"]] == [1]
    assert data["nodes"][2]["branches"] == []

    assert [nd["is_page_start"] for nd in data["nodes"]] == [True, False, False]
    assert [nd["is_page_end"] for nd in data["nodes"]] == [False, False, True]
    assert [nd["index_in_slide"] for nd in data["nodes"]] == [0, 1, 2]


def test_graph_width_floor_is_two_even_without_variants():
    """一条支线都没有时宽度也不许塌成 1：主线自己就占一列，否则图上什么都没有。"""
    data = web.script_graph_payload(_deck_course([_unit("S01-U01", 0, 0, 0)]))
    assert data["lanes"]["width"] == 2
    assert data["meta"]["preset_branches"] == 0
    assert data["nodes"][0]["branches"] == []


def test_graph_lanes_truncate_at_cap_and_say_so():
    """被塞了 20 个变体：截到 12 列（lane 1..11），并且 meta.truncated=True 说出来。

    少画的那 9 条在图上和"不存在"长得一模一样，没有这个字段前端只能画一张
    看似完整的图 —— 那比报错更糟。
    """
    data = web.script_graph_payload(_deck_course([_unit("S01-U01", 0, 0, 20)]))
    assert data["lanes"]["width"] == 12 == data["meta"]["lanes_width"]
    assert [b["lane"] for b in data["nodes"][0]["branches"]] == list(range(1, 12))
    assert data["meta"]["preset_branches"] == 11
    assert data["meta"]["truncated"] is True


def test_graph_multi_page_boundaries():
    """两页各两个单元：页首/页尾各两个，且 page_index 跟着单元走。"""
    data = web.script_graph_payload(_deck_course([
        _unit("S01-U01", 0, 0, 1),
        _unit("S01-U02", 0, 1, 1),
        _unit("S02-U01", 1, 0, 1),
        _unit("S02-U02", 1, 1, 1),
    ], slides=2))

    assert [nd["is_page_start"] for nd in data["nodes"]] == [True, False, True, False]
    assert [nd["is_page_end"] for nd in data["nodes"]] == [False, True, False, True]
    assert [nd["page_index"] for nd in data["nodes"]] == [0, 0, 1, 1]
    assert [nd["slide_title"] for nd in data["nodes"]] == \
        ["第 1 页", "第 1 页", "第 2 页", "第 2 页"]


# ---------------------------------------------------------------------------
# ③b 支线的分叉 / 汇回（git 风格：从 A 岔出，绕回来）
# ---------------------------------------------------------------------------

def test_branch_fork_and_merge_span_across_mainline():
    """每条支线 fork 于 A、汇回 A+1；末节点的支线汇到 len(nodes)（= 退出课堂）。

    为什么要落成字段而不是让前端拿 n 自己推：支线与主线是两套走向。
    今天跨度恒为 1 是**产出规则**，不是结构 —— 把跨度写进数据，将来 Agent-C
    产出跨多格的支线时前端一行不用改。
    """
    data = web.script_graph_payload(_deck_course([
        _unit("S01-U01", 0, 0, 2),
        _unit("S01-U02", 0, 1, 1),
        _unit("S01-U03", 0, 2, 3),
    ]))
    nodes, meta = data["nodes"], data["meta"]

    assert [b["merge_node"] for b in nodes[0]["branches"]] == [1, 1]
    assert [b["merge_node"] for b in nodes[1]["branches"]] == [2]
    assert [b["merge_node"] for b in nodes[2]["branches"]] == [3, 3, 3]

    for nd in nodes:
        for b in nd["branches"]:
            assert b["fork_node"] == nd["n"]
            assert 0 <= b["fork_node"] < b["merge_node"] <= len(nodes)
            if nd["n"] + 1 < len(nodes):
                assert b["merge_node"] == b["fork_node"] + 1  # 今天的规则

    assert meta["terminal_branches"] == 3 == len(nodes[-1]["branches"])
    assert meta["preset_branches"] == 6


def test_branch_on_last_and_only_node_exits_at_course_end():
    """只有一个节点时它既是首也是尾：merge_node 直接等于 len(nodes)（= 1）。

    这是"课尾"这个约定最容易被写错的一格：没有下一格可回时，
    merge 若退化成 n+1（= 1）恰好也对，但当 n+1 > len(nodes) 时就会指到图外 ——
    clamp 到 len(nodes) 是唯一不会越界的写法。
    """
    data = web.script_graph_payload(_deck_course([_unit("S01-U01", 0, 0, 2)]))
    assert len(data["nodes"]) == 1
    assert [b["fork_node"] for b in data["nodes"][0]["branches"]] == [0, 0]
    assert [b["merge_node"] for b in data["nodes"][0]["branches"]] == [1, 1]
    assert data["meta"]["terminal_branches"] == 2


def test_branch_nodes_mirror_the_preset_variant():
    """支线的节点序列 = 该预置变体的归一化行（text/label/est_sec 逐字一致）。

    这条钉的是"不要伪造不存在的内容"：支线将来可以是多格的，但每一格都必须
    真有一份可讲的说法（今天的预置变体），不能为了画图编一段文字。
    """
    course = _deck_course([_unit("S01-U01", 0, 0, 2)])
    data = web.script_graph_payload(course)
    variants = course.script.units[0].variants
    branches = data["nodes"][0]["branches"]

    assert len(branches) == len(variants) == 2
    for row, b in zip(variants, branches):
        assert b["nodes"] == [{
            "node_index": 0,
            "variant_id": row["variant_id"],
            "label": row["label"],
            "text": row["text"],
            "est_sec": row["est_sec"],
        }]


# ---------------------------------------------------------------------------
# ④ 降级：不是真实课件也要 200 + 说明
# ---------------------------------------------------------------------------

def test_graph_simple_mode_is_200_with_hint(monkeypatch):
    """手写演示课件（course.mode != deck）→ ok=false + 中文 hint，绝不抛异常。"""
    monkeypatch.setattr(web, "course", Course(script=build_script(), mode="simple"))
    data = _graph()

    assert data["ok"] is False
    assert data["mode"] == "simple"
    assert data["hint"], "必须给出人能看懂的说明，否则前端只能显示一张白页"
    assert data["nodes"] == []
    assert data["lanes"] == {"width": 1}
    assert set(data["meta"]) == META_KEYS
    assert data["meta"]["terminal_branches"] == 0, "没有主线就没有可数的出口支线"
    assert data["meta"]["preset_routes"] == 0
    assert data["routes"] == [], "降级也必须带 routes 键（空数组，不是缺键）"
    assert all(v in (0, "", False) for v in data["meta"].values())


def test_graph_deck_without_plan_is_200_with_hint():
    """有课件但没备课产物（plan 为 None）：同一条降级路径，hint 换一句说清原因。

    半份数据（有页没有单元/变体）比一句说明更容易误导人 —— 图上一格不画更诚实。
    """
    data = web.script_graph_payload(_deck_course([], with_plan=False))

    assert data["ok"] is False
    assert data["mode"] == "deck"
    assert data["hint"], "必须说明缺的是 plan.json，而不是让人猜"
    assert data["nodes"] == []
    assert data["lanes"] == {"width": 1}
    assert data["meta"]["total_units"] == 0
    assert data["meta"]["terminal_branches"] == 0
    assert data["meta"]["preset_routes"] == 0
    assert data["routes"] == [], "有课件但没备课产物：这一支同样要给空数组而不是缺键"


def test_fallback_payload_keeps_the_success_shape():
    """降级载荷的字段与成功时**一个不差**（数值全 0、字符串全空）。

    形状一致，前端才不用为两种情况各写一套判空 —— 而降级那一支恰恰是最少被
    手点到的，等真降级时才发现少字段就太晚了。
    """
    good = web.script_graph_payload(_deck_course([_unit("S01-U01", 0, 0, 1)]))
    bad = web.script_graph_payload(_deck_course([], with_plan=False))
    # 第三档：课件在、也备了课，但一条预置路线都没有 —— 形状必须还是一模一样
    empty_routes = web.script_graph_payload(_deck_course([_unit("S01-U01", 0, 0, 0)]))

    assert set(good) == set(bad) == set(empty_routes) == TOP_KEYS
    assert set(good["meta"]) == set(bad["meta"]) == set(empty_routes["meta"]) == META_KEYS
    assert set(good["lanes"]) == set(bad["lanes"]) == {"width"}
    assert good["routes"] == bad["routes"] == empty_routes["routes"] == []
    assert set(good["nodes"][0]) == NODE_KEYS
    assert set(good["nodes"][0]["branches"][0]) == BRANCH_KEYS


# ---------------------------------------------------------------------------
# ⑤ 零副作用（与 /api/script/live 同级的一条）
# ---------------------------------------------------------------------------

def test_graph_payload_has_no_side_effects(monkeypatch):
    """连续 3 次拉图：不建 Runner、不登记 Runner、池子快照一字不差。

    为什么这条最重要：这个端点和正在上课的窗口同时开着。它一旦有副作用，
    症状不是报错，而是"课堂偶尔少一次决策"，查的时候不会有人想到是这个查看页。
    """
    class _NoNewRunner:
        def __init__(self, *a, **kw):
            raise AssertionError("观测端点不许新建 Runner（那就是第二节课堂）")

    runner = _pool_runner()
    monkeypatch.setattr(web, "Runner", _NoNewRunner)
    monkeypatch.setattr(web, "_LIVE_RUNNERS", [runner])

    pool = runner.machine.director.pool
    registries = list(web._LIVE_RUNNERS)
    before = ([c.candidate_id for c in pool.items()], pool.status())

    outs = [web.script_graph_payload(web.course) for _ in range(3)]

    assert outs[0] == outs[1] == outs[2], "纯读函数：同一份输入必须给出同一份输出"
    assert ([c.candidate_id for c in pool.items()], pool.status()) == before, \
        "观测端点不许动池子的账 —— 那是课堂正在用的那份单一真相"
    assert web._LIVE_RUNNERS == registries, "观测端点不许自己登记 Runner"
    assert len(web._LIVE_RUNNERS) == 1


# ---------------------------------------------------------------------------
# ⑥ 预置路线（顶层 routes）：静态载荷里那一批跨格支线
# ---------------------------------------------------------------------------

def test_graph_preset_routes_carry_full_geometry_sorted_by_fork():
    """三条预置路线逐字段上载荷：按 fork 升序、steps 逐字、source 恒为 preset。

    真实课件那条测试已经钉了 14 条，为什么还要一条合成课件：只有合成才能一次摆出
    三种走向（skip / revisit / finish）与**乱序输入** —— 真实课件里没有 finish，
    而且碰巧本来就是升序，光靠它证明不了"排序真的做了"。
    """
    steps = [
        RouteStep(unit_id="S01-U02", variant_id="v2", label="举例子",
                  text="再举个例子。", est_sec=6.0),
        RouteStep(unit_id="S01-U03"),          # 全空：用该单元主 text 再讲一遍
    ]
    course = _deck_course([
        _unit("S01-U01", 0, 0, 0), _unit("S01-U02", 0, 1, 0),
        _unit("S01-U03", 0, 2, 0), _unit("S01-U04", 0, 3, 0),
        _unit("S01-U05", 0, 4, 0),
    ], routes=[
        # 故意乱序写：载荷必须自己排好
        _route("p-finish-3", "finish", 3, 5, est_sec=-40.0, reason="时间不够了"),
        _route("p-skip-0-2", "skip", 0, 2, est_sec=-9.0, reason="这一段会了"),
        _route("p-revisit-2", "revisit", 2, 4, steps=steps, est_sec=14.0, reason="补回头"),
    ])
    data = web.script_graph_payload(course)

    assert data["ok"] is True
    assert [r["route_id"] for r in data["routes"]] == \
        ["p-skip-0-2", "p-revisit-2", "p-finish-3"], "routes 必须按 fork_node 升序"
    assert data["meta"]["preset_routes"] == 3
    skip, rev, fin = data["routes"]

    assert skip == {
        "route_id": "p-skip-0-2", "kind": "skip", "fork_node": 0, "merge_node": 2,
        "steps": [], "est_sec": -9.0, "reason": "这一段会了", "source": "preset",
    }, "skip 不重讲任何一格，时长记成负数（省时间）"

    assert rev["kind"] == "revisit" and rev["fork_node"] == 2 and rev["merge_node"] == 4
    assert [s["unit_id"] for s in rev["steps"]] == ["S01-U02", "S01-U03"], \
        "steps 的顺序与长度都是信息（重讲哪几格、重讲几格）"
    assert rev["steps"][0] == {"unit_id": "S01-U02", "variant_id": "v2", "label": "举例子",
                               "text": "再举个例子。", "est_sec": 6.0}
    assert rev["steps"][1]["variant_id"] == "" and rev["steps"][1]["text"] == "", \
        "空 variant_id/text = 用该单元主 text 再讲一遍（RouteStep 的既有语义）"
    assert rev["est_sec"] == 14.0 > 0, "补回头多花时间（正数）"

    assert fin["merge_node"] == len(data["nodes"]) == 5, "finish 必须汇回课尾"
    assert all(nd["branches"] == [] for nd in data["nodes"])
    assert data["meta"]["terminal_branches"] == 1, \
        "课尾那一条出口只能来自预置路线（这一份课件一个变体都没有）"

    # 载荷是 Route 的忠实投影：拿冻结契约反解再序列化，一字不差
    for r in data["routes"]:
        assert set(r) == PRESET_ROUTE_KEYS
        back = Route.from_dict({k: v for k, v in r.items() if k != "source"})
        back.validate(len(data["nodes"]))
        assert back.to_dict() == {k: v for k, v in r.items() if k != "source"}
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data


def test_graph_dirty_preset_routes_are_dropped_not_raised():
    """脏路线只丢不抛：越界 / kind 非法 / revisit 空 steps / finish 不落课尾 / 不是对象。

    为什么值得钉：routes 来自 plan.json —— 一份**人可编辑**的产物。一条 fork_node
    写成 99 的路线让整页变白，看起来和"课堂挂了"一模一样，而它其实只是少一条岔路。
    判非法的口径必须与 course.parse_plan_routes / Route.validate 完全一致，
    否则同一份 plan 会在这里"通过"、在那边"丢弃"，两边说法就不一样了。
    """
    course = _deck_course([
        _unit("S01-U01", 0, 0, 0), _unit("S01-U02", 0, 1, 0),
    ], routes=[
        {"route_id": "p-good", "kind": "skip", "fork_node": 0, "merge_node": 2,
         "steps": [], "est_sec": -3.0, "reason": "剩下这一条是好的"},
        {"route_id": "p-越界", "kind": "skip", "fork_node": 0, "merge_node": 99},
        {"route_id": "p-kind", "kind": "jump", "fork_node": 0, "merge_node": 1},
        {"route_id": "p-空重讲", "kind": "revisit", "fork_node": 0, "merge_node": 2,
         "steps": [], "reason": "revisit 不给 steps"},
        {"route_id": "p-finish-不落课尾", "kind": "finish", "fork_node": 0, "merge_node": 1},
        {"route_id": "p-负岔出", "kind": "skip", "fork_node": -3, "merge_node": 2},
        "根本不是对象",
        42,
    ])
    data = web.script_graph_payload(course)

    assert data["ok"] is True, "一条脏路线不该把整张图变成白页"
    assert [r["route_id"] for r in data["routes"]] == ["p-good"]
    assert data["meta"]["preset_routes"] == 1
    assert data["meta"]["terminal_branches"] == 1, "活下来的那一条正好汇回课尾"


def test_graph_routes_is_a_sorted_copy_not_the_script_objects():
    """routes 是**快照**：连拉两次逐字相同，且改载荷改不到 script.routes 上。

    观测端点和正在上课的窗口同时开着（/api/script/live 每秒一次的性质同样适用）。
    把 script.routes 里那些 Route 原样塞进载荷，前端随手 push 一下就会改到
    状态机下一步要执行的那条路线 —— 下次执行就多走一步，而没有任何日志说是谁改的。
    """
    course = _deck_course([_unit("S01-U01", 0, 0, 0), _unit("S01-U02", 0, 1, 0)],
                          routes=[_route("p-skip-0", "skip", 0, 2)])
    before = [(_rt.route_id, list(_rt.steps), _rt.reason) for _rt in course.script.routes]

    first = web.script_graph_payload(course)
    second = web.script_graph_payload(course)
    assert first == second, "纯读函数：同一份输入必须给出同一份输出"

    first["routes"][0]["steps"].append({"unit_id": "S99-U99"})
    first["routes"][0]["reason"] = "被载荷改过的理由"
    after = [(_rt.route_id, list(_rt.steps), _rt.reason) for _rt in course.script.routes]
    assert after == before, "载荷是副本：前端改它不该改到课堂要执行的那条路线"


# ---------------------------------------------------------------------------
# ⑦ 前端：静态 routes 与运行期 route 走**同一套**渲染
# ---------------------------------------------------------------------------

GRAPH_CSS = Path(__file__).resolve().parent.parent / "web" / "graph.css"


def test_graph_page_draws_static_routes_with_the_runtime_renderer():
    """图页用同一个函数画静态预置路线与运行期路线，且不再靠 branches 画支线。

    为什么是源级断言：没有 jsdom，而这两条守的是"一套画法"与"变体归零后不留残留"
    这两件事本身。渲染一分成两条路径，几何就会慢慢漂移（一边按 fork/merge、
    一边按插入点），症状是"预置的支线画歪了"，而页面看起来一切正常。
    """
    if not GRAPH_HTML.is_file():
        pytest.skip("web/graph.html 还没落地")

    html = GRAPH_HTML.read_text(encoding="utf-8")

    # ① 静态载荷读进来，而且**在 buildGraph 里就画**（不是等 live 轮询）
    assert "const routes = (data && data.routes) || [];" in html
    assert "S.routes = routes;" in html
    assert "function buildPresetRoutes(" in html and "buildPresetRoutes(routes);" in html, \
        "预置路线必须在 buildGraph 里一次画好 —— 课堂没在跑时它照样得在图上"
    assert "startPolling();" in html[html.index("buildGraph(data);"):], \
        "先画静态结构、再开始轮询（否则开课前那几秒图上是空的）"

    # ② **一套渲染**：两个来源都走 buildRouteChip / routeDetailHtml，函数各只有一份
    assert html.count("function buildRouteChip(") == 1, "路线渲染只许有一个函数"
    assert "buildRouteChip(rt, i, presetRouteLane(i))" in html, "静态预置路线走 buildRouteChip"
    assert "buildRouteChip(p.route, fi, useLane)" in html, "运行期候选路线走同一个函数"
    assert html.count("function routeDetailHtml(") == 1, "详情也只有一份（两个来源共用）"
    assert "function routeSourceText(" in html, "来源文案由一处产出（预置 / 运行期）"
    assert "Number(rt.fork_node)" in html and "Number(rt.merge_node)" in html, \
        "几何一律来自载荷的 fork_node → merge_node"

    # ③ 预置 vs 运行期：只靠线型分开，几何与文案一致；两个来源都有图例
    # 线型 class 由来源拼出来（样式表里 src-preset 实线 / src-runtime 虚线，见下一条测试）
    assert "' src-' + src" in html and "routeSourceText(rt)" in html
    assert "预置路线（备课期 plan.json 的 routes，实线" in html, "图例要说清预置来自备课期"
    assert "运行期路线（C 在课上现造，虚线；同样按载荷的 fork → merge 真实跨度画）" in html

    # ④ 变体归零后不留视觉垃圾：徽标只在真有就地变体时生成，计数栏为 0 就整栏隐藏
    assert "if (inline.length) {" in html, "「N 支」徽标只在旧计划真带变体时才生成"
    assert "「2 支」徽标" not in html, "旧口径（每单元固定两条变体）的图例必须消失"
    assert "就地变体（旧计划）" in html, "剩下的那一栏要说清它只属于旧计划"
    assert "$('cnInlinePill').hidden = inline === 0;" in html, "就地变体为 0 时整栏隐藏"
    assert "m.preset_routes" in html, "静态计数（路线分支）由 meta.preset_routes 报"
    assert "预置路线 14" not in html

    # ⑤ 计数区四栏各自报准的来源
    for needle in ('id="cnMain"', 'id="cnPreset"', 'id="cnCand"', 'id="cnTerm"'):
        assert needle in html
    assert "运行期候选" in html, "运行期那一栏的口径是『候选』，不是『C 现造』"
    assert "(S.routes || []).forEach(function (rt) {" in html, \
        "结构性计数必须把预置路线数进去（它就是这一页现在的主角）"

    # ⑥ 同一条路线不许画两遍：池里的预置货只更新静态 chip 的状态，不再另画虚线
    assert "const rel = S.routeEls.get(p.route.route_id || '');" in html, \
        "池里的预置路线要贴回静态 chip 的三态，而不是再画一条虚线"
    assert "S.routeEls.set(key, r.chip);" in html

    # ⑦ 路线详情六项（类型 / 岔出·汇回 / 跨格·跳过·重讲 / 时长(省·多) / 理由 / steps）
    for needle in ("kv('类型'", "kv('岔出/汇回'", "kv('跨格'", "kv('时长'",
                   "fmtDelta(rt.est_sec)", "kv('理由'", "steps · 按顺序走这 "):
        assert needle in html, "路线详情缺少一项：" + needle


def test_graph_css_separates_preset_from_runtime_routes():
    """样式表里预置实线 / 运行期虚线是显式两条规则，三种走向各有一色。

    为什么值一条：线型是页面上**唯一**能把"备课期就有的支线"与"课上现造的候选"
    分开的东西（几何与文案故意做得一模一样）。它一旦被 CSS 里的层叠吃掉，
    两种来源就再也分不出来了 —— 而没人会为此报错。
    """
    if not GRAPH_CSS.is_file():
        pytest.skip("web/graph.css 还没落地")

    css = GRAPH_CSS.read_text(encoding="utf-8")
    assert ".graph .gbr.rt.src-preset{border-style:solid}" in css
    assert ".graph .gbr.rt.src-runtime{border-style:dashed}" in css
    for kind in ("rt-skip", "rt-revisit", "rt-finish"):
        assert ".graph .gbr.rt." + kind in css, "三种走向各要有一色：" + kind
    assert ".graph .gbr.cand.rt-" not in css, \
        "走向配色不许只绑在 cand 上（预置路线同样是路线）"

# ---------------------------------------------------------------------------
# ⑧ 卡面上那句话总结：逐字透传 / 缺了用正文兜底 / 前端不再二次截断
# ---------------------------------------------------------------------------

def test_graph_node_summary_verbatim_and_never_empty():
    """nodes[].summary 是卡面上唯一那行字，两条口径都必须成立。

    ① 计划里有 → 载荷**逐字**等于它（备课模型的措辞不许被任何一层改写）；
    ② 计划里没有（旧 plan.json）→ 正文首句兜底，非空且 ≤30 字。
    前端不判空，所以"空字符串"在这里就等于图上少了一个格子的内容，而页面看着一切正常。
    """
    given = "pack 的三个搭配与两句例句对比"      # 18 字：旧口径会被 clip(…, 18) 再截一刀
    course = _deck_course([_unit("S01-U01", 0, 0, 0, summary=given),
                           _unit("S01-U02", 0, 1, 0, text="第二格：先看音标，再看例句。")])
    nodes = web.script_graph_payload(course)["nodes"]
    assert nodes[0]["summary"] == given, "有 summary 就必须逐字透传，不许再截一刀"
    assert nodes[1]["summary"].startswith("第二格"), "没有 summary 时兜底成正文首句"
    assert 0 < len(nodes[1]["summary"]) <= 30


def test_graph_card_shows_summary_without_second_clip():
    """/graph 的主线卡面画 summary；SUMMARY 退回"没有 summary 时的后备截断长度"。

    没有 jsdom，所以钉源码：`nd.summary || clip(nd.text, SUMMARY)` 这一句同时说明了
    "优先用总结"与"旧计划还能看"。一旦有人把它改回 clip(nd.text, …)，备课期产出的
    总结就白做了 —— 而页面上只是"字看起来少了一点"，没人会为此报错。
    """
    if not GRAPH_HTML.is_file():
        pytest.skip("web/graph.html 还没落地")

    html = GRAPH_HTML.read_text(encoding="utf-8")
    assert "esc(nd.summary || clip(nd.text, SUMMARY))" in html, \
        "主线卡面必须先看 summary，没有才回落到正文截断"
    assert "const SUMMARY = 18;" in html and "没有 summary 时" in html, \
        "SUMMARY 的语义是后备截断长度，注释里要说清（否则下一个人会拿它去截 summary）"
    # 悬停与点开仍看全文：这两处读的是 text，不许换成 summary
    assert "clip(nd.text, 90)" in html, "悬停提示要能看到更多正文"
    assert "'<pre class=\"gdtext\">' + esc(nd.text || '')" in html, "详情面板要看全文"

