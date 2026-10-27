"""live 载荷里的路线几何（/api/script/live 的 route 子对象）的测试。

为什么单开一个文件
------------------
S4 这一棒只改**观测面**的一侧：live 候选行多出一个 route 子对象，前端按真实跨度画。
所以这里要钉的不是"能不能显示路线"，而是四件会静默出事的事：

  1. **路线行带全几何**：走向、岔出/汇回格、steps、时长、理由 —— 少一个字段，
     前端就退回"把每条路线都画成汇回下一格"的那个小菱形，而页面看起来完全正常。
  2. **变体行一个字节都不许多**：路线与变体共用同一张表，给变体行顺手补一个
     "route": null 也能跑，但那样"这一行是跨格走向还是就地重讲"就只能靠判空值，
     而不是靠键的有无。
  3. **空池时形状不变**：课堂没在跑的时候这一页必须和改动前一模一样。
  4. **零副作用**：这个端点每秒被调一次，它一旦顺手改一笔池子/状态机，
     课堂那边就少一次决策，而这一页看起来一切正常。

调用方式沿用 tests/test_script_tree.py：项目没装 httpx（fastapi.testclient 用不了），
而端点本来就是纯 async 函数，asyncio.run(web.api_xxx()) 直接 await 更快，
也更容易把"没有新建 Runner""返回的 steps 是副本"这类断言写死。
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
from director.decider import Director, RuleDecider  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.route import Route, RouteStep, derive_route_id  # noqa: E402
from eval import MetricsTracker  # noqa: E402
from state import ClassroomState, Phase  # noqa: E402

GRAPH_HTML = Path(__file__).resolve().parent.parent / "web" / "graph.html"

#: route 子对象的**精确**字段集合。写成精确相等而不是"包含"：多一个键就是
#: 契约变了（前端要为它承担一个恒为空的字段），少一个键就是画不出真实跨度。
ROUTE_KEYS = {"route_id", "kind", "fork_node", "merge_node", "steps", "est_sec", "reason"}
#: 路线一步的字段（照抄 RouteStep：variant_id/text 空 = 用该单元主 text）。
STEP_KEYS = {"unit_id", "variant_id", "label", "text", "est_sec"}
#: **改动前** live 候选行的字段。变体行必须逐字还是这六个键。
ROW_KEYS = {"candidate_id", "target_unit", "source", "status", "variant_id", "label"}
#: live 载荷的顶层字段（空池时也必须一字不差）。
PAYLOAD_KEYS = {"running", "current", "switched", "routed", "pool", "pool_total", "stats"}

#: 课程总格数：Route.validate 与 "finish 汇回课尾" 都拿它当尺子。
TOTAL = len(web.script.units)
UNITS = [u.unit_id for u in web.script.units]


def _json(res) -> dict:
    """端点可能直接返回 dict，也可能用 JSONResponse（老端点就是这么写的）。"""
    if isinstance(res, JSONResponse):
        return json.loads(res.body.decode("utf-8"))
    return res


def _live() -> dict:
    return _json(asyncio.run(web.api_script_live()))


def _variant_cand(cid: str, unit: str, vid: str, label: str, source: str) -> Candidate:
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


def _route_cand(cid: str, route: Route, unit: str, *, source: str = "agent_c") -> Candidate:
    return Candidate(
        candidate_id=cid,
        target_unit=unit,
        route=route,
        when={},
        source=source,
        created_at=1.0,
        created_at_unit=unit,
        expires_at_seq=0,
    )


def _skip_route() -> Route:
    """跳讲：从 #4 岔出、跳过 2 格（#5 #6）、在 #7 汇回（est_sec 负数 = 省时间）。"""
    return Route(
        route_id=derive_route_id("skip", 4, 7),
        kind="skip",
        fork_node=4,
        merge_node=7,
        steps=[],
        est_sec=-12.5,
        reason="这一页学生已经会了，直接跳过",
    )


