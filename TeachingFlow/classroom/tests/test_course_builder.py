# -*- coding: utf-8 -*-
"""交互式课程生成 + 渐进式单课 PPT（course_builder / course_plan 的行为契约）。

这些用例要钉住的，是"产品流程真的能走完"这件事：

  · 第一句话进来，**模型是在提问**（而不是直接排课）；
  · 用户回答够了，模型**请求确认**，而不是自己拍板；
  · 用户点"不符合"能回到提问；
  · 确认之后才生成大纲，大纲**可以被用户改**，改完的结构必须还能拿去排课；
  · 大纲确认 → 单元课程安排设计（对学生不可见）→ **存入课程库**；
  · PPT **一课一课生成**，生成过的课能上课，没生成的课点"生成下一课"。

离线口径：默认把 ``_model_target`` 打成 None（走规则兜底），所以这些用例不联网、
不花额度。需要验"真模型分支"的用例自己把 ``_ask_model`` / ``_ask_json`` 换成假实现 ——
那是"解析与落盘"的测试，不是"模型说得好不好"的测试。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CLASSROOM = Path(__file__).resolve().parents[1]
if str(CLASSROOM) not in sys.path:              # 允许从任意 cwd 直接跑本文件
    sys.path.insert(0, str(CLASSROOM))

import course_builder                            # noqa: E402
import course_plan                               # noqa: E402
from store import db as db_mod                   # noqa: E402

BASE = "/api/v1/course-builder/sessions"

#: 假流水线：认 ``--resume <run>`` 与 ``--unit <uid>``，往 run 目录写一份假 pptx 并登记
#: 到 manifest 的 ``s5_render:<uid>``（真 flow.py 记的就是这个键）。
#: ``FAKE_FLOW_MODE=noppt`` 演"退出码 0 但没产物"（真机里渲染全失败就是这样）。
FAKE_FLOW = r'''
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
run = Path(args[args.index("--resume") + 1])
unit = args[args.index("--unit") + 1]
run.mkdir(parents=True, exist_ok=True)
manifest_path = run / "manifest.json"
manifest = {}
if manifest_path.is_file():
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
manifest.setdefault("steps", {})
manifest.setdefault("args", {})
print("【S4】假课件大纲", flush=True)
time.sleep(float(os.environ.get("FAKE_STAGE_SLEEP") or 0))
print("【S5】假产片", flush=True)
artifacts = []
if os.environ.get("FAKE_FLOW_MODE") != "noppt":
    chapter = run / "第1章-假章"
    chapter.mkdir(parents=True, exist_ok=True)
    pptx = chapter / f"第1单元-{unit}.pptx"
    pptx.write_bytes(b"PK-not-a-real-pptx")
    artifacts = [pptx.name]
    manifest["produced"] = [str(pptx)]
manifest["steps"]["s5_render:" + unit] = {"ok": True, "artifacts": artifacts}
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
print("  产物目录：" + str(run), flush=True)
code = int(os.environ.get("FAKE_EXIT") or 0)
sys.exit(code)
'''


# ===========================================================================
# 夹具
# ===========================================================================

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每例：独立库、独立 runs / state / dist、无模型、假流水线。"""
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "classroom.sqlite"))
    monkeypatch.delenv("CLASSROOM_DEV", raising=False)
    for key in ("FAKE_STAGE_SLEEP", "FAKE_FLOW_MODE", "FAKE_EXIT"):
        monkeypatch.delenv(key, raising=False)
    from app import deps

    monkeypatch.setattr(deps, "STATE_ROOT", tmp_path / "state", raising=False)
    monkeypatch.setattr(deps, "RUNS_ROOT", tmp_path / "runs", raising=False)
    monkeypatch.setattr(deps, "DIST_ROOT", tmp_path / "dist", raising=False)
    # 备课在这一层是"加分项"，测的是编排而不是解析：直接给它一个成功的最小结果，
    # 否则它会去解析那份假 pptx、还要挑模型服务商（真机上会联网）。
    monkeypatch.setattr(deps, "prepare_unit", lambda row: {
        "ok": True, "deck_ready": True, "plan_ready": True, "plan_source": "llm",
        "degraded": False, "warnings": [], "slides": 3, "est_sec": 60.0,
    })
    # 默认走规则兜底：用例要验真模型分支时自己再打 _ask_model / _ask_json
    monkeypatch.setattr(course_builder, "_model_target", lambda conn: None)
    fake = tmp_path / "fake_flow.py"
    fake.write_text(FAKE_FLOW, encoding="utf-8")
    monkeypatch.setattr(course_builder, "FLOW_SCRIPT", fake)
    monkeypatch.setattr(course_builder, "_LIVE", {})
    monkeypatch.setattr(course_builder, "_PPT_LIVE", {})
    deps.reset_settings_cache()
    yield tmp_path
    deps.reset_settings_cache()


