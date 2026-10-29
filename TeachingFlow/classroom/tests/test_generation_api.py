"""出片任务接口（config / 发起 / 轮询 / 取消 / 重启对账）。

几条测试纪律：

* **假子进程**：既不调真模型，也不跑真的 flow.py —— 这些用例验的是"课堂怎么驱动一条
  流水线"（命令拼装、阶段解析、进度推进、取消、登记课程），不是出片本身；
* 每例一个临时库 + 临时 runs 根 + 清空的 DEEPSEEK_*：本机配没配 key 都不该影响
  任何一条判据（前置检查问的是库，不是 .env）；
* 轮询按真时间采样，所以假脚本每个阶段故意睡一点 —— 不睡就采不到中间态，
  而"进度单调推进"恰恰只在中间态里看得出真假。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CLASSROOM = Path(__file__).resolve().parents[1]
if str(CLASSROOM) not in sys.path:              # 允许从任意 cwd 直接跑本文件
    sys.path.insert(0, str(CLASSROOM))

import generation                                # noqa: E402
from store import db as db_mod                   # noqa: E402

API = "/api/v1/generations"
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
GOAL = "两周突击高中物理的受力分析"

#: 假流水线：打与 flow.py 同形状的【S0】…【S5】，并写一份假 run 目录（manifest + pptx）。
#: 速度/退出码/pid 落盘/有没有产物都由 FAKE_* 环境变量控制 —— 同一个脚本能演成功、
#: 失败、取消，以及"退出码 0 但一份片都没产"（真机里 flow.py 就是这样的）。
FAKE_FLOW = '''
import json, os, sys, time
from pathlib import Path

runs = Path(os.environ["FAKE_RUNS_ROOT"])
run = runs / "20260101" / "测试课程_120000"
chapter = run / "第1章-入门"
chapter.mkdir(parents=True, exist_ok=True)
pptx = chapter / "第1单元-引言.pptx"
produced = []
if os.environ.get("FAKE_NO_PPTX"):
    print("  ✗ s5_render:ch01-u1", flush=True)
    print("RUN_REPORT " + json.dumps({"status": "failed", "role": "teacher_ppt",
                                     "brief": "教案正文" * 75}, ensure_ascii=False),
          flush=True)
else:
    pptx.write_bytes(b"PK-not-a-real-pptx")
    produced.append(str(pptx))
(run / "manifest.json").write_text(
    json.dumps({"input": "假目标", "produced": produced}, ensure_ascii=False),
    encoding="utf-8")
pid_file = os.environ.get("FAKE_PID_FILE")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
print("  产物目录：" + str(run), flush=True)
sleep_for = float(os.environ.get("FAKE_STAGE_SLEEP") or 0)
for i in range(6):
    print("【S%d】假阶段 %d" % (i, i), flush=True)
    time.sleep(sleep_for)
code = int(os.environ.get("FAKE_EXIT") or 0)
if code:
    print("❌ 假失败：这条流水线故意退出 %d" % code, flush=True)
sys.exit(code)
'''


# ===========================================================================
# 夹具
# ===========================================================================

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每例：独立库、独立 runs 根、清空的 DEEPSEEK_*、清空的内存活任务表。"""
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "classroom.sqlite"))
    monkeypatch.setenv("FAKE_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.delenv("CLASSROOM_DEV", raising=False)
    for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    for key in ("FAKE_STAGE_SLEEP", "FAKE_PID_FILE", "FAKE_EXIT", "FAKE_NO_PPTX"):
        monkeypatch.delenv(key, raising=False)
    # 前置检查问的是库里的默认服务商（CLASSROOM_DB 已指到 tmp），没有读 .env 的路径了；
    # 三个 DEEPSEEK_* 仍然清掉：没有服务商时 config_view 的 model/base_url 只是占位，
    # 占了 config.deepseek_profile() 的 env 默认值，本机配没配不该影响这条判据。

    from app import deps

    monkeypatch.setattr(deps, "STATE_ROOT", tmp_path / "state", raising=False)
    monkeypatch.setattr(deps, "RUNS_ROOT", tmp_path / "runs", raising=False)
    monkeypatch.setattr(generation, "_LIVE", {})
    deps.reset_settings_cache()
    yield
    deps.reset_settings_cache()


@pytest.fixture()
def client(_isolate):
    from app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def fake_flow(tmp_path, monkeypatch):
    """把 FLOW_SCRIPT 换成假脚本（真 flow.py 一次都不跑）。"""
    path = tmp_path / "fake_flow.py"
    path.write_text(FAKE_FLOW, encoding="utf-8")
    monkeypatch.setattr(generation, "FLOW_SCRIPT", path)
    return path


# ===========================================================================
# 小工具
# ===========================================================================

def _create(client, **body):
    body.setdefault("goal", GOAL)
    return client.post(API, json=body)


def _snap(client, gen_id):
    return client.get(f"{API}/{gen_id}").json()


def _poll(client, gen_id, timeout=25.0):
    """轮询到终态，返回 (最后一条快照, 采样序列)。"""
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        snap = _snap(client, gen_id)
        seen.append(snap)
        if snap["status"] in TERMINAL:
            return snap, seen
        time.sleep(0.03)
    raise AssertionError(f"{timeout}s 内没到终态，最后一条：{json.dumps(seen[-1], ensure_ascii=False)}")


def _wait_file(path: Path, timeout=25.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        time.sleep(0.02)
    raise AssertionError(f"{path} 一直没出现")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace")
        return f'"{pid}"' in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_dead(pid: int, timeout=25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _conn():
    conn = db_mod.connect()
    db_mod.migrate(conn)
    return conn


def _seed_active(status="running", gen_id="g-leftover") -> str:
    """直接写一条"上个进程留下的"运行中任务（对账用例的输入）。"""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO generations(id, goal, scope_stage, subject, term, level, status,"
            " mode, stage, stage_label, percent, log_tail, created_at, started_at,"
            " finished_at, elapsed_sec, result, error)"
            " VALUES(:id, :goal, :scope, '', '', '', :status, 'real', 'S2', '挑选单元', 45,"
            " :log, '2026-01-01T00:00:00', '2026-01-01T00:00:01', '', 0.0, '', '')",
            {"id": gen_id, "goal": "旧任务", "scope": "高中", "status": status,
             "log": json.dumps(['python flow.py "旧任务" --to render'], ensure_ascii=False)})
        conn.commit()
    finally:
        conn.close()
    return gen_id


# ===========================================================================
# config：本机现在能不能出片
# ===========================================================================

def test_config_without_provider_says_where_to_configure(client):
    body = client.get(f"{API}/config").json()
    assert set(body) == {"ready", "has_key", "model", "base_url", "mock_allowed",
                         "mode", "reason"}
    assert body["ready"] is False
    assert body["has_key"] is False
    assert body["mock_allowed"] is False
    assert body["mode"] == "real"
    # 真源已经搬进库：reason 该指「设置 · 模型服务商」，指 .env 是错的去处
    assert "模型服务商" in body["reason"] and "设置" in body["reason"]
    # 键集是冻结契约，没有服务商时 model/base_url 也得给一行可显示的东西
    assert body["model"] and body["base_url"]


def test_config_in_dev_allows_mock(client, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    body = client.get(f"{API}/config").json()
    assert body["ready"] is True
    assert body["mock_allowed"] is True
    assert body["has_key"] is False
    assert body["mode"] == "mock"
    assert "CLASSROOM_DEV" in body["reason"]


# ===========================================================================
# 发起：前置条件与命令拼装
# ===========================================================================

def test_create_without_provider_is_400(client):
    r = _create(client, stage="高中")
    assert r.status_code == 400
    body = r.json()
    # code=no_provider 是页面分派"去设置页配服务商"的凭据（GenerateView 按码跳）
    assert body["ok"] is False and body["code"] == "no_provider"
    assert "模型服务商" in body["message"] and "设置" in body["message"]


def test_create_without_stage_is_400(client, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    r = _create(client)
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "bad_request"
    assert "学段" in body["message"]


def test_level_outside_the_table_is_400(client, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    r = _create(client, stage="高中", level="expert")
    assert r.status_code == 400
    assert r.json()["message"] == "level 只支持 beginner / intermediate"


def test_level_is_translated_only_when_given():
    """表里翻译成 flow.py 的 CLI 词汇；没传就一个字都不拼（让链路自己抽）。"""
    argv = generation.build_command(goal="x", stage="高中", level="beginner")
    assert "--level" in argv
    assert argv[argv.index("--level") + 1] == "入门"
    assert "--level" not in generation.build_command(goal="x", stage="高中")


def test_command_line_shape_for_mock_run(client, fake_flow, monkeypatch):
    """dev+mock：202、mode=mock，首行日志就是将要执行的那条命令（原样可复现）。"""
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    r = _create(client, stage="高中", term="short", level="beginner")
    assert r.status_code == 202
    snap = r.json()
    assert snap["status"] in ("queued", "running")
    assert snap["mode"] == "mock"
    cmd = snap["log_tail"][0]
    assert cmd.startswith("python ")
    assert f'"{GOAL}"' in cmd
    assert "--to render --no-classroom --no-input --stage 高中" in cmd
    assert "--accept-detected" in cmd          # 没填学科 → 必须兜住
    assert "--subject" not in cmd              # 空学科绝不拼成 --subject ""
    assert "--term short --level 入门" in cmd
    assert cmd.endswith("--mock --render")
    _poll(client, snap["id"])


def test_subject_is_passed_and_accept_detected_dropped(client, fake_flow, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    snap = _create(client, stage="高中", subject="物理").json()
    cmd = snap["log_tail"][0]
    assert "--subject 物理" in cmd
    assert "--accept-detected" not in cmd
    _poll(client, snap["id"])


def test_second_task_conflicts(client, fake_flow, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.5")
    first = _create(client, stage="高中")
    assert first.status_code == 202
    r = _create(client, stage="高中")
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["code"] == "conflict"
    # 收拾干净：别让用例结束时还有个慢子进程在跑
    c = client.post(f"{API}/{first.json()['id']}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelled"


# ===========================================================================
# 轮询：进度单调推进 + 成功后登记课程
# ===========================================================================

def test_mock_run_succeeds_and_registers_course(client, fake_flow, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.05")
    gen_id = _create(client, stage="高中").json()["id"]
    final, _seen = _poll(client, gen_id)
    assert final["status"] == "succeeded"
    assert final["progress"]["percent"] == 100.0
    assert final["progress"]["done"] == final["progress"]["total"] == 6
    assert final["stage"] == "S5" and final["stage_label"]
    assert final["finished_at"] and final["error"] == ""
    assert len(final["log_tail"]) <= generation.LOG_TAIL_KEEP
    # 冻结契约的字段集：多一个少一个都算改契约，所以逐层钉死
    assert set(final) == {"id", "goal", "status", "mode", "stage", "stage_label",
                          "progress", "log_tail", "started_at", "finished_at",
                          "elapsed_sec", "result", "error"}
    assert set(final["progress"]) == {"done", "total", "percent"}
    result = final["result"]
    assert set(result) == {"course_id", "course_title", "pptx", "scan"}
    assert set(result["scan"]) == {"added", "updated", "total"}
    assert result["pptx"] and result["pptx"][0].endswith("第1单元-引言.pptx")
    assert result["scan"]["total"] == 1 and result["scan"]["added"] == 1
    assert result["course_id"] and result["course_title"]

    # 课程库里真的能查到（不是只写在 result 里）
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM courses WHERE id=?",
                           (result["course_id"],)).fetchone()
        units = conn.execute("SELECT * FROM units WHERE course_id=?",
                             (result["course_id"],)).fetchall()
    finally:
        conn.close()
    assert row is not None and row["run_dir"].endswith("测试课程_120000")
    assert len(units) == 1


def test_progress_and_stage_move_forward_monotonically(client, fake_flow, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.12")
    gen_id = _create(client, stage="高中").json()["id"]
    final, seen = _poll(client, gen_id)

    percents = [float(s["progress"]["percent"]) for s in seen]
    assert percents == sorted(percents), f"进度倒退了：{percents}"
    ranks = [generation.STAGE_ORDER.index(s["stage"]) if s["stage"] in
             generation.STAGE_ORDER else -1 for s in seen]
    assert ranks == sorted(ranks), f"阶段倒退了：{ranks}"
    assert len(set(percents)) >= 3                    # 真的在动，不是一步到位
    assert any(s["status"] == "running" for s in seen)
    assert final["status"] == "succeeded"
    assert final["progress"]["percent"] == 100.0


# ===========================================================================
# 取消：杀整棵进程树
# ===========================================================================

def test_cancel_kills_the_process_tree(client, fake_flow, tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "5")
    pid_file = tmp_path / "fake.pid"
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    gen_id = _create(client, stage="高中").json()["id"]
    pid = int(_wait_file(pid_file))
    assert _alive(pid), "假流水线这会儿应该还在跑"

    r = client.post(f"{API}/{gen_id}/cancel")
    assert r.status_code == 200
    snap = r.json()
    assert snap["status"] == "cancelled"
    assert snap["finished_at"]
    assert snap["error"]
    assert snap["log_tail"][-1].startswith("⛔")
    assert _wait_dead(pid), f"取消后进程 {pid} 还活着"

    again = client.post(f"{API}/{gen_id}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "conflict"
    assert _snap(client, gen_id)["status"] == "cancelled"


# ===========================================================================
# 失败：退出码 + 日志尾
# ===========================================================================

def test_failure_records_exit_code_and_tail(client, fake_flow, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    monkeypatch.setenv("FAKE_EXIT", "3")
    gen_id = _create(client, stage="高中").json()["id"]
    final, _seen = _poll(client, gen_id)
    assert final["status"] == "failed"
    assert "退出码 3" in final["error"]
    assert "假失败" in final["error"]
    assert final["result"] is None
    assert final["finished_at"]


def test_exit_zero_without_any_pptx_is_failure(client, fake_flow, monkeypatch):
    """退出码 0 但零产物 = 失败：flow.py 会吞掉单元级渲染失败并照样返回 0。"""
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    monkeypatch.setenv("FAKE_NO_PPTX", "1")
    gen_id = _create(client, stage="高中").json()["id"]
    final, _seen = _poll(client, gen_id)
    assert final["status"] == "failed"
    assert final["result"]["pptx"] == []
    assert "没有产出任何 PPTX" in final["error"]
    assert "✗ s5_render" in final["error"]            # 原样的判决行
    assert "DEEPSEEK_API_KEY" in final["error"]       # 末尾的下一步指引
    # error 是给页面贴的一段字，不是日志：几千字的 RUN_REPORT 行必须被挡在外面
    assert "RUN_REPORT" not in final["error"]
    assert len(final["error"]) <= 600
    assert final["error"].startswith("出片没有产出任何 PPTX")   # 结论句仍在最前
    assert "--to render" in final["log_tail"][0]      # 命令行仍是首行
    assert not any("✅" in line for line in final["log_tail"])   # 不许自相矛盾地报完成


def test_exit_zero_with_pptx_but_no_course_keeps_success(client, fake_flow, tmp_path,
                                                          monkeypatch):
    """有产物但课程没登记上：仍然是成功，只是把"未登记"说清楚。"""
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.01")
    # 产物写到课堂扫的那个 runs 根之外 → 扫描扫不到，课程自然登记不上
    monkeypatch.setenv("FAKE_RUNS_ROOT", str(tmp_path / "outside"))
    gen_id = _create(client, stage="高中").json()["id"]
    final, _seen = _poll(client, gen_id)
    assert final["status"] == "succeeded"
    assert len(final["result"]["pptx"]) == 1
    assert final["result"]["scan"]["total"] == 0
    assert final["result"]["course_id"] == ""
    assert "未登记" in final["error"]
    assert "没有产出任何 PPTX" not in final["error"]


# ===========================================================================
# 列表 / 404
# ===========================================================================

def test_list_returns_items_and_active(client, fake_flow, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "0.4")
    gen_id = _create(client, stage="高中").json()["id"]
    data = client.get(f"{API}?limit=10").json()
    assert data["items"] and data["items"][0]["id"] == gen_id
    assert data["active"] is not None and data["active"]["id"] == gen_id

    _poll(client, gen_id)
    after = client.get(f"{API}?limit=10").json()
    assert after["active"] is None
    assert after["items"][0]["status"] == "succeeded"


def test_unknown_task_is_404(client):
    assert client.get(f"{API}/g-不存在").status_code == 404
    r = client.post(f"{API}/g-不存在/cancel")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


# ===========================================================================
# 重启对账
# ===========================================================================

def test_restart_marks_leftover_tasks_interrupted():
    gen_id = _seed_active("running")
    _seed_active("queued", gen_id="g-leftover-2")
    assert generation.reconcile() == 2

    conn = _conn()
    try:
        row = dict(conn.execute("SELECT * FROM generations WHERE id=?",
                                (gen_id,)).fetchone())
    finally:
        conn.close()
    snap = generation._row_snapshot(row)
    assert snap["status"] == "interrupted"
    assert snap["finished_at"]
    assert "重启" in snap["error"]
    assert "interrupted" in snap["log_tail"][-1]
    assert snap["log_tail"][0].startswith("python ")      # 命令行首行不被对账挤掉
    assert snap["progress"]["percent"] == 45.0            # 进度如实停在原处，不假装跑完
    assert generation.reconcile() == 0                    # 幂等：结算过的不再动
