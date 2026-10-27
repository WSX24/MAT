"""消息契约测试 —— 抓"只在少走的支路上才炸"的一类 bug。

为什么单独测这个：
  `Runner.send()` 参数重名、键名打错、`state` 少一个字段，
  端到端要跑几十秒才可能碰到，而且往往只在**探针超时自动翻页**这种支路上触发。
  实测踩过：自动翻页时 `TypeError: got multiple values for keyword argument 'slide'`，
  翻页失败又让探针重开 → 同一个错误反复重试，页面永远停在第 1 页。

这里把每条消息都构造一遍，并校验前端真正会读的字段。
运行：  python tests\\test_protocol.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings  # noqa: E402
from eval import MetricsTracker  # noqa: E402
from llm import LLM  # noqa: E402
from machine import PhaseHandlers, SpeechSeg  # noqa: E402
from script import build_script  # noqa: E402
from speech import TTS  # noqa: E402
from state import Anchor, ClassroomState, Utterance  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))


class FakeMachine:
    """只接 on_speech_end —— `_speak` 结尾必须回调它，否则状态机会卡住复读。"""

    def __init__(self) -> None:
        self.ends: list[str] = []

    async def on_speech_end(self, kind: str) -> None:
        self.ends.append(kind)


def build_runner(tts_delay: float = 0.0):
    """把 Runner 拼起来，但换掉 WebSocket 和 TTS（都不联网）。

    `tts_delay` 用来撑开"发出 segment_start → 合成音频"这段窗口，
    好让测试在这段窗口里模拟"客户端提前回 speech_done"。
    """
    import web

    r = web.Runner.__new__(web.Runner)
    r.ws = FakeWS()
    r.settings = load_settings()
    r.script = build_script()
    r.llm = LLM(r.settings)
    r.tts = SlowTTS(tts_delay)
    r.state = ClassroomState(course_id=r.script.course_id)
    r.metrics = MetricsTracker()
    r.log = []
    r._speech_done = asyncio.Event()
    r._speech_seq = 0
    r._seq = 0
    r._stray_done = 0
    r._no_audio_warned = set()
    r._pending_pause_ms = 0
    r._task = None
    r._closing = False
    r._closed = False
    r.machine = FakeMachine()
    r.handlers = PhaseHandlers(
        speak=r._speak, notify=r._notify, phase_changed=r._phase_changed,
        state_changed=r._state_changed, transcript=r._transcript,
        page_changed=r._page_changed, highlight=r._highlight,
    )
    return r


class SlowTTS:
    """可控延迟的假 TTS：撑开合成窗口，方便测"提前回 done"的竞态。"""

    def __init__(self, delay: float = 0.0, audio: bytes | None = None):
        self.delay = delay
        self.audio = audio
        self.last_error = ""

    async def synth(self, text: str):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.audio is None:
            return None
        p = Path(tempfile.gettempdir()) / "fake_tts_seg.mp3"
        p.write_bytes(self.audio)
        return p

    def status(self) -> dict:
        if self.audio is None:
            return {"enabled": False, "reason": "TTS_ENABLED=0", "last_error": "",
                    "broken_found": 0, "failed": 0, "cached": 0}
        return {"enabled": True, "last_error": "", "broken_found": 0,
                "failed": 0, "cached": 1}


#: 前端 paintState() 会直接读的字段 —— 缺一个面板就变成空白
STATE_FIELDS = ("phase", "slide_index", "slide_count", "unit", "pending",
                "resume_point", "covered", "covered_count", "deferred",
                "q_rounds", "comprehension", "probe_window", "metrics")

#: hello 必须带的字段（前端开局就要用）
HELLO_FIELDS = ("course", "goals", "slide", "slide_count", "units", "total_est_sec",
                "provider", "provider_label", "tts", "params", "phase",
                "slide_index", "metrics")


async def _collect() -> tuple[object, list[str]]:
    r = build_runner()
    problems: list[str] = []

    # 每条消息都构造一次 —— 参数重名/键名打错在这一步就会抛出来
    await r._notify("info", "开始上课")
    await r._phase_changed(r.state)
    await r._state_changed()
    await r._transcript(Utterance(speaker="teacher", text="测试", unit_id="S1-U01", slide=0))
    await r._highlight([Anchor(element_id="", text_hint="判别式")])
    for i in range(r.script.slide_count):
        await r._page_changed(i)
    await r.send("hello", **r.bootstrap())

    by_kind: dict[str, list[dict]] = {}
    for m in r.ws.sent:
        by_kind.setdefault(m.get("kind", "?"), []).append(m)

    for kind in ("notify", "phase", "state", "transcript", "highlight", "page", "hello"):
        if kind not in by_kind:
            problems.append(f"没有构造出 {kind} 消息")

    # page：前端 renderSlide(m.slide) 要求 slide 是对象；页码靠 slide_index
    for m in by_kind.get("page", []):
        sl = m.get("slide")
        if not isinstance(sl, dict):
            problems.append(f"page.slide 必须是对象：{sl!r}")
            continue
        for key in ("index", "title", "elements"):
            if key not in sl:
                problems.append(f"page.slide 缺 {key}")
        if not isinstance(m.get("slide_index"), int):
            problems.append("page 缺 slide_index（前端页码不会更新）")
        elif m["slide_index"] != sl.get("index"):
            problems.append(f"page.slide_index({m['slide_index']}) "
                            f"与 slide.index({sl.get('index')}) 不一致")

    hello = by_kind.get("hello", [{}])[0]
    for key in HELLO_FIELDS:
        if key not in hello:
            problems.append(f"hello 缺 {key}")
    if hello.get("slide", {}).get("index") != 0:
        problems.append("hello 的 slide 不是第 1 页")

    st = by_kind.get("state", [{}])[0]
    for key in STATE_FIELDS:
        if key not in st:
            problems.append(f"state 缺 {key}（paintState 会读空）")
    if not isinstance(st.get("covered"), list):
        problems.append("state.covered 必须是数组（前端用 .slice）")
    if not isinstance(st.get("pending"), list):
        problems.append("state.pending 必须是数组（前端用 .join）")
    for key in ("yield_latency_ms", "probe", "resume", "advance"):
        if key not in st.get("metrics", {}):
            problems.append(f"state.metrics 缺 {key}")

    return r, problems


def test_message_contract() -> None:
    r, problems = asyncio.run(_collect())
    assert not problems, "消息契约不符:\n" + "\n".join(f"  {p}" for p in problems)
    kinds = sorted({m.get("kind") for m in r.ws.sent})
    print(f"[P1] 构造 {len(r.ws.sent)} 条消息 · {kinds} 全部符合契约")


def test_page_payload_matches_frontend() -> None:
    """`slide_payload` 的返回形状必须与前端 renderSlide 的读法一致。"""
    r = build_runner()
    for i in range(r.script.slide_count):
        payload = r.slide_payload(i)
        assert set(payload) == {"slide"}, f"顶层键应为 slide：{set(payload)}"
        sl = payload["slide"]
        assert isinstance(sl, dict), sl
        assert isinstance(sl["index"], int)
        assert isinstance(sl["title"], str) and sl["title"]
        assert isinstance(sl["elements"], list) and sl["elements"], "页面不该没有元素"
        for e in sl["elements"]:
            for key in ("element_id", "kind", "text", "reveal"):
                assert key in e, f"元素缺 {key}: {e}"
    print(f"[P2] 页数据形状与前端一致（{r.script.slide_count} 页）")


def test_last_slide_payload_is_none() -> None:
    """越界翻页不能抛异常 —— 得给前端一个明确的 None。"""
    r = build_runner()
    assert r.slide_payload(r.script.slide_count) == {"slide": None}
    assert r.slide_payload(-1) == {"slide": None}
    print("[P3] 越界页返回 {'slide': None}")


def test_early_speech_done_is_honored() -> None:
    """客户端**提前**回 speech_done 也必须被认。

    这是实测踩到的竞态：等待信号是在发完 `segment_end` 之后才 `clear()` 的，
    而客户端只要收到 `segment_start` 就可能回 done —— 那一瞬间会被后面的
    `clear()` 抹掉，于是误报"播放等待超时"，同一段回答还会被重播好几遍。
    """

    async def go():
        # 合成慢一点，撑开窗口，让"提前回 done"真的落在等待开始之前
        r = build_runner(tts_delay=0.15)
        seg = SpeechSeg(text="判别式的符号决定根的个数。", kind="answer")

        async def fake_client():
            # 一看到 segment_start 就立刻回 done（不等 segment_end、不等音频）
            for _ in range(200):
                for m in r.ws.sent:
                    if m.get("kind") == "segment_start":
                        r._speech_seq = m["seq"]
                        r._speech_done.set()
                        return
                await asyncio.sleep(0.005)

        await asyncio.gather(r._speak(seg), fake_client())

    asyncio.run(go())
    r_after: list[dict] = []

    async def inspect():
        r = build_runner(tts_delay=0.15)
        seg = SpeechSeg(text="判别式的符号决定根的个数。", kind="answer")

        async def fake_client():
            for _ in range(200):
                for m in r.ws.sent:
                    if m.get("kind") == "segment_start":
                        r._speech_seq = m["seq"]
                        r._speech_done.set()
                        return
                await asyncio.sleep(0.005)

        await asyncio.gather(r._speak(seg), fake_client())
        r_after.extend(r.ws.sent)

    asyncio.run(inspect())
    warns = [m for m in r_after
             if m.get("kind") == "notify" and "播放等待超时" in str(m.get("text", ""))]
    assert not warns, f"提前回的 speech_done 被吞掉了，误报超时：{warns}"
    print("[P4] 提前回的 speech_done 被认（不再误报超时/重播）")


def test_stray_done_does_not_end_the_wait() -> None:
    """别的段（旧序号）的 signal 不能当成这一段播完，但要继续等正确的那个。"""

    async def go():
        r = build_runner(tts_delay=0.05)
        seg = SpeechSeg(text="判别式的符号决定根的个数。", kind="answer")

        async def fake_client():
            for _ in range(200):
                if any(m.get("kind") == "segment_end" for m in r.ws.sent):
                    break
                await asyncio.sleep(0.005)
            # 先塞一个错误序号的信号，再回正确的
            r._speech_seq = 999
            r._speech_done.set()
            await asyncio.sleep(0.05)
            r._speech_seq = r._seq
            r._speech_done.set()

        await asyncio.gather(r._speak(seg), fake_client())
        return r

    r = asyncio.run(go())
    assert r._stray_done >= 1, "错误序号的信号应当被记录为错配"
    warns = [m for m in r.ws.sent
             if m.get("kind") == "notify" and "播放等待超时" in str(m.get("text", ""))]
    assert not warns, f"收到正确序号后不该再报超时：{warns}"
    print("[P5] 错配信号被忽略且继续等待，不误判超时")


def test_no_audio_is_explained_not_silent() -> None:
    """一段话没音频时必须给出原因 —— 否则界面上就是"说完了但没出声"。"""

    async def go():
        r = build_runner(tts_delay=0.0)      # SlowTTS(audio=None) → 合成返回 None
        seg = SpeechSeg(text="判别式的符号决定根的个数。", kind="answer")

        async def fake_client():
            for _ in range(200):
                if any(m.get("kind") == "segment_end" for m in r.ws.sent):
                    break
                await asyncio.sleep(0.005)
            r._speech_seq = r._seq
            r._speech_done.set()

        await asyncio.gather(r._speak(seg), fake_client())
        return r

    r = asyncio.run(go())
    msgs = [m for m in r.ws.sent
            if m.get("kind") == "notify" and "只有字幕" in str(m.get("text", ""))]
    assert msgs, "没有音频却没有任何提示（静默故障）"
    # 同一个原因只提醒一次，不能每段都刷
    assert len(msgs) == 1, f"同一个原因应只提醒一次，实际 {len(msgs)} 次"
    print("[P6] 无音频时给出原因，且不刷屏")


def test_marker_plan_recovers_after_silent_page_change() -> None:
    """机器**悄悄换页**（不发 page_changed）之后，读表处必须自己去重算本页清单。

    `Machine._back_to_lecturing` 归位时直接改 `state.slide_index`，并不触发
    `page_changed` —— 于是"翻页去回答、再回到原页"之后，预规划的那份清单
    还停在被翻过去的那一页。只在翻页时算的话，回来这一页剩下的句子就全是
    marker=None，逐句高亮会整段消失（不报错、不告警，只是灯不亮了）。
    """

    async def go():
        r = build_runner()
        await r._page_changed(0)                 # 正常翻页 → 规划第 1 页
        assert r._markers_slide == 0, f"翻页后没记下已规划的页：{r._markers_slide}"
        # 找一张**不是第 1 页**、且有锚点的单元
        target, unit = None, None
        for i in range(1, r.script.slide_count):
            us = [u for u in r.script.units_of(i) if u.anchors]
            if us:
                target, unit = i, us[0]
                break
        assert unit is not None, "演示脚本里找不到带锚点的非首页单元"
        # 机器换页：只改 state.slide_index，**不走 _page_changed**
        r.state.slide_index = target
        r.state.unit_progress[unit.unit_id] = 1
        assert (unit.unit_id, 1) not in r._markers, \
            "这一页还没被规划过，表里不该已经有它"
        marker = r._marker_for(SpeechSeg(text="占位", kind="unit", unit_id=unit.unit_id))
        assert marker is not None, "悄悄换页后没有惰性重算（marker=None = 高亮整段消失）"
        assert marker["element_id"] == unit.anchors[0].element_id, marker
        assert marker["sentence"] == 1, marker
        assert r._markers_slide == target, f"重算后没更新已规划的页：{r._markers_slide}"

        # 已在同一页时不该再重算（逐句下发不能多出开销）：换成另一份表能被保留
        r._markers = {("哨兵", 1): {"unit_id": "哨兵", "sentence": 1, "element_id": "x"}}
        r._markers_slide = target
        r._marker_for(SpeechSeg(text="占位", kind="unit", unit_id=unit.unit_id))
        assert ("哨兵", 1) in r._markers, "页没变却重算了 —— 每句都会多算一遍"

    asyncio.run(go())
    print("[P7] 悄悄换页后读表处惰性重算本页清单（同页不重算）")


def test_rephrase_keeps_marker_but_answers_clear_it() -> None:
    """复述沿用同一处的灯、应答清掉 —— 两类段刻意不同对待。

    复述（rephrase）/归位（resume）说的**就是同一行**，此时撤掉标记等于
    "老师重复一遍却把手指收回去"，视觉与语音打架；应答、提问是**新话题**，
    旧标记留在屏幕上会张冠李戴（学生在看上一处，老师已经讲到别处了）。
    """

    async def go():
        r = build_runner()
        r.state.slide_index = 1
        await r._page_changed(1)                 # 正常翻页 → 规划第 2 页
        us = [u for u in r.script.units_of(1) if u.anchors]
        assert len(us) >= 2, "演示脚本第 2 页应该有两个以上带锚点的单元"
        unit = us[0]
        r.state.unit_progress[unit.unit_id] = 1
        m_unit = r._marker_for(SpeechSeg(text="占位", kind="unit", unit_id=unit.unit_id))
        assert m_unit is not None, "讲解句没拿到标记"
        assert m_unit["element_id"] == unit.anchors[0].element_id, m_unit

        m_rep = r._marker_for(SpeechSeg(text="换个说法", kind="rephrase",
                                        unit_id=unit.unit_id))
        assert m_rep == m_unit, f"复述时标记被清掉了：{m_rep}"
        m_resume = r._marker_for(SpeechSeg(text="回到刚才那里", kind="resume",
                                           unit_id=unit.unit_id))
        assert m_resume == m_unit, f"归位时标记被清掉了：{m_resume}"
        assert r._marker_for(SpeechSeg(text="嗯", kind="answer",
                                       unit_id=unit.unit_id)) is None, \
            "应答是新话题，旧标记必须清掉（否则张冠李戴）"

        # 同一页的下一个单元、那一句没有重点：缓存不该被更新，
        # 复述也不能把上一个单元的灯带过来
        other = us[1]
        r.state.unit_progress[other.unit_id] = 99
        assert r._marker_for(SpeechSeg(text="占位", kind="unit",
                                       unit_id=other.unit_id)) is None
        m_other = r._marker_for(SpeechSeg(text="换个说法", kind="rephrase",
                                         unit_id=other.unit_id))
        assert m_other is None, f"换到下一个单元后仍返回了上一单元的标记：{m_other}"

    asyncio.run(go())
    print("[P8] 复述/归位沿用同一处的标记，应答清掉，跨单元不带灯")


ALL = [test_message_contract, test_page_payload_matches_frontend,
       test_last_slide_payload_is_none, test_early_speech_done_is_honored,
       test_stray_done_does_not_end_the_wait, test_no_audio_is_explained_not_silent,
       test_marker_plan_recovers_after_silent_page_change,
       test_rephrase_keeps_marker_but_answers_clear_it]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
    print(f"\n消息契约结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
