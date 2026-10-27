"""会话运行器：一个 sqlite 会话 = 一节正在上的课。

它只做三件事，其余一律交给既有引擎：

1. **装配**：单元行（pptx 路径）→ `course.load_course` → `Machine`。解析/备课/状态机
   全部复用，这里不重写任何一条课堂规则 —— 重写一份的代价是两套"什么时候翻页"的
   判断会慢慢分叉，而分叉只在真实课堂里才看得出来。
2. **翻译**：把 `PhaseHandlers` 的七个回调翻译成设计文档 §4 的 WS 帧。
3. **落库**：会话位置/理解信号**节流**写库（每 `FLUSH_INTERVAL` 秒或每 `EVENT_BATCH` 条事件），
   事件每会话只留最近 `repo.EVENTS_KEEP` 条。

几条踩过坑的约束，动这个文件之前先读：

* `on_speech_end(kind)` **必须**在每段真正播完之后调用。状态转移全靠它；少调一次
  状态机会停在同一段上，学生看到的是老师反复说同一句话。
* 段序号 `seq` 是绑定关系：前端回来时必须原样带回。只看"有没有 speech_done"不够，
  一次错配就会让"等播完"落到错误的段落上（复读整节课）。
* 暂停要**真的**停住：暂停期间不许再发 `segment_start` / `page`（音频与推送都停），
  状态机也不许推进（它推进的唯一出口是 `_speak`，把门设在门口即可）。
  继续时从**原位**接着讲 —— 被打断的那一段重发一遍，而不是跳到下一段。
* 断线 = 暂停（落库 paused），不是结束：学生关掉页面下次进来要能续学。
  只有 `cmd:finish` 或课上完了才是 ended。
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from config import Settings
from machine import Machine, PhaseHandlers, SpeechSeg
from models import LessonScript, Slide, SlideElement
from state import ClassroomState, Utterance

from store import repo

log = logging.getLogger("classroom.runner")

#: 进度落库节流（秒）。学生每一句都写库既没必要，也会让 sqlite 成为热路径。
FLUSH_INTERVAL = 2.0
#: 事件攒够这么多条就写一次
EVENT_BATCH = 20
#: 语速档位（设计文档 §4：0.8 | 1 | 1.2）
RATE_CHOICES = (0.8, 1.0, 1.2)

#: 内部标识的样子：`import-981b1da75b2f`、`run-…`、`s-…`、`u-…`。
#: 用来判断"这个名字能不能念给学生听" —— 学生不该在字幕里读到 deck_id。
_INTERNAL_NAME = re.compile(r"^(?:import|run|deck)-[0-9a-z_.-]{4,}$|^[uspr]-[0-9a-f]{6,}$", re.I)
#: 绝对路径（Windows 盘符或 POSIX 根）。学生看到的句子不该带本机路径。
_PATH_TOKEN = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/])[^\s，。；）」'\"、]+")
#: URL 要先保护：`http://host:11434/v1` 里的 "//" 会被路径规则误伤成 "httpv1"。
_URL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s，。；）」'\"、]+")


def _basename(token: str) -> str:
    return token.replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1] or token


def shorten_paths(text: str) -> str:
    """把文本里的绝对路径压成文件名（"C:\a\b\第一课.pptx" → "第一课.pptx"）。

    为什么需要它：异常消息、内容管线的 warnings 里几乎必然带路径，
    而这些句子会经 notify / 字幕原样出现在学生面前。
    """
    raw = str(text or "")
    if not raw:
        return raw
    stashed: dict[str, str] = {}

    def _stash(match):
        key = f"\x00{len(stashed)}\x00"      # 先换成一个不会出现在文案里的占位符
        stashed[key] = match.group(0)
        return key

    protected = _URL_TOKEN.sub(_stash, raw)   # URL 原样留到最后
    out = _PATH_TOKEN.sub(lambda m: _basename(m.group(0)), protected)
    for key, url in stashed.items():
        out = out.replace(key, url)
    return out


def rate_string(value: float) -> str:
    """0.8 / 1 / 1.2 → edge-tts 的 "-20%" / "+0%" / "+20%"。"""
    return f"{int(round((float(value) - 1.0) * 100)):+d}%"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionRunner:
    """驱动一个会话。`send` 是 (`kind`, payload) → await 的发送函数（WS 或测试里的收集器）。"""

    def __init__(
        self,
        *,
        session: dict,
        unit: dict,
        profile: dict | None,
        send: Callable[[str, dict], Awaitable[None]],
        conn,
        settings: Settings | None = None,
        tts=None,
        tts_factory: Callable[[str], Any] | None = None,
        course=None,
        llm=None,
        clock: Callable[[], float] = time.monotonic,
        speech_timeout: float | None = None,
    ):
        self.session = dict(session)
        self.unit = dict(unit)
        self.profile = dict(profile or {})
        self.session_id = self.session["id"]
        self.unit_id = self.session.get("unit_id") or self.unit.get("id") or ""
        self.profile_id = self.session.get("profile_id") or self.profile.get("id") or ""
        self._send = send
        self.conn = conn
        self.settings = settings
        self.tts = tts
        self._tts_factory = tts_factory
        #: 注入的课件（测试用；生产走 load_course）。None = 由单元行现装。
        self._course = course
        self._llm = llm
        self._clock = clock
        #: 等前端 `speech_done` 的上限；None = 按字数估（真课堂），测试里给个小数
        self._speech_timeout = speech_timeout

        # -- 位置（续学就从这里开始） --------------------------------------
        self._start_slide = int(self.session.get("position_slide") or 0)
        self._start_unit = int(self.session.get("position_unit") or 0)

        # -- 运行期 ------------------------------------------------------
        self.script: LessonScript | None = None
        #: 学生看到的课程名（单元标题 → 课程标题 → 课件标题，见 _resolve_display_title）
        self.display_title = ""
        #: 改写前的课件标题（通常就是 deck 目录名/文件名）。出口兜底时把它也换掉 ——
        #: 引擎侧任何一处回显旧标题（notify/字幕）都因此到不了学生面前。
        self._initial_title = ""
        self.state = ClassroomState(course_id=str(self.unit.get("course_id") or ""))
        self.state.slide_index = max(0, self._start_slide)
        self.state.seg_index = max(0, self._start_unit)
        self.machine: Machine | None = None
        self._metrics = None
        self._task: asyncio.Task | None = None
        self._closing = False
        self._closed = False

        # 暂停/继续：`_resume_ev` set = 运行中
        self._paused = False
        self._resume_ev = asyncio.Event()
        self._resume_ev.set()
        self._paused_at: float | None = None
        self._paused_total = 0.0

        # 段序号与"这一段播完了"信号
        self._seq = 0
        self._speech_seq = 0
        self._speech_done = asyncio.Event()

        # 节流落库
        self._events: list[tuple[str, dict]] = []
        self._last_flush = 0.0
        self._t0 = self._clock()

        # 前端语速
        self.rate = 1.0
        # 手动翻页 → 整页直接显示全（分步显示是为"讲到才出现"，手翻的意图是"我要看这一页"）
        self._manual_nav = False
        self._markers: dict = {}
        self._markers_slide: int | None = None
        self._no_audio_warned: set[str] = set()
        self._recap: dict | None = None
        #: recap 帧是否已经推过（正常收尾 _finalize_ended 推，异常收尾 finish 兜底推，
        #: 两条路都推会让前端收到两帧 —— 它做了去重，但没必要让它替我们挡）
        self._recap_sent = False
        #: 这节课已经收尾（自然讲完 / 学生结束 / 出错停机）。
        #: 收尾之后 run 已经不在跑了，pause/resume 只能"什么也不做并说明" ——
        #: 否则一句迟到的 pause 会把 ended 覆盖成 paused，学生再进来说不复用也不新建。
        self._ended = False

    # ==================================================================
    # 装配
    # ==================================================================
    async def start(self) -> None:
        """建状态机、发 hello/page、起任务。**不等课堂结束**。"""
        if self.settings is None:
            try:
                from config import load_settings

                self.settings = load_settings()
                log.warning("运行器没有收到配置快照，已自行读取 session=%s", self.session_id)
            except Exception as e:
                log.error("读取配置失败 session=%s: %s", self.session_id, e)
        if self.settings is None:
            # 没有配置就没有 probe_window 等参数，状态机起来也是坏的：
            # 明说比在里面 AttributeError 好查得多。
            await self.send("notify", level="error",
                            text="配置读不出来（.env 缺失或写坏了），这节课开不起来。")
            repo.update_session(self.conn, self.session_id, status="paused")
            return
        self._course = self._course or self._build_course()
        self.script = self._course.script
        # 标题必须是**人能读的名字**：`script.title` 来自 deck 标题，而 deck 标题由
        # **文件名**派生 —— 导入的课件文件名是 `import-<hash>.pptx`，于是字幕第一行
        # 会变成「开始上课：《import-981b1da75b2f》」。学生不该看见 deck_id。
        # 只在显示层改写：单元/课程/课件都不变，下一次装配还是原来的 id。
        self._initial_title = str(self.script.title or "")
        self.display_title = self._resolve_display_title()
        if self.display_title and self.display_title != str(self.script.title):
            log.info("课堂标题改用可见名 session=%s title=%s（课件标题 %s）",
                     self.session_id, self.display_title, self.script.title)
            self.script.title = self.display_title
        self.state.course_id = self.script.course_id or self.state.course_id
        self.state.student_id = self.profile_id or self.state.student_id
        # 续学位置要对齐脚本：上次的页数可能比这份脚本多（课件换了），越界就从头讲
        self.state.slide_index = min(max(0, self.state.slide_index),
                                     max(0, self.script.slide_count - 1))
        if self.state.slide_index != self._start_slide:
            self.state.seg_index = 0
        if self._llm is None:
            self._llm = self._build_llm()
        self.machine = Machine(
            self.script, self.state, self.settings, self._llm, self._handlers(),
            self._metrics_tracker(),
        )
        self._plan_markers(self.state.slide_index)
        repo.update_session(self.conn, self.session_id, status="active")
        self._event("session_start", slide=self.state.slide_index,
                    unit_index=self.state.seg_index)
        await self.send("hello", **self._hello_payload())
        await self._page(self.state.slide_index, reveal_all=True)
        self._task = asyncio.create_task(self._run_task(), name=f"classroom-{self.session_id}")
        log.info("课堂开始 session=%s unit=%s pos=%s/%s",
                 self.session_id, self.unit_id, self.state.slide_index, self.state.seg_index)

    def _resolve_display_title(self) -> str:
        """学生看到的课程名：**单元标题 → 课程标题 → 课件标题**。

        这个顺序不是随便定的：单元标题是学生刚才点进去的那一行，最贴近"我在上什么"；
        课程标题次之（换单元时字幕会换名字，反而更准确）；课件标题是最后回落 ——
        它可能就是个文件名（甚至 deck_id），所以排在最后。
        三个候选都像内部标识时，退回"这节课"：宁可平淡，也不泄漏。
        """
        candidates: list[str] = [str(self.unit.get("title") or "").strip()]
        course_id = str(self.unit.get("course_id") or "")
        if course_id:
            try:
                course = repo.get_course(self.conn, course_id) or {}
                candidates.append(str(course.get("title") or "").strip())
            except Exception as e:      # 读库失败不该影响开课
                log.warning("读课程标题失败 session=%s: %s", self.session_id, e)
        candidates.append(str(getattr(self.script, "title", "") or "").strip())
        for name in candidates:
            if self._looks_user_facing(name):
                return name
        return "这节课"

    def _looks_user_facing(self, name: str) -> bool:
        if not name:
            return False
        internal = {self.unit_id, self.session_id, self.profile_id,
                    str(self.unit.get("course_id") or ""),
                    str(self.unit.get("pptx_path") or "")}
        if name in internal:
            return False
        return _INTERNAL_NAME.match(name) is None

    def _user_text(self, text: str) -> str:
        """出口兜底：把内部标识从**面向学生**的文本里摘掉。

        为什么在出口兜一层，而不是逐处改文案：这些句子来自状态机/内容管线，
        它们的每一行都不归我改；但学生看到的每一帧都经过这里。兜住这一层之后，
        任何一处往 notify 里拼 deck_id / 会话 id / 绝对路径的改动都会被挡住。
        """
        out = str(text or "")
        if not out:
            return out
        deck = getattr(self._course, "deck", None) if self._course is not None else None
        human = self.display_title or "本课课件"
        internal = [
            (str(getattr(deck, "deck_id", "") or ""), human),   # deck 目录名
            (self._initial_title, human),                       # 改写前的课件标题
            (self.session_id, "本节课"),
        ]
        for token, word in internal:
            if token and token != word:
                out = out.replace(token, word)
        return shorten_paths(out)

    def _handlers(self) -> PhaseHandlers:
        return PhaseHandlers(
            speak=self._speak,
            notify=self._notify,
            phase_changed=self._phase_changed,
            state_changed=self._state_changed,
            transcript=self._transcript,
            page_changed=self._page_changed,
            highlight=self._highlight,
            thinking=self._thinking,
        )

    def _metrics_tracker(self):
        try:
            from eval import MetricsTracker

            self._metrics = MetricsTracker()
        except Exception:                       # 指标不是上课的必要条件
            self._metrics = None
        return self._metrics

    def _build_llm(self):
        if self.settings is None:
            return None
        try:
            from llm import LLM

            return LLM(self.settings)
        except Exception as e:
            # 没 key / 没装 openai 时课堂照样能讲（答疑会退回预置话术并报错一次）
            log.warning("模型客户端构造失败（答疑降级）：%s: %s", type(e).__name__, e)
            return None

    def _build_course(self):
        """单元行 → Course。优先已解析的 deck（秒开），没有就让引擎现场解析。"""
        if self.settings is None:
            return self._fallback_course("没有可用的配置快照")
        pptx = str(self.unit.get("pptx_path") or "")
        if not pptx:
            return self._fallback_course("这个单元没有课件路径")
        try:
            from deck.store import deck_json, make_deck_id

            deck_id = make_deck_id(pptx)
            ready = deck_json(deck_id).is_file()
        except Exception:
            deck_id, ready = "", False
        # 注意：deck_id 指向不存在的 deck.json 时 load_course 会**退回示例课件**
        # （course.py 里那条 FileNotFoundError 分支），宁可让它现场解析真正的 pptx。
        settings = dataclasses.replace(
            self.settings,
            deck_id=deck_id if ready else "",
            pptx_file="" if ready else pptx,
        )
        try:
            from course import load_course

            course = load_course(settings)
        except Exception as e:
            log.exception("装配课件失败")
            return self._fallback_course(f"课件装配失败：{type(e).__name__}: {e}")
        if course.mode != "deck":
            log.warning("课件没能装配成 deck，已退回示例课件 unit=%s", self.unit_id)
        return course

    def _fallback_course(self, reason: str) -> Any:
        """兜底课件：这节课至少能开起来（一页、一段话），并明确告诉学生哪里不对。"""
        title = self._user_text(str(self.unit.get("title") or "这节课"))
        # 技术原因（异常消息/路径）只写日志：这句话会**念给学生听**，
        # 里面出现 "FileNotFoundError: C:\…" 只会让人以为课堂坏了。
        log.warning("课件不可用，改用占位讲解 session=%s: %s", self.session_id, reason)
        text = (f"《{title}》的课件还没有准备好。"
                f"先把这段占位讲解读完，再去单元页点一次「准备课件」。")
        slide = Slide(index=0, title=title,
                      elements=[SlideElement(element_id="s01-t1", kind="text", text=text,
                                             order=0)])
        from state import LectureUnit

        unit = LectureUnit(unit_id=f"{self.unit_id}-u1", slide=0, index_in_slide=0,
                           text=text, is_page_end=True, kp=[])
        script = LessonScript(course_id=str(self.unit.get("course_id") or ""),
                              title=title, session_goals=[], slides=[slide], units=[unit])
        from course import Course

        log.warning("使用兜底课件 session=%s reason=%s", self.session_id, reason)
        return Course(script=script, mode="simple", notes=[reason])

    # ==================================================================
    # 暂停闸门
    # ==================================================================
    async def _wait_running(self) -> None:
        """暂停期间的门：所有会出声 / 会换页的推送都必须先过这一关。

        为什么门设在推送口而不是状态机里：状态机推进的唯一出口是 `_speak`，
        而翻页要经过 `page_changed`。把门放在这两个出口上，
        "暂停了还在讲"和"暂停了页面还在翻"就同时被堵住了，
        而且不用去改状态机一行代码（它本来就不知道"学生按了暂停"这件事）。
        """
        if self._paused:
            await self._resume_ev.wait()

    # ==================================================================
    # 发送
    # ==================================================================
    async def send(self, kind: str, **payload: Any) -> None:
        if self._closed:
            return
        try:
            await self._send(kind, payload)
        except Exception:
            # 学生关掉页面时后台任务可能正好在推帧 —— 这是正常收尾，不该冒异常
            self._closed = True

    def position(self) -> dict:
        return {"slide": int(self.state.slide_index), "unit_index": int(self.state.seg_index)}

    def _hello_payload(self) -> dict:
        return {
            "unit": {
                "id": self.unit.get("id") or self.unit_id,
                "course_id": self.unit.get("course_id") or "",
                "title": self.unit.get("title") or (self.script.title if self.script else ""),
                "slides": int(self.unit.get("slides") or (self.script.slide_count if self.script else 0)),
                "chapter": self.unit.get("chapter") or "",
            },
            "profile": {"id": self.profile.get("id") or self.profile_id,
                        "name": self.profile.get("name") or ""},
            "position": self.position(),
        }

    async def _send_state(self) -> None:
        """把完整快照推给前端（**过暂停闸门**）。

        暂停期间状态机仍可能在内部换相位（例如探针开启），那是它自己的事；
        但学生按下暂停之后屏幕上不该再有任何变化 —— 所以这里等闸门。
        暂停/继续这两个动作本身的状态帧用 `self.send` 直发，见 pause()/resume()。
        """
        await self._wait_running()
        await self.send("state", **self._state_payload())

    def _state_payload(self) -> dict:
        return {
            "phase": self.state.phase.value,
            "comprehension": dict(self.state.comprehension),
            "position": self.position(),
        }

    # ==================================================================
    # 回调实现（状态机 → WS 帧）
    # ==================================================================
    async def _notify(self, level: str, text: str) -> None:
        # 过闸门：暂停期间"暂停了还在弹提示"同样是一种没停住。
        # 代价是状态机里一条通知会等续播才发出 —— 可以接受，close() 会放行它。
        await self._wait_running()
        text = self._user_text(text)
        await self.send("notify", level=level, text=text)
        self._event("notify", level=level, text=text)

    async def _phase_changed(self, state: ClassroomState) -> None:
        self._event("phase", phase=state.phase.value, slide=state.slide_index,
                    unit_index=state.seg_index)
        await self._send_state()

    async def _state_changed(self) -> None:
        await self._send_state()

    async def _thinking(self, on: bool, show_secs: bool = False) -> None:
        # 契约里没有 "thinking" 帧：模型在思考这件事由 state.phase 表达，
        # 内容绝不外泄。这里只记事件，供 /dev 排查"学生为什么等了 20 秒"。
        self._event("thinking", on=bool(on), sec=bool(show_secs))

    async def _highlight(self, anchors: list) -> None:
        # 逐句高亮已经随 segment_start 的 anchor_ids 下发（一句话一个锚点），
        # 整单元批量高亮这个老协议不再使用；留一个事件便于排查"灯没亮"。
        self._event("highlight", n=len(anchors or []))

    async def _transcript(self, u: Utterance) -> None:
        await self._wait_running()
        text = self._user_text(u.text)
        await self.send("transcript", speaker=u.speaker, text=text)
        self._event("transcript", speaker=u.speaker, text=text[:120])

    async def _page_changed(self, slide_index: int) -> None:
        reveal_all = self._manual_nav
        self._manual_nav = False
        await self._page(slide_index, reveal_all=reveal_all)

    async def _page(self, slide_index: int, *, reveal_all: bool = False) -> None:
        await self._wait_running()
        self._plan_markers(slide_index)
        slide = self._slide_payload(slide_index, reveal_all=reveal_all)
        await self.send("page", slide_index=int(slide_index),
                        slide_count=int(self.script.slide_count if self.script else 0),
                        slide=slide, media_base=self._media_base())
        self._event("page", slide=int(slide_index))
        await self._tick_persist()

    async def _speak(self, seg: SpeechSeg) -> None:
        """把一段话送出去并等它播完 —— 段末让位就发生在这里。"""
        if self._closing or self._closed:
            return
        while True:
            await self._wait_running()
            if self._closing or self._closed:
                return
            self._seq += 1
            seq = self._seq
            self._speech_done.clear()
            self._speech_seq = 0
            await self.send(
                "segment_start", seq=seq, text=seg.text,
                seg_kind=seg.kind, unit_id=seg.unit_id,
                raise_hand=bool(seg.raise_hand),
                anchor_ids=[getattr(a, "element_id", "") for a in (seg.anchors or [])],
                ops=[op.to_dict() if hasattr(op, "to_dict") else dict(op)
                     for op in (seg.ops or [])],
                est_sec=round(len(seg.text) / 5.0, 1),
                marker=self._marker_for(seg),
            )
            await self._send_audio(seq, seg.text)
            await self.send("segment_end", seq=seq, text=seg.text)
            self._event("segment", kind=seg.kind, seq=seq, text=(seg.text or "")[:60])
            done = await self._wait_speech_done(seq, seg)
            if done:
                break
            # 被暂停打断 → 继续之后**重发这一段**（从原位接着讲，而不是跳过去）
            self._event("resume_replay", seq=seq)
        await self.machine.on_speech_end(seg.kind)
        if seg.pause_after_ms:
            await asyncio.sleep(min(seg.pause_after_ms / 1000.0, 10.0))
        await self._tick_persist()

    async def _wait_speech_done(self, seq: int, seg: SpeechSeg) -> bool:
        """等这一段播完。True = 真播完；False = 被暂停打断（要重发这一段）。"""
        if self._speech_timeout is not None:
            timeout = float(self._speech_timeout)
        else:
            # 按字数估，再兜一个下限：学生的浏览器可能不回报 speech_done
            # （自动播放被拦、脚本报错），那时按"已播完"继续总比课堂卡死强。
            timeout = max(15.0, len(seg.text) * 0.45 + 10.0)
        deadline = self._clock() + timeout
        while True:
            if self._closing:
                # 学生按了"结束"或连接断了：这句话已经不需要等它播完。
                # 不在这里退出的话，收尾要一直等到这一段自己的超时（15s+），
                # 学生看到的是"点了结束，页面卡住十几秒"。
                return True
            if self._paused:
                await self._resume_ev.wait()
                return False
            left = deadline - self._clock()
            if left <= 0:
                await self.send("notify", level="warn", text="播放等待超时，按已播完继续")
                self._event("speech_timeout", seq=seq)
                return True
            try:
                await asyncio.wait_for(self._speech_done.wait(), timeout=left)
            except asyncio.TimeoutError:
                continue
            if self._closing:
                return True
            if self._paused:
                await self._resume_ev.wait()
                return False
            if self._speech_seq == seq:
                return True
            # 不是这一段的完成信号（上一段迟到的 done）：清掉继续等这一段
            self._speech_done.clear()
            self._event("stray_speech_done", expect=seq, got=self._speech_seq)

    async def _send_audio(self, seq: int, text: str) -> None:
        tts = self.tts
        if tts is None or not getattr(tts, "enabled", False):
            self._warn_no_audio_once("TTS 未启用")
            return
        try:
            path = await tts.synth(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("合成失败 seq=%s: %s: %s", seq, type(e).__name__, e)
            path = None
        if path is None:
            self._warn_no_audio_once(getattr(tts, "last_error", "") or "合成结果为空")
            return
        try:
            raw = path.read_bytes()
        except OSError as e:
            self._warn_no_audio_once(f"音频读取失败：{e}")
            return
        if not raw:
            self._warn_no_audio_once("音频是 0 字节")
            return
        await self.send("audio", seq=seq, url=f"/assets/audio/{path.name}",
                        b64=base64.b64encode(raw).decode("ascii"), mime="audio/mpeg")

    def _warn_no_audio_once(self, reason: str) -> None:
        """一段话没有音频时说清楚为什么 —— 同一个原因只提醒一次。"""
        if reason in self._no_audio_warned:
            return
        self._no_audio_warned.add(reason)
        asyncio.create_task(self.send(
            "notify", level="warn",
            text=self._user_text(f"这一段只有字幕、没有语音：{reason}")))

    # ---- 课件载荷 -------------------------------------------------------
    def _slide_payload(self, slide_index: int, *, reveal_all: bool = False) -> dict | None:
        course = self._course
        if course is not None and course.mode == "deck" and course.deck is not None:
            return self._deck_slide_payload(course, slide_index, reveal_all=reveal_all)
        slide = self.script.slide_at(slide_index) if self.script else None
        if slide is None:
            return None
        reveal = {r for u in (self.script.units_of(slide_index) if self.script else [])
                  for r in (u.reveal or [])}
        return {
            "mode": "simple",
            "index": slide.index,
            "title": slide.title,
            "elements": [
                {"element_id": e.element_id, "kind": e.kind, "text": e.text,
                 "reveal": e.element_id in reveal}
                for e in slide.elements
            ],
        }

    def _media_base(self) -> str:
        """slide 里图片要拼的前缀。没有真实课件就是空字符串。"""
        course = self._course
        if course is not None and getattr(course, "deck", None) is not None:
            return f"/api/v1/units/{self.unit_id}/media/"
        return ""

    def _deck_slide_payload(self, course, slide_index: int, *,
                            reveal_all: bool = False) -> dict | None:
        """真实课件的一页 → 前端可重绘的载荷。

        **直接透传 deck.json 里的 slide 对象**（`DeckDoc.to_dict()` 就是
        `asdict(self)`，所以 `asdict(slide)` 与盘上那份逐字段一致），
        只往上**追加**三个渲染提示：`mode` / `width+height` / `media_base` / `reveal`。
        不裁剪字段是硬要求：前端按 deck.json 的 schema（box/fill/line/
        text.paras[].runs/pic/table/svg_path/preset/source）重绘，少一个字段就是
        一块空白 —— 而这种缺失在后端看起来毫无异常。
        """
        deck = course.deck
        s = deck.slide_at(slide_index)
        if s is None:
            return None
        reveal: list[str] = []
        if course.plan is not None and not reveal_all:
            for u in course.plan.units_of(slide_index):
                for op in (u.ops or []):
                    if op.get("op") in ("reveal", "step"):
                        for t in [op.get("target")] + list(op.get("targets") or []):
                            if t and t not in reveal:
                                reveal.append(t)
        payload = dataclasses.asdict(s)
        payload.update({
            "mode": "deck",
            "width": deck.width_px,
            "height": deck.height_px,
            "media_base": self._media_base(),
            "reveal": reveal,
        })
        return payload

    # ---- 逐句锚点（一句话亮一个点） ------------------------------------
    def _plan_markers(self, slide_index: int) -> None:
        try:
            from script_gen import plan_markers

            self._markers = {(m["unit_id"], m["sentence"]): m
                             for m in plan_markers(self.script.units_of(slide_index))}
        except Exception:
            self._markers = {}
        self._markers_slide = slide_index

    def _marker_for(self, seg: SpeechSeg) -> dict | None:
        """这一句该亮哪一个锚点。

        `state.unit_progress` 在机器**返回 seg 之前**就写成了"这一句自己的序号"，
        所以这里读到的一定是当前这句，不需要另记一份进度。
        页对不上时先重算 —— 机器归位时会直接改 `state.slide_index` 而不触发
        `page_changed`，不兜这一下，"翻页去答疑再回来"之后整页就没高亮了。
        """
        if self._markers_slide != self.state.slide_index:
            self._plan_markers(self.state.slide_index)
        if seg.kind != "unit" or not seg.unit_id:
            return None
        sentence = self.state.unit_progress.get(seg.unit_id, 0)
        return self._markers.get((seg.unit_id, sentence))

    # ==================================================================
    # 客户端消息
    # ==================================================================
    async def on_input(self, text: str, *, via: str = "text") -> None:
        text = (text or "").strip()
        if not text or self.machine is None:
            return
        self._event("input", text=text[:120], via=via)
        await self.machine.handle_input(text, via=via)

    async def on_speech_done(self, seq: int = 0) -> None:
        self._speech_seq = int(seq or 0)
        self._speech_done.set()

    async def on_rate(self, value: Any) -> None:
        try:
            v = round(float(value), 2)
        except (TypeError, ValueError):
            v = 0.0
        if v not in RATE_CHOICES:
            await self.send("notify", level="warn",
                            text=f"语速只支持 {', '.join(str(x) for x in RATE_CHOICES)}")
            return
        self.rate = v
        if self._tts_factory is not None:
            self.tts = self._tts_factory(rate_string(v))
        self._event("rate", value=v)
        await self.send("notify", level="info", text=f"语速已切到 {v} 倍（下一句起生效）")

    async def on_command(self, cmd: str) -> None:
        cmd = (cmd or "").strip()
        if cmd == "pause":
            await self.pause()
            return
        if cmd == "resume":
            await self.resume()
            return
        if cmd == "finish":
            await self.finish()
            return
        if cmd in ("next", "prev"):
            if self.machine is None:
                return
            self._event("cmd", cmd=cmd)
            # 手动翻页 → 整页直接显示全（分步显示是为"讲到才出现"）
            self._manual_nav = True
            try:
                await self.machine.handle_command(cmd)
            finally:
                self._manual_nav = False
            return
        await self.send("notify", level="warn", text=f"不认识的指令：{cmd or '（空）'}")

    # ==================================================================
    # 暂停 / 继续 / 下课
    # ==================================================================
    async def pause(self) -> None:
        if self._ended:
            await self.send("notify", level="info", text="这节课已经结束了，不用再暂停")
            return
        if self._paused:
            await self.send("notify", level="info", text="已经暂停了")
            return
        self._paused = True
        self._resume_ev.clear()
        self._paused_at = self._clock()
        # 唤醒正在等 speech_done 的那一段：它醒来会看见 _paused 并原地让位
        self._speech_seq = 0
        self._speech_done.set()
        self._event("pause", **self.position())
        self._persist(force=True, status="paused")
        await self.send("notify", level="info", text="已暂停")
        await self.send("state", **self._state_payload())

    async def resume(self) -> None:
        if self._ended:
            await self.send("notify", level="info", text="这节课已经结束了")
            return
        if not self._paused:
            await self.send("notify", level="info", text="课堂本来就在进行中")
            return
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None
        self._paused = False
        self._resume_ev.set()
        self._event("resume", **self.position())
        self._persist(force=True, status="active")
        await self.send("notify", level="info", text="继续")
        await self.send("state", **self._state_payload())

    async def finish(self) -> dict:
        """学生按下"结束这节课"：停机 + 落库 ended + 回 recap。"""
        if self._ended:
            return self._recap or self.build_recap()
        self._closing = True
        if self.machine is not None:
            # 唯一一处私有触点：Machine 的公开 API（handle_input / handle_command /
            # next_speech / on_speech_end）没有"下课"这个动作，而契约要求 cmd:finish。
            self.machine._finished.set()
        # 顺手放行"等这一段播完"的等待（_wait_speech_done 看见 _closing 会直接返回）
        self._speech_seq = 0
        self._speech_done.set()
        await self._join(timeout=3.0, graceful=True)
        self._ended = True
        recap = self._recap or self.build_recap()
        self._recap = recap
        self._persist(force=True, status="ended", ended=True)
        self._flush_events()
        if not self._recap_sent:                 # _finalize_ended 没机会跑时补一帧
            self._recap_sent = True
            await self.send("recap", **recap)
        return recap

    async def close(self) -> None:
        """客户端断开：取消任务、落库 paused（下次进来能续学）。"""
        self._closing = True
        self._resume_ev.set()          # 放行还在等暂停的手
        self._speech_done.set()
        await self._join(timeout=3.0)
        self._closed = True
        try:
            # 已经自然讲完的课，断开连接不许把它改回 paused（否则课后小结会变成"没上完"）
            self._persist(force=True, status=None if self._ended else "paused")
            self._flush_events()
        except Exception:
            log.exception("会话收尾落库失败 session=%s", self.session_id)

    async def _join(self, timeout: float, *, graceful: bool = False) -> None:
        """收掉主任务。

        `graceful=True`（下课）：**先等它自己跑完**再取消。顺序反过来就丢收尾 ——
        `_finished` 已经 set 的情况下任务会立刻走 `_finalize_ended`（落库 + 发 recap），
        一上来 cancel 的话那句 recap 永远不会发出去，学生按下"结束"看不到小结。
        `graceful=False`（断开连接）：立刻取消，学生已经走了，没必要等课堂讲完。
        """
        task, self._task = self._task, None
        if task is None or task.done():
            return
        if graceful:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                return
            except asyncio.TimeoutError:
                log.warning("收尾超时，改为取消 session=%s", self.session_id)
            except (asyncio.CancelledError, Exception):
                return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            log.exception("会话任务收尾异常 session=%s", self.session_id)

    # ==================================================================
    # 主任务
    # ==================================================================
    async def _run_task(self) -> None:
        try:
            await self.machine.start()          # 内部分别驱动 pump，直到 _finished
            await self.machine.wait_finished()
            await self._finalize_ended()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("课堂循环异常 session=%s", self.session_id)
            self._event("fatal", err=f"{type(e).__name__}: {e}")
            await self.send("notify", level="error",
                            text=f"课堂出错了，已停下来：{type(e).__name__}: {e}")
            self._persist(force=True, status="paused")
            self._flush_events()

    async def _finalize_ended(self) -> None:
        self._ended = True
        recap = self.build_recap()
        self._recap = recap
        self._persist(force=True, status="ended", ended=True)
        repo.add_event(self.conn, self.session_id, "recap", recap)
        self._flush_events()
        self._recap_sent = True
        await self.send("recap", **recap)
        log.info("课堂结束 session=%s elapsed=%.1fs", self.session_id, recap["elapsed_sec"])

    # ==================================================================
    # 落库
    # ==================================================================
    def _elapsed(self) -> float:
        now = self._clock()
        paused = self._paused_total
        if self._paused_at is not None:
            paused += now - self._paused_at
        return max(0.0, now - self._t0 - paused)

    def _percent(self) -> float:
        script = self.script
        if script is None or not script.slide_count:
            return 0.0
        if self.state.slide_index >= script.slide_count:
            return 100.0
        on_page = max(1, len(script.units_of(self.state.slide_index)))
        frac = min(1.0, (self.state.seg_index + 1) / on_page)
        return round(min(100.0, 100.0 * (self.state.slide_index + frac) / script.slide_count), 1)

    def _event(self, name: str, **payload: Any) -> None:
        # 第一个参数叫 name 而不是 kind：事件载荷里自己就有 kind 字段
        # （段类型），同名会让 `self._event("segment", kind=seg.kind)` 直接 TypeError。
        self._events.append((name, payload))
        if len(self._events) >= EVENT_BATCH:
            self._flush_events()

    def _flush_events(self) -> None:
        if not self._events:
            return
        pending, self._events = self._events, []
        for kind, payload in pending:
            try:
                repo.add_event(self.conn, self.session_id, kind, payload)
            except Exception:
                log.exception("写事件失败 session=%s kind=%s", self.session_id, kind)

    def _persist(self, *, force: bool = False, status: str | None = None,
                 ended: bool = False) -> None:
        """把位置/理解信号/耗时写进会话行与 progress 行（默认节流）。"""
        now = self._clock()
        if not force and (now - self._last_flush) < FLUSH_INTERVAL:
            return
        self._last_flush = now
        fields: dict[str, Any] = {
            "elapsed_sec": round(self._elapsed(), 1),
            "position_slide": int(self.state.slide_index),
            "position_unit": int(self.state.seg_index),
            "comprehension": json.dumps(dict(self.state.comprehension), ensure_ascii=False),
            "qa_count": len(self.state.qa_history),
        }
        if status:
            fields["status"] = status
        if ended:
            fields["ended_at"] = _now()
        try:
            repo.update_session(self.conn, self.session_id, **fields)
        except Exception:
            log.exception("写会话失败 session=%s", self.session_id)
            return
        # topics 也一并落一份：运行器不在场时（刷新过页面、服务重启过），
        # `GET /sessions/{id}/recap` 只能从库里拼 recap —— 没有它，"这节课讲了什么"
        # 就永远是空的，而那份数据本来就在手边。
        self._event("progress", slide=int(self.state.slide_index),
                    unit_index=int(self.state.seg_index),
                    units_given=sum(1 for v in self.state.unit_progress.values() if int(v) > 0),
                    topics=self._covered_topics(3),
                    percent=self._percent())
        try:
            repo.upsert_progress(
                self.conn, self.profile_id, self.unit_id,
                percent=self._percent(),
                last_slide=int(self.state.slide_index),
                last_unit=int(self.state.seg_index),
                study_sec=float(fields["elapsed_sec"]),
            )
        except Exception:
            log.exception("写进度失败 session=%s", self.session_id)

    async def _tick_persist(self) -> None:
        self._persist()

    # ==================================================================
    # 课后小结
    # ==================================================================
    def _covered_topics(self, limit: int = 5) -> list[str]:
        """已讲单元的一句话总结（引擎已经把它截到 30 字）。"""
        script = self.script
        if script is None:
            return []
        topics: list[str] = []
        for uid, done in self.state.unit_progress.items():
            if int(done) <= 0:
                continue
            unit = script.unit(uid)
            if unit is None:
                continue
            summary = str(getattr(unit, "summary", "") or "").strip()
            if summary and summary not in topics:
                topics.append(summary)
            if len(topics) >= limit:
                break
        return topics

    def build_recap(self) -> dict:
        """recap 的形状按设计文档 §4：五個字段，一个都不许少。

        `topics` 取已讲单元的一句话说总结（`LectureUnit.summary`，引擎已经截到 30 字），
        最多 5 条 —— 学生的"这节课讲了什么"要能一眼扫完。
        """
        st = self.state
        script = self.script
        slides_total = int(script.slide_count) if script else 0
        covered = min(slides_total, int(st.slide_index) + 1) if slides_total else 0
        given = sum(1 for v in st.unit_progress.values() if int(v) > 0)
        topics = self._covered_topics(5)
        return {
            "elapsed_sec": round(self._elapsed(), 1),
            "slides_covered": covered,
            "units_given": given,
            "qa_count": len(st.qa_history),
            "topics": topics,
        }