def _revisit_route() -> Route:
    """补回头：从 #8 岔出、把前面两格重讲一遍、在 #11 汇回（est_sec 正数 = 多花时间）。"""
    return Route(
        route_id=derive_route_id("revisit", 8, 11),
        kind="revisit",
        fork_node=8,
        merge_node=11,
        steps=[
            RouteStep(unit_id=UNITS[4], variant_id="v2", label="举例子", text="再举个例子。",
                      est_sec=6.0),
            RouteStep(unit_id=UNITS[5]),          # 全空：用该单元主 text 再讲一遍
        ],
        est_sec=14.0,
        reason="刚才那格探针没确认，补回头再讲一次",
    )


def _finish_route() -> Route:
    """提前收尾：从 #12 直接跳到课尾（merge == 总格数）。"""
    return Route(
        route_id=derive_route_id("finish", 12, TOTAL),
        kind="finish",
        fork_node=12,
        merge_node=TOTAL,
        steps=[],
        est_sec=-90.0,
        reason="时间不够了，讲到这里就下课",
    )


def _machine(pool: CandidatePool, *, slide_index: int = 3, seg_index: int = 0):
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
    return types.SimpleNamespace(
        state=state, metrics=metrics, director=Director(pool, RuleDecider()),
        script=web.script)


def _register(monkeypatch, machine) -> None:
    monkeypatch.setattr(web, "_LIVE_RUNNERS", [types.SimpleNamespace(machine=machine)])


def _route_pool() -> CandidatePool:
    """一格预置变体 + 三条路线（skip / revisit / finish）+ 一条已消费的路线。

    CAP=5：alive 恰好 4 条，第 5 条已消费 —— 顺带钉住"alive 全给、作废的垫在后面"
    这条池纪律没有被新字段弄坏。
    """
    pool = CandidatePool()
    pool.put(_variant_cand("c-preset-v1", UNITS[4], "v1", "换说法", "preset"))
    pool.put(_route_cand("c-route-skip", _skip_route(), UNITS[4]))
    pool.put(_route_cand("c-route-revisit", _revisit_route(), UNITS[8]))
    pool.put(_route_cand("c-route-finish", _finish_route(), UNITS[12]))
    pool.put(_route_cand("c-route-done", _skip_route(), UNITS[4]))
    pool.mark_consumed("c-route-done")
    return pool


# ---------------------------------------------------------------------------
# ① 路线行：几何逐字段
# ---------------------------------------------------------------------------

def test_live_route_row_carries_full_route_geometry(monkeypatch):
    """三条走向各自带上 route_id / kind / fork_node / merge_node / steps / est_sec / reason。

    这一条钉的是"前端拿得到真实跨度"：merge_node 一旦缺失或恒为 fork_node+1，
    图上的 skip / revisit / finish 就又变回同一个汇回下一格的小菱形。
    """
    _register(monkeypatch, _machine(_route_pool()))
    data = _live()

    rows = {r["candidate_id"]: r for r in data["pool"]}
    assert data["pool_total"] == 5

    # -- skip：不重讲任何一格，merge_node 就是汇回的那一格
    skip_row = rows["c-route-skip"]
    assert ROW_KEYS <= set(skip_row), "路线行不许弄丢任何既有字段"
    rt = skip_row["route"]
    assert set(rt) == ROUTE_KEYS
    assert rt["route_id"] == derive_route_id("skip", 4, 7) == "r-skip-4-3"
    assert rt["kind"] == "skip"
    assert rt["fork_node"] == 4 and rt["merge_node"] == 7
    assert rt["steps"] == [], "skip 只是跳过若干格，不重讲任何一格"
    assert rt["est_sec"] == -12.5 and rt["est_sec"] < 0, "跳讲的时长必须记成'省时间'（负数）"
    assert rt["reason"] == "这一页学生已经会了，直接跳过"
    # 路线行没有变体载荷：variant_id / label 保持空串，语义没有变
    assert skip_row["variant_id"] == "" and skip_row["label"] == ""
    assert skip_row["source"] == "agent_c" and skip_row["status"] == "alive"

    # -- revisit：steps 的**顺序与长度**都是信息（重讲哪几格、重讲几格）
    rev = rows["c-route-revisit"]["route"]
    assert rev["kind"] == "revisit" and rev["fork_node"] == 8 and rev["merge_node"] == 11
    assert len(rev["steps"]) == 2, "revisit 的 steps 长度就是'重讲几格'"
    assert [s["unit_id"] for s in rev["steps"]] == [UNITS[4], UNITS[5]]
    for step in rev["steps"]:
        assert set(step) == STEP_KEYS
    assert rev["steps"][0]["variant_id"] == "v2" and rev["steps"][0]["est_sec"] == 6.0
    assert rev["steps"][1]["variant_id"] == "" and rev["steps"][1]["text"] == "", \
        "空 variant_id/text = 用该单元主 text 再讲一遍（RouteStep 的既有语义）"
    assert rev["est_sec"] == 14.0 and rev["est_sec"] > 0, "补回头多花时间（正数）"

    # -- finish：merge_node 就是课程末尾，图上前端据此画「退出课堂」
    fin = rows["c-route-finish"]["route"]
    assert fin["kind"] == "finish" and fin["fork_node"] == 12
    assert fin["merge_node"] == TOTAL, "提前收尾必须汇回课尾（Route.validate 的同一条）"
    assert fin["steps"] == []

    # -- 载荷里的 route 必须仍是一条**合法**的路由：拿冻结契约反解再序列化，一字不差
    for cid in ("c-route-skip", "c-route-revisit", "c-route-finish"):
        route = rows[cid]["route"]
        back = Route.from_dict(route)
        back.validate(TOTAL)          # 越界 / revisit 空 steps / finish 不落课尾都会炸
        assert back.to_dict() == route, "载荷是 Route 的忠实投影，不是另造的一套结构"
        assert route["route_id"] == derive_route_id(
            route["kind"], route["fork_node"], route["merge_node"]), \
            "route_id 必须仍是按冻结约定派生的幂等键"

    # -- 全程 JSON 可序列化（前端拿到的就是这一份）
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data


