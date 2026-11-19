"""库里的默认模型服务商怎么接进出片与课堂两条链路。

钉住四件事（这四件事以前各走各的，出片看 .env、课堂看 LLM_PROVIDER）：

1. 出片的前置条件改成问库：没有可用默认 → 400 且 code=no_provider（页面按这个码
   直接弹「设置 · 模型服务商」）；dev 下仍能 --mock 出去，那是"只想先看界面"的逃生舱；
2. 真出片时，子进程环境里的 DEEPSEEK_* 三项来自库 —— 这条只能靠**假子进程**把
   它真拿到的 os.environ 打回盘上才算证据，断言命令拼装是断不出来的；
3. 课堂引擎 config.load_settings()：库 > env > 内置默认；库里没有默认时行为与今天
   完全一致（env 优先），那是 500 多条既有用例能继续绿的前提；
4. 两个 AsyncOpenAI 构造点把 identity 头**并进**服务商固定头里 —— 覆盖掉的话
   OpenCode Go 的 x-opencode-session 就没了，课堂当场 400。

纪律：每例一个临时库（CLASSROOM_DB）、假流水线、不联网、不碰仓库根那份 .env。
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

import config                                     # noqa: E402
import generation                                  # noqa: E402
import modelconfig                                 # noqa: E402
from store import db as db_mod                     # noqa: E402

API = "/api/v1/generations"
GOAL = "两周突击高中物理的受力分析"
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")

#: 假流水线：把子进程**真正**拿到的 DEEPSEEK_* 三项与 PYTHONUTF8 写回 run 目录，
#: 再写一份假 pptx + manifest 让成功路径能走完。不调模型，也不联网。
FAKE_FLOW = '''
import json, os
from pathlib import Path

run = Path(os.environ["FAKE_RUNS_ROOT"]) / "20260101" / "注入测试_120000"
chapter = run / "第1章-入门"
chapter.mkdir(parents=True, exist_ok=True)
pptx = chapter / "第1单元-引言.pptx"
pptx.write_bytes(b"PK-not-a-real-pptx")
(run / "manifest.json").write_text(json.dumps({"produced": [str(pptx)]}), encoding="utf-8")
(run / "env.json").write_text(json.dumps(
    {k: os.environ.get(k, "") for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
                                        "DEEPSEEK_MODEL", "PYTHONUTF8")}), encoding="utf-8")
for i in range(6):
    print("【S%d】假阶段 %d" % (i, i), flush=True)
print("  产物目录：" + str(run), flush=True)
'''


# ===========================================================================
# 夹具
# ===========================================================================

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每例：独立库、独立 runs 根、清空的内存活任务表、不继承任何 DEEPSEEK_*。"""
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "classroom.sqlite"))
    monkeypatch.setenv("FAKE_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.delenv("CLASSROOM_DEV", raising=False)
    for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(key, raising=False)

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

def _conn():
    conn = db_mod.connect()
    db_mod.migrate(conn)
    return conn


def _seed_default(*, kind="deepseek", base_url="https://db.example/v1",
                  api_key="sk-from-db-0001", model="deepseek-from-db") -> str:
    """在（临时的）库里建一家服务商并设为默认，返回 provider id。"""
    conn = _conn()
    try:
        provider = modelconfig.create_provider(
            conn, name="库里的服务商", kind=kind, base_url=base_url,
            api_key=api_key, models=[model] if model else [])
        modelconfig.set_default(conn, provider["id"], model)
        return provider["id"]
    finally:
        conn.close()


def _create(client, **body):
    body.setdefault("goal", GOAL)
    body.setdefault("stage", "高中")
    return client.post(API, json=body)


def _poll(client, gen_id, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"{API}/{gen_id}").json()
        if snap["status"] in TERMINAL:
            return snap
        time.sleep(0.03)
    raise AssertionError(f"{timeout}s 内没到终态：{gen_id}")


# ===========================================================================
# 出片前置：真源是库
# ===========================================================================

def test_no_default_outside_dev_is_400_no_provider(client):
    """库里没有默认服务商 → 400 no_provider（页面据此弹「去设置里配」）。"""
    r = _create(client)
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False and body["code"] == "no_provider"
    assert "模型服务商" in body["message"] and "设置" in body["message"]
    assert "DEEPSEEK_API_KEY" not in body["message"], \
        "真源已经搬进库，指 .env 是一个错的去处"

    view = client.get(f"{API}/config").json()
    assert set(view) == {"ready", "has_key", "model", "base_url", "mock_allowed",
                         "mode", "reason"}
    assert view["ready"] is False and view["has_key"] is False and view["mode"] == "real"
    assert "模型服务商" in view["reason"]


def test_no_default_in_dev_falls_back_to_mock(client, fake_flow, monkeypatch):
    """dev 逃生舱：没有服务商也允许 --mock 出片，理由里写清"只验链路"。"""
    monkeypatch.setenv("CLASSROOM_DEV", "1")
    r = _create(client)
    assert r.status_code == 202
    snap = r.json()
    assert snap["mode"] == "mock"
    assert "--mock" in snap["log_tail"][0]
    view = client.get(f"{API}/config").json()
    assert view["ready"] is True and view["mock_allowed"] is True and view["mode"] == "mock"
    assert "CLASSROOM_DEV" in view["reason"]
    assert _poll(client, snap["id"])["status"] == "succeeded"


def test_default_without_key_is_400_no_provider(client):
    """有默认但没填 key：同样是 no_provider，话术指向设置页把 key 填上。"""
    _seed_default(api_key="")
    r = _create(client)
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "no_provider"
    assert "API Key" in body["message"] and "设置" in body["message"]
    view = client.get(f"{API}/config").json()
    assert view["has_key"] is False and view["ready"] is False
    assert view["model"] == "deepseek-from-db" and view["base_url"] == "https://db.example/v1"


# ===========================================================================
# 真出片：库里那三项真的进了子进程
# ===========================================================================

def test_default_key_and_model_reach_the_subprocess_env(client, fake_flow, tmp_path):
    """子进程 env 里的三个键来自库（假脚本把它真拿到的 os.environ 打回盘上）。"""
    _seed_default(kind="deepseek", base_url="http://127.0.0.1:9/db/v1",
                  api_key="sk-from-db-0001", model="deepseek-from-db")
    r = _create(client)
    assert r.status_code == 202
    snap = r.json()
    assert snap["mode"] == "real"
    assert "--mock" not in snap["log_tail"][0]

    view = client.get(f"{API}/config").json()
    assert view["ready"] is True and view["has_key"] is True and view["mode"] == "real"
    assert view["model"] == "deepseek-from-db"
    assert view["base_url"] == "http://127.0.0.1:9/db/v1"

    assert _poll(client, snap["id"])["status"] == "succeeded"
    env_file = tmp_path / "runs" / "20260101" / "注入测试_120000" / "env.json"
    env = json.loads(env_file.read_text(encoding="utf-8"))
    assert env == {"DEEPSEEK_API_KEY": "sk-from-db-0001",
                   "DEEPSEEK_BASE_URL": "http://127.0.0.1:9/db/v1",
                   "DEEPSEEK_MODEL": "deepseek-from-db",
                   "PYTHONUTF8": "1"}


# ===========================================================================
# 课堂引擎：库 > env > 内置默认
# ===========================================================================

def test_load_settings_takes_base_url_and_model_from_the_db_default(monkeypatch):
    """库里有默认时它盖过 LLM_PROVIDER，且 active 取到的就是库里那一份。"""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    _seed_default(kind="deepseek", base_url="http://127.0.0.1:9/db/v1",
                  api_key="sk-from-db-0001", model="deepseek-from-db")
    s = config.load_settings()
    assert s.provider == "deepseek"
    assert s.active.base_url == "http://127.0.0.1:9/db/v1"
    assert s.active.model == "deepseek-from-db"
    assert s.active.api_key == "sk-from-db-0001"


def test_load_settings_without_db_default_keeps_env_priority(monkeypatch):
    """库里没有默认时一个字都不改：provider 与 model 仍然由 env 说话。"""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "env-里的模型")
    s = config.load_settings()
    assert s.provider == "ollama"
    assert s.providers["ollama"].model == "env-里的模型"
    assert set(s.providers) == {"ollama", "opencode", "opencode_go"}


