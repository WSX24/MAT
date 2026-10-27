"""分支树观测面（/script + /api/script/tree + /api/script/live）的测试。

为什么单开一个文件
------------------
这一页和别的页面有一个根本区别：**它要在课堂正讲着的时候被每秒调用**。
所以这里要钉的不是"能不能显示"，而是三件会静默出事的事：

  1. course.mode != "deck"（手写演示课件）时**不许 500** —— 这是"这份课件没有这个
     视图"，不是"服务器坏了"。抛出去的表现是审核同事看到一片红，而课堂本身没事。
  2. /api/script/live **零副作用**：它只许读 state / events / pool。一旦有人往里
     加一句"顺手推进一下"或者"顺手取一次 MCP 事件"，课堂那边会安静地少一次事件、
     少一次决策 —— 而这一页看起来完全正常，事后没有任何东西能对账。
  3. CandidatePool.items() 是**快照**：观测面改到池子的账上，改的就是课堂正在用的
     那份单一真相。

调用方式是 asyncio.run(web.api_xxx())（与 tests/test_modelcfg.py 同一套手法）：
项目没有装 httpx，fastapi.testclient 用不了；而这套端点本来就是纯 async 函数，
直接 await 反而更快，也更容易把"没有新建 Runner"这类断言写死。
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
from director.decider import Director, RuleDecider  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from eval import MetricsTracker  # noqa: E402
from script import build_script  # noqa: E402
from state import ClassroomState, Phase  # noqa: E402

#: 真实课件（.env 的 DECK_ID 指向的那份）的规模。这三条数写在验收里，
#: 少了任何一条就说明 tree 的 pages / units / routes 有一边漏了。
#:
#: 口径变更（变体停产、真实课件迁到新格式）：REAL_VARIANTS 从 354 变成 0 ——
#: 就地变体已停产，plan.json 迁成"顶层 routes + 单元里没有 variants"，加载时那条
#: "旧格式才补挂 354 条"的分支不再触发。分支改由预置路线承载，条数记在 REAL_ROUTES。
REAL_SLIDES = 42
REAL_UNITS = 177
REAL_VARIANTS = 0
REAL_ROUTES = 14


def _json(res) -> dict:
    """端点可能直接返回 dict，也可能用 JSONResponse（老端点就是这么写的）。"""
    if isinstance(res, JSONResponse):
        return json.loads(res.body.decode("utf-8"))
    return res


def _tree() -> dict:
    return _json(asyncio.run(web.api_script_tree()))


def _live() -> dict:
    return _json(asyncio.run(web.api_script_live()))


def _cand(cid: str, unit: str, vid: str, label: str, source: str) -> Candidate:
    return Candidate(
        candidate_id=cid,
        target_unit=unit,
        variant=UnitVariant(variant_id=vid, label=label, text="另一种说法。"),
        when={},
        source=source,
        created_at=1.0,
        created_at_unit=unit,
        expires_at_seq=0,
    )


def _fake_machine(slide_index: int = 3, seg_index: int = 0):
    """一台"正在上课"的最小替身：只提供观测端点**允许读**的那三样东西。

    不用真状态机：真状态机要起预取 task，测"零副作用"时反而分不清
    "多出来的 task 是端点造的，还是课堂本来就有的"。
    """
    state = ClassroomState()
    state.slide_index = slide_index
    state.seg_index = seg_index
    state.phase = Phase.LECTURING
    state.seq = 11
    unit_id = web.script.unit_of(slide_index, seg_index).unit_id
    state.unit_progress = {unit_id: 2}

    metrics = MetricsTracker()
    metrics.event("variant_routed", site="S03-U02", target=unit_id,
                  candidate_id="c-" + unit_id + "-v1", variant_id="v1", position=(3, 0, 1))
    metrics.event("variant_applied", unit=unit_id, variant_id="v1", label="换说法",
                  candidate_id="c-" + unit_id + "-v1", decided_at="S03-U02",
                  position=(3, 0, 1), probe_before="timeout", facts_before="facts")
    metrics.event("variant_replay_end", unit=unit_id, reason="done", label="换说法",
                  variant_id="v1", probe_before="timeout", probe_after="confirmed", sec=1.2)

    pool = CandidatePool()
    pool.put(_cand("c-" + unit_id + "-v1", unit_id, "v1", "换说法", "preset"))
    pool.put(_cand("c-agent-S05-U02-x1", "S05-U02", "x1", "换个例子", "agent_c"))

    director = Director(pool, RuleDecider())
    return types.SimpleNamespace(state=state, metrics=metrics, director=director,
                                 script=web.script)


def _register(monkeypatch, machine) -> None:
    monkeypatch.setattr(web, "_LIVE_RUNNERS", [types.SimpleNamespace(machine=machine)])


# ---------------------------------------------------------------------------
# ① 页面与路由
# ---------------------------------------------------------------------------

def test_script_page_returns_html_with_tree_container():
    """GET /script 是纯静态页：它必须自带树的容器，否则前端根本没地方挂 42 页。

    这里不断言"画得对不对"（那要靠人眼看），只钉住三处**名字**：
    树容器 id、样式表、标题 —— 前端重构时这三个名字一变，测试就该响。
    """
    res = asyncio.run(web.script_page())
    assert res.status_code == 200
    html = res.body.decode("utf-8")
    assert 'id="tree"' in html
    assert "/static/script.css" in html
    assert "分支树" in html
    # 只读轮询的两条硬要求都写在页面上：页面隐藏就停、间隔 1 秒
    assert "document.hidden" in html
    assert "POLL_MS = 1000" in html


def test_script_routes_are_registered():
    """三个路径都要在 app 上（少一个就是 404，而 404 在浏览器里只表现为空页面）。"""
    paths = {getattr(r, "path", "") for r in web.app.routes}
    assert {"/script", "/api/script/tree", "/api/script/live"} <= paths


# ---------------------------------------------------------------------------
# ② 静态结构：真实课件的规模与形状
# ---------------------------------------------------------------------------

def test_default_render_is_fully_collapsed_regardless_of_branches():
    """首次加载一律全折叠 —— 折叠是常量，**不许由"这一页有没有分支"推出来**。

    口径变更（变体停产 + 真实课件迁到新格式）：旧版这条用例的数据前提是
    "177/177 个单元都有变体，于是 hasBranch 恒真"。变体停产后那个前提消失了 ——
    真实课件现在一个变体都没有，hasBranch 恒**假**。而"折叠不许挂在 hasBranch 上"
    这条要求反而更硬了：今天恒假、将来预置路线画进这一页之后又会变真，任何一个恒值
    都会让 (hasBranch ? '' : ' collapsed') 这种写法在两端各错一次
    （恒真 = 打开就全展开，177 个单元摊开；恒假 = 打开就是一张永远合着的树）。
    折叠态回答"课长什么样"、展开态回答"这一页有哪些分支"，两个问题不该共用一个默认值。

    项目里没有 DOM（没装 httpx/TestClient，也没有 jsdom），所以这里钉两件事：
      ① 数据面：真实课件下**一个变体都没有**（这正是"恒假"那一端，不许悄悄变化）；
      ② 代码面：首渲染的 class 是常量 'page collapsed'；自动展开只发生在 #follow 勾选时，
         且"展开"那一步排在"跟随"判断之后。
    运行期证据（headless 渲染：首次加载 .page:not(.collapsed) 计数为 0）见交付报告。
    """
    data = _tree()
    if data["ok"] is not True:
        pytest.skip("这台机器上没有真实课件（DECK_ID 未配置）")
    assert data["meta"]["variants_total"] == REAL_VARIANTS, \
        "真实课件下变体已停产：variants_total 必须是 0"
    for page in data["pages"]:
        assert not any(u["variants"] for u in page["units"]), \
            f"第 {page['index'] + 1} 页又出现了变体 —— 口径变了，默认折叠的论证要重看"

    html = asyncio.run(web.script_page()).body.decode("utf-8")
    js = html.split("<script>")[-1]          # 页面里只有一段内联脚本
    assert "sec.className = 'page collapsed';" in js, \
        "首渲染必须无条件折叠（写成常量），不许再挂条件"
    assert "(hasBranch ? '' : ' collapsed')" not in js, \
        "按 hasBranch 决定初始展开的写法不许回来 —— 它在本课件上等于默认全展开"
    # 折叠态仍要能一眼读完：页头摘要里必须带"N 单元 · M 分支"
    assert "page.units.length + ' 单元'" in js

    # 自动展开只许在「跟随当前」勾选时发生；关掉跟随，用户折起来的页不能被反复打开
    body = js.split("function applyCurrent")[1].split("function renderLivebar")[0]
    i_follow = body.index("$('follow').checked")
    i_expand = body.index("sec.classList.remove('collapsed')")
    assert i_follow < i_expand, "展开当前页这一步必须落在「跟随当前」判断之内"
    assert body.count("classList.remove('collapsed')") == 1, \
        "applyCurrent 里只该有一处展开（换页时那一次），否则折叠会被轮询反复推翻"


def test_tree_real_deck_counts_and_no_variants():
    """真实课件下 42 页 / 177 单元 / **0 变体**，分支改由 14 条预置路线承载。

    口径变更：这条用例原来钉的是 variants_total == 354、每个单元都有 v1/v2
    （旧格式的 plan.json 在加载时被内存补挂）。变体停产之后真实 plan.json 迁到新格式
    （顶层 routes、单元里没有 variants），于是 /script 这份台账的口径跟着变：
    变体列恒空；路线是**跨格**的，不挂在单个 unit 上，所以这里钉的是
    "脚本里确实带着 N 条合法路线，且每条都指向这份台账里真实存在的格"。
    """
    if web.course.mode != "deck" or web.course.plan is None:
        pytest.skip("这台机器上没有真实课件（DECK_ID 未配置）")

    data = _tree()
    assert data["ok"] is True, "真实课件下必须是 ok=True"
    meta = data["meta"]
    assert meta["total_slides"] == REAL_SLIDES
    assert meta["total_units"] == REAL_UNITS
    assert meta["variants_total"] == REAL_VARIANTS, \
        "变体已停产：台账里的变体总数必须是 0"

    pages = data["pages"]
    assert len(pages) == REAL_SLIDES
    assert [p["index"] for p in pages] == list(range(REAL_SLIDES)), \
        "页序必须是 0..41，不做任何重排"

    units = [u for p in pages for u in p["units"]]
    assert len(units) == REAL_UNITS
    for u in units:
        for key in ("unit_id", "index_in_slide", "text", "est_sec", "kp",
                    "is_page_end", "anchors_count", "ops_count", "variants"):
            assert key in u, f"{u.get('unit_id')} 少了字段 {key}"
        assert u["variants"] == [], f"{u['unit_id']} 不该再有变体（停产 = 一个都不补挂）"

    # 分支的新载体：LessonScript.routes（/script 这一页的台账要能对上脚本里那批路线）
    routes = web.course.script.routes
    assert len(routes) == REAL_ROUTES, \
        f"真实课件迁移后应有 {REAL_ROUTES} 条预置路线，实际 {len(routes)}"
    unit_ids = {u["unit_id"] for u in units}
    for route in routes:
        assert route.route_id.startswith("p-"), f"{route.route_id} 不是预置路线"
        route.validate(len(units))
        assert 0 <= route.fork_node < route.merge_node <= len(units)
        for step in route.steps:
            assert step.unit_id in unit_ids, \
                f"{route.route_id} 指向台账里不存在的格 {step.unit_id}"
    print(f"[台账] {REAL_SLIDES} 页 / {REAL_UNITS} 单元 / 变体 0 个 / 预置路线 {len(routes)} 条")


# ---------------------------------------------------------------------------
# ③ 降级：不是真实课件也要 200 + 说明
# ---------------------------------------------------------------------------

def test_tree_simple_mode_is_200_with_hint(monkeypatch):
    """手写演示课件（course.mode != deck）→ ok=false + hint，绝不抛异常。"""
    monkeypatch.setattr(web, "course", Course(script=build_script(), mode="simple"))
    data = _tree()
    assert data["ok"] is False
    assert data["hint"], "必须给出人能看懂的说明，否则前端只能显示一张白页"
    assert data["pages"] == []
    assert data["meta"]["variants_total"] == 0


def test_tree_deck_without_plan_is_200_with_hint(monkeypatch):
    """有课件但没 plan（没备过课）也是同一条降级路径 —— 半份数据比说明更容易误导人。"""
    if web.course.deck is None:
        pytest.skip("这台机器上没有真实课件")
    monkeypatch.setattr(web, "course",
                        Course(script=web.course.script, mode="deck",
                               deck=web.course.deck, plan=None))
    data = _tree()
    assert data["ok"] is False
    assert data["hint"]


# ---------------------------------------------------------------------------
# ④ 运行期叠加
# ---------------------------------------------------------------------------

def test_live_not_running(monkeypatch):
    """没有课堂在跑时只回一句 running=false —— 页面靠它决定要不要显示高亮。"""
    monkeypatch.setattr(web, "_LIVE_RUNNERS", [])
    assert _live() == {"running": False}


def test_live_reports_current_switched_routed_and_pool(monkeypatch):
    """在跑时：当前位置 / 被换成了哪一支（按 unit 归集）/ 决策记录 / 池内候选 / J 的计数。"""
    machine = _fake_machine()
    _register(monkeypatch, machine)
    data = _live()

    assert data["running"] is True
    cur = data["current"]
    assert cur["slide_index"] == 3 and cur["seg_index"] == 0
    assert cur["phase"] == "LECTURING"
    unit_id = cur["unit_id"]
    assert unit_id in machine.state.unit_progress
    assert cur["unit_progress"]["spoken"] == 2
    assert cur["unit_progress"]["total"] > 0

    rows = data["switched"][unit_id]
    assert rows[0]["variant_id"] == "v1" and rows[0]["label"] == "换说法"
    assert rows[0]["decided_at"] == "S03-U02"
    assert rows[0]["probe_before"] == "timeout"
    assert rows[0]["probe_after"] == "confirmed", \
        "probe_after 必须从 variant_replay_end 里 join 出来"

    assert data["routed"][0]["site"] == "S03-U02"
    assert data["routed"][0]["target"] == unit_id
    assert data["routed"][0]["variant_id"] == "v1"

    sources = {row["source"] for row in data["pool"]}
    assert sources == {"preset", "agent_c"}, "两种分支来源都要能看见（实线 + 虚线）"
    for row in data["pool"]:
        for key in ("candidate_id", "target_unit", "source", "status", "variant_id", "label"):
            assert key in row
        assert row["status"] in ("alive", "consumed", "expired")

    assert set(data["stats"]) == {"prefetched", "applied", "none", "timeout", "stale", "error"}


# ---------------------------------------------------------------------------
# ⑤ 零副作用（本文件最重要的一条）
# ---------------------------------------------------------------------------

def test_live_polling_has_no_side_effects(monkeypatch):
    """连续 3 次调用 /api/script/live：状态不变、池子不变、事件不减、不建 Runner、不建 task。

    为什么值得单独立一条：这个端点每秒都会被调用一次。它一旦有副作用，症状不是报错，
    而是"课堂偶尔少一次决策"或者"页尾探针记不上" —— 查的时候没人会想到是那个查看页。
    """
    machine = _fake_machine()
    monkeypatch.setattr(web, "_LIVE_RUNNERS", [types.SimpleNamespace(machine=machine)])

    class _NoNewRunner:
        def __init__(self, *a, **kw):
            raise AssertionError("观测端点不许新建 Runner（那就是第二节课堂）")

    monkeypatch.setattr(web, "Runner", _NoNewRunner)

    state, metrics, director = machine.state, machine.metrics, machine.director
    registries = list(web._LIVE_RUNNERS)

    def snapshot() -> tuple:
        return (
            state.seq, dict(state.unit_progress), state.phase,
            state.slide_index, state.seg_index,
            len(metrics.events), [e["kind"] for e in metrics.events],
            dict(director.stats), director.pool.status(),
            [c.candidate_id for c in director.pool.items()],
        )

    before = snapshot()

    async def three_polls():
        tasks_before = len(asyncio.all_tasks())
        outs = [await web.api_script_live() for _ in range(3)]
        return outs, tasks_before, len(asyncio.all_tasks())

    outs, tasks_before, tasks_after = asyncio.run(three_polls())

    assert all(o["running"] is True for o in outs)
    assert snapshot() == before, "三次只读轮询之后，状态机/池子/事件必须一字不差"
    assert tasks_after == tasks_before, "观测端点不许在事件循环里留下新 task"
    assert web._LIVE_RUNNERS == registries, "观测端点不许自己登记 Runner"


# ---------------------------------------------------------------------------
# ⑥ 池的快照语义
# ---------------------------------------------------------------------------

def test_pool_items_returns_full_snapshot():
    """items() 给全部候选，且是快照：改返回的 list 不影响池子。"""
    pool = CandidatePool()
    pool.put(_cand("c-A-v1", "A", "v1", "换说法", "preset"))
    pool.put(_cand("c-B-x1", "B", "x1", "换个例子", "agent_c"))

    items = pool.items()
    assert [c.candidate_id for c in items] == ["c-A-v1", "c-B-x1"]
    assert len(items) == len(pool.items()) == 2

    items.clear()                       # 观测面把返回的 list 扔了
    items.append("脏数据")
    assert len(pool.items()) == 2, "返回的 list 必须是独立的，池子不能被它带动"
    assert pool.alive_count() == 2
    assert pool.get("c-A-v1") is not None
    assert pool.status() == {"alive": 2, "consumed": 0, "expired": 0}
