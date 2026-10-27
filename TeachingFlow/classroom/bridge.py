"""把任意路径的 .pptx 接进课堂预处理链：TeachingFlow 出片 → 课堂备课。

为什么需要这一层
----------------
课堂自己的作业入口是 preprocess.start_job(name)，name 只认 pptx/ 下的文件名、
decks/<id>/source.txt 里记的那条路径，或者（本模块用上的）显式 source_path ——
出片链路的产物长在 runs/<日期>/<课程>/第N章-章名/第M单元-单元名.pptx，从来不在
pptx/ 里。两条路可选：

  a) 把 pptx 拷进 pptx/ 再调 start_job —— 一份 28MB 的课件等于复制一遍，而课堂
     只是想解析它（解析产物本来就落在 decks/<deck_id>/）；
  b) 给 start_job 一个"源文件就在这儿"的入口。

本模块走 b。所以这里**一行 ingest / 备课逻辑都不复制**：解析、备课、预置路线、
备份、写盘、单飞（同一份源只许一个作业）、失败不冒泡，全部由 preprocess 的既有
阶段与作业记录承担。bridge 只做三件事：把路径交进去、等作业结束、把结果折成
flow 能直接写进 manifest 的形状。

三条纪律（与 preprocess 模块 docstring 同一套口径，这里不破例）
-----------------------------------------------------------
1. **绝不抛异常**：调用方（flow.py）正站在一串已经产出的 PPTX 中间，一份失败只该
   多一条记录，不该改写整次运行的结论。所有错误都写进返回值的 error。
2. **不复制源文件**：见上。
3. **不写 os.environ**：要临时换服务商就换 Settings，不改进程环境 —— 同一个进程里
   可能正上着课（课堂 Web 服务是常驻的）。

没有可用模型时怎么办（规则兜底，与直连入口同一口径）
---------------------------------------------------
备课要模型，解析不要。所以拿不到可用模型时 action="full" 走**规则兜底**：
解析完用 script_gen.plan_rule 本地切单元（照读课件、秒级、不联网），plan.json
照常落盘，返回 ok=True + degraded=True + 一句可读的 note。

为什么不是"只解析就收工"（这一版改口径的原因）：直连入口（run.py plan /
/api/preprocess/start action=plan）在模型不可用时本来就是按页回退成规则计划，
bridge 若停在只有 deck.json，同一份 pptx 走出片链路和走网页会得到两份不同的产物 ——
而"照读课件"至少是一节能上的课。配好 key 后再跑一次 action="plan" 就换成正式备课，
不必重新解析。

反过来 action="plan"/"summarize" 本来就只有模型那一段活儿，没模型就如实
ok=False —— 那种情况报"成功"是骗人。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import preprocess
from config import deepseek_profile, load_settings
from deck import store as deck_store

#: 轮询作业进度的间隔。解析一份 42 页课件要几十秒，这个频率只是让 stages 记得细一点
POLL_SEC = 0.2

#: 本地服务商探活的连接超时。ollama 没起来时是"立刻 refused"，0.4s 只是兜底，
#: 不能更长 —— 备课还没开始就先卡几秒，用户会以为课堂挂了。
PROBE_SEC = 0.4

#: 这些主机名算"本地服务"（要真的探活）：远程的探活只会白等一轮超时
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "")

_ACTIONS = ("full", "plan", "ingest", "summarize")

#: preprocess._log 写的是 "HH:MM:SS [stage] 说明" —— 阶段时间线从这里回读
_LOG_STAGE = re.compile(r"^\S+\s+\[([a-z]+)\]")


def _blank(src, action: str) -> dict:
    """返回值的空壳 —— 每个出口都从它出发，字段一个都不缺。"""
    return {"ok": False, "deck_id": "", "source": str(src), "action": action,
            "error": None, "stages": [], "stats": {},
            "degraded": False, "note": ""}


def _say(log, line: str) -> None:
    """旁路日志：调用方的 print 挂了也不该把备课判成失败。"""
    if log is None:
        return
    try:
        log(line)
    except Exception:
        pass


def _host_port(base_url: str) -> tuple[str, int]:
    u = urlparse(base_url if "//" in base_url else "//" + base_url)
    port = u.port or (443 if u.scheme == "https" else 80)
    return (u.hostname or ""), port


def model_ready(settings) -> tuple[bool, str]:
    """这份配置现在能不能真去调模型：返回 (可用, 不可用的原因)。

    判据两条，都是"别浪费一次作业"：
      1. 服务商配了 key（ollama 的 api_key 是占位值 "ollama"，所以对它是"有没有
         真起来"，看第二条）；
      2. **本地**服务商还要真连得上端口。远程的不探活：没网时探活只是白等一轮，
         而真连不上时 plan 阶段自己的报错更准（也更快给出人话）。
    """
    try:
        prof = settings.active
    except SystemExit as exc:              # LLM_PROVIDER 写了个不认识的
        return False, str(exc)
    if not prof.api_key:
        return False, f"{prof.name} 没配 api_key"
    host, port = _host_port(prof.base_url)
    if host in _LOCAL_HOSTS:
        try:
            socket.create_connection((host or "127.0.0.1", port), timeout=PROBE_SEC).close()
        except OSError:
            return False, f"{prof.name} 连不上（{prof.base_url}）—— 本地模型服务没起来"
    return True, ""


def pick_settings(settings=None):
    """挑一份这次备课真能用的配置：返回 (Settings 或 None, 说明)。

    顺序是"用户说的 → 当前配置的 → 仓库根那把 key 能开的"：
      - 显式传了 settings：只用它。调用方点了名，不替它改；
      - 否则看 LLM_PROVIDER 那家；它连不上而仓库根 .env 里有 DEEPSEEK_API_KEY，
        就临时用 deepseek（"课堂共享 MAT 那份 key"的落点，且不动 os.environ）；
      - 都没有 → None，调用方走规则兜底（plan_rule，不调模型）。
    """
    if settings is not None:
        ok, why = model_ready(settings)
        return (settings if ok else None), why
    cur = load_settings()
    ok, why = model_ready(cur)
    if ok:
        return cur, ""
    ds = deepseek_profile()
    if ds.api_key:
        return replace(cur, provider="deepseek",
                       providers={**cur.providers, "deepseek": ds}), ""
    return None, why


def _stats(deck_id: str) -> dict:
    """落盘结果的规模：几页、几单元、几个已补总结 —— 与课件库同一口径（plan_stats）。"""
    st = preprocess.plan_stats(deck_id)
    has_deck = deck_store.deck_json(deck_id).exists()
    slides = 0
    if has_deck:
        try:
            raw = json.loads(deck_store.deck_json(deck_id).read_text(encoding="utf-8"))
            slides = len(raw.get("slides") or [])
        except Exception:
            slides = 0          # 坏文件只是这个数字不准，不该把备课结果判成失败
    return {"slides": slides, "units": st["units"], "summarized": st["summarized"],
            "has_goals": st["has_goals"], "has_deck": has_deck,
            "has_plan": deck_store.plan_json(deck_id).exists(),
            "error": st["error"]}


async def _wait(job_id: str, *, timeout=None) -> tuple[dict | None, list[str], bool]:
    """等作业结束，顺手记下经过的 stage。

    为什么轮询而不是 preprocess.wait_job：wait_job 一等等到底，中间什么都不回；
    而 manifest 里想留的是"这份课件按 ingest → plan → routes 走完了"，得自己看。
    """
    seen: list[str] = []
    started = time.monotonic()
    while True:
        snap = preprocess.get_job(job_id)
        if snap is None:
            return None, seen, False
        stage = str(snap.get("stage") or "")
        if stage and (not seen or seen[-1] != stage):
            seen.append(stage)
        if snap.get("status") != "running":
            return snap, seen, False
        if timeout is not None and time.monotonic() - started > timeout:
            return snap, seen, True
        await asyncio.sleep(POLL_SEC)


def _stage_timeline(snap: dict, polled: list[str]) -> list[str]:
    """这份课件实际走过哪几个阶段。

    为什么要读 log_tail：作业常常在两个轮询之间就跑完了，ingest/plan/routes 这几个
    stage 一闪而过 —— 只信轮询的话，一份解析+备课全跑完的作业会记成
    ["queued", "done"]，看不出它干过什么。preprocess._enter 每进一个 stage 都往日志
    里写一行 "HH:MM:SS [stage] 说明"，那里才是真正的时间线。
    """
    seq = ["queued"] + _logged_stages(snap) + list(polled) + [str(snap.get("stage") or "")]
    out: list[str] = []
    for stage in seq:
        if stage and stage not in out:
            out.append(stage)
    return out


def _logged_stages(snap: dict) -> list[str]:
    out: list[str] = []
    for line in snap.get("log_tail") or []:
        m = _LOG_STAGE.match(str(line))
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


async def preprocess_pptx(src, *, action: str = "full", settings=None, log=None,
                          timeout=None) -> dict:
    """把 src（任意路径的 .pptx）跑成 decks/<deck_id>/{deck.json,plan.json}。

    返回 {ok, deck_id, source, action, error, stages, stats, degraded, note}，
    **任何情况都不抛异常**（见模块 docstring 纪律 1）。

    action 与 preprocess.start_job 同义：full 全链 / ingest 只解析 / plan 只备课 /
    summarize 只补总结。没可用模型时 full 走规则兜底（degraded=True，照读课件不调模型）。
    """
    action = str(action or "full").strip().lower()
    out = _blank(src, action)
    try:
        if action not in _ACTIONS:
            out["error"] = f"不认识的 action：{action!r}（只认 {'/'.join(_ACTIONS)}）"
            return out
        path = Path(src).expanduser()
        if not path.is_file():
            out["error"] = f"源文件不存在：{path}"
            return out
        if path.suffix.lower() != ".pptx":
            out["error"] = f"只认 .pptx（这份是 {path.name!r}）—— 解析器读不了别的格式"
            return out

        # 只有真要调模型的那几档才看模型：只解析的作业不该因为一份坏 .env 而失败
        rule_only = False
        if action != "ingest":
            chosen, why = pick_settings(settings)
            if chosen is None:
                if action == "full":
                    # 不放进 plan 阶段去"逐页试模型再回退"：那要按页等超时，
                    # 换来的还是同一份规则计划（见模块 docstring）。
                    rule_only = True
                    out["degraded"] = True
                    out["note"] = (f"未配置可用模型（{why}）→ 规则兜底（照读课件）："
                                   "解析与本地切单元照跑，plan.json 已落盘；"
                                   "配好 key 后再跑 action='plan' 可换正式备课")
                    _say(log, out["note"])
                else:
                    out["error"] = (f"未配置可用模型（{why}）—— "
                                    f"action={action!r} 就是模型那一段活儿")
                    return out
            else:
                settings = chosen

        _say(log, f"交作业：{path.name}（action={action}"
                  + ("，规则兜底" if rule_only else "") + "）")
        job = await preprocess.start_job(path.name, action=action,
                                        source_path=path, settings=settings,
                                        rule_only=rule_only)
        out["deck_id"] = str(job.get("deck_id") or "")
        if job.get("status") == "failed":
            # 被拒也是作业记录的一种（源不存在/单飞/缺 deck.json…），原因在 error
            out["error"] = str(job.get("error") or "作业被拒（没写原因）")
            out["stages"] = [str(job.get("stage") or "")]
            return out
        _say(log, f"作业 {job.get('id')} 已开始：deck_id={out['deck_id']}")

        snap, stages, timed_out = await _wait(job["id"], timeout=timeout)
        if snap is not None:
            stages = _stage_timeline(snap, stages)
        out["stages"] = stages
        if snap is None:
            out["error"] = "作业记录不见了（进程内作业表被裁剪？）"
            return out
        out["deck_id"] = str(snap.get("deck_id") or out["deck_id"])
        if timed_out:
            # 超时不能只报一句错就走：作业还在后台跑，而它会把这份源的单飞一直占着
            # （之后每个作业都被"正在预处理"拒掉）。显式取消，并等它把状态收尾。
            last = await preprocess.cancel_job(
                job["id"], reason=f"bridge 等待超时（>{timeout}s）")
            if last is not None:
                stages = _stage_timeline(last, stages)
                out["stages"] = stages
            out["error"] = (f"等待超时（>{timeout}s，最后阶段 {(last or snap).get('stage')}）"
                            "—— 作业已取消，重跑一次即可")
            return out
        if snap.get("status") != "done":
            out["error"] = str(snap.get("error")
                               or f"作业未完成（status={snap.get('status')}）")
            return out
        out["stats"] = _stats(out["deck_id"])
        out["ok"] = bool(out["stats"]["has_deck"])
        if not out["ok"]:
            out["error"] = "解析没有落盘 deck.json"
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def preprocess_pptx_sync(src, **kw) -> dict:
    """同步包装：flow.py 这类同步调用方用。

    当前线程**已经有跑着的事件循环**时（uvicorn/FastAPI 里被同步调用），
    asyncio.run 会直接 RuntimeError("asyncio.run() cannot be called from a running
    event loop")。那种情况丢进**新线程 + 新 loop** 跑：作业链需要的只是一个能
    create_task 的循环，与调用方的循环互不干涉（反过来在别人的循环里嵌套跑才是
    真会炸）。线程是 daemon：调用方被 kill 时不会卡在 join 上。
    """
    def _run() -> dict:
        return asyncio.run(preprocess_pptx(src, **kw))

    def _failed(exc: BaseException) -> dict:
        out = _blank(src, str(kw.get("action") or "full").strip().lower())
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    try:
        asyncio.get_running_loop()
    except RuntimeError:                    # 常规路径：没人在跑循环，直接开一个
        try:
            return _run()
        except Exception as exc:
            return _failed(exc)

    box: dict = {}

    def _target() -> None:
        try:
            box["result"] = _run()
        except BaseException as exc:        # 线程里的异常没人接就会静默消失
            box["error"] = exc

    thread = threading.Thread(target=_target, name="classroom-bridge", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        return _failed(box["error"])
    return box.get("result") or _blank(src, str(kw.get("action") or "full").strip().lower())
