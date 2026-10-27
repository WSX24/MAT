"""Web 层：把状态机接到浏览器上，并采集《课堂交互设计》§7.6 的指标。

Playback 模型（关键）
---------------------
状态机的 `speak(seg)` 会 **阻塞到这一段真的播完**，前端收到 `segment_end`
后播完回一条 `speech_done` 才算数。这样"让位"天然发生在音频边界上 ——
与《课堂交互设计》§4.1 的段末让位完全一致，不需要额外处理音频打断。

时序：
    segment_start -> (synth) -> audio_delta ... -> segment_end -> [前端播放] -> speech_done
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import (
    ASR_ENV_KEYS,
    INTENT_SMALL_ENV_KEYS,
    INTENT_SMALL_SWITCH_ENV_KEYS,
    PROVIDER_ENV_KEYS,
    ROOT,
    Settings,
    apply_env_values,
    env_file,
    load_settings,
    save_env_values,
)
from course import Course, load_course, print_course_notes
from director.route import Route
from eval import MetricsTracker
from llm import LLM
from machine import Machine, PhaseHandlers, SpeechSeg
from mcp_server import McpBrainServer, build_access_prompt
from models import LessonScript
from phrases import INTENT_ROUTER, PHRASE_BANK
from script_gen import plan_markers
from speech import TTS
from state import ClassroomState, Phase, Utterance
PROVIDER_INFO = {
    "ollama": {
        "label": "Ollama（本地）",
        "hint": "需要先启动 ollama 并 ollama pull 对应模型",
    },
    "opencode": {
        "label": "OpenCode Zen",
        "hint": "需要 OPENCODE_API_KEY；base_url 与 model 可在 .env 覆盖",
    },
    "opencode_go": {
        "label": "OpenCode Go（订阅套餐）",
        "hint": "需要 OPENCODE_GO_API_KEY（缺省回落 OPENCODE_API_KEY）；"
                "默认模型 deepseek-v4.1-flash，可在 .env 覆盖",
    },
}


# ===========================================================================
# 运行器
# ===========================================================================

class Runner:
    """一个课堂会话。个人自学场景下每个连接一个 Runner。"""

    #: 这一次翻页是不是**学生自己按的**（手动翻页 → 整页直接显示全）。
    #:
    #: 给**类级默认值**，而不是只写在 `__init__` 里：消息契约测试是用
    #: `Runner.__new__` 拼实例的（不走 `__init__`），只在 `__init__` 里赋值的话，
    #: 那些用例一读属性就 AttributeError。而 `_page_changed` 是**第一次自动翻页**
    #: 就会走到的路径 —— 少这个默认值，课堂会在第一次翻页时整个停住
    #: （异常被 asyncio 吞掉，前端只表现为"老师突然不说话了"；实测踩过）。
    _manual_nav = False

    #: 本页的标记清单：(unit_id, sentence) → {"unit_id","sentence","element_id"}。
    #:
    #: 同样给类级默认值（理由同上）：消息契约测试用 `Runner.__new__` 拼实例、
    #: 直接调 `_speak`，这时还没有任何一页被规划过 —— 类级空表让那句"查不到就 None"
    #: 自然成立，而不是抛 AttributeError 把整节课停掉。
    _markers: dict = {}

    #: 已经按哪一页算过 `_markers`（None = 还没算过任何一页）。
    #:
    #: 与 `_markers` 一起给类级默认值，理由相同：`Runner.__new__` 拼出来的实例
    #: 直接调 `_marker_for` 时也要能判断"页对不上"并自己算一次。
    _markers_slide: int | None = None

    #: 最近一个**非 None** 的标记，以及它属于哪个单元。
    #:
    #: 复述（rephrase）与"回到刚才那处"（resume）说的是同一行内容，
    #: 需要沿用上一处点亮的那个锚点；但它只对**同一个单元**成立 ——
    #: 单元或页一变，旧标记指向的就已经不是这一处了（所以配一个 unit_id 记着）。
    _last_marker: dict | None = None
    _last_marker_unit: str | None = None

    def __init__(self, ws: WebSocket, settings: Settings, course: Course,
                 llm: LLM, tts: TTS):
        self.ws = ws
        self.settings = settings
        self._course = course
        self.script = course.script
        self.llm = llm
        self.tts = tts

        self.state = ClassroomState(course_id=course.script.course_id)
        self.metrics = MetricsTracker()
        self.log: list[dict] = []
        self._speech_done = asyncio.Event()
        self._speech_seq = 0        # 前端回来的 speech_done 对应哪一段
        self._seq = 0               # 已发出的段序号
        self._stray_done = 0        # 错配的 speech_done 次数（应该恒为 0）
        self._no_audio_warned: set[str] = set()   # "这一段没声音"的原因只提醒一次
        self._pending_pause_ms = 0
        self._task: asyncio.Task | None = None
        self._closing = False
        self._closed = False        # 连接已经断了（后台推送要安静地放弃）
        #: 这一次翻页是不是**学生自己按的**（手动翻页 → 整页直接显示全）。
        #: 必须在 __init__ 里就给初值：`_page_changed` 会读它，而第一次自动翻页
        #: 就发生在开课后不久 —— 少这一行会让课堂在第一次翻页时整个停住
        #: （AttributeError 被 asyncio 吞掉，前端只表现为"老师突然不说话了"）。
        self._manual_nav = False
        self._markers = {}
        self._markers_slide = None
        self._last_marker = None
        self._last_marker_unit = None

        self.handlers = PhaseHandlers(
            speak=self._speak,
            notify=self._notify,
            phase_changed=self._phase_changed,
            state_changed=self._state_changed,
            transcript=self._transcript,
            page_changed=self._page_changed,
            highlight=self._highlight,
            thinking=self._thinking,
        )
        self.machine = Machine(course.script, self.state, settings, llm, self.handlers,
                               self.metrics, mcp=MCP_SERVER)
        # 决策层是 opt-in（§11 最后一行）：默认关时下面这段一行都不执行，连
        # director.assembly / agent.proposer 都不会被 import —— 默认课堂与改动前逐字一致。
        #: C 的 proposer 与后台 task 留着给 start()/close()：两个开关任一为假时恒为 None，
        #: 于是"起循环"和"取消循环"两处也各只需一行判断。
        self._agent_c = None
        self._agent_c_task: asyncio.Task | None = None
        if settings.director_enabled:
            from director.assembly import wire

            if settings.agent_c_enabled:
                # C 的真实模型接入（§7.2 ④）：独立 LLM 实例，不复用课堂那个客户端（§3.2 D2）。
                # import 放在这里而不是模块头：默认课堂多一个 agent.proposer 的
                # import 就等于多一个 openai 客户端可达的路径，承诺是"一个字节都不动"。
                from agent.proposer import build_proposer

                self._agent_c = build_proposer(settings)
            # 有真 proposer 才告诉 wire "C 能干活"，wire 自己绝不起循环（装配必成功，
            # 生产可失败 —— 见 director/assembly.py 的 serve_agent_c）。
            wire(self.machine, settings=settings, proposer=self._agent_c)

    # -- 发送 -------------------------------------------------------------
    @property
    def course(self) -> Course:
        """本节课件。

        用属性而不是普通字段，是为了让测试里 `Runner.__new__` 拼出来的实例也能用
        （老的消息契约测试就是这么造的）—— 没设 course 时按"手写演示课件"处理，
        行为与接入真实 PPT 之前完全一致。
        """
        c = getattr(self, "_course", None)
        if c is None:
            c = Course(script=self.script)
            self._course = c
        return c

    async def send(self, kind: str, **payload: Any) -> None:
        msg = {"kind": kind, "ts": datetime.now().isoformat(timespec="seconds"), **payload}
        self.log.append(msg)
        if len(self.log) > 600:
            self.log = self.log[-600:]
        if self._closed:
            return
        try:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False, default=str))
        except Exception:
            # 学生关掉页面时可能正好有后台任务在推消息（例如字幕）——
            # 这是正常的收尾，不该冒出一个未捕获异常。
            self._closed = True

    # -- 回调实现 ---------------------------------------------------------
    async def _notify(self, level: str, text: str) -> None:
        await self.send("notify", level=level, text=text)

    async def _thinking(self, on: bool, show_secs: bool = False) -> None:
        """模型在思考 / 想完了。

        只发**状态**，不发内容 —— 模型的自言自语不该出现在课堂上。
        收到 on=True 时前端会显示一个带秒表的"老师在思考…"，
        让这段（开了思考以后会到几十秒的）等待变得可理解。
        """
        await self.send("thinking", on=on, sec=show_secs)

    async def _phase_changed(self, state: ClassroomState) -> None:
        await self.send("phase", phase=state.phase.value, slide=state.slide_index,
                        seg=state.seg_index)

    async def _state_changed(self) -> None:
        await self.send("state", **self.snapshot())

    async def _transcript(self, u: Utterance) -> None:
        slide_obj = self.script.slide_at(u.slide)
        slide_title = slide_obj.title if slide_obj else ""
        await self.send("transcript", speaker=u.speaker, text=u.text,
                        unit_id=u.unit_id, slide=u.slide, slide_title=slide_title)

    def _plan_markers(self, slide_index: int) -> None:
        """把**这一页**的标记清单算好，逐句下发时只做一次查表。

        放在翻页这一刻算，而不是每句现算：句子边界是让位点，
        那里的任何多余开销都是学生等出来的。
        """
        self._markers = {(m["unit_id"], m["sentence"]): m
                         for m in plan_markers(self.script.units_of(slide_index))}
        self._markers_slide = slide_index
        # 换了页，上一处点亮的那个锚点已经不在屏幕上了：留在缓存里会把灯带过页
        self._last_marker = None
        self._last_marker_unit = None

    def _marker_for(self, seg: SpeechSeg) -> dict | None:
        """这一句该亮哪一个锚点（没有就 None）。

        句号直接取 `state.unit_progress`：机器在**返回 seg 之前**就把它写成了
        "这一句自己的序号"，所以这里读到的一定是当前这句，不需要另记一份进度。
        读表之前先对一次页：`Machine._back_to_lecturing` 归位时直接改
        `state.slide_index`，**不触发 `page_changed`** —— 于是"翻页去回答、
        再回到原页"之后，清单还停在被翻过去的那一页，回来这一页剩下的句子
        会全部变成 marker=None（高亮整段消失）。这里是**唯一读表的地方**，
        兜这一下就覆盖了所有"机器悄悄换页"的路径（含以后新增的跳转）；
        只有页对不上时才重算，正常逐句下发不会多出每句一次的开销。

        两类段对"上一处的灯"态度相反，不能一刀切：
          - 复述（rephrase）与回到刚才那处（resume）说的**就是同一行**，
            这时把灯撤掉，等于老师重复一遍却把手指收回去 —— 视觉与语音打架，所以沿用。
          - 应答/提问/串场是新话题，旧标记留在屏幕上会**张冠李戴**
            （学生在看上一处的高亮，老师已经讲到别的地方了），所以清掉。
        讲解句本身若这一句没有重点，也只是"这句不指哪"，不是换了话题，缓存照留。
        """
        if self._markers_slide != self.state.slide_index:
            self._plan_markers(self.state.slide_index)

        if seg.kind == "unit":
            if not seg.unit_id:
                return None
            sentence = self.state.unit_progress.get(seg.unit_id, 0)
            marker = self._markers.get((seg.unit_id, sentence))
            if marker is not None:
                self._last_marker, self._last_marker_unit = marker, seg.unit_id
            return marker

        if seg.kind in ("rephrase", "resume"):
            if not seg.unit_id or self._last_marker_unit != seg.unit_id:
                # 已经不是同一个单元了：旧界定不清，宁可没有灯也不要指错地方
                self._last_marker = None
                self._last_marker_unit = None
                return None
            return self._last_marker

        return None

    async def _page_changed(self, slide_index: int) -> None:
        # 带上 slide_index/slide_count，前端据此更新"第几页"
        # （只画课件不更新页码的话，翻页看起来像没生效）
        #
        # 学生自己按下"下一页"时，整页**直接显示全**：分步显示是为了"讲到才出现"，
        # 而手动翻页的意图是"我要看这一页"。否则翻过去看到的是一片空白 ——
        # 学生会以为课件坏了（这条是实测截图复查时抓出来的）。
        reveal_all = self._manual_nav
        self._manual_nav = False
        self._plan_markers(slide_index)
        await self.send("page", slide_index=slide_index,
                        slide_count=self.script.slide_count,
                        **self.slide_payload(slide_index, reveal_all=reveal_all))

    async def _highlight(self, anchors: list) -> None:
        await self.send("highlight", anchors=[asdict(a) for a in anchors])

    async def _speak(self, seg: SpeechSeg) -> None:
        """把一段话送到前端并等它播完 —— 这就是段末让位的物理边界。

        **序号绑定**：每一段带一个自增 seq，前端回来时必须原样带回。
        只看"有没有 speech_done"是不够的 —— 一次错配（上一段的 done 落在这一段上、
        或某段根本没回）就会让"等播完"落到错误的段落上，进而整节课开始复读同一句话。
        """
        if self._closing:
            return
        self._seq += 1
        seq = self._seq
        # **先清等待信号，再发 segment_start**。顺序反过来（发完 segment_end 才 clear）
        # 会吞掉早到的 speech_done：客户端一收到 segment_start 就可能回 done，
        # 那一瞬间会被后面的 clear() 抹掉，于是误报"播放等待超时"，同一段还会重播几遍。
        self._speech_done.clear()
        self._speech_seq = 0
        slide_obj = self.script.slide_at(self.state.slide_index)
        await self.send(
            "segment_start", seq=seq, text=seg.text, seg_kind=seg.kind,
            unit_id=seg.unit_id, raise_hand=seg.raise_hand,
            slide=self.state.slide_index,
            slide_title=slide_obj.title if slide_obj else "",
            anchors=[asdict(a) for a in seg.anchors],
            # 课件操作与这句话同时生效：说到哪、指到哪是同一件事（见 deck/ops.py）
            ops=[op.to_dict() if hasattr(op, "to_dict") else dict(op) for op in (seg.ops or [])],
            est_sec=round(len(seg.text) / 5.0, 1),
            # 逐句高亮：这一句只亮这一个锚点（整单元批量高亮留在下面，
            # 协议不删 —— 前端已经不拿它当作讲解焦点了）
            marker=self._marker_for(seg),
        )
        if seg.anchors:
            await self._highlight(seg.anchors)

        url = ""
        audio = await self.tts.synth(seg.text)
        if audio is not None:
            try:
                url = f"/api/audio/{audio.name}"
                b64 = base64.b64encode(audio.read_bytes()).decode("ascii")
                await self.send("audio_delta", seq=seq, url=url, b64=b64, mime="audio/mpeg")
            except Exception as e:  # 读文件失败不该让课堂停下
                url = ""
                await self._notify("warn", f"音频读取失败，降级为文字：{e}")
        if not url:
            # 这一段没有声音。以前是完全静默的 —— 学生只看到字幕，界面上没有任何解释。
            await self._warn_no_audio(seg)

        await self.send("segment_end", seq=seq, text=seg.text, seg_kind=seg.kind,
                        unit_id=seg.unit_id, audio_url=url)

        # 等前端播完（§7.4：音频边界即让位边界）。
        # 只认**属于这一段**的完成信号；收到别的段的信号不能当成播完，
        # 但也不该直接放弃等待 —— 把它的信号清掉，继续等这一段。
        deadline = time.monotonic() + max(15.0, len(seg.text) * 0.45 + 10.0)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                await self._notify("warn", "播放等待超时，按已播完继续")
                break
            try:
                await asyncio.wait_for(self._speech_done.wait(), timeout=left)
            except asyncio.TimeoutError:
                await self._notify("warn", "播放等待超时，按已播完继续")
                break
            if self._speech_seq == seq:
                break
            self._stray_done += 1
            self.log.append({"kind": "_stray_speech_done",
                             "expect": seq, "got": self._speech_seq})
            self._speech_done.clear()   # 不是这一段的信号，继续等

        # **必须把"这一段说完了"告诉状态机** —— 状态转移全靠这个回调。
        # 少了这一步，状态机会永远停在当前产出段上（回执会无限复读）。
        await self.machine.on_speech_end(seg.kind)

        if self._pending_pause_ms:
            await asyncio.sleep(self._pending_pause_ms / 1000.0)
            self._pending_pause_ms = 0

    async def _warn_no_audio(self, seg: SpeechSeg) -> None:
        """一段话没有音频时，说清楚为什么 —— 否则前端看着像"说完了但没出声"。

        同一个原因只提醒一次，避免每段都刷。
        """
        st = self.tts.status()
        why = ""
        if not st.get("enabled"):
            why = st.get("reason") or "TTS 未启用"
        elif st.get("last_error"):
            why = st["last_error"]
        else:
            why = "合成结果为空"
        if why in self._no_audio_warned:
            return
        self._no_audio_warned.add(why)
        await self._notify("warn", f"这一段只有字幕、没有语音：{why}")

    # -- 课堂生命周期 -----------------------------------------------------
    async def start(self) -> None:
        # 开课不经过 `_page_changed`（第一页是"本来就在那儿"的），
        # 少了这一行，第 1 页的每一句都会是 marker=None —— 也就是整页没有高亮。
        self._plan_markers(self.state.slide_index)
        await self.send("hello", **self.bootstrap())
        self._task = asyncio.create_task(self.machine.start())
        if self._agent_c is not None:
            # C 的后台循环（§7.1）：每 agent_c_interval 秒醒一次，直到课堂结束。
            # "可装配但默认不启动"变成"真正可用"，靠的就是这一行 —— 之前没有真 proposer，
            # TemplateProposer 产的是占位文案（"第 9 个单元讲的是这一点"），接进真课堂
            # 学生只会听到废话，所以那时只装 J、不起循环。
            from director.assembly import serve_agent_c

            self._agent_c_task = asyncio.create_task(serve_agent_c(
                self.machine, proposer=self._agent_c, deck=self.course.deck,
                interval_s=self.settings.agent_c_interval,
            ))

    async def on_speech_done(self, seq: int = 0) -> None:
        self._speech_seq = int(seq or 0)
        self._speech_done.set()

    async def on_input(self, text: str, *, via: str = "text") -> None:
        await self.machine.handle_input(text, via=via)
        if await self._maybe_kick():
            return

    async def on_command(self, cmd: str) -> None:
        if cmd == "finish":
            self.machine._finished.set()
            return
        # 手动翻页：这一页要整页显示（见 _page_changed 里的说明）
        self._manual_nav = cmd in ("next", "prev", "goto", "jump")
        try:
            await self.machine.handle_command(cmd)
        finally:
            self._manual_nav = False
        await self._maybe_kick()

    async def _maybe_kick(self) -> bool:
        """主循环停下来时把它拉起来（对应 Machine._kick 的判据）。"""
        if self.machine._busy or self.machine._finished.is_set():
            return False
        await self.machine._kick()
        return True

    async def close(self) -> None:
        self._closing = True
        self._closed = True
        self._speech_done.set()
        if self._task:
            self._task.cancel()
        # 课后不许留悬挂 task（§11）：C 的循环在这里取消并**等它真的停下**。
        # 只 cancel 不 await 会留下一个还没跑完的 task，事件循环关掉时才炸 —— 那时
        # 表面上课堂早就结束了，没人会把这条报错和 C 联系起来。
        task, self._agent_c_task = self._agent_c_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass                     # 停后台角色的错不该冒到收课流程上（§11）

    # -- 数据 -------------------------------------------------------------
    def bootstrap(self) -> dict:
        return {
            "course": self.script.title,
            "goals": self.script.session_goals,
            "slide_count": self.script.slide_count,
            "total_est_sec": self.script.total_est_sec,
            "provider": self.settings.describe(),
            "provider_name": self.settings.provider,
            "provider_label": PROVIDER_INFO.get(self.settings.provider, {}).get("label", ""),
            "tts": self.tts.status(),
            "course_info": {
                "mode": self.course.mode,
                "deck_id": self.course.deck_id,
                "plan_source": self.course.plan.source if self.course.plan else "",
                "notes": self.course.notes,
            },
            "deck": (
                {"deck_id": self.course.deck.deck_id, "title": self.course.deck.title,
                 "slides": self.course.deck.slide_count,
                 "width": self.course.deck.width_px, "height": self.course.deck.height_px,
                 "warnings": len(self.course.deck.warnings)}
                if self.course.deck else None
            ),
            "params": {
                "probe_window": self.settings.probe_window,
                "max_rephrase": self.settings.max_rephrase,
                "unit_char_warn": self.settings.unit_char_warn,
            },
            "units": [
                {
                    "unit_id": u.unit_id, "slide": u.slide, "index_in_slide": u.index_in_slide,
                    "chars": len(u.text), "est_sec": u.est_sec, "text": u.text,
                    "is_page_end": u.is_page_end, "kp": u.kp,
                    "must_teach": u.must_teach,
                    "anchors": [a.element_id for a in u.anchors],
                    "ops": [op.to_dict() if hasattr(op, "to_dict") else dict(op)
                            for op in (u.ops or [])],
                }
                for u in self.script.units
            ],
            **self.slide_payload(self.state.slide_index),
            **self.snapshot(),
        }

    def slide_payload(self, slide_index: int, *, reveal_all: bool = False) -> dict:
        """一页课件的内容。

        返回的字典**直接带 `slide` 键**，调用方一律用 `send("page", **payload)` 展开。
        （曾经还额外传一次 `slide=slide_index`，两次同名 → `TypeError`。
         这个错误发生在"探针超时自动翻页"时，翻页失败又导致探针重开，
         于是同一个错误被反复重试 —— 前端看到的是一页永远翻不过去。）

        两种模式：
          - `deck`  ：真实 pptx 重绘（元素带几何/样式/段落，前端绝对定位照着画）
          - `simple`：原型自带的堆叠版式（保持旧行为，字段与原先完全一致）

        `reveal_all=True` 时不做"分步显示"——整页直接给出（手动翻页、预览用）。
        """
        if self.course.mode == "deck" and self.course.deck is not None:
            return {"slide": deck_slide_payload(self.course, slide_index,
                                                reveal_all=reveal_all)}
        slide = self.script.slide_at(slide_index)
        if slide is None:
            return {"slide": None}
        # 本页所有单元声明为"分步出现"的元素 —— 前端据此先隐藏、讲到时再展开
        reveal = {r for u in self.script.units_of(slide_index) for r in u.reveal}
        return {
            "slide": {
                "mode": "simple",
                "index": slide.index,
                "title": slide.title,
                "elements": [
                    {"element_id": e.element_id, "kind": e.kind, "text": e.text,
                     "reveal": e.element_id in reveal}
                    for e in slide.elements
                ],
            }
        }

    def snapshot(self) -> dict:
        st = self.state
        unit = self.script.unit_of(st.slide_index, st.seg_index)
        sents = unit.sentences if unit else []
        return {
            "phase": st.phase.value,
            "slide_index": st.slide_index,
            "slide_count": self.script.slide_count,
            "seg_index": st.seg_index,
            "unit": (
                {"unit_id": unit.unit_id, "text": unit.text, "kp": unit.kp,
                 "sentences": sents,
                 "done": st.unit_progress.get(unit.unit_id, 0),
                 "is_page_end": unit.is_page_end}
                if unit else None
            ),
            "pending": [q.raw for q in st.pending_questions],
            "active": st.active.raw if st.active else None,
            "resume_point": asdict(st.resume_point) if st.resume_point else None,
            "covered": [c.kp for c in st.covered],
            "covered_count": len(st.covered),
            "deferred": list(dict.fromkeys(st.deferred)),
            "q_rounds": dict(st.q_rounds),
            "comprehension": dict(st.comprehension),
            "probe_window": self.settings.probe_window,
            "intent_router": INTENT_ROUTER.status(self.settings),
            "mcp": MCP_SERVER.mode.status_dict(),
            "metrics": {
                "yield_latency_ms": [round(x, 1) for x in self.metrics.yield_ms],
                "probe": {
                    "sent": self.metrics.probe_sent,
                    "confirmed": self.metrics.probe_confirmed,
                    "interrupted": self.metrics.probe_interrupted,
                    "timeout": self.metrics.probe_timeout,
                },
                "resume": {"ok": self.metrics.resumed_ok, "fail": self.metrics.resumed_fail},
                "advance": {"auto": self.metrics.advance_auto,
                            "manual": self.metrics.advance_manual},
            },
        }


# ===========================================================================
# 课件载荷
# ===========================================================================

def deck_slide_payload(course: Course, slide_index: int, *,
                       reveal_all: bool = False) -> dict | None:
    """真实课件的一页 → 前端可直接重绘的载荷。

    元素完整下发（几何/填充/字体/段落/图片引用），因为**重绘要在前端完成**：
    后端只保证"数据对"，前端只保证"画得像"，两边不互相猜。

    `reveal_all=True` 不返回分步隐藏清单 —— 用于预览/手动翻页（整页直接可见）。
    """
    if course.deck is None:
        return None
    s = course.deck.slide_at(slide_index)
    if s is None:
        return None
    # 本页被 step/reveal 指到的锚点 —— 前端先把它们藏起来，讲到时才出现（分步显示）
    reveal: list[str] = []
    if course.plan is not None and not reveal_all:
        for u in course.plan.units_of(slide_index):
            for op in u.ops:
                if op.get("op") in ("reveal", "step"):
                    for t in [op.get("target")] + list(op.get("targets") or []):
                        if t and t not in reveal:
                            reveal.append(t)
    return {
        "mode": "deck",
        "index": s.index,
        "title": s.title,
        "width": course.deck.width_px,
        "height": course.deck.height_px,
        "background": asdict(s.background) if s.background else None,
        "media_base": f"/api/deck/media/{course.deck.deck_id}/",
        "layout": s.layout_name,
        "elements": [asdict(e) for e in s.elements],
        # 版式/母版的装饰：只画不锚（列表单独给，别混进 elements —— 否则
        # 锚点清单里会出现 "s04-la-sh01" 这种讲课时说不出来的 id）
        "decorations": [asdict(e) for e in (s.decorations or [])],
        "steps": [asdict(st) for st in s.steps],
        "reveal": reveal,
    }


# ===========================================================================
# FastAPI
# ===========================================================================

settings = load_settings()
course = load_course(settings)
script = course.script
print_course_notes(course)
settings.cache_dir.mkdir(parents=True, exist_ok=True)
tts = TTS(settings.tts_enabled, settings.tts_voice, settings.tts_rate, settings.cache_dir)
llm = LLM(settings)
MCP_SERVER = McpBrainServer()


def _seed_intent_router(s: Settings) -> None:
    """把 .env 里的小模型配置灌回意图路由器（启动时 + 每次保存后调用）。

    路由器以前只在运行时 configure，进程一重启就回到"自动复用主模型"——
    界面里改好的小模型看起来生效了，重启课堂又变回去，这类错最难查。
    两个开关也在这里一起恢复：只恢复三项的话，界面上"关掉了"的预判重启后又自己
    打开，用户看到的是"保存没生效"，而它其实是被默认值悄悄盖回去了。
    """
    INTENT_ROUTER.configure(
        enabled=s.intent_small_enabled,
        use_small_llm=s.intent_small_use_small_llm,
        small_model=s.intent_small_model,
        small_base_url=s.intent_small_base_url,
        small_api_key=s.intent_small_api_key,
    )


_seed_intent_router(settings)

app = FastAPI(title="课堂交互原型")
# 课件重绘器与样式表：上课页 / 预览页 / 审核页共用，所以必须是独立的静态文件
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.on_event("startup")
async def _startup() -> None:
    """启动后预合成固定话术 —— 压低运行时的首字延迟（§7.3 预置话术库）。"""

    async def _task_dump_loop(interval: float = 20.0) -> None:
        """把"每个协程现在停在哪儿"定期打出来（CLASSROOM_TASKDUMP=1）。

        为什么值得留一个：状态机一旦**死等**（事件没人 set、锁没人放、回调里套回调），
        服务端表面只是"安静"—— 日志里什么都没有，光看输出完全无法定位。
        这个开关一开，卡住的地方会以 `文件:行号` 的形式直接摆出来。
        """

        def _where(t: asyncio.Task) -> str:
            stack = t.get_stack()
            if not stack:
                return f"state={t._state}"
            fr = stack[-1]
            path = fr.f_code.co_filename
            return (f"{path.split('prototype')[-1].lstrip(chr(92) + '/')}:"
                    f"{fr.f_lineno} in {fr.f_code.co_name} state={t._state}")

        while True:
            await asyncio.sleep(interval)
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            print(f"[taskdump] 活动任务 {len(others)} 个", flush=True)
            for t in others:
                print(f"[taskdump]   {t.get_name():28s} {_where(t)}", flush=True)

    async def _warm() -> None:
        from machine import ACK_TEXT

        fixed = [
            ACK_TEXT,
            "到这里清楚吗？不清楚可以直接说没听懂。",
            "好，我们接着看这一页。",
            "这个问题问得好，先停一下。",
            "好，我们回到刚才那里。",
        ]
        for s in script.slides:
            fixed.append(f"这个我们得回第 {s.index + 1} 页去看一眼，就是这一行。")
        ok, total = await tts.warmup(fixed)
        print(f"[warmup] 预合成 {ok}/{total} 条固定话术")

    async def _preflight() -> None:
        """启动时摸一次模型服务：只用来**提前说清楚问题**，以及把模型预热进内存。

        两个坑都踩过，所以这里刻意做得很保守：

        1. **不要用它去判定"服务不可用"**。第一次请求常常是在等模型加载进内存
           （实测本地 9B 要十几秒），用超时去判会误杀 —— 学生会听到兜底话术，
           而服务其实是好的。真正的降级判定交给"连接被拒绝"这类**确定的**失败。
        2. **不阻塞任何东西**，它本来就是后台任务。

        它的真正价值是：冷启动的十几秒提前在开课时花掉，
        学生第一次提问就不用等模型加载了。
        """
        from machine import _explain_llm_failure

        try:
            await llm.chat([{"role": "user", "content": "回复：好"}], max_tokens=4)
            print(f"[preflight] ✅ 模型连通（已预热）：{settings.describe()}")
        except Exception as e:
            print("[preflight] ⚠ 模型暂时连不上（不拦着上课，答疑先走预置话术）：\n"
                  + _explain_llm_failure(e))

    asyncio.create_task(_warm())
    asyncio.create_task(_preflight())
    # 排查死等用：CLASSROOM_TASKDUMP=1 时定期打印"每个协程停在哪儿"
    if os.environ.get("CLASSROOM_TASKDUMP"):
        asyncio.create_task(_task_dump_loop())


#: 页面响应一律带这个：改了前端之后不该靠"教用户硬刷新"来生效
_NO_STORE = {"Cache-Control": "no-store, must-revalidate"}

#: 前端版本号。页脚（#params 那行）会显示它，用来一眼判断"我看到的是新页面还是旧页面"。
#: 改前端就+1 —— 排查过两次"我这儿没有那个按钮"，最后都是浏览器拿着旧页面。
UI_VERSION = "ui8"


def _with_asset_versions(html: str) -> str:
    """给静态资源加 `?v=<mtime>`：前端一改，浏览器就必然重新拉取。

    只靠 HTML 的 no-store 是不够的 —— CSS/JS 是浏览器缓存里最容易"以为更新了、
    其实还是上一版"的东西，而它的表现正是"按钮/浮窗不出现"。
    """
    stamps = []
    for asset in ("classroom.css", "deck.js", "script.css", "graph.css"):
        try:
            stamps.append(str(int((ROOT / "web" / asset).stat().st_mtime)))
        except OSError:
            stamps.append("0")
    v = "-".join(stamps)
    return (html
            .replace('href="/static/classroom.css"', f'href="/static/classroom.css?v={v}"')
            .replace('href="/static/script.css"', f'href="/static/script.css?v={v}"')
            .replace('href="/static/graph.css"', f'href="/static/graph.css?v={v}"')
            .replace('src="/static/deck.js"', f'src="/static/deck.js?v={v}"'))


def _page(name: str) -> HTMLResponse:
    """页面一律不缓存（内容和静态资源都带版本）。

    踩过：改了 front-end 之后有人看到的还是旧页面（HTMLResponse 不带任何缓存头，
    浏览器就按启发式规则自己缓存），表现是"你说的按钮我这儿没有"。
    """
    return HTMLResponse(
        _with_asset_versions((ROOT / "web" / name).read_text(encoding="utf-8")),
        headers=_NO_STORE,
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return _page("index.html")


@app.get("/preview", response_class=HTMLResponse)
async def preview() -> HTMLResponse:
    """课件重绘预览页：用来目视核对"解析出来的版式 vs 真实 PPT"。"""
    return _page("preview.html")


@app.get("/script", response_class=HTMLResponse)
async def script_page() -> HTMLResponse:
    """课堂脚本分支树：这门课有几支、现在走到哪一支、C 又在哪儿造了新分支。

    为什么单开一页而不是上课页里的一个浮窗：它必须能和**正在上课**的窗口同时开着。
    嵌进上课页就只能共用那条 WS 会话 —— 打开查看、刷新一次，课就断一次。
    独立页面 + 只读轮询是唯一能做到"看一眼不影响课堂"的形态（见 /api/script/live）。
    """
    return _page("script.html")


@app.get("/review", response_class=HTMLResponse)
async def review() -> HTMLResponse:
    """课堂脚本审核页（《课堂交互设计》§2.7 可审核性）。

    课件 + 脚本是课堂的输入，它们错了后面全套机制都会忠实地讲错。
    所以要有**最小的人工审核面**：按页列出单元、锚点、时长、低置信度锚点、课件操作。
    """
    import html as _html

    if course.mode != "deck" or course.deck is None or course.plan is None:
        return HTMLResponse(
            "<div class='review'><h1>课堂脚本审核</h1>"
            "<p class='hint'>当前是原型自带的手写演示课件，没有可审核的生成结果。"
            "用 <code>DECK_ID=&lt;id&gt;</code> 指定一份已解析的真实课件后再看这里。</p>"
            "<p><a href='/api/decks'>已解析的课件列表</a> · "
            "<a href='/api/audit'>体检结果(JSON)</a> · "
            '<a href="/graph">课堂走向图</a></p></div>',
            headers=_NO_STORE)

    from script_gen import audit_plan

    deck, plan = course.deck, course.plan
    idx = deck.anchor_index()
    audit = audit_plan(deck, plan, settings.unit_char_warn)
    bad = [line for line in audit if "❌" in line]
    warn = [line for line in audit if "⚠️" in line]

    def esc(s) -> str:
        return _html.escape(str(s))

    parts: list[str] = []
    cov = course.anchor_report
    parts.append(f"""<div class="review">