def test_live_route_rows_keep_the_alive_first_pool_discipline(monkeypatch):
    """路线行也照旧享受池纪律：alive 全给、作废的垫在后面，不因为多了几何就先占位。"""
    _register(monkeypatch, _machine(_route_pool()))
    pool = _live()["pool"]

    statuses = [r["status"] for r in pool]
    first_dead = next((k for k, s in enumerate(statuses) if s != "alive"), len(statuses))
    assert all(s == "alive" for s in statuses[:first_dead]), "alive 必须排在前面"
    assert statuses[first_dead:] == ["consumed"]
    assert pool[first_dead]["route"]["kind"] == "skip", "作废的路线行同样带着几何"


# ---------------------------------------------------------------------------
# ② 变体行：逐字回归（多一个键就是契约变了）
# ---------------------------------------------------------------------------

def test_live_variant_rows_are_byte_identical_to_before(monkeypatch):
    """只有变体候选时，pool 行必须**逐字**等于改动前的那份结构（六个键，没有 route）。

    为什么值得单钉一条：这条端点是前端唯一的运行期输入，而"顺手给每行补一个
    route: null"是最像进步的那种改动 —— 它让所有行长得一样，代价是前端再也
    无法用键的有无区分"跨格走向"和"就地重讲"。
    """
    pool = CandidatePool()
    pool.put(_variant_cand("c-p-1", UNITS[1], "v1", "换说法", "preset"))
    pool.put(_variant_cand("c-a-1", UNITS[8], "x1", "换个例子", "agent_c"))
    _register(monkeypatch, _machine(pool))

    assert _live()["pool"] == [
        {"candidate_id": "c-p-1", "target_unit": UNITS[1], "source": "preset",
         "status": "alive", "variant_id": "v1", "label": "换说法"},
        {"candidate_id": "c-a-1", "target_unit": UNITS[8], "source": "agent_c",
         "status": "alive", "variant_id": "x1", "label": "换个例子"},
    ]


