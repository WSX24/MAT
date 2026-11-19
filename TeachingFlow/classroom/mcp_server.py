"""AI 课堂「外置大脑」MCP Server 实现。

对应设计文档：《MCP方案设计-AI课堂外置大脑.md》
参考实现：minecraft-numen McpServer.java / McpMode.java / McpTranscript.java

三条铁律（§0.4）：
1. agent 永不出现在状态机的阻塞链上（状态机 4s 快答先行，agent 只做补充/纠正）。
2. get_events 绝不能拿 Machine._lock（独立 EventBus + asyncio.Event）。
3. 发言权（floor）永远在状态机手上（say 只是入队，播出时机由让位点决定）。
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from llm import build_ctx, clamp_spoken, sanitize_spoken, split_sentences
from state import Comprehension, Phase, estimate_seconds

if TYPE_CHECKING:
    from machine import Machine

# ===========================================================================
# 常量与参数表（§0.3, §4.1, §8）
# ===========================================================================

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "classroom-mcp"
SERVER_VERSION = "1.0.0"

DEFAULT_WAIT_SECONDS = 50
MAX_WAIT_SECONDS = 50
QUIET_AFTER_MS = 120_000
QUERY_STALE_SECONDS = 60.0
AGENT_SILENT_SPEAK_DONE_SECONDS = 8.0
MAX_EVENT_QUEUE_SIZE = 20
MAX_SAY_QUEUE_DEPTH = 2
MAX_SAY_CHARS = 300
HARD_MAX_TOOLS = 6

INSTRUCTIONS_ZH = """你们正在驱动一节 AI 课堂。你是这堂课的外置大脑：状态机负责讲课和节奏，
你只在状态机判不出学生意图时提供补充。

铁律：
1. 循环调用 get_events(wait_seconds=50)。它就是你的耳朵。
   学生说话会立即唤醒它（urgent），不需要等满 50 秒。
2. 收到 type=query 时先看 triage 和 handled_by：
   - handled_by="state_machine"：老师已经答过了。只有当你确信答错、
     或能提供关键补充时才 say；否则直接回到第 1 步，不要为了说话而说话。
   - handled_by=null：没人答。但老师仍在讲课，你的话会排队等让位点。
3. 说任何话都用 say(text)。它立即返回 receipt，不代表已经播出。
   看 receipt 里的 queue_depth 和 can_speak_now：
   - queue_depth > 2 说明你已经排了太多话，停下来等播出。
   - can_speak_now=false 时不要连续 say，等下一个 get_events。
4. 你只回答，不推进课堂。不要调 next_unit / next_page 除非学生明确要求。
5. 每次 say 都要带 from_version（get_events 给的 snapshot.version）。
   版本过期时状态机会给你的话加前缀或转为待回看，不要自己猜。
6. 不要为了"有内容可说"而编造。没把握就调工具查，查不到就承认不知道。
7. 你下一次调用 get_events 就等于告诉状态机"我说完了"。"""

INSTRUCTIONS_EN = """You are the external brain for a live AI classroom. The state machine drives lecturing and pacing; you only provide supplements when student intent falls into the fallback escape hatch.

Rules:
1. Loop on get_events(wait_seconds=50). Urgent student queries wake it immediately (<1ms).
2. On type="query", inspect triage and handled_by:
   - handled_by="state_machine": the built-in teacher already answered. Only call say() if you have a critical correction or supplement; otherwise return to step 1 immediately.
   - handled_by=null: unanswered yet; your say() will queue for the next yield boundary.
3. Always speak via say(text, from_version). Check receipt.queue_depth and receipt.can_speak_now:
   - Stop speaking if queue_depth >= 2 or can_speak_now is false.
4. Always pass from_version=snapshot.version from the event. Stale answers are auto-prefixed or deferred.
5. Do not fabricate facts. Use query_rag / search_course / student_memory when unsure.
6. Calling get_events again signals to the classroom that you have finished speaking."""


def build_access_prompt(endpoint: str = "http://127.0.0.1:8000/mcp") -> str:
    """生成给外部 Agent（Claude Code / Codex / Cursor 等）的一键接入与值守提示词（参照 McpAccessPrompt.java）。"""
    return f"""我正在运行「AI 课堂交互原型」，请作为这堂课的「外置大脑（External Brain）」接入它的 MCP Server 并开始值守。

## 1. MCP 端点信息
- HTTP MCP Endpoint: `{endpoint}`
- 若你的宿主支持 HTTP / Streamable-HTTP MCP，请直接连接 `{endpoint}`；
- 若只支持 stdio，请使用 `npx -y mcp-remote {endpoint}`，或直接运行本仓库内置桥接器：
  `python run.py mcp-stdio {endpoint}`

## 2. 接入后立即执行的值守循环（铁律）
连接成功后，请立即开始以下循环，不要等待我再次催促：
1. 调用 `get_events(wait_seconds=50)` 挂起监听课堂事件（学生提问会微秒级立即唤醒返回）。
2. 当收到 `type="query"` 的学生提问事件时：
   - 查看 `handled_by`：若为 `"state_machine"`，说明内置老师已在 4 秒内给过快答，你只在需要关键补充或纠错时发言；若为 `null`，说明内置模型离线，由你负责解答。
   - 如需查阅课件细节或学生薄弱点，先调用只读工具 `get_state`、`query_rag`、`search_course` 或 `student_memory`。
   - 调用 `say(text="你的补充话术（口语化、2~3句内）", from_version=event.snapshot.version, final=True)` 将话术排入课堂让位点队列。
   - 检查 `say` 返回的 `receipt`：若 `queue_depth >= 2` 或 `can_speak_now == false`，停止继续 `say`。