<h1>课堂脚本审核 · 《{esc(deck.title)}》</h1>
<p class="hint">课件 {deck.slide_count} 页 / {deck.width_px}×{deck.height_px}px ·
脚本来源 <b>{esc(plan.source)}</b>{(' · ' + esc(plan.model)) if plan.model else ''} ·
单元 {cov.get('units', 0)} 个 · 预计 {cov.get('est_sec', 0) / 60:.1f} 分钟 ·
最长单元 {cov.get('longest_unit_sec', 0):.1f} 秒（学生最坏等待时长）</p>
<p>
  <span class="ok">锚点命中率 {cov.get('anchor_hit', 0)}/{cov.get('anchors', 0)}
  = {cov.get('anchor_hit_rate', 0) * 100:.1f}%</span>
  <span class="hint">（段落级 {cov.get('para_level_anchors', 0)} · 低置信
  {cov.get('low_confidence_anchors', 0)}）</span>
  · 课件操作 {cov.get('ops', 0)} 个（非法 {cov.get('ops_invalid', 0)}）
  · <span class="{'bad' if bad else 'ok'}">错误 {len(bad)}</span>
  · <span class="warn">告警 {len(warn)}</span>
</p>
<p class="hint">页面：<a href="/">上课页</a> ·
<a href="/preview?deck={esc(deck.deck_id)}">课件重绘预览</a> ·
<a href="/script">分支树</a> ·
<a href="/graph">课堂走向图</a> ·
<a href="/api/audit">体检 JSON</a> ·
<a href="/api/decks">课件列表</a></p>""")

    if bad:
        parts.append("<h2>❌ 必须先处理</h2><pre>" + esc("\n".join(bad[:40])) + "</pre>")
    if plan.warnings:
        parts.append("<h2>生成过程告警</h2><pre>"
                     + esc("\n".join(plan.warnings[:40])) + "</pre>")

    parts.append(f"<h2>按页审核（{len(plan.units)} 个单元）</h2>")
    for s in deck.slides:
        us = plan.units_of(s.index)
        parts.append(f"<h3><span class='pagetag'>第 {s.index + 1} 页</span>"
                     f"{esc(s.title or '（无标题）')}"
                     f"<span class='hint'> · 元素 {len(s.elements)} · 单元 {len(us)}</span></h3>")
        if not us:
            parts.append("<p class='warn'>这一页没有任何讲解单元（课堂上会直接跳过）。</p>")
        for u in us:
            n = len(u.text)
            klass = "bad" if n > settings.unit_char_warn else ("warn" if n > 180 or n < 40 else "ok")
            anchors = "".join(
                f"<span class='a'>{esc(a.get('element_id'))}"
                f"<span class='hint'> {esc(idx.get(a.get('element_id', ''), '❌不存在')[:26])}</span></span>"
                for a in u.anchors)
            ops = "".join(
                f"<span class='op'>{esc(o.get('op'))}@{esc(o.get('at_sentence', 1))}→"
                f"{esc(o.get('target') or ','.join(o.get('targets') or []))}</span>"
                for o in u.ops)
            note = f" · <span class='hint'>{esc(u.note)}</span>" if u.note else ""
            parts.append(
                f"<div class='unit'><div class='meta'>"
                f"<span class='{klass}'>{n} 字 · 约 {u.est_sec():.1f} 秒</span>"
                f" · kp {esc(','.join(u.kp)) or '—'}"
                f"{' · 页尾' if u.is_page_end else ''}{note}</div>"
                f"<div style='margin:6px 0'>{esc(u.text)}</div>"
                f"<div class='meta'>锚点：{anchors or '<span class=bad>无</span>'}</div>"
                f"<div class='meta'>课件操作：{ops or '—'}</div></div>")
    parts.append("<p class='hint'>提示：锚点里的 id 可以在"
                 f"<a href='/preview?deck={esc(deck.deck_id)}'>重绘预览</a>"
                 "里点一下，直接在课件上看到它高亮到哪一行 —— 这是验收锚点质量最快的办法。</p>")
    parts.append("</div>")
    return HTMLResponse("".join(parts), headers=_NO_STORE)


# ===========================================================================
# 课堂脚本分支树（只读观测面）
# ===========================================================================
#
# 两个端点分工：
#   /api/script/tree  静态结构（页 → 单元 → 变体），拉一次。
#   /api/script/live  运行期叠加（当前位置 / 被换成了哪一支 / 池内候选），每秒轮询。
# 拆成两个，是因为前者（177 单元 × 2 变体）不会变、后者每秒都变 —— 合起来就等于
# 每秒把整棵树重算一遍再推给浏览器，那是把观测页做成压测。

#: 正在上课的 Runner。**只有观测面、以及「切换当前课件」的准入判断读它**，别处一律不许碰。
#:
#: 为什么必须登记：分支树页要和正在上课的窗口同时开着，而 Runner 原先只是
#: ws_endpoint 里的局部变量 —— 除它自己，没人拿得到那台状态机。想"另开一条 WS 去问"
#: 等于新建第二节课（第二个状态机、第二条音频流），那不是观测，是复制课堂。
#: 所以这里只在建 / 销两个时刻各改一行，读它的 /api/script/live 只读属性。
_LIVE_RUNNERS: list[Runner] = []

#: /api/script/live 里池候选的条数上限。
#: 整节课跑下来池里会积下几百条已作废的货（每一批预置货都会被后来的装载判过期），
#: 每秒把它们原样推一遍是这个页面最没意义的那部分开销。alive 的那几条永远全给，
#: 会撞到上限的只有 consumed / expired。
POOL_VIEW_LIMIT = 120


def _live_machine() -> Machine | None:
    """当前正在上的那台状态机；没有课堂在跑 → None。"""
    for runner in reversed(_LIVE_RUNNERS):
        machine = getattr(runner, "machine", None)
        if machine is not None:
            return machine
    return None


def _live_course() -> Course | None:
    """正在上课的那一份课件；没有课堂在跑 → None。

    为什么取**最近**登记的那一个：页面要回答的是"此刻课堂在用哪份课件"，不是
    "历史上用过哪几份"。Runner 一建立就把 course 存进 `_course`（见 Runner.__init__），
    所以这里和 `_live_machine` 一样只读、不碰状态机、不发消息。
    """
    for runner in reversed(_LIVE_RUNNERS):
        live = getattr(runner, "_course", None)
        if live is not None:
            return live
    return None


def _variant_row(raw: Any) -> dict:
    """一个变体 → 树上的一根分支。

    dict 与 UnitVariant 两种形态都要认：变体在 plan.json 里是 dict、在池里是对象，
    只认一种的话另一半分支会**静默消失** —— 而这页面存在的意义正是"一次看全"。
    est_sec 缺省时按字数派生（与 UnitVariant.__post_init__ 同一把尺子），
    否则树上会出现"0 秒的变体"这种一眼就不可能是真的数字。
    """
    d = raw if isinstance(raw, dict) else (
        raw.to_dict() if hasattr(raw, "to_dict") else {})
    text = str(d.get("text") or "")
    est = float(d.get("est_sec") or 0.0) or round(len(text) / 5.0, 1)
    return {
        "variant_id": str(d.get("variant_id") or ""),
        "label": str(d.get("label") or ""),
        "text": text,
        "est_sec": est,
    }


def _live_float(value: Any, default: float = 0.0) -> float:
    """脏数字兜底（观测面专用）。

    为什么观测面自己兜一层：route 的字段来自 C 的一次写入，也来自 apply_config
    之类可以塞进任意 JSON 的入口。一条 fork_node 是 "abc" 的路线不该让
    /api/script/live 500 —— 这一页是"看一眼课堂"，它挂了不代表课堂挂了，
    但看起来就是课堂挂了。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _live_route_row(route: Any) -> dict:
    """一条路线 → live 候选行里的 route 子对象（几何一次给全）。

    为什么逐个字段抄出来，而不是 route.to_dict() 直接透传：这份载荷是**前端画图**的
    契约。透传等于把 Route 的内部字段名变成浏览器里的字段名 —— Route 哪天多一个内部
    字段（比如重算标记），载荷就跟着变，而前端的几何不会因此变好，只是多了一个没人读的键。
    显式列出来的另一个好处：这里列的这几个键，就是"画一条路线所需"的完整集合。

    为什么 steps 也逐字段抄：前端要读的是 unit_id 序列（**长度本身也是信息**：
    revisit 重讲几格全靠它），而 RouteStep 允许携带几千字的 text —— 抄一遍能保证
    形状稳定，顺手把 dict 形态的脏步骤也统一成同一种形状。
    """
    def pick(key: str, default: Any = None) -> Any:
        """Route 对象与它 JSON 反解回来的 dict 都要认（C 从 JSON 里回填时是 dict）。"""
        if isinstance(route, dict):
            return route.get(key, default)
        return getattr(route, key, default)

    steps: list[dict] = []
    for step in (pick("steps") or []):
        if hasattr(step, "to_dict"):
            raw = step.to_dict()
        elif isinstance(step, dict):
            raw = dict(step)
        else:
            continue
        steps.append({
            "unit_id": str(raw.get("unit_id") or ""),
            "variant_id": str(raw.get("variant_id") or ""),
            "label": str(raw.get("label") or ""),
            "text": str(raw.get("text") or ""),
            "est_sec": _live_float(raw.get("est_sec")),
        })
    return {
        "route_id": str(pick("route_id", "") or ""),
        "kind": str(pick("kind", "") or ""),
        "fork_node": int(_live_float(pick("fork_node", 0))),
        "merge_node": int(_live_float(pick("merge_node", 0))),
        "steps": steps,
        # 符号约定沿用 Route：负数 = 省时间 / 正数 = 多花时间。观测面不改符号，
        # 前端要画"省还是花"就得拿同一个数判断，两边各写一次判断迟早分叉。
        "est_sec": _live_float(pick("est_sec", 0.0)),
        "reason": str(pick("reason", "") or ""),
    }


