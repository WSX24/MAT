"""语音合成：edge-tts（免费在线 TTS）。

- 按 (文本, 音色, 语速) 的哈希缓存音频，重复的话术不重合成
- **句级切分** —— 句级切分是句级流式合成的前提，也是让位边界的基础
- 失败自动降级为纯文字，不影响状态机（状态机不依赖音频）
- 首次启动会批量预热脚本音频（见 web.py 的 warmup）

⚠ 缓存完整性是这个文件的第一要务
--------------------------------
这里曾经有一个**静默**故障：0 字节的 mp3 被当成"合成成功"发出去，学生只看到字幕、
完全听不到声音，而控制台、页面、测试全都不报错。根因是两条：

1. `comm.save(最终路径)` 直接写目标文件 —— 进程在合成中途被杀（强杀/崩溃/刷新）
   就会在缓存里留下半截图；
2. 缓存命中那条路径只看"文件存不存在"，**不校验大小**，于是半截图被永久当成有效结果。

对策是**结构性的**，不靠调用方自觉：合成先写 `.part` 临时文件，校验通过后再原子改名。
这样最终缓存路径里**只可能出现完整文件**；被中断时留下的 `.part` 启动时清掉即可。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

try:
    import edge_tts
except Exception:  # pragma: no cover
    edge_tts = None

#: 微软已经只认 v2 的 Sec-MS-GEC 令牌（`Sec-MS-GEC-Version: 2-…`）。7.2.x 及更早算的是
#: v1，握手回来一律 403 —— 这不是网络抖动，**每一句都会失败**，重试也救不了。
#: requirements.txt 已经钉了版本；这里再挡一次，是因为"装了但装旧了"从现象上只表现成
#: "只有字幕、没有声音"，等到课堂上学生开口才发现，代价太高。
MIN_VERSION = (7, 3, 1)
#: 升级命令只写一处：改版本号时不会漏掉某一句提示
UPGRADE_HINT = 'python -m pip install -U "edge-tts>={}"'.format(
    ".".join(str(n) for n in MIN_VERSION))


def edge_tts_version() -> str:
    """装着的 edge-tts 版本号；没装给空字符串。"""
    return str(getattr(edge_tts, "__version__", "") or "")


def _version_tuple(text: str) -> tuple[int, ...]:
    """"7.2.8" → (7, 2, 8)；认不出来给空元组（见 version_problem 的放行规则）。"""
    parts: list[int] = []
    for chunk in text.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_problem() -> str:
    """版本太旧就给一句原因；够用（或认不出来）给空字符串。

    认不出来放行是有意的：测试替身没有 `__version__`，不该被这个检查拦成"语音关闭"。
    这里只负责挡住**确定太旧**的那一档。
    """
    got = _version_tuple(edge_tts_version())
    if not got or got >= MIN_VERSION:
        return ""
    want = ".".join(str(n) for n in MIN_VERSION)
    return (f"edge-tts {edge_tts_version()} 太旧，需 >= {want}：微软已改用 v2 的 "
            f"Sec-MS-GEC 令牌，旧版每一句都 403")


#: 合成中的临时文件后缀（不在最终缓存路径上）
PART_SUFFIX = ".part"
#: 单次合成的尝试次数。edge-tts 是**在线**服务，偶发 `NoAudioReceived`/连接抖动很常见
#: （实测同一句话刚才失败、几秒后连测 3 次全成功）。不重试就等于白丢一次说话机会。
SYNTH_ATTEMPTS = 2
#: 失败后多久才允许再试。太短会在服务真挂掉时反复打它；太长会让一次偶发失败
#: 把某句话"闷"很久。以前这里是永久拉黑 —— 一次抖动就让那句话整节课都没声音。
FAIL_RETRY_AFTER = 60.0


class TTS:
    def __init__(self, enabled: bool, voice: str, rate: str, cache_dir: Path):
        #: 版本太旧时这一句非空 —— 那种情况下语音能力**等于没有**：旧版算的令牌微软不认，
        #: 每句话都白跑一趟网络再降级。当场关掉，比课堂上刷 403 诚实。
        self.problem = version_problem()
        self.enabled = bool(enabled) and edge_tts is not None and not self.problem
        self.voice = voice
        self.rate = rate
        self.dir = cache_dir / "tts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._failed: dict[str, float] = {}   # key -> 允许再次尝试的时刻
        #: 最近一次失败的原因（TTS 失败以前是静默的，出了问题查不出来）
        self.last_error = ""
        #: 丢弃过的坏缓存个数（0 字节的半截图）
        self.broken_found = 0
        # 启动时清掉上次被中断留下的残留
        self._sweep()
        if enabled and self.problem:
            print(f"[tts] 语音已关闭：{self.problem}\n"
                  f"      升级后重启：{UPGRADE_HINT}", flush=True)

    def _sweep(self) -> None:
        """清掉中断残留：`.part` 临时文件、以及历史遗留的 0 字节缓存。"""
        for stale in self.dir.glob(f"*{PART_SUFFIX}"):
            try:
                stale.unlink(missing_ok=True)
            except Exception:
                pass
        for bad in self.dir.glob("*.mp3"):
            try:
                if bad.stat().st_size == 0:
                    bad.unlink(missing_ok=True)
                    self.broken_found += 1
            except Exception:
                pass

    # -- 对外 -------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.enabled

    def key(self, text: str) -> str:
        raw = f"{self.voice}|{self.rate}|{text}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:20]

    def path(self, text: str) -> Path:
        return self.dir / f"{self.key(text)}.mp3"

    def cached(self, text: str) -> bool:
        """有**可用**的缓存。0 字节不算 —— 那正是"只有字幕没声音"的元凶。"""
        p = self.path(text)
        try:
            return p.exists() and p.stat().st_size > 0
        except OSError:
            return False

    async def synth(self, text: str) -> Path | None:
        """合成一句话，返回 mp3 路径；失败或未启用返回 None（前端降级为纯文字）。"""
        if not self.enabled or not text.strip():
            return None

        p = self.path(text)
        if p.exists():
            try:
                if p.stat().st_size > 0:
                    return p
            except OSError:
                pass
            # 坏缓存（0 字节）：**不能**当成成功。删掉，往下走重新合成。
            self.broken_found += 1
            self.last_error = "缓存文件是 0 字节，已丢弃并重新合成"
            print(f"[tts] 发现 0 字节缓存，丢弃重合成：{text[:30]}", flush=True)
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

        # 拉黑只是"暂时别试了"，不是永久沉默 —— 见 FAIL_RETRY_AFTER
        until = self._failed.get(self.key(text))
        if until is not None and time.monotonic() < until:
            return None

        # 先写临时文件，校验通过再原子改名 —— 最终缓存路径里只会有完整文件
        tmp = p.with_name(p.name + PART_SUFFIX)
        last_exc: Exception | None = None
        for attempt in range(SYNTH_ATTEMPTS):
            try:
                comm = edge_tts.Communicate(text=text, voice=self.voice, rate=self.rate)
                await comm.save(str(tmp))
                if not tmp.exists() or tmp.stat().st_size == 0:
                    raise RuntimeError("edge-tts 生成了空音频")
                tmp.replace(p)
                self._failed.pop(self.key(text), None)
                return p
            except asyncio.CancelledError:
                # 被取消（服务正在关闭）—— 清掉半截图再抛，别污染缓存
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            except Exception as e:
                last_exc = e
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                if attempt + 1 < SYNTH_ATTEMPTS:
                    await asyncio.sleep(0.4)      # 给偶发抖动一点时间

        self._failed[self.key(text)] = time.monotonic() + FAIL_RETRY_AFTER
        self.last_error = f"{type(last_exc).__name__}: {last_exc}"
        # 403 = 握手被拒。几乎只有一种原因：edge-tts 太旧，算的还是微软
        # 已经不认的 v1 令牌（详见 requirements.txt）。**重试救不了它** ——
        # 每一句都会失败，学生会看到"只有字幕没有声音"，所以必须直接说清怎么修。
        hint = ""
        if "403" in str(last_exc):
            hint = ("\n      ⚠ 403 = 握手被拒，通常是 edge-tts 版本太旧"
                    "（微软已改用 v2 的 Sec-MS-GEC 令牌）。升级即可：\n"
                    f"        {UPGRADE_HINT}")
        # 以前这里是 `except Exception: pass` —— TTS 坏了完全查不出来
        print(f"[tts] 合成失败（试了 {SYNTH_ATTEMPTS} 次），这一段降级为纯文字："
              f"{self.last_error}{hint}\n"
              f"      文本：{text[:40]}\n"
              f"      {int(FAIL_RETRY_AFTER)} 秒后这句话会再试一次", flush=True)
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    async def warmup(self, texts: list[str], workers: int = 4) -> tuple[int, int]:
        """批量预合成。返回 (成功, 总数)。"""
        if not self.enabled:
            return (0, len(texts))

        todo = [t for t in dict.fromkeys(texts) if t.strip()]
        sem = asyncio.Semaphore(workers)
        ok = 0

        async def _one(t: str) -> None:
            nonlocal ok
            async with sem:
                if await self.synth(t):
                    ok += 1

        await asyncio.gather(*(_one(t) for t in todo))
        return (ok, len(todo))

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "version": edge_tts_version(),
            "voice": self.voice,
            "rate": self.rate,
            "cached": len(list(self.dir.glob("*.mp3"))) if self.dir.exists() else 0,
            "failed": len(self._failed),
            "broken_found": self.broken_found,
            "last_error": self.last_error,
            "reason": "" if self.enabled else (
                self.problem
                or (f"edge-tts 未安装（{UPGRADE_HINT}）" if edge_tts is None else "TTS_ENABLED=0")
            ),
        }
