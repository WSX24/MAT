"""交互式课程生成：对话提问 → 需求确认 → 课程大纲 → 单元课程安排设计 → 存入课程库。

为什么另起一条链路（而不是改 flow.py）
------------------------------------
``flow.py`` 的 S0 是「一句话 → 规则抽学段/学科 → 内置模块表 → 估周期」，它把"用户到底想
学到什么程度、有什么基础、想学多久"全部交给了关键词与默认值 —— 于是「我想学习离散数学」
会被判成「初中·数学·数与式」再排 2 周。这一层要补的正是那一步：**真正问清楚再排课**。

它不重写任何既有能力，只把四段接起来：

    course_plan.py（纯函数：规整 / 蓝图 / 渲染）
        ↑
    本模块（会话状态机 + 模型工具调用 + 后台任务 + 落盘 + 入库）
        ↓
    flow.py --resume <run> --unit <uid>（S4 页骨架 + S5 产片，逐单元按需跑）

三处刻意的产品决定
------------------
1. **大纲先给用户看、改完再排课**。所以「生成大纲」与「生成单元设计」是两个阶段：
   前者快（一次模型调用），后者慢（每章一次调用）且用户看不到内容 —— 把它压到确认
   之后，用户就不会盯着一个没有反馈的长进度条改主意。
2. **PPT 一课一课生成**。``flow.py`` 一次能排 N 个单元，但首次全量出片要十几分钟；
   渐进式生成把等待摊到"上哪一课之前"，而 ``units.pptx_path`` 就是"这课有没有片"的账本。
3. **没有模型也要能走完**。``course_plan.offline_*`` 让无 key 的机器与测试照样得到
   结构正确的大纲与设计；真实建模只是让内容更好，不是让链路能跑。

进度为什么写在库里
------------------
与 generation.py 同一口径：运行中任务的实时快照在内存（``_LIVE``），阶段切换时写库。
代价是进程重启后库里的行会停在"运行中"，所以 ``reconcile()`` 在启动时把它们结算成
interrupted —— 否则页面会永远显示一个不会动的进度条。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import course_plan
import modelconfig
from store import db as db_mod, repo

log = logging.getLogger("classroom.course_builder")

#: TeachingFlow/（flow.py 的 cwd）
FLOW_DIR = db_mod.CLASSROOM_ROOT.parent
FLOW_SCRIPT = FLOW_DIR / "flow.py"

#: 后台任务超时：一次单元设计 = 每章一次模型调用，一章按 3 分钟算
DESIGN_TIMEOUT = 45 * 60
#: 出片：实测一个单元 3~5 分钟，30 分钟没结束基本是卡死
PPT_TIMEOUT = 30 * 60
#: 模型单次调用超时：对话一轮 / 要一段 JSON（大纲、单元设计）。
#: 分开两个值：对话是"用户正等着"的，不能等太久；大纲是一次性大输出，允许慢一些。
DIALOG_TIMEOUT = 90.0
JSON_TIMEOUT = 240.0

#: 多久没有新输出就补一行"我还在运行"（渲染阶段本来就可能几分钟不出声）
HEARTBEAT_SECONDS = 45.0

LOG_TAIL_KEEP = 60

#: 阶段词表（前端按它画进度，别在别处另造一套）
SESSION_TASK_STAGES = {
    "outline": ("理解需求", "生成大纲", "整理章单元"),
    "design": ("读取大纲", "生成单元设计", "写入课程库"),
}

PHASES = ("clarifying", "confirming_requirements", "generating_outline",
          "editing_outline", "generating_course", "ready")

_ACTIVE_PHASES = ("generating_outline", "generating_course")

PPT_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")


class BuilderError(Exception):
    """发起/推进不了。code 直接对应 HTTP 语义（bad_request | not_found | conflict）。"""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.code = code


# ===========================================================================
# 基础
# ===========================================================================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect():
    conn = db_mod.connect()
    db_mod.migrate(conn)
    return conn


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _runs_root() -> Path:
    """runs 根：读 deps 的当前值（测试会把它指到 tmp_path）。"""
    try:
        from app import deps

        return Path(deps.RUNS_ROOT)
    except Exception:
        return FLOW_DIR / "runs"


def _safe_name(text: str, limit: int = 40) -> str:
    """与 core/artifacts.py::safe_name 同口径（目录名要能被 catalog 读回标题）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", text or "").strip("_")
    return s[:limit] or "未命名"


# ===========================================================================
# 会话行
# ===========================================================================

_SESSION_COLUMNS = (
    "id, title, phase, messages_json, requirements_json, outline_json, outline_path,"
    " run_dir, course_id, unit_designs_count, task_kind, task_status, task_stage,"
    " task_percent, task_error, task_log, created_at, updated_at"
)


def _blank_task() -> dict:
    return {"kind": "", "status": "", "stage": "", "percent": 0.0, "error": "", "log": []}


def _row_to_session(row) -> dict:
    data = dict(row)
    return {
        "id": str(data.get("id") or ""),
        "title": str(data.get("title") or ""),
        "phase": str(data.get("phase") or "clarifying"),
        "messages": _loads(data.get("messages_json"), []),
        "requirements": _loads(data.get("requirements_json"), None),
        "outline": _loads(data.get("outline_json"), None),
        "outline_path": str(data.get("outline_path") or ""),
        "run_dir": str(data.get("run_dir") or ""),
        "course_id": str(data.get("course_id") or ""),
        "unit_designs_count": int(data.get("unit_designs_count") or 0),
        "task": {
            "kind": str(data.get("task_kind") or ""),
            "status": str(data.get("task_status") or ""),
            "stage": str(data.get("task_stage") or ""),
            "percent": float(data.get("task_percent") or 0.0),
            "error": str(data.get("task_error") or ""),
            "log": _loads(data.get("task_log"), []),
        },
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def _get_row(conn, session_id: str) -> dict:
    row = conn.execute(f"SELECT {_SESSION_COLUMNS} FROM course_builder_sessions WHERE id=?",
                       (session_id,)).fetchone()
    if row is None:
        raise BuilderError("找不到这个课程生成会话", "not_found")
    return dict(row)


def _save_row(conn, row: dict) -> None:
    row["updated_at"] = _now()
    conn.execute(
        "UPDATE course_builder_sessions SET title=:title, phase=:phase,"
        " messages_json=:messages_json, requirements_json=:requirements_json,"
        " outline_json=:outline_json, outline_path=:outline_path, run_dir=:run_dir,"
        " course_id=:course_id, unit_designs_count=:unit_designs_count,"
        " task_kind=:task_kind, task_status=:task_status, task_stage=:task_stage,"
        " task_percent=:task_percent, task_error=:task_error, task_log=:task_log,"
        " updated_at=:updated_at WHERE id=:id", row)
    conn.commit()


def _view(row: dict) -> dict:
    """库里的行 + 内存里的实时任务 → 前端要的快照。"""
    data = _row_to_session(row)
    live = _LIVE.get(str(row.get("id") or ""))
    if live is not None:
        data["task"] = {**live, "log": list(live.get("log") or [])}
    data["llm"] = _llm_hint()
    return data


#: 「这次用哪个模型」的提示缓存：它只给页面一句文案，而会话是 1.2 秒轮询一次 ——
#: 每次轮询都开一条连接问一遍，纯属把轮询变成写放大。30 秒够短，改完配置一眼就见效。
_HINT = {"at": 0.0, "value": None}
HINT_TTL = 30.0


def _llm_hint() -> dict:
    """前端要有能力说清"这次是真模型排的课还是规则兜底"。"""
    now = time.monotonic()
    cached = _HINT["value"]
    if cached is not None and now - float(_HINT["at"]) < HINT_TTL:
        return cached
    conn = None
    try:
        conn = _connect()
        target = _model_target(conn)
    except Exception:
        target = None
    finally:
        if conn is not None:
            conn.close()
    if target is None:
        value = {"ready": False, "model": "",
                 "reason": "没有可用的模型服务商：大纲与单元设计会用规则兜底"}
    else:
        value = {"ready": True, "model": target["model"], "reason": ""}
    _HINT.update({"at": now, "value": value})
    return value


def _reset_llm_hint() -> None:
    _HINT.update({"at": 0.0, "value": None})


# ===========================================================================
# 消息
# ===========================================================================

def _next_message_id(messages: list[dict]) -> int:
    return max([int(m.get("id") or 0) for m in messages] or [0]) + 1


def _push(messages: list[dict], role: str, text: str = "", **extra) -> dict:
    item = {"id": _next_message_id(messages), "role": role, "text": text,
            "at": _now(), **extra}
    messages.append(item)
    return item


def _pending_tool(messages: list[dict]) -> dict | None:
    """最后一条还没被回答/决定的工具消息（前端据此高亮那张卡）。"""
    for item in reversed(messages):
        tool = item.get("tool")
        if not isinstance(tool, dict):
            continue
        if tool.get("name") == "ask_user_question" and not tool.get("answered"):
            return tool
        if tool.get("name") == "confirm_course_requirements" and not tool.get("decision"):
            return tool
    return None


def _mark_answered(messages: list[dict], answer: str) -> None:
    tool = _pending_tool(messages)
    if tool is None or tool.get("name") != "ask_user_question":
        return
    tool["answered"] = True
    tool["answer"] = answer


# ===========================================================================
# 模型出口
# ===========================================================================

def _model_target(conn) -> dict | None:
    """这次排课用哪家模型：库里的默认服务商优先，其次仓库根 .env。

    与 generation.py 同一条口径：**能不能真跑**由"有 key（或本地端点）且有模型名"
    判定。没有就返回 None，调用方走规则兜底而不是报错 —— 没有 key 的机器也该
    能把整条产品流程点完。
    """
    default = None
    try:
        default = modelconfig.get_default(conn)
    except Exception:
        default = None
    if default:
        provider = default.get("provider") or {}
        model = str(default.get("model") or "").strip()
        base_url = str(provider.get("base_url") or "").strip()
        api_key = str(provider.get("api_key") or "").strip()
        if model and base_url and (api_key or _is_local(base_url)):
            return {"base_url": base_url, "api_key": api_key, "model": model,
                    "name": str(provider.get("name") or provider.get("kind") or "默认服务商")}
    try:
        import config

        settings = config.load_settings()
        profile = settings.active
        if profile.base_url and profile.model and (profile.api_key or _is_local(profile.base_url)):
            return {"base_url": profile.base_url, "api_key": profile.api_key or "",
                    "model": profile.model, "name": profile.name}
    except Exception:
        pass
    return None


def _is_local(base_url: str) -> bool:
    low = (base_url or "").lower()
    return any(h in low for h in ("127.0.0.1", "localhost", "0.0.0.0"))


def _client(target: dict):
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url=target["base_url"],
        api_key=target["api_key"] or "not-set",
        timeout=JSON_TIMEOUT,
        # 与 classroom/llm.py 同一条理由：某些 httpx 版本对压缩响应会抛 TypeError
        default_headers={"Accept-Encoding": "identity"},
    )