3. 完成本轮补充（或收到空事件 `events: []`）后，**立即再次调用 `get_events(wait_seconds=50)`**（这同时会告诉课堂状态机「我说完了」）。"""


# ===========================================================================
# 观察用现场缓冲 McpTranscript（§5.3，与消费队列严格分离）
# ===========================================================================

@dataclass(frozen=True)
class TranscriptLine:
    ts: str
    mono: float
    kind: str       # "QUERY" | "SAY" | "TOOL" | "EVENT"
    text: str
    error: bool = False


class McpTranscript:
    """给人类与开发者面板查看的只读现场记录（上限 200 条，绝不混入事件消费队列）。"""

    CAP = 200

    def __init__(self) -> None:
        self._lines: deque[TranscriptLine] = deque(maxlen=self.CAP)

    def record(self, kind: str, text: str, *, error: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._lines.append(
            TranscriptLine(
                ts=datetime.now().isoformat(timespec="seconds"),
                mono=time.monotonic(),
                kind=kind,
                text=text[:240],
                error=error,
            )
        )

    def view(self, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self._lines)[-limit:]
        return [
            {"ts": x.ts, "kind": x.kind, "text": x.text, "error": x.error}
            for x in items
        ]

    def clear(self) -> None:
        self._lines.clear()


# ===========================================================================
# 单一状态对象：ActiveAgentTurn + McpMode（§3.3, §7.3, §13.1）
# ===========================================================================

@dataclass
class ActiveAgentTurn:
    """外脑当前被唤醒后的一轮发言状态（对应 §7.3 四级「说完」判据）。"""

    run_id: int
    query_seq: int
    started_at: float = field(default_factory=time.monotonic)
    last_touch: float = field(default_factory=time.monotonic)
    say_count: int = 0
    final_marked: bool = False


class McpMode:
    """外置大脑模式的唯一状态源（参照 numen McpMode.java，§3.3 & §13.1）。

    三态掌舵表（§3.3）：
      1. 未连接（client_name is None）       → driving() == False（状态机全权）
      2. 已连接 + 活跃（QUIET_AFTER_MS 内）  → driving() == True（外脑有发言权，floor 仍在状态机）
      3. 已连接 + 静默超时                   → quietFallback 接管，driving() == False
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        quiet_fallback: bool = True,
        quiet_after_ms: int = QUIET_AFTER_MS,
        silent_speak_done_sec: float = AGENT_SILENT_SPEAK_DONE_SECONDS,
        lang: str = "zh",
    ) -> None:
        self.enabled: bool = enabled
        self.quiet_fallback: bool = quiet_fallback
        self.quiet_after_ms: int = max(1_000, int(quiet_after_ms))
        self.silent_speak_done_sec: float = float(silent_speak_done_sec)
        self.lang: str = lang if lang in ("zh", "en") else "zh"

        # 初值保证 driving() 为 False（§3.3 / §10.3 #8：防冷启动死锁）
        self.client_name: str | None = None
        self.last_activity_ms: float = 0.0
        self.announced_quiet: bool = False
        self.active_run: ActiveAgentTurn | None = None
        self._run_counter: int = 0

    @staticmethod
    def _now_ms() -> float:
        return time.monotonic() * 1000.0

    def touch(self) -> None:
        """收到任何合法 MCP 请求（含 ping、长轮询返回）时刷新活跃时刻。"""
        self.last_activity_ms = self._now_ms()
        if self.active_run is not None:
            self.active_run.last_touch = time.monotonic()

    def handshake(self, client_name: str) -> None:
        """initialize 握手完成：记录客户端名称并激活。"""
        self.client_name = (client_name or "unknown client").strip() or "unknown client"
        self.announced_quiet = False
        self.touch()

    def disconnect(self) -> None:
        """显式断开或重置连接状态。"""
        self.client_name = None
        self.active_run = None
        self.announced_quiet = False

    def quiet_now(self, now_ms: float | None = None) -> bool:
        """外脑是否从未连接或已超过 QUIET_AFTER_MS 未活动。"""
        if self.client_name is None or self.last_activity_ms <= 0:
            return True
        cur = self._now_ms() if now_ms is None else now_ms
        return (cur - self.last_activity_ms) > self.quiet_after_ms

    def driving(self, now_ms: float | None = None) -> bool:
        """外脑当前是否在线参与（初值必为 False，initialize 后且未静默超时才为 True）。"""
        if not self.enabled or self.client_name is None:
            return False
        if self.quiet_fallback and self.quiet_now(now_ms):
            return False
        return True

    def state_label(self, now_ms: float | None = None) -> str:
        """三态掌舵表枚举：unconnected | active | quiet_fallback。"""
        if not self.enabled or self.client_name is None:
            return "unconnected"
        if self.quiet_now(now_ms):
            return "quiet_fallback"
        return "active"

    def open_agent_turn(self, query_seq: int) -> ActiveAgentTurn:
        """急件唤醒外脑时开启一轮发言跟踪。"""
        self._run_counter += 1
        now_s = time.monotonic()
        self.active_run = ActiveAgentTurn(
            run_id=self._run_counter,
            query_seq=query_seq,
            started_at=now_s,
            last_touch=now_s,
        )
        return self.active_run

    def on_get_events_called(self) -> None:
        """主判据（§7.3）：agent 下一次调 get_events 就等于「我说完了」。"""
        self.touch()
        self.active_run = None

    def on_say_called(self, *, final: bool = False) -> None:
        """辅判据（§7.3）：say(final=True) 显式标记说完。"""
        self.touch()
        if self.active_run is not None:
            self.active_run.say_count += 1
            self.active_run.last_touch = time.monotonic()
            if final:
                self.active_run.final_marked = True
                self.active_run = None

    def is_agent_turn_finished(self) -> bool:
        """四级兜底判定外脑本轮是否已说完（§7.3）。"""
        run = self.active_run
        if run is None:
            return True
        # 级别 2：显式 final
        if run.final_marked:
            self.active_run = None
            return True
        # 级别 4：终极兜底 quietFallback (120s)
        if not self.driving():
            self.active_run = None
            return True
        # 级别 3：静默无活动超时 (8s)
        if (time.monotonic() - run.last_touch) >= self.silent_speak_done_sec:
            self.active_run = None
            return True
        return False

    def check_edge_transition(self) -> tuple[str, str] | None:
        """失联/回归的边沿检测（只在状态边沿触发一次通知，照搬 McpMode.java:175-193）。"""
        if not self.enabled or self.client_name is None:
            self.announced_quiet = False
            return None
        quiet = self.quiet_now()
        if quiet and not self.announced_quiet:
            self.announced_quiet = True
            self.active_run = None
            return (
                "warn",
                f"[MCP 外置大脑] {self.client_name} 已静默超过 {self.quiet_after_ms // 1000}s，状态机全权接管课堂",
            )
        if not quiet and self.announced_quiet:
            self.announced_quiet = False
            return (
                "info",
                f"[MCP 外置大脑] {self.client_name} 已恢复活跃连接",
            )
        return None

    def status_dict(self) -> dict[str, Any]:
        idle_ms = (
            max(0, int(self._now_ms() - self.last_activity_ms))
            if self.last_activity_ms > 0
            else None
        )
        return {
            "enabled": self.enabled,
            "state": self.state_label(),
            "driving": self.driving(),
            "client_name": self.client_name,
            "quiet_fallback": self.quiet_fallback,
            "quiet_after_ms": self.quiet_after_ms,
            "idle_ms": idle_ms,
            "active_turn": (
                {
                    "run_id": self.active_run.run_id,
                    "query_seq": self.active_run.query_seq,
                    "say_count": self.active_run.say_count,
                }
                if self.active_run is not None
                else None
            ),
        }


