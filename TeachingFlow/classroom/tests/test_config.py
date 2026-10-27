"""服务商配置测试 —— 配置错了，整节课都在跟错的服务商说话。

为什么单独立一个文件：`opencode_go`（订阅套餐）和 `opencode`（Zen，按量付费）
是**两条计费通道**，端点只差一段 `/go`。这类"差一点点"的配置最容易在改动中
悄悄串线，而且串线之后一切看起来都正常 —— 只是钱从另一条通道走。
另外 Go 强制要求 `x-opencode-session` 请求头（缺了直接 400 MissingSessionID，
连 key 对不对都轮不到判断），这条外部契约必须被测试钉住。
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    apply_env_values,
    load_settings,
    save_env_values,
)

GO_BASE_URL = "https://opencode.ai/zen/go/v1"
GO_MODEL = "deepseek-v4.1-flash"


@contextmanager
def env(**kv):
    """临时改环境变量（None = 删掉），退出时原样恢复。"""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_opencode_go_uses_the_go_endpoint() -> None:
    with env(OPENCODE_GO_BASE_URL=None, OPENCODE_GO_MODEL=None):
        p = load_settings().providers["opencode_go"]
    assert p.base_url == GO_BASE_URL, f"opencode_go 的端点不是 Go 通道：{p.base_url}"
    assert p.model == GO_MODEL, f"opencode_go 的默认模型应为 {GO_MODEL}：{p.model}"
    # 与 Zen 必须是不同的地址，否则订阅额度和按量付费就串了
    zen = load_settings().providers["opencode"]
    assert zen.base_url != p.base_url, "Go 与 Zen 的端点不该相同"
    print("[C1] opencode_go 指向 Go 订阅通道，默认模型 DeepSeek V4.1")


def test_go_key_falls_back_to_zen_key() -> None:
    with env(OPENCODE_GO_API_KEY=None, OPENCODE_API_KEY="sk-zen"):
        assert load_settings().providers["opencode_go"].api_key == "sk-zen", \
            "没单独配 Go 的 key 时应回落到 OPENCODE_API_KEY"
    with env(OPENCODE_GO_API_KEY="sk-go", OPENCODE_API_KEY="sk-zen"):
        assert load_settings().providers["opencode_go"].api_key == "sk-go", \
            "单独配了 Go 的 key 时必须优先用它"
    print("[C2] 密钥优先级：OPENCODE_GO_API_KEY → OPENCODE_API_KEY")


def test_go_sends_session_header_and_others_do_not() -> None:
    s = load_settings()
    go = s.providers["opencode_go"]
    assert go.headers.get("x-opencode-session"), \
        "OpenCode Go 缺 x-opencode-session 会被直接 400（MissingSessionID）"
    for name in ("ollama", "opencode"):
        assert not s.providers[name].headers, f"{name} 不该带 Go 的专用请求头"
    print("[C3] 只有 opencode_go 带 x-opencode-session")


def test_session_can_be_pinned_and_reaches_the_client() -> None:
    with env(LLM_PROVIDER="opencode_go", OPENCODE_GO_API_KEY="sk-x",
             OPENCODE_GO_SESSION="sess-fixed"):
        from llm import LLM

        client = LLM(load_settings()).client
        pinned = load_settings().providers["opencode_go"].headers["x-opencode-session"]
    assert pinned == "sess-fixed", f"OPENCODE_GO_SESSION 没生效：{pinned}"
    sent = dict(getattr(client, "default_headers", {}) or {})
    assert sent.get("x-opencode-session") == "sess-fixed", \
        "配置里的会话头没有真的进到 HTTP 客户端（等于没加）"
    print("[C4] 会话头可钉住，且真的进了客户端请求头")


def test_unknown_provider_lists_the_choices() -> None:
    with env(LLM_PROVIDER="nope"):
        tray = None
        try:
            load_settings().active
        except SystemExit as e:
            tray = str(e)
        assert tray is not None, "未知服务商必须 fail-fast，不能悄悄退化成别的通道"
        assert "opencode_go" in tray, f"报错里要列出可选服务商：{tray}"
    print("[C5] 未知服务商报错并列出全部可选")


def test_env_rewrite_is_surgical_and_keeps_crlf() -> None:
    """GUI 保存只能动受管的那几行：注释、其它键、换行符都得原样。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_bytes("# 注释：端口为什么改过\r\nLLM_PROVIDER=ollama\r\n"
                      "OLLAMA_MODEL=old\r\nTTS_ENABLED=1\r\n".encode("utf-8"))
        save_env_values(p, {"LLM_PROVIDER": "opencode_go", "OLLAMA_MODEL": "new"})
        raw = p.read_bytes().decode("utf-8")
    assert "# 注释：端口为什么改过" in raw, "注释被写没了（.env 的说明书就在这里）"
    assert "TTS_ENABLED=1" in raw, "不受管的键被动了"
    assert "LLM_PROVIDER=opencode_go" in raw and "OLLAMA_MODEL=new" in raw
    assert "OLLAMA_MODEL=old" not in raw
    assert raw.count("\r\n") >= 4 and "\n" not in raw.replace("\r\n", ""), \
        "CRLF 必须保留 —— 否则整份文件每一行都算改动，diff 看不出真正改了哪两行"
    print("[C6] 写回 .env 只动受管键，注释与 CRLF 原样保留")