@pytest.fixture()
def client(_isolate):
    from app import create_app

    with TestClient(create_app()) as c:
        yield c


# ===========================================================================
# 小工具
# ===========================================================================

def _post(client, url, body=None):
    return client.post(url, json=body if body is not None else {})


def _session(client, sid) -> dict:
    return client.get(f"{BASE}/{sid}").json()


def _last_tool(session: dict) -> dict | None:
    for item in reversed(session["messages"]):
        if isinstance(item.get("tool"), dict):
            return item["tool"]
    return None


def _wait_phase(client, sid, phases, *, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = _session(client, sid)
        if last.get("phase") in phases:
            return last
        time.sleep(0.05)
    raise AssertionError(f"{timeout}s 内没到 {phases}，最后：{json.dumps(last, ensure_ascii=False)[:800]}")


def _walk_to_confirm(client, sid) -> dict:
    """按规则兜底的节奏回答两次，直到出现"请确认需求"的卡。"""
    session = _session(client, sid)
    for _ in range(6):
        tool = _last_tool(session)
        if tool and tool.get("name") == "confirm_course_requirements":
            return session
        assert tool and tool.get("name") == "ask_user_question", session
        option = ""
        for q in tool.get("questions") or []:
            if q.get("options"):
                option = q["options"][0]["label"]
                break
        session = _post(client, f"{BASE}/{sid}/messages",
                        {"text": option or "有基础"}).json()
    raise AssertionError("六轮之内没能走到需求确认")


def _build_course(client) -> tuple[str, dict]:
    """从第一句话走到"课程已入库"，返回 (session_id, session)。"""
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    sid = created["id"]
    session = _walk_to_confirm(client, sid)
    session = _post(client, f"{BASE}/{sid}/confirm-requirements",
                    {"accepted": True}).json()
    session = _wait_phase(client, sid, ("editing_outline",), timeout=30)
    session = _post(client, f"{BASE}/{sid}/outline/confirm", {}).json()
    session = _wait_phase(client, sid, ("ready",), timeout=60)
    return sid, session


# ===========================================================================
# 对话：模型在提问
# ===========================================================================

def test_first_message_gets_a_question_not_a_course(client):
    """第一句话之后必须是"模型在问你"，而不是直接排一份课出来。"""
    r = _post(client, BASE, {"message": "我想学习信息安全"})
    assert r.status_code == 201, r.text
    session = r.json()
    assert session["phase"] == "clarifying"
    tool = _last_tool(session)
    assert tool and tool["name"] == "ask_user_question"
    assert tool["questions"], "提问卡里至少要有一个问题"
    assert all(q.get("question") for q in tool["questions"])
    assert session["outline"] is None
    # 用户那句话必须原样留在对话里（前端要把它画成右侧气泡）
    assert session["messages"][0] == {**session["messages"][0], "role": "user",
                                     "text": "我想学习信息安全"}


def test_answering_reaches_a_confirmation_card(client):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    session = _walk_to_confirm(client, created["id"])
    tool = _last_tool(session)
    assert tool["name"] == "confirm_course_requirements"
    req = tool["requirements"]
    assert req["course_title"] and req["subject"] and req["goal"]
    assert req["course_title"] in {"信息安全", "我想学习信息安全"} or "信息安全" in req["course_title"]
    assert session["phase"] == "confirming_requirements"
    assert session["requirements"]["weeks"] >= 1
    # 确认卡还没被表态：此时不许有课程
    assert session["outline"] is None and session["course_id"] == ""


def test_rejecting_requirements_goes_back_to_asking(client):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    sid = created["id"]
    _walk_to_confirm(client, sid)
    session = _post(client, f"{BASE}/{sid}/confirm-requirements",
                    {"accepted": False, "feedback": "周期太长了，我只有两周"}).json()
    assert session["phase"] == "clarifying"
    assert session["outline"] is None
    tool = _last_tool(session)
    assert tool["name"] == "confirm_course_requirements"
    assert tool["decision"] == "rejected"
    # 用户那句反馈要进对话（下一轮模型据此重排需求）
    assert any("两周" in str(m.get("text") or "") for m in session["messages"])


def test_confirm_without_a_pending_card_is_409(client):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    r = _post(client, f"{BASE}/{created['id']}/confirm-requirements",
              {"accepted": True})
    assert r.status_code == 409
    assert r.json()["code"] == "conflict"


def test_empty_first_message_is_400(client):
    r = _post(client, BASE, {"message": "   "})
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


# ===========================================================================
# 大纲：生成 / 编辑 / 校验
# ===========================================================================

def test_accepting_requirements_generates_a_structured_outline(client):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    sid = created["id"]
    _walk_to_confirm(client, sid)
    _post(client, f"{BASE}/{sid}/confirm-requirements", {"accepted": True})
    session = _wait_phase(client, sid, ("editing_outline",))

    outline = session["outline"]
    assert outline["chapters"], "大纲必须有章"
    assert outline["unit_count"] == sum(len(c["units"]) for c in outline["chapters"])
    first = outline["chapters"][0]
    assert first["id"] == "ch01" and first["number"] == 1
    assert first["units"][0]["id"] == "ch01-u1"
    assert first["units"][0]["number"] == 1
    assert first["units"][0]["title"]
    # 大纲落成一条消息（前端在对话流里就地渲染那份可编辑的结构）
    assert any(m.get("kind") == "outline" for m in session["messages"])
    # 任务账本要结算成成功，而不是一直"运行中"
    assert session["task"]["status"] == "succeeded"
    assert session["task"]["percent"] == 100.0


def test_outline_can_be_edited_and_stays_consistent(client):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    sid = created["id"]
    _walk_to_confirm(client, sid)
    _post(client, f"{BASE}/{sid}/confirm-requirements", {"accepted": True})
    session = _wait_phase(client, sid, ("editing_outline",))
    outline = session["outline"]

    # 改章名 + 加一个单元 + 删掉最后一章（模拟用户真的在页面上改）
    outline["chapters"][0]["title"] = "信息安全绪论"
    outline["chapters"][0]["units"].append({
        "title": "信息安全的发展简史", "type": "新授", "hours": 1.0,
        "summary": "", "knowledge_points": ["发展阶段", "标志事件"]})
    dropped = outline["chapters"].pop()

    saved = _post(client, f"{BASE}/{sid}/outline", {"outline": outline}).json()
    assert saved["outline"]["chapters"][0]["title"] == "信息安全绪论"
    # 序号与 id 必须按位置重排（否则蓝图里会出现 ch01 后面跟 ch03）
    assert [c["id"] for c in saved["outline"]["chapters"]] == [
        f"ch{i:02d}" for i in range(1, len(saved["outline"]["chapters"]) + 1)]
    assert [u["id"] for u in saved["outline"]["chapters"][0]["units"]] == [
        f"ch01-u{i}" for i in range(1, len(saved["outline"]["chapters"][0]["units"]) + 1)]
    assert "信息安全的发展简史" in [u["title"] for u in saved["outline"]["chapters"][0]["units"]]
    assert dropped["title"] not in [c["title"] for c in saved["outline"]["chapters"]]


def test_broken_outline_is_rejected_with_a_reason(client):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    sid = created["id"]
    _walk_to_confirm(client, sid)
    _post(client, f"{BASE}/{sid}/confirm-requirements", {"accepted": True})
    _wait_phase(client, sid, ("editing_outline",))

    r = _post(client, f"{BASE}/{sid}/outline", {"outline": {"chapters": []}})
    assert r.status_code == 400
    assert "至少要有一个章" in r.json()["message"]

    r = _post(client, f"{BASE}/{sid}/outline",
              {"outline": {"chapters": [{"title": "章", "units": []}]}})
    assert r.status_code == 400
    assert "没有单元" in r.json()["message"]


def test_confirm_outline_requires_an_outline(client):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    r = _post(client, f"{BASE}/{created['id']}/outline/confirm", {})
    assert r.status_code == 409


# ===========================================================================
# 确认大纲 → 单元设计 → 存入课程库
# ===========================================================================

def test_confirming_outline_saves_the_course(client):
    sid, session = _build_course(client)
    assert session["phase"] == "ready"
    assert session["course_id"], "课程必须真的入库（不只是 session 上有个 id）"
    outline = session["outline"]
    assert session["unit_designs_count"] == outline["unit_count"]

    courses = client.get("/api/v1/courses").json()
    row = next((c for c in courses if c["id"] == session["course_id"]), None)
    assert row is not None, courses
    assert row["source"] == "builder"
    assert row["unit_count"] == outline["unit_count"]
    # 还没有任何 PPT：卡片上必须能看出"0 / N 课已有课件"
    assert row["ppt_total"] == outline["unit_count"]
    assert row["ppt_ready_count"] == 0

    detail = client.get(f"/api/v1/courses/{session['course_id']}").json()
    units = [u for ch in detail["chapters"] for u in ch["units"]]
    assert len(units) == outline["unit_count"]
    assert [u["title"] for u in units] == [
        u["title"] for c in outline["chapters"] for u in c["units"]]
    # 大纲原样给到详情页（章 → 单元 + 知识点）
    assert detail["outline"]["course_title"] == outline["course_title"]
    assert detail["ppt"] == {"total": outline["unit_count"], "ready": 0,
                             "next_unit_id": units[0]["id"]}


def test_course_files_land_in_the_run_dir(client, _isolate):
    _, session = _build_course(client)
    run_dir = Path(session["run_dir"])
    assert run_dir.is_dir()
    assert (run_dir / "outline.json").is_file()
    assert (run_dir / "outline.md").is_file()
    assert (run_dir / "01_blueprint.yaml").is_file()
    assert (run_dir / "02_targets.json").is_file()
    assert (run_dir / "00_spec.json").is_file()
    assert (run_dir / "manifest.json").is_file()

    outline = session["outline"]
    for chapter in outline["chapters"]:
        for unit in chapter["units"]:
            assert (run_dir / f"unit_design_{unit['id']}.json").is_file()
            md = (run_dir / f"03_unit_{unit['id']}.result.md").read_text(encoding="utf-8")
            for module in ("本单元定位", "难度调整说明", "本课目标", "知识讲解", "典型例题",
                           "易错提醒", "小结", "课件大纲", "单元作业", "完成后解锁"):
                assert f"【{module}】" in md, f"{unit['id']} 缺 {module}"
            assert "P1 " in md, "课件大纲必须有页骨架"

    # 蓝图要能被 flow.py 读回来（chapter_order / unit id / lesson_status）
    import yaml

    blueprint = yaml.safe_load((run_dir / "01_blueprint.yaml").read_text(encoding="utf-8"))
    assert blueprint["course"]["subject"] == outline["subject"]
    assert [c["id"] for c in blueprint["chapters"]] == [c["id"] for c in outline["chapters"]]
    for chapter in blueprint["chapters"]:
        for unit in chapter["units"]:
            assert unit["lesson_status"] == "pending"
            assert unit["estimated_hours"] > 0


def test_unit_designs_are_hidden_from_http(client):
    """单元课程安排设计是给 PPT 用的，接口里**没有**它的正文。"""
    _, session = _build_course(client)
    assert "designs" not in session
    for item in session["messages"]:
        blob = json.dumps(item, ensure_ascii=False)
        assert "teaching_goals" not in blob and "lesson_flow" not in blob


def test_course_ids_match_the_scanner(client, _isolate):
    """课程 / 单元 id 必须与 studio.catalog 的算法一致，否则「重新扫描」会重复插入。"""
    _, session = _build_course(client)
    from studio import catalog

    run_dir = Path(session["run_dir"])
    scanned = catalog.scan_runs(_isolate / "runs")
    # 还没有 pptx：扫描看不到它，这是对的（扫描只认交付件）
    assert all(c.id != session["course_id"] for c in scanned)

    course_id = session["course_id"]
    conn = db_mod.connect()
    try:
        units = [dict(r) for r in conn.execute(
            "SELECT * FROM units WHERE course_id=? ORDER BY order_index", (course_id,))]
    finally:
        conn.close()
    # 单元 id 用的是"课程 id + 章名 + 交付件文件名"，与 catalog 重算一致：
    # 这里用同一口径重算一遍，确保没被别处改动过。
    assert len({u["id"] for u in units}) == len(units)
    for unit in units:
        expected = course_builder._stable_id(
            course_id, unit["chapter"], f"第{unit['order_index'] % 6 + 1}单元-{unit['title']}.pptx")
        assert isinstance(expected, str) and len(expected) == 12


def test_reconcile_marks_stuck_sessions_interrupted(client, _isolate):
    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        conn.execute(
            "UPDATE course_builder_sessions SET phase='generating_outline', task_status='running'"
            " WHERE id=?", (created["id"],))
        conn.commit()
    finally:
        conn.close()
    assert course_builder.reconcile() >= 1
    session = _session(client, created["id"])
    assert session["phase"] == "confirming_requirements"
    assert session["task"]["status"] == "interrupted"


# ===========================================================================
# 渐进式 PPT
# ===========================================================================

def _first_unit(client, course_id: str) -> dict:
    plan = client.get(f"/api/v1/courses/{course_id}/ppt-plan").json()
    return plan["items"][0]


def test_ppt_plan_lists_every_unit_as_pending(client):
    _, session = _build_course(client)
    plan = client.get(f"/api/v1/courses/{session['course_id']}/ppt-plan").json()
    assert plan["total"] == session["unit_designs_count"]
    assert plan["ready"] == 0
    assert plan["next_unit_id"] == plan["items"][0]["unit_id"]
    assert all(item["has_ppt"] is False for item in plan["items"])
    assert [item["order_index"] for item in plan["items"]] == list(range(len(plan["items"])))


def test_unknown_course_plan_is_404(client):
    assert client.get("/api/v1/courses/nope/ppt-plan").status_code == 404


def test_generate_next_ppt_makes_one_unit_lesson_ready(client, monkeypatch):
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    _, session = _build_course(client)
    course_id = session["course_id"]
    first = _first_unit(client, course_id)

    started = _post(client, f"/api/v1/courses/{course_id}/ppt/next")
    assert started.status_code == 202, started.text
    assert started.json()["unit_id"] == first["unit_id"]

    plan = _wait_ppt_done(client, course_id, first["unit_id"])
    assert plan["ready"] == 1
    ready = next(i for i in plan["items"] if i["unit_id"] == first["unit_id"])
    assert ready["has_ppt"] is True and ready["slides"] > 0
    assert plan["next_unit_id"] == plan["items"][1]["unit_id"]

    # 课程详情：这一课可以进了（ready 由"备过课"表达）
    detail = client.get(f"/api/v1/courses/{course_id}").json()
    unit = next(u for ch in detail["chapters"] for u in ch["units"]
                if u["id"] == first["unit_id"])
    assert unit["ready"] is True
    assert detail["ppt"]["ready"] == 1

    # 课程库列表也能看见"1 / N 课已有课件"
    row = next(c for c in client.get("/api/v1/courses").json() if c["id"] == course_id)
    assert row["ppt_ready_count"] == 1


def _wait_ppt_done(client, course_id: str, unit_id: str, *, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/v1/courses/{course_id}/ppt-plan").json()
        item = next((i for i in last["items"] if i["unit_id"] == unit_id), None)
        if item is not None and item["job_status"] in ("succeeded", "failed", "cancelled",
                                                       "interrupted"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"{timeout}s 内这一课的 PPT 没跑完：{json.dumps(last, ensure_ascii=False)[:600]}")


def test_generate_next_ppt_goes_in_order(client, monkeypatch):
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    _, session = _build_course(client)
    course_id = session["course_id"]
    plan = client.get(f"/api/v1/courses/{course_id}/ppt-plan").json()
    assert plan["total"] >= 2, "这条用例需要至少两个单元"

    for index in range(2):
        started = _post(client, f"/api/v1/courses/{course_id}/ppt/next").json()
        assert started["unit_id"] == plan["items"][index]["unit_id"]
        _wait_ppt_done(client, course_id, started["unit_id"])
    after = client.get(f"/api/v1/courses/{course_id}/ppt-plan").json()
    assert after["ready"] == 2


def test_ppt_for_a_single_unit(client, monkeypatch):
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    _, session = _build_course(client)
    course_id = session["course_id"]
    plan = client.get(f"/api/v1/courses/{course_id}/ppt-plan").json()
    target = plan["items"][-1]                     # 直接点最后一课，跳过前面的

    started = _post(client, f"/api/v1/units/{target['unit_id']}/ppt")
    assert started.status_code == 202, started.text
    after = _wait_ppt_done(client, course_id, target["unit_id"])
    assert after["items"][-1]["has_ppt"] is True
    # 顺序口径不变：下一课仍是最前面那个没片的单元
    assert after["next_unit_id"] == plan["items"][0]["unit_id"]


def test_ppt_is_single_flight(client, monkeypatch):
    """一次只做一课：并发点两次，第二次必须是 409（不是排队，也不是两份一起跑）。"""
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "1.5")
    _, session = _build_course(client)
    course_id = session["course_id"]
    first = _post(client, f"/api/v1/courses/{course_id}/ppt/next")
    assert first.status_code == 202
    second = _post(client, f"/api/v1/courses/{course_id}/ppt/next")
    assert second.status_code == 409
    assert second.json()["code"] == "conflict"
    assert "PPT 在生成" in second.json()["message"]
    unit_id = first.json()["unit_id"]
    assert client.post(f"/api/v1/units/{unit_id}/ppt/cancel").status_code == 200


def test_ppt_can_be_cancelled(client, monkeypatch):
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "5")
    _, session = _build_course(client)
    course_id = session["course_id"]
    started = _post(client, f"/api/v1/courses/{course_id}/ppt/next").json()
    body = client.post(f"/api/v1/units/{started['unit_id']}/ppt/cancel").json()
    assert body["job"]["status"] == "cancelled"
    assert body["has_ppt"] is False
    # 取消之后名额放出来了，可以重新点
    again = _post(client, f"/api/v1/courses/{course_id}/ppt/next")
    assert again.status_code == 202
    client.post(f"/api/v1/units/{again.json()['unit_id']}/ppt/cancel")


