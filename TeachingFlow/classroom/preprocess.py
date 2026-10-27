"""预处理作业管理器 + 课件源库：一次「预处理」= 解析 → 备课 → 预置路线。

为什么要有这一层（而不是让 Web 层直接调 run.cmd_plan）
----------------------------------------------------
cmd_plan 是**同步的命令行**：它自己 asyncio.run 一次备课、把进度 print 到终端、出错就抛。
Web 层要的是另一套形状 —— 点一下立刻拿到 job_id、轮询看进度、失败了还能看到最后几行
日志。所以这里只做「把三段串起来 + 记录状态」，三段本身一行都不复制：

  - 解析 ingest_pptx（deck/pptx_ingest.py）
  - 备课 plan_llm（script_gen.py）
  - 预置路线 plan_routes（agent/routes.py）、写盘 run._save_plan

run.py / script_gen.py / state.py / web.py / web/*.html 这一棒**另有并行 agent 在改**，
所以对它们全是只读引用（`from run import _save_plan, _route_polisher`）。反过来把
cmd_plan 整段抄过来是最坏的选择：合并部分页、备份、路线润色降级这三处口径一旦分开演化，
就必然出现「命令行备的和页面上备的不是同一节课」。

为什么作业跑在**同进程**的 asyncio 任务里（不用 subprocess）
------------------------------------------------------------
作业要和 library() 共用 decks/ 与 pptx/ 两个目录、共用同一份配置。起子进程只会多一份
"环境不一致 / 子进程死了没人回收"的风险，换不来任何好处。代价是这里必须自己扛三件事：

  1. 解析与路线是**同步**重活 → 丢进线程（asyncio.to_thread）。解析一份 42 页的课件要
     几十秒，直接在协程里跑会把事件循环堵死，轮询请求同时被堵住 —— 前端看到的不是
     "进度 30%"，而是"服务没响应"。路线那条更要紧：`run._route_polisher` 内部是
     `asyncio.run(...)`，在**正在跑的**事件循环里调用会直接 RuntimeError。
  2. 后台任务没人 await → 异常必须在这里收干净，否则 job 永远停在 running（协程异常只会
     变成一句 "Task exception was never retrieved"），前端就一直转圈。
  3. 作业记录是**进程内存** → 要有上限，页面开一天也不会自己释放。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

from config import ROOT
from deck import store as deck_store

#: 课件源库：上传/拷进来的 pptx 原样存在这里（解析产物仍然进 decks/<deck_id>/）
SOURCES_DIR: Path = ROOT / "pptx"

#: job["log_tail"] 保留的行数（只要尾巴：一次 42 页的备课会打几百行）
LOG_LINES = 200

#: 进程内最多留多少条作业记录（旧的先丢，running 的不丢）
MAX_JOBS = 50

#: 上传上限。一整册教材的 pptx 也就十几 MB，200 是"明显不可能是课件"那条线 ——
#: 没有这条线时 save_upload 会把整个字节流直接写盘，一个误传的大文件就能占满磁盘。
MAX_UPLOAD_MB = 200

#: 支持的 action：全链 / 只重新备课 / 只解析 / 只补总结
_ACTIONS = ("full", "plan", "ingest", "summarize")

#: 文件名里不许出现的字符：Windows 的 \/:*?"<>| 与控制字符
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: 作业被取消/超时后落的 error：这句话会显示在 /preprocess 的作业行上，
#: 所以要说清"为什么没了"和"接下来还能做什么"，而不是只写"cancelled"。
CANCELLED = "作业被取消（服务停止或超时）—— 重新点一次即可"

#: 模块级作业表：id → 内部记录（对外只经 _public 暴露契约里的字段）
_jobs: dict[str, dict] = {}

#: 「这份文件根本打不开」那一类异常名：zip 容器坏了，或 pptx 包里缺了必备部件。
#: 按名字比对而不是 isinstance：这些类来自 python-pptx，而 preprocess 是被 web.py
#: 在启动路径上 import 的，能少拖一个第三方库就少拖一个。
_UNOPENABLE = {"BadZipFile", "LargeZipFile", "PackageNotFoundError", "InvalidXmlError"}


# ===========================================================================
# 对外：课件源库
# ===========================================================================

def library() -> list[dict]:
    """源库：pptx/ 下每个源文件一行，带上它对应 deck 的解析/备课状态。

    为什么在这里聚合、而不是让前端分两次取「源列表」和「课件列表」：两份列表在时间上
    必然错位（刚上传还没解析、刚解析完还没备课），前端要靠 deck_id 自己 join，接错了
    就会显示「已解析」而点开会话是空的。聚合在一处，错也只错一处。

    只列 .pptx：解析器只认它，别的文件列出来也点不动（上传错了当场就看得见）。
    坏文件只填 error，绝不抛 —— 一个坏掉的 plan.json 不该让整页课件库打不开。

    但它**不是页面主表格**：源库只看得见 pptx/，而仓库里那份真实课件的源文件在仓库根
    目录（当初直接从根目录 ingest）。只按这一侧取值，页面就会在 decks/ 明明躺着一份能
    上课的课件时显示"空课件库"。主行取 deck_rows()，本函数补"还没解析的源"。
    """
    out: list[dict] = []
    if not SOURCES_DIR.is_dir():
        return out
    for p in sorted(SOURCES_DIR.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".pptx":
            continue
        out.append(_entry(p))
    return out


def deck_rows() -> list[dict]:
    """已解析课件一行一条 —— 页面主表格的行（**包括源文件不在 pptx/ 里的**）。

    为什么不能直接给前端 list_decks()：它只读 deck.json，"有没有总结、补了几条、
    上一版备份在不在、源 pptx 还找不找得到"这几件事它一概不知道，而这些正是用户
    在这一页判断"该点哪个按钮"的全部依据。让前端拿三份列表自己 join，接错了的表现
    是按钮莫名其妙灰着 —— 用户只会以为功能没做。

    坏 plan.json 只填 error，绝不抛：一行坏掉不该让整页课件库打不开。
    """
    out: list[dict] = []
    for d in deck_store.list_decks():
        did = str(d.get("deck_id") or "")
        if not did:
            continue
        st = plan_stats(did)
        recorded = _source_path_recorded(did)
        in_lib = _library_source(did)
        out.append({
            "deck_id": did,
            "title": d.get("title") or did,
            "slides": int(d.get("slides") or 0),
            "has_plan": deck_store.plan_json(did).exists(),
            "units": st["units"],
            "summarized": st["summarized"],
            "has_goals": st["has_goals"],
            "has_bak": deck_store.plan_bak_json(did).exists(),
            # 名字优先取 source.txt 记的那个（它就是当初解析的文件），没有就取源库里的同名文件
            "source_name": ((recorded.name if recorded is not None else "")
                            or (in_lib.name if in_lib is not None else "")),
            "source_in_library": in_lib is not None,
            "source_path": str(recorded) if recorded is not None else "",
            "source_resolved": recorded is not None and recorded.is_file(),
            "error": d.get("error") or st["error"],
        })
    return out


def save_upload(filename: str, data: bytes) -> dict:
    """把上传的 pptx 落进源库，返回它在 library() 里的那一条（多一个 replaced 标记）。

    文件名必须自己洗：`../../x.pptx` 能写穿 SOURCES_DIR；而**中文不能洗** ——
    deck_id 是从文件名派生的，中文一丢，所有课件都会撞成同一个 deck（`deck`）。
    重名就覆盖：同一份课件重传是常态，"拒绝覆盖"会让用户为了换一版去手动删文件。

    后缀与体积在**写盘之前**拦下来（抛 ValueError，Web 层转 400）：这两样送进解析器
    分别只会得到一句看不懂的 zip 报错和一块被写满的磁盘，而在这里说人话是免费的。
    """
    name = _safe_name(filename)
    if Path(name).suffix.lower() != ".pptx":
        raise ValueError(f"只认 .pptx（这份是 {name!r}）—— 解析器读不了别的格式，"
                         "请在 PowerPoint 里「另存为 .pptx」再传")
    size = len(data or b"")
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"文件 {size / 1048576:.1f} MB，超过 {MAX_UPLOAD_MB} MB 上限 —— "
                         "课件正常只有十几 MB，先确认没传错文件")
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    dst = SOURCES_DIR / name
    replaced = dst.exists()
    # 先写隐藏的临时文件再原子替换：轮询正好撞上写入时会读到半个 pptx，
    # 解析器对半个文件只会给一句看不懂的 zip 报错；替换之后没有中间态。
    tmp = SOURCES_DIR / f".{name}.part"
    tmp.write_bytes(data)
    tmp.replace(dst)
    row = _entry(dst)
    row["replaced"] = replaced
    return row


class IngestError(RuntimeError):
    """解析失败，且 message 本身就是给用户看的那一句（repr 也是它）。

    为什么单单为它改 repr：作业的 error 字段是 repr(e) 存下来的（这样维护者能一眼看出
    异常类型）。坏文件的异常类型（BadZipFile）恰恰是**不该**出现在用户眼前的那部分，
    所以这里让 repr 直接等于人话，而不是在作业层再写一次"哪些异常要抄近路"。
    """

    def __repr__(self) -> str:
        return str(self)


def explain_ingest_error(src, exc: BaseException) -> str:
    """把解析阶段抛出来的原始异常翻成人话（作业 error、run.py pptx 子命令共用这一句）。

    两档，因为用户要做的动作完全不同：
      - 打不开（zip 容器坏了 / 包里缺必备部件）：解析器只会说 "File is not a zip file"，
        用户既看不懂也猜不到该干什么。唯一有用的动作是"在 PowerPoint 里另存一次再传"，
        所以那句话里一个技术名词都不留。
      - 其它异常：保留类型名（维护者要拿它去搜 issue），但补一句上下文说明先查什么。
    """
    name = Path(src).name if src else "这份文件"
    if isinstance(exc, zipfile.BadZipFile) or type(exc).__name__ in _UNOPENABLE:
        return (f"这份 pptx 打不开（不是有效的 pptx/zip）：{name}"
                " —— 请在 PowerPoint 里另存一次再传")
    return (f"解析 {name} 时出错：{type(exc).__name__}: {exc}"
            " —— 先确认路径对不对、文件没坏、不是加密的，也没被别的程序占着")


# ===========================================================================
# 对外：作业
# ===========================================================================

async def start_job(name: str, *, action: str = "full",
                    slides: list[int] | None = None,
                    force: bool = False,
                    source_path: Path | None = None,
                    settings=None, rule_only: bool = False) -> dict:
    """开一个后台预处理作业，**立刻**返回作业快照；进度靠 get_job 轮询。

    为什么是 async：作业要挂在调用方的事件循环上再 create_task —— Web 层就是在循环里
    调它的。为什么不等结果：一次备课可能跑几十分钟，同步接口会把 HTTP 请求一起拖死
    （代理超时、页面白屏），"后台作业 + 轮询"解决的正是这件事。

    没跑起来的情况（action 不认识 / 源不存在 / 缺 deck.json / 同一份源已在跑）也返回
    **同形的 dict**，原因放在 error 里：这样 Web 层只有一条渲染分支，不用为"被拒"再
    写一套。单飞是硬约束，不是优化 —— 两个作业同时写同一个 plan.json，后写的会把
    先写的正常结果盖成半截。

    force 只对 action="summarize" 有意义：True 时先把 unit.summary 清空再交给回填
    （照 run.cmd_summarize --force 的口径），这样"补哪些"始终只有一个判据 —— 空的才补。

    source_path / settings 是给 bridge 的**增量**参数（Web 层不传，行为一个字不变）：
      - source_path：源文件在 pptx/ 之外时直接给路径。出片链路产出的 PPTX 长在
        runs/<日期>/<课程>/ 底下，拷进 pptx/ 等于把 28MB 的文件复制一份，所以这里
        只认路径、不复制（解析产物照样落 decks/<deck_id>/）。
      - settings：本次作业用哪份配置。主 provider 连不上时 bridge 会临时换一家
        （deepseek），而不是去改 os.environ —— 那个进程里可能正上着课。
      - rule_only：不碰模型，备课那段直接用 plan_rule（照读课件）。只在"连一份可用
        配置都没有"时由 bridge 传 True —— 那种情况下 plan_llm 会逐页等模型超时再回退，
        白等几十分钟换来的正是同一份规则计划。
    """
    action = str(action or "full").strip().lower()
    slides = [int(s) for s in slides] if slides else None
    source, src, deck_id = _resolve(name, source_path=source_path)

    if action not in _ACTIONS:
        return _reject(action, source, deck_id,
                       f"不认识的 action：{action!r}（只认 {'/'.join(_ACTIONS)}）")
    # 只有解析那两档要源文件；补总结只碰盘上那份 plan.json，没源文件也照样能跑。
    if action in ("full", "ingest") and src is None:
        return _reject(action, source, deck_id,
                       f"源库里没有 {source!r}（pptx/ 与 decks/{deck_id}/source.txt 记录的路径"
                       f"都找不到 —— 先上传，或把 pptx 放进 {SOURCES_DIR}）")
    if action in ("plan", "summarize") and not deck_store.deck_json(deck_id).exists():
        return _reject(action, source, deck_id,
                       f"还没有 decks/{deck_id}/deck.json —— 先跑一次 action='full' 或 'ingest'")
    if action == "summarize" and not deck_store.plan_json(deck_id).exists():
        return _reject(action, source, deck_id,
                       f"还没有 decks/{deck_id}/plan.json —— 补总结只补已有的计划，"
                       "先跑一次 action='plan' 备课")

    busy = _running_for(source, deck_id)
    if busy is not None:
        return _reject(action, source, deck_id,
                       f"「{busy['source']}」正在预处理（作业 {busy['id']}，阶段 {busy['stage']}）"
                       f"—— 同一份源同时只能跑一个")

    rec = _new_record(action, source, deck_id)
    _jobs[rec["id"]] = rec
    _prune()
    _log(rec, f"排队：action={action} 源={source}"
              + (f"，只备第 {[i + 1 for i in slides]} 页" if slides else ""))
    # 任务的引用必须留住（存在记录里）。create_task 的返回值一旦被回收，任务可能在
    # 跑一半时被 GC 掉 —— asyncio 文档对这一点有明确警告。
    rec["task"] = asyncio.create_task(
        _run_job(rec, src=src, slides=slides, force=bool(force), settings=settings,
                 rule_only=bool(rule_only)))
    return _public(rec)


def get_job(job_id: str) -> dict | None:
    """按 id 读作业快照；查不到返回 None（Web 层据此回 404，而不是抛）。"""
    rec = _jobs.get(str(job_id or ""))
    return _public(rec) if rec is not None else None


def list_jobs(limit: int = 20) -> list[dict]:
    """最近的作业，新的在前。

    顺序靠 dict 的插入序（started_at 只到秒，同一秒开的两个作业靠它排不出先后）。
    """
    recs = list(_jobs.values())[::-1]
    return [_public(r) for r in recs[:max(0, int(limit))]]


async def cancel_job(job_id: str, *, reason: str = "") -> dict | None:
    """取消一个作业，并且**等它真正收尾**之后才返回快照。

    为什么必须等：状态归一写在 _run_job 的 finally 里。cancel() 只是"请求取消"，
    立刻返回快照读到的还是 status="running" —— 调用方拿它判断"到底停没停"会永远判错，
    单飞也还没解开。所以这里 cancel 之后 await 一次任务结束（asyncio.wait 不会把任务的
    异常抛给调用方，CancelledError 也一样）。
    """
    rec = _jobs.get(str(job_id or ""))
    if rec is None:
        return None
    task = rec.get("task")
    if task is not None and not task.done():
        if reason:
            _log(rec, f"取消请求：{reason}")
        task.cancel()
        await asyncio.wait({task})
    if rec["status"] == "running":
        # 任务还没被调度就被取消时，协程体一次都没执行过 —— _run_job 的 finally 不会跑。
        # 这一档只靠 finally 兜不住，所以在这里补一次同样的归一。
        rec["stage"], rec["status"] = "failed", "failed"
        rec["error"] = rec["error"] or CANCELLED
        rec["finished_at"] = rec["finished_at"] or _now()
        _log(rec, f"❌ {rec['error']}")
    return _public(rec)


async def wait_job(job_id: str, *, timeout: float | None = None) -> dict | None:
    """等某个作业结束（测试与脚本用；Web 层走 get_job 轮询，不需要它）。"""
    rec = _jobs.get(str(job_id or ""))
    if rec is None:
        return None
    task = rec.get("task")
    if task is not None and not task.done():
        await asyncio.wait({task}, timeout=timeout)
    return _public(rec)


# ===========================================================================
# 作业主体：三段，一段一个 stage
# ===========================================================================

async def _run_job(rec: dict, *, src: Path | None, slides: list[int] | None,
              force: bool = False, settings=None, rule_only: bool = False) -> None:
    """作业主体。任何异常都收进 job["error"]，绝不往外冒。

    为什么最外层必须有 try：这是 create_task 起的后台任务，异常没人 await，
    只会被 asyncio 记一句 "Task exception was never retrieved"，而 job 永远停在
    running —— 前端一直转圈，也没人知道为什么。

    CancelledError 照样往上抛（取消是调用方的决定，不该被这里吞成"失败"），
    但 finally 里会把仍是 running 的记录归一成 failed：留一条永远 running 的记录，
    后果不是"页面难看"，而是这份源被单飞永久锁死。

    settings 不传就照旧 load_settings()（Web 层的每一条路都不传）；传了就用它，
    且**只在这一条作业里**有效 —— 配置是作业自己的事，不写回 os.environ。
    """
    action, deck_id = rec["action"], rec["deck_id"]
    try:
        if action == "summarize":
            # 补总结既不要源文件、也不重新备课：它只碰盘上那份 plan.json 的 summary。
            from config import load_settings

            await _stage_summarize(rec, deck_id=deck_id,
                                   settings=settings or load_settings(),
                                   slides=slides, force=force)
        else:
            deck = None
            if action in ("full", "ingest"):
                deck = await _stage_ingest(rec, src, deck_id)
            if action != "ingest":
                if deck is None:                # action="plan"：重用上次解析的 deck.json
                    deck = await _stage_load(rec, deck_id)
                # 配置留到最后再读：只解析的作业不该因为一份坏 .env 而失败。
                from config import load_settings

                cfg = settings or load_settings()
                plan = await _stage_plan(rec, deck, cfg, slides, rule_only=rule_only)
                await _stage_routes(rec, plan, deck, cfg, rule_only=rule_only)
        rec["stage"], rec["status"] = "done", "done"
        rec["progress"]["label"] = "完成"
        _log(rec, "✅ 作业完成")
    except asyncio.CancelledError:
        _log(rec, f"❌ {CANCELLED}")
        raise
    except Exception as e:
        rec["stage"], rec["status"] = "failed", "failed"
        rec["error"] = repr(e)
        _log(rec, f"❌ 失败：{rec['error']}")
    finally:
        # 取消/超时不会走上面任何一条分支：CancelledError 从某个 await 点冒出来时
        # status 还是 running，而 _running_for 只认 running、_prune 又拒绝删 running ——
        # 不在这里归一，这份源就永远"正在预处理"，之后的每个作业都被单飞拒掉。
        # 落成 failed（而不是新造一个 cancelled）：契约里 stage/status 没有第三个值。
        if rec["status"] == "running":
            rec["stage"], rec["status"] = "failed", "failed"
            rec["error"] = rec["error"] or CANCELLED
            _log(rec, f"❌ {rec['error']}")
        rec["finished_at"] = _now()


async def _stage_ingest(rec: dict, src: Path | None, deck_id: str):
    """解析源 pptx → decks/<deck_id>/（deck.json + media/ + source.txt）。"""
    from deck.pptx_ingest import ingest_pptx

    _enter(rec, "ingest", "解析课件（大课件要几十秒）")
    # 解析拿不到逐页回调（ingest_pptx 没有 on_progress），所以这一段的进度条是
    # 不确定态的，只有 label；但**不能**直接在协程里跑，见模块 docstring 第 1 点。
    try:
        deck = await asyncio.to_thread(ingest_pptx, src, deck_id=deck_id)
    except Exception as exc:
        # 坏文件的原始异常（"File is not a zip file"）等于没说：翻译成人话再往上抛，
        # 作业 error 与 /preprocess 页面看到的就是同一句。from exc 留住原始 traceback。
        raise IngestError(explain_ingest_error(src, exc)) from exc
    _progress(rec, deck.slide_count, deck.slide_count, f"解析完成：{deck.slide_count} 页")
    _log(rec, f"解析完成：{deck.slide_count} 页 · 媒体 {len(deck.media)}"
              f" · 告警 {len(deck.warnings)}")
    return deck


async def _stage_load(rec: dict, deck_id: str):
    """action="plan"：从盘上读回上次解析的课件。"""
    from deck.store import load_deck

    _enter(rec, "plan", "读取已有课件")
    deck = await asyncio.to_thread(load_deck, deck_id)
    _progress(rec, deck.slide_count, deck.slide_count, f"课件 {deck.slide_count} 页")
    return deck


async def _stage_plan(rec: dict, deck, settings, slides: list[int] | None, *,
                     rule_only: bool = False):
    """备课：逐页调模型（plan_llm 自己会做"这一页失败了用规则兜底"）。

    rule_only=True 时整段不碰模型，直接 plan_rule —— 这就是直连入口
    （run.py plan --rule / 页面上的"规则兜底"）产出的同一种计划。留着这一档，
    是因为没有可用模型时 plan_llm 会**逐页**等模型超时再回退，白等几十分钟，
    最后落到的还是同一份规则计划。
    """
    from script_gen import plan_llm, plan_rule

    if rule_only:
        _enter(rec, "plan", "规则兜底备课（照读课件，不调模型）")
        # 纯本地 CPU 活，但 42 页也要几秒 —— 同步跑会把轮询请求一起堵住。
        plan = await asyncio.to_thread(plan_rule, deck, slides=slides)
        _log(rec, f"规则兜底：{len(plan.units)} 个单元（照读课件，未调模型）")
    else:
        from llm import LLM

        _enter(rec, "plan", "模型备课")
        llm = LLM(settings)

        def _on_progress(done: int, total: int, slide) -> None:
            _progress(rec, done, total, f"备课：第 {slide.index + 1} 页")
            _log(rec, f"第 {done}/{total} 页 {slide.title or '（无标题）'}")

        plan = await plan_llm(deck, llm, slides=slides, on_progress=_on_progress)
    if not plan.units:
        # 空计划写下去等于把用户上一版好好的计划换成空的，这里必须停。
        raise RuntimeError("备课没有产出任何单元 —— 检查模型服务后重试")
    plan = _merge_partial(plan, deck, slides)
    _log(rec, f"备课完成：{len(plan.units)} 个单元 · 告警 {len(plan.warnings)}")
    return plan


async def _stage_routes(rec: dict, plan, deck, settings, *,
                         rule_only: bool = False) -> None:
    """预置路线 + 备份 + 写盘（顺序不能换：备份必须在覆盖写之前）。"""
    from agent.routes import plan_routes, route_entry
    from run import _route_polisher, _save_plan

    _enter(rec, "routes", "预置路线")
    # 润色复用 run._route_polisher：拿不到模型时它自己降级成 None（= 只用规则文案）。
    # 那段降级逻辑（"润色只是加分项，不该让备课失败"）抄一遍就会分叉。
    # rule_only 时直接给 None，连"试一次润色"都不试：没有模型，每次试都是一个完整超时，
    # 十几条路线就是一串白等（run.py plan --rule 的口径也是 llm=None）。
    polisher = None if rule_only else _route_polisher(settings, tag="preprocess")
    # to_thread 不只是为了不堵事件循环：_route_polisher 内部是 asyncio.run(...)，
    # 在正在运行的循环里直接调它会 RuntimeError。
    routes = await asyncio.to_thread(
        lambda: plan_routes(plan, deck, llm=polisher, char_warn=settings.unit_char_warn))
    _log(rec, f"预置路线：{len(routes)} 条")

    # 备份必须在写盘之前：重备崩了（模型吐一半、进程被杀）用户手里还得有一份能上课的计划。
    bak = deck_store.backup_plan(deck.deck_id)
    rec["backup"] = bak.name if bak is not None else None
    if bak is not None:
        _log(rec, f"旧计划已备份 → {bak.name}")

    path = deck_store.plan_json(deck.deck_id)
    _save_plan(path, plan, [route_entry(r) for r in routes])
    _log(rec, f"已写入 {path}")


async def _stage_summarize(rec: dict, *, deck_id: str, settings, slides: list[int] | None,
                           force: bool) -> None:
    """补总结：只写 units[*].summary，正文 / 锚点 / 课件操作一格都不动。

    为什么单独一个 stage 而不是复用备课那条：备课会把整份计划**重新生成**（调模型、
    花钱、几十分钟），而"走向图的一格要一句话"这个需求只差一层皮。已经上过课的
    定稿计划不该为了这一行字被重写一遍。

    stage 报的是 "plan"：作业契约里 stage 只有 queued|ingest|plan|routes|done|failed
    六个值，补总结属于"计划那一格"的活儿；具体在做什么由 label 说（那里是自由的）。
    """
    from deck.store import load_deck
    from llm import LLM
    from run import _save_plan
    from script_gen import LessonPlan, backfill_summaries

    _enter(rec, "plan", "补生成总结")
    deck = await asyncio.to_thread(load_deck, deck_id)
    path = deck_store.plan_json(deck_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    plan = LessonPlan.from_dict(raw)
    keep = set(slides) if slides else None
    if force:
        for u in plan.units:
            if keep is None or u.slide in keep:
                u.summary = ""
    todo = [u for u in plan.units
            if (keep is None or u.slide in keep) and not str(u.summary or "").strip()]
    _progress(rec, 0, len(todo), f"待补 {len(todo)} 个总结")
    _log(rec, f"课件《{deck.title}》{deck.slide_count} 页 / {len(plan.units)} 个单元"
              f" → 待补 {len(todo)} 个" + ("（force：已有的也重写）" if force else ""))
    if not todo:
        # 没有待补的就不写盘：写一遍等于把 plan.json 的 mtime 和备份都白白动一次。
        _progress(rec, len(plan.units), len(plan.units), "没有待补的单元")
        _log(rec, "没有待补的单元，不写盘")
        return

    llm = LLM(settings)

    def _on_progress(done: int, total: int, slide) -> None:
        _progress(rec, done, total, f"补总结：第 {slide.index + 1} 页")
        _log(rec, f"第 {done}/{total} 页 {slide.title or '（无标题）'}")

    res = await backfill_summaries(deck, plan, llm, slides=slides, on_progress=_on_progress)
    for w in res["warnings"]:
        _log(rec, w)
    _log(rec, f"补上 {res['updated']} 个单元 / 跳过 {res['skipped']} 个")

    # routes 必须从**原始文件**里取回来再透传：_save_plan 的 data 来自 plan.to_json()，
    # LessonPlan 根本没有 routes 这个字段。少透传一次，走向图上那十几条支线就凭空消失，
    # 而且没有任何报错 —— 页面照常打开，只是岔路没了。
    kept = raw.get("routes") if isinstance(raw.get("routes"), list) else None
    bak = deck_store.backup_plan(deck_id)
    rec["backup"] = bak.name if bak is not None else None
    if bak is not None:
        _log(rec, f"旧计划已备份 → {bak.name}")
    _save_plan(path, plan, kept)
    filled = sum(1 for u in plan.units if str(u.summary or "").strip())
    _progress(rec, filled, len(plan.units), f"已补 {res['updated']} 个总结")
    _log(rec, f"已写入 {path}（有总结的单元 {filled}/{len(plan.units)}，routes {len(kept or [])} 条）")


def _merge_partial(plan, deck, slides: list[int] | None):
    """只备几页时把其余页的旧单元接回来（照 run.cmd_plan 的口径）。

    为什么：只备第 4 页却把 42 页的计划整份覆盖掉，就是"试一页 = 毁一节课"。
    """
    if not slides:
        return plan
    from script_gen import LessonPlan, measure_plan

    path = deck_store.plan_json(deck.deck_id)
    if not path.exists():
        return plan
    old = LessonPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
    keep = [u for u in old.units if u.slide not in set(slides)]
    plan.units = sorted(keep + plan.units, key=lambda u: u.slide)
    plan.warnings.append(
        f"部分备课（第 {[i + 1 for i in slides]} 页）：其余 {len(keep)} 个单元沿用旧计划")
    plan.coverage = measure_plan(deck, plan)
    return plan


# ===========================================================================
# 内部：源解析 / 记录读写 / 单飞
# ===========================================================================

def _resolve(name: str, *, source_path: Path | None = None) -> tuple[str, Path | None, str]:
    """name → (source, 源文件, deck_id)。

    两种给法都认：源库里的**文件名**（Web 上传后的常态），或**deck_id**（源文件被删/
    改名后只想重新备课）。先按文件名找，找不到再当 deck_id 试 —— deck_id 本来就是从
    文件名派生的，所以源文件还在的时候两者一致。

    按 deck_id 找到时再回退查 `decks/<deck_id>/source.txt` 第一行：仓库里那份真实课件
    当初是直接从仓库根目录 ingest 的，pptx/ 里从来没有过它。少了这条回退，"重新解析+
    备课"在这份最要紧的课件上永远是灰的 —— 用户为了重解析得先手动再传一遍 16MB。

    另一条回退补的是**上传之后、第一次解析之前**那段空窗：这时候 `decks/<id>/` 还不存在
    （所以 deck.json 那条回退不成立），而页面手里只有显示名（title 把 `.pptx` 去掉了、
    全角标点原样留着），源库里的文件却是带后缀的全名 —— 一字之差就是"源库里没有"。
    顺序是按"越接近用户本意越先试"：补后缀 → 按派生 id 认人 → source.txt。
    三条都只认源库里**真实存在**的文件，绝不凭空造一条指向不存在文件的路。

    source_path 是给 bridge 的增量入口（TeachingFlow 出片后的 PPTX 不在 pptx/ 里）：
    它必须**真实存在**才算数，且只认路径、绝不复制 —— 出片产物可能 28MB，
    复制一份只为了走一遍上传逻辑毫无意义。给错了就落回下面那套常规解析，
    不新增"指向不存在文件"的路。
    """
    if source_path is not None:
        given = Path(source_path).expanduser()
        if given.is_file() and given.suffix.lower() == ".pptx":
            # 源文件名与 deck_id 都跟着**这一份**走：作业记录、单飞判定、落点全凭它
            return given.name, given, deck_store.make_deck_id(given.name)
    raw = str(name or "").strip()
    source = _safe_name(raw)
    src = SOURCES_DIR / source
    if src.is_file():
        return source, src, deck_store.make_deck_id(source)
    # ① 显示名 → 源文件全名（"x" → "x.pptx"；已经是全名的不再补一次）
    cands: list[Path | None] = []
    if Path(source).suffix.lower() != ".pptx":
        cands.append(SOURCES_DIR / f"{source}.pptx")
    # ② 派生 id 认人：全角冒号/空格在两边都被压成下划线，逐字比对才认不出
    cands.append(_library_source(deck_store.make_deck_id(source)))
    for cand in cands:
        if cand is not None and cand.is_file():
            # 源文件与 deck_id 都取**盘上那份**的：作业记录、单飞判定、产物落点全跟着它走
            return cand.name, cand, deck_store.make_deck_id(cand.name)
    for did in (deck_store.make_deck_id(raw), _safe_name(raw)):
        if deck_store.deck_json(did).exists():
            return source, _source_from_txt(did), did
    return source, None, deck_store.make_deck_id(raw)


def _source_path_recorded(deck_id: str) -> Path | None:
    """decks/<deck_id>/source.txt 第一行记的源路径 —— **不管它此刻还在不在**。

    deck.store.save_deck 写的是 `{source_path}\nsha1=…\n`。相对路径按 ROOT 解析：
    命令行里用相对路径跑出来的 deck，换个工作目录打开页面也还得认得它。
    """
    txt = deck_store.deck_dir(deck_id) / "source.txt"
    try:
        first = txt.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not first:
        return None
    p = Path(first)
    return p if p.is_absolute() else (ROOT / p)


def _source_from_txt(deck_id: str) -> Path | None:
    """source.txt 记的那条路径，且它此刻**仍然存在**（否则一律当没有）。"""
    p = _source_path_recorded(deck_id)
    return p if p is not None and p.is_file() else None


def _library_source(deck_id: str) -> Path | None:
    """pptx/ 里与这个 deck 对得上的源文件：派生出的 deck_id 相同就算同一份。

    比"文件名逐字相等"松一档是有意的 —— 页面要回答的是"这份课件还能不能重新解析"，
    而 _resolve 正是按派生 id 认人的。
    """
    if not SOURCES_DIR.is_dir():
        return None
    for p in sorted(SOURCES_DIR.iterdir()):
        if (p.is_file() and p.suffix.lower() == ".pptx"
                and deck_store.make_deck_id(p.name) == deck_id):
            return p
    return None


def _safe_name(filename: str) -> str:
    """上传文件名 → 纯文件名：去掉目录部分、`..` 与非法字符，中文原样保留。

    为什么不用 werkzeug 的 secure_filename：它会把中文整段吃掉（"词汇解析.pptx" 只剩
    "" 或 ".pptx"），而 deck_id 是文件名派生的 —— 中文一丢，所有课件都是同一个 deck。
    """
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _BAD_NAME.sub("_", name).strip()
    while ".." in name:
        # 去掉目录分隔符之后 ".." 已经穿不出去，但留着容易让审核的人看串，
        # 顺手压成一个点（"a..b.pptx" → "a.b.pptx"）。
        name = name.replace("..", ".")
    name = name.strip(". ")
    return name[:120] or "upload.pptx"


def plan_stats(deck_id: str) -> dict:
    """盘上那份 plan.json 的规模：几个单元、其中几个已经有总结、有没有整节课总览。

    为什么单列一个函数而不是散在 _entry 与 deck_rows 里：这两处最后并成**同一张表**，
    口径一旦分叉，同一行就会出现"源库说已补 3/5、课件库说 0/5"。坏文件只填 error。
    """
    out = {"units": 0, "summarized": 0, "has_goals": False, "error": None}
    path = deck_store.plan_json(deck_id)
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:         # 坏文件只填 error，不许把整页课件库带崩
        out["error"] = f"plan.json 读不动：{type(e).__name__}: {e}"
        return out
    units = data.get("units") if isinstance(data, dict) else None
    units = units if isinstance(units, list) else []
    out["units"] = len(units)
    out["summarized"] = sum(1 for u in units
                            if isinstance(u, dict) and str(u.get("summary") or "").strip())
    # plan.json 没有单独的 summary 顶层字段：整节课的总览就是 session_goals
    # （备课阶段从首页/目录页推出来的），空数组 = 没备出总览。
    out["has_goals"] = bool(isinstance(data, dict) and data.get("session_goals"))
    return out


def _entry(src: Path) -> dict:
    """一条源库记录：文件事实 + 它对应 deck/plan 的状态。

    has_summary 已改名为 has_goals（值完全不变），必须知道为什么：
    单元级 unit.summary 上线之后，"整节课有一份总览（session_goals 非空）"与
    "总结补全了（units[*].summary 有几个非空）"是两个不同的东西。继续叫 has_summary，
    页面就会把前者显示成后者 —— 一份 177 单元、0 条 summary 的课件会被显示成"已补全"，
    用户点进走向图却一个字都没有。所以前者叫 has_goals，后者由 summarized/units 两个
    数字回答（见 plan_stats）。
    """
    deck_id = deck_store.make_deck_id(src.name)
    base = deck_store.deck_dir(deck_id)
    row = {
        "name": src.name,
        "size": 0,
        "mtime": 0.0,
        "deck_id": deck_id,
        "has_deck": (base / "deck.json").exists(),
        "has_plan": (base / "plan.json").exists(),
        "units": 0,
        "summarized": 0,
        "has_goals": False,
        "error": None,
    }
    try:
        st = src.stat()
        row["size"], row["mtime"] = st.st_size, st.st_mtime
    except OSError as e:            # 扫描过程中被删、被别的进程占住
        row["error"] = f"{type(e).__name__}: {e}"
    if row["has_plan"]:
        st = plan_stats(deck_id)
        row.update(units=st["units"], summarized=st["summarized"],
                   has_goals=st["has_goals"], error=st["error"])
    return row


def _new_record(action: str, source: str, deck_id: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "action": action,
        "source": source,
        "deck_id": deck_id,
        "stage": "queued",
        "status": "running",
        "progress": {"done": 0, "total": 0, "label": "排队"},
        "log": deque(maxlen=LOG_LINES),
        "backup": None,
        "error": None,
        "started_at": _now(),
        "finished_at": None,
        "task": None,
    }


def _public(rec: dict) -> dict:
    """对外快照：只有 Web 契约里的那十一个字段，内部键（log deque / task）不外泄。

    log_tail 与 progress 各拷一份：作业还在往后写，外部拿到的是"这一刻"的快照，
    不会读到一半发现多了几行，也改不动作业自己的状态。
    """
    return {
        "id": rec["id"],
        "action": rec["action"],
        "source": rec["source"],
        "deck_id": rec["deck_id"],
        "stage": rec["stage"],
        "status": rec["status"],
        "progress": dict(rec["progress"]),
        "log_tail": list(rec["log"]),
        "backup": rec["backup"],
        "error": rec["error"],
        "started_at": rec["started_at"],
        "finished_at": rec["finished_at"],
    }


def _reject(action: str, source: str, deck_id: str, message: str) -> dict:
    """没跑起来的作业也留一条记录：返回的 dict 与正常作业同形，原因在 error/log_tail。"""
    rec = _new_record(action, source, deck_id)
    rec["stage"], rec["status"] = "failed", "failed"
    rec["error"] = message
    rec["finished_at"] = _now()
    _log(rec, f"❌ {message}")
    _jobs[rec["id"]] = rec
    _prune()
    return _public(rec)


def _running_for(source: str, deck_id: str) -> dict | None:
    """单飞判定：同一份源正在跑就不许再开一个。

    除了 source 也认 deck_id —— 两个不同的文件名可能派生同一个 deck_id
    （"a b.pptx" 与 "a_b.pptx"），它们抢的其实是**同一份** plan.json，
    只看 source 会漏掉这种情况。
    """
    for rec in _jobs.values():
        if rec["status"] == "running" and (rec["source"] == source
                                           or rec["deck_id"] == deck_id):
            return _public(rec)
    return None


def _prune() -> None:
    """只留最近 MAX_JOBS 条：作业记录在进程内存里，页面开一天也不会自己释放。

    只删已经结束的 —— 删掉 running 的那条，前端就再也查不到它在跑什么了。
    """
    while len(_jobs) > MAX_JOBS:
        for jid, rec in _jobs.items():
            if rec["status"] != "running":
                del _jobs[jid]
                break
        else:
            return


def _log(rec: dict, line: str) -> None:
    """写一行作业日志。deque(maxlen=200) 自己丢最旧的，不用手工裁。"""
    rec["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {line}")


def _enter(rec: dict, stage: str, label: str) -> None:
    """进入一个 stage：换 stage 与 label，done/total **保留**不动。

    为什么不把进度清零：清零之后 UI 在"预置路线"那一段只能显示 0/0，而它真正该说的是
    "42/42 页已经备完，正在铺岔路"；更要紧的是清零会把**结束时的最终进度**也变成 0/0 ——
    页面跑到最后反而看不出这节课备了多少页。
    """
    rec["stage"] = stage
    rec["progress"] = {"done": rec["progress"]["done"],
                       "total": rec["progress"]["total"], "label": label}
    _log(rec, f"[{stage}] {label}")


def _progress(rec: dict, done: int, total: int, label: str) -> None:
    rec["progress"] = {"done": int(done), "total": int(total), "label": label}


def _now() -> str:
    """ISO 时间串（与 plan.created_at / deck.ingested_at 同一口径，UI 直接显示）。"""
    return datetime.now().isoformat(timespec="seconds")
