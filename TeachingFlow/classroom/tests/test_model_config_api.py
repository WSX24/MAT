"""模型服务商中枢：库里的真源 + 拉模型 + 默认模型 + 子进程环境出口。

几条测试纪律：

* 每例一个临时库（CLASSROOM_DB → tmp_path），绝不碰真库；
* 拉模型一律 monkeypatch 掉 httpx.get：这些用例验的是"三种返回形状怎么认、
  失败怎么报"，不是服务商有没有挂。跑测试不该需要网络，也不该因为某家 401 就红；
* export_env_file 的默认路径被指到 tmp 下：真跑一次就会改掉仓库根的
  PPT-Agent/.env，那是别人的配置文件；
* 明文 key 的判据用**整个响应体文本**去搜（不只是字段名）：字段改名、多嵌一层、
  顺手 dump 一下，都会被这一条抓住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

CLASSROOM = Path(__file__).resolve().parents[1]
if str(CLASSROOM) not in sys.path:              # 允许从任意 cwd 直接跑本文件
    sys.path.insert(0, str(CLASSROOM))

import modelconfig                                # noqa: E402
from store import db as db_mod                    # noqa: E402

API = "/api/v1/model-providers"
DEFAULT_API = "/api/v1/model-default"

KEY = "sk-abcdefghijklmnop-1234"                 # 21 位，后 4 位是 -1234 之外的 1234
BASE = "https://api.deepseek.com/v1"

PROVIDER = {"name": "DeepSeek", "kind": "deepseek", "base_url": BASE,
            "api_key": KEY, "models": ["deepseek-chat"], "models_source": "provider"}


# ===========================================================================
# 夹具
# ===========================================================================

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每例：独立库、独立 static 目录、.env 落盘位置指到 tmp 下。"""
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "classroom.sqlite"))
    monkeypatch.delenv("CLASSROOM_DEV", raising=False)
    # 默认路径也指走：POST /model-providers/export-env 不带 path，
    # 不打这个补丁的话它会去改仓库根那份真的 .env
    monkeypatch.setattr(modelconfig, "repo_env_file", lambda: tmp_path / "repo" / ".env")
    from app import deps

    monkeypatch.setattr(deps, "STATE_ROOT", tmp_path / "state", raising=False)
    monkeypatch.setattr(deps, "DIST_ROOT", tmp_path / "dist", raising=False)
    deps.reset_settings_cache()
    yield
    deps.reset_settings_cache()


@pytest.fixture()
def conn(_isolate):
    connection = db_mod.init_db()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def client(_isolate):
    from app import create_app

    with TestClient(create_app()) as c:
        yield c


# ===========================================================================
# 替身：假 httpx（三种返回形状 / 各种失败）
# ===========================================================================

class _Resp:
    def __init__(self, payload=None, status_code=200, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


def _fake_get(monkeypatch, *, payload=None, status_code=200, exc=None, text=None):
    calls: list[dict] = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        if exc is not None:
            raise exc
        return _Resp(payload, status_code, text)

    monkeypatch.setattr(modelconfig.httpx, "get", fake_get)
    return calls


def _make(client) -> dict:
    resp = client.post(API, json=PROVIDER)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# 契约形状 / 掩码
# ===========================================================================

def test_list_providers_empty_contract(client):
    """空库：items 是空数组、default 是 null（页面首屏就靠这一条）。"""
    resp = client.get(API)
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "default": None}


def test_create_provider_returns_public_shape(client):
    body = _make(client)
    assert set(body) == {"id", "name", "kind", "base_url", "api_key_masked", "has_key",
                        "models", "models_source", "created_at", "updated_at"}
    assert body["id"].startswith("pv-") and len(body["id"]) == 11
    assert body["name"] == "DeepSeek" and body["kind"] == "deepseek"
    assert body["base_url"] == BASE
    assert body["models"] == ["deepseek-chat"] and body["models_source"] == "provider"
    assert body["has_key"] is True
    assert client.get(API).json()["items"][0]["id"] == body["id"]


def test_masked_key_never_leaks_in_http(client):
    """明文 key 不出 HTTP：字段没了、整段响应体里也搜不到。"""
    created = _make(client)
    assert created["api_key_masked"].endswith(KEY[-4:])
    assert created["api_key_masked"] != KEY
    assert len(created["api_key_masked"]) == 5                # 省略号 + 后 4 位

    # 默认那一路同样不能带明文（get_default 手里的 provider 是含明文的内部形状）
    assert client.post(DEFAULT_API, json={"provider_id": created["id"],
                                          "model": "deepseek-chat"}).status_code == 200
    for resp in (client.get(API),
                 client.patch(f"{API}/{created['id']}", json={"name": "改名"}),
                 client.put(f"{API}/{created['id']}/models", json={"models": ["m"]})):
        assert resp.status_code in (200, 201), resp.text
        assert KEY not in resp.text
        assert '"api_key"' not in resp.text            # 连明文字段名都不该出现
        assert created["id"] in resp.text
    for item in client.get(API).json()["items"]:
        assert "api_key" not in item