async def _raw_chat(target: dict, messages: list[dict], *, tools: list[dict] | None = None,
                    temperature: float = 0.4, max_tokens: int = 2000) -> tuple[str, list[dict]]:
    """一次对话调用，返回 (正文, 工具调用列表)。"""
    client = _client(target)
    kwargs: dict[str, Any] = {
        "model": target["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = await client.chat.completions.create(**kwargs)
    message = resp.choices[0].message
    text = (message.content or "").strip()
    calls: list[dict] = []
    for call in (getattr(message, "tool_calls", None) or []):
        fn = getattr(call, "function", None)
        if fn is None:
            continue
        try:
            arguments = json.loads(fn.arguments or "{}")
        except (TypeError, ValueError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append({"name": str(fn.name or ""), "arguments": arguments})
    legacy = getattr(message, "function_call", None)
    if not calls and legacy is not None:
        try:
            arguments = json.loads(getattr(legacy, "arguments", "") or "{}")
        except (TypeError, ValueError):
            arguments = {}
        calls.append({"name": str(getattr(legacy, "name", "") or ""),
                      "arguments": arguments if isinstance(arguments, dict) else {}})
    return text, calls


#: 服务商不支持 function calling 时的兜底话术：让它把工具调用写成 JSON
_TOOL_JSON_HINT = """\
你现在没有可用的工具接口。请**只输出一段 JSON**（不要 markdown 围栏、不要解释），形状：

{"tool": "ask_user_question", "arguments": {"intro": "…", "questions": [{"id": "…", "header": "…", "question": "…", "options": [{"label": "…", "description": "…"}], "multiple": false}]}}

或者

{"tool": "confirm_course_requirements", "arguments": {"reply": "…", "requirements": {"course_title": "…", "subject": "…", "stage": "高中", "goal": "…", "student_level": "基础", "prerequisite": "…", "weeks": 6, "hours_per_week": 3, "key_topics": ["…"], "summary": "…"}}}

规则不变：每次必须选其中一个工具。"""

#: 这些状态码/关键词说明"服务商不认 tools"，值得退一步用 JSON 约定再问一次。
#: 反过来，502 / 连接失败 / 超时是**上游坏了** —— 换个问法也救不回来，只会让用户
#: 多等一整个超时（实测：本地 ollama 返回 502，页面在"发送中"挂了十几秒还没动静）。
_UNSUPPORTED_TOOL_HINTS = ("tool", "function", "unsupported", "not supported", "invalid_request")


def _worth_json_retry(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (400, 404, 405, 415, 422, 501):
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(hint in text for hint in _UNSUPPORTED_TOOL_HINTS) or status in (404, 405, 422, 501)
    return False


async def _ask_model(target: dict, messages: list[dict], *, temperature: float = 0.4,
                     max_tokens: int = 2000) -> tuple[str, list[dict]]:
    """带工具的对话；服务商不支持 tools 时退回"让它输出 JSON"。"""
    try:
        return await asyncio.wait_for(
            _raw_chat(target, messages, tools=_TOOLS,
                      temperature=temperature, max_tokens=max_tokens),
            timeout=DIALOG_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"模型 {DIALOG_TIMEOUT:g} 秒没有响应") from exc
    except Exception as exc:                       # noqa: BLE001
        if not _worth_json_retry(exc):
            raise
        log.warning("课程生成：工具调用不可用（%s: %s），改用 JSON 约定", type(exc).__name__, exc)
    try:
        text, _ = await asyncio.wait_for(
            _raw_chat(target, [*messages, {"role": "system", "content": _TOOL_JSON_HINT}],
                      temperature=temperature, max_tokens=max_tokens),
            timeout=DIALOG_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"模型 {DIALOG_TIMEOUT:g} 秒没有响应") from exc
    data = _parse_json_loose(text)
    if isinstance(data, dict) and data.get("tool"):
        args = data.get("arguments")
        return str(data.get("reply") or data.get("intro") or ""), [
            {"name": str(data["tool"]), "arguments": args if isinstance(args, dict) else {}}]
    return text, []


async def _ask_json(target: dict, messages: list[dict], *, max_tokens: int = 6000,
                    temperature: float = 0.3) -> dict:
    """要一段 JSON（大纲 / 单元设计）。解析不出就抛。"""
    client = _client(target)
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=target["model"], messages=messages, temperature=temperature,
                max_tokens=max_tokens, response_format={"type": "json_object"}),
            timeout=JSON_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"模型 {JSON_TIMEOUT:g} 秒没有响应（大纲/单元设计）") from exc
    text = (resp.choices[0].message.content or "").strip()
    data = _parse_json_loose(text)
    if not isinstance(data, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return data


def _parse_json_loose(text: str):
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        pass
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        with contextlib.suppress(TypeError, ValueError):
            return json.loads(fenced.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        with contextlib.suppress(TypeError, ValueError):
            return json.loads(raw[start:end + 1])
    return None


# ===========================================================================
# 工具定义（模型提问 / 请求确认）
# ===========================================================================

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": (
                "向用户提问，用来弄清楚学习目标细节：想学到什么程度、现在有什么基础、"
                "预计学习周期、有没有想重点覆盖或跳过的内容。一次最多问 3 个问题，"
                "每个问题都要给出可直接点选的选项。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intro": {"type": "string", "description": "对用户上一句话的简短回应（1~2 句）"},
                    "questions": {
                        "type": "array",
                        "description": "要问的问题（1~3 个）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "短标识，如 depth / foundation / period"},
                                "header": {"type": "string", "description": "问题分组标题，如「想学到什么程度」"},
                                "question": {"type": "string", "description": "问题正文"},
                                "multiple": {"type": "boolean", "description": "是否允许多选"},
                                "options": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string", "description": "选项文字"},
                                            "description": {"type": "string", "description": "一句话解释"},
                                        },
                                        "required": ["label"],
                                    },
                                },
                            },
                            "required": ["id", "question"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_course_requirements",
            "description": (
                "当你已经充分了解用户想学什么（主题、程度、基础、周期、重点）之后调用："
                "把整理好的课程需求摘要展示给用户，请用户确认是否符合他的想法。"
                "用户确认后系统才会生成课程大纲。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reply": {"type": "string", "description": "一两句话，说明这是整理结果"},
                    "requirements": {
                        "type": "object",
                        "properties": {
                            "course_title": {"type": "string", "description": "课程名，简洁具体"},
                            "subject": {"type": "string", "description": "学科 / 领域，如「信息安全」「离散数学」"},
                            "stage": {"type": "string", "enum": list(course_plan.STAGES)},
                            "goal": {"type": "string", "description": "学完之后能做到什么（一段话）"},
                            "student_level": {"type": "string", "enum": list(course_plan.LEVELS)},
                            "prerequisite": {"type": "string", "description": "用户目前已有的基础"},
                            "weeks": {"type": "integer", "description": "预计总周期（周）"},
                            "hours_per_week": {"type": "integer", "description": "每周可投入课时"},
                            "key_topics": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "用户明确点出要覆盖的重点内容",
                            },
                            "summary": {"type": "string", "description": "两三句话的课程说明"},
                        },
                        "required": ["course_title", "subject", "goal"],
                    },
                },
                "required": ["requirements"],
            },
        },
    },
]

_SYSTEM_PROMPT = """\
你是一位经验丰富的课程规划师，正在和用户一对一沟通，准备为他/她排一门课。
你的目标不是马上给出一份课表，而是**先真正弄清楚他想学什么**。

## 你必须弄清楚的四件事
1. 想学到什么程度（了解 / 会做题 / 能独立应用 ……）
2. 现在有什么基础（零基础 / 学过一点 / 有基础想提高）
3. 预计投入多长时间（总共几周、每周几小时）
4. 有没有特别想覆盖或特别想跳过的内容

## 工作方式
- 每轮最多问 3 个问题，问题要具体、可直接点选；不要一次把所有问题倒出来。
- 用户的回答可能很简短（例如"有基础"、"两个月"），你要把它落到上面四件事里。
- 用户也可能主动提出别的想法（"我不想学证明"、"重点是考试"），要据此调整理解。
- 当四件事基本清楚时，调用 confirm_course_requirements 把整理结果摊给用户确认。
- 用户说"不符合 / 再改改"时，回到提问，只把不清楚的那一项问细。

## 硬性要求
- **每一次回复都必须调用工具**（ask_user_question 或 confirm_course_requirements），
  不允许只回一段话。
- 不要替用户编造他的基础与周期；不确定就问。
- 语气自然、简短，像一位耐心的老师；不要写小标题、不要用 markdown 列表。
- 一律用中文。
"""


def _dialog_messages(session: dict) -> list[dict]:
    """会话 → 模型消息序列（把工具调用与回答翻译成可读对话）。"""
    out: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages = (session.get("messages") or [])[-30:]
    for item in messages:
        role = item.get("role")
        text = str(item.get("text") or "").strip()
        tool = item.get("tool") if isinstance(item.get("tool"), dict) else None
        if role == "user":
            out.append({"role": "user", "content": text or "（用户没有输入文字）"})
            continue
        if role != "assistant":
            continue
        if tool is None:
            out.append({"role": "assistant", "content": text or "（继续）"})
            continue
        if tool.get("name") == "ask_user_question":
            questions = tool.get("questions") or []
            flat = "；".join(
                f"{q.get('header') or ''}：{q.get('question') or ''}"
                + (f"（选项：{'/'.join(o.get('label', '') for o in q.get('options') or [])}）"
                   if q.get("options") else "")
                for q in questions if isinstance(q, dict))
            out.append({"role": "assistant", "content": f"【向用户提问】{flat}"})
            if tool.get("answered"):
                out.append({"role": "user", "content": f"（回答）{tool.get('answer') or ''}"})
            continue
        if tool.get("name") == "confirm_course_requirements":
            req = tool.get("requirements") or {}
            brief = (f"课程名：{req.get('course_title')}；学科：{req.get('subject')}；"
                     f"学段：{req.get('stage')}；水平：{req.get('student_level')}；"
                     f"周期：{req.get('weeks')} 周 × {req.get('hours_per_week')} 课时；"
                     f"目标：{req.get('goal')}")
            out.append({"role": "assistant", "content": f"【请用户确认】{brief}"})
            decision = tool.get("decision")
            if decision == "accepted":
                out.append({"role": "user", "content": "（用户已确认，符合我的想法）"})
            elif decision == "rejected":
                out.append({"role": "user",
                            "content": f"（用户认为不符合，需要继续细化）{tool.get('feedback') or ''}"})
    return out