def test_env_rewrite_appends_missing_keys_once() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("# 带头注释\nLLM_PROVIDER=ollama\n", encoding="utf-8")
        save_env_values(p, {"OPENCODE_GO_MODEL": "deepseek-v4.1-flash"})
        first = p.read_text(encoding="utf-8")
        save_env_values(p, {"OPENCODE_GO_MODEL": "deepseek-v4.1-flash"})
        second = p.read_text(encoding="utf-8")
    assert "OPENCODE_GO_MODEL=deepseek-v4.1-flash" in first, "缺的键要追加进去"
    assert first.count("OPENCODE_GO_MODEL=") == 1, "同一个键不能写成两行"
    assert first == second, "重复保存不该改变文件（幂等）"
    print("[C7] 缺的键追加一次，重复保存幂等")


def test_apply_env_values_beats_dotenv() -> None:
    """只写文件不生效：dotenv 不会覆盖进程里已有的环境变量。"""
    with env(LLM_PROVIDER="ollama"):
        assert load_settings().provider == "ollama"
        apply_env_values({"LLM_PROVIDER": "opencode_go"})
        assert load_settings().provider == "opencode_go", \
            "写进 os.environ 的新值必须立刻对 load_settings 生效"
    print("[C8] 保存后当场生效（不被 dotenv 的旧值挡住）")


def test_env_rewrite_does_not_introduce_crlf() -> None:
    """LF 的 .env 不能被写成 CRLF：一次保存就整份文件全变（Windows 上真踩过）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_bytes(b"LLM_PROVIDER=ollama\nTTS_ENABLED=1\n")
        save_env_values(p, {"LLM_PROVIDER": "opencode_go"})
        got = p.read_bytes()
    assert got.count(b"\r\n") == 0, "不能凭空引入 CRLF（write_text 的默认行为会）"
    assert got == b"LLM_PROVIDER=opencode_go\nTTS_ENABLED=1\n", f"写回结果不忠实：{got!r}"
    print("[C9] LF 文件写回仍是 LF（不引入 CRLF）")


ALL = [
    test_opencode_go_uses_the_go_endpoint,
    test_go_key_falls_back_to_zen_key,
    test_go_sends_session_header_and_others_do_not,
    test_session_can_be_pinned_and_reaches_the_client,
    test_unknown_provider_lists_the_choices,
    test_env_rewrite_is_surgical_and_keeps_crlf,
    test_env_rewrite_appends_missing_keys_once,
    test_apply_env_values_beats_dotenv,
    test_env_rewrite_does_not_introduce_crlf,
]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
    print(f"\n服务商配置结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
