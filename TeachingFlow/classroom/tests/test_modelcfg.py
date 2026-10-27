"""统一模型配置接口测试 —— 抓"面板里改了、上课用的还是旧的"这一类静默错。

为什么单独立一个文件：这四个分区（主模型 / 小模型 / ASR / TTS）原来散在三个面板里，
每处自己拼 .env 键、自己拼请求参数。这类代码错了**不会报错** —— 界面显示"已保存"，
而真正在跑的那份配置纹丝不动（或者换个进程就变回去，因为压根没落盘）。
这里钉住三件事：

  1. 只写本分区自己的 .env 键，且重启后 load_settings() 读得到（真的落盘了）；
  2. 保存后立刻对**正在跑的**进程生效（settings / INTENT_ROUTER / 全局 tts）；
  3. 测试接口在没 key、没网络、没样本音频时给 ok:false + 人话，绝不 500。

所有落盘都用 tmp_path + DSH_ENV_FILE 指到临时文件，绝不动用户真实的 prototype/.env。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web  # noqa: E402
from config import load_settings  # noqa: E402
from phrases import INTENT_ROUTER  # noqa: E402

#: 临时 .env 的初始内容：主模型指向一个**没人监听**的本地端口 —— 用例要真的发请求，
#: 但绝不能打到真实的付费通道上（真 key 留在环境里就等于测试在花钱）。
TMP_ENV = """# 临时 .env（测试用，不是用户那份）
LLM_PROVIDER=opencode
OPENCODE_BASE_URL=http://127.0.0.1:9/v1
OPENCODE_MODEL=tmp-model
OPENCODE_API_KEY=

QWEN_ASR_BASE_URL=http://127.0.0.1:9/v1
QWEN_ASR_MODEL=qwen3-asr-flash
QWEN_ASR_API_KEY=