def test_live_route_row_accepts_dict_form_route(monkeypatch):
    """route 是 dict（JSON 反解回来的那一形态）时，载荷同样给全几何。

    为什么值得钉：Route 从 JSON 回填时是 dict，而观测面只要有一处写死 getattr，
    这条路线在图上就变成"跳讲 0 格"的小菱形 —— 不报错，只是画错了。
    """
    pool = CandidatePool()
    pool.put(Candidate(
        candidate_id="c-route-dict", target_unit=UNITS[2],
        route={"route_id": "r-skip-2-3", "kind": "skip", "fork_node": 2, "merge_node": 5,
               "steps": [], "est_sec": -7.0, "reason": "dict 形态"},
        when={}, source="agent_c", created_at=1.0, created_at_unit=UNITS[2],
        expires_at_seq=0,
    ))
    _register(monkeypatch, _machine(pool))

    route = _live()["pool"][0]["route"]
    assert route == {"route_id": "r-skip-2-3", "kind": "skip", "fork_node": 2,
                     "merge_node": 5, "steps": [], "est_sec": -7.0, "reason": "dict 形态"}


def test_live_mixed_pool_only_route_rows_grow_a_route_key(monkeypatch):
    """同一份载荷里：路线行有 route 键，变体行没有 —— 键的有无就是两种载荷的判据。"""
    _register(monkeypatch, _machine(_route_pool()))
    pool = _live()["pool"]

    with_route = [r for r in pool if "route" in r]
    without_route = [r for r in pool if "route" not in r]
    assert len(with_route) == 4 and len(without_route) == 1
    assert {r["candidate_id"] for r in without_route} == {"c-preset-v1"}
    for row in without_route:
        assert set(row) == ROW_KEYS
    for row in with_route:
        assert set(row) == ROW_KEYS | {"route"}, "路线行 = 既有六字段 + route，不多不少"


# ---------------------------------------------------------------------------
# ③ 空池：形状不变
# ---------------------------------------------------------------------------

def test_live_payload_shape_is_unchanged_with_empty_pool(monkeypatch):
    """课堂在跑但池子空着：顶层字段与 pool/pool_total 必须和改动前一模一样。

    "没在跑课堂时页面和现在一模一样"是这一棒的硬约束之一，而空池是它最容易
    被悄悄破的一档（顺手加一个顶层 routes 列表就破了）。
    """
    _register(monkeypatch, _machine(CandidatePool()))
    data = _live()

    assert set(data) == PAYLOAD_KEYS
    assert data["pool"] == [] and data["pool_total"] == 0
    assert set(data["current"]) == {"slide_index", "seg_index", "phase", "unit_id",
                                    "unit_progress"}
    assert data["switched"] == {}
    # routed 行的字段也是既有契约：路线只走 pool，不许顺手在这里加一列
    assert len(data["routed"]) == 1
    assert set(data["routed"][0]) == {"site", "target", "candidate_id", "variant_id", "at"}
    # 用 '"route":' 而不是 'route'：载荷里本来就有 routed 这个键名
    assert '"route":' not in json.dumps(data, ensure_ascii=False), \
        "池子空着的时候载荷里不该出现任何 route 子对象"


# ---------------------------------------------------------------------------
# ④ 零副作用（本文件最重要的一条）
# ---------------------------------------------------------------------------