# ===========================================================================
# 规则兜底（没有模型时）
# ===========================================================================

def _user_texts(messages: list[dict]) -> list[str]:
    return [str(m.get("text") or "") for m in messages if m.get("role") == "user"]


def _slot_answers(messages: list[dict]) -> dict[str, str]:
    """ask 工具 → 那一次问答的答案。

    答案有两种存法，都要认：
      · 工具已标记 answered（用户回答之后我们把它写进去了）；
      · 工具还没标记，但紧随其后就是一条用户消息（离线判定时看到的是这种中间态）。
    """
    out: dict[str, str] = {}
    for index, item in enumerate(messages):
        tool = item.get("tool") if isinstance(item.get("tool"), dict) else None
        if not tool or tool.get("name") != "ask_user_question":
            continue
        ids = [str((q or {}).get("id") or "") for q in tool.get("questions") or []]
        ids = [qid for qid in ids if qid]
        text = str(tool.get("answer") or "").strip()
        if not text and not tool.get("answered"):
            for nxt in messages[index + 1:]:
                if nxt.get("role") == "user":
                    text = str(nxt.get("text") or "").strip()
                    break
                if isinstance(nxt.get("tool"), dict):
                    break
        if text:
            for qid in ids:
                out[qid] = text
    return out


def _title_from(text: str) -> str:
    s = re.sub(r"^[\s，。！？、]*(?:我想|我想要|我要|帮我|请|想|准备|打算)?\s*"
               r"(?:学习|学|了解|系统学|系统学习|掌握|补|复习|突击|快速学)?\s*", "", text or "")
    s = s.strip(" 　，。！？、~～")
    s = re.sub(r"^(一下|一些|一点)", "", s).strip()
    return (s or "我的课程")[:30]


def _offline_turn(row: dict) -> None:
    """没有模型（或模型挂了）时的对话推进：按固定顺序问，问够了就请用户确认。"""
    messages: list[dict] = _loads(row.get("messages_json"), [])
    try:
        _offline_turn_inner(row, messages)
    finally:
        row["messages_json"] = _dumps(messages)


def _offline_turn_inner(row: dict, messages: list[dict]) -> None:
    answers = _slot_answers(messages)
    user_texts = _user_texts(messages)
    seed = " ".join(user_texts)
    req = course_plan.normalize_requirements({
        "course_title": _title_from(user_texts[0] if user_texts else ""),
        "weeks": course_plan.infer_weeks(seed),
        "stage": course_plan.infer_stage(seed),
        "student_level": course_plan.infer_level(seed),
        "goal": "",
        "key_topics": [],
    }, seed)

    pending = course_plan.offline_questions(req, set(answers))
    if len(answers) < 2 and pending:
        questions = pending[:1]
        intro = ("好的，我先把几个关键问题确认一下——" if not answers
                 else "收到，再确认一个点就好。")
        _push(messages, "assistant", intro, tool={
            "name": "ask_user_question", "call_id": f"ask-{uuid.uuid4().hex[:8]}",
            "intro": intro, "questions": questions, "answered": False, "answer": "",
        })
        row["phase"] = "clarifying"
        return

    # 够了：把问答折成需求，请用户确认
    depth = answers.get("depth") or answers.get("foundation") or ""
    if "零基础" in depth:
        req["student_level"] = "入门"
    elif "有基础" in depth or "进阶" in depth:
        req["student_level"] = "进阶"
    if "2 周" in (answers.get("period") or ""):
        req["weeks"] = 2
    elif "1 个月" in (answers.get("period") or ""):
        req["weeks"] = 4
    focus = answers.get("focus") or ""
    if focus and "按标准课程顺序" not in focus:
        req["key_topics"] = [part.strip() for part in re.split(r"[、,，;；/]", focus) if part.strip()][:6]
    if not req.get("goal"):
        req["goal"] = (f"系统掌握《{req['course_title']}》的核心内容，"
                       f"达到「{_level_phrase(req['student_level'])}」的水平，并能独立完成相关练习。")

    intro = "我把你的需求整理成下面这份，看看是不是你的意思："
    _push(messages, "assistant", intro, tool={
        "name": "confirm_course_requirements", "call_id": f"confirm-{uuid.uuid4().hex[:8]}",
        "reply": intro, "requirements": req, "decision": "", "feedback": "",
    })
    row["requirements_json"] = _dumps(req)
    row["phase"] = "confirming_requirements"
    if not row.get("title"):
        row["title"] = req["course_title"]


def _level_phrase(level: str) -> str:
    return {"入门": "听得懂、说得出基本概念",
            "基础": "概念清楚、常见题会做",
            "中等": "能独立完成常规题与部分综合题",
            "进阶": "能处理综合问题并解释给别人听",
            "竞赛": "能应对竞赛难度的变式题"}.get(level, "掌握核心内容")


# ===========================================================================
# 建会话 / 推进对话
# ===========================================================================

async def create_session(conn, first_message: str) -> dict:
    text = (first_message or "").strip()
    if not text:
        raise BuilderError("先说一句你想学什么（例如「我想学习信息安全」）")
    session_id = f"cb-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO course_builder_sessions(id, title, phase, messages_json,"
        " requirements_json, outline_json, outline_path, run_dir, course_id,"
        " unit_designs_count, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, _title_from(text), "clarifying", "[]", "", "", "", "", "", 0,
         _now(), _now()))
    conn.commit()
    log.info("课程生成 新建会话 id=%s 首句长度=%s", session_id, len(text))
    return await send_message(conn, session_id, text)


async def send_message(conn, session_id: str, text: str) -> dict:
    """用户发一句话 → 标记上一次提问已被回答 → 跑一轮模型 → 落库。"""
    row = _get_row(conn, session_id)
    if row["phase"] in _ACTIVE_PHASES:
        raise BuilderError("正在生成课程，等这一步结束再继续对话", "conflict")
    session = _row_to_session(row)
    messages: list[dict] = session["messages"]
    content = (text or "").strip()
    if not content:
        raise BuilderError("消息不能为空")
    _mark_answered(messages, content)
    _push(messages, "user", content)
    row["messages_json"] = _dumps(messages)
    row["task_error"] = ""

    target = _model_target(conn)
    if target is None:
        _offline_turn(row)
    else:
        try:
            reply, calls = await _ask_model(target, _dialog_messages(_row_to_session(row)))
            _apply_tool_reply(row, reply, calls)
        except Exception as exc:                   # noqa: BLE001 —— 模型挂了不等于课堂挂了
            log.warning("课程生成 模型调用失败 id=%s err=%s: %s",
                        session_id, type(exc).__name__, exc)
            # 失败必须**说出来**：用户看到的是"模型在提问"，而实际上这一轮是规则兜底。
            # 不说的话，页面会一直显示"已就绪的模型"，而内容明显不像模型问的。
            # 只说一次：每轮都插一条同样的说明，会把对话刷成一堵墙。
            if not any(m.get("kind") == "notice" for m in messages):
                _push(messages, "assistant",
                      f"模型这次没有响应（{type(exc).__name__}）。我先按规则帮你把需求理清楚——"
                      f"配好模型后可以重新开始一次对话，那边的追问会更贴合你的情况。",
                      kind="notice")
            row["messages_json"] = _dumps(messages)
            _offline_turn(row)
            _append_task_note(row, f"模型调用失败（{type(exc).__name__}），已用规则继续")

    _save_row(conn, row)
    return _view(row)


def _append_task_note(row: dict, note: str) -> None:
    row["task_error"] = (row.get("task_error") or "")[:400] or note


def _apply_tool_reply(row: dict, reply: str, calls: list[dict]) -> None:
    """把模型这一轮的输出落成一条（或两条）消息。"""
    messages: list[dict] = _loads(row.get("messages_json"), [])
    try:
        _apply_tool_reply_inner(row, messages, reply, calls)
    finally:
        row["messages_json"] = _dumps(messages)


def _apply_tool_reply_inner(row: dict, messages: list[dict], reply: str,
                            calls: list[dict]) -> None:
    ask = next((c for c in calls if c.get("name") == "ask_user_question"), None)
    confirm = next((c for c in calls if c.get("name") == "confirm_course_requirements"), None)

    if ask is not None:
        args = ask.get("arguments") or {}
        questions = _normalize_questions(args.get("questions"))
        if not questions:
            questions = _normalize_questions([{
                "id": "detail", "header": "再明确一点",
                "question": "能再具体说说你想学到什么程度吗？",
                "options": [{"label": "了解即可"}, {"label": "会做题"}, {"label": "能独立应用"}],
            }])
        intro = str(args.get("intro") or reply or "").strip()
        _push(messages, "assistant", intro or "我想先确认几个问题：", tool={
            "name": "ask_user_question", "call_id": f"ask-{uuid.uuid4().hex[:8]}",
            "intro": intro, "questions": questions, "answered": False, "answer": "",
        })
        row["phase"] = "clarifying"
        return

    if confirm is not None:
        args = confirm.get("arguments") or {}
        requirements = course_plan.normalize_requirements(
            args.get("requirements"), " ".join(_user_texts(messages)))
        text = str(args.get("reply") or reply or "").strip()
        _push(messages, "assistant", text or "我把你的需求整理成下面这份，看看是不是你的意思：",
              tool={
                  "name": "confirm_course_requirements",
                  "call_id": f"confirm-{uuid.uuid4().hex[:8]}",
                  "reply": text, "requirements": requirements,
                  "decision": "", "feedback": "",
              })
        row["requirements_json"] = _dumps(requirements)
        row["phase"] = "confirming_requirements"
        if not row.get("title"):
            row["title"] = requirements["course_title"]
        return

    # 模型没调工具：如实展示它说的话，并把球抛回给用户
    _push(messages, "assistant", reply or "我们继续——你想学的是哪一块，想到什么程度？")
    row["phase"] = "clarifying"