def test_unknown_kind_gets_a_custom_profile(monkeypatch):
    """kind 不在内置表里（openrouter 这类）：注入 custom 档案，思考口径给 off。"""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    _seed_default(kind="openrouter", base_url="https://openrouter.ai/api/v1",
                  api_key="sk-router-0001", model="openrouter/auto")
    s = config.load_settings()
    assert s.provider == "custom"
    assert s.active.name == "custom"
    assert s.active.base_url == "https://openrouter.ai/api/v1"
    assert s.active.model == "openrouter/auto"
    assert s.active.thinking_style == "off", \
        "没见过的端点不塞 reasoning_effort：多一个字段换来的是 400"


# ===========================================================================
# 传输：identity 头是并上去的，不是替换
# ===========================================================================

#: 文件 → 该文件里"原有头变量"的写法。两样必须同时出现在同一个构造块里。
CLIENT_FILES = {"llm.py": "self.profile.headers", "phrases.py": "prof.headers"}
IDENTITY = '\"Accept-Encoding\": \"identity\"'


def _client_blocks(path: Path) -> list[str]:
    """切出文件里每个 AsyncOpenAI(...) 的括号内文本（按括号配对，不做语法分析）。"""
    text = path.read_text(encoding="utf-8")
    found: list[str] = []
    start = 0
    while True:
        at = text.find("AsyncOpenAI(", start)
        if at < 0:
            return found
        depth = 0
        for i in range(at + len("AsyncOpenAI(") - 1, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    found.append(text[at + len("AsyncOpenAI("):i])
                    start = i
                    break


@pytest.mark.parametrize("name,headers_var", list(CLIENT_FILES.items()))
def test_clients_merge_identity_header_with_provider_headers(name, headers_var):
    blocks = _client_blocks(CLASSROOM / name)
    assert blocks, f"{name} 里一个 AsyncOpenAI( 都没有 —— 约定表过时了"
    for body in blocks:
        assert IDENTITY in body, (
            f"{name} 的客户端没要 identity 编码：压缩响应会让 httpx2 2.16.0 抛 "
            "TypeError: process() takes no keyword arguments")
        assert headers_var in body, (
            f"{name} 的 identity 头把 {headers_var} 顶掉了："
            "OpenCode Go 缺 x-opencode-session 会直接 400")
