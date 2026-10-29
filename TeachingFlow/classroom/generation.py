"""出片任务：HTTP 发起 → 后台跑 flow.py → 进度与结果落 sqlite。

为什么是子进程，而不是在这里 import flow
---------------------------------------
1. flow.py 的 S0-S5 是**同步阻塞**的模型调用链，实测一跑十几分钟；放进事件循环就是整个
   课堂服务停摆 —— 连"看进度"的轮询请求都会一起卡死；
2. 中止时只杀 flow 这一个进程是不够的：它自己还会拉起 pipeline（乃至 LibreOffice），
   进程边界是唯一能一次性收干净的地方（Windows 用 taskkill /T）；
3. flow.py 属于另一条产品线（TeachingFlow），课堂不该把它那一整套依赖拖进自己的进程。

为什么实时进度只在内存、库里的行只在阶段切换时写
---------------------------------------------
页面 1 秒轮询一次 GET /generations/{id}，那次只做一条 SELECT：运行中任务的实时快照在
本进程内存里（_LIVE）。每行日志都写一次库会把轮询变成写放大器，而收益是零 ——
课堂是单进程 uvicorn（仓库根 app.py）。代价与对应的交代都在 reconcile()：库里的行停在
最后一次阶段切换，所以进程重启后能诚实地把它对账成 interrupted，而不是假装还在跑。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config
import modelconfig
from store import db as db_mod

log = logging.getLogger("classroom.generation")

#: flow.py 的 cwd 与本体（TeachingFlow/）
FLOW_DIR = db_mod.CLASSROOM_ROOT.parent
FLOW_SCRIPT = FLOW_DIR / "flow.py"

#: 硬超时：全链路实测十几分钟，30 分钟还没结束基本是卡死或在无限重试
HARD_TIMEOUT = 30 * 60
#: 只留最后 40 行（首行是命令行原文，永远不参与截断）
LOG_TAIL_KEEP = 40
#: 多久没有新输出就补一行"我还在跑"。渲染阶段本来就可能几分钟不出声，
#: 不给这行的话页面只剩一个在涨的秒数，分不清"在跑"和"卡死"。
HEARTBEAT_SECONDS = 30.0

#: 学段 = flow.py 的 --stage 取值（与 core/scope.py::STAGES 同一口径）。
#: 学段不许用 --accept-detected 兜：它决定模块清单与难度基线，猜错的代价是整份废稿。
SCOPES = ("小学", "初中", "高中", "大学", "考研")

#: 接口层给语义值（types.ts 已定型，契约冻结），flow.py --level 只认中文五档 ——
#: 翻译必须发生在后端：它是 flow.py CLI 词汇的唯一边界。beginner/intermediate 之外的值
#: 一律 400，绝不静默丢弃（悄悄不拼的后果是"我选了入门，出来的是基础"）。
LEVEL_TO_CLI = {"beginner": "入门", "intermediate": "中等"}
#: --term 与 flow.py 的 choices 逐字同值，不翻译
TERMS = ("long", "short")

STAGE_ORDER = ("S0", "S1", "S2", "S3", "S4", "S5")
STAGE_LABELS = {
    "S0": "读懂需求",
    "S1": "规划课程方案",
    "S2": "挑选单元",
    "S3": "撰写单元教学",
    "S4": "设计课件大纲",
    "S5": "渲染 PPTX",
}
#: 阶段 → 百分比，取每个阶段区间的**上界**：页面上的数字只要一动，就说明有一条真实日志行
#: 推进了它，而不是定时器自己往上爬。S3/S4 同属 45-75，分别落在 60 / 75。
STAGE_PERCENT = {"S0": 20, "S1": 35, "S2": 45, "S3": 60, "S4": 75, "S5": 95}

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

_STATUS_ZH = {"succeeded": "已成功", "failed": "已失败",
              "cancelled": "已取消", "interrupted": "已中断"}

_STAGE_LINE = re.compile(r"【(S[0-5])】")
_RUN_DIR_LINE = re.compile(r"(?:产物目录|运行结束)：\s*(.+?)\s*$")
_PPTX_TOKEN = re.compile(r"([^\s\"'，。；]+\.pptx)")
#: rc=0 却一份 PPTX 都没有：flow.py 对**单元级渲染失败只记账不中止**（真机实测：
#: s5_render 失败、s0_module 失败，流程照样走到 _finish 并返回 0）。所以"进程成功"
#: 不等于"出片成功"，判定必须落在产物上。
_NO_PRODUCT = "出片没有产出任何 PPTX（流水线退出码 0，但渲染阶段全部失败）"
_NO_PRODUCT_NEXT = ("先看日志尾里的渲染失败原因（那三个 DEEPSEEK_* 键现在由「设置 · "
                    "模型服务商」里的默认服务商注入，常见是 DEEPSEEK_API_KEY 或模型名不对）")

#: error 是**页面直接贴的那段字**，不是日志：几千字的单行 JSON（RUN_REPORT、教案正文）
#: 贴进失败卡片就是一堵墙，学生/老师只看得出"崩了"。所以有长度纪律：
#: 单行跳过阈值 / 单行截断长度 / 整条上限。
LINE_PICK_MAX = 180
LINE_LIMIT = 160
ERROR_LIMIT = 600
#: 一级：以这些符号开头的短行才是流水线的判决（"✗ s5_render:ch01-u1"）
_CONCLUSION_HEAD = re.compile(r"^[✗❌⛔]")
#: 二级：没挑够时退一步取含这些词的短行（学段/学科中止的选项说明也在这里）
_CONCLUSION_HINT = re.compile(
    r"未能确定|确认当前值|· 学段|· 学科|退出码|渲染失败|Traceback|错误|异常")
_ERROR_SEP = "；"


class GenerationError(Exception):
    """发起/取消不了。code 直接对应 HTTP 语义（bad_request | not_found | conflict）。"""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class _Job:
    """内存里的活任务。数据库只记阶段切换，实时进度全在这儿。"""

    snapshot: dict
    argv: list[str]
    env: dict
    rank: int = -1
    started: float = 0.0
    proc: asyncio.subprocess.Process | None = None
    finished: bool = False
    run_dir: str = ""
    pptx: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    #: 最后一次收到子进程输出的时刻（monotonic）。心跳靠它判断"真的没动静"。
    last_output: float = 0.0


#: 运行中的任务（进程内）。用例会替换它；跨进程的实时进度只对跑它的那个进程可见。
_LIVE: dict[str, _Job] = {}


# ===========================================================================
# 基础
# ===========================================================================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect():
    """后台任务自己的连接：它不走 FastAPI 的 get_conn 依赖，迁移得自己补。"""
    conn = db_mod.connect()
    db_mod.migrate(conn)
    return conn


def _dev_enabled() -> bool:
    """CLASSROOM_DEV=1。函数内 import：app.deps 经由路由反向依赖本模块，模块级会成环。"""
    from app import deps

    return deps.dev_enabled()


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _append_log(snap: dict, line: str) -> None:
    """追加一行日志。截断时从**第一条之后**删：首行是命令行原文，页面靠它复现这次出片。"""
    tail = snap.setdefault("log_tail", [])
    tail.append(line)
    while len(tail) > LOG_TAIL_KEEP:
        del tail[1]


def _progress(stage: str, status: str, percent) -> dict:
    total = len(STAGE_ORDER)
    if status == "succeeded":
        return {"done": total, "total": total, "percent": 100.0}
    done = STAGE_ORDER.index(stage) + 1 if stage in STAGE_ORDER else 0
    return {"done": done, "total": total, "percent": round(float(percent or 0.0), 1)}


def _copy(snap: dict) -> dict:
    out = dict(snap)
    out["progress"] = dict(snap.get("progress") or {})
    out["log_tail"] = list(snap.get("log_tail") or [])
    out["result"] = (dict(snap["result"]) if isinstance(snap.get("result"), dict)
                     else snap.get("result"))
    return out


# ===========================================================================
# 前置条件
# ===========================================================================

def _db_providers() -> list[dict]:
    """库里已有的服务商（对外掩码形状）。只用来把 400 的人话讲准：
    “一个都还没加” 与 “加进来了但没设默认” 是两种不同的下一步。"""
    conn = _connect()
    try:
        return modelconfig.list_providers(conn)
    finally:
        conn.close()


def _db_default() -> dict | None:
    """库里的默认服务商（**含明文 key**，只在本进程用）；没有就 None。

    出片的真源只有库：子进程要的三个键由 modelconfig.env_for_subprocess() 注入，
    所以"有没有 key、用哪个模型"必须问同一份数据。前置检查若还看 .env，就会出现
    "页面说可以出片，子进程里一个 key 都没有"这类只在真跑时才现形的错。
    """
    conn = _connect()
    try:
        return modelconfig.get_default(conn)
    finally:
        conn.close()


def _usable(default: dict | None) -> bool:
    """默认服务商能不能真跑：有 key 也得有模型名（空模型名要跑到一半才报错）。"""
    if not default:
        return False
    provider = default.get("provider") or {}
    has_key = bool(str(provider.get("api_key") or "").strip())
    return has_key and bool(str(default.get("model") or ""))


def _no_provider_message(default: dict | None, *, providers: list[dict] | None = None) -> str:
    """400 的人话：缺什么、去哪儿补。

    页面按 code=no_provider 直接弹「设置 · 模型服务商」，所以 message 只需要
    把"为什么点不动"说清楚；不写变量名 —— 真源已经搬进库，指 .env 是错的去处。
    """
    provider = (default or {}).get("provider") or {}
    if not default:
        names = [str(p.get("name") or p.get("kind") or "").strip() for p in (providers or [])]
        names = [n for n in names if n]
        if names:
            # 服务商加进来了、只是没点「设为默认」：不点它库里 default 永远是空，
            # 出片就一直按"没有服务商"拒绝。原来这句"添加一个"会让人以为白干了。
            return (f"服务商「{'、'.join(names)}」已经加进来了，但还没选默认模型："
                    f"到「设置 · 模型服务商」在它的卡片里选一个模型、点「设为默认」，"
                    f"再回到这里出片")
        return ("还没有可用的模型服务商：到「设置 · 模型服务商」里添加一个"
                "（填 base_url 与 API Key）并点「设为默认」，再回到这里出片")
    name = str(provider.get("name") or provider.get("kind") or "默认服务商")
    if not str(provider.get("api_key") or "").strip():
        return (f"默认服务商「{name}」还没填 API Key：到「设置 · 模型服务商」里"
                f"把它填上（密钥只存在本地库），再回到这里出片")
    return (f"默认服务商「{name}」还没选模型：到「设置 · 模型服务商」里选一个模型"
            f"并保存默认，再回到这里出片")


def _resolve_mode(conn) -> str:
    """这次真跑还是 mock。前置不满足时抛 400 no_provider（页面据此跳设置页）。"""
    default = modelconfig.get_default(conn)
    if _usable(default):
        return "real"
    if _dev_enabled():
        # dev 逃生舱：没有服务商也能把链路走完（--mock 假数据，不产真课件）
        return "mock"
    raise GenerationError(_no_provider_message(default, providers=_db_providers()),
                          "no_provider")


def config_view() -> dict:
    """GET /generations/config：页面据此决定"开始出片"能不能点、以及为什么。

    键集是冻结契约（ready/has_key/model/base_url/mock_allowed/mode/reason），
    model 与 base_url 改读库里的默认服务商 —— 与出片时真正注入子进程的是同一份。
    """
    default = _db_default()
    provider = (default or {}).get("provider") or {}
    usable = _usable(default)
    has_key = bool(str(provider.get("api_key") or "").strip())
    mock_allowed = _dev_enabled()
    name = str(provider.get("name") or provider.get("kind") or "")
    model = str((default or {}).get("model") or "")
    base_url = str(provider.get("base_url") or "")
    if not default:
        # 一个服务商都没有时 model/base_url 只是占位：页面要有一行可显示，但 has_key
        # 仍是 false —— 这不是"已配置"，只是"配好之后大概长这样"。
        placeholder = config.deepseek_profile()
        model, base_url = placeholder.model, placeholder.base_url
    if usable:
        reason = f"默认服务商「{name}」（{model} · {base_url}）已就绪，可以真出片"
    else:
        reason = _no_provider_message(default, providers=_db_providers())
        if mock_allowed:
            reason += ("；CLASSROOM_DEV=1：现在会用 --mock 出片"
                       "（假数据，只验证链路，不产真课件）")
    return {
        "ready": usable or mock_allowed,
        "has_key": has_key,
        "model": model,
        "base_url": base_url,
        "mock_allowed": mock_allowed,
        # 没有服务商也没有 dev 时仍然是 real：那是这次出片**会走**的模式，ready=false 拦住了它
        "mode": "real" if usable or not mock_allowed else "mock",
        "reason": reason,
    }


# ===========================================================================
# 命令
# ===========================================================================

def build_command(*, goal: str, stage: str, subject: str = "", term: str = "",
                  level: str = "", mode: str = "real") -> list[str]:
    """拼出真正要执行的参数表（顺序即页面看到的那条命令）。"""
    argv = [str(FLOW_SCRIPT), goal, "--to", "render", "--no-classroom", "--no-input",
            "--stage", stage]
    if subject:
        argv += ["--subject", subject]
    else:
        # 学科没填时用 --accept-detected 兜住：学段已知，它只会接受学科的残句推断，
        # 不会把学段改回去（core/scope.py::resolve_identity）—— 不兜的话链路会因为
        # "学科不在标准表里"跑到一半就中止，而那不是想要的默认。
        argv.append("--accept-detected")
    if term:
        argv += ["--term", term]
    if level:
        # 语义值 → flow.py 的 CLI 词汇：全后端就这一处翻译（见 LEVEL_TO_CLI 的说明）。
        # 没传就一个字都不拼：让链路自己从原话里抽，抽不到时它有自己的缺省。
        argv += ["--level", LEVEL_TO_CLI[level]]
    if mode == "mock":
        # mock 默认不产片：不追加 --render 的话 S5 直接跳过，整条链路就没有 PPTX 可登记
        argv += ["--mock", "--render"]
    return argv


def describe_command(argv: list[str]) -> str:
    """给页面"高级"区的那一行：python flow.py "一句话" --to render …"""
    parts = ["python"]
    for index, item in enumerate(argv):
        if index == 0:
            parts.append(_script_ref(item))
        elif index == 1:
            parts.append(f'"{item}"')
        else:
            parts.append(item)
    return " ".join(parts)


def _script_ref(path: str) -> str:
    p = Path(path)
    try:
        return p.relative_to(FLOW_DIR).as_posix()
    except ValueError:
        return str(p)


def _nameable(goal: str) -> bool:
    """这句话里有没有能拿来做目录名的字符（flow.py 的 safe_name 会剔掉标点/空白）。"""
    return bool(re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", goal or "").strip("_"))


# ===========================================================================
# 建任务 / 查任务
# ===========================================================================

async def create(conn, *, goal: str, stage: str, subject: str = "", term: str = "",
                 level: str = "") -> dict:
    """校验 → 落库 → 拉起后台任务，返回排队中的 snapshot。

    检查"有没有任务在跑"与写入新行之间**没有一个 await**：请求体跑在事件循环线程上，
    这两步对其它请求因此是原子的 —— 全局单并发不需要再加一把锁。
    """
    goal = (goal or "").strip()
    stage = (stage or "").strip()
    subject = (subject or "").strip()
    term = (term or "").strip()
    level = (level or "").strip()

    if not goal:
        raise GenerationError("请先写下这次想出片的一句话目标（例如：两周突击高一物理的受力分析）")
    if not _nameable(goal):
        raise GenerationError("这句话里没有一个能用来建目录的字符（只有标点/空格），换一句话")
    if stage not in SCOPES:
        raise GenerationError("请先选学段（小学/初中/高中/大学/考研）："
                              "它决定模块清单与难度基线，链路不对它做猜测")
    if term and term not in TERMS:
        raise GenerationError("term 只支持 long / short")
    if level and level not in LEVEL_TO_CLI:
        raise GenerationError("level 只支持 beginner / intermediate")

    mode = _resolve_mode(conn)
    busy = _active_id(conn)
    if busy:
        raise GenerationError(f"已经有一条出片任务在跑（{busy}）：出片全局只允许一个，"
                              f"等它结束或先取消它", "conflict")

    argv = build_command(goal=goal, stage=stage, subject=subject, term=term,
                         level=level, mode=mode)
    gen_id = f"g-{uuid.uuid4().hex[:12]}"
    snap = {
        "id": gen_id,
        "goal": goal,
        "status": "queued",
        "mode": mode,
        "stage": "",
        "stage_label": "排队中",
        "progress": _progress("", "queued", 0.0),
        "log_tail": [describe_command(argv)],
        "started_at": "",
        "finished_at": "",
        "elapsed_sec": 0.0,
        "result": None,
        "error": "",
    }
    # 库 > 继承来的 os.environ：flow.py 只认这三个键（modelconfig.env_for_subprocess
    # 在"没有默认"时返回 {}，于是这里退回今天的纯继承行为）
    job = _Job(snapshot=snap, argv=argv,
               # PYTHONUNBUFFERED 不是可选项：stdout 一接成管道，Python 默认按 8KB 块缓冲，
               # flow.py 里的 print() 又没有 flush，于是"进程活着但一行输出都收不到"，
               # 页面就会永远停在"排队中 0%"（实测：跑 171 秒 log_tail 只有我们自己那行命令行，
               # 而进程一退出日志就全出来了 —— 正是块缓冲的特征）。
               env={**os.environ, **modelconfig.env_for_subprocess(conn),
                    "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    _insert(conn, snap, scope_stage=stage, subject=subject, term=term, level=level)
    _LIVE[gen_id] = job
    # 强引用挂回 job：asyncio 只弱引用任务，不留一份的话它可能被中途回收
    job.task = asyncio.get_running_loop().create_task(_drive(job))
    log.info("出片 排队 id=%s mode=%s scope=%s subject=%s term=%s level=%s goal_len=%s",
             gen_id, mode, stage, subject or "-", term or "-", level or "-", len(goal))
    return _copy(snap)


def _active_id(conn) -> str:
    row = conn.execute(
        "SELECT id FROM generations WHERE status IN ('queued','running') "
        "ORDER BY created_at, rowid LIMIT 1").fetchone()
    return str(row["id"]) if row is not None else ""


def _row_params(snap: dict, *, scope_stage: str = "", subject: str = "", term: str = "",
                level: str = "", created_at: str = "") -> dict:
    return {
        "id": snap["id"], "goal": snap["goal"], "scope_stage": scope_stage,
        "subject": subject, "term": term, "level": level,
        "status": snap["status"], "mode": snap["mode"],
        "stage": snap["stage"], "stage_label": snap["stage_label"],
        "percent": float((snap.get("progress") or {}).get("percent") or 0.0),
        "log_tail": json.dumps(snap.get("log_tail") or [], ensure_ascii=False),
        "created_at": created_at or _now(),
        "started_at": snap["started_at"] or "", "finished_at": snap["finished_at"] or "",
        "elapsed_sec": float(snap["elapsed_sec"] or 0.0),
        "result": (json.dumps(snap["result"], ensure_ascii=False)
                   if snap.get("result") else ""),
        "error": snap.get("error") or "",
    }


def _insert(conn, snap: dict, *, scope_stage: str, subject: str, term: str,
            level: str) -> None:
    conn.execute(
        """INSERT INTO generations(id, goal, scope_stage, subject, term, level, status, mode,
                                   stage, stage_label, percent, log_tail, created_at,
                                   started_at, finished_at, elapsed_sec, result, error)
           VALUES(:id, :goal, :scope_stage, :subject, :term, :level, :status, :mode,
                  :stage, :stage_label, :percent, :log_tail, :created_at,
                  :started_at, :finished_at, :elapsed_sec, :result, :error)""",
        _row_params(snap, scope_stage=scope_stage, subject=subject, term=term,
                    level=level, created_at=_now()),
    )
    conn.commit()


def _save(conn, snap: dict, **extra) -> None:
    """把快照里可变的几列写回库（阶段切换与结束时各一次）。"""
    conn.execute(
        "UPDATE generations SET status=:status, stage=:stage, stage_label=:stage_label, "
        "percent=:percent, log_tail=:log_tail, started_at=:started_at, "
        "finished_at=:finished_at, elapsed_sec=:elapsed_sec, result=:result, error=:error "
        "WHERE id=:id", _row_params(snap, **extra))
    conn.commit()


def _row_snapshot(row: dict) -> dict:
    stage = str(row.get("stage") or "")
    status = str(row.get("status") or "")
    label = str(row.get("stage_label") or "")
    if not label:
        label = "排队中" if status == "queued" else ""
    return {
        "id": str(row["id"]),
        "goal": str(row.get("goal") or ""),
        "status": status,
        "mode": str(row.get("mode") or "real"),
        "stage": stage,
        "stage_label": label,
        "progress": _progress(stage, status, row.get("percent")),
        "log_tail": [str(x) for x in _loads(row.get("log_tail"), [])],
        "started_at": str(row.get("started_at") or ""),
        "finished_at": str(row.get("finished_at") or ""),
        "elapsed_sec": round(float(row.get("elapsed_sec") or 0.0), 1),
        "result": _loads(row.get("result"), None),
        "error": str(row.get("error") or ""),
    }


def _view(row: dict, job: _Job | None) -> dict:
    """DB 行 + 内存里的实时进度 → snapshot（一次 SELECT 就有轮询要的全部内容）。"""
    if job is None or job.finished:
        return _row_snapshot(row)
    live = _copy(job.snapshot)
    if job.started:
        live["elapsed_sec"] = round(time.monotonic() - job.started, 1)
    return live


def get(conn, gen_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if row is None:
        return None
    return _view(dict(row), _LIVE.get(gen_id))


def list_recent(conn, limit: int = 10) -> dict:
    capped = max(1, min(int(limit or 10), 100))
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM generations ORDER BY created_at DESC, rowid DESC LIMIT ?", (capped,))]
    items = [_view(row, _LIVE.get(row["id"])) for row in rows]
    active = next((s for s in items if s["status"] in ACTIVE_STATUSES), None)
    return {"items": items, "active": active}


# ===========================================================================
# 跑：起进程、读 stdout、结算
# ===========================================================================

async def _drive(job: _Job) -> None:
    """一条任务的一生。任何一步失败都要落到终态 —— 停在 running 就是永久的幽灵任务。"""
    conn = _connect()
    try:
        extra: dict = {} if os.name == "nt" else {"start_new_session": True}
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, *job.argv,
                cwd=str(FLOW_DIR), env=job.env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                **extra)
        except OSError as exc:
            _append_log(job.snapshot, f"❌ 起不来：{type(exc).__name__}: {exc}")
            _finish(job, conn, "failed", error=f"启动失败：{type(exc).__name__}: {exc}")
            return
        job.proc = proc
        job.started = time.monotonic()
        job.snapshot.update(status="running", started_at=_now())
        _save(conn, job.snapshot)
        log.info("出片 开始 id=%s pid=%s", job.snapshot["id"], proc.pid)

        job.last_output = time.monotonic()
        # 进程起来了就不是"排队"了：首条阶段标记要等 S0 读完原话才出现（判模块的模型调用
        # 可能几十秒），中间这段只显示"排队中 0%"会让人以为没在跑。
        job.snapshot.update(stage_label="流水线已启动（等首条阶段输出）")
        _append_log(job.snapshot, f"（已启动 pid={proc.pid}，等待流水线输出）")
        _save(conn, job.snapshot)
        hb_stop = asyncio.Event()
        hb = asyncio.get_running_loop().create_task(_heartbeat(job, hb_stop))
        try:
            await asyncio.wait_for(_pump(job, conn, proc), HARD_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            _append_log(job.snapshot, f"⏱ 超过 {HARD_TIMEOUT // 60} 分钟仍未结束，已终止")
            _finish(job, conn, "failed",
                    error=f"超时：超过 {HARD_TIMEOUT // 60} 分钟还没出片，已终止整棵进程树")
            return
        finally:
            hb_stop.set()
            hb.cancel()
        if job.finished:            # 取消路径已经结算过，别把 cancelled 改回 failed
            return
        rc = proc.returncode if proc.returncode is not None else await proc.wait()
        if job.finished:
            return
        if rc != 0:
            _finish(job, conn, "failed",
                    error=_failure_error(rc, job.snapshot["log_tail"]))
            return
        await _on_success(job, conn)
    except Exception as exc:        # 兜底：驱动自己出 bug 也不许留下运行中的行
        log.exception("出片 驱动异常 id=%s", job.snapshot["id"])
        with contextlib.suppress(Exception):
            _finish(job, conn, "failed", error=f"内部错误：{type(exc).__name__}: {exc}")
    finally:
        _LIVE.pop(job.snapshot["id"], None)
        conn.close()


async def _pump(job: _Job, conn, proc) -> None:
    """逐行读 stdout。子进程的输出就是进度的唯一事实源，不做任何猜测。"""
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        job.last_output = time.monotonic()
        _ingest(job, conn, raw.decode("utf-8", "replace").rstrip("\r\n"))
    await proc.wait()


async def _heartbeat(job: _Job, stop: asyncio.Event) -> None:
    """长时间没有新输出时补一行"我还在跑"。

    渲染阶段本来就可能几分钟不出声（S5 交给 PPT-Agent 之后更是如此），加上
    "页面只显示已用时长"会让人分不清"在跑"和"卡死"。这行不参与任何判定，
    只是让台账与页面诚实；进度与成败仍然只由子进程的输出决定。

    判据是"空闲时长"带一点容差（0.9 倍），不能写成"距上次输出是否 >= 30 秒"：
    心跳 tick 与最后一行输出之间的差常常刚好差那么零点几秒，严格比较会一拍一拍地错过
    （真机实测：跑到 40 秒还没有补行）。也不能用"上次输出时刻变没变"：进程启动后第一秒
    就会吐 4 行，首拍必然被判成"有新输出"而顺延到第二拍。
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        if job.finished:
            return
        idle = time.monotonic() - (job.last_output or job.started or time.monotonic())
        if idle < HEARTBEAT_SECONDS * 0.9:
            continue
        _append_log(job.snapshot,
                    f"（仍在运行：已 {int(idle)} 秒没有新输出，流水线多半在算或渲染）")