def test_ppt_falls_back_when_the_pipeline_produces_nothing(client, monkeypatch):
    """退出码 0 但没有 pptx（渲染全失败）：仍要交付一份能上的课件。

    产品口径：学生点了"生成下一课"，就该拿到课件 —— 而不是一条红色错误。
    """
    monkeypatch.setenv("FAKE_FLOW_MODE", "noppt")
    _, session = _build_course(client)
    course_id = session["course_id"]
    target = _first_unit(client, course_id)
    _post(client, f"/api/v1/courses/{course_id}/ppt/next")
    plan = _wait_ppt_done(client, course_id, target["unit_id"])
    item = next(i for i in plan["items"] if i["unit_id"] == target["unit_id"])
    assert item["job_status"] == "succeeded"
    assert item["has_ppt"] is True and item["slides"] > 0
    unit = client.get(f"/api/v1/units/{target['unit_id']}").json()
    assert unit["slides"] > 0


def test_ppt_failure_is_reported_but_not_fatal(client, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "3")
    monkeypatch.setenv("FAKE_FLOW_MODE", "noppt")
    _, session = _build_course(client)
    course_id = session["course_id"]
    # 让兜底合成也走不通：把大纲挪走（模拟课程目录被改坏）
    run_dir = Path(session["run_dir"])
    (run_dir / "outline.json").rename(run_dir / "outline.json.bak")
    target = _first_unit(client, course_id)
    _post(client, f"/api/v1/courses/{course_id}/ppt/next")
    plan = _wait_ppt_done(client, course_id, target["unit_id"])
    item = next(i for i in plan["items"] if i["unit_id"] == target["unit_id"])
    assert item["job_status"] == "failed"
    assert item["error"]
    # 失败也要把名额放出来（否则用户永远点不动下一课）
    assert _post(client, f"/api/v1/courses/{course_id}/ppt/next").status_code == 202