def _normalize_questions(raw: Any) -> list[dict]:
    """把模型给的提问压成前端能直接渲染的卡片（最多 3 个，每个最多 6 个选项）。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        options: list[dict] = []
        for option in (item.get("options") or [])[:6]:
            if isinstance(option, dict):
                label = str(option.get("label") or "").strip()
                description = str(option.get("description") or "").strip()
            else:
                label, description = str(option).strip(), ""
            if label:
                options.append({"label": label[:60], "description": description[:120]})
        out.append({
            "id": str(item.get("id") or f"q{len(out) + 1}")[:24],
            "header": str(item.get("header") or "")[:40],
            "question": question[:200],
            "multiple": bool(item.get("multiple")),
            "options": options,
        })
    return out


# ===========================================================================
# 确认需求 → 生成大纲（后台任务）
# ===========================================================================

async def confirm_requirements(conn, session_id: str, *, accepted: bool,
                               requirements: dict | None = None,
                               feedback: str = "") -> dict:
    row = _get_row(conn, session_id)
    markers: list[dict] = _loads(row["messages_json"], [])
    tool = _pending_tool(markers)
    if tool is None or tool.get("name") != "confirm_course_requirements":
        raise BuilderError("现在没有待确认的课程需求：先和模型把想学什么聊清楚", "conflict")

    if requirements and isinstance(requirements, dict):
        base = _loads(row["requirements_json"], {}) or {}
        req = course_plan.normalize_requirements({**base, **requirements},
                                                 " ".join(_user_texts(markers)))
    else:
        req = _loads(row["requirements_json"], None) or \
            course_plan.normalize_requirements(tool.get("requirements"), "")

    tool["decision"] = "accepted" if accepted else "rejected"
    if not accepted:
        tool["feedback"] = (feedback or "").strip()[:400]
        _push(markers, "user", feedback.strip() or "不太对，我们再细化一下")
        row["messages_json"] = _dumps(markers)
        row["phase"] = "clarifying"
        _save_row(conn, row)
        return _view(row)

    row["messages_json"] = _dumps(markers)
    row["requirements_json"] = _dumps(req)
    row["title"] = req["course_title"]
    row["phase"] = "generating_outline"
    row["task_kind"] = "outline"
    row["task_status"] = "running"
    row["task_stage"] = SESSION_TASK_STAGES["outline"][1]
    row["task_percent"] = 5.0
    row["task_error"] = ""
    row["task_log"] = _dumps([f"开始生成大纲：{req['course_title']}（{req['weeks']} 周）"])
    _save_row(conn, row)
    _start_task(session_id, _job_outline(session_id))
    return _view(row)


def _start_task(key: str, coro) -> None:
    previous = _TASKS.get(key)
    if previous is not None and not previous.done():
        raise BuilderError("上一步还没结束", "conflict")
    task = asyncio.get_running_loop().create_task(coro)
    _TASKS[key] = task
    task.add_done_callback(lambda t, k=key: _TASKS.pop(k, None))


def _task_log(row: dict, line: str) -> None:
    log_tail = _loads(row.get("task_log"), [])
    if not isinstance(log_tail, list):
        log_tail = []
    log_tail.append(line)
    while len(log_tail) > LOG_TAIL_KEEP:
        del log_tail[0]
    row["task_log"] = _dumps(log_tail)
    live = _LIVE.get(str(row.get("id") or ""))
    if live is not None:
        live["log"] = list(log_tail)


def _set_task(row: dict, *, status: str | None = None, stage: str | None = None,
              percent: float | None = None, error: str | None = None,
              kind: str | None = None) -> None:
    if status is not None:
        row["task_status"] = status
    if stage is not None:
        row["task_stage"] = stage
    if percent is not None:
        row["task_percent"] = round(float(percent), 1)
    if error is not None:
        row["task_error"] = error
    if kind is not None:
        row["task_kind"] = kind
    live = _LIVE.get(str(row.get("id") or ""))
    if live is not None:
        live.update({"kind": row.get("task_kind") or "", "status": row.get("task_status") or "",
                     "stage": row.get("task_stage") or "",
                     "percent": float(row.get("task_percent") or 0.0),
                     "error": row.get("task_error") or "",
                     "log": _loads(row.get("task_log"), [])})


async def _job_outline(session_id: str) -> None:
    """生成课程大纲：一次模型调用 → 规整 → 落盘 → 等用户编辑。"""
    conn = _connect()
    try:
        row = _get_row(conn, session_id)
        _LIVE[session_id] = {"kind": "outline", "status": "running", "stage": "生成大纲",
                             "percent": 10.0, "error": "", "log": []}
        req = _loads(row["requirements_json"], {}) or {}
        target = _model_target(conn)
        outline = None
        if target is not None:
            try:
                data = await _ask_json(target, _outline_messages(req), max_tokens=6000)
                outline = course_plan.normalize_outline(data, req)
                _task_log(row, f"模型排出 {outline['chapter_count']} 章 / {outline['unit_count']} 单元")
            except Exception as exc:               # noqa: BLE001
                log.warning("课程生成：大纲失败 id=%s err=%s: %s", session_id, type(exc).__name__, exc)
                _task_log(row, f"模型生成大纲失败（{type(exc).__name__}），改用规则排课")
        if outline is None:
            outline = course_plan.offline_outline(req)
            _task_log(row, "已用规则排出大纲（可在下一步手动调整）")

        _set_task(row, stage="整理章单元", percent=80.0)
        row["outline_json"] = _dumps(outline)
        row["phase"] = "editing_outline"
        row["title"] = outline.get("course_title") or row.get("title") or ""
        messages: list[dict] = _loads(row["messages_json"], [])
        _push(messages, "assistant",
              f"大纲排好了：{outline['chapter_count']} 章、{outline['unit_count']} 个单元，"
              f"约 {course_plan.outline_total_hours(outline):g} 课时。"
              f"你可以直接改标题、增删单元，确认没问题后再开始编排每一课。",
              kind="outline")
        row["messages_json"] = _dumps(messages)
        _set_task(row, status="succeeded", stage="大纲已生成", percent=100.0, error="")
        _task_log(row, "大纲已生成，等待用户确认")
        _save_row(conn, row)
        log.info("课程生成 大纲完成 id=%s chapters=%s units=%s",
                 session_id, outline["chapter_count"], outline["unit_count"])
    except Exception as exc:                       # noqa: BLE001
        log.exception("课程生成 大纲任务异常 id=%s", session_id)
        with contextlib.suppress(Exception):
            row = _get_row(conn, session_id)
            row["phase"] = "confirming_requirements"
            _set_task(row, status="failed", error=f"生成大纲失败：{type(exc).__name__}: {exc}")
            _save_row(conn, row)
    finally:
        _LIVE.pop(session_id, None)
        conn.close()


def _outline_messages(req: dict) -> list[dict]:
    """排大纲的提示词：只要结构化结果，章/单元粒度和截图一致（章 → 单元）。"""
    system = """\
你是一位课程设计专家。请根据用户确认过的学习需求，排出一份**章 → 单元**两级结构的课程大纲。

要求：
1. 章是主题块（3~6 个单元），单元是一次课（1~2 课时能讲完）。
2. 单元标题要具体到"这一节课讲什么"，不要写"第一章内容"这种空话。
3. 每个单元给 2~5 个知识点（knowledge_points），它们是后面出 PPT 的知识清单。
4. 章的划分要符合学科自身的逻辑顺序（先基础后综合，先概念后方法）。
5. 章数控制在 2~6 章，每章 2~5 个单元；总课时要贴近"周期 × 每周课时"。
6. 只输出 JSON，不要解释、不要 markdown 围栏。

JSON 形状：
{"course_title": "…", "subject": "…", "stage": "高中", "goal": "…",
 "student_level": "基础", "summary": "两三句话的课程说明",
 "weeks": 6, "hours_per_week": 3,
 "chapters": [
   {"title": "章标题", "summary": "这一章解决什么问题",
    "units": [
      {"title": "单元标题", "type": "新授|巩固|复习", "hours": 1.0,
       "summary": "这一节课的一句话说明",
       "knowledge_points": ["知识点1", "知识点2"]}
    ]}
 ]}