def script_tree_payload(course: Course) -> dict:
    """整门课的分支结构（页 → 单元 → 变体），纯读。

    为什么不学 /review 直接拼 HTML：折叠状态、当前位置高亮、候选池叠加都要每秒变，
    服务端渲染就等于每秒重算几百个节点再整页替换。静态结构拉一次、变化的部分交给
    /api/script/live，是同一份数据更省事的用法。

    course.mode != "deck"（或没有 plan）时**不报错**：手写演示课件本来就没有分支可看，
    这是"这份课件没有这个视图"，不是"服务器坏了"。所以照旧 200，用 ok=false + hint
    说清原因，前端据此显示一句提示，而不是给一张白页让人猜。
    """
    deck, plan, script = course.deck, course.plan, course.script
    if course.mode != "deck" or deck is None or plan is None:
        return {
            "ok": False,
            "mode": course.mode,
            "hint": ("当前是原型自带的手写演示课件（没有 plan.json），没有分支可看。"
                     "用 DECK_ID=<课件id> 指定一份已解析的真实课件，再跑 "
                     "python run.py plan <课件id> 备一版带变体的脚本即可。"),
            "meta": {
                "deck_id": "",
                "deck_title": script.title,
                "total_slides": script.slide_count,
                "total_units": len(script.units),
                "variants_total": 0,
                "plan_source": "",
            },
            "pages": [],
        }

    pages: list[dict] = []
    variants_total = 0
    for slide in deck.slides:
        units: list[dict] = []
        for u in script.units_of(slide.index):
            variants = [_variant_row(v) for v in (u.variants or [])]
            variants_total += len(variants)
            units.append({
                "unit_id": u.unit_id,
                "index_in_slide": u.index_in_slide,
                "text": u.text,
                "est_sec": u.est_sec,
                "kp": list(u.kp),
                "is_page_end": bool(u.is_page_end),
                "anchors_count": len(u.anchors),
                "ops_count": len(u.ops),
                "variants": variants,
            })
        # 没有单元的页照样留下：页边界本身是这份结构的一部分，
        # 而"这一页课堂上会被整页跳过"恰恰是审核时最该看见的一件事。
        pages.append({"index": slide.index, "title": slide.title, "units": units})

    return {
        "ok": True,
        "mode": course.mode,
        "hint": "",
        "meta": {
            "deck_id": deck.deck_id,
            "deck_title": deck.title,
            "total_slides": deck.slide_count,
            "total_units": len(script.units),
            "variants_total": variants_total,
            "plan_source": plan.source,
        },
        "pages": pages,
    }