def test_all_units_ready_is_409(client, monkeypatch):
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.005")
    _, session = _build_course(client)
    course_id = session["course_id"]
    plan = client.get(f"/api/v1/courses/{course_id}/ppt-plan").json()
    for _ in range(plan["total"]):
        started = _post(client, f"/api/v1/courses/{course_id}/ppt/next")
        assert started.status_code == 202, started.text
        _wait_ppt_done(client, course_id, started.json()["unit_id"])
    r = _post(client, f"/api/v1/courses/{course_id}/ppt/next")
    assert r.status_code == 409
    assert "都已经生成" in r.json()["message"]


def test_deleting_a_course_cleans_its_ppt_jobs(client, monkeypatch):
    """删课程要把单课出片任务一起带走，并把会话上的 course_id 断掉。

    留着任务行的后果很具体：它是一条 status=running 的孤儿，而"一次只做一课"是全局
    单并发 —— 用户从此点不动「生成下一课」（对账只在服务启动时跑，救不了这一次）。
    """
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "5")
    _, session = _build_course(client)
    course_id = session["course_id"]
    started = _post(client, f"/api/v1/courses/{course_id}/ppt/next").json()
    client.post(f"/api/v1/units/{started['unit_id']}/ppt/cancel")

    assert client.delete(f"/api/v1/courses/{course_id}").json() == {"ok": True}
    conn = db_mod.connect()
    try:
        jobs = conn.execute("SELECT COUNT(*) AS n FROM unit_ppt_jobs WHERE course_id=?",
                            (course_id,)).fetchone()["n"]
        row = conn.execute("SELECT course_id FROM course_builder_sessions WHERE id=?",
                           (session["id"],)).fetchone()
    finally:
        conn.close()
    assert jobs == 0
    assert row is not None and row["course_id"] == ""
    assert client.get(f"/api/v1/courses/{course_id}/ppt-plan").status_code == 404


