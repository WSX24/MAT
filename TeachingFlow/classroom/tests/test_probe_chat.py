"""「测试对话」探测：只有真发一次请求，才看得出余额/额度这类问题。

为什么单开一个文件：这里验的是"状态码怎么翻成人话、200 但不像对话怎么判"，
与 test_model_config_api.py 的 CRUD/拉清单关注点不同；两者都用临时库与
monkeypatch 掉的 httpx，不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

CLASSROOM = Path(__file__).resolve().parents[1]
if str(CLASSROOM) not in sys.path:
    sys.path.insert(0, str(CLASSROOM))

import modelconfig                                # noqa: E402
from store import db as db_mod                    # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "classroom.sqlite"))
    c = db_mod.connect()
    db_mod.migrate(c)
    yield c
    c.close()


def _provider(conn, **over) -> dict:
    fields = {"name": "DeepSeek", "kind": "deepseek", "base_url": "https://api.deepseek.com/v1",
              "api_key": "sk-test-abcdefgh", "models": ["deepseek-chat"],
              "models_source": "provider"}
    fields.update(over)
    return modelconfig.create_provider(conn, **fields)


class _Resp:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_probe_ok(conn, monkeypatch):
    sent: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers or {}, body=json or {})
        return _Resp(200, {"id": "chatcmpl-1", "object": "chat.completion",
                           "choices": [{"message": {"content": "p"}}]})

    monkeypatch.setattr(modelconfig.httpx, "post", fake_post)
    pid = _provider(conn)["id"]
    out = modelconfig.probe_chat(conn, pid)
    assert out["ok"] is True and out["status"] == 200
    assert "deepseek-chat" in out["message"]
    assert out["hint"] == ""
    # 出站请求的三个关键点：路径、不许压缩、带鉴权
    assert sent["url"].endswith("/chat/completions")
    assert sent["headers"]["Accept-Encoding"] == "identity"
    assert sent["headers"]["Authorization"] == "Bearer sk-test-abcdefgh"
    assert sent["body"]["max_tokens"] == 1 and sent["body"]["model"] == "deepseek-chat"


@pytest.mark.parametrize("status,needle", [(401, "API Key"), (402, "余额"),
                                           (403, "权限"), (404, "base_url"),
                                           (429, "额度")])
def test_probe_maps_status_to_human(conn, monkeypatch, status, needle):
    monkeypatch.setattr(modelconfig.httpx, "post", lambda *a, **k: _Resp(status, {"error": "x"}))
    pid = _provider(conn)["id"]
    out = modelconfig.probe_chat(conn, pid)
    assert out["ok"] is False and out["status"] == status
    assert needle in out["message"]
    assert out["hint"], "每个状态码都要给出下一步动作，不能只说数字"


def test_probe_402_says_where_to_pay(conn, monkeypatch):
    """用户真机上遇到的正是这条：402 Insufficient Balance。"""
    monkeypatch.setattr(modelconfig.httpx, "post",
                        lambda *a, **k: _Resp(402, {"error": {"message": "Insufficient Balance"}}))
    out = modelconfig.probe_chat(conn, _provider(conn)["id"])
    assert out["status"] == 402
    assert "充值" in out["hint"]


def test_probe_connection_error(conn, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(modelconfig.httpx, "post", boom)
    out = modelconfig.probe_chat(conn, _provider(conn)["id"])
    assert out["ok"] is False and out["status"] == 0
    assert "连不上" in out["message"] and "api.deepseek.com" in out["message"]


def test_probe_timeout(conn, monkeypatch):
    def slow(*a, **k):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(modelconfig.httpx, "post", slow)
    out = modelconfig.probe_chat(conn, _provider(conn)["id"])
    assert out["ok"] is False and out["status"] == 0 and "没有响应" in out["message"]


def test_probe_without_any_model(conn, monkeypatch):
    monkeypatch.setattr(modelconfig.httpx, "post", lambda *a, **k: _Resp(200, {}))
    pid = _provider(conn, models=[], models_source="")["id"]
    out = modelconfig.probe_chat(conn, pid)
    assert out["ok"] is False and "还没有模型" in out["message"]
    assert "拉取模型" in out["hint"]


def test_probe_200_but_not_a_completion(conn, monkeypatch):
    """有些中转 200 返回错误 JSON —— 不能当成成功。"""
    monkeypatch.setattr(modelconfig.httpx, "post",
                        lambda *a, **k: _Resp(200, {"error": "invalid request"}, text="{\"error\": \"invalid request\"}"))
    out = modelconfig.probe_chat(conn, _provider(conn)["id"])
    assert out["ok"] is False and out["status"] == 200
    assert "不像一次对话" in out["message"]


def test_probe_unknown_provider_raises(conn):
    with pytest.raises(KeyError):
        modelconfig.probe_chat(conn, "pv-nope")