"""
    user = [
        f"课程名：{req.get('course_title')}",
        f"学科/领域：{req.get('subject')}",
        f"学段：{req.get('stage')}",
        f"学习者水平：{req.get('student_level')}",
        f"已有基础：{req.get('prerequisite') or '未特别说明'}",
        f"学习目标：{req.get('goal')}",
        f"周期：{req.get('weeks')} 周 × 每周 {req.get('hours_per_week')} 课时",
        f"重点内容：{'、'.join(req.get('key_topics') or []) or '未特别指定'}",
        "",
        "请排出课程大纲（JSON）。",
    ]
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user)}]


# ===========================================================================
# 大纲编辑 / 确认
# ===========================================================================

def save_outline(conn, session_id: str, outline: Any) -> dict:
    """保存用户编辑过的大纲（规整后再存，非法结构直接 400 而不是悄悄丢掉）。"""
    row = _get_row(conn, session_id)
    if row["phase"] in _ACTIVE_PHASES:
        raise BuilderError("正在生成课程，等这一步结束再改大纲", "conflict")
    problems = course_plan.validate_outline(outline)
    if problems:
        raise BuilderError("大纲还不能用：" + "；".join(problems[:4]))
    req = _loads(row["requirements_json"], {}) or {}
    normalized = course_plan.normalize_outline(outline, req)
    row["outline_json"] = _dumps(normalized)
    if row["phase"] == "editing_outline":
        row["phase"] = "editing_outline"
    _save_row(conn, row)
    return _view(row)


async def start_unit_designs(conn, session_id: str, outline: Any | None = None) -> dict:
    """用户确认大纲 → 后台生成每个单元的课程安排设计，然后存入课程库。"""
    row = _get_row(conn, session_id)
    if row["phase"] in _ACTIVE_PHASES:
        raise BuilderError("正在生成课程，等这一步结束再继续", "conflict")
    if outline is not None:
        problems = course_plan.validate_outline(outline)
        if problems:
            raise BuilderError("大纲还不能用：" + "；".join(problems[:4]))
        req = _loads(row["requirements_json"], {}) or {}
        row["outline_json"] = _dumps(course_plan.normalize_outline(outline, req))
        _save_row(conn, row)

    current = _loads(row["outline_json"], None)
    if not current:
        raise BuilderError("还没有生成课程大纲：先生成再确认", "conflict")

    row["phase"] = "generating_course"
    row["task_kind"] = "design"
    row["task_status"] = "running"
    row["task_stage"] = SESSION_TASK_STAGES["design"][1]
    row["task_percent"] = 2.0
    row["task_error"] = ""
    row["task_log"] = _dumps([f"开始编排 {current['unit_count']} 个单元的课程设计"])
    _save_row(conn, row)
    _start_task(session_id, _job_designs(session_id))
    return _view(row)


async def _job_designs(session_id: str) -> None:
    """逐**单元**生成课程安排设计 → 写盘 → 入库。单课失败只降级该课，不推翻整门课。

    为什么是"一次一课"而不是"一次一章"：一章 2~3 个单元的设计 JSON 很容易超过模型
    单次输出上限（实测 deepseek 写到第 2 个单元的练习就被截断，整段 JSON 因此不可解析，
    那一章全部退化成规则兜底）。拆到单课之后，输出规模可控、失败半径只有一课，
    进度也细得能看出"第 5/16 单元"在动。
    """
    conn = _connect()
    try:
        row = _get_row(conn, session_id)
        _LIVE[session_id] = {"kind": "design", "status": "running",
                             "stage": "生成单元设计", "percent": 3.0,
                             "error": "", "log": []}
        outline = _loads(row["outline_json"], {}) or {}
        chapters = outline.get("chapters") or []
        flat = course_plan.flatten_outline(outline)
        total = max(1, len(flat))
        target = _model_target(conn)
        designs: dict[str, dict] = {}
        model_alive = target is not None
        index = 0
        for chapter in chapters:
            for unit in chapter.get("units") or []:
                stage_label = f"第 {index + 1}/{total} 课「{unit.get('title')}」"
                _set_task(row, stage=stage_label, percent=3 + 92 * index / total)
                _task_log(row, f"编排 {stage_label}")
                raw = None
                if model_alive:
                    try:
                        data = await _ask_json(
                            target,
                            _design_messages(outline, chapter, unit, flat, index),
                            max_tokens=4000)
                        raw = _pick_design(data, unit)
                    except Exception as exc:       # noqa: BLE001
                        log.warning("课程生成：单元设计失败 unit=%s err=%s: %s",
                                    unit.get("id"), type(exc).__name__, exc)
                        _task_log(row, f"模型失败（{type(exc).__name__}），本课改用规则兜底")
                        # 一家挂了不会只挂一次：后面每一课都去等同一个超时，用户看到的
                        # 是一条几乎不动的进度条。所以第一次失败之后本轮全部走规则兜底。
                        model_alive = False
                        _task_log(row, "后续单元不再尝试模型，直接用规则兜底")
                if isinstance(raw, dict):
                    designs[unit["id"]] = course_plan.normalize_design(
                        raw, unit, chapter, outline)
                else:
                    designs[unit["id"]] = course_plan.offline_design(
                        unit, chapter, outline,
                        prev_title=_prev_unit_title(flat, unit["id"]),
                        next_title=_next_unit_title(flat, unit["id"]))
                index += 1

        _set_task(row, stage="写入课程库", percent=97.0)
        course_id, run_dir, count = _persist_course(conn, row, outline, designs)
        row["course_id"] = course_id
        row["run_dir"] = str(run_dir)
        row["outline_path"] = str(Path(run_dir) / "outline.json")
        row["unit_designs_count"] = count
        row["phase"] = "ready"
        messages: list[dict] = _loads(row["messages_json"], [])
        _push(messages, "assistant",
              f"课程已经存进「我的课堂」：{outline.get('course_title') or '未命名课程'}，"
              f"共 {count} 个单元。PPT 按课生成——上到哪一课，就生成哪一课的 PPT。",
              kind="course_ready", course_id=course_id)
        row["messages_json"] = _dumps(messages)
        _set_task(row, status="succeeded", stage="已存入我的课堂", percent=100.0, error="")
        _task_log(row, f"已存入我的课堂：{count} 个单元")
        _save_row(conn, row)
        log.info("课程生成 完成 id=%s course=%s units=%s", session_id, course_id, count)
    except Exception as exc:                       # noqa: BLE001
        log.exception("课程生成 单元设计任务异常 id=%s", session_id)
        with contextlib.suppress(Exception):
            row = _get_row(conn, session_id)
            row["phase"] = "editing_outline"
            _set_task(row, status="failed",
                      error=f"生成课程设计失败：{type(exc).__name__}: {exc}")
            _save_row(conn, row)
    finally:
        _LIVE.pop(session_id, None)
        conn.close()


def _next_unit_title(flat: list[dict], unit_id: str) -> str:
    for index, item in enumerate(flat):
        if item["unit_id"] == unit_id:
            return flat[index + 1]["unit_title"] if index + 1 < len(flat) else ""
    return ""


def _pick_design(data: Any, unit: dict) -> dict | None:
    """模型可能给 {units:[…]} / {designs:{uid:…}} / 直接一个单元设计 —— 都认。"""
    if not isinstance(data, dict):
        return None
    items = data.get("units") or data.get("designs") or data.get("unit_designs")
    unit_id = str(unit.get("id") or "")
    if isinstance(items, dict):
        candidate = items.get(unit_id)
        if isinstance(candidate, dict):
            return candidate
        first = next((v for v in items.values() if isinstance(v, dict)), None)
        return first
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("unit_id") or item.get("id") or "") in ("", unit_id):
                return item
        return next((item for item in items if isinstance(item, dict)), None)
    if "knowledge_points" in data or "slides" in data or "lesson_flow" in data:
        return data
    return None


def _design_messages(outline: dict, chapter: dict, unit: dict, flat: list[dict],
                     index: int) -> list[dict]:
    """单课备课设计的提示词：本课知识点 + 与前面内容的衔接 + 份量按课时。

    输出规模是刻意压住的（每点解释 ≤ 80 字、练习 4~6 题、8~14 页）：一次只要一课，
    模型就不会写到手滑截断 —— 截断的 JSON 等于这一课白跑。
    """
    done = []
    for item in flat[:index]:
        done.append(f"- {item['chapter_title']} / {item['unit_title']}"
                    f"（{'、'.join(item.get('knowledge_points') or []) or '—'}）")
    if len(done) > 10:
        done = done[-10:]
    next_title = flat[index + 1]["unit_title"] if index + 1 < len(flat) else ""
    prev_title = flat[index - 1]["unit_title"] if index > 0 else ""

    system = """\
你是一位一线教师，正在为**一个单元**写备课设计（学生看不到这份内容，它只用来出课件）。

要求：
1. 知识点落到可以讲、可以考的程度，每个知识点解释不超过 80 字。
2. 写清这一课与前面内容的衔接：用到哪些旧知识、和之前哪些练习相关（prerequisites）。
3. 课件页（slides）按"一页一个教学动作"排：目标 / 逐知识点 / 例题 / 小结，8~14 页，
   每页 points 不超过 4 条。
4. 练习（exercises）4~6 题，基础题与变式题各有，写清建议答案要点（answer ≤ 60 字）。
5. 只输出 JSON（一个对象，不要包 units 数组），不要解释、不要 markdown 围栏。

JSON 形状：
{"unit_id": "…", "teaching_goals": ["…"],
 "knowledge_points": [{"name": "…", "explain": "…", "difficulty": "基础"}],
 "prerequisites": ["本课要用到的旧知识"],
 "lesson_flow": [{"phase": "导入", "minutes": 5, "activity": "…"}],
 "examples": ["例题与解析（一条即可）"],
 "exercises": [{"q": "…", "answer": "…", "kind": "基础"}],
 "common_mistakes": ["…"],
 "summary": "这一课的一句话总结",
 "slides": [{"title": "页标题", "points": ["要点"]}]}