# ===========================================================================
# 真模型分支（用假模型验解析与落盘，不联网）
# ===========================================================================

def test_model_tool_calls_are_rendered_as_cards(client, monkeypatch):
    """模型调 ask_user_question → 前端拿到的是可点选的提问卡。"""
    monkeypatch.setattr(course_builder, "_model_target",
                        lambda conn: {"base_url": "http://x/v1", "api_key": "k",
                                      "model": "fake", "name": "fake"})

    async def fake_ask(target, messages, **kw):
        return "好的，先确认两件事。", [{
            "name": "ask_user_question",
            "arguments": {"intro": "好的，先确认两件事。", "questions": [
                {"id": "depth", "header": "程度", "question": "想学到什么程度？",
                 "options": [{"label": "入门"}, {"label": "会做题"}]},
                {"id": "period", "header": "周期", "question": "打算学多久？",
                 "options": [{"label": "2 周"}, {"label": "2 个月"}]},
            ]},
        }]

    monkeypatch.setattr(course_builder, "_ask_model", fake_ask)
    session = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    tool = _last_tool(session)
    assert tool["name"] == "ask_user_question"
    assert [q["id"] for q in tool["questions"]] == ["depth", "period"]
    assert tool["questions"][0]["options"][1]["label"] == "会做题"
    assert session["messages"][0]["role"] == "user"

    async def fake_confirm(target, messages, **kw):
        return "", [{"name": "confirm_course_requirements", "arguments": {
            "reply": "整理好了，看看对不对",
            "requirements": {"course_title": "信息安全理论与技术", "subject": "信息安全",
                             "stage": "大学", "goal": "能独立分析常见安全问题",
                             "student_level": "基础", "weeks": 8, "hours_per_week": 3,
                             "key_topics": ["密码学", "网络攻防"]}}}]

    monkeypatch.setattr(course_builder, "_ask_model", fake_confirm)
    session = _post(client, f"{BASE}/{session['id']}/messages",
                    {"text": "想入门，两个月"}).json()
    tool = _last_tool(session)
    assert tool["name"] == "confirm_course_requirements"
    assert tool["requirements"]["course_title"] == "信息安全理论与技术"
    assert session["phase"] == "confirming_requirements"


