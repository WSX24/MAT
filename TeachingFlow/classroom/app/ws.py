"""WS 会话入口（设计文档 §4 `/ws/classroom/{session_id}`）。

这一层**只做接线**：取行 → 造运行器 → 转发四种客户端消息 → 断开时收尾落库。
课堂怎么讲、什么时候推进、暂停怎么停住，全在 `studio/runner.py` 里 —— 这个文件
出现一行"推进逻辑"就说明分层被写漏了。

两条协议细节：

* 每条 S→C 帧都是 `{"kind": ..., "ts": ...}` 的形状里带 payload；`kind` 是契约字段，
  前端按它分发。
* 收到不是 JSON / 不认识的 `t` 时**回一条 notify 而不是断开**：学生端脚本出问题时，
  断开只会让人以为"服务挂了"，一条中文提示才能让人知道该改哪儿。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from store import db as db_mod
from store import repo

from . import deps

log = logging.getLogger("classroom.ws")

#: 本进程正在跑的会话（session_id → runner）。
#: `POST /sessions/{id}/end` 靠它让运行器先收尾（算 recap、落库）再返回；
#: 多进程部署时它只覆盖本进程，但那属于 v1 非目标（无房间码/多机同步）。
LIVE: dict[str, object] = {}


def get_runner(session_id: str):
    return LIVE.get(session_id)


def build_runner(*, session: dict, unit: dict, profile: dict | None, send, conn):
    """造运行器。测试 monkeypatch 这里注入 stub 课件（生产不传这两个参数）。"""
    from studio.runner import SessionRunner

    return SessionRunner(
        session=session,
        unit=unit,
        profile=profile,
        send=send,
        conn=conn,
        settings=deps.get_settings(),
        tts=deps.tts_for(),
        tts_factory=deps.tts_for,
    )


async def classroom_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()

    async def send(kind: str, payload: dict) -> None:
        frame = {"kind": kind, "ts": datetime.now().isoformat(timespec="seconds"), **payload}
        await websocket.send_text(json.dumps(frame, ensure_ascii=False, default=str))

    conn = db_mod.connect()
    runner = None
    try:
        deps.ensure_db(conn)
        session = repo.get_session(conn, session_id)
        if session is None:
            await send("notify", {"level": "error",
                                  "text": "找不到这个会话（可能已被删除），请回课程库重新开课。"})
            await websocket.close()
            return
        unit = repo.get_unit(conn, session["unit_id"]) or {}
        profile = repo.get_profile(conn, session["profile_id"])
        runner = build_runner(session=session, unit=unit, profile=profile,
                              send=send, conn=conn)
        LIVE[session_id] = runner
        await runner.start()
        while True:
            raw = await websocket.receive_text()
            await _dispatch(runner, raw, send)
    except WebSocketDisconnect:
        log.info("客户端断开 session=%s", session_id)
    except Exception:
        log.exception("WS 异常 session=%s", session_id)
        try:
            await send("notify", {"level": "error", "text": "课堂连接出错，已断开。"})
        except Exception:
            pass
    finally:
        LIVE.pop(session_id, None)
        if runner is not None:
            try:
                await runner.close()          # 断线 = 暂停（可续学），不是结束
            except Exception:
                log.exception("运行器收尾失败 session=%s", session_id)
        try:
            conn.close()
        except Exception:
            pass


async def _dispatch(runner, raw: str, send) -> None:
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        await send("notify", {"level": "warn", "text": "收到一条不是 JSON 的消息，已忽略。"})
        return
    if not isinstance(msg, dict):
        await send("notify", {"level": "warn", "text": "消息必须是 JSON 对象。"})
        return
    kind = msg.get("t")
    if kind == "input":
        await runner.on_input(str(msg.get("text") or ""), via=str(msg.get("via") or "text"))
    elif kind == "cmd":
        await runner.on_command(str(msg.get("cmd") or ""))
    elif kind == "rate":
        await runner.on_rate(msg.get("value"))
    elif kind == "speech_done":
        try:
            seq = int(msg.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        await runner.on_speech_done(seq)
    else:
        await send("notify", {"level": "warn",
                             "text": f"不认识的消息类型：{kind!r}（支持 input / cmd / rate / speech_done）"})