@app.get("/api/script/tree")
async def api_script_tree():
    """整门课的分支结构（一次拉取）。只读：不碰状态机、不碰池子。"""
    return script_tree_payload(course)


@app.get("/api/script/live")
async def api_script_live():
    """运行期叠加：走到哪一格、哪些格被换成了哪一支、池里还剩什么。

    **零副作用是硬约束，不是风格**：课堂正讲着的时候这个端点每秒都会被调用一次，
    所以它只读三处 —— machine.state / machine.metrics.events / machine.director.pool。
    不推进状态机、不创建 Runner、不消费 EventBus。

    为什么特意点出 EventBus：MCP 的 get_events 是"读一次就没了"的消费型接口，
    拿它做观测等于把主课堂的事件偷走 —— 课堂那边少了事件，这个页面看起来却一切正常，
    事后没有任何东西能对账。观测面只许读留痕，不许读队列。

    switched 的权威来源是 variant_applied 事件（而不是从 unit.text 反推）：
    文本一样的两个变体分不出来，而"这一格到底被换成了哪一支"必须能回答。
    probe_after 来自 variant_replay_end（同一格讲完/被打断时记的那条）：
    不跨事件 join 这一次，前端就只能知道"换了什么"、不知道"换过去之后学生怎么样"。
    """
    machine = _live_machine()
    if machine is None:
        return {"running": False}

    st = machine.state
    events = list(machine.metrics.events)     # 复制：下面的归集不许动原列表

    # 当前单元：与状态机的 _cur_unit_id 同一套判定，但这里自己算 ——
    # 观测面调状态机的私有方法，等于把两件事绑死（它哪天顺手改一点，这里就静默错）。
    live_script = getattr(machine, "script", None) or script
    current_unit = ""
    if st.phase in (Phase.ANSWERING, Phase.RESUMING) and st.active is not None:
        current_unit = str(getattr(st.active, "at_unit", "") or "")
    if not current_unit:
        unit = live_script.unit_of(st.slide_index, st.seg_index)
        current_unit = unit.unit_id if unit else ""
    unit_obj = live_script.unit(current_unit) if current_unit else None
    spoken = int(st.unit_progress.get(current_unit, 0)) if current_unit else 0

    switched: dict[str, list[dict]] = {}
    routed: list[dict] = []
    replay_after: dict[str, str] = {}
    for ev in events:
        kind = ev.get("kind")
        if kind == "variant_applied":
            unit_id = str(ev.get("unit") or "")
            switched.setdefault(unit_id, []).append({
                "unit": unit_id,
                "decided_at": str(ev.get("decided_at") or ""),
                "variant_id": str(ev.get("variant_id") or ""),
                "label": str(ev.get("label") or ""),
                "candidate_id": str(ev.get("candidate_id") or ""),
                "probe_before": str(ev.get("probe_before") or ""),
                "probe_after": "",
                "at": str(ev.get("at") or ""),
            })
        elif kind == "variant_routed":
            routed.append({
                "site": str(ev.get("site") or ""),
                "target": str(ev.get("target") or ""),
                "candidate_id": str(ev.get("candidate_id") or ""),
                "variant_id": str(ev.get("variant_id") or ""),
                "at": str(ev.get("at") or ""),
            })
        elif kind == "variant_replay_end":
            replay_after[str(ev.get("unit") or "")] = str(ev.get("probe_after") or "")
    for rows in switched.values():
        for row in rows:
            row["probe_after"] = replay_after.get(row["unit"], "")

    director = getattr(machine, "director", None)
    stats: dict = {}
    pool_rows: list[dict] = []
    pool_total = 0
    if director is not None:
        stats = dict(getattr(director, "stats", {}) or {})
        try:
            cands = director.pool.items()
        except Exception:
            cands = []      # 池子坏了不该让观测页 500：它是观测，不是课堂逻辑
        pool_total = len(cands)
        rows: list[dict] = []
        for c in cands:
            variant = getattr(c, "variant", None)
            row = {
                "candidate_id": str(getattr(c, "candidate_id", "")),
                "target_unit": str(getattr(c, "target_unit", "")),
                "source": str(getattr(c, "source", "")),
                "status": str(getattr(c, "status", "")),
                "variant_id": str(getattr(variant, "variant_id", "")),
                "label": str(getattr(variant, "label", "")),
            }
            # 路线行多一个 route 子对象；**变体行一个键都不加**（"route": None 也能跑，
            # 但那样每一行都挂着一个恒为空的字段，"这一行画的是跨格走向还是就地重讲"
            # 就无法用键的**有无**来判了 —— 前端要为两种情况各写一套判空）。
            route = getattr(c, "route", None)
            if route is not None:
                row["route"] = _live_route_row(route)
            rows.append(row)
        # alive 全给（CAP=5，本来就少），作废的只给最近一批（理由见 POOL_VIEW_LIMIT）。
        alive = [r for r in rows if r["status"] == "alive"]
        rest = [r for r in rows if r["status"] != "alive"][-POOL_VIEW_LIMIT:]
        pool_rows = alive + rest

    return {
        "running": True,
        "current": {
            "slide_index": st.slide_index,
            "seg_index": st.seg_index,
            "phase": st.phase.value,
            "unit_id": current_unit,
            "unit_progress": {
                "spoken": spoken,
                "total": len(unit_obj.sentences) if unit_obj is not None else 0,
            },
        },
        "switched": switched,
        "routed": routed,
        "pool": pool_rows,
        "pool_total": pool_total,
        "stats": stats,
    }


# ===========================================================================
# 横向提交图（只读观测面）
# ===========================================================================
#
# 与上面的分支树是**同一份数据的另一种投影**：
#   分支树竖着排（页 → 单元 → 变体），回答"这门课有几支、现在走到哪一支"；
#   提交图把全部单元摊成一条主线，再把支线按真实跨度画出去（从岔出格到汇回格），
#   回答"这一课有几条岔路、从哪岔出、在哪汇回、跨了几格"。
#   支线今天只有一个来源：备课期预置路线（plan.json 的 routes，走本载荷的顶层 routes）；
#   就地变体已停产，nodes[].branches 因此在真实课件上是空数组 —— 契约不改，只是没货。
# 两个页面各自成型、不共用一份 JSON：共用就意味着任何一边改字段都要先吵一次，
# 而这两个问题本来就不是一个。

#: 泳道宽度上限（含主线那一列）。变体数量来自 plan.json，可以被后来的备课流程改大 ——
#: 一个被塞了 40 个变体的单元不该把整张图压成一根看不见的细线。
#: 截断必须让前端说出来（而不是悄悄少画几支），所以 meta.truncated 是有意进契约的。
#: 真实课件的就地变体已停产（归零），这条上限今天只剩"旧计划还在用"的那一档会碰到。
_GRAPH_LANE_CAP = 12


def _graph_route_row(route: Any, total_nodes: int) -> dict | None:
    """一条预置路线 → 顶层 routes 数组里的一行；脏/非法一律返回 None（只丢不抛）。

    为什么与 live 行里的 route 子对象**逐字同形**：前端只有一套路线渲染（几何来自
    fork_node → merge_node，文案来自 kind / steps / est_sec / reason）。两个来源给两种形状，
    等于让前端为"同一个东西"写两遍，而两遍迟早会画得不一样。
    唯一多出来的键是 source: "preset" —— 它回答"这条线开课前就在，还是课上现造的"，
    也正好是前端"实线 / 虚线"的判据。

    为什么这里再校验一次而不是信 course.script.routes：script.routes 是解析期过的
    （course.parse_plan_routes），但这份载荷也可能被喂进一个手搓的 LessonScript
    （测试、以及将来别的入口）。一条 fork_node 越过课尾的路线画到画布外，比不画更糟。
    """
    total = max(0, int(total_nodes))
    try:
        obj = route if isinstance(route, Route) else Route.from_dict(
            route if isinstance(route, dict) else {})
        obj.validate(total)      # 越界 / revisit 空 steps / finish 不落课尾都会炸
    except Exception:            # kind 非法 / 结构越界 / 字段读不出来
        return None
    row = _live_route_row(obj)
    row["source"] = "preset"
    return row