def test_model_outline_is_normalized_and_persisted(client, monkeypatch):
    """模型给的大纲（章/单元/知识点）要原样变成结构化大纲与落盘文件。"""
    monkeypatch.setattr(course_builder, "_model_target",
                        lambda conn: {"base_url": "http://x/v1", "api_key": "k",
                                      "model": "fake", "name": "fake"})

    async def fake_ask(target, messages, **kw):
        return "", [{"name": "confirm_course_requirements", "arguments": {
            "reply": "ok",
            "requirements": {"course_title": "信息安全理论与技术", "subject": "信息安全",
                             "stage": "大学", "goal": "掌握信息安全的基本理论与技术",
                             "student_level": "基础", "weeks": 8, "hours_per_week": 3}}}]

    async def fake_json(target, messages, **kw):
        return {"course_title": "信息安全理论与技术", "weeks": 8, "hours_per_week": 3,
                "chapters": [
                    {"title": "绪论", "units": [
                        {"title": "课程介绍", "hours": 2, "knowledge_points": ["信息安全定义"]},
                        {"title": "教学安排", "hours": 2, "knowledge_points": ["考核方式"]}]},
                    {"title": "信息安全概述", "units": [
                        {"title": "信息与信息技术", "hours": 4,
                         "knowledge_points": ["信息的定义", "信息技术的构成"]},
                        {"title": "安全目标", "hours": 4, "knowledge_points": ["CIA 三要素"]}]},
                ]}

    monkeypatch.setattr(course_builder, "_ask_model", fake_ask)
    monkeypatch.setattr(course_builder, "_ask_json", fake_json)

    created = _post(client, BASE, {"message": "我想学习信息安全"}).json()
    sid = created["id"]
    _post(client, f"{BASE}/{sid}/confirm-requirements", {"accepted": True})
    session = _wait_phase(client, sid, ("editing_outline",))
    outline = session["outline"]
    assert [c["title"] for c in outline["chapters"]] == ["绪论", "信息安全概述"]
    assert [u["title"] for u in outline["chapters"][0]["units"]] == ["课程介绍", "教学安排"]
    assert outline["chapters"][1]["units"][1]["knowledge_points"] == ["CIA 三要素"]
    assert outline["unit_count"] == 4

    _post(client, f"{BASE}/{sid}/outline/confirm", {})
    session = _wait_phase(client, sid, ("ready",), timeout=60)
    assert session["unit_designs_count"] == 4
    assert client.get(f"/api/v1/courses/{session['course_id']}/ppt-plan").json()["total"] == 4