def test_live_route_payload_has_no_side_effects(monkeypatch):
    """连续 3 次拉 live：状态/事件/池子一字不差，不建 Runner，返回的 steps 是副本。

    为什么"steps 是副本"值得单独断言：观测面如果把池里那条 Route 的 steps 列表
    原样（同一个 list）放进载荷，前端随手 push 一下就改了课堂正在执行的路线 ——
    下次执行就会多走一步，而没有任何日志会显示是谁改的。
    """
    pool = _route_pool()
    machine = _machine(pool)
    _register(monkeypatch, machine)

    class _NoNewRunner:
        def __init__(self, *a, **kw):
            raise AssertionError("观测端点不许新建 Runner（那就是第二节课堂）")

    monkeypatch.setattr(web, "Runner", _NoNewRunner)

    state, metrics, director = machine.state, machine.metrics, machine.director
    registries = list(web._LIVE_RUNNERS)
    revisit = pool.get("c-route-revisit").route

    def snapshot() -> tuple:
        return (
            state.seq, dict(state.unit_progress), state.phase,
            state.slide_index, state.seg_index,
            len(metrics.events), [e["kind"] for e in metrics.events],
            dict(director.stats), pool.status(),
            [(c.candidate_id, c.status, c.version) for c in pool.items()],
            [(s.unit_id, s.variant_id, s.est_sec) for s in revisit.steps],
        )

    before = snapshot()

    async def three_polls():
        tasks_before = len(asyncio.all_tasks())
        outs = [await web.api_script_live() for _ in range(3)]
        return outs, tasks_before, len(asyncio.all_tasks())

    outs, tasks_before, tasks_after = asyncio.run(three_polls())

    assert all(o["running"] is True for o in outs)
    assert all(len(o["pool"]) == 5 for o in outs)
    assert snapshot() == before, "三次只读轮询之后，状态机/池子/事件/路线结构必须一字不差"
    assert tasks_after == tasks_before, "观测端点不许在事件循环里留下新 task"
    assert web._LIVE_RUNNERS == registries, "观测端点不许自己登记 Runner"

    # 改载荷里的 steps 不许改到池子里那条 Route 上（前端随手 push 的那一下）
    outs[0]["pool"][1]["route"]["steps"].append({"unit_id": "S99-U99"})
    outs[0]["pool"][1]["route"]["steps"][0]["label"] = "被观测面改掉的标签"
    assert [s.unit_id for s in revisit.steps] == [UNITS[4], UNITS[5]]
    assert revisit.steps[0].label == "举例子", \
        "载荷是快照：前端改它不该改到课堂正在执行的那条路线"


# ---------------------------------------------------------------------------
# ⑤ 前端渲染路径：跨度不再写死成"汇回下一格"
# ---------------------------------------------------------------------------

def test_graph_page_draws_route_span_from_payload():
    """图页必须按载荷里的 fork/merge 画，且 chip 文案与详情能读出走向与 steps。

    这条是源级断言（读 graph.html 而不是跑浏览器）：它守的是**那一行**——
    改动前 kind 为 cand 时一律把 merge 算成 i 加 1，所有路线因此都是小菱形。
    值不了多少次运行，但改了它就等于把路线又画回同一个点。
    """
    if not GRAPH_HTML.is_file():
        pytest.skip("web/graph.html 还没落地")

    html = GRAPH_HTML.read_text(encoding="utf-8")

    # ① 硬编码那一行必须消失，改成读载荷
    assert "else merge = (i + 1 < S.nodes.length) ? (i + 1) : -1;" not in html, \
        "运行期候选的汇回点不许再写死成'下一格'"
    assert "else merge = candMerge(b, i);" in html, "候选行的 merge 必须来自 candMerge"
    assert "Number(rt.merge_node)" in html, "candMerge 的跨度必须来自载荷的 merge_node"
    assert "Number(rt.fork_node)" in html, "岔出格同样以载荷的 fork_node 为准"

    # ② 画布上要能一眼分出三种走向
    for text in ("跳讲", "补回头", "提前收尾"):
        assert text in html, "chip 文案必须能一眼看出走向：" + text
    assert "提前收尾 → 课尾" in html
    assert "rt-' + rkind" in html, "走向 class 由 kind 拼出来（rt-skip / rt-revisit / rt-finish）"

    # ②' chip 上那个格数是**真跳过**的格数（跨度 - 1），不是跨度
    #     从 #8 岔出、在 #11 汇回，中间真正没讲的是两格；按跨度写会每一格都多算一格。
    #     Route.summary 是同一个口径（那里写死"跳过 {merge - fork - 1} 格"）。
    assert "function routeSkip(rt, i)" in html, "跳过格数必须由 routeSkip 单独算（口径只此一处）"
    assert "Math.max(0, routeSpan(rt, i) - 1)" in html, "真跳过 = 跨度 - 1"
    assert "routeKindText(rt) + ' ' + routeSkip(rt, i) + ' 格'" in html, \
        "skip 的 chip 必须报真跳过格数，不许拿跨度冒充"
    assert "routeKindText(rt) + ' ' + routeSpan(rt, i) + ' 格'" not in html, \
        "拿跨度当跳过格数就是这次修掉的那个口径错误"

    # ②'' 格数**按走向分开报**（这一条修的是文案噪声）：revisit 只报「重讲 N 格」，
    #      skip / finish 只报「跳过 N 格」。
    #      "补回头 1 格（跳过 0 格）· 重讲 2 格"里那个"跳过 0 格"是纯噪声 ——
    #      补回头本来就不跳过任何一格，写上它只会让详情读起来像"这一支又跳又重讲"。
    assert "function routeCountText(rt, i)" in html, "格数文案必须由 routeCountText 一处产出"
    assert "return '重讲 ' + n + ' 格';" in html, "revisit 只报重讲了几格"
    assert "return '跳过 ' + routeSkip(rt, i) + ' 格';" in html, "skip / finish 报真跳过格数"
    assert "' 格（跳过 ' + routeSkip(rt, i) + ' 格）'" not in html, \
        "详情里那句『（跳过 0 格）』必须消失"
    assert "kv('跨格', routeSpan(rt, i) + ' 格 · ' + routeCountText(rt, i))" in html, \
        "详情面板：跨度 + 按走向分开的格数"
    assert "'（' + routeCountText(rt, i) + ' / 跨 ' + routeSpan(rt, i) + ' 格'" in html, \
        "tooltip 也按同一条规则报数（『跳过 0 格』在那里同样是噪声）"
    #     chip 第一行一个字都没动：仍然 clip 到 9 个字，revisit 仍是"补回头 N 格"
    assert "clip(routeTag(rt, i), 9)" in html, "chip 的 9 字约束与文案不许被这次改动碰"

    # ③ 详情面板要列出 steps 与理由
    assert "steps · 按顺序走这 " in html, "详情面板必须列出 steps 的单元序列"
    assert "kv('理由', rt.reason" in html, "详情面板必须给出 reason"

    # ④ 计数区把路线候选与变体候选分开数
    assert 'id="cnRoute"' in html and 'id="cnVar"' in html
    assert "if (p.route)" in html, "计数要按载荷区分路线/变体，而不是一锅数"

    # ⑤ 跨 3 格的路线 chip 会落在别的候选头上：撞了要顺延泳道，不许叠成一团
    assert "function pickCandLane(" in html
    assert "function candLaneFree(" in html