TTS_ENABLED=1
TTS_VOICE=zh-CN-YunxiNeural
TTS_RATE=+0%
"""

#: 与 TMP_ENV 对应、需要钉进 os.environ 的键（load_settings 读的是环境变量，
#: 不是文件 —— 所以"重启后还在"必须靠 _reload_env 重新读一遍文件来证明）
FAKE_ENV = {
    "LLM_PROVIDER": "opencode",
    "OPENCODE_BASE_URL": "http://127.0.0.1:9/v1",
    "OPENCODE_MODEL": "tmp-model",
    "OPENCODE_API_KEY": "",
    # Go 的 key 会回落用 Zen 那把，不一起清掉就会偷偷拿到真 key
    "OPENCODE_GO_API_KEY": "",
    "OPENCODE_GO_BASE_URL": "http://127.0.0.1:9/go/v1",
    "DASHSCOPE_API_KEY": "",
    "QWEN_ASR_BASE_URL": "http://127.0.0.1:9/v1",
    "QWEN_ASR_MODEL": "qwen3-asr-flash",
    "QWEN_ASR_API_KEY": "",
    "TTS_ENABLED": "1",
    "TTS_VOICE": "zh-CN-YunxiNeural",
    "TTS_RATE": "+0%",
    "INTENT_SMALL_BASE_URL": "",
    "INTENT_SMALL_MODEL": "",
    "INTENT_SMALL_API_KEY": "",
    # 留空 = 回退默认（两个都开）；下面有专门一条用例钉住"非法值也回退默认"
    "INTENT_SMALL_ENABLED": "",
    "INTENT_SMALL_USE_SMALL_LLM": "",
    "LLM_TIMEOUT": "5",
}


class FakeTTS:
    """假 edge-tts：测试不许依赖网络，但"成功/失败/不可用"三条路都得能走到。"""

    mode = "ok"          # ok | fail | dead
    last_text = ""

    def __init__(self, enabled: bool, voice: str, rate: str, cache_dir=None):
        self.voice = voice
        self.rate = rate
        self.enabled = bool(enabled) and FakeTTS.mode != "dead"
        self.dir = Path(cache_dir or ".") / "tts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.last_error = ""

    @property
    def available(self) -> bool:
        return self.enabled

    def key(self, text: str) -> str:
        return f"{abs(hash((self.voice, self.rate, text)))}"

    def path(self, text: str) -> Path:
        return self.dir / f"{self.key(text)}.mp3"

    def cached(self, text: str) -> bool:
        return self.path(text).exists()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "voice": self.voice,
            "rate": self.rate,
            "reason": "" if self.enabled else "edge-tts 不可用（测试替身）",
            "last_error": self.last_error,
        }

    async def synth(self, text: str) -> Path | None:
        FakeTTS.last_text = text
        if not self.enabled:
            return None
        if FakeTTS.mode == "fail":
            self.last_error = "RuntimeError: 连接被重置（测试替身）"
            return None
        p = self.path(text)
        p.write_bytes(b"ID3\x03\x00\x00\x00" + b"\xff\xfb\x90\x00" * 40)
        return p


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """每个用例一份临时 .env；用例结束后把进程级全局原样还回去。

    必须这么干：web 的 settings / llm / tts 和 INTENT_ROUTER 都是**进程级单例**，
    用例之间互相污染就是"单跑通过、一起跑挂"，而这类失败最难查。
    """
    env_path = tmp_path / ".env"
    env_path.write_text(TMP_ENV, encoding="utf-8")
    monkeypatch.setenv("DSH_ENV_FILE", str(env_path))
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)

    FakeTTS.mode = "ok"
    FakeTTS.last_text = ""
    saved = (web.settings, web.llm, web.tts,
             INTENT_ROUTER.enabled, INTENT_ROUTER.use_small_llm,
             INTENT_ROUTER.small_model, INTENT_ROUTER.small_base_url,
             INTENT_ROUTER.small_api_key, dict(INTENT_ROUTER.stats),
             list(INTENT_ROUTER.history))
    INTENT_ROUTER.configure(enabled=True, use_small_llm=True, small_model="",
                            small_base_url="", small_api_key="")
    yield env_path
    web.settings, web.llm, web.tts = saved[0], saved[1], saved[2]
    INTENT_ROUTER.configure(enabled=saved[3], use_small_llm=saved[4],
                            small_model=saved[5], small_base_url=saved[6],
                            small_api_key=saved[7])
    INTENT_ROUTER.stats.clear()
    INTENT_ROUTER.stats.update(saved[8])
    INTENT_ROUTER.history = saved[9]


def _json(res) -> dict:
    """端点可能直接返回 dict，也可能用 JSONResponse 表达失败（旧端点就是这样）。"""
    if isinstance(res, JSONResponse):
        return json.loads(res.body.decode("utf-8"))
    return res


def _snapshot() -> dict:
    return _json(asyncio.run(web.api_modelcfg()))


def _save(body: dict) -> dict:
    return _json(asyncio.run(web.api_modelcfg_save(body)))


def _test(body: dict) -> dict:
    return _json(asyncio.run(web.api_modelcfg_test(body)))


def _test_all() -> dict:
    return _json(asyncio.run(web.api_modelcfg_test_all({})))


def _reload_env(env_path: Path, *keys: str) -> None:
    """模拟"重启进程"：先把这些键从环境里抹掉，再像 config 启动时那样从 .env 读一遍。

    不这么做的话"落盘成功"是测不出来的 —— apply_env_values() 已经把值写进
    os.environ 了，load_settings() 读到的可能只是内存里那份。
    """
    for k in keys:
        os.environ.pop(k, None)
    assert load_dotenv(env_path, override=True), f"临时 .env 没被读进来：{env_path}"


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def test_snapshot_shape_matches_contract(isolated, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-SENTINEL-DO-NOT-ECHO")
    snap = _snapshot()

    assert snap["env_file"] == str(isolated), snap["env_file"]
    assert snap["active_provider"] == "opencode"
    assert set(snap["sections"]) == {"main", "small", "asr", "tts"}

    main = snap["sections"]["main"]
    assert set(main) == {"provider", "base_url", "model", "key_set",
                         "providers", "env"}
    assert main["env"] == {"base_url": "OPENCODE_BASE_URL",
                           "model": "OPENCODE_MODEL",
                           "api_key": "OPENCODE_API_KEY"}
    assert main["key_set"] is True, "环境里配了 key，key_set 必须为真"
    assert [p["name"] for p in main["providers"]] == ["ollama", "opencode", "opencode_go"]
    assert [p["active"] for p in main["providers"]] == [False, True, False]
    for row in main["providers"]:
        assert {"name", "label", "hint", "base_url", "model", "key_set",
                "active"} <= set(row), f"服务商行少了字段：{sorted(row)}"

    small = snap["sections"]["small"]
    assert set(small) == {"enabled", "use_small_llm", "model", "base_url",
                          "key_set", "env"}
    assert small["env"] == {"base_url": "INTENT_SMALL_BASE_URL",
                            "model": "INTENT_SMALL_MODEL",
                            "api_key": "INTENT_SMALL_API_KEY"}

    asr = snap["sections"]["asr"]
    assert set(asr) == {"base_url", "model", "key_set", "presets"}
    assert asr["presets"] and {"id", "label", "hint"} <= set(asr["presets"][0])

    tts = snap["sections"]["tts"]
    assert set(tts) == {"enabled", "voice", "rate", "available", "voices", "last_error"}
    assert "zh-CN-YunxiNeural" in {v["id"] for v in tts["voices"]}
    assert {"id", "label"} <= set(tts["voices"][0])

    dump = json.dumps(snap, ensure_ascii=False)
    assert "sk-SENTINEL-DO-NOT-ECHO" not in dump, "快照里出现了 API Key 明文"
    for key in ("OPENCODE_API_KEY", "OPENCODE_GO_API_KEY", "QWEN_ASR_API_KEY"):
        value = os.environ.get(key) or ""
        assert not value or value not in dump, f"{key} 的明文漏进快照了"


# ---------------------------------------------------------------------------
# 保存：写回 .env + 重启后仍在 + 当场生效
# ---------------------------------------------------------------------------

def test_main_save_roundtrip_and_reload(isolated) -> None:
    out = _save({"section": "main", "provider": "opencode",
                 "base_url": "http://127.0.0.1:9/edited/v1", "model": "edited-model",
                 "api_key": "sk-main-test"})
    assert out["ok"] is True and out["section"] == "main", out
    assert out["status"]["model"] == "edited-model"
    assert out["status"]["key_set"] is True
    assert "opencode" in out["describe"]

    raw = isolated.read_text(encoding="utf-8")
    for line in ("LLM_PROVIDER=opencode",
                 "OPENCODE_BASE_URL=http://127.0.0.1:9/edited/v1",
                 "OPENCODE_MODEL=edited-model",
                 "OPENCODE_API_KEY=sk-main-test"):
        assert line in raw, f"临时 .env 里没写进这一行：{line}"

    assert _snapshot()["sections"]["main"]["model"] == "edited-model"

    _reload_env(isolated, "LLM_PROVIDER", "OPENCODE_BASE_URL", "OPENCODE_MODEL",
                "OPENCODE_API_KEY")
    s = load_settings()
    assert s.provider == "opencode"
    assert s.providers["opencode"].base_url == "http://127.0.0.1:9/edited/v1"
    assert s.providers["opencode"].model == "edited-model"
    assert s.providers["opencode"].api_key == "sk-main-test"


def test_small_save_persists_and_takes_effect_immediately(isolated) -> None:
    assert INTENT_ROUTER.small_model == "", "起点应是自动复用主模型"

    out = _save({"section": "small", "enabled": True, "use_small_llm": True,
                 "model": "qwen2.5:1.5b",
                 "base_url": "http://127.0.0.1:9/ollama/v1",
                 "api_key": "sk-small-test"})
    assert out["ok"] is True, out
    assert out["status"]["model"] == "qwen2.5:1.5b"
    assert out["status"]["key_set"] is True

    raw = isolated.read_text(encoding="utf-8")
    for line in ("INTENT_SMALL_MODEL=qwen2.5:1.5b",
                 "INTENT_SMALL_BASE_URL=http://127.0.0.1:9/ollama/v1",
                 "INTENT_SMALL_API_KEY=sk-small-test"):
        assert line in raw, f"临时 .env 里没写进这一行：{line}"

    # 立即生效：dev 面板的 status 直接读路由器（不是读 .env）
    status = _json(asyncio.run(web.api_dev_intent()))
    assert status["small_model"] == "qwen2.5:1.5b"
    assert status["small_base_url"] == "http://127.0.0.1:9/ollama/v1"

    _save({"section": "small", "enabled": False, "use_small_llm": False})
    assert INTENT_ROUTER.enabled is False and INTENT_ROUTER.use_small_llm is False
    assert INTENT_ROUTER.small_model == "qwen2.5:1.5b", "只改开关不能把小模型名弄丢"

    _reload_env(isolated, "INTENT_SMALL_MODEL", "INTENT_SMALL_BASE_URL",
                "INTENT_SMALL_API_KEY", "INTENT_SMALL_ENABLED",
                "INTENT_SMALL_USE_SMALL_LLM")
    s = load_settings()
    assert s.intent_small_model == "qwen2.5:1.5b"
    assert s.intent_small_base_url == "http://127.0.0.1:9/ollama/v1"
    assert s.intent_small_api_key == "sk-small-test"


def test_small_switches_persist_and_survive_restart(isolated) -> None:
    """两个开关必须落盘 —— 四个分区里不能只有这一处"保存了但重启不算数"。"""
    out = _save({"section": "small", "enabled": False, "use_small_llm": False})
    assert out["ok"] is True, out
    raw = isolated.read_text(encoding="utf-8")
    assert "INTENT_SMALL_ENABLED=0" in raw, f".env 里没写进开关：\n{raw}"
    assert "INTENT_SMALL_USE_SMALL_LLM=0" in raw

    snap = _snapshot()["sections"]["small"]
    assert snap["enabled"] is False and snap["use_small_llm"] is False
    status = _json(asyncio.run(web.api_dev_intent()))
    assert status["enabled"] is False and status["use_small_llm"] is False

    # 不传这两个字段 = 保持已保存值，也不能被默认值盖回去
    _save({"section": "small", "model": "qwen2.5:1.5b"})
    assert "INTENT_SMALL_ENABLED=0" in isolated.read_text(encoding="utf-8")
    snap = _snapshot()["sections"]["small"]
    assert snap["enabled"] is False and snap["use_small_llm"] is False, snap

    # 模拟重启：抹掉进程里的值，从 .env 重新读，再走一遍启动时的 seed
    _reload_env(isolated, "INTENT_SMALL_ENABLED", "INTENT_SMALL_USE_SMALL_LLM")
    s = load_settings()
    assert s.intent_small_enabled is False and s.intent_small_use_small_llm is False
    INTENT_ROUTER.configure(enabled=True, use_small_llm=True)   # 先打回默认，验证 seed 真的起作用
    web._seed_intent_router(s)
    assert INTENT_ROUTER.enabled is False and INTENT_ROUTER.use_small_llm is False


def test_small_switch_invalid_value_falls_back_to_default(isolated, monkeypatch) -> None:
    """手抖写错不值当把功能悄悄关掉：认不出来就按默认（开）。"""
    for bad in ("enable", "yes please", "打开"):
        monkeypatch.setenv("INTENT_SMALL_ENABLED", bad)
        monkeypatch.setenv("INTENT_SMALL_USE_SMALL_LLM", bad)
        s = load_settings()
        assert s.intent_small_enabled is True, f"{bad!r} 应回退默认 True"
        assert s.intent_small_use_small_llm is True, f"{bad!r} 应回退默认 True"

    # 空值同样回退默认；而仓库习惯的 1/0 必须认得出
    monkeypatch.setenv("INTENT_SMALL_ENABLED", "")
    monkeypatch.setenv("INTENT_SMALL_USE_SMALL_LLM", "0")
    s = load_settings()
    assert s.intent_small_enabled is True and s.intent_small_use_small_llm is False


def test_asr_save_roundtrip_and_reload(isolated) -> None:
    out = _save({"section": "asr", "base_url": "http://127.0.0.1:9/asr/v1",
                 "model": "qwen-audio-turbo", "api_key": "sk-asr-test"})
    assert out["ok"] is True, out
    assert out["status"]["model"] == "qwen-audio-turbo"
    assert out["status"]["key_set"] is True

    raw = isolated.read_text(encoding="utf-8")
    for line in ("QWEN_ASR_BASE_URL=http://127.0.0.1:9/asr/v1",
                 "QWEN_ASR_MODEL=qwen-audio-turbo",
                 "QWEN_ASR_API_KEY=sk-asr-test"):
        assert line in raw, f"临时 .env 里没写进这一行：{line}"
    assert "INTENT_SMALL_MODEL" not in raw, "保存 ASR 不该动手到小模型的键"

    _reload_env(isolated, "QWEN_ASR_BASE_URL", "QWEN_ASR_MODEL", "QWEN_ASR_API_KEY")
    s = load_settings()
    assert (s.asr_base_url, s.asr_model, s.asr_api_key) == (
        "http://127.0.0.1:9/asr/v1", "qwen-audio-turbo", "sk-asr-test")


def test_tts_save_rebuilds_the_global_instance(isolated, monkeypatch) -> None:
    monkeypatch.setattr(web, "TTS", FakeTTS)
    before_dir = web.tts.dir

    out = _save({"section": "tts", "voice": "zh-CN-XiaoxiaoNeural",
                 "rate": "-10%", "enabled": True})
    assert out["ok"] is True, out

    raw = isolated.read_text(encoding="utf-8")
    for line in ("TTS_VOICE=zh-CN-XiaoxiaoNeural", "TTS_RATE=-10%", "TTS_ENABLED=1"):
        assert line in raw, f"临时 .env 里没写进这一行：{line}"

    # 关键：全局实例真的换了（只改 .env 的话"保存音色"对下一句话毫无影响）
    assert web.tts.voice == "zh-CN-XiaoxiaoNeural"
    assert web.tts.rate == "-10%"
    assert web.tts.dir == before_dir, "重建 TTS 不能换缓存目录（换了等于整节课重新合成）"

    tts = _snapshot()["sections"]["tts"]
    assert tts["voice"] == "zh-CN-XiaoxiaoNeural" and tts["rate"] == "-10%"

    _save({"section": "tts", "enabled": False})
    assert "TTS_ENABLED=0" in isolated.read_text(encoding="utf-8")
    assert web.tts.enabled is False, "关掉语音也要当场生效"

    _reload_env(isolated, "TTS_VOICE", "TTS_RATE", "TTS_ENABLED")
    s = load_settings()
    assert (s.tts_voice, s.tts_rate, s.tts_enabled) == (
        "zh-CN-XiaoxiaoNeural", "-10%", False)


def test_empty_field_means_keep_not_clear(isolated) -> None:
    """界面留空 = 不修改。手滑清空一次 key 就会让人以为"服务商挂了"。"""
    _save({"section": "main", "provider": "opencode", "api_key": "sk-keep-me"})
    out = _save({"section": "main", "provider": "opencode", "api_key": "", "model": ""})
    assert out["ok"] is True, out
    raw = isolated.read_text(encoding="utf-8")
    assert "OPENCODE_API_KEY=sk-keep-me" in raw
    assert "OPENCODE_MODEL=tmp-model" in raw, "留空的 model 不该被写成空值"
    assert out["status"]["key_set"] is True


def test_unknown_section_reports_instead_of_raising(isolated) -> None:
    out = _save({"section": "nope"})
    assert out["ok"] is False and "未知的分区" in out["message"]
    out = _test({"section": "nope"})
    assert out["ok"] is False and "未知的分区" in out["message"]


# ---------------------------------------------------------------------------
# 真实测试接口：失败也必须 200 + 人话
# ---------------------------------------------------------------------------

def test_main_test_explains_connection_failure(isolated) -> None:
    out = _test({"section": "main", "provider": "opencode",
                 "base_url": "http://127.0.0.1:9/v1", "model": "probe-model"})
    assert out["section"] == "main"
    assert out["ok"] is False, out
    assert isinstance(out["elapsed_ms"], int)
    assert out["message"].strip(), "失败必须给出可读原因，不能只报 ok:false"
    assert any(k in out["message"] for k in ("连不上", "连接", "超时", "模型", "key")),         f"原因说得不像人话：{out['message']}"


def test_small_test_restores_router(isolated) -> None:
    """探测调用不许改动全局路由器 —— 它是单例，污染了下一句学生输入就打错模型。"""
    INTENT_ROUTER.configure(small_model="keep-model",
                            small_base_url="http://127.0.0.1:9/keep/v1",
                            small_api_key="keep-key", use_small_llm=False)
    snap = (INTENT_ROUTER.enabled, INTENT_ROUTER.use_small_llm,
            INTENT_ROUTER.small_model, INTENT_ROUTER.small_base_url,
            INTENT_ROUTER.small_api_key, dict(INTENT_ROUTER.stats),
            list(INTENT_ROUTER.history))

    out = _test({"section": "small", "model": "probe-small",
                 "base_url": "http://127.0.0.1:9/v1"})
    assert out["ok"] is False, out
    assert out["message"].strip()

    after = (INTENT_ROUTER.enabled, INTENT_ROUTER.use_small_llm,
             INTENT_ROUTER.small_model, INTENT_ROUTER.small_base_url,
             INTENT_ROUTER.small_api_key, dict(INTENT_ROUTER.stats),
             list(INTENT_ROUTER.history))
    assert after == snap, "测完必须把 INTENT_ROUTER（含统计/历史）恢复原样"


def test_asr_test_without_key_is_explained(isolated, tmp_path, monkeypatch) -> None:
    # 样本音频 _probe.mp3 是二进制，搬进 MAT 时不入库（TeachingFlow/classroom 下不放）。
    # 本用例钉的是"没配 key 时给的是人话"，没 key 时真转写走不到，样本用临时替身顶上。
    monkeypatch.setattr(web, "ROOT", tmp_path)
    (tmp_path / "_probe.mp3").write_bytes(b"\x00" * 64)
    out = _test({"section": "asr", "base_url": "http://127.0.0.1:9/v1",
                 "model": "qwen3-asr-flash"})
    assert out["ok"] is False, out
    assert "尚未配置" in out["message"] and "QWEN_ASR_API_KEY" in out["message"]


def test_asr_test_without_sample_audio_is_explained(isolated, tmp_path,
                                                    monkeypatch) -> None:
    monkeypatch.setattr(web, "ROOT", tmp_path)
    out = _test({"section": "asr", "api_key": "sk-fake"})
    assert out["ok"] is False, out
    assert "_probe.mp3" in out["message"], f"要说清缺的是哪个样本：{out['message']}"


def test_tts_test_returns_playable_audio_url(isolated, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web, "TTS", FakeTTS)
    monkeypatch.setattr(web, "settings",
                        replace(web.settings, cache_dir=tmp_path / "cache"))

    out = _test({"section": "tts", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+10%"})
    assert out["ok"] is True, out
    assert out["audio_url"].startswith("/api/audio/"), out["audio_url"]
    name = out["audio_url"].rsplit("/", 1)[-1]
    assert (tmp_path / "cache" / "tts" / name).exists(), "试听音频没落到缓存目录"
    assert FakeTTS.last_text == web.TTS_PROBE_TEXT, "合成的必须是那句固定的试听句"


def test_tts_test_failure_is_explained(isolated, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web, "TTS", FakeTTS)
    monkeypatch.setattr(web, "settings",
                        replace(web.settings, cache_dir=tmp_path / "cache"))

    FakeTTS.mode = "fail"
    out = _test({"section": "tts"})
    assert out["ok"] is False and "连接被重置" in out["message"], out

    FakeTTS.mode = "dead"
    out = _test({"section": "tts"})
    assert out["ok"] is False and "edge-tts" in out["message"], out


def test_test_all_returns_four_results(isolated, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web, "TTS", FakeTTS)
    monkeypatch.setattr(web, "settings",
                        replace(web.settings, cache_dir=tmp_path / "cache"))

    out = _test_all()
    assert set(out) == {"ok", "elapsed_ms", "results"}
    results = out["results"]
    assert [r["section"] for r in results] == ["main", "small", "asr", "tts"]
    for r in results:
        assert {"section", "ok", "message", "elapsed_ms"} <= set(r)
        assert isinstance(r["ok"], bool) and isinstance(r["elapsed_ms"], int)
        assert r["message"].strip()
    assert out["ok"] is all(r["ok"] for r in results)
    assert results[0]["ok"] is False, "端口 9 没人监听，主模型不该判成连通"
    assert results[2]["ok"] is False, "没配 ASR key，不该判成连通"
    assert results[3]["ok"] is True, "假 TTS 能合成，results 不该整片 false"


# ---------------------------------------------------------------------------
# 旧端点回归：改内部实现可以，对外形状不行
# ---------------------------------------------------------------------------

def test_legacy_endpoints_keep_their_shape(isolated) -> None:
    providers = _json(asyncio.run(web.api_providers()))
    assert set(providers) == {"active", "describe", "env_file", "providers"}
    assert set(providers["providers"][0]) == {"name", "label", "hint", "base_url",
                                              "model", "key_set", "env"}

    asr = _json(asyncio.run(web.api_asr_config()))
    assert {"base_url", "model", "key_set", "env_file", "presets", "describe"}         == set(asr)

    intent = _json(asyncio.run(web.api_dev_intent()))
    assert {"enabled", "use_small_llm", "small_model", "effective_model",
            "small_base_url", "enums", "stats", "recent"} == set(intent)

    bad = asyncio.run(web.api_providers_save({"provider": "nope"}))
    assert isinstance(bad, JSONResponse) and bad.status_code == 400
    assert "error" in _json(bad)

    probe = _json(asyncio.run(web.api_providers_test({
        "provider": "opencode", "base_url": "http://127.0.0.1:9/v1"})))
    assert set(probe) == {"ok", "message"} and probe["ok"] is False

    no_audio = asyncio.run(web.api_asr_transcribe({}))
    assert isinstance(no_audio, JSONResponse) and no_audio.status_code == 400
    assert _json(no_audio)["error"] == "未收到音频数据"