"""
    user = "\n".join([
        f"课程：{outline.get('course_title')}（{outline.get('subject')}·{outline.get('stage')}"
        f"·学生水平 {outline.get('student_level')}·{outline.get('weeks')} 周 × "
        f"{outline.get('hours_per_week')} 课时/周）",
        f"课程目标：{outline.get('goal')}",
        f"本章：第{chapter.get('number')}章 {chapter.get('title')}",
        f"本课（第 {index + 1} 课，unit_id={unit['id']}）：{unit['title']}"
        f"｜{float(unit.get('hours') or 1):g} 课时｜{unit.get('type') or '新授'}",
        "大纲给的知识点：" + ("、".join(unit.get("knowledge_points") or []) or "（自行设计）"),
        f"上一课：{prev_title or '无'}｜下一课：{next_title or '无'}",
        "",
        "前面已学内容（用于衔接，不要重复讲）：",
        "\n".join(done) or "（本课是第一章第一节，没有前置单元）",
        "",
        "请只写这一课的设计（JSON）。",
    ])
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# ===========================================================================
# 落盘 + 入库
# ===========================================================================

def _stable_id(*parts: str) -> str:
    """与 studio/catalog.py::_stable_id 逐字节同口径。

    必须一致：课程存进库之后，用户点「重新扫描」时 catalog 会用这段算法重算 id；
    算法不同就会把同一门课再插一遍，学生的进度跟着丢。
    """
    import hashlib

    raw = "/".join(str(p).replace("\\", "/").strip("/") for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _unit_filename(unit: dict) -> str:
    """与 core/naming.py::unit_label 同口径：``第M单元-单元名.pptx``。"""
    no = int(unit.get("number") or 1)
    return f"第{no}单元-{course_plan._clean_name(unit.get('title'), '未命名单元')}.pptx"


def _chapter_dirname(chapter: dict) -> str:
    no = int(chapter.get("number") or 1)
    title = course_plan._clean_name(chapter.get("title"), "")
    return f"第{no}章-{title}" if title else f"第{no}章"


def _new_run_dir(title: str) -> Path:
    now = datetime.now()
    base = _runs_root() / f"{now:%Y%m%d}"
    stem = f"{_safe_name(title, 40)}_{now:%H%M%S}"
    path = base / stem
    n = 2
    while path.exists():
        path = base / f"{stem}-{n}"
        n += 1
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _persist_course(conn, row: dict, outline: dict, designs: dict[str, dict]) -> tuple[str, Path, int]:
    """大纲 + 全部单元设计 → 盘上的 run 目录 → 课程库（courses / units）。

    盘上的形状必须与 flow.py 的一次运行一致（00_spec / 01_blueprint / 02_targets /
    03_unit_*.result.md），因为「生成 PPT」那一步就是 `flow.py --resume <这里>`。
    """
    title = outline.get("course_title") or "未命名课程"
    run_dir = _new_run_dir(title)
    blueprint = course_plan.outline_to_blueprint(outline)
    flat = course_plan.flatten_outline(outline)
    chapters = outline.get("chapters") or []

    import yaml

    _write(run_dir / "outline.json", json.dumps(outline, ensure_ascii=False, indent=2))
    _write(run_dir / "outline.md", course_plan.render_outline_markdown(outline))
    _write(run_dir / "01_blueprint.yaml",
           yaml.safe_dump(blueprint, allow_unicode=True, sort_keys=False))
    _write(run_dir / "02_targets.json",
           json.dumps({"targets": course_plan.blueprint_targets(outline),
                       "notes": ["由课程大纲生成"]}, ensure_ascii=False, indent=2))
    _write(run_dir / "00_spec.json",
           json.dumps(_spec_payload(outline), ensure_ascii=False, indent=2))

    count = 0
    for index, chapter in enumerate(chapters):
        for unit in chapter.get("units") or []:
            design = designs.get(unit["id"])
            if design is None:
                design = course_plan.offline_design(unit, chapter, outline)
            _write(run_dir / f"unit_design_{unit['id']}.json",
                   json.dumps(design, ensure_ascii=False, indent=2))
            prev_title = _prev_unit_title(flat, unit["id"])
            next_title = _next_unit_title(flat, unit["id"])
            _write(run_dir / f"03_unit_{unit['id']}.result.md",
                   course_plan.render_unit_markdown(
                       design, chapter_index=index, prev_title=prev_title,
                       next_title=next_title))
            count += 1

    # 课程 id 必须与 catalog 的算法一致（见 _stable_id 的说明）
    runs_root = _runs_root()
    try:
        rel = run_dir.relative_to(runs_root.parent).as_posix()
    except ValueError:
        rel = run_dir.name
    course_id = _stable_id(rel)

    manifest = {
        "dir": str(run_dir),
        "input": title,
        "mode": "course_builder",
        "steps": {
            "outline": {"ok": True, "at": _now(),
                        "artifacts": ["outline.json", "outline.md", "01_blueprint.yaml"]},
            "unit_designs": {"ok": True, "at": _now(), "units": count},
        },
        "notes": [],
        "produced": [],
        "spec": {"subject": outline.get("subject") or "",
                 "stage": outline.get("stage") or "",
                 "goal": outline.get("goal") or "",
                 "weeks": outline.get("weeks"),
                 "hours_per_week": outline.get("hours_per_week")},
        "args": {"text": title},
    }
    _write(run_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    repo.upsert_course(conn, {
        "id": course_id,
        "title": title,
        "subject": outline.get("subject") or "",
        "source": "builder",
        "run_dir": str(run_dir),
        "source_path": str(run_dir),
    })
    order = 0
    for chapter in chapters:
        for unit in chapter.get("units") or []:
            unit_id = _stable_id(course_id, chapter.get("title") or "", _unit_filename(unit))
            repo.upsert_unit(conn, {
                "id": unit_id,
                "course_id": course_id,
                "chapter": chapter.get("title") or "",
                "title": unit.get("title") or "",
                "order_index": order,
                "pptx_path": "",
                "slides": 0,
                "est_sec": None,
            })
            order += 1
    return course_id, run_dir, count


def _prev_unit_title(flat: list[dict], unit_id: str) -> str:
    for index, item in enumerate(flat):
        if item["unit_id"] == unit_id:
            return flat[index - 1]["unit_title"] if index > 0 else ""
    return ""


def _spec_payload(outline: dict) -> dict:
    """``00_spec.json``：flow.py ``--resume`` 会用 ``scope.FlowSpec(**json)`` 读回来。

    字段必须与 FlowSpec 完全对齐（多一个都会 TypeError），所以这里是**显式列举**，
    不是 ``vars()`` 转发。
    """
    weeks = int(outline.get("weeks") or 6)
    return {
        "raw": outline.get("course_title") or "",
        "stage": outline.get("stage") if outline.get("stage") in course_plan.STAGES else "高中",
        "stage_known": True,
        "subject": outline.get("subject") or "通用",
        "subject_known": True,
        "level": outline.get("student_level") if outline.get("student_level") in course_plan.LEVELS else "基础",
        "module": outline.get("course_title") or "",
        "goal": outline.get("goal") or "",
        "weeks": weeks,
        "term": "短期" if weeks <= 3 else "长期",
        "term_source": "用户在对话中确认",
        "weeks_source": "用户在对话中确认",
        "hours_per_week": int(outline.get("hours_per_week") or 3),
        "hours_source": "用户在对话中确认",
        "pages": None,
        "units_per_run": 1,
        "focus": "",
        "work": "",
        "single_text": False,
        "effort": "",
        "intensity": "",
        "pages_source": "",
        "modules": [{"name": c.get("title") or "", "goal": c.get("summary") or ""}
                    for c in outline.get("chapters") or []],
        "source": "课程大纲",
        "notes": ["由交互式课程生成写入；PPT 逐单元按需生成"],
    }


# ===========================================================================
# 读 / 列表 / 删除
# ===========================================================================

def get_session(conn, session_id: str) -> dict:
    return _view(_get_row(conn, session_id))


def list_sessions(conn, limit: int = 20) -> dict:
    capped = max(1, min(int(limit or 20), 100))
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM course_builder_sessions"
        " ORDER BY updated_at DESC, rowid DESC LIMIT ?", (capped,))]
    items = []
    for row in rows:
        data = _view(row)
        items.append({
            "id": data["id"], "title": data["title"], "phase": data["phase"],
            "course_id": data["course_id"], "updated_at": data["updated_at"],
            "chapter_count": (data["outline"] or {}).get("chapter_count")
            if isinstance(data["outline"], dict) else None,
            "unit_count": (data["outline"] or {}).get("unit_count")
            if isinstance(data["outline"], dict) else None,
            "task": data["task"],
        })
    active = next((item for item in items
                   if item["phase"] in _ACTIVE_PHASES), None)
    return {"items": items, "active": active}


def delete_session(conn, session_id: str) -> dict:
    _get_row(conn, session_id)
    task = _TASKS.get(session_id)
    if task is not None and not task.done():
        task.cancel()
    _LIVE.pop(session_id, None)
    conn.execute("DELETE FROM course_builder_sessions WHERE id=?", (session_id,))
    conn.commit()
    return {"ok": True}


# ===========================================================================
# 渐进式 PPT：计划 / 发起 / 取消
# ===========================================================================

def _unit_has_ppt(unit: dict) -> bool:
    path = str(unit.get("pptx_path") or "")
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def ppt_plan(conn, course_id: str) -> dict:
    """这门课每个单元的 PPT 状态 + "下一课"是谁。

    "下一课"取**第一个还没有 PPT 的单元**（按 order_index）：这就是「顺序生成下一个
    单元的 PPT」的口径 —— 学生上到哪，就生成到哪。
    """
    course = repo.get_course(conn, course_id)
    if course is None:
        raise BuilderError("找不到这门课程", "not_found")
    units = repo.units_of_course(conn, course_id)
    jobs = {str(r["unit_id"]): dict(r) for r in conn.execute(
        "SELECT * FROM unit_ppt_jobs WHERE course_id=? ORDER BY created_at DESC, rowid DESC",
        (course_id,))}
    items: list[dict] = []
    next_unit_id = ""
    for unit in units:
        has_ppt = _unit_has_ppt(unit)
        job = jobs.get(unit["id"])
        status = ""
        if job is not None:
            status = str(job.get("status") or "")
        if not has_ppt and status in ("queued", "running"):
            status = status
        elif has_ppt:
            status = "succeeded"
        elif status in PPT_STATUSES:
            status = status
        else:
            status = ""
        if not has_ppt and not next_unit_id:
            next_unit_id = unit["id"]
        items.append({
            "unit_id": unit["id"],
            "chapter": unit["chapter"] or "",
            "title": unit["title"] or "",
            "order_index": int(unit["order_index"] or 0),
            "has_ppt": has_ppt,
            "slides": int(unit["slides"] or 0),
            "est_sec": round(float(unit["est_sec"] or 0.0), 1),
            "job_status": status,
            "job_id": str((job or {}).get("id") or ""),
            "job_percent": float((job or {}).get("percent") or 0.0),
            "job_stage": str((job or {}).get("stage_label") or ""),
            "error": str((job or {}).get("error") or ""),
        })
    return {
        "course_id": course_id,
        "title": course.get("title") or "",
        "total": len(items),
        "ready": sum(1 for item in items if item["has_ppt"]),
        "next_unit_id": next_unit_id,
        "items": items,
    }


def _active_ppt_job(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM unit_ppt_jobs WHERE status IN ('queued','running')"
        " ORDER BY created_at, rowid LIMIT 1").fetchone()
    return dict(row) if row is not None else None


async def start_next_ppt(conn, course_id: str) -> dict:
    """顺序生成：给这门课第一个还没有 PPT 的单元开一个任务。"""
    plan = ppt_plan(conn, course_id)
    if not plan["next_unit_id"]:
        raise BuilderError("这门课所有单元的 PPT 都已经生成了", "conflict")
    return await start_unit_ppt(conn, plan["next_unit_id"])


async def start_unit_ppt(conn, unit_id: str) -> dict:
    unit = repo.get_unit(conn, unit_id)
    if unit is None:
        raise BuilderError("找不到这个单元", "not_found")
    course = repo.get_course(conn, unit["course_id"] or "")
    if course is None:
        raise BuilderError("这个单元所属的课程不存在", "not_found")
    if _unit_has_ppt(unit):
        raise BuilderError("这个单元的 PPT 已经生成过了", "conflict")
    busy = _active_ppt_job(conn)
    if busy is not None:
        raise BuilderError(
            f"已经有一课的 PPT 在生成（「{busy.get('unit_title') or busy.get('unit_id')}」）："
            f"逐课生成是有意的——等它做完再点下一课", "conflict")

    default = modelconfig.get_default(conn)
    mode = "real" if (default and modelconfig_public_usable(default)) else "rule"
    job_id = f"up-{uuid.uuid4().hex[:12]}"
    snapshot = {
        "id": job_id,
        "course_id": unit["course_id"],
        "unit_id": unit_id,
        "unit_title": unit["title"] or "",
        "status": "queued",
        "mode": mode,
        "stage": "S4",
        "stage_label": "排队中",
        "percent": 0.0,
        "log_tail": [],
        "error": "",
        "created_at": _now(),
        "started_at": "",
        "finished_at": "",
        "elapsed_sec": 0.0,
        "pptx_path": "",
        "slides": 0,
    }
    conn.execute(
        "INSERT INTO unit_ppt_jobs(id, course_id, unit_id, unit_title, status, mode, stage,"
        " stage_label, percent, log_tail, pptx_path, slides, error, created_at,"
        " started_at, finished_at, elapsed_sec)"
        " VALUES(:id,:course_id,:unit_id,:unit_title,:status,:mode,:stage,:stage_label,"
        " :percent,:log_tail,:pptx_path,:slides,:error,:created_at,:started_at,"
        " :finished_at,:elapsed_sec)",
        {**snapshot, "log_tail": "[]"})
    conn.commit()
    job = _PptJob(snapshot=snapshot, course=course, unit=unit)
    _PPT_LIVE[job_id] = job
    job.task = asyncio.get_running_loop().create_task(_drive_ppt(job))
    log.info("PPT 排队 unit=%s course=%s mode=%s", unit_id, unit["course_id"], mode)
    return _ppt_view(snapshot)


def modelconfig_public_usable(default: dict | None) -> bool:
    if not default:
        return False
    provider = default.get("provider") or {}
    return bool(str(provider.get("api_key") or "").strip()) and bool(str(default.get("model") or ""))


def _ppt_view(snapshot: dict) -> dict:
    return {
        "id": snapshot["id"],
        "course_id": snapshot["course_id"],
        "unit_id": snapshot["unit_id"],
        "unit_title": snapshot["unit_title"],
        "status": snapshot["status"],
        "mode": snapshot["mode"],
        "stage": snapshot["stage"],
        "stage_label": snapshot["stage_label"],
        "percent": snapshot["percent"],
        "log_tail": list(snapshot.get("log_tail") or []),
        "error": snapshot["error"],
        "pptx_path": snapshot["pptx_path"],
        "slides": snapshot["slides"],
        "started_at": snapshot["started_at"],
        "finished_at": snapshot["finished_at"],
        "elapsed_sec": snapshot["elapsed_sec"],
    }


def get_unit_ppt(conn, unit_id: str) -> dict:
    """这个单元最近一次 PPT 任务（没有就返回 has_ppt 的现状）。"""
    row = conn.execute(
        "SELECT * FROM unit_ppt_jobs WHERE unit_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (unit_id,)).fetchone()
    unit = repo.get_unit(conn, unit_id)
    if unit is None:
        raise BuilderError("找不到这个单元", "not_found")
    if row is None:
        return {"unit_id": unit_id, "has_ppt": _unit_has_ppt(unit), "job": None,
                "slides": int(unit["slides"] or 0)}
    data = dict(row)
    live = _PPT_LIVE.get(str(data["id"]))
    if live is not None:
        data["status"] = live.snapshot["status"]
        data["stage"] = live.snapshot["stage"]
        data["stage_label"] = live.snapshot["stage_label"]
        data["percent"] = live.snapshot["percent"]
    return {
        "unit_id": unit_id,
        "has_ppt": _unit_has_ppt(unit),
        "slides": int(unit["slides"] or 0),
        "job": {
            "id": data["id"], "status": data["status"], "mode": data["mode"],
            "stage": data["stage"], "stage_label": data["stage_label"],
            "percent": float(data["percent"] or 0.0),
            "log_tail": _loads(data["log_tail"], []),
            "error": data["error"] or "",
            "pptx_path": data["pptx_path"] or "",
            "slides": int(data["slides"] or 0),
            "elapsed_sec": float(data["elapsed_sec"] or 0.0),
        },
    }


async def cancel_unit_ppt(conn, unit_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM unit_ppt_jobs WHERE unit_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (unit_id,)).fetchone()
    if row is None:
        raise BuilderError("这个单元还没有生成过 PPT", "not_found")
    data = dict(row)
    if data["status"] not in ("queued", "running"):
        raise BuilderError("这一课已经结束了，不能再取消", "conflict")
    job = _PPT_LIVE.get(str(data["id"]))
    if job is not None:
        job.snapshot["status"] = "cancelled"
        job.snapshot["error"] = "已取消"
        job.snapshot["finished_at"] = _now()
        job.finished = True
        await _kill_tree(job.proc)
        _ppt_save(conn, job.snapshot)
        _PPT_LIVE.pop(str(data["id"]), None)
    else:
        data["status"] = "cancelled"
        data["error"] = "已取消（本进程里没有对应进程）"
        data["finished_at"] = _now()
        _ppt_save_conn(conn, data)
    return get_unit_ppt(conn, unit_id)


# ===========================================================================
# 渐进式 PPT：跑
# ===========================================================================

@dataclass
class _PptJob:
    snapshot: dict
    course: dict
    unit: dict
    proc: Any = None
    started: float = 0.0
    finished: bool = False
    rank: int = -1
    task: Any = None
    last_output: float = 0.0


_LIVE: dict[str, dict] = {}
_PPT_LIVE: dict[str, _PptJob] = {}
_TASKS: dict[str, asyncio.Task] = {}

#: flow.py 的阶段标记（与 generation.STAGE_ORDER 同源，但只关心 S4/S5）
_PPT_STAGES = {"S4": (60.0, "设计课件大纲"), "S5": (90.0, "渲染 PPTX")}
_STAGE_LINE = re.compile(r"【(S[0-5])")
_PPTX_TOKEN = re.compile(r"([^\s\"'，。；]+\.pptx)")


def _ppt_log(snapshot: dict, line: str) -> None:
    tail = snapshot.setdefault("log_tail", [])
    tail.append(line)
    while len(tail) > LOG_TAIL_KEEP:
        del tail[0]


def _ppt_save(conn, snapshot: dict) -> None:
    _ppt_save_conn(conn, snapshot)


def _ppt_save_conn(conn, snapshot: dict) -> None:
    conn.execute(
        "UPDATE unit_ppt_jobs SET status=:status, stage=:stage, stage_label=:stage_label,"
        " percent=:percent, log_tail=:log_tail, pptx_path=:pptx_path, slides=:slides,"
        " error=:error, started_at=:started_at, finished_at=:finished_at,"
        " elapsed_sec=:elapsed_sec WHERE id=:id",
        {**snapshot,
         "log_tail": json.dumps(snapshot.get("log_tail") or [], ensure_ascii=False)})
    conn.commit()


async def _drive_ppt(job: _PptJob) -> None:
    """一课 PPT 的一生：先跑 flow.py --resume --unit；失败就本地合成一份。"""
    conn = _connect()
    try:
        snap = job.snapshot
        snap["status"] = "running"
        snap["started_at"] = _now()
        snap["stage_label"] = "准备出片"
        job.started = time.monotonic()
        _ppt_log(snap, f"开始为「{snap['unit_title']}」生成课件")
        _ppt_save(conn, snap)

        run_dir = Path(str(job.course.get("run_dir") or ""))
        produced: Path | None = None
        error = ""
        if run_dir.is_dir() and FLOW_SCRIPT.is_file():
            produced, error = await _run_flow_for_unit(job, conn, run_dir)
        else:
            error = f"课程目录不可用（{run_dir}）"

        if job.finished:                           # 取消路径已经结算过，别把 cancelled 改回来
            return

        if produced is None:
            # 兜底：不联网、不依赖 pipeline，按大纲直接合成一份能上的课件。
            # 产品上必须兜：没有 key 的机器、或者 pipeline 挂了，用户点"生成下一课"
            # 也该拿到课件，而不是一条红色错误。
            try:
                produced = _synthesize_pptx(job, run_dir)
                _ppt_log(snap, f"流水线未产出（{error or '未知原因'}），已用大纲直接合成课件")
            except Exception as exc:               # noqa: BLE001
                log.exception("PPT 兜底合成失败 unit=%s", snap["unit_id"])
                snap["finished_at"] = _now()
                _ppt_finish(conn, job, "failed",
                            error=f"生成失败：{error or f'{type(exc).__name__}: {exc}'}")
                return

        _finish_ppt_ok(conn, job, Path(produced))
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            _ppt_finish(conn, job, "cancelled", error="已取消")
        raise
    except Exception as exc:                       # noqa: BLE001
        log.exception("PPT 任务异常 unit=%s", job.snapshot.get("unit_id"))
        with contextlib.suppress(Exception):
            _ppt_finish(conn, job, "failed", error=f"内部错误：{type(exc).__name__}: {exc}")
    finally:
        _PPT_LIVE.pop(job.snapshot["id"], None)
        conn.close()


def _ppt_finish(conn, job: _PptJob, status: str, *, error: str = "") -> None:
    snap = job.snapshot
    if job.finished:                               # 只结算一次（取消路径先到）
        return
    job.finished = True
    snap["status"] = status
    snap["finished_at"] = _now()
    if job.started:
        snap["elapsed_sec"] = round(time.monotonic() - job.started, 1)
    snap["error"] = error
    if status == "succeeded":
        snap["percent"] = 100.0
    _ppt_save(conn, snap)
    log.info("PPT 结束 unit=%s status=%s error=%s", snap["unit_id"], status, error[:160])


async def _run_flow_for_unit(job: _PptJob, conn, run_dir: Path) -> tuple[Path | None, str]:
    """调 flow.py（S4 + S5），逐行把进度写进任务。返回 (交付件, 失败原因)。"""
    snap = job.snapshot
    outline = _load_outline(run_dir)
    plan_unit = _plan_unit(run_dir, job.unit)
    if plan_unit is None:
        return None, "这门课的大纲里找不到这个单元（课程可能被手工改过）"
    title = str(job.course.get("title") or outline.get("course_title") or "")
    argv = [str(FLOW_SCRIPT), title, "--resume", str(run_dir),
            "--unit", str(plan_unit["unit_id"]), "--units", "1",
            "--to", "render", "--no-classroom", "--no-input"]
    if os.environ.get("COURSE_BUILDER_NO_IMAGES") == "1":
        argv.append("--no-images")
    snap["stage_label"] = "启动出片流水线"
    _ppt_log(snap, "命令：" + _short_cmd(argv))
    _ppt_save(conn, snap)

    env = {**os.environ}
    with contextlib.suppress(Exception):
        env.update(modelconfig.env_for_subprocess(conn))
    env.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    extra = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = await asyncio.create_subprocess_exec(
            __import__("sys").executable, *argv, cwd=str(FLOW_DIR), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **extra)
    except OSError as exc:
        return None, f"流水线起不来：{type(exc).__name__}: {exc}"
    job.proc = proc
    job.last_output = time.monotonic()
    try:
        await asyncio.wait_for(_pump_flow(job, conn, proc), PPT_TIMEOUT)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        return None, f"超时：超过 {PPT_TIMEOUT // 60} 分钟仍未出片"
    if job.finished:                              # 取消路径已经结算过
        return None, "已取消"
    rc = proc.returncode if proc.returncode is not None else await proc.wait()
    produced = _deliverable_from_manifest(run_dir, str(plan_unit["unit_id"]))
    if rc != 0:
        return produced, f"流水线退出码 {rc}"
    return produced, "" if produced else "流水线退出码 0，但没有产出 PPTX"


def _load_outline(run_dir: Path) -> dict:
    path = run_dir / "outline.json"
    if not path.is_file():
        return {}
    data = _loads(path.read_text(encoding="utf-8", errors="replace"), {})
    return data if isinstance(data, dict) else {}


def _plan_unit(run_dir: Path, unit: dict) -> dict | None:
    """库里的单元 → 大纲里的单元条目（蓝图 id / 章号 / 章内单元号）。

    为什么不能直接用 ``units.id``：库里那个 id 是 catalog 口径的 sha1（为了"重新扫描
    不重复插"），而 flow.py 的 ``--unit`` 认的是蓝图 id（``ch01-u1``）。两者之间没有
    任何存储关系，**唯一的桥是顺序** —— 入库时按大纲顺序写 ``order_index``，
    这里按它取回同一条。
    """
    outline = _load_outline(run_dir)
    flat = course_plan.flatten_outline(outline) if outline else []
    index = int(unit.get("order_index") or 0)
    if 0 <= index < len(flat):
        candidate = flat[index]
        if candidate["unit_title"] == (unit.get("title") or "") or not unit.get("title"):
            return candidate
    title = (unit.get("title") or "").strip()
    for item in flat:
        if item["unit_title"] == title:
            return item
    return None


def _short_cmd(argv: list[str]) -> str:
    parts: list[str] = ["python"]
    for index, item in enumerate(argv):
        if index == 0:
            with contextlib.suppress(ValueError):
                parts.append(Path(item).relative_to(FLOW_DIR).as_posix())
                continue
            parts.append(item)
        elif index == 1:
            parts.append(f'"{item}"')
        else:
            parts.append(item)
    text = " ".join(parts)
    return text if len(text) <= 300 else text[:299] + "…"


async def _pump_flow(job: _PptJob, conn, proc) -> None:
    """逐行读 flow 的输出：阶段标记推进进度，其余只留最后几十行。

    没有输出超过 ``HEARTBEAT_SECONDS`` 就补一行"我还在跑"：出片在渲染阶段可能几分钟
    不吭声，页面上只有一个不动的 0% 和"启动出片流水线" —— 那和卡死长得一模一样。
    """
    assert proc.stdout is not None
    while True:
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            idle = time.monotonic() - (job.last_output or job.started or time.monotonic())
            _ppt_log(job.snapshot,
                     f"（仍在运行：已 {int(idle)} 秒没有新输出，多半在算或渲染）")
            _ppt_save(conn, job.snapshot)
            continue
        if not raw:
            break
        job.last_output = time.monotonic()
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line.strip():
            continue
        _ppt_log(job.snapshot, line[:300])
        found = _STAGE_LINE.search(line)
        if found is not None:
            stage = found.group(1)
            percent, label = _PPT_STAGES.get(stage, (0.0, ""))
            if percent and percent > float(job.snapshot["percent"]):
                job.snapshot["stage"] = stage
                job.snapshot["stage_label"] = label or job.snapshot["stage_label"]
                job.snapshot["percent"] = percent
                _ppt_save(conn, job.snapshot)
    await proc.wait()


def _deliverable_from_manifest(run_dir: Path, unit_id: str) -> Path | None:
    """从 manifest 的 ``s5_render:<uid>`` 拿交付件；退一步按命名口径找。"""
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        data = _loads(manifest_path.read_text(encoding="utf-8", errors="replace"), {})
        step = ((data.get("steps") or {}).get(f"s5_render:{unit_id}") or {})
        for item in step.get("artifacts") or []:
            path = Path(str(item))
            if path.suffix.lower() == ".pptx" and path.is_file():
                return path
            candidate = run_dir / str(item)
            if candidate.suffix.lower() == ".pptx" and candidate.is_file():
                return candidate
    hits = sorted(p for p in run_dir.rglob("*.pptx") if not p.name.startswith("~$"))
    return hits[-1] if hits else None


def _synthesize_pptx(job: _PptJob, run_dir: Path) -> Path:
    """按 ``unit_design_<uid>.json`` 直接合成一份 PPTX（离线 / 流水线失败时的兜底）。

    它不用模型：页序与要点全部来自已经生成好的单元设计。所以它产出的不是"另一套内容"，
    而是同一份设计的另一种渲染 —— 这也让它成为"渐进式生成永远能给出课件"的保证。
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    unit = job.unit
    plan_unit = _plan_unit(run_dir, unit)
    if plan_unit is None:
        raise RuntimeError("大纲里找不到这个单元")
    outline = _load_outline(run_dir)
    design_path = run_dir / f"unit_design_{plan_unit['unit_id']}.json"
    design = _loads(design_path.read_text(encoding="utf-8", errors="replace"), {}) \
        if design_path.is_file() else {}
    if not isinstance(design, dict) or not design:
        design = course_plan.offline_design(
            {"id": plan_unit["unit_id"], "title": plan_unit["unit_title"],
             "hours": plan_unit.get("hours") or 1.0, "type": plan_unit.get("unit_type") or "新授",
             "knowledge_points": plan_unit.get("knowledge_points") or []},
            {"id": plan_unit["chapter_id"], "title": plan_unit["chapter_title"],
             "units": [{"id": plan_unit["unit_id"], "title": plan_unit["unit_title"]}]},
            outline)

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    def _add(title: str, lines: list[str], *, kicker: str = "") -> None:
        slide = prs.slides.add_slide(blank)
        if kicker:
            box = slide.shapes.add_textbox(Inches(0.9), Inches(0.5), Inches(11.5), Inches(0.5))
            box.text_frame.text = kicker
            box.text_frame.paragraphs[0].font.size = Pt(14)
        head = slide.shapes.add_textbox(Inches(0.9), Inches(1.0), Inches(11.5), Inches(1.1))
        head.text_frame.text = title
        head.text_frame.paragraphs[0].font.size = Pt(30)
        head.text_frame.paragraphs[0].font.bold = True
        if lines:
            body = slide.shapes.add_textbox(Inches(0.9), Inches(2.3), Inches(11.5), Inches(4.6))
            frame = body.text_frame
            frame.word_wrap = True
            for index, line in enumerate(lines):
                para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                para.text = f"• {line}"
                para.font.size = Pt(18)

    _add(design.get("title") or plan_unit["unit_title"] or "本课", [], kicker="课件")
    _add("本课目标", list(design.get("teaching_goals") or [])[:6] or ["掌握本课核心内容"], kicker="目标")
    if design.get("prerequisites"):
        _add("课前需要的基础", list(design["prerequisites"])[:6], kicker="衔接")
    for slide in design.get("slides") or []:
        _add(str(slide.get("title") or ""), list(slide.get("points") or [])[:6], kicker="讲解")
    for index, item in enumerate((design.get("exercises") or [])[:4]):
        _add(f"练习 {index + 1}", [str(item.get("q") or "")], kicker="练习")
    if design.get("summary"):
        _add("小结", [str(design["summary"])], kicker="小结")

    chapter_dir = run_dir / _chapter_dirname({
        "number": plan_unit["chapter_order"], "title": plan_unit["chapter_title"]})
    chapter_dir.mkdir(parents=True, exist_ok=True)
    target = chapter_dir / _unit_filename({
        "number": int(plan_unit["unit_index"]) + 1,
        "title": plan_unit["unit_title"] or plan_unit["unit_id"]})
    prs.save(str(target))
    return target