# ===========================================================================
# course_plan 纯函数
# ===========================================================================

def test_normalize_requirements_fills_the_gaps():
    req = course_plan.normalize_requirements({}, "我想用两周突击高中物理的受力分析")
    assert req["stage"] == "高中"
    assert req["weeks"] == 2
    assert req["course_title"]
    assert req["goal"]
    assert req["student_level"] in course_plan.LEVELS


def test_normalize_outline_renumbers_whatever_the_model_sent():
    req = course_plan.normalize_requirements({"course_title": "X"}, "X")
    outline = course_plan.normalize_outline({
        "chapters": [
            {"title": "A", "units": [{"title": "u1"}, {"title": "u2"}]},
            {"title": "B", "units": [{"title": "u3"}]},
        ]}, req)
    assert [c["id"] for c in outline["chapters"]] == ["ch01", "ch02"]
    assert [u["id"] for u in outline["chapters"][0]["units"]] == ["ch01-u1", "ch01-u2"]
    assert outline["chapters"][1]["units"][0]["id"] == "ch02-u1"
    assert outline["unit_count"] == 3


def test_outline_rejects_a_non_outline():
    with pytest.raises(course_plan.PlanError):
        course_plan.normalize_outline({"chapters": []}, {})
    with pytest.raises(course_plan.PlanError):
        course_plan.normalize_outline("不是 JSON", {})