# ---------------------------------------------------------------------------
# ⑥ 前端：运行期路线与静态预置路线共用一套渲染
# ---------------------------------------------------------------------------

def test_graph_page_renders_runtime_and_preset_routes_through_one_function():
    """运行期路线（live 行的 route）与静态预置路线走同一个 buildRouteChip。

    为什么这条要留在本文件：静态 routes 是**第二条**喂给同一个渲染函数的来源。
    它一旦另起一条路径，两个来源的几何就会漂移 —— 而它们本来就是同一种东西，
    只是"课前预置"与"课上现造"的区别；漂移之后没人能说清哪一套是对的。
    """
    if not GRAPH_HTML.is_file():
        pytest.skip("web/graph.html 还没落地")

    html = GRAPH_HTML.read_text(encoding="utf-8")

    # ① 候选行不再自己拼路线 chip：有 route 就交给 buildRouteChip
    assert "const r = p.route" in html
    assert "buildRouteChip(p.route, fi, useLane)" in html
    # ② 静态载荷同样交给它（另一条路只画旧计划的就地变体）
    assert "buildRouteChip(rt, i, presetRouteLane(i))" in html
    assert html.count("function buildRouteChip(") == 1, "路线渲染只许有一个函数"
    # ③ 路线几何不再绑在"候选行"这一档上：b.route 有值就按路线画
    assert "const rt = b.route || null;" in html
    assert "const rt = (kind === 'cand' && b.route) ? b.route : null;" not in html, \
        "路线渲染不许再只对 kind === 'cand' 生效 —— 预置路线也是路线"
    # ④ 同一格上"预置一条 + 运行时又造一条"不许叠成一张：泳道分配要看得见预置那几条
    assert "S.routeGeom.forEach(function (g) {" in html, \
        "挑候选泳道时必须避开预置路线——否则同 fork 的两条 chip 会叠在一起"
    assert "const rel = S.routeEls.get(p.route.route_id || '');" in html, \
        "池里的预置路线只更新静态 chip 的三态，不再另画一条虚线"