def script_graph_payload(course: Course) -> dict:
    """整门课的横向提交图：主线 + 支线插入点 + 泳道。纯读，零副作用。

    为什么 n 直接取 script.units 里的位置：状态机装载候选时用的
    script.order_index() 说的就是"它在 units 列表里的下标"。图上另起一套编号，
    前端点第 K 格、后端哼第 K 格就会**静默**错开 —— 不崩不报错，只是换错了地方。
    所以这里的 n 不是"排出来的"，它和 order_index 在源头就是同一个东西。

    为什么零副作用是硬约束而不是风格：这个端点会和正在上课的窗口同时被调用
    （/api/script/live 每秒一次的性质同样适用）。一旦它顺手建 Runner、推进状态机、
    或者取一次 MCP 的事件队列，课堂那边就安静地少一次决策，而这一页看起来完全正常。
    所以这里只读 course 这一个对象，别的什么都不碰。

    为什么要显式的 fork/merge（而不让前端拿 n 自己推）：支线是"从某一格岔出、
    在后面的某一格汇回主线"，跟主线的顺序不是同一个东西。今天每条预置支线都是
    岔出后绕一步就回主线（fork=n、merge=n+1），但那是**今天的产出规则**，不是结构本身 ——
    把跨度写成字段，等 Agent-C 产出跨多个单元的支线时，前端一行都不用改。
    走到课尾那一步没有下一格可回，merge 就取 len(nodes)：前端把它画成"退出课堂"，
    而不是再发明一个 -1 之类的哨兵值（哨兵迟早会和合法的 n 撞上）。

    为什么 course.mode != "deck"（或没有 plan）要 200 + ok=false：
    手写演示课件本来就没有单元和 plan.json，也就没有主线可摊 —— 那是
    "这份课件没有这个视图"，不是"服务器坏了"。抛错或 404 的表现是审核同事
    看到一片红，而课堂本身好好的。降级时形状照旧（字段一个不少、数值全 0），
    前端就不必为两种情况各写一套判空。

    为什么预置路线（plan.json 的 routes）走**顶层 routes** 而不是塞进 nodes[].branches：
    路线是跨格的（从 #108 岔出、在 #113 汇回），它不属于任何一格 —— 塞进某一格的
    branches 就等于宣称"这条线挂在那一格上"，而岔出格只是它的起点。
    nodes[].branches 的既有契约（就地变体，含 fork/merge/nodes）因此一个字都不改：
    旧 plan.json 还有变体时照旧画，真实课件上它现在是空数组。
    routes 与 live 候选行里的 route 子对象逐字同形（只多 source: "preset"），
    前端才能用同一套渲染画"备课期预置的"与"课上现造的"两种来源。
    """
    deck, plan, script = course.deck, course.plan, course.script
    if course.mode != "deck" or deck is None or plan is None:
        # 两种"没有这个视图"的原因必须分开说：一个是课件不对，一个是课件对了但没备课。
        if course.mode != "deck" or deck is None:
            hint = ("当前是原型自带的手写演示课件（没有 plan.json），没有提交图可看。"
                    "用 DECK_ID=<课件id> 指定一份已解析的真实课件，再跑 "
                    "python run.py plan <课件id> 备一版带变体的脚本即可。")
        else:
            hint = (f"课件在，但这门课还没有备课产物（plan.json）：没有讲解单元、"
                    f"也没有预置变体，所以没有提交图可看。"
                    f"跑 python run.py plan {deck.deck_id} 备一版再回来。")
        return {
            "ok": False,
            "mode": "deck" if course.mode == "deck" else "simple",
            "hint": hint,
            "meta": {
                "deck_id": "",
                "deck_title": "",
                "plan_source": "",
                "total_slides": 0,
                "total_units": 0,
                "mainline_nodes": 0,
                "preset_branches": 0,
                #: 备课期预置路线的条数。preset_branches 保留原语义（就地变体分支数），
                #: 两者**不许互相顶替**：真实课件上前者是 14、后者是 0。
                "preset_routes": 0,
                "lanes_width": 0,
                "terminal_branches": 0,
                "truncated": False,
            },
            "lanes": {"width": 1},
            "nodes": [],
            "routes": [],          # 降级也要有这个键（空数组，不是缺键）
        }

    units = list(script.units)
    total_nodes = len(units)
    slide_titles = {s.index: s.title for s in deck.slides}

    # 备课期预置路线：按 fork_node 升序，脏数据只丢不抛（见 _graph_route_row）。
    # 为什么按 fork 排而不是照 plan.json 的书写顺序：图的横轴是"课的时间轴"，
    # 而 routes 的产出顺序由产出规则决定（先按页扫、再按跨度），与画图的顺序无关。
    preset_routes = [row for row in (
        _graph_route_row(rt, total_nodes)
        for rt in (getattr(script, "routes", None) or [])
    ) if row is not None]
    preset_routes.sort(key=lambda r: (r["fork_node"], r["merge_node"], r["route_id"]))

    # 先把每个单元的预置变体归一化出来，再统一定泳道宽度：
    # 宽度由"整门课最大的那一格"决定，边算边定会让先算到的节点用窄宽度、后面的用宽宽度 ——
    # 同一张图里两套宽度，前端没法拿一个常量去定位。
    rows_by_unit = [[_variant_row(v) for v in (u.variants or [])] for u in units]
    widest = max((len(rows) for rows in rows_by_unit), default=0)
    width = max(2, min(_GRAPH_LANE_CAP, 1 + widest))
    truncated = 1 + widest > _GRAPH_LANE_CAP

    nodes: list[dict] = []
    branches_total = 0
    terminal_branches = 0
    for n, (u, rows) in enumerate(zip(units, rows_by_unit)):
        # 汇回点：今天每条预置支线都是岔出后绕一步就回主线（fork=n → merge=n+1）；
        # 最后一格的下一步就是"下课"，所以它汇到 len(nodes)。
        merge_to = n + 1 if n + 1 < total_nodes else total_nodes
        # lane 从 1 起：0 那一列留给主线，前端按 lane 绝对定位时不用再 +1 换算。
        # 超出泳道宽度的支线在这里被截掉（meta.truncated 负责说明这件事）。
        branches = [{
            "branch_id": f"preset:{u.unit_id}|{row['variant_id']}",
            "source": "preset",
            "variant_id": row["variant_id"],
            "label": row["label"],
            "text": row["text"],
            "est_sec": row["est_sec"],
            "lane": lane,
            # insert_node 与 fork_node 今天同值：老前端按 insert_node 挂支线，保留它不返工。
            "insert_node": n,
            "insert_unit": u.unit_id,
            "fork_node": n,
            "merge_node": merge_to,
            # 支线上的节点序列。今天只有一格（"换一种说法"讲的还是这一格的内容），
            # 但结构按任意跨度留好：将来跨多个单元的支线直接往这个列表里追加，
            # 前端不必改形状。内容照抄归一化后的变体行，不另造文字。
            "nodes": [{
                "node_index": 0,
                "variant_id": row["variant_id"],
                "label": row["label"],
                "text": row["text"],
                "est_sec": row["est_sec"],
            }],
        } for lane, row in enumerate(rows[:width - 1], start=1)]
        branches_total += len(branches)
        # 按契约定义数（merge_node == len(nodes)），而不是按 n 反推 —— 跨度规则一放宽，
        # 反推的那一版就会静默少数。
        terminal_branches += sum(1 for b in branches if b["merge_node"] == total_nodes)
        nodes.append({
            "n": n,
            "unit_id": u.unit_id,
            "slide_index": u.slide,
            "page_index": u.slide,
            "index_in_slide": u.index_in_slide,
            "slide_title": str(slide_titles.get(u.slide) or ""),
            # 页边界按"相邻两个单元的 slide 有没有变"算，而不是读 unit.is_page_end：
            # 那个标记是备课时的假设，少标或多标一个页框就直接画错边界。
            "is_page_start": n == 0 or units[n - 1].slide != u.slide,
            "is_page_end": n + 1 == len(units) or units[n + 1].slide != u.slide,
            "text": u.text,
            # 卡面上显示的那句话总结：LectureUnit 保证非空（空则用正文首句兜底），
            # 前端直接用，不判空。
            "summary": u.summary,
            "est_sec": float(u.est_sec),
            "kp": list(u.kp),
            "anchors_count": len(u.anchors),
            "ops_count": len(u.ops),
            "branches": branches,
        })

    # 预置路线里汇回课尾的（finish，或跨度正好到课尾）同样算课尾出口：
    # 前端"课尾分支"那一栏与这个数必须对得上 —— 两个来源分开数就会在页面上说两套话。
    terminal_branches += sum(1 for r in preset_routes if r["merge_node"] == total_nodes)

    return {
        "ok": True,
        "mode": "deck",
        "hint": "",
        "meta": {
            "deck_id": deck.deck_id,
            "deck_title": deck.title,
            "plan_source": plan.source,
            "total_slides": deck.slide_count,
            "total_units": len(units),
            "mainline_nodes": len(nodes),
            "preset_branches": branches_total,
            "preset_routes": len(preset_routes),
            "lanes_width": width,
            "terminal_branches": terminal_branches,
            "truncated": truncated,
        },
        "lanes": {"width": width},
        "nodes": nodes,
        "routes": preset_routes,
    }


@app.get("/api/script/graph")
async def api_script_graph():
    """整门课的提交图（一次拉取）。只读：不碰状态机、不碰池子、不建 Runner。"""
    return script_graph_payload(course)


@app.get("/graph", response_class=HTMLResponse)
async def graph_page() -> HTMLResponse:
    """横向提交图页：先看清整门课的主线，再看每个插入点上挂了几条支线。

    为什么和 /script 分成两页：竖着排回答"现在在哪一支"，横着排回答"这一格能换成
    哪几种说法、换掉之后落在哪"。同一份数据两种投影，两页的加载节奏也不同
    （结构一次拉取，运行期叠加才需要轮询）。
    """
    return _page("graph.html")


@app.get("/hub", response_class=HTMLResponse)
async def hub_page() -> HTMLResponse:
    """启动台：把这一套里"人该看的页面"一屏列全。

    为什么需要它：双击 start.cmd 起的这套原型，人类页面已经有上课页 / 课堂走向图 /
    分支树 / 脚本审核 / 课件预览五个。启动器一次只能开一个地址 —— 五个标签页一起弹
    主次不分（审核页与预览页多数时候并不需要看），只开上课页则等于把其余四个藏起来。
    所以双击后落在这一页：它是入口，不是一个新功能。

    本页只读：不碰状态机、不建 Runner、不消费事件。加载时各读一次
    /api/script/graph 与 /api/script/live 用来显示"课堂在跑没有"，**不轮询** ——
    每秒轮询的活儿已经由 /graph 与 /script 各自在做，这里再开一条只是白烧 CPU。
    """
    return _page("hub.html")


@app.get("/preprocess", response_class=HTMLResponse)
async def preprocess_page() -> HTMLResponse:
    """预处理操作台：上传或选一份 PPT → 解析/备课/预置路线一条链 → 看进度与日志。

    为什么要有这一页：三段预处理以前只有命令行入口，重备一次要记住三条命令和两个目录；
    而"给存量课件补总结"这种只动一层的事，在命令行里还得先想清楚哪个命令不会碰正文。
    页面把这两件事都变成按钮 —— 点哪个按钮，代码里就只有那一条路径。
    """
    return _page("preprocess.html")


@app.get("/api/deck/slide/{deck_id}/{index}")
async def api_deck_slide(deck_id: str, index: int, reveal: str = ""):
    """某一页的重绘载荷（预览页用；也方便调试解析结果）。

    `?reveal=all` 会把"分步隐藏"关掉，整页直接显示 —— 目视核对版式时用这个。
    """
    from deck.store import load_deck
    from course import Course as _Course

    try:
        deck = course.deck if (course.deck is not None and course.deck.deck_id == deck_id) \
            else load_deck(deck_id)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    plan = course.plan if (course.plan is not None and course.deck_id == deck_id) else None
    fake = _Course(script=script, mode="deck", deck=deck, plan=plan)
    reveal_all = reveal in ("all", "1", "true")
    slide = deck_slide_payload(fake, index, reveal_all=reveal_all)
    if slide is None:
        return JSONResponse({"error": f"没有第 {index + 1} 页"}, status_code=404)
    return {"slide": slide, "slide_count": deck.slide_count, "deck": deck.deck_id}


@app.get("/api/deck/{deck_id}/plan")
async def api_deck_plan(deck_id: str):
    from deck.store import plan_json

    p = plan_json(deck_id)
    if not p.exists():
        return JSONResponse({"error": "还没有 plan.json"}, status_code=404)
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))


@app.get("/api/audio/{name}")
async def audio(name: str):
    p = (tts.dir / name).resolve()
    if not str(p).startswith(str(tts.dir.resolve())) or not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="audio/mpeg")


@app.get("/api/deck/media/{deck_id}/{name}")
async def deck_media(deck_id: str, name: str):
    """真实课件的图片（从 pptx 里抽出来的，或外链下载下来的）。"""
    from deck.store import media_dir

    base = media_dir(deck_id).resolve()
    p = (base / name).resolve()
    if base not in p.parents or not p.is_file():     # 防目录穿越
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


@app.get("/api/decks")
async def api_decks():
    """已解析的课件列表（审核页与 CLI 都用它）。"""
    from deck.store import list_decks

    return {"decks": list_decks(), "active": course.deck_id}


# ===========================================================================
# 预处理操作台（/preprocess）
# ===========================================================================

def _active_payload() -> dict:
    """当前上课用的那份课件 —— 页面顶部常驻那一行的全部依据。

    为什么把 env_file 一起给：界面上"已写回 .env"这句话必须指向**一份具体文件**。
    用 `--env xxx` 或 DSH_ENV_FILE 起服务时，用户手里有好几份 .env，光说"写了"
    等于让人去猜是哪一份。

    simple 档给一个能显示的中文名（"手写演示课件"）而不是空串：页面渲染的是
    「当前上课：《…》」，空串会让它变成一对空书名号，看着像坏了。
    """
    deck = course.deck
    simple = course.mode != "deck" or deck is None
    return {
        "deck_id": course.deck_id,
        "title": "手写演示课件" if simple else deck.title,
        "mode": "simple" if simple else "deck",
        "env_file": str(env_file()),
    }


def _live_payload() -> dict:
    """课堂此刻在跑没有、在跑的话是哪一份。

    running 是**连接数**（`_LIVE_RUNNERS` 的长度），不是一次布尔：换课件被拦时
    得说清"有几个课堂开着"，否则用户关掉一个以为就好了。
    """
    live = _live_course()
    return {
        "running": len(_LIVE_RUNNERS),
        "deck_id": live.deck_id if live is not None else "",
        "title": (live.deck.title if live is not None and live.deck is not None else ""),
    }


@app.get("/api/preprocess/library")
async def api_preprocess_library():
    """课件库快照：源库（pptx/ 那侧）+ 已解析课件（decks/ 那侧）+ 最近作业 + 当前课件。

    两样都给，缺一不可：sources 看得见"传上来了还没解析"的文件，decks 看得见"源文件
    已经不在了但还能上课"的那份（仓库里那份真实课件就是这样）。只给一侧，页面就会在
    decks/ 明明有课件时显示"空课件库"，而补总结恰恰只有那一侧能点。

    active / live 也挂在同一个响应里：这一页的每一个按钮都要看"当前是哪份、课堂在不在跑"
    才决定灰不灰，另开一个端点就必然出现两个快照不同步的那一瞬 —— 按钮状态和列表状态
    打架，用户看到的是"点了没反应"。
    """
    from preprocess import deck_rows, library, list_jobs

    return {"ok": True, "sources": library(), "decks": deck_rows(), "jobs": list_jobs(),
            "active": _active_payload(), "live": _live_payload()}


@app.post("/api/preprocess/upload")
async def api_preprocess_upload(file: UploadFile):
    """收一份 pptx 进源库。失败一律 **400 + 人话**，不是 500。

    大小先按 file.size 判一刀再读：UploadFile 是边收边落盘的，超限文件若先
    `await file.read()` 就是先把整个字节流读进内存再拒绝。
    """
    from preprocess import MAX_UPLOAD_MB, save_upload

    limit = MAX_UPLOAD_MB * 1024 * 1024

    def _too_big(size: int) -> JSONResponse:
        return JSONResponse(
            {"ok": False,
             "error": f"文件 {size / 1048576:.1f} MB，超过 {MAX_UPLOAD_MB} MB 上限 —— "
                      "课件正常只有十几 MB，先确认没传错文件"},
            status_code=400)

    try:
        if file.size is not None and file.size > limit:
            return _too_big(int(file.size))
        data = await file.read(limit + 1)
        if len(data) > limit:
            return _too_big(len(data))
        row = save_upload(file.filename or "", data)
    except ValueError as e:              # 后缀不对 / 太大：save_upload 的人话直接透出
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    finally:
        await file.close()
    return {"ok": True, "row": row}