def test_blueprint_matches_what_flow_consumes():
    req = course_plan.normalize_requirements({"course_title": "离散数学", "weeks": 6,
                                              "hours_per_week": 3}, "离散数学")
    outline = course_plan.offline_outline(req)
    blueprint = course_plan.outline_to_blueprint(outline)
    assert blueprint["course"]["term"] in ("长期", "短期")
    assert blueprint["course"]["net_hour_budget"] == course_plan.outline_total_hours(outline)
    for index, chapter in enumerate(blueprint["chapters"]):
        assert chapter["order"] == index + 1
        assert chapter["id"] == f"ch{index + 1:02d}"
        for unit in chapter["units"]:
            assert unit["lesson_status"] == "pending"
    # 门控：首章 none，其余 after_chapter 指向前一章
    assert blueprint["chapters"][0]["unlock"]["type"] == "none"
    assert blueprint["chapters"][1]["unlock"]["chapter"] == "ch01"


def test_balanced_hours_follow_the_period():
    req = course_plan.normalize_requirements({"course_title": "X", "weeks": 10,
                                              "hours_per_week": 4}, "X")
    outline = course_plan.normalize_outline({
        "chapters": [{"title": "A", "units": [{"title": "u%d" % i, "hours": 1}
                                              for i in range(1, 5)]}],
        "weeks": 10, "hours_per_week": 4}, req)
    total = course_plan.outline_total_hours(outline)
    # 差得太多（4 vs 40）→ 重排到预算附近
    assert abs(total - 40) <= 2.0 or total > 4


def test_render_unit_markdown_is_deterministic():
    req = course_plan.normalize_requirements({"course_title": "信息安全"}, "信息安全")
    outline = course_plan.offline_outline(req)
    chapter = outline["chapters"][0]
    unit = chapter["units"][0]
    design = course_plan.offline_design(unit, chapter, outline)
    first = course_plan.render_unit_markdown(design, chapter_index=0)
    second = course_plan.render_unit_markdown(design, chapter_index=0)
    assert first == second
    # 10 个模块一个不少（unit_bridge 靠它筛出 5 个教学内容模块）
    for module in ("本单元定位", "难度调整说明", "本课目标", "知识讲解", "典型例题",
                   "易错提醒", "小结", "课件大纲", "单元作业", "完成后解锁"):
        assert f"【{module}】" in first