def test_short_key_masked_as_stars(client):
    """短于 8 位一律 ****（露 4 位等于露掉一半）。"""
    body = client.post(API, json={**PROVIDER, "api_key": "sk-1234"}).json()
    assert body["api_key_masked"] == "****"
    assert body["has_key"] is True


def test_no_key_means_empty_mask(client):
    body = client.post(API, json={**PROVIDER, "api_key": "", "kind": "ollama",
                                  "base_url": "http://127.0.0.1:11434/v1"}).json()
    assert body["api_key_masked"] == "" and body["has_key"] is False


# ===========================================================================
# CRUD
# ===========================================================================

def test_patch_provider_keeps_key_when_absent(client):
    """PATCH 不带 api_key = 不改（不是"清空"）。"""
    created = _make(client)
    resp = client.patch(f"{API}/{created['id']}",
                        json={"name": "DeepSeek 官方", "models": ["a", "b"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "DeepSeek 官方"
    assert body["has_key"] is True
    assert body["api_key_masked"] == created["api_key_masked"]
    assert body["models"] == ["a", "b"]
    assert body["base_url"] == BASE                   # 没碰的字段保持原样


def test_patch_provider_clears_key_with_empty_string(client):
    """PATCH 带 api_key='' = 清空（界面上"删掉这把 key"必须做得到）。"""
    created = _make(client)
    body = client.patch(f"{API}/{created['id']}", json={"api_key": ""}).json()
    assert body["has_key"] is False and body["api_key_masked"] == ""
    assert client.get(API).json()["items"][0]["has_key"] is False


def test_patch_null_key_means_unchanged(client):
    """前端把整份表单发上来时，api_key: null 必须当成"没给"，不能把密钥清掉。"""
    created = _make(client)
    body = client.patch(f"{API}/{created['id']}",
                        json={"name": "改名", "api_key": None}).json()
    assert body["name"] == "改名" and body["has_key"] is True
    assert body["api_key_masked"] == created["api_key_masked"]


def test_delete_provider_204_then_404(client):
    created = _make(client)
    resp = client.delete(f"{API}/{created['id']}")
    assert resp.status_code == 204 and resp.content == b""
    assert client.get(API).json()["items"] == []
    assert client.patch(f"{API}/{created['id']}", json={"name": "x"}).status_code == 404
    assert client.delete(f"{API}/{created['id']}").status_code == 404


# ===========================================================================
# 默认服务商 / 默认模型
# ===========================================================================

def test_set_default_and_read_back(client):
    created = _make(client)
    resp = client.post(DEFAULT_API, json={"provider_id": created["id"],
                                          "model": "deepseek-chat"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"provider_id", "model", "provider", "warning"}
    assert body["provider_id"] == created["id"]
    assert body["model"] == "deepseek-chat"
    assert body["warning"] == ""
    assert body["provider"]["api_key_masked"] == created["api_key_masked"]

    listed = client.get(API).json()["default"]
    assert listed["provider_id"] == created["id"] and listed["model"] == "deepseek-chat"
    assert listed["provider"]["has_key"] is True


def test_default_warns_when_model_not_in_list(client):
    """模型名手滑时立刻提示：等出片才报错就太晚了。"""
    created = _make(client)
    body = client.post(DEFAULT_API, json={"provider_id": created["id"],
                                          "model": "deepseek-caht"}).json()
    assert body["model"] == "deepseek-caht" and body["warning"]


def test_delete_provider_clears_default(client):
    created = _make(client)
    client.post(DEFAULT_API, json={"provider_id": created["id"], "model": "deepseek-chat"})
    client.delete(f"{API}/{created['id']}")
    assert client.get(API).json()["default"] is None


def test_get_default_self_heals_dangling_pointer(conn):
    """默认指向的服务商被删（绕过 delete_provider 直接删行）→ None 且库里的默认一并清掉。"""
    provider = modelconfig.create_provider(conn, name="DeepSeek", kind="deepseek",
                                           base_url=BASE, api_key=KEY,
                                           models=["deepseek-chat"])
    modelconfig.set_default(conn, provider["id"], "deepseek-chat")
    assert modelconfig.get_default(conn) is not None

    conn.execute("DELETE FROM model_providers WHERE id=?", (provider["id"],))
    conn.commit()

    assert modelconfig.get_default(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 0


def test_default_falls_back_to_first_model(conn):
    """默认模型留空但服务商有清单：用第一条顶上（flow.py 不能拿到空模型名）。"""
    provider = modelconfig.create_provider(conn, name="Ollama", kind="ollama",
                                           base_url="http://127.0.0.1:11434/v1",
                                           api_key="", models=["qwen2.5:7b", "llama3"])
    modelconfig.set_default(conn, provider["id"], "qwen2.5:7b")
    conn.execute("DELETE FROM app_settings WHERE key='default_model'")
    conn.commit()
    assert modelconfig.get_default(conn)["model"] == "qwen2.5:7b"


# ===========================================================================
# 拉模型清单
# ===========================================================================

@pytest.mark.parametrize("payload,expected", [
    ({"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]},
     ["deepseek-chat", "deepseek-reasoner"]),
    ({"models": [{"name": "qwen2.5:7b"}, {"name": "llama3"}]},
     ["llama3", "qwen2.5:7b"]),
    ({"models": ["a-model", "b-model"]}, ["a-model", "b-model"]),
])
def test_fetch_models_accepts_three_shapes(monkeypatch, payload, expected):
    calls = _fake_get(monkeypatch, payload=payload)
    assert modelconfig.fetch_models(BASE, KEY) == expected
    assert calls[0]["url"] == BASE + "/models"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert calls[0]["headers"]["Accept-Encoding"] == "identity"
    assert calls[0]["timeout"] == 10.0


def test_fetch_models_dedupes_and_sorts(monkeypatch):
    _fake_get(monkeypatch, payload={"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]})
    assert modelconfig.fetch_models(BASE, KEY) == ["a", "b"]


def test_fetch_models_sends_no_auth_without_key(monkeypatch):
    """Ollama 那种没 key 的：不能塞一个空的 Bearer（有的实现直接判 401）。"""
    calls = _fake_get(monkeypatch, payload={"models": []})
    assert modelconfig.fetch_models("http://127.0.0.1:11434/v1", "") == []
    assert "Authorization" not in calls[0]["headers"]


def test_fetch_models_error_carries_base_url(monkeypatch):
    """401 → ModelFetchError，message 里有 base_url 与失败原因（用户要照着改配置）。"""
    _fake_get(monkeypatch, status_code=401, text="{}")
    with pytest.raises(modelconfig.ModelFetchError) as err:
        modelconfig.fetch_models(BASE, "sk-wrong")
    message = str(err.value)
    assert BASE in message and "401" in message


def test_fetch_models_timeout_carries_base_url(monkeypatch):
    _fake_get(monkeypatch, exc=httpx.TimeoutException("timed out"))
    with pytest.raises(modelconfig.ModelFetchError) as err:
        modelconfig.fetch_models(BASE, KEY, timeout=1.5)
    message = str(err.value)
    assert BASE in message and "超时" in message and "1.5" in message


def test_fetch_models_https_502_upstream_error(client, monkeypatch):
    """HTTP 面：拉模型失败是 502 + code=upstream_error，不是 500。"""
    created = _make(client)
    _fake_get(monkeypatch, status_code=401, text="{}")
    resp = client.post(f"{API}/{created['id']}/models")
    assert resp.status_code == 502
    body = resp.json()
    assert body["ok"] is False and body["code"] == "upstream_error"
    assert BASE in body["message"] and "401" in body["message"]


def test_fetch_models_endpoint_caches_provider_source(client, monkeypatch):
    created = _make(client)
    _fake_get(monkeypatch, payload={"data": [{"id": "m2"}, {"id": "m1"}]})
    resp = client.post(f"{API}/{created['id']}/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == ["m1", "m2"]
    assert body["models_source"] == "provider"
    assert body["fetched_at"]
    cached = client.get(API).json()["items"][0]
    assert cached["models"] == ["m1", "m2"]
    assert cached["models_source"] == "provider"


def test_save_models_manual(client):
    created = _make(client)
    resp = client.put(f"{API}/{created['id']}/models", json={"models": ["x", "y", "x"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == ["x", "y"] and body["models_source"] == "manual"
    assert client.put(f"{API}/pv-nope/models", json={"models": []}).status_code == 404


def test_save_models_rejects_bad_shape(client):
    created = _make(client)
    assert client.put(f"{API}/{created['id']}", json={}).status_code == 405
    resp = client.put(f"{API}/{created['id']}/models", json={"models": "不是数组"})
    assert resp.status_code == 400 and resp.json()["code"] == "bad_request"


# ===========================================================================
# 出口：子进程环境 / .env
# ===========================================================================

def test_env_for_subprocess_without_default_is_empty(conn):
    modelconfig.create_provider(conn, name="DeepSeek", kind="deepseek",
                                base_url=BASE, api_key=KEY)
    assert modelconfig.env_for_subprocess(conn) == {}


def test_env_for_subprocess_maps_default(conn):
    """默认服务商 → flow.py 唯一认识的三个键（ollama 也走这套：它也是 OpenAI 兼容端点）。"""
    provider = modelconfig.create_provider(
        conn, name="Ollama", kind="ollama", base_url="http://127.0.0.1:11434/v1",
        api_key="", models=["qwen2.5:7b"])
    modelconfig.set_default(conn, provider["id"], "qwen2.5:7b")
    env = modelconfig.env_for_subprocess(conn)
    assert env == {"DEEPSEEK_BASE_URL": "http://127.0.0.1:11434/v1",
                   "DEEPSEEK_MODEL": "qwen2.5:7b"}
    # 没配 key 就不该塞一个空的 DEEPSEEK_API_KEY（那会把别人的真 key 顶掉）
    assert "DEEPSEEK_API_KEY" not in env


def test_export_env_file_is_idempotent_and_keeps_other_lines(conn, tmp_path):
    env_file = tmp_path / "PPT-Agent" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    original = ("# 这份文件的注释是说明书，一个字节都不许动\n"
                "OTHER_KEY=keep-me\n"
                "DEEPSEEK_API_KEY=old-key\n"
                "\n"
                "DEEPSEEK_MODEL=old-model\n")
    env_file.write_text(original, encoding="utf-8")

    provider = modelconfig.create_provider(conn, name="DeepSeek", kind="deepseek",
                                           base_url=BASE, api_key=KEY)
    modelconfig.set_default(conn, provider["id"], "deepseek-chat")

    first = modelconfig.export_env_file(conn, env_file)
    text = env_file.read_text(encoding="utf-8")
    assert sorted(first["written"]) == sorted(modelconfig.ENV_KEYS)
    assert first["skipped"] == []
    assert text.splitlines()[0] == original.splitlines()[0]      # 注释原样
    assert "OTHER_KEY=keep-me" in text                          # 无关键原样
    assert f"DEEPSEEK_API_KEY={KEY}" in text                    # 值被换掉
    assert "old-key" not in text and "old-model" not in text
    assert f"DEEPSEEK_BASE_URL={BASE}" in text                   # 缺的键被追加

    second = modelconfig.export_env_file(conn, env_file)
    assert env_file.read_text(encoding="utf-8") == text          # 幂等：一个字节都不变
    assert second["written"] == first["written"]


def test_export_env_file_without_default_touches_nothing(conn, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# 别动我\nFOO=1\n", encoding="utf-8")
    result = modelconfig.export_env_file(conn, env_file)
    assert result["written"] == [] and sorted(result["skipped"]) == sorted(modelconfig.ENV_KEYS)
    assert env_file.read_text(encoding="utf-8") == "# 别动我\nFOO=1\n"


def test_export_env_endpoint_uses_default_path(client):
    """不带 path 时写到 repo_env_file()（夹具已把它指到 tmp 下）。"""
    created = _make(client)
    client.post(DEFAULT_API, json={"provider_id": created["id"], "model": "deepseek-chat"})
    resp = client.post(f"{API}/export-env")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"].endswith(".env")
    assert sorted(body["written"]) == sorted(modelconfig.ENV_KEYS)
    written = Path(body["path"]).read_text(encoding="utf-8")
    assert f"DEEPSEEK_API_KEY={KEY}" in written


# ===========================================================================
# 错误码
# ===========================================================================

@pytest.mark.parametrize("payload,needle", [
    ({**PROVIDER, "name": ""}, "名称"),
    ({**PROVIDER, "kind": "nope"}, "类型"),
    ({**PROVIDER, "base_url": ""}, "base_url"),
    ({**PROVIDER, "base_url": "api.deepseek.com/v1"}, "http"),
    ({**PROVIDER, "models": "不是数组"}, "models"),
])
def test_create_provider_bad_input_400(client, payload, needle):
    resp = client.post(API, json=payload)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False and body["code"] == "bad_request"
    assert needle in body["message"]


def test_model_default_bad_input_400(client):
    created = _make(client)
    assert client.post(DEFAULT_API, json={}).status_code == 400
    assert client.post(DEFAULT_API,
                       json={"provider_id": created["id"], "model": ""}).status_code == 400
    missing = client.post(DEFAULT_API, json={"provider_id": "pv-nope",
                                             "model": "m"})
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_provider_routes_404(client):
    assert client.patch(f"{API}/pv-nope", json={"name": "x"}).status_code == 404
    assert client.delete(f"{API}/pv-nope").status_code == 404
    assert client.post(f"{API}/pv-nope/models").status_code == 404