def _ingest(job: _Job, conn, line: str) -> None:
    if job.finished:
        return
    if line.strip():
        _append_log(job.snapshot, line)
    found = _STAGE_LINE.search(line)
    if found is not None:
        _advance(job, conn, found.group(1))
    found = _RUN_DIR_LINE.search(line)
    if found is not None:
        job.run_dir = found.group(1)
    for token in _PPTX_TOKEN.findall(line):
        if token not in job.pptx:
            job.pptx.append(token)


def _advance(job: _Job, conn, stage: str) -> None:
    """阶段只许前进。

    多单元时会重复走 S3/S4/S5（第二个单元又来一遍），按"取最大值"处理，
    页面上的进度才不会来回跳 —— 进度条倒着走比走得慢更让人不敢信。
    """
    rank = STAGE_ORDER.index(stage)
    if rank <= job.rank:
        return
    job.rank = rank
    snap = job.snapshot
    snap["stage"] = stage
    snap["stage_label"] = STAGE_LABELS[stage]
    snap["progress"] = _progress(stage, snap["status"], STAGE_PERCENT[stage])
    _save(conn, snap)


def _clip(text: str, limit: int = LINE_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _conclusion_lines(tail: list[str], limit: int = 2) -> list[str]:
    """从日志尾里挑 1~2 行"结论"，长行一律不取。

    两条硬规矩：
    1. 单行超过 LINE_PICK_MAX 的直接跳过 —— RUN_REPORT 那种把整份 JSON/教案正文打在
       一行里的输出，是"过程"不是"结论"，贴到页面上只会把原因淹掉；
    2. 先挑以 ✗ / ❌ / ⛔ 开头的行（哪一步判了失败），不够再退一步挑带关键词的行
       （学段/学科中止的选项说明就走这一档）。
    """
    head_hits: list[str] = []
    hint_hits: list[str] = []
    seen: set[str] = set()
    for line in tail:
        text = (line or "").strip()
        if not text or len(text) > LINE_PICK_MAX or text in seen:
            continue
        seen.add(text)
        if _CONCLUSION_HEAD.match(text):
            head_hits.append(_clip(text))
        elif _CONCLUSION_HINT.search(text):
            hint_hits.append(_clip(text))
    return (head_hits + hint_hits)[:limit]


def _join_error(head: str, lines: list[str], next_step: str = "") -> str:
    """结论 + 结论行 + 下一步，三段用「；」连，总长压到 ERROR_LIMIT。

    超长时**先丢中间行、再压结论句**，"下一步"那句永远留在末尾：页面要的是
    "出什么事 + 我该怎么办"，中间细节可以去日志尾里翻。
    """
    kept = list(lines)
    while True:
        text = _ERROR_SEP.join(p for p in [head, *kept, next_step] if p)
        if len(text) <= ERROR_LIMIT or not kept:
            break
        kept.pop()
    if len(text) <= ERROR_LIMIT:
        return text
    room = max(ERROR_LIMIT - len(next_step) - len(_ERROR_SEP) - 1, 40)
    return _ERROR_SEP.join(p for p in [_clip(head, room), next_step] if p)[:ERROR_LIMIT]


#: 日志里出现的状态码 → 下一步动作。和 modelconfig._STATUS_ACTIONS 同一套说法，
#: 这里单独列是因为出片看到的是"流水线转述的服务商错误"，不是 HTTP 响应对象。
_FAILURE_ACTIONS: tuple[tuple[str, str], ...] = (
    ("402", "去服务商后台充值，或在「设置」新增另一家有额度的服务商并设为默认"),
    ("Insufficient Balance", "去服务商后台充值，或在「设置」换一家有额度的服务商"),
    ("401", "到「设置 · 编辑」更新这家的 API Key"),
    ("403", "确认这把 Key 有该模型的权限"),
    ("429", "稍后再试，或换一把 Key / 另一家服务商"),
    ("404", "确认 base_url 以 /v1 结尾、模型名与服务商一致"),
    ("invalid model", "到「设置」把默认模型改成一个服务商支持的模型名"),
)


def _failure_action(text: str) -> str:
    """从失败文本里认出"下一步该按哪个键"。认不出就给空串，别硬编一句套话。"""
    low = text.lower()
    for token, action in _FAILURE_ACTIONS:
        if token.lower() in low:
            return action
    return ""


def _failure_error(rc: int, tail: list[str]) -> str:
    """失败原因 = 退出码 + 日志尾里的结论行 + 下一步动作（认得出来时）。

    只报"402 Insufficient Balance"对用户是死路：他知道数字，不知道要去充值。
    所以把状态码翻成动作 —— 与设置页「测试对话」用的是同一套说法。
    """
    picked = _conclusion_lines(tail, 3)
    head = _join_error(f"退出码 {rc}", picked) if picked else f"退出码 {rc}：（没有任何输出）"
    action = _failure_action(head)
    return f"{head}；{action}" if action else head


def _finish(job: _Job, conn, status: str, *, result: dict | None = None,
            error: str = "") -> dict:
    """结算成终态。幂等：取消路径先结算过，驱动结束时就不再改。"""
    if job.finished:
        return _copy(job.snapshot)
    job.finished = True
    return _settle(job, conn, status, result=result, error=error)


def _settle(job: _Job, conn, status: str, *, result: dict | None = None,
            error: str = "") -> dict:
    """把终态写进快照与库（调用方负责先认领 job.finished）。"""
    snap = job.snapshot
    snap["status"] = status
    snap["finished_at"] = _now()
    if job.started:
        snap["elapsed_sec"] = round(time.monotonic() - job.started, 1)
    if status == "succeeded":
        snap["progress"] = _progress(snap["stage"], "succeeded", 100.0)
    snap["result"] = result
    snap["error"] = error
    _save(conn, snap)
    log.info("出片 结束 id=%s status=%s elapsed=%ss pptx=%s error=%s",
             snap["id"], status, snap["elapsed_sec"],
             len((result or {}).get("pptx") or []), error[:200])
    return _copy(snap)


async def _on_success(job: _Job, conn) -> None:
    """成功路径：找产物 → 登记课程库 → 把"登记到了哪门课"写进 result。"""
    snap = job.snapshot
    run_dir = Path(job.run_dir) if job.run_dir else None
    pptx = _products(job, run_dir)
    if not pptx:
        # 退出码 0 不算数：渲染全失败时 flow.py 依然返回 0（见 _NO_PRODUCT 的注释）。
        # 这里既不该登记课程（没有东西可登），也不该说"产物已生成"——那是谎话。
        # 先按子进程输出算 error，再补自己这一行：反过来的话结论句会被自己的日志
        # 当成"结论行"再挑一次，error 里那句话就会出现两遍。
        error = _no_product_error(snap["log_tail"])
        _append_log(snap, "❌ 出片没有产出任何 PPTX（流水线退出码 0，渲染阶段全部失败）")
        _finish(job, conn, "failed",
                result={"course_id": "", "course_title": "", "pptx": [],
                        "scan": {"added": 0, "updated": 0, "total": 0}},
                error=error)
        return
    scan = {"added": 0, "updated": 0, "total": 0}
    note = ""
    try:
        # 扫描要开 pptx 数页数（几百毫秒到几秒），别让正在轮询的 GET 陪着一起卡
        scanned = await asyncio.to_thread(_scan_and_upsert)
        # result.scan 是冻结契约的三个计数；扫描的警告进日志 —— 页面上的解释归 error
        # 与 log_tail，往 scan 里塞第四第五个键就是单方面改契约。
        scan = {k: int(scanned.get(k) or 0) for k in ("added", "updated", "total")}
        for line in scanned.get("warnings") or []:
            _append_log(snap, f"⚠️ 扫描：{line}")
    except Exception as exc:
        note = f"产物已生成但未登记：课程库扫描失败 {type(exc).__name__}: {exc}"
        log.warning("出片 登记失败 id=%s err=%s: %s",
                    snap["id"], type(exc).__name__, exc)

    course = _course_by_run_dir(conn, run_dir)
    result = {"course_id": "", "course_title": "", "pptx": pptx, "scan": scan}
    if course is not None:
        result["course_id"] = str(course["id"])
        result["course_title"] = str(course["title"] or "")
        _append_log(snap, f"📚 已登记课程：{result['course_title'] or result['course_id']}"
                          f"（新增 {scan['added']} / 更新 {scan['updated']}"
                          f" / 本次扫到 {scan['total']} 门）")
    else:
        if not note:
            note = ("产物已生成但未登记：课程库里没有与本次 run 目录对应的课程"
                    f"（{run_dir or '输出里没找到 run 目录'}）")
        _append_log(snap, f"⚠️ {note}")
    _append_log(snap, f"✅ 出片完成：{len(pptx)} 份 PPTX" if pptx
                else "✅ 出片结束：本次没有产出 PPTX（mock 未产片？）")
    _finish(job, conn, "succeeded", result=result, error=note)


def _scan_and_upsert() -> dict:
    """课程库登记。函数内 import：courses 路由与本模块在同一次装配里互相依赖。"""
    from app.routes.courses import scan_and_upsert

    conn = _connect()
    try:
        return scan_and_upsert(conn)
    finally:
        conn.close()


def _norm_path(value) -> str:
    return os.path.normcase(os.path.normpath(str(value or ""))).rstrip("\\/")


def _course_by_run_dir(conn, run_dir: Path | None) -> dict | None:
    """按 run 目录找回本次登记的课程。

    比的是规范化后的绝对路径：扫描给的 run_dir 与 flow.py 打印的产物目录本该逐字相同，
    差一级只可能是 runs 根配错或目录被搬过，那种情况用"谁在谁里面"再认一次。
    """
    if run_dir is None:
        return None
    key = _norm_path(run_dir)
    rows = [dict(r) for r in conn.execute("SELECT id, title, run_dir FROM courses")]
    for row in rows:
        if row["run_dir"] and _norm_path(row["run_dir"]) == key:
            return row
    for row in rows:
        other = _norm_path(row["run_dir"])
        if other and (other.startswith(key + os.sep) or key.startswith(other + os.sep)):
            return row
    return None


def _products(job: _Job, run_dir: Path | None) -> list[str]:
    """这次 run 真实的交付件：stdout 里提到的 + manifest.produced/扫盘，取并集去重。

    只认**盘上真的存在**的文件：日志里出现某个 .pptx 不代表它是本次产物 ——
    "已有交付件，跳过"那行同样带路径，按日志判定就会把空跑判成成功。
    """
    found: dict[str, str] = {}
    for item in [*job.pptx, *_pptx_in(run_dir)]:
        path = Path(item)
        if not path.is_file():
            continue
        found.setdefault(_norm_path(path), str(path))
    return list(found.values())


def _no_product_error(tail: list[str]) -> str:
    """零产物的失败说明：结论 + 原样的结论行（最多 2 行）+ 下一步。"""
    return _join_error(_NO_PRODUCT, _conclusion_lines(tail, 2), _NO_PRODUCT_NEXT)


def _pptx_in(run_dir: Path | None) -> list[str]:
    """run 目录里的交付件：先信 manifest.json 的 produced，退一步才扫盘。"""
    if run_dir is None or not run_dir.is_dir():
        return []
    out: list[str] = []
    manifest = run_dir / "manifest.json"
    if manifest.is_file():
        data = _loads(manifest.read_text(encoding="utf-8", errors="replace"), {})
        if isinstance(data, dict):
            for item in data.get("produced") or []:
                p = Path(str(item))
                if p.suffix.lower() != ".pptx":
                    continue
                out.append(str(p if p.is_absolute() else run_dir / p))
    if out:
        return out
    try:
        return sorted(str(p) for p in run_dir.rglob("*.pptx") if p.is_file())
    except OSError:
        return []


# ===========================================================================
# 取消
# ===========================================================================

async def cancel(conn, gen_id: str) -> dict:
    row = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if row is None:
        raise GenerationError("找不到这条出片任务", "not_found")
    row = dict(row)
    if row["status"] in TERMINAL_STATUSES:
        raise GenerationError(
            f"这条出片任务已经结束（{_STATUS_ZH.get(row['status'], row['status'])}），"
            f"不能再取消", "conflict")

    job = _LIVE.get(gen_id)
    if job is None:
        # 内存里没有它：可能是上个进程留下的行，也可能跑在别的 worker 里。
        # 这里不假装"已经杀掉了进程"，只把状态结算掉，并把这句话写进日志。
        snap = _row_snapshot(row)
        snap["status"] = "cancelled"
        snap["finished_at"] = _now()
        snap["error"] = "已取消（本进程里没有对应进程：任务来自上一次服务，或跑在别的进程）"
        _append_log(snap, "⛔ 已取消")
        _save(conn, snap)
        log.info("出片 取消 id=%s（本进程无对应进程）", gen_id)
        return snap

    # 先认领结算再去杀：进程一死，驱动那边立刻会读到非 0 退出码 ——
    # 不先占住终态，cancelled 就会被它改写成 failed（用户点了取消，看到的却是失败）。
    job.finished = True
    await _kill_tree(job.proc)
    _append_log(job.snapshot, "⛔ 已取消：整棵进程树已终止")
    snap = _settle(job, conn, "cancelled", error="已取消")
    _LIVE.pop(gen_id, None)
    log.info("出片 取消 id=%s", gen_id)
    return snap


async def _kill_tree(proc) -> None:
    """杀**整棵**进程树：flow.py 会拉起 pipeline 甚至 LibreOffice，只杀它自己会留下孤儿 ——
    它们继续吃 CPU、占着文件，下一次出片会撞在上面。"""
    if proc is None or proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError as exc:
        log.warning("出片 终止失败 pid=%s err=%s: %s", proc.pid, type(exc).__name__, exc)
    try:
        await asyncio.wait_for(proc.wait(), 10)
        return
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


# ===========================================================================
# 重启对账
# ===========================================================================

def reconcile(conn=None) -> int:
    """把上一个进程留下的 queued/running 结算成 interrupted，返回改了几条。

    为什么必须做：任务只在阶段切换与结束时写库，服务被 Ctrl+C / 崩掉时库里就停在
    running。页面照着库显示就是"永远在跑、进度不动"，更糟的是那条幽灵任务还占着
    全局单并发的名额 —— 出片按钮从此再也点不动，而且没有一句话解释。
    """
    own = conn is None
    if own:
        conn = _connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM generations WHERE status IN ('queued','running')")]
        for row in rows:
            snap = _row_snapshot(row)
            snap["status"] = "interrupted"
            snap["finished_at"] = _now()
            snap["error"] = ("服务重启：这条任务在上一个进程里运行，结果未知"
                             "（产物请到 TeachingFlow/runs 下确认）")
            _append_log(snap, "⚠️ 服务重启：不假装还在跑，已标记为 interrupted")
            _save(conn, snap)
            log.info("出片 对账 id=%s -> interrupted", snap["id"])
        return len(rows)
    finally:
        if own:
            conn.close()