def _finish_ppt_ok(conn, job: _PptJob, pptx: Path) -> None:
    """成功：登记到单元、顺手备一次课、写任务终态。"""
    snap = job.snapshot
    unit_id = str(job.unit["id"])
    slides = 0
    try:
        from pptx import Presentation

        slides = len(Presentation(str(pptx)).slides)
    except Exception:
        slides = 0
    with contextlib.suppress(Exception):
        repo.upsert_unit(conn, {
            "id": unit_id,
            "course_id": job.unit["course_id"],
            "chapter": job.unit.get("chapter") or "",
            "title": job.unit.get("title") or "",
            "order_index": int(job.unit.get("order_index") or 0),
            "pptx_path": str(pptx),
            "slides": slides,
            "est_sec": None,
        })

    # 备课（解析 + 讲解脚本）：失败只记一行，不影响"这一课有片了"这个事实
    note = ""
    try:
        from app import deps

        row = repo.get_unit(conn, unit_id)
        if row is not None:
            result = deps.prepare_unit(row)
            if result.get("ok") or result.get("deck_ready"):
                repo.mark_unit_prepared(conn, unit_id, slides=result.get("slides"),
                                        est_sec=result.get("est_sec"),
                                        plan_source=result.get("plan_source") or "")
                slides = int(result.get("slides") or slides)
            else:
                note = "；".join(result.get("warnings") or [])[:200]
    except Exception as exc:                       # noqa: BLE001
        note = f"{type(exc).__name__}: {exc}"[:200]

    snap["pptx_path"] = str(pptx)
    snap["slides"] = slides
    snap["stage"] = "S5"
    snap["stage_label"] = "课件已生成"
    if note:
        _ppt_log(snap, f"备课提示：{note}")
    _ppt_finish(conn, job, "succeeded", error="")
    log.info("PPT 完成 unit=%s slides=%s path=%s", unit_id, slides, pptx)