@app.post("/api/preprocess/start")
async def api_preprocess_start(body: dict):
    """开一个后台作业并**立刻**返回快照；长任务一行都不在这里跑。

    被拒（源不存在 / 同一份源正在跑 / 缺 deck.json）仍然是 200：那不是请求错误，
    而是"这次没跑起来"的业务结果 —— 形状与正常作业同形，原因在 job.error。
    """
    from preprocess import start_job

    slides = body.get("slides")
    slides = [int(s) for s in slides if isinstance(s, int)] if isinstance(slides, list) else None
    job = await start_job(str(body.get("name") or ""),
                          action=str(body.get("action") or "full"),
                          slides=slides or None,
                          force=bool(body.get("force")))
    return {"ok": job["status"] != "failed", "job": job}


@app.get("/api/preprocess/job/{job_id}")
async def api_preprocess_job(job_id: str):
    """按 id 读作业快照；查不到 404（作业表在进程内存里，服务重启就没了）。

    404 也要带 error：前端轮询时静默空 body，用户看到的是"进度条卡在 40%"。
    """
    from preprocess import get_job

    job = get_job(job_id)
    if job is None:
        return JSONResponse(
            {"ok": False,
             "error": f"没有作业 {job_id} —— 作业记录只活在服务进程内存里，服务重启就查不到了"},
            status_code=404)
    return {"ok": True, "job": job}


# ===========================================================================
# 当前上课用哪份课件（入口在 /preprocess，切完立刻生效 + 写回 .env）
# ===========================================================================

@app.post("/api/deck/activate")
async def api_deck_activate(body: dict):
    """把"当前上课用哪份课件"切过去：**立刻**生效（下一节课就用新的）+ 写回 .env。

    为什么不是重启才生效：`ws_endpoint` 每开一节课都按**当时**的全局 course 装配 Runner
    （web.py:2520），已经在上的那一节自带 course/script/state。所以换课件既不必重启，
    也不会把正在上的课换掉 —— 那正是"课堂在跑时不许切"的补充说明：技术上做得到，
    产品上不允许（老师讲到一半课件换了，学生面前的表现是中途消失）。

    顺序就是正确性，逐条都是为了避开一个已经踩过的坑：
      1) 先写 `os.environ`：config 只在 **import 时** `load_dotenv()` 一次，之后
         `load_settings()` 读的是 `os.environ`。只写 .env 的话，紧接着读到的还是
         进程启动时的旧值 —— 表现是"保存了但没生效"。
      2) 再 `load_settings()`：DECK_ID 已经在环境里，新的 Settings 才带着新课件。
      3) 再装配 course（放线程里：deck.json 1.3MB，同步跑会卡住事件循环，正在上的
         那一节课的心跳/音频就一起卡了）。
      4) **成功之后**才写 .env：反过来的话，装配失败会留下一份"下次重启才炸"的配置。
      5) **成功之后**才 rebind 全局 settings/course/script：失败时旧课件必须照旧能上课。
      6) 最后控制台留痕（与启动时同一份口径，出问题的人在终端里看得见）。

    失败一律回滚 `os.environ["DECK_ID"]`：不回滚的话，下一次 load_settings() 会读到
    一个"从没成功装上"的 DECK_ID，那才是真正的鬼故事。
    """
    global settings, course, script

    from deck.store import deck_json

    deck_id = str((body or {}).get("deck_id") or "").strip()
    active = _active_payload()
    if deck_id == active["deck_id"]:
        # 幂等放在最前面：本来就没要换，"课堂在跑"也就不该拦（拦了会让人以为按钮坏了）
        return {"ok": True, "changed": False, "active": active,
                "env_file": active["env_file"], "note": "已经是当前课件"}

    # 空串 = 手写演示课件（course.load_course 看到 DECK_ID 为空就走 simple 档）
    if deck_id and not deck_json(deck_id).exists():
        return JSONResponse(
            {"ok": False, "error": f"没有这份课件：{deck_id}（先预处理或选别的）"},
            status_code=400)

    running = len(_LIVE_RUNNERS)
    if running:
        return JSONResponse(
            {"ok": False, "error": f"课堂正在跑（{running} 个连接）—— 先结课再换课件"},
            status_code=409)

    had_env = "DECK_ID" in os.environ
    old_env = os.environ.get("DECK_ID", "")
    try:
        os.environ["DECK_ID"] = deck_id                          # 1)
        new_settings = load_settings()                           # 2)
        new_course = await asyncio.to_thread(load_course, new_settings)   # 3)
        save_env_values(env_file(), {"DECK_ID": deck_id})        # 4)
        settings, course, script = new_settings, new_course, new_course.script   # 5)
    except Exception as e:
        if had_env:
            os.environ["DECK_ID"] = old_env
        else:
            os.environ.pop("DECK_ID", None)
        return JSONResponse({"ok": False, "error": repr(e)}, status_code=500)

    try:
        print_course_notes(new_course)                           # 6)
    except Exception as e:
        # 打印是日志，不是这一步的一部分：Windows 控制台按 cp936 编码时，课件标题里的
        # 中文/符号能让它抛 UnicodeEncodeError —— 那不该把一次**已经成功**的切换
        # 报成失败（.env 与全局都已经换好了，报失败会让用户重复点）。
        print(f"[course] notes not printed ({type(e).__name__}) — switch already applied")

    return {"ok": True, "changed": True, "active": _active_payload(),
            "env_file": str(env_file()),
            "note": "已切换；上课页刷新后生效（.env 已写回，重启也保持）"}


# ===========================================================================
# 服务商配置（GUI）
# ===========================================================================

def _apply_settings(s: Settings) -> None:
    """把换好的配置接到正在跑的服务上。

    只重建 `settings` 与 `llm` —— 课件（course）和已合成的语音（tts）跟服务商
    无关，重建它们等于把正在上的那一节课踢掉线。新连接（新课堂）拿到新客户端。
    """
    global settings, llm
    settings = s
    llm = LLM(s)


def _provider_rows(s: Settings) -> list[dict]:
    """面板要显示的每家服务商：当前值 + 它对应的 .env 变量名（**不含 key 明文**）。"""
    rows: list[dict] = []
    for name, p in s.providers.items():
        info = PROVIDER_INFO.get(name, {})
        base_env, model_env, key_env = PROVIDER_ENV_KEYS.get(name, ("", "", ""))
        rows.append({
            "name": name,
            "label": info.get("label", name),
            "hint": info.get("hint", ""),
            "base_url": p.base_url,
            "model": p.model,
            # ollama 的默认占位值不算"配了 key"
            "key_set": bool(p.api_key) and p.api_key != "ollama",
            "env": {"base_url": base_env, "model": model_env, "key": key_env},
        })
    return rows


@app.get("/api/providers")
async def api_providers():
    """服务商面板的初始数据。key 只报"有没有"，绝不回显。"""
    s = load_settings()
    return {
        "active": s.provider,
        "describe": s.describe(),
        "env_file": str(env_file()),
        "providers": _provider_rows(s),
    }


@app.post("/api/providers")
async def api_providers_save(body: dict):
    """保存服务商配置：写回 .env，并立即接到正在跑的服务上。

    空字符串 = 不修改（界面留空表示"保持原值"，不是"清空"）—— 否则手滑清空
    一次 key 就会让人以为"服务商挂了"。
    """
    name = str(body.get("provider") or "").strip()
    s = load_settings()
    if name not in s.providers:
        return JSONResponse({"error": f"未知的服务商：{name}"}, status_code=400)
    base_env, model_env, key_env = PROVIDER_ENV_KEYS.get(name, ("", "", ""))
    values = {"LLM_PROVIDER": name}
    for env_name, field in ((base_env, "base_url"), (model_env, "model"),
                            (key_env, "api_key")):
        v = str(body.get(field) or "").strip()
        if env_name and v:
            values[env_name] = v
    try:
        path = save_env_values(env_file(), values)
    except OSError as e:
        return JSONResponse({"error": f"写配置失败：{e}"}, status_code=500)
    # 必须把新值同步进本进程环境变量，否则 load_settings() 读到的还是启动时那份
    apply_env_values(values)
    _apply_settings(load_settings())
    return {
        "saved": True,
        "env_file": str(path),
        "active": settings.provider,
        "describe": settings.describe(),
        "providers": _provider_rows(settings),
    }


@app.post("/api/providers/test")
async def api_providers_test(body: dict):
    """用界面上**还没保存**的那份参数试一次连通性：不动 .env，也不换客户端。"""
    name = str(body.get("provider") or "").strip()
    s = load_settings()
    if name not in s.providers:
        return JSONResponse({"error": f"未知的服务商：{name}"}, status_code=400)
    cur = s.providers[name]
    cand = replace(
        cur,
        base_url=str(body.get("base_url") or "").strip() or cur.base_url,
        model=str(body.get("model") or "").strip() or cur.model,
        api_key=str(body.get("api_key") or "").strip() or cur.api_key,
    )
    probe = replace(s, provider=name, providers={**s.providers, name: cand})
    from machine import _explain_llm_failure

    try:
        client = LLM(probe)
        out = await asyncio.wait_for(
            client.chat([{"role": "user", "content": "回复：好"}], max_tokens=8),
            timeout=min(30.0, s.timeout),
        )
        return {"ok": True,
                "message": f"连通 · {name} · {cand.model} · 返回：{str(out)[:60]}"}
    except Exception as e:
        return {"ok": False, "message": _explain_llm_failure(e)}


# ===========================================================================
# 千问语音转文字（Qwen ASR）配置与转写接口
# ===========================================================================

QWEN_ASR_PRESETS = [
    {
        "id": "qwen3-asr-flash",
        "label": "千问 Qwen3-ASR-Flash（推荐 · 极速高精度）",
        "hint": "阿里云百炼 DashScope 兼容接口，支持中英混合课堂提问识别",
    },
    {
        "id": "qwen-audio-turbo",
        "label": "千问 Qwen-Audio-Turbo（多模态语音理解）",
        "hint": "阿里云百炼千问音频模型，支持语音转写与口语指令识别",
    },
    {
        "id": "qwen-omni-turbo",
        "label": "千问 Qwen-Omni-Turbo（全模态实时语音）",
        "hint": "千问全模态模型，兼容 input_audio 音频转写",
    },
    {
        "id": "paraformer-v2",
        "label": "通义 Paraformer-v2 / OpenAI Whisper 兼容",
        "hint": "标准 /v1/audio/transcriptions 兼容端点",
    },
]


def _asr_status(s: Settings) -> dict:
    return {
        "base_url": s.asr_base_url,
        "model": s.asr_model,
        "key_set": bool(s.asr_api_key),
        "env_file": str(env_file()),
        "presets": QWEN_ASR_PRESETS,
        "describe": (
            f"千问语音 · {s.asr_model} · {'Key已配置' if s.asr_api_key else '未配Key(浏览器语音兜底)'}"
        ),
    }


@app.get("/api/asr/config")
async def api_asr_config():
    """读取千问语音转文字（Qwen ASR）配置。API Key 只报是否已设置，绝不回显明文。"""
    return _asr_status(load_settings())


@app.post("/api/asr/config")
async def api_asr_config_save(body: dict):
    """保存千问语音转文字配置到 .env 并热生效。"""
    base_env, model_env, key_env = ASR_ENV_KEYS
    values: dict[str, str] = {}
    for env_name, field in ((base_env, "base_url"), (model_env, "model"),
                            (key_env, "api_key")):
        v = str(body.get(field) or "").strip()
        if env_name and v:
            values[env_name] = v
    if values:
        try:
            save_env_values(env_file(), values)
        except OSError as e:
            return JSONResponse({"error": f"写配置失败：{e}"}, status_code=500)
        apply_env_values(values)
        _apply_settings(load_settings())
    return {"saved": True, **_asr_status(settings)}


def _httpx():
    """取 httpx。本机 venv 装的是改名发行版 httpx2，只有它自己的 alias_httpx()
    才会注册出 httpx 这个模块名 —— 少了这一步 /api/asr/transcribe 直接
    ModuleNotFoundError（表现为端点 500，而页面上只看得出"语音没反应"）。"""
    try:
        import httpx

        return httpx
    except ImportError:
        import httpx2

        httpx2.alias_httpx()
        import httpx

        return httpx


async def _asr_transcribe_core(s: Settings, b64: str, fmt: str,
                               mime: str) -> tuple[int, dict]:
    """千问 ASR 转写的纯逻辑：(HTTP 状态码, 响应体)。

    抽成一份而不是让配置页再写一套：这类"多路径回退"的请求构造最容易两边走偏，
    而走偏之后表现是"配置页测通了、上课时转写不出来"。状态码仍由调用方发，
    旧端点的 400/502/200 语义一字不动。
    """
    try:
        httpx = _httpx()
    except ImportError as e:
        return 502, {"ok": False, "error": f"语音转写需要 httpx（pip install httpx）：{e}"}
    if not b64:
        return 400, {"ok": False, "error": "未收到音频数据"}
    if not s.asr_api_key:
        return 400, {
            "ok": False,
            "code": "no_key",
            "error": "尚未配置千问语音转文字 API Key（QWEN_ASR_API_KEY）",
        }

    base = s.asr_base_url.rstrip("/")
    model = s.asr_model.strip() or "qwen3-asr-flash"
    headers = {"Authorization": f"Bearer {s.asr_api_key}"}
    data_uri = f"data:{mime};base64,{b64}"

    # 两条路都记状态码：有一种失败光看响应体是**空的**（实测 ws- 这类
    # 面向工作空间的端点，两条路都只回 404 + 空 body），只说"转写失败"根本查不出来。
    code1: int | None = None
    code2: int | None = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 路径 1：千问 Qwen3-ASR / Qwen-Audio / Qwen-Omni（DashScope OpenAI 兼容 chat/completions）
        if "qwen" in model.lower() or "omni" in model.lower():
            content: list[dict[str, Any]] = [
                {"type": "input_audio", "input_audio": {"data": data_uri, "format": fmt}}
            ]
            if "asr" not in model.lower():
                content.append({
                    "type": "text",
                    "text": "请将这段语音准确转写为文字，只输出转写后的文字本身，不要附带任何解释或标点说明。",
                })
            payload: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": content}],
            }
            if "qwen3-asr" in model.lower():
                payload["extra_body"] = {"asr_options": {"enable_itn": True}}
            try:
                r = await client.post(
                    f"{base}/chat/completions",
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                )
                code1 = r.status_code
                if r.status_code == 200:
                    j = r.json()
                    choices = j.get("choices") or []
                    msg = (choices[0].get("message") or {}) if choices else {}
                    txt = msg.get("content") or ""
                    if isinstance(txt, list):
                        txt = "".join(
                            str(part.get("text") or "")
                            for part in txt if isinstance(part, dict)
                        )
                    txt = str(txt).strip()
                    if txt:
                        return 200, {"ok": True, "text": txt, "model": model}
                err_detail = r.text[:200]
            except Exception as exc:
                err_detail = f"{type(exc).__name__}: {exc}"
        else:
            err_detail = ""

        # 路径 2：OpenAI Whisper / Paraformer 兼容的 /audio/transcriptions 端点
        try:
            raw_bytes = base64.b64decode(b64)
            files = {"file": (f"speech.{fmt}", raw_bytes, mime)}
            form = {"model": model, "language": "zh"}
            r2 = await client.post(
                f"{base}/audio/transcriptions",
                headers=headers,
                data=form,
                files=files,
            )
            code2 = r2.status_code
            if r2.status_code == 200:
                j2 = r2.json()
                txt2 = str(j2.get("text") or "").strip()
                if txt2:
                    return 200, {"ok": True, "text": txt2, "model": model}
            if not err_detail:
                err_detail = r2.text[:200]
        except Exception as exc:
            if not err_detail:
                err_detail = f"{type(exc).__name__}: {exc}"

    if not err_detail:
        got = " / ".join(f"HTTP {c}" if c is not None else "没发出去"
                         for c in (code1, code2))
        err_detail = (f"接口回了 {got}，响应体是空的 —— "
                      f"多半是 base_url 不对（当前 {base}）")
    return 502, {"ok": False, "error": f"千问语音转写失败（{model}）：{err_detail}"}


