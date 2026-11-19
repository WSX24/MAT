"""课堂走向图（GET /api/v1/units/{id}/graph）的契约测试。

为什么单开一个文件
------------------
这条接口有三处会**静默**坏掉的地方，而它们的症状都不像"接口错了"：

  ① **n 与 `script.units` 的下标必须是同一套**。差一格的话课堂照讲，只是图上那个
     指针指着别的内容 —— 契约里写死了"n == 它在 script.units 里的下标"，所以这条要
     逐格断言，而不是只数一下长度。
  ② **一条脏路线只能丢它自己**。整页被一条越界的路线带塌，前端看到的是"图白了"，
     而真正的原因（这份 plan.json 有一行手改坏了）在图上看不出来。所以既要验"坏的
     那条不见了"，也要验"好的那条还在"。
  ③ **进度指针的三档顺序**（本进程在跑 > 库里最近一条会话 > 都没有）。落空的表现是
     "课上到一半，图上没有指针"，这在测试之外几乎没人会发现。

全部离线：不联网、不调模型、不读写 decks/ 下的真实课件（临时目录里现造一份 plan.json）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

CLASSROOM = Path(__file__).resolve().parents[1]
if str(CLASSROOM) not in sys.path:              # 允许从任意 cwd 直接跑本文件
    sys.path.insert(0, str(CLASSROOM))

from course import Course                       # noqa: E402
from director.route import Route, RouteStep     # noqa: E402
from models import LessonScript, Slide          # noqa: E402
from state import ClassroomState, LectureUnit, Phase  # noqa: E402
from store import db as db_mod                  # noqa: E402
from store import repo                          # noqa: E402
from studio import graph                        # noqa: E402

#: 图只认磁盘上有没有 plan.json，所以那一步在临时目录里造（不碰仓库里的真实课件）。
DECK_ID = "走向图_测试课件"


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture()
def decks_dir(tmp_path, monkeypatch):
    """临时 decks/ 根目录 + 一份空 plan.json（= 这个单元备过课）。"""
    from deck import store as deck_store

    root = tmp_path / "decks"
    (root / DECK_ID).mkdir(parents=True)
    (root / DECK_ID / "plan.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deck_store, "DECKS_DIR", root, raising=False)
    return root


def _units() -> list[LectureUnit]:
    """4 格 / 2 页：第 0 页两格（第 2 格是页尾），第 1 页两格。"""
    return [
        LectureUnit(unit_id="s1-u1", slide=0, index_in_slide=0, kp=["K_0001"],
                    summary="函数三要素", is_page_end=False,
                    text="我们先看函数的三要素。\n第二段不该出现在卡面上。"),
        LectureUnit(unit_id="s1-u2", slide=0, index_in_slide=1, kp=["K_0002"],
                    summary="三要素缺一不可", is_page_end=True,
                    text="定义域、值域、对应关系，缺一不可。"),
        LectureUnit(unit_id="s2-u1", slide=1, index_in_slide=0, kp=["K_0003"],
                    summary="单调性", is_page_end=False,
                    text="接下来看单调性。"),
        LectureUnit(unit_id="s2-u2", slide=1, index_in_slide=1, kp=[],
                    summary="小结", is_page_end=True,
                    text="最后总结一下。"),
    ]


def _script(*routes: Route) -> LessonScript:
    return LessonScript(
        course_id="c1", title="函数复习",
        slides=[Slide(index=0, title="函数三要素"), Slide(index=1, title="单调性")],
        units=_units(), routes=list(routes),
    )


def _course(*routes: Route, mode: str = "deck", deck_id: str = DECK_ID) -> Course:
    return Course(script=_script(*routes), mode=mode,
                  deck=SimpleNamespace(deck_id=deck_id))


def _conn():
    conn = db_mod.connect()
    db_mod.migrate(conn)
    return conn


# ===========================================================================
# 一、主线节点：n 必须等于它在 script.units 里的下标
# ===========================================================================
def test_nodes_are_the_script_units_in_order(decks_dir):
    good = Route(route_id="p-skip-1-3", kind="skip", fork_node=1, merge_node=3,
                 steps=[], est_sec=-40.0, reason="第 2 格是复习，跳过不影响")
    body = graph.build_unit_graph(_course(good), unit_id="u-1",
                                  title="连通度 κ λ δ 与 Menger 定理")

    assert body["ok"] is True
    assert body["mode"] == "deck"
    assert body["hint"] == ""
    assert body["unit"] == {"id": "u-1", "title": "连通度 κ λ δ 与 Menger 定理"}

    nodes = body["nodes"]
    assert [n["n"] for n in nodes] == [0, 1, 2, 3] == list(range(len(nodes)))
    assert [n["unit_id"] for n in nodes] == ["s1-u1", "s1-u2", "s2-u1", "s2-u2"]
    # 字段名与形状冻结：前端并行开发，多一个键少一个键都不许悄悄发生
    assert set(nodes[0]) == {"n", "unit_id", "slide", "page", "page_title", "summary",
                             "preview", "est_sec", "kp", "page_end"}
    assert [n["page"] for n in nodes] == [1, 1, 2, 2]          # 1 基给界面显示
    assert nodes[0]["page_title"] == "函数三要素"
    assert nodes[2]["page_title"] == "单调性"
    assert nodes[0]["summary"] == "函数三要素"
    assert nodes[0]["preview"] == "我们先看函数的三要素。"      # 正文首段，不是整段
    assert nodes[0]["kp"] == ["K_0001"] and nodes[3]["kp"] == []
    assert [n["page_end"] for n in nodes] == [False, True, False, True]

    assert body["totals"] == {
        "nodes": 4, "routes": 1, "slides": 2,
        "est_sec": round(sum(n["est_sec"] for n in nodes), 1),
    }


def test_summary_and_preview_are_clipped(decks_dir):
    """卡面 ≤30 字、详情首段 ≤60 字：这是版面尺寸，不是文风。"""
    unit = LectureUnit(unit_id="s1-u1", slide=0, index_in_slide=0,
                       summary="总结" * 40, text="正文" * 80)
    script = LessonScript(course_id="c1", title="t",
                          slides=[Slide(index=0, title="第1页")], units=[unit])
    body = graph.build_unit_graph(
        Course(script=script, mode="deck", deck=SimpleNamespace(deck_id=DECK_ID)))
    node = body["nodes"][0]
    assert len(node["summary"]) <= 30 and node["summary"].endswith("…")
    assert len(node["preview"]) <= 60 and node["preview"].endswith("…")


# ===========================================================================
# 二、预置路线：结构越界的只丢它自己
# ===========================================================================
def test_out_of_range_route_is_dropped_and_the_others_survive(decks_dir):
    good = Route(route_id="p-skip-1-3", kind="skip", fork_node=1, merge_node=3,
                 steps=[], est_sec=-40.0, reason="第 2 格是复习，跳过不影响")
    revisit = Route(route_id="p-revisit-2", kind="revisit", fork_node=2, merge_node=3,
                    steps=[RouteStep(unit_id="s1-u2", label="换个说法", est_sec=12.0)],
                    est_sec=12.0, reason="三要素没听懂，回头再讲一遍")
    beyond = Route(route_id="p-skip-0-99", kind="skip", fork_node=0, merge_node=99,
                   steps=[], est_sec=-999.0, reason="merge 越界")
    finish_bad = Route(route_id="p-finish-0-2", kind="finish", fork_node=0, merge_node=2,
                       steps=[], est_sec=-1.0, reason="finish 的 merge 必须等于总格数")
    revisit_empty = Route(route_id="p-revisit-1", kind="revisit", fork_node=1, merge_node=3,
                          steps=[], est_sec=5.0, reason="revisit 没给 steps")

    body = graph.build_unit_graph(_course(beyond, good, finish_bad, revisit, revisit_empty))

    assert body["ok"] is True, "丢一条脏路线不该把整页带塌"
    assert [r["route_id"] for r in body["routes"]] == ["p-skip-1-3", "p-revisit-2"]
    assert body["totals"]["routes"] == 2

    skip, back = body["routes"]
    assert set(skip) == {"route_id", "kind", "label", "fork_node", "merge_node", "span",
                         "skipped", "repeats", "est_sec", "summary", "reason", "steps"}
    assert (skip["kind"], skip["label"]) == ("skip", "跳讲")
    assert (skip["fork_node"], skip["merge_node"], skip["span"]) == (1, 3, 2)
    assert skip["skipped"] == 1 and skip["repeats"] == 0
    assert skip["est_sec"] == -40.0                      # 负 = 省
    assert skip["summary"].startswith("跳讲：跳过 1 格 · 省 40 秒")
    assert len(skip["summary"]) <= 40                    # Route.summary(max_chars=40)
    assert skip["steps"] == []

    assert (back["kind"], back["label"]) == ("revisit", "补回头")
    assert back["skipped"] == 0 and back["repeats"] == 1  # revisit 不跳过，重讲 1 格
    assert back["est_sec"] == 12.0                        # 正 = 多花
    assert back["steps"] == [{"unit_id": "s1-u2", "label": "换个说法", "text": "",
                              "est_sec": 12.0}]


# ===========================================================================
# 三、降级：ok=false，形状仍然完整
# ===========================================================================
def test_simple_mode_degrades_with_a_human_hint():
    course = Course(script=_script(), mode="simple",
                    notes=["❌ PPTX_FILE 指向的文件不存在：C:/runs/x/第一课.pptx"])
    body = graph.build_unit_graph(course, unit_id="u-9", title="某单元")

    assert body["ok"] is False
    assert body["mode"] == "simple"
    assert body["nodes"] == [] and body["routes"] == []
    assert body["totals"] == {"nodes": 0, "routes": 0, "slides": 0, "est_sec": 0.0}
    assert body["unit"] == {"id": "u-9", "title": "某单元"}
    assert "先在单元页点一次「准备课件」" in body["hint"]
    assert "❌" not in body["hint"], "notes 的进度标记不该出现在人话里"
    assert "C:/runs" not in body["hint"], "本机路径要压成文件名"
    assert "第一课.pptx" in body["hint"], "文件名要留下：那是用户唯一认得出的线索"
    assert body["live"]["source"] == "none" and body["live"]["running"] is False


def test_missing_plan_json_degrades(decks_dir):
    """规则兜底出来的 plan 也算"没备过课"：那种脚本画出来是一屏空白卡片。"""
    (decks_dir / DECK_ID / "plan.json").unlink()
    body = graph.build_unit_graph(_course(), unit_id="u-1", title="函数复习")

    assert body["ok"] is False
    assert body["mode"] == "deck"            # 模式照实说：课件本身是好的
    assert "plan.json" in body["hint"]
    assert body["nodes"] == [] and body["totals"]["nodes"] == 0


def test_unassembled_course_degrades_with_the_reason():
    body = graph.build_unit_graph(None, unit_id="u-1", reason="这个单元还没登记课件文件。")
    assert body["ok"] is False
    assert body["mode"] == "simple"
    assert body["hint"] == "这个单元还没登记课件文件。"
    assert body["totals"] == {"nodes": 0, "routes": 0, "slides": 0, "est_sec": 0.0}


# ===========================================================================
# 四、进度指针三档（LIVE > 会话 > 无）
# ===========================================================================
def _fake_runner(unit_id: str = "u-1", *, said: str = "s2-u1", machine_unit: str = "s2-u1",
                 ended: bool = False):
    state = ClassroomState()
    state.slide_index = 1
    state.phase = Phase.LECTURING
    state.comprehension = {machine_unit: "confused"}
    if said:
        state.emit("teacher", "接下来看单调性。", unit_id=said)
    return SimpleNamespace(
        session={"id": "s-live", "unit_id": unit_id, "profile_id": "p1", "status": "active"},
        session_id="s-live", state=state, machine=SimpleNamespace(_cur_unit_id=lambda: machine_unit),
        _ended=ended, _closing=False,
    )


def test_live_pointer_prefers_the_running_runner(decks_dir):
    from app import ws

    ws.LIVE["s-live"] = _fake_runner()
    try:
        body = graph.build_unit_graph(_course(), live=dict(ws.LIVE), unit_id="u-1")
    finally:
        ws.LIVE.pop("s-live", None)

    live = body["live"]
    assert live["running"] is True and live["source"] == "live"
    assert live["session_id"] == "s-live"
    assert live["node"] == 2                       # 最后一句 utterance 在 s2-u1（第 2 格）
    assert live["slide"] == 1
    assert live["phase"] == "LECTURING"
    assert live["comprehension"] == 0.0          # confused → 0；confirmed 1 / vague 0.5


def test_live_pointer_falls_back_to_the_machine(decks_dir):
    """最后一句是学生提问（没有 unit_id）时，退回去问状态机自己在哪一格。"""
    from app import ws

    ws.LIVE["s-live"] = _fake_runner(said="", machine_unit="s1-u2")
    try:
        body = graph.build_unit_graph(_course(), live=dict(ws.LIVE), unit_id="u-1")
    finally:
        ws.LIVE.pop("s-live", None)
    assert body["live"]["source"] == "live"
    assert body["live"]["node"] == 1


def test_finished_runner_is_not_a_live_pointer(decks_dir):
    """已收尾的运行器还在 LIVE 里（等 WS 收尾）时不能当成"正在上"。"""
    from app import ws

    ws.LIVE["s-live"] = _fake_runner(ended=True)
    try:
        body = graph.build_unit_graph(_course(), live=dict(ws.LIVE), unit_id="u-1")
    finally:
        ws.LIVE.pop("s-live", None)
    assert body["live"]["source"] == "none"


def test_progress_pointer_comes_from_the_latest_session(decks_dir):
    conn = _conn()
    try:
        repo.create_session(conn, "u-1", "p1", position_slide=1, position_unit=1)
        body = graph.build_unit_graph(_course(), conn=conn, profile_id="p1", unit_id="u-1")
    finally:
        conn.close()

    live = body["live"]
    assert live["running"] is False and live["source"] == "progress"
    assert live["session_id"].startswith("s-")
    assert live["slide"] == 1
    # position_unit 是段序号不是格号：这一页的**第一格**才是上次停的地方
    assert live["node"] == 2
    assert live["phase"] == "active"
    assert live["comprehension"] is None


def test_no_session_and_no_runner_gives_source_none(decks_dir):
    body = graph.build_unit_graph(_course(), live={}, conn=None, unit_id="u-1")
    assert body["live"] == {"running": False, "source": "none", "session_id": "",
                           "node": None, "slide": None, "phase": "",
                           "comprehension": None}


# ===========================================================================
# 五、接口本身：注册了、404 是中文、降级也 200
# ===========================================================================
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "classroom.sqlite"))
    from app import deps

    monkeypatch.setattr(deps, "STATE_ROOT", tmp_path / "state", raising=False)
    monkeypatch.setattr(deps, "DIST_ROOT", tmp_path / "dist", raising=False)
    deps.reset_settings_cache()
    from app import create_app

    with TestClient(create_app()) as c:
        yield c
    deps.reset_settings_cache()


def _seed_unit(pptx: Path) -> None:
    conn = _conn()
    try:
        repo.upsert_course(conn, {"id": "c1", "title": "测试课程", "subject": "数学",
                                  "source": "run", "run_dir": "/tmp/run1"})
        repo.upsert_unit(conn, {"id": "u-1", "course_id": "c1", "title": "第 1 单元",
                                "pptx_path": str(pptx), "order_index": 0})
    finally:
        conn.close()


def test_graph_endpoint_is_registered_and_degrades_to_200(client, tmp_path):
    _seed_unit(tmp_path / "没有这个文件.pptx")
    resp = client.get("/api/v1/units/u-1/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["hint"]
    assert body["unit"] == {"id": "u-1", "title": "第 1 单元"}
    assert body["nodes"] == [] and body["routes"] == []
    assert body["totals"] == {"nodes": 0, "routes": 0, "slides": 0, "est_sec": 0.0}
    assert body["live"]["source"] == "none"
    # 硬约束：这条接口会被轮询，它一个字节都不许写进库（不建会话、不记事件）
    conn = _conn()
    try:
        counts = (conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    finally:
        conn.close()
    assert counts == (0, 0), "走向图必须零副作用"


def test_graph_endpoint_404s_for_an_unknown_unit(client):
    resp = client.get("/api/v1/units/u-nope/graph")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False