# ===========================================================================
# 事件总线 EventBus（两车道：urgent / normal，独立于 Machine._lock，§5 & §10.1）
# ===========================================================================

class EventBus:
    """双车道事件总线 + 阻塞式长轮询。

    铁律 #2：绝不触碰 Machine._lock。
    三档返回（§4.3）：
      1. 急件（urgent=True）立即唤醒返回（< 1ms）
      2. 到点清扫返回累积普件（wait_seconds 到期）
      3. 到点无事件返回极短的空 + idle_hint
    """

    def __init__(
        self,
        mode: McpMode,
        *,
        max_queue_size: int = MAX_EVENT_QUEUE_SIZE,
        stale_seconds: float = QUERY_STALE_SECONDS,
        on_drop: Callable[[dict[str, Any]], None] | None = None,
        idle_hint_provider: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.mode = mode
        self.max_queue_size = max_queue_size
        self.stale_seconds = stale_seconds
        self.on_drop = on_drop
        self.idle_hint_provider = idle_hint_provider

        self._q: list[dict[str, Any]] = []
        self._wake = asyncio.Event()
        self._seq = 0
        self._urgent_epoch = 0       # 用于检测「两次调用之间是否有新急件到达」（§10.3 #6）
        self._consumed_urgent_epoch = 0
        self.poll_count = 0
        self.dropped_count = 0
        self.last_wakeup_latency_ms: float = 0.0

    @property
    def queue_size(self) -> int:
        return len(self._q)

    def has_urgent(self) -> bool:
        return any(bool(ev.get("urgent")) for ev in self._q)

    def has_unconsumed_urgent(self) -> bool:
        """自上次 get_events 消费后，是否有未消费的急件在排队（供只读工具打断检查，§10.3 #6）。"""
        return self._urgent_epoch > self._consumed_urgent_epoch and self.has_urgent()

    def push(self, ev: dict[str, Any]) -> dict[str, Any]:
        """推入一条事件（O(1)，永不阻塞）。"""
        self._seq += 1
        mono_now = time.monotonic()
        envelope: dict[str, Any] = {
            "type": ev.get("type", "event"),
            "kind": ev.get("kind", "unknown"),
            "urgent": bool(ev.get("urgent", False)),
            "seq": self._seq,
            "at": datetime.now().isoformat(timespec="seconds"),
            "_mono_ts": mono_now,
        }
        for k, v in ev.items():
            if k not in envelope:
                envelope[k] = v

        if len(self._q) >= self.max_queue_size:
            dropped = self._q.pop(0)
            self.dropped_count += 1
            if self.on_drop is not None:
                try:
                    self.on_drop(dropped)
                except Exception:
                    pass

        self._q.append(envelope)
        if envelope["urgent"]:
            self._urgent_epoch += 1
            self._wake.set()
        return envelope

    async def get_events(self, wait_seconds: int = DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
        """长轮询取事件（§4.3, §10.1）。"""
        wait = max(0, min(MAX_WAIT_SECONDS, int(wait_seconds)))
        self.poll_count += 1
        # 挂起开始前先 touch，并将上一轮 agent 发言标为「已说完」（§7.3 主判据）
        self.mode.on_get_events_called()

        # 只有当前队列里没有急件时才挂起等待急件唤醒；
        # 普通事件（speech_finished / unit_finished）静静累积，到点后统一清扫返回。
        if wait > 0 and not self.has_urgent():
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=float(wait))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

        # 挂起返回时再次 touch，防止 50s 长轮询被 quietFallback 误判失联（§10.3 #2）
        self.mode.touch()

        now_mono = time.monotonic()
        batch, self._q = self._q, []
        self._consumed_urgent_epoch = self._urgent_epoch
        self._wake.clear()

        out_events: list[dict[str, Any]] = []
        latest_urgent_mono: float | None = None
        for item in batch:
            ev = dict(item)
            mono_ts = float(ev.pop("_mono_ts", now_mono))
            if ev.get("type") == "query" and (now_mono - mono_ts) > self.stale_seconds:
                ev["stale"] = True
            if ev.get("urgent"):
                latest_urgent_mono = mono_ts
                self.mode.open_agent_turn(int(ev.get("seq", 0)))
            out_events.append(ev)

        if latest_urgent_mono is not None:
            self.last_wakeup_latency_ms = round((now_mono - latest_urgent_mono) * 1000.0, 3)

        if not out_events:
            hint = None
            if self.idle_hint_provider is not None:
                try:
                    hint = self.idle_hint_provider()
                except Exception:
                    hint = None
            return {"events": [], "idle_hint": hint}
        return {"events": out_events, "idle_hint": None}


# ===========================================================================
# Phase 3：跨会话学员记忆模型（同步读 < 0.1ms，异步落盘写，不阻塞状态机）
# ===========================================================================

class StudentMemoryStore:
    """跨会话学员模型（Phase 3：异步写、同步读）。"""

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        if self.store_path and self.store_path.exists():
            try:
                raw = json.loads(self.store_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
            except Exception:
                self._data = {}

    def _profile(self, student_id: str) -> dict[str, Any]:
        sid = (student_id or "S001").strip() or "S001"
        prof = self._data.get(sid)
        if not isinstance(prof, dict):
            prof = {
                "student_id": sid,
                "notes": {},
                "weak_kp": [],
                "confirmed_kp": [],
                "updated_at": "",
            }
            self._data[sid] = prof
        return prof

    def read_sync(self, student_id: str, key: str = "") -> dict[str, Any]:
        """同步读取学员画像（纯内存读取，零 I/O 阻塞）。"""
        prof = self._profile(student_id)
        if key:
            return {
                "student_id": prof["student_id"],
                "key": key,
                "value": prof.get("notes", {}).get(key),
            }
        return {
            "student_id": prof["student_id"],
            "notes": dict(prof.get("notes", {})),
            "weak_kp": list(prof.get("weak_kp", [])),
            "confirmed_kp": list(prof.get("confirmed_kp", [])),
            "updated_at": prof.get("updated_at", ""),
        }

    def write_async(self, student_id: str, key: str, note: str) -> dict[str, Any]:
        """更新内存并异步写盘。"""
        prof = self._profile(student_id)
        k = (key or "general").strip() or "general"
        prof.setdefault("notes", {})[k] = note.strip()
        prof["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._schedule_flush()
        return {"ok": True, "student_id": prof["student_id"], "key": k, "note": prof["notes"][k]}

    def delete_async(self, student_id: str, key: str) -> dict[str, Any]:
        prof = self._profile(student_id)
        existed = prof.get("notes", {}).pop(key, None) is not None
        prof["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._schedule_flush()
        return {"ok": True, "student_id": prof["student_id"], "key": key, "deleted": existed}

    def record_signal_async(
        self,
        student_id: str,
        *,
        weak_kp: list[str] | None = None,
        confirmed_kp: list[str] | None = None,
    ) -> None:
        prof = self._profile(student_id)
        for kp in (weak_kp or []):
            if kp and kp not in prof.setdefault("weak_kp", []):
                prof["weak_kp"].append(kp)
        for kp in (confirmed_kp or []):
            if kp and kp not in prof.setdefault("confirmed_kp", []):
                prof["confirmed_kp"].append(kp)
        prof["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self.store_path is None:
            return
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = loop.create_task(self._flush_to_disk())
        except RuntimeError:
            # 无运行中事件循环时（同步单测）直接写盘
            self._write_sync()

    async def _flush_to_disk(self) -> None:
        await asyncio.sleep(0.01)
        if not self._dirty or self.store_path is None:
            return
        self._dirty = False
        await asyncio.to_thread(self._write_sync)

    def _write_sync(self) -> None:
        if self.store_path is None:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            self.store_path.write_text(payload, encoding="utf-8")
        except Exception:
            pass


# ===========================================================================
# MCP Server 主体（JSON-RPC 2.0 over HTTP POST /mcp，§4）
# ===========================================================================

class _RpcArgError(ValueError):
    """触发 JSON-RPC -32602 参数错误。"""


class McpBrainServer:
    """状态机侧 MCP Server 实现（唯一权威、双活、永不停摆）。"""

    def __init__(
        self,
        machine: "Machine | None" = None,
        *,
        mode: McpMode | None = None,
        memory_store: StudentMemoryStore | None = None,
        allow_action_tools: bool = False,
    ) -> None:
        self.mode = mode or McpMode()
        self.transcript = McpTranscript()
        self.allow_action_tools = allow_action_tools
        self._machine: "Machine | None" = None
        self._say_seq = 0
        self.last_say_enqueue_ms: float = 0.0
        self.say_receipts: list[dict[str, Any]] = []

        cache_path = Path(__file__).resolve().parent / ".cache" / "student_memory.json"
        self.memory = memory_store or StudentMemoryStore(cache_path)

        self.event_bus = EventBus(
            self.mode,
            on_drop=self._on_event_dropped,
            idle_hint_provider=self.compute_idle_hint,
        )
        if machine is not None:
            self.bind_machine(machine)

    @property
    def machine(self) -> "Machine | None":
        return self._machine

    def bind_machine(self, machine: "Machine | None") -> None:
        """绑定当前活跃的课堂状态机实例。"""
        self._machine = machine
        if machine is not None:
            machine.attach_mcp(self)

    def _on_event_dropped(self, dropped: dict[str, Any]) -> None:
        if self._machine is not None:
            self._machine.metrics.event(
                "event_dropped",
                dropped_seq=dropped.get("seq"),
                dropped_kind=dropped.get("kind"),
            )

    # ==================================================================
    # 快照、版本校验、发言权（Floor Control）与预知 idle_hint
    # ==================================================================

    def compute_floor(self) -> dict[str, Any]:
        """计算当前发言权状态（§7.2 `can_speak` 判据表）。"""
        m = self._machine
        if m is None:
            return {"can_speak": False, "next_window_ms": 0, "reason": "classroom not started"}

        q_depth = len(m.agent_speech_queue)
        if q_depth >= MAX_SAY_QUEUE_DEPTH:
            return {
                "can_speak": False,
                "next_window_ms": max(1000, q_depth * 2000),
                "reason": "队列里已有 ≥2 条待播话术",
            }

        st = m.state
        if st.phase in (
            Phase.ACKNOWLEDGING,
            Phase.YIELDING,
            Phase.REDIRECTING,
            Phase.ANSWERING,
            Phase.RESUMING,
        ):
            rem_sents = len(m._answer_buf) + len(m._rephrase_buf)
            est_ms = max(800, rem_sents * 1800)
            return {
                "can_speak": False,
                "next_window_ms": est_ms,
                "reason": "状态机正在播自己的回答",
            }

        if st.phase in (Phase.PAUSING_AUDIO, Phase.PAUSING):
            return {
                "can_speak": True,
                "next_window_ms": 0,
                "reason": "探针窗口内",
            }

        # LECTURING：检查当前讲解单元是否还有未播完的句子
        unit = m.script.unit_of(st.slide_index, st.seg_index)
        if unit is not None:
            sents = m._cur_unit_sents or split_sentences(unit.text)
            done = st.unit_progress.get(unit.unit_id, 0)
            if done < len(sents):
                rem_text = "".join(sents[done:])
                rem_ms = max(400, int(estimate_seconds(rem_text) * 1000))
                return {
                    "can_speak": False,
                    "next_window_ms": rem_ms,
                    "reason": "当前讲解单元未播完",
                }
        return {
            "can_speak": True,
            "next_window_ms": 0,
            "reason": "单元刚播完",
        }

    def compute_idle_hint(self) -> dict[str, Any] | None:
        """利用预生成课堂脚本预知未来，给出下次有效轮询提示（§4.3, Phase 2）。"""
        m = self._machine
        if m is None:
            return None
        st = m.state
        if st.phase in (Phase.PAUSING_AUDIO, Phase.PAUSING):
            return {
                "next_useful_poll_ms": int(m.settings.probe_window * 1000),
                "reason": "当前处于页尾理解探针窗口",
            }
        units = m.script.units_of(st.slide_index)[st.seg_index:]
        if not units:
            return {"next_useful_poll_ms": 50_000, "reason": "本页单元即将结束"}
        continuous = 0
        est_sec = 0.0
        for u in units:
            continuous += 1
            est_sec += u.est_sec
            if u.is_page_end:
                break
        return {
            "next_useful_poll_ms": max(5_000, int(est_sec * 1000)),
            "reason": f"接下来 {continuous} 个单元是连续讲解，无探针",
        }

    def check_version(
        self, from_version: dict[str, Any] | None
    ) -> tuple[str, int | None]:
        """校验 say(from_version) 与当前课堂位置的偏差（§6.2）。

        返回 (version_check, from_slide_1based)：
          - "ok"             : 页码一致（或未提供 from_version）
          - "stale_prefixed" : 页码变了但相差 <= 2 页（自动加「补充一下刚才第 X 页那个点——」）
          - "dropped"        : 页码相差 > 2 页（丢弃并转入 state.deferred）
        """
        m = self._machine
        if m is None or not isinstance(from_version, dict) or "slide" not in from_version:
            return "ok", None
        try:
            raw_slide = int(from_version["slide"])
        except (TypeError, ValueError):
            return "ok", None

        # 统一到 1-based 页码（若传入 0 则视为第 1 页）
        from_page = max(1, raw_slide)
        cur_page = m.state.slide_index + 1
        diff = abs(cur_page - from_page)
        if diff == 0:
            return "ok", from_page
        if diff <= 2:
            return "stale_prefixed", from_page
        return "dropped", from_page

    # ==================================================================
    # 事件生成辅助（供 Machine / Runner 接线点调用，§5.1 & §5.2 & §10.2）
    # ==================================================================

    def emit_student_query(
        self,
        text: str,
        *,
        via: str = "text",
        triage_stage: str = "fallback",
        triage_verdict: str = "unknown",
        triage_reason: str = "规则未命中，小模型也未给出 qkind",
        llm_saved: bool = False,
        handled_by: str | None = "state_machine",
    ) -> dict[str, Any]:
        """当 triage 为 fallback 时推入 urgent query 事件（§5.2 事件信封）。"""
        m = self._machine
        snap_ver = {"slide": 1, "unit": "", "seq": 0}
        snap_digest = ""
        phase_str = "LECTURING"
        if m is not None:
            ctx = build_ctx(m.script, m.state)
            snap_ver = ctx.version
            snap_digest = ctx.digest()
            phase_str = m.state.phase.value

        kind = "student_voice" if via == "voice" else "student_text"
        self.transcript.record("QUERY", f"[{kind}] {text}")
        return self.event_bus.push(
            {
                "type": "query",
                "kind": kind,
                "urgent": True,
                "phase": phase_str,
                "student": {"text": text, "via": via},
                "triage": {
                    "stage": triage_stage,
                    "verdict": triage_verdict,
                    "reason": triage_reason,
                    "llm_saved": bool(llm_saved),
                },
                "handled_by": handled_by,
                "floor": self.compute_floor(),
                "snapshot": {
                    "version": snap_ver,
                    "digest": snap_digest,
                },
            }
        )

    def emit_classroom_event(self, kind: str, **payload: Any) -> dict[str, Any]:
        """推入普通课堂事件（urgent=False，§5.1）。"""
        m = self._machine
        phase_str = m.state.phase.value if m is not None else "LECTURING"
        snap_ver = {"slide": (m.state.slide_index + 1) if m else 1,
                    "unit": m._cur_unit_id() if m else "",
                    "seq": m.state.seq if m else 0}
        return self.event_bus.push(
            {
                "type": "event",
                "kind": kind,
                "urgent": False,
                "phase": phase_str,
                "floor": self.compute_floor(),
                "snapshot": {"version": snap_ver},
                **payload,
            }
        )

    # ==================================================================
    # JSON-RPC 2.0 协议层（§4.1, §4.7）
    # ==================================================================

    @property
    def instructions(self) -> str:
        return INSTRUCTIONS_EN if self.mode.lang == "en" else INSTRUCTIONS_ZH

    async def handle_jsonrpc(self, raw_body: bytes | str | dict | list) -> tuple[int, Any]:
        """处理一次 HTTP POST /mcp 请求，返回 (http_status, response_json_or_none)。"""
        self.mode.touch()

        if isinstance(raw_body, (bytes, str)):
            text = raw_body.decode("utf-8", errors="replace") if isinstance(raw_body, bytes) else raw_body
            try:
                parsed = json.loads(text)
            except Exception as e:
                return 200, self._error_response(None, -32700, f"parse error: {e}")
        else:
            parsed = raw_body

        if isinstance(parsed, list):
            if not parsed:
                return 200, self._error_response(None, -32600, "empty batch")
            out = []
            for item in parsed:
                if not isinstance(item, dict):
                    out.append(self._error_response(None, -32600, "invalid request"))
                    continue
                res = await self._dispatch_one(item)
                if res is not None:
                    out.append(res)
            if not out:
                return 202, None
            return 200, out

        if not isinstance(parsed, dict):
            return 200, self._error_response(None, -32600, "invalid request")

        res = await self._dispatch_one(parsed)
        if res is None:
            return 202, None
        return 200, res

    async def _dispatch_one(self, req: dict[str, Any]) -> dict[str, Any] | None:
        method = str(req.get("method") or "")
        has_id = "id" in req and req.get("id") is not None
        req_id = req.get("id")

        # Notifications（如 notifications/initialized）无 id，处理副作用后不返回响应体
        if not has_id:
            if method == "notifications/initialized":
                if self.mode.client_name is None:
                    self.mode.handshake("mcp-client")
                else:
                    self.mode.touch()
            return None

        params = req.get("params")
        if params is not None and not isinstance(params, dict):
            return self._error_response(req_id, -32602, "bad arguments: params must be an object")
        params_dict: dict[str, Any] = params or {}

        if method == "initialize":
            return self._ok_response(req_id, self._handle_initialize(params_dict))
        if method == "ping":
            self.mode.touch()
            return self._ok_response(req_id, {})
        if method == "tools/list":
            self.mode.touch()
            return self._ok_response(req_id, self._handle_tools_list())
        if method == "tools/call":
            self.mode.touch()
            return await self._handle_tools_call(req_id, params_dict)

        return self._error_response(req_id, -32601, f"method not found: {method}")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        proto = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        cinfo = params.get("clientInfo")
        if isinstance(cinfo, dict):
            cname = str(cinfo.get("name") or "unknown client").strip()
            cver = str(cinfo.get("version") or "").strip()
            label = f"{cname} {cver}".strip()
        else:
            label = "unknown client"
        self.mode.handshake(label)
        return {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": self.instructions,
        }

    # ==================================================================
    # tools/list（硬约束 6 个工具，§2 D8 & §8）
    # ==================================================================

    def _tool_definitions(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "name": "get_events",
                "description": (
                    "阻塞式长轮询获取课堂事件。学生提问（urgent）会微秒级立即唤醒返回；"
                    "到期清扫返回累积普通事件或空列表。读过的事件消费即消失。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "wait_seconds": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": MAX_WAIT_SECONDS,
                            "default": DEFAULT_WAIT_SECONDS,
                        }
                    },
                },
            },
            {
                "name": "get_state",
                "description": (
                    "非阻塞读取当前课堂进度快照（ContextSnapshot：含 version、slide、unit、"
                    "covered_kp、comprehension、deferred、recent_qa、q_rounds 及 floor 状态）。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "slide": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "可选：查看指定页码（1 起算），不传则取当前页",
                        }
                    },
                },
            },
            {
                "name": "say",
                "description": (
                    "向课堂播放队列提交补充或纠正话术（非阻塞，立即返回 receipt）。"
                    "状态机在当前单元播完的让位点按序播出。必须传入事件的 from_version。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string", "maxLength": MAX_SAY_CHARS},
                        "from_version": {"type": "object"},
                        "final": {"type": "boolean", "default": False},
                    },
                },
            },
            {
                "name": "query_rag",
                "description": (
                    "在当前课程课件、讲稿单元、知识点与锚点中检索相关内容（只读）。"
                    "若检索期间有新的学生急件到达，会在步骤间自动打断并提示先调 get_events。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                    },
                },
            },
            {
                "name": "search_course",
                "description": "按页码或关键词查看课件元素、大纲与锚点详情（只读）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "slide": {"type": "integer", "minimum": 1},
                        "keyword": {"type": "string"},
                    },
                },
            },
            {
                "name": "student_memory",
                "description": "跨会话学员画像与薄弱点记忆（同步读、异步写，不阻塞课堂）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["read", "write", "delete"],
                            "default": "read",
                        },
                        "key": {"type": "string"},
                        "note": {"type": "string"},
                    },
                },
            },
        ]
        # 默认严格遵守 D8 硬约束：只暴露 6 个工具
        if not self.allow_action_tools:
            return tools[:HARD_MAX_TOOLS]

        # Phase 4 可选实验开关开启时，额外附加行动类工具（§4.6）
        tools.extend(
            [
                {
                    "name": "deck_op",
                    "description": "执行白名单内的课件操作（仅在学生明确要求时允许）。",
                    "inputSchema": {
                        "type": "object",
                        "required": ["op"],
                        "properties": {
                            "op": {"type": "string"},
                            "target": {"type": "string"},
                        },
                    },
                },
                {
                    "name": "next_page",
                    "description": "翻页（仅在学生明确要求时允许）。",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["next", "prev"], "default": "next"}
                        },
                    },
                },
            ]
        )
        return tools

    def _handle_tools_list(self) -> dict[str, Any]:
        return {"tools": self._tool_definitions()}

    # ==================================================================
    # tools/call 派发与错误语义（§4.7）
    # ==================================================================

    async def _handle_tools_call(
        self, req_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return self._error_response(req_id, -32602, "bad arguments: arguments must be an object")

        known_tools = {
            "get_events",
            "get_state",
            "say",
            "query_rag",
            "search_course",
            "student_memory",
        }
        action_tools = {"next_unit", "repeat", "next_page", "prev_page", "deck_op"}

        if name not in known_tools and name not in action_tools:
            # §4.7：未知工具名 → JSON-RPC -32601 "unknown tool: xxx"
            return self._error_response(req_id, -32601, f"unknown tool: {name}")

        # 先做参数 schema 校验（§4.7：参数不合 schema → JSON-RPC -32602）
        try:
            self._validate_tool_args(name, args)
        except _RpcArgError as arg_err:
            return self._error_response(req_id, -32602, str(arg_err))

        # §4.7：课堂未开始（无 course / machine） → 正常返回 "classroom not started" + isError: true
        if self._machine is None and name not in ("student_memory",):
            return self._ok_response(req_id, self._content("classroom not started", is_error=True))

        try:
            if name == "get_events":
                res = await self._tool_get_events(args)
            elif name == "get_state":
                res = await self._tool_get_state(args)
            elif name == "say":
                res = await self._tool_say(args)
            elif name == "query_rag":
                res = await self._tool_query_rag(args)
            elif name == "search_course":
                res = await self._tool_search_course(args)
            elif name == "student_memory":
                res = await self._tool_student_memory(args)
            else:
                res = await self._tool_action(name, args)

            if name not in ("get_events", "say"):
                self.transcript.record("TOOL", f"{name}({json.dumps(args, ensure_ascii=False)})")
            return self._ok_response(req_id, res)
        except Exception as exc:
            # §4.7 & §10.3 #10：内部异常绝不吞掉，打 traceback + metrics + notify + isError: true
            tb = traceback.format_exc()
            print(f"[mcp] ❌ 工具 {name} 内部异常：\n{tb}", flush=True)
            err_msg = f"{type(exc).__name__}: {exc}"
            self.transcript.record("TOOL", f"{name} FAILED: {err_msg}", error=True)
            if self._machine is not None:
                self._machine.metrics.event("mcp_error", tool=name, err=err_msg)
                try:
                    await self._machine.h.notify("warn", f"[MCP 异常] {name}: {err_msg}")
                except Exception:
                    pass
            return self._ok_response(req_id, self._content(f"internal error: {err_msg}", is_error=True))

    def _validate_tool_args(self, name: str, args: dict[str, Any]) -> None:
        if name == "get_events":
            if "wait_seconds" in args and args["wait_seconds"] is not None:
                w = args["wait_seconds"]
                if isinstance(w, bool) or not isinstance(w, (int, float)):
                    raise _RpcArgError("bad arguments: wait_seconds 必须是整数")
                if int(w) < 0:
                    raise _RpcArgError("bad arguments: wait_seconds 不能小于 0")
        elif name == "get_state":
            if "slide" in args and args["slide"] is not None:
                s = args["slide"]
                if isinstance(s, bool) or not isinstance(s, int) or s < 1:
                    raise _RpcArgError("bad arguments: slide 必须是 >=1 的整数")
        elif name == "say":
            if "text" not in args or args.get("text") is None:
                raise _RpcArgError("bad arguments: 缺 text")
            if not isinstance(args["text"], str) or not args["text"].strip():
                raise _RpcArgError("bad arguments: 缺 text")
            if "from_version" in args and args["from_version"] is not None:
                if not isinstance(args["from_version"], dict):
                    raise _RpcArgError("bad arguments: from_version 必须是对象")
        elif name == "query_rag":
            q = args.get("query")
            if not isinstance(q, str) or not q.strip():
                raise _RpcArgError("bad arguments: 缺 query")
            if "top_k" in args and args["top_k"] is not None:
                tk = args["top_k"]
                if isinstance(tk, bool) or not isinstance(tk, int) or tk < 1:
                    raise _RpcArgError("bad arguments: top_k 必须是正整数")
        elif name == "student_memory":
            act = args.get("action", "read")
            if act not in ("read", "write", "delete"):
                raise _RpcArgError(f"bad arguments: 未知 action {act!r}")
            if act == "write" and not str(args.get("note") or "").strip():
                raise _RpcArgError("bad arguments: write 需要非空 note")

    # ==================================================================
    # 具体工具实现
    # ==================================================================

    async def _tool_get_events(self, args: dict[str, Any]) -> dict[str, Any]:
        wait = DEFAULT_WAIT_SECONDS
        if "wait_seconds" in args and args["wait_seconds"] is not None:
            wait = max(0, min(MAX_WAIT_SECONDS, int(args["wait_seconds"])))
        payload = await self.event_bus.get_events(wait_seconds=wait)
        # 紧凑序列化，压低空返回 token 成本（§4.3 & §12 #9：< 30 tokens）
        return self._content(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def _tool_get_state(self, args: dict[str, Any]) -> dict[str, Any]:
        assert self._machine is not None
        m = self._machine
        slide_arg = args.get("slide")
        slide_override = (int(slide_arg) - 1) if slide_arg is not None else None
        if slide_override is not None and (slide_override < 0 or slide_override >= m.script.slide_count):
            return self._content(f"slide out of range (1..{m.script.slide_count})", is_error=True)

        ctx = build_ctx(m.script, m.state, slide_override=slide_override)
        data = ctx.to_dict()
        data["phase"] = m.state.phase.value
        data["floor"] = self.compute_floor()
        return self._content(json.dumps(data, ensure_ascii=False))

    async def _tool_say(self, args: dict[str, Any]) -> dict[str, Any]:
        assert self._machine is not None
        t0 = time.monotonic()
        m = self._machine
        raw_text = str(args["text"]).strip()
        from_version = args.get("from_version")
        final = bool(args.get("final", False))

        self.mode.on_say_called(final=final)
        self._say_seq += 1
        seq = self._say_seq

        # 1. 版本校验（§6.2）
        ver_status, from_page = self.check_version(from_version)
        if ver_status == "dropped":
            # 相差 > 2 页：丢弃并记入 state.deferred（复用待回看机制）
            defer_item = f"第{from_page}页补充：{clamp_spoken(raw_text, 40)}"
            if defer_item not in m.state.deferred:
                m.state.deferred.append(defer_item)
            self.emit_classroom_event("deferred_added", item=defer_item, from_slide=from_page)
            floor = self.compute_floor()
            receipt = {
                "status": "dropped",
                "seq": seq,
                "queue_depth": len(m.agent_speech_queue),
                "can_speak_now": False,
                "speak_after_ms": floor["next_window_ms"],
                "reason": f"版本已过期（来自第 {from_page} 页，当前第 {m.state.slide_index + 1} 页），已转入待回看",
                "version_check": "dropped",
            }
            self._record_say_receipt(receipt, t0)
            return self._content(json.dumps(receipt, ensure_ascii=False))

        # 2. 队列深度防话痨检查（§7.2, §10.3 #5, §12 #8：单次队列深度 ≤ 2）
        if len(m.agent_speech_queue) >= MAX_SAY_QUEUE_DEPTH:
            floor = self.compute_floor()
            receipt = {
                "status": "throttled",
                "seq": seq,
                "queue_depth": len(m.agent_speech_queue),
                "can_speak_now": False,
                "speak_after_ms": floor["next_window_ms"],
                "reason": "队列深度已达上限（2 条），请等待播出后再发言",
                "version_check": ver_status,
            }
            self._record_say_receipt(receipt, t0)
            return self._content(json.dumps(receipt, ensure_ascii=False))

        # 3. 净化与版本过期前缀（§6.2, §8：上限 300 字）
        clean = sanitize_spoken(raw_text) or raw_text
        if ver_status == "stale_prefixed" and from_page is not None:
            prefix = f"补充一下刚才第 {from_page} 页那个点——"
            if not clean.startswith("补充一下刚才第"):
                clean = prefix + clean
        clean = clamp_spoken(clean, MAX_SAY_CHARS)

        # 4. 入队（非阻塞，由状态机在让位点播出，铁律 #3）
        m.enqueue_agent_speech(clean, seq=seq, from_version=from_version, version_check=ver_status)
        self.transcript.record("SAY", f"[#{seq} · {ver_status}] {clean}")

        q_depth = len(m.agent_speech_queue)
        floor = self.compute_floor()
        can_speak_now = bool(floor["can_speak"]) and q_depth < MAX_SAY_QUEUE_DEPTH
        receipt = {
            "status": "queued",
            "seq": seq,
            "queue_depth": q_depth,
            "can_speak_now": can_speak_now,
            "speak_after_ms": floor["next_window_ms"],
            "reason": floor["reason"],
            "version_check": ver_status,
        }
        self._record_say_receipt(receipt, t0)
        return self._content(json.dumps(receipt, ensure_ascii=False))

    def _record_say_receipt(self, receipt: dict[str, Any], t0: float) -> None:
        self.last_say_enqueue_ms = round((time.monotonic() - t0) * 1000.0, 3)
        self.say_receipts.append({**receipt, "enqueue_ms": self.last_say_enqueue_ms})
        if len(self.say_receipts) > 100:
            self.say_receipts = self.say_receipts[-100:]
        if self._machine is not None:
            self._machine.metrics.event(
                "mcp_say",
                seq=receipt["seq"],
                status=receipt["status"],
                queue_depth=receipt["queue_depth"],
                version_check=receipt["version_check"],
                enqueue_ms=self.last_say_enqueue_ms,
            )

    async def _tool_query_rag(self, args: dict[str, Any]) -> dict[str, Any]:
        """检索课件与讲稿知识库，支持「两次调用之间被急件打断」（§10.3 #6）。"""
        assert self._machine is not None
        m = self._machine
        query = str(args["query"]).strip()
        top_k = max(1, min(10, int(args.get("top_k") or 3)))

        # 步骤 1 前检查是否有未消费的急件到达（§10.3 #6）
        if self.event_bus.has_unconsumed_urgent():
            return self._content(
                json.dumps(
                    {
                        "interrupted": True,
                        "reason": "urgent_query_arrived",
                        "message": "收到学生新提问（urgent），当前检索已让路，请立即调用 get_events。",
                        "results": [],
                    },
                    ensure_ascii=False,
                )
            )

        tokens = [tok.lower() for tok in query.replace("？", " ").replace("?", " ").split() if tok]
        chars = {ch for ch in query.lower() if ch.strip() and ch not in "，。！？、；：\"'（）() "}

        scored: list[tuple[float, dict[str, Any]]] = []
        for idx, u in enumerate(m.script.units):
            # 分段检索中途让出事件循环并检查急件打断（模拟两次子调用之间打断）
            if idx > 0 and idx % 20 == 0:
                await asyncio.sleep(0)
                if self.event_bus.has_unconsumed_urgent():
                    return self._content(
                        json.dumps(
                            {
                                "interrupted": True,
                                "reason": "urgent_query_arrived",
                                "message": "检索中途收到学生急件提问，已打断并返回部分结果，请立即调用 get_events。",
                                "results": [item for _, item in sorted(scored, key=lambda x: -x[0])[:top_k]],
                            },
                            ensure_ascii=False,
                        )
                    )

            slide = m.script.slide_at(u.slide)
            slide_title = slide.title if slide else ""
            elem_text = " ".join(e.text for e in (slide.elements if slide else []) if e.text)
            haystack = f"{slide_title} {u.text} {' '.join(u.kp)} {elem_text}".lower()

            score = 0.0
            if query.lower() in haystack:
                score += 5.0
            for tok in tokens:
                if tok in haystack:
                    score += 2.0
            if chars:
                hit_chars = sum(1 for ch in chars if ch in haystack)
                score += hit_chars / max(1, len(chars))

            if score > 0.15:
                scored.append(
                    (
                        round(score, 3),
                        {
                            "slide": u.slide + 1,
                            "unit_id": u.unit_id,
                            "slide_title": slide_title,
                            "kp": list(u.kp),
                            "text": u.text[:160],
                            "elements": [
                                {"id": e.element_id, "text": e.text[:80]}
                                for e in (slide.elements[:6] if slide else [])
                                if e.text
                            ],
                        },
                    )
                )

        scored.sort(key=lambda x: -x[0])
        hits = [item for _, item in scored[:top_k]]
        return self._content(
            json.dumps({"interrupted": False, "query": query, "results": hits}, ensure_ascii=False)
        )

    async def _tool_search_course(self, args: dict[str, Any]) -> dict[str, Any]:
        assert self._machine is not None
        m = self._machine
        if self.event_bus.has_unconsumed_urgent():
            return self._content(
                json.dumps(
                    {
                        "interrupted": True,
                        "reason": "urgent_query_arrived",
                        "message": "收到学生新提问（urgent），请先调用 get_events。",
                    },
                    ensure_ascii=False,
                )
            )

        slide_no = args.get("slide")
        keyword = str(args.get("keyword") or "").strip().lower()

        if slide_no is not None:
            idx = int(slide_no) - 1
            slide = m.script.slide_at(idx)
            if slide is None:
                return self._content(f"没有第 {slide_no} 页（共 {m.script.slide_count} 页）", is_error=True)
            units = m.script.units_of(idx)
            return self._content(
                json.dumps(
                    {
                        "slide": idx + 1,
                        "title": slide.title,
                        "elements": [
                            {"id": e.element_id, "kind": e.kind, "text": e.text}
                            for e in slide.elements
                        ],
                        "units": [
                            {"unit_id": u.unit_id, "text": u.text, "kp": u.kp}
                            for u in units
                        ],
                    },
                    ensure_ascii=False,
                )
            )

        slides_out = []
        for s in m.script.slides:
            units = m.script.units_of(s.index)
            joined = f"{s.title} " + " ".join(e.text for e in s.elements) + " " + " ".join(u.text for u in units)
            if keyword and keyword not in joined.lower():
                continue
            slides_out.append(
                {
                    "slide": s.index + 1,
                    "title": s.title,
                    "unit_count": len(units),
                    "kp": [kp for u in units for kp in u.kp],
                }
            )
        return self._content(
            json.dumps(
                {
                    "course_id": m.script.course_id,
                    "title": m.script.title,
                    "slide_count": m.script.slide_count,
                    "slides": slides_out[:20],
                },
                ensure_ascii=False,
            )
        )

    async def _tool_student_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        sid = self._machine.state.student_id if self._machine is not None else "S001"
        action = str(args.get("action") or "read").strip()
        key = str(args.get("key") or "").strip()
        note = str(args.get("note") or "").strip()

        if action == "read":
            data = self.memory.read_sync(sid, key=key)
            if self._machine is not None and not key:
                data["session_comprehension"] = dict(self._machine.state.comprehension)
                data["session_deferred"] = list(dict.fromkeys(self._machine.state.deferred))
            return self._content(json.dumps(data, ensure_ascii=False))
        if action == "write":
            res = self.memory.write_async(sid, key=key or "note", note=note)
            return self._content(json.dumps(res, ensure_ascii=False))
        res = self.memory.delete_async(sid, key=key)
        return self._content(json.dumps(res, ensure_ascii=False))

    async def _tool_action(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """§4.6 行动类工具：默认不开放，开启后做严格白名单与锚点校验。"""
        if not self.allow_action_tools:
            return self._content(
                f"action tool '{name}' is disabled in read-only advisor mode (§4.6)",
                is_error=True,
            )
        assert self._machine is not None
        m = self._machine
        if name == "deck_op":
            from deck.ops import DeckOp, validate_ops

            op_name = str(args.get("op") or "").strip()
            target = str(args.get("target") or "").strip()
            slide = m.script.slide_at(m.state.slide_index)
            anchor_ids = {e.element_id for e in (slide.elements if slide else [])}
            op_obj = DeckOp(op=op_name, target=target, at_sentence=1)
            errs = validate_ops([op_obj], anchor_ids, unit_id=m._cur_unit_id())
            if errs:
                return self._content("; ".join(errs), is_error=True)
            await m.h.highlight(slide.elements[:1] if slide and slide.elements else [])
            return self._content(json.dumps({"ok": True, "op": op_obj.to_dict()}, ensure_ascii=False))

        if name in ("next_page", "prev_page"):
            direction = "prev" if name == "prev_page" else str(args.get("direction") or "next")
            await m.handle_command("prev" if direction == "prev" else "next")
            return self._content(json.dumps({"ok": True, "slide": m.state.slide_index + 1}))

        if name == "repeat":
            await m.handle_command("repeat")
            return self._content(json.dumps({"ok": True, "action": "repeat"}))

        if name == "next_unit":
            await m.handle_command("next")
            return self._content(json.dumps({"ok": True, "action": "next_unit"}))

        return self._content(f"unsupported action: {name}", is_error=True)

    # ==================================================================
    # JSON-RPC 封包辅助
    # ==================================================================

    @staticmethod
    def _content(text: str, *, is_error: bool = False) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": text}],
            "isError": bool(is_error),
        }

    @staticmethod
    def _ok_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        }

    @staticmethod
    def _error_response(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": int(code),
                "message": str(message),
            },
        }