@app.post("/api/asr/transcribe")
async def api_asr_transcribe(body: dict):
    """将前端录制的音频通过千问语音转文字模型（Qwen ASR）转写为文本。

    这里只管接参数、发状态码 —— 转写逻辑与统一配置页的「测试」共用
    _asr_transcribe_core，两边不许各写一份。
    """
    fmt = str(body.get("format") or "wav").strip().lower()
    code, payload = await _asr_transcribe_core(
        load_settings(),
        str(body.get("audio_b64") or "").strip(),
        fmt,
        str(body.get("mime") or f"audio/{fmt}").strip(),
    )
    return payload if code == 200 else JSONResponse(payload, status_code=code)


# ===========================================================================
# 统一模型配置（主模型 / 小模型 / 语音识别 / 语音合成）
# ===========================================================================
# 四个分区共用一套语义：GET 给快照，POST save 只写本分区自己的 .env 键并热生效，
# POST test 用"界面上还没保存的编辑值"真跑一次。以前这四个入口散在三处，
# 每处各自拼 .env 键、各自拼请求参数 —— 走偏一次的表现就是"面板里显示对了，
# 上课时用的还是旧的"，而这类错在日志里完全是安静的。

MODELCFG_SECTIONS = ("main", "small", "asr", "tts")

#: TTS 面板可选音色（edge-tts 的 zh-CN 音色远不止这些，只列课堂用得上的）
TTS_VOICES = [
    {"id": "zh-CN-YunxiNeural", "label": "云希 · 男声（默认）"},
    {"id": "zh-CN-XiaoxiaoNeural", "label": "晓晓 · 女声（亲和）"},
    {"id": "zh-CN-YunyangNeural", "label": "云扬 · 男声（播音腔）"},
    {"id": "zh-CN-XiaoyiNeural", "label": "晓伊 · 女声（少年感）"},
    {"id": "zh-CN-YunjianNeural", "label": "云健 · 男声（沉稳）"},
]

#: 语音合成的试听句子：固定一句，缓存才不会因为"每次测都不一样"越堆越多
TTS_PROBE_TEXT = "同学你好，现在开始上课"

#: 小模型测试用的学生语句。必须**能绕过** classify_by_fast_rules 的规则枚举，
#: 否则测的是规则而不是小模型（tests/test_modelcfg.py 里有一条断言钉住这点）。
SMALL_PROBE_TEXT = "这个单词和刚才那个词的用法有区别吗"


def _clean(body: dict, field: str) -> str:
    """界面上这一格的值。空字符串 = 不修改（界面留空是"保持原值"，不是"清空"）。"""
    return str(body.get(field) or "").strip()


def _clean_bool(body: dict, field: str) -> bool | None:
    """界面上这个开关的值：null / 空串 / 没传 = 不修改（None）。

    为什么要认 "0"/"false" 这种字符串：前端哪天把 checkbox 的值当字符串发过来，
    "false" 在 Python 里是**真值** —— 一个"关掉"的请求反而把开关打开，
    而且界面上只会显示"已保存"，查不出来。
    """
    raw = body.get(field)
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if not s:
            return None
        return s in ("1", "true", "yes", "on")
    return bool(raw)


def _write_env(values: dict[str, str]) -> Path:
    """写回 .env，并把新值同步进本进程的环境变量。

    只写文件是不够的：load_dotenv() 不覆盖进程里已有的变量，于是"点了保存、
    用的还是旧值"（旧面板踩过，这里沿用同一对策）。
    """
    path = save_env_values(env_file(), values)
    apply_env_values(values)
    return path


def _apply_tts_settings(s: Settings) -> TTS:
    """只换语音这一层：settings 跟新，全局 tts 按新音色/语速重建。

    为什么不直接复用 _apply_settings()：它会连 llm 一起重建 —— 改一次音色就把
    llm.degraded（"连不上就别再等了"）清掉，正在上的那节课会白等一轮超时；
    而它又**不**重建 tts，所以语音保存光靠它根本不生效。
    缓存目录沿用同一份：缓存键是 voice|rate|text，换音色只是换一组文件名，
    不会串味，也没必要清缓存（清了下一节课还得整个重新合成一遍）。
    """
    global settings, tts
    settings = s
    tts = TTS(s.tts_enabled, s.tts_voice, s.tts_rate, s.cache_dir)
    return tts


# -- 快照 -------------------------------------------------------------------

def _modelcfg_main(s: Settings) -> dict:
    base_env, model_env, key_env = PROVIDER_ENV_KEYS.get(s.provider, ("", "", ""))
    rows = _provider_rows(s)
    for row in rows:
        row["active"] = row["name"] == s.provider
    prof = s.providers.get(s.provider)
    return {
        "provider": s.provider,
        "base_url": prof.base_url if prof else "",
        "model": prof.model if prof else "",
        # 与 _provider_rows 同一口径：ollama 的占位 key 不算"配了 key"
        "key_set": bool(prof) and bool(prof.api_key) and prof.api_key != "ollama",
        "providers": rows,
        "env": {"base_url": base_env, "model": model_env, "api_key": key_env},
    }


def _modelcfg_small(s: Settings) -> dict:
    base_env, model_env, key_env = INTENT_SMALL_ENV_KEYS
    return {
        # 开关读**运行中的路由器**：那才是下一句话真的会走哪条路（保存与启动
        # 都经 _seed_intent_router 从 .env 灌进来，两者不会说两套话）
        "enabled": INTENT_ROUTER.enabled,
        "use_small_llm": INTENT_ROUTER.use_small_llm,
        "model": s.intent_small_model,
        "base_url": s.intent_small_base_url,
        "key_set": bool(s.intent_small_api_key),
        "env": {"base_url": base_env, "model": model_env, "api_key": key_env},
    }


def _modelcfg_asr(s: Settings) -> dict:
    st = _asr_status(s)
    return {k: st[k] for k in ("base_url", "model", "key_set", "presets")}


def _modelcfg_tts(s: Settings) -> dict:
    return {
        "enabled": s.tts_enabled,
        # 音色/语速读**全局实例**：面板显示的就该是下一句话真的会用哪个嗓子。
        # 保存后 _apply_tts_settings() 重建了它，这里自然就是新值。
        "voice": tts.voice,
        "rate": tts.rate,
        "available": tts.available,
        "voices": TTS_VOICES,
        "last_error": tts.last_error,
    }


def _modelcfg_snapshot() -> dict:
    s = load_settings()
    return {
        "env_file": str(env_file()),
        "active_provider": s.provider,
        "sections": {
            "main": _modelcfg_main(s),
            "small": _modelcfg_small(s),
            "asr": _modelcfg_asr(s),
            "tts": _modelcfg_tts(s),
        },
    }


@app.get("/api/modelcfg")
async def api_modelcfg():
    """统一模型配置快照：四个分区一次给全。API Key 只报"有没有"，绝不回显明文。"""
    return _modelcfg_snapshot()


# -- 保存 -------------------------------------------------------------------

def _save_fail(section: str, message: str) -> dict:
    """保存失败也走 200：前端只认这个形状，4xx/5xx 在页面里表现为"点了没反应"。"""
    return {"ok": False, "section": section, "status": {}, "describe": "", "message": message}


def _save_ok(section: str, describe: str) -> dict:
    """成功后回本分区的**新快照**，前端不用再 GET 一次（少一次来回就少一次错位）。"""
    return {"ok": True, "section": section,
            "status": _modelcfg_snapshot()["sections"][section],
            "describe": describe}


def _save_main(body: dict) -> dict:
    s = load_settings()
    name = _clean(body, "provider") or s.provider
    if name not in s.providers:
        return _save_fail("main", f"未知的服务商：{name}（可选 {' / '.join(s.providers)}）")
    base_env, model_env, key_env = PROVIDER_ENV_KEYS.get(name, ("", "", ""))
    values = {"LLM_PROVIDER": name}
    for env_name, field in ((base_env, "base_url"), (model_env, "model"),
                            (key_env, "api_key")):
        v = _clean(body, field)
        if env_name and v:
            values[env_name] = v
    _write_env(values)
    _apply_settings(load_settings())
    return _save_ok("main", settings.describe())


def _save_small(body: dict) -> dict:
    base_env, model_env, key_env = INTENT_SMALL_ENV_KEYS
    values: dict[str, str] = {}
    for env_name, field in ((base_env, "base_url"), (model_env, "model"),
                            (key_env, "api_key")):
        v = _clean(body, field)
        if v:
            values[env_name] = v
    # 两个开关也要落盘：不传 = 不修改（保持已保存值），传了才写。
    # 以前它们只 configure 到运行时，界面上关掉、重启又打开 —— 四个分区里
    # 只有这一处"保存了但重启不算数"，跟其它三个分区不一致。
    for env_name, field in zip(INTENT_SMALL_SWITCH_ENV_KEYS,
                              ("enabled", "use_small_llm")):
        flag = _clean_bool(body, field)
        if flag is not None:
            values[env_name] = "1" if flag else "0"
    if values:
        _write_env(values)
    s = load_settings()
    # 与启动时同一个入口（五键一起灌）：这样"保存后立刻生效"和"重启后生效"
    # 不可能读成两套值
    _seed_intent_router(s)
    return _save_ok("small", (
        f"小模型 · {s.intent_small_model or '自动复用当前服务商'}"
        f" · {'已启用' if INTENT_ROUTER.enabled else '已关闭'}"
        f" · key:{'已设置' if s.intent_small_api_key else '未设置'}"))


def _save_asr(body: dict) -> dict:
    base_env, model_env, key_env = ASR_ENV_KEYS
    values: dict[str, str] = {}
    for env_name, field in ((base_env, "base_url"), (model_env, "model"),
                            (key_env, "api_key")):
        v = _clean(body, field)
        if v:
            values[env_name] = v
    if values:
        _write_env(values)
        _apply_settings(load_settings())
    return _save_ok("asr", _asr_status(load_settings())["describe"])


def _save_tts(body: dict) -> dict:
    values: dict[str, str] = {}
    voice = _clean(body, "voice")
    rate = _clean(body, "rate")
    if voice:
        values["TTS_VOICE"] = voice
    if rate:
        values["TTS_RATE"] = rate
    if "enabled" in body:
        values["TTS_ENABLED"] = "1" if body.get("enabled") else "0"
    if values:
        _write_env(values)
    # 全局 tts 是启动时构造的：不单独重建它，"保存了音色"对下一句话毫无影响
    _apply_tts_settings(load_settings())
    return _save_ok("tts", (
        f"语音 · {tts.voice} · 语速 {tts.rate}"
        f" · {'已启用' if tts.enabled else '未启用'}"))


@app.post("/api/modelcfg/save")
async def api_modelcfg_save(body: dict):
    """保存某一个分区：只写该分区自己的 .env 键，然后立刻接到正在跑的进程上。"""
    section = _clean(body, "section")
    if section not in MODELCFG_SECTIONS:
        return _save_fail(section, f"未知的分区：{section or '（空）'}"
                                   f"（可选 {' / '.join(MODELCFG_SECTIONS)}）")
    try:
        if section == "main":
            return _save_main(body)
        if section == "small":
            return _save_small(body)
        if section == "asr":
            return _save_asr(body)
        return _save_tts(body)
    except OSError as e:
        return _save_fail(section, f"写配置失败：{e}")


# -- 真实测试 ---------------------------------------------------------------

async def _cfg_test_main(body: dict) -> dict:
    """用候选参数真发一次对话。max_tokens 压到 8：这里只问"活着吗"，不问质量。"""
    from machine import _explain_llm_failure

    s = load_settings()
    name = _clean(body, "provider") or s.provider
    if name not in s.providers:
        return {"ok": False, "message": f"未知的服务商：{name}"}
    cur = s.providers[name]
    cand = replace(
        cur,
        base_url=_clean(body, "base_url") or cur.base_url,
        model=_clean(body, "model") or cur.model,
        api_key=_clean(body, "api_key") or cur.api_key,
    )
    probe = replace(s, provider=name, providers={**s.providers, name: cand})
    try:
        out = await asyncio.wait_for(
            LLM(probe).chat([{"role": "user", "content": "回复：好"}], max_tokens=8),
            timeout=min(30.0, s.timeout),
        )
    except Exception as e:
        return {"ok": False, "message": _explain_llm_failure(e)}
    return {"ok": True, "message": f"连通 · {name} · {cand.model}",
            "detail": str(out)[:80]}


async def _small_failure_reason(s: Settings, base_url: str, model: str,
                               api_key: str) -> str:
    """小模型判定失败后，用同一批参数直连一次，把原因翻译成人话。

    INTENT_ROUTER.route() 对异常是**静默回退**的 —— 上课时这是对的（不能因为
    小模型抖一下就打断课堂），但配置页必须能说清到底哪儿不对，所以这里复现一次。
    """
    from machine import _explain_llm_failure

    prof = s.providers.get(s.provider) or next(iter(s.providers.values()))
    cand = replace(
        prof,
        base_url=base_url or prof.base_url,
        model=model or prof.model,
        api_key=api_key or prof.api_key or "ollama",
    )
    probe = replace(s, provider=prof.name, providers={**s.providers, prof.name: cand})
    try:
        await asyncio.wait_for(
            LLM(probe).chat([{"role": "user", "content": "回复：好"}], max_tokens=8),
            timeout=min(15.0, s.timeout),
        )
    except Exception as e:
        return f"小模型调用失败：{_explain_llm_failure(e)}"
    return ("小模型接口能连上，但返回的不是约定的 JSON 枚举（多半是这家的模型"
            "不支持 JSON 输出，换一个指令跟随好一点的小模型试试）")


