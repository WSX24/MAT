"""语音缓存完整性测试 —— 抓"只有字幕没有声音"这一类**静默**故障。

为什么单独测这个：
  实测踩过一个很难发现的故障：0 字节的 mp3 被当成"合成成功"发出去。
  学生只看到字幕、一点声音都听不到，而控制台、页面、端到端脚本全都不报错
  （那几个脚本只读文本、从不看音频字节数）。
  根因是两条：`comm.save(最终路径)` 直接写目标文件，进程在合成中途被杀就留下半截图；
  缓存命中那条路径只看文件在不在、**不校验大小**。

  这类 bug 的共同点是"看着一切正常"，所以必须用测试钉住，
  而不是靠下次演示时凭耳朵发现。

运行：  python tests\\test_tts.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import speech  # noqa: E402
from speech import TTS  # noqa: E402

GOOD = b"ID3\x03\x00\x00\x00" + b"\xff\xfb\x90\x00" * 200   # 够长、非空，当有效音频用
TEXT = "判别式的符号决定根的个数。"


class FakeCommunicate:
    """假的 edge-tts：按脚本决定写出什么，用来复现各种中断/空产出场景。"""

    mode = "ok"          # ok | empty | explodes | cancels
    calls = 0

    def __init__(self, text: str, voice: str = "", rate: str = "", **kw) -> None:
        self.text = text

    async def save(self, path: str) -> None:
        FakeCommunicate.calls += 1
        p = Path(path)
        if FakeCommunicate.mode == "explodes":
            # 已经建了文件但没写完就炸（等价于被强杀留下的半截图）
            p.write_bytes(b"")
            raise RuntimeError("连接在合成途中断开")
        if FakeCommunicate.mode == "empty":
            p.write_bytes(b"")
            return
        if FakeCommunicate.mode == "cancels":
            p.write_bytes(b"")
            raise asyncio.CancelledError()
        if FakeCommunicate.mode == "flaky_once":
            # 第一次抖动（真实场景：NoAudioReceived），之后正常
            if FakeCommunicate.calls == 1:
                p.write_bytes(b"")
                raise RuntimeError("NoAudioReceived: No audio was received")
            p.write_bytes(GOOD)
            return
        p.write_bytes(GOOD)


class _FakeEdgeTTS(SimpleNamespace):
    Communicate = FakeCommunicate


class patch_tts:
    """临时把 speech 模块里的 edge_tts 换成假的。"""

    def __enter__(self):
        speech._real_edge = speech.edge_tts
        speech.edge_tts = _FakeEdgeTTS()
        return self

    def __exit__(self, *exc):
        speech.edge_tts = speech._real_edge
        return False


def make_tts(tmp: Path) -> TTS:
    return TTS(True, "zh-CN-YunxiNeural", "+0%", tmp)


# ---------------------------------------------------------------- 用例

def test_zero_byte_cache_is_not_valid() -> None:
    """0 字节缓存绝不能当成"合成成功" —— 这就是没声音的直接原因。"""
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        p = tts.path(TEXT)
        p.write_bytes(b"")                      # 种一个坏缓存
        assert tts.cached(TEXT) is False, "cached() 不能把 0 字节当有效缓存"

        FakeCommunicate.mode = "ok"
        got = asyncio.run(tts.synth(TEXT))
        assert got is not None, "遇到坏缓存应当删掉并重新合成，而不是返回它"
        assert got.stat().st_size > 0, "重新合成后必须是有效音频"
        assert tts.broken_found >= 1, "丢弃坏缓存这件事要能被观测到"
    print("[T1] 0 字节缓存被识别为坏、自动重合成")


def test_interrupted_synthesis_never_pollutes_cache() -> None:
    """合成中途失败 → 最终缓存路径**不能**出现文件（原子写入的核心保证）。"""
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        FakeCommunicate.mode = "explodes"
        got = asyncio.run(tts.synth(TEXT))
        assert got is None, "合成失败应当返回 None（降级为纯文字）"
        assert not tts.path(TEXT).exists(), \
            "失败后最终缓存路径不能留下文件 —— 否则下次会被当成有效缓存"
        parts = list(tts.dir.glob("*.part"))
        assert not parts, f"临时文件要清掉，残留：{parts}"
    print("[T2] 合成中断不留半截图（原子写入）")


def test_success_is_atomic_and_reusable() -> None:
    """成功路径：先写 .part、再原子改名；第二次直接命中缓存。"""
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        FakeCommunicate.mode = "ok"
        FakeCommunicate.calls = 0
        p = asyncio.run(tts.synth(TEXT))
        assert p is not None and p.stat().st_size > 0
        assert not list(tts.dir.glob("*.part")), "成功后不该留临时文件"
        assert tts.cached(TEXT) is True

        before = FakeCommunicate.calls
        again = asyncio.run(tts.synth(TEXT))
        assert again == p
        assert FakeCommunicate.calls == before, "第二次应命中缓存，不再调用合成"
    print("[T3] 成功路径原子、可复用")


def test_empty_output_is_not_success() -> None:
    """服务商返回空音频时不能返回路径 —— 否则就是"有字幕没声音"。"""
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        FakeCommunicate.mode = "empty"
        got = asyncio.run(tts.synth(TEXT))
        assert got is None
        assert not tts.path(TEXT).exists()
        assert tts.last_error, "失败必须留下原因（以前是静默吞掉的）"
    print(f"[T4] 空产出不算成功，且留下原因")


def test_failure_is_not_silent() -> None:
    """失败要可诊断：last_error 有内容，且 status() 带得出来。"""
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        FakeCommunicate.mode = "explodes"
        asyncio.run(tts.synth(TEXT))
        assert "连接在合成途中断开" in tts.last_error, tts.last_error
        st = tts.status()
        for key in ("last_error", "broken_found", "failed", "cached"):
            assert key in st, f"status() 缺 {key}"
    print("[T5] 失败可诊断，status() 暴露原因")


def test_cancelled_synthesis_cleans_up() -> None:
    """被取消（服务关闭）也要清掉半截图，不能把脏文件留在缓存里。"""
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        FakeCommunicate.mode = "cancels"
        try:
            asyncio.run(tts.synth(TEXT))
        except asyncio.CancelledError:
            pass
        assert not tts.path(TEXT).exists()
        assert not list(tts.dir.glob("*.part")), "取消后临时文件要清掉"
    print("[T6] 取消后不留残留")


def test_transient_failure_is_retried() -> None:
    """偶发失败要重试：edge-tts 是在线服务，一次抖动不该让学生听不到这句话。

    实测：同一句话刚才失败、几秒后连测 3 次全成功。不重试就是白丢一次说话机会。
    """
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        FakeCommunicate.mode = "flaky_once"      # 第一次失败，第二次成功
        FakeCommunicate.calls = 0
        got = asyncio.run(tts.synth(TEXT))
        assert got is not None, "第一次抖动就放弃 —— 应当重试后成功"
        assert got.stat().st_size > 0
        assert FakeCommunicate.calls >= 2, f"应当重试过一次，实际调用 {FakeCommunicate.calls} 次"
    print("[T8] 偶发失败会重试，抖动不会白丢一句话")


def test_failure_is_not_a_permanent_mute() -> None:
    """失败只是"暂时别试了"，不是永久拉黑。

    以前用 set 永久记录 —— 一次网络抖动就把那句话整节课闷住了，
    服务恢复了它还是没声音。
    """
    with tempfile.TemporaryDirectory() as d, patch_tts():
        tmp = Path(d)
        tts = make_tts(tmp)
        key = tts.key(TEXT)
        FakeCommunicate.mode = "explodes"
        assert asyncio.run(tts.synth(TEXT)) is None

        # 还在冷却期内：不再去打服务（避免服务真挂时被反复捶）
        FakeCommunicate.calls = 0
        assert asyncio.run(tts.synth(TEXT)) is None
        assert FakeCommunicate.calls == 0, "冷却期内不该重复打服务"

        # 把冷却截止时间挪到过去 = 假装时间到、服务也恢复了
        assert key in tts._failed, "失败要记录冷却时间"
        tts._failed[key] = time.monotonic() - 1.0
        FakeCommunicate.mode = "ok"
        got = asyncio.run(tts.synth(TEXT))
        assert got is not None, "冷却期过后服务已恢复，却还是没声音（被永久拉黑了）"
        assert got.stat().st_size > 0
        assert key not in tts._failed, "成功后要解除冷却记录"
    print("[T9] 失败不是永久拉黑，服务恢复后能重新出声")


def test_startup_sweeps_leftovers() -> None:
    """启动时要清掉上次中断留下的 .part 和历史遗留的 0 字节缓存。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        old = make_tts(tmp)                      # 先建目录
        bad = old.dir / "deadbeef.mp3"
        bad.write_bytes(b"")
        part = old.dir / "deadbeef.mp3.part"
        part.write_bytes(b"half")
        assert bad.exists() and part.exists()

        fresh = make_tts(tmp)                    # 重新构造 = 重启
        assert not part.exists(), "启动时要清掉 .part 残留"
        assert not bad.exists(), "启动时要清掉 0 字节缓存（否则它会一直被当成有效）"
        assert fresh.broken_found >= 1
    print("[T7] 启动自动清理残留")


ALL = [test_zero_byte_cache_is_not_valid,
       test_interrupted_synthesis_never_pollutes_cache,
       test_success_is_atomic_and_reusable,
       test_empty_output_is_not_success,
       test_failure_is_not_silent,
       test_cancelled_synthesis_cleans_up,
       test_transient_failure_is_retried,
       test_failure_is_not_a_permanent_mute,
       test_startup_sweeps_leftovers]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
    print(f"\n语音缓存结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