async def _kill_tree(proc) -> None:
    """杀整棵进程树（与 generation.py 同一理由：flow 会拉起 pipeline / LibreOffice）。"""
    if proc is None or proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            import signal as _signal

            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
    except OSError as exc:
        log.warning("PPT 终止失败 pid=%s err=%s: %s", proc.pid, type(exc).__name__, exc)
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), 10)


# ===========================================================================
# 重启对账
# ===========================================================================

def reconcile(conn=None) -> int:
    """把上一个进程留下的"运行中"结算成 interrupted（会话任务 + PPT 任务）。"""
    _reset_llm_hint()
    own = conn is None
    if own:
        conn = _connect()
    changed = 0
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM course_builder_sessions"
            " WHERE phase IN ('generating_outline','generating_course')")]
        for row in rows:
            row["phase"] = "editing_outline" if row.get("outline_json") else "confirming_requirements"
            row["task_status"] = "interrupted"
            row["task_error"] = "服务重启：这一步在之前的进程里运行，结果未知，请重试"
            _save_row(conn, row)
            changed += 1
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM unit_ppt_jobs WHERE status IN ('queued','running')")]
        for job in jobs:
            job["status"] = "interrupted"
            job["error"] = "服务重启：这一课在之前的进程里生成，结果未知（产物请到 runs 下确认）"
            job["finished_at"] = _now()
            _ppt_save_conn(conn, job)
            changed += 1
        return changed
    finally:
        if own:
            conn.close()