async def _cfg_test_small(body: dict) -> dict:
    """用候选参数真跑一次意图判定，跑完把路由器恢复原样。

    恢复是硬要求：路由器是**全局单例**，这个接口随时可能在开发面板里被点 ——
    留下一次候选配置，下一句学生输入就会打到错误的模型上。
    """
    s = load_settings()
    model = _clean(body, "model") or s.intent_small_model
    base_url = _clean(body, "base_url") or s.intent_small_base_url
    api_key = _clean(body, "api_key") or s.intent_small_api_key
    saved = (INTENT_ROUTER.enabled, INTENT_ROUTER.use_small_llm,
             INTENT_ROUTER.small_model, INTENT_ROUTER.small_base_url,
             INTENT_ROUTER.small_api_key)
    # 统计/历史也一起还原：探测调用不该混进课堂的真实数据里
    stats, history = dict(INTENT_ROUTER.stats), list(INTENT_ROUTER.history)
    try:
        INTENT_ROUTER.configure(
            enabled=True, use_small_llm=True, small_model=model,
            small_base_url=base_url, small_api_key=api_key,
        )
        dec = await asyncio.wait_for(
            INTENT_ROUTER.route(SMALL_PROBE_TEXT, s, "课堂讲授中"),
            timeout=min(30.0, s.timeout),
        )
    except Exception as e:
        return {"ok": False, "message": f"小模型判定失败：{type(e).__name__}: {e}"}
    finally:
        INTENT_ROUTER.configure(
            enabled=saved[0], use_small_llm=saved[1], small_model=saved[2],
            small_base_url=saved[3], small_api_key=saved[4],
        )
        INTENT_ROUTER.stats.clear()
        INTENT_ROUTER.stats.update(stats)
        INTENT_ROUTER.history = history

    if dec.stage == "small_llm":
        return {"ok": True,
                "message": f"小模型连通 · {model or '主模型+关思考'}"
                           f" · 判定 {dec.intent.value}（{dec.latency_ms:.0f} ms）",
                "detail": dec.reason}
    if dec.stage == "rule":
        # 规则枚举优先于小模型：候选语句必须绕过规则，否则等于没测到小模型
        return {"ok": False, "message": "这句测试语句命中了规则枚举，没有真正调用小模型"}
    return {"ok": False, "detail": dec.reason,
            "message": await _small_failure_reason(s, base_url, model, api_key)}


async def _cfg_test_asr(body: dict) -> dict:
    """用仓库自带的样本音频真走一次转写（样本缺失就直说，不抛异常）。"""
    s = load_settings()
    probe = replace(
        s,
        asr_base_url=_clean(body, "base_url") or s.asr_base_url,
        asr_model=_clean(body, "model") or s.asr_model,
        asr_api_key=_clean(body, "api_key") or s.asr_api_key,
    )
    sample = ROOT / "_probe.mp3"
    if not sample.exists():
        return {"ok": False,
                "message": f"找不到样本音频 {sample}，无法真实走一次转写"
                           "（放一段课堂录音的 mp3 到这个路径再测）"}
    b64 = base64.b64encode(sample.read_bytes()).decode()
    _code, payload = await _asr_transcribe_core(probe, b64, "mp3", "audio/mpeg")
    text = str(payload.get("text") or "")
    if payload.get("ok") and text:
        return {"ok": True, "message": f"转写成功 · {probe.asr_model} · {text[:40]}",
                "detail": text[:200]}
    return {"ok": False, "message": str(payload.get("error") or "转写失败（没有返回文字）")}


async def _cfg_test_tts(body: dict) -> dict:
    """用候选音色/语速真合成一句，落到缓存目录并给出前端能直接播的 URL。"""
    s = load_settings()
    voice = _clean(body, "voice") or s.tts_voice or "zh-CN-YunxiNeural"
    rate = _clean(body, "rate") or s.tts_rate or "+0%"
    # 用独立的临时实例，而不是改全局 tts 的 voice：改全局会在一瞬间让正在上的
    # 那一句话用上候选音色（课堂中途换嗓子）。缓存目录必须与全局同一个 ——
    # 否则返回的 audio_url 指向的是另一个目录，前端点了播不出声。
    probe = TTS(True, voice, rate, settings.cache_dir)
    if not probe.enabled:
        return {"ok": False,
                "message": probe.status()["reason"] or "edge-tts 不可用，无法合成语音"}
    path = await probe.synth(TTS_PROBE_TEXT)
    if path is None:
        return {"ok": False,
                "message": f"合成失败（{voice} / {rate}）："
                           f"{probe.last_error or 'edge-tts 没有返回音频，检查网络后重试'}"}
    return {"ok": True,
            "message": f"合成成功 · {voice} · 语速 {rate}"
                       f" · {max(1, path.stat().st_size // 1024)} KB",
            "audio_url": f"/api/audio/{path.name}"}


async def _run_section_test(section: str, overrides: dict) -> dict:
    """跑一个分区的真实测试。任何异常都收成 ok:false —— 前端只认 200。"""
    t0 = time.perf_counter()
    try:
        if section == "main":
            out = await _cfg_test_main(overrides)
        elif section == "small":
            out = await _cfg_test_small(overrides)
        elif section == "asr":
            out = await _cfg_test_asr(overrides)
        else:
            out = await _cfg_test_tts(overrides)
    except Exception as e:
        out = {"ok": False, "message": f"测试时出现异常：{type(e).__name__}: {e}"}
    res = {"section": section, "ok": bool(out.get("ok")),
           "message": str(out.get("message") or ""),
           "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    for k in ("detail", "audio_url"):
        if out.get(k):
            res[k] = out[k]
    return res


@app.post("/api/modelcfg/test")
async def api_modelcfg_test(body: dict):
    """用界面上还没保存的编辑值真测一次（overrides 留空 = 测已保存的那份）。"""
    section = _clean(body, "section")
    if section not in MODELCFG_SECTIONS:
        return {"ok": False, "section": section, "elapsed_ms": 0,
                "message": f"未知的分区：{section or '（空）'}"
                           f"（可选 {' / '.join(MODELCFG_SECTIONS)}）"}
    return await _run_section_test(section, body)


@app.post("/api/modelcfg/test_all")
async def api_modelcfg_test_all(body: dict):
    """四个分区一起测。并发跑：串起来最坏要等四段超时，用户会以为页面卡死。"""
    t0 = time.perf_counter()
    results = list(await asyncio.gather(
        *(_run_section_test(sec, {}) for sec in MODELCFG_SECTIONS)))
    return {"ok": all(r["ok"] for r in results),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "results": results}


# ===========================================================================
# 开发者模式：让位话术库管理 & 快速意图路由管理接口
# ===========================================================================

def _phrases_with_tts() -> list[dict[str, Any]]:
    cats = PHRASE_BANK.list_all()
    for c in cats:
        c["tts_cached"] = [
            tts.cached(p.format(page=1, anchor="重点行"))
            for p in c["items"]
        ]
    return cats


@app.get("/api/dev/phrases")
async def api_dev_phrases():
    """读取全部让位话术分类及每条话术的 TTS 缓存状态。"""
    return {"categories": _phrases_with_tts()}


@app.post("/api/dev/phrases")
async def api_dev_phrases_save(body: dict):
    """管理让位话术库：编辑/新增/删除话术、切换激活项、切换固定/LLM模式、恢复默认或预合成语音。"""
    action = str(body.get("action") or "update").strip()
    if action == "reset":
        PHRASE_BANK.reset_to_defaults(save=True)
        return {"ok": True, "message": "已恢复全部默认让位话术", "categories": _phrases_with_tts()}

    if action == "warmup":
        texts: list[str] = []
        for c in PHRASE_BANK.list_all():
            for p in c["items"]:
                if "{page}" in p:
                    for s in script.slides[:10]:
                        texts.append(p.format(page=s.index + 1))
                elif "{anchor}" in p:
                    texts.append(p.format(anchor="刚才的位置"))
                else:
                    texts.append(p)
        ok, total = await tts.warmup(texts)
        return {
            "ok": True,
            "message": f"已完成话术音频预合成：{ok}/{total} 条可用",
            "categories": _phrases_with_tts(),
        }

    key = str(body.get("key") or "").strip()
    items = body.get("items")
    active = body.get("active")
    mode = body.get("mode")
    try:
        updated = PHRASE_BANK.update_category(
            key,
            items=items if isinstance(items, list) else None,
            active=int(active) if active is not None else None,
            mode=str(mode) if mode is not None else None,
        )
    except (KeyError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "updated": updated, "categories": _phrases_with_tts()}


@app.get("/api/dev/intent")
async def api_dev_intent():
    """读取快速意图路由器（枚举直通 + 小模型预判）状态与统计。"""
    return INTENT_ROUTER.status(settings)


@app.post("/api/dev/intent")
async def api_dev_intent_action(body: dict):
    """配置或沙盒测试快速意图路由器。"""
    action = str(body.get("action") or "config").strip()
    if action == "test":
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "请输入要测试的学生语句"}, status_code=400)
        dec = await INTENT_ROUTER.route(text, settings, str(body.get("slide_title") or ""))
        return {"ok": True, "decision": dec.to_dict(), "status": INTENT_ROUTER.status(settings)}

    INTENT_ROUTER.configure(
        enabled=body.get("enabled") if "enabled" in body else None,
        use_small_llm=body.get("use_small_llm") if "use_small_llm" in body else None,
        small_model=str(body.get("small_model")) if "small_model" in body else None,
        small_base_url=str(body.get("small_base_url")) if "small_base_url" in body else None,
        small_api_key=str(body.get("small_api_key")) if "small_api_key" in body else None,
    )
    return {"ok": True, "status": INTENT_ROUTER.status(settings)}


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """AI 课堂外置大脑 MCP 端点（JSON-RPC 2.0 over HTTP POST /mcp，见《MCP方案设计》§4）。"""
    raw = await request.body()
    status_code, resp = await MCP_SERVER.handle_jsonrpc(raw)
    if status_code == 202 or resp is None:
        return Response(status_code=202)
    return JSONResponse(resp, status_code=status_code)


@app.get("/api/dev/mcp")
async def api_dev_mcp():
    """读取 MCP 外置大脑运行状态、事件总线指标与观察用副本记录。"""
    ep = f"http://{settings.host}:{settings.port}/mcp"
    return {
        "mode": MCP_SERVER.mode.status_dict(),
        "event_bus": {
            "queue_size": MCP_SERVER.event_bus.queue_size,
            "poll_count": MCP_SERVER.event_bus.poll_count,
            "dropped_count": MCP_SERVER.event_bus.dropped_count,
            "last_wakeup_latency_ms": MCP_SERVER.event_bus.last_wakeup_latency_ms,
        },
        "say": {
            "last_enqueue_ms": MCP_SERVER.last_say_enqueue_ms,
            "recent_receipts": MCP_SERVER.say_receipts[-10:],
        },
        "transcript": MCP_SERVER.transcript.view(30),
        "endpoint": "/mcp",
        "full_endpoint": ep,
        "access_prompt": build_access_prompt(ep),
    }


@app.post("/api/dev/mcp")
async def api_dev_mcp_action(body: dict):
    """配置或控制 MCP 外置大脑模式。"""
    if "enabled" in body:
        MCP_SERVER.mode.enabled = bool(body["enabled"])
    if "quiet_fallback" in body:
        MCP_SERVER.mode.quiet_fallback = bool(body["quiet_fallback"])
    if "quiet_after_ms" in body and body["quiet_after_ms"] is not None:
        MCP_SERVER.mode.quiet_after_ms = max(5_000, int(body["quiet_after_ms"]))
    if "lang" in body and body["lang"] in ("zh", "en"):
        MCP_SERVER.mode.lang = str(body["lang"])
    if "allow_action_tools" in body:
        MCP_SERVER.allow_action_tools = bool(body["allow_action_tools"])
    if body.get("action") == "disconnect":
        MCP_SERVER.mode.disconnect()
    return await api_dev_mcp()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    runner = Runner(ws, settings, course, llm, tts)
    # 登记给只读观测面（分支树页的 /api/script/live）：这里只存一个引用，
    # 不碰状态机、不发任何消息。注销在下面的 finally 里，别让下线的课堂被当成"还在上"。
    _LIVE_RUNNERS.append(runner)
    try:
        await runner.start()
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("t")
            if t == "input":
                via = str(msg.get("via") or "text")
                await runner.on_input(str(msg.get("text", "")), via=via)
            elif t == "cmd":
                await runner.on_command(str(msg.get("cmd", "")))
            elif t == "dev":
                act = str(msg.get("action", ""))
                runner._manual_nav = (act == "goto_slide")
                try:
                    await runner.machine.handle_dev_action(act, slide=msg.get("slide", 0))
                finally:
                    runner._manual_nav = False
            elif t == "speech_done":
                await runner.on_speech_done(int(msg.get("seq", 0)))
            elif t == "metrics":
                await runner.send("metrics_dump", **runner.metrics.summary(runner.state))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await runner.send("notify", level="error", text=f"{type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        try:
            _LIVE_RUNNERS.remove(runner)
        except ValueError:
            pass          # 已经不在（重复关闭）—— 注销是清理，不该反过来抛
        await runner.close()


@app.get("/api/state")
async def api_state():
    s = load_settings()
    return {
        "ui": UI_VERSION,
        "provider": s.describe(),
        "provider_name": s.provider,
        "model": s.active.model,
        "base_url": s.active.base_url,
        "tts": tts.status(),
        "course": script.title,
        "slides": script.slide_count,
        "units": len(script.units),
        "total_est_sec": script.total_est_sec,
        "mode": course.mode,
        "deck_id": course.deck_id,
        "plan_source": course.plan.source if course.plan else "",
        "anchor_report": course.anchor_report,
    }


@app.get("/api/audit")
async def api_audit():
    from eval import audit_script

    if course.mode == "deck" and course.deck is not None and course.plan is not None:
        from script_gen import audit_plan

        return {"report": audit_plan(course.deck, course.plan, settings.unit_char_warn)}
    return {"report": audit_script(script, settings.unit_char_warn)}
