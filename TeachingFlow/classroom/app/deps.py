"""应用层依赖：数据库连接、配置快照、内容管线（W3）适配、TTS。

三处刻意的"不直接 import"：

1. **W3 的三个模块惰性导入**（`studio.catalog` / `materialize` / `outline`）。
   它们是并行开发的另一条线，还没落地时 W1 必须照样能起、能跑测试 ——
   于是这里 import 失败**不是异常**，而是"内容管线未就绪"这一条可解释的降级。
2. **TTS / LLM 不在导入期构造**：`edge-tts` 缺包、没 key、没网时构造失败会连
   `/api/health` 一起打死，而健康检查恰恰是最该活着的那个路由。
3. **配置快照按进程缓存一次**：`load_settings()` 每次都会读 .env 与 os.environ，
   每个请求读一次既慢又会让"改了配置不生效"和"改了立刻生效"变得不可预测。
   要刷新就重启服务（或调 `reset_settings_cache()`）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator

from store import db as db_mod

log = logging.getLogger("classroom.app")

APP_VERSION = "1.0.0"

#: 引擎目录（TeachingFlow/classroom）；引擎模块都是顶层模块名，靠 sys.path 找到
CLASSROOM_ROOT = db_mod.CLASSROOM_ROOT
#: TeachingFlow/runs —— 课程来源
RUNS_ROOT = CLASSROOM_ROOT.parent / "runs"
#: TeachingFlow/state —— 上传的 pptx、真库都在这里（gitignored 的运行产物区）
STATE_ROOT = CLASSROOM_ROOT.parent / "state"
#: 前端产物
DIST_ROOT = CLASSROOM_ROOT / "webapp" / "dist"


# ===========================================================================
# 数据库
# ===========================================================================

#: 已经迁移过的库路径（进程内）。迁移是幂等的，但每条连接都跑一遍 executescript
#: 会让每个请求多几毫秒，而且 WAL 文件会被反复 touch。
_MIGRATED: set[str] = set()


def ensure_db(conn) -> None:
    key = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if key in _MIGRATED:
        return
    db_mod.migrate(conn)
    _MIGRATED.add(key)


def get_conn() -> Iterator[Any]:
    """FastAPI 依赖：每个请求一条连接（自动迁移 + 自动关闭）。"""
    conn = db_mod.connect()
    try:
        ensure_db(conn)
        yield conn
    finally:
        conn.close()


# ===========================================================================
# 配置快照
# ===========================================================================

_settings = None


def get_settings():
    """当前配置（进程内缓存）。配置坏了也只降级，不抛到路由外面。"""
    global _settings
    if _settings is not None:
        return _settings
    from config import load_settings

    try:
        _settings = load_settings()
    except Exception as e:                      # .env 写坏了 / 服务商不认识
        log.error("读取配置失败（engine=degenerate）：%s: %s", type(e).__name__, e)
        _settings = None
    return _settings


def reset_settings_cache() -> None:
    global _settings
    _settings = None


def boot() -> None:
    """启动自检：迁移库 + 保证有一个可用档案 + 打一行"我在哪、开着什么"。

    任何一步失败都只记日志：数据库坏了的时候，最该活着的就是 `/api/health`
    —— 它要是跟着退出，人看到的只是"服务起不来"，而不知道是库的问题。
    """
    try:
        conn = db_mod.connect()
        try:
            ensure_db(conn)
            from store import repo

            repo.ensure_default_profile(conn)
        finally:
            conn.close()
    except Exception as e:
        log.error("启动自检失败 db=%s err=%s: %s", db_mod.db_path(), type(e).__name__, e)
    s = get_settings()
    log.info("启动完成 version=%s db=%s dev=%s provider=%s tts=%s dist=%s",
             APP_VERSION, db_mod.db_path(), dev_enabled(),
             (s.provider if s is not None else "?"), tts_configured(),
             (DIST_ROOT / "index.html").is_file())


def unit_ready(unit_row: dict) -> bool:
    """这个单元备好了没有。

    判据是"备过一次"（prepared_at 或 plan_source 非空），不是"磁盘上的文件此刻还在"：
    deck.json / plan.json 会被清缓存清掉，拿它当判据的话列表会在两次扫描之间闪烁，
    而"我备过这节课"这件事学生记得比磁盘准。
    """
    return (bool(str(unit_row.get("plan_source") or "").strip())
            or bool(str(unit_row.get("prepared_at") or "").strip()))


def engine_info() -> dict:
    """`/api/health` 与 `hello` 用的引擎指纹。取不到就给空字符串，绝不抛。"""
    s = get_settings()
    if s is None:
        return {"provider": "", "model": "", "tts": False}
    model = ""
    try:
        model = s.active.model
    except Exception:
        model = ""
    return {"provider": s.provider, "model": model, "tts": tts_configured()}


# ===========================================================================
# TTS
# ===========================================================================

_tts_cache: dict[str, Any] = {}


def tts_for(rate: str | None = None):
    """按语速取一个 TTS 实例（同语速复用）。

    TTS 的缓存键包含 rate，所以"1.2 倍速"与"正常语速"是两份不同的音频，
    互不覆盖 —— 学生切回去时不会听到上一档语速的残留。
    """
    s = get_settings()
    if s is None:
        return None
    key = rate or ""
    if key in _tts_cache:
        return _tts_cache[key]
    try:
        s.cache_dir.mkdir(parents=True, exist_ok=True)
        from speech import TTS

        tts = TTS(s.tts_enabled, s.tts_voice, rate or s.tts_rate, s.cache_dir)
    except Exception as e:                      # 没装 edge-tts 等
        log.warning("TTS 不可用（降级为纯文字）：%s: %s", type(e).__name__, e)
        tts = None
    _tts_cache[key] = tts
    return tts


def tts_configured() -> bool:
    """配置里 TTS 是开着的吗（**不构造实例**）。

    `/api/health` 会调它，而健康检查不该有副作用：构造 TTS 会在磁盘上建缓存目录
    并清理残留文件。要求学生"每 10 秒查一次健康"顺手改磁盘，是那种平时看不出来、
    出事时又查不出来的问题。
    """
    s = get_settings()
    return bool(getattr(s, "tts_enabled", False)) if s is not None else False


def rate_string(value: float) -> str:
    """0.8 / 1 / 1.2 → edge-tts 的 '+0%' / '-20%' / '+20%'。"""
    pct = int(round((float(value) - 1.0) * 100))
    return f"{pct:+d}%"


# ===========================================================================
# 内容管线（W3）适配
# ===========================================================================
#
# W3 的三个模块对本层来说只有四个入口：scan_runs / unit_from_pptx /
# prepare_unit / build_outline。这里额外做一件"按实际签名挑实参"的事，
# 因为 **两份任务书的签名不一致**：
#   · 设计文档 §5：prepare_unit(unit_row) / build_outline(unit_row)
#   · W3 任务书  ：prepare_unit(pptx_path, *, deck_root=None) / build_outline(deck_dir)
# 谁对都行 —— 调用点不该押注，于是按被调函数的**真实参数名**挑值。

_ALIASES: dict[str, tuple[str, ...]] = {
    "pptx": ("pptx", "pptx_path", "_default"),
    "pptx_path": ("pptx_path", "pptx", "_default"),
    "unit_row": ("unit_row", "row", "unit", "_default"),
    "row": ("row", "unit_row", "_default"),
    "deck_dir": ("deck_dir", "deck_path", "_default"),
    "runs_root": ("runs_root", "root", "_default"),
    "path": ("path", "_default", "pptx_path"),
    "course_id": ("course_id",),
    "title": ("title", "name"),
    "chapter": ("chapter",),
    "order": ("order", "order_index"),
    "order_index": ("order_index", "order"),
    "force": ("force",),
    "deck_root": ("deck_root",),
}


def _w3(module_name: str, attr: str):
    """惰性取 W3 的函数；模块还没落地就返回 None（不是错误）。"""
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        log.info("内容管线未就绪 module=%s (%s)", module_name, type(e).__name__)
        return None
    fn = getattr(mod, attr, None)
    if fn is None:
        log.info("内容管线缺函数 %s.%s", module_name, attr)
    return fn


def _adapt_call(fn, values: dict[str, Any]):
    """按 fn 的真实参数名（含别名）挑实参调用。"""
    signature = inspect.signature(fn)
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        found = None
        for alias in _ALIASES.get(name, (name,)):
            if alias in values:
                found = values[alias]
                break
        if found is None and param.default is not inspect.Parameter.empty:
            continue
        if found is None:
            raise TypeError(f"无法为参数 {name} 提供实参")
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            args.append(found)
        else:
            kwargs[name] = found
    return fn(*args, **kwargs)


def _obj_fields(obj: Any) -> dict:
    """W3 的返回可能是 dataclass 也可能是 dict —— 统一成 dict。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    out = {}
    for name in ("id", "title", "subject", "source", "run_dir", "units", "chapter",
                 "order_index", "pptx_path", "slides", "est_sec", "warnings", "ignored"):
        if hasattr(obj, name):
            out[name] = getattr(obj, name)
    return out


def _unit_dict(unit: Any) -> dict:
    data = _obj_fields(unit)
    data.setdefault("id", "")
    data.setdefault("chapter", "")
    data.setdefault("title", "")
    data.setdefault("order_index", 0)
    # W3 的字段名是 pptx_path；万一哪天改成 path 也认
    data.setdefault("pptx_path", data.get("path") or "")
    return data


def scan_runs(runs_root: Path | None = None) -> tuple[list[dict], list[str]]:
    """扫描 runs 目录 → (课程 dict 列表, warnings)。W3 未就绪返回 ([], [解释])。"""
    fn = _w3("studio.catalog", "scan_runs")
    if fn is None:
        return [], ["内容管线未就绪（studio/catalog.py 缺失）：本次扫描没有结果"]
    root = Path(runs_root or RUNS_ROOT)
    try:
        result = _adapt_call(fn, {"runs_root": root, "root": root, "_default": root})
    except Exception as e:
        log.exception("scan_runs 失败")
        return [], [f"扫描失败：{type(e).__name__}: {e}"]
    # W3 的 scan_runs 返回 ScanResult（list 的子类，另带 warnings / ignored 账本）。
    # 账本要原样往上带：mock 运行没有交付件是常态，不解释的话"扫描完了 0 门课"
    # 看着像功能坏了。
    warnings = list(getattr(result, "warnings", []) or [])
    ignored = int(getattr(result, "ignored", 0) or 0)
    if ignored:
        warnings.append(f"有 {ignored} 个目录看着像课程但没有交付件（pptx），已跳过")
    courses = [_obj_fields(c) for c in (result or [])]
    for c in courses:
        c["units"] = [_unit_dict(u) for u in (c.get("units") or [])]
    return courses, warnings


def unit_from_pptx(pptx: Path, *, course_id: str, title: str, chapter: str,
                   order: int) -> tuple[dict, list[str]]:
    """一份 pptx → 单元 dict。W3 未就绪时用内置兜底（只数页数，不解析内容）。"""
    fn = _w3("studio.catalog", "unit_from_pptx")
    values = {"pptx_path": Path(pptx), "pptx": Path(pptx), "_default": Path(pptx),
              "course_id": course_id, "title": title, "chapter": chapter,
              "order": order, "order_index": order}
    if fn is not None:
        try:
            return _unit_dict(_adapt_call(fn, values)), []
        except Exception as e:
            log.exception("unit_from_pptx 失败，改用内置兜底")
            warn = f"内容管线解析失败，已退回只数页数：{type(e).__name__}: {e}"
    else:
        warn = "内容管线未就绪（studio/catalog.py 缺失）：单元只登记文件名与页数"
    return _fallback_unit_from_pptx(Path(pptx), course_id=course_id, title=title,
                                    chapter=chapter, order=order), [warn]


def _fallback_unit_from_pptx(pptx: Path, *, course_id: str, title: str, chapter: str,
                             order: int) -> dict:
    """W3 缺席时的最小登记：id 用路径哈希（稳定）、页数用 python-pptx 数。

    这不是"第二个解析器"：它不产出 deck.json / plan.json，只保证"导入能落库、
    UI 能看到一行"，真正的解析仍然只有 W3 一条路。
    """
    slides = None
    try:
        from pptx import Presentation

        slides = len(Presentation(str(pptx)).slides)
    except Exception:
        slides = None
    digest = hashlib.sha1(str(pptx.resolve()).encode("utf-8")).hexdigest()[:12]
    return {
        "id": f"u-{digest}",
        "course_id": course_id,
        "title": title or pptx.stem,
        "chapter": chapter or "",
        "order_index": int(order),
        "pptx_path": str(pptx),
        "slides": slides,
        "est_sec": None,
    }


#: 绝对路径 token（Windows 盘符 / POSIX 根）。警告里带路径是排障需要，
#: 但把本机路径原样甩到界面上既不可读、也把作者机器的目录结构泄给了学生。
_ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/])[^\s，。；）」'\"、]+")
#: URL 也是"带 //": 必须先在替换前保护起来，否则 `http://127.0.0.1:11434/v1`
#: 会被压成 `httpv1` —— 警告里那句"哪个地址连不上"就废了。
_URL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s，。；）」'\"、]+")


def _shorten_paths(text: str) -> str:
    """把警告里的绝对路径压成文件名（"C:\\a\\b\\第一课.pptx" → "第一课.pptx"）。"""
    raw = str(text or "")
    if not raw:
        return raw
    stashed: dict[str, str] = {}

    def _stash(match: "re.Match[str]") -> str:
        key = f"\x00{len(stashed)}\x00"
        stashed[key] = match.group(0)
        return key

    protected = _URL_TOKEN.sub(_stash, raw)

    def _name(match: "re.Match[str]") -> str:
        token = match.group(0).replace("/", "\\").rstrip("\\")
        return token.rsplit("\\", 1)[-1] or match.group(0)

    out = _ABS_PATH.sub(_name, protected)
    for key, url in stashed.items():
        out = out.replace(key, url)
    return out


def prepare_unit(unit_row: dict) -> dict:
    """准备一个单元（解析 deck + 备课）。契约要求的五个字段一定都在。"""
    empty = {"ok": False, "deck_ready": False, "plan_ready": False,
             "plan_source": "", "degraded": True, "warnings": []}
    fn = _w3("studio.materialize", "prepare_unit")
    pptx = Path(unit_row.get("pptx_path") or "")
    if fn is None:
        empty["warnings"] = ["内容管线未就绪（studio/materialize.py 缺失）：这个单元还没有准备"]
        return empty
    if not str(pptx) or not pptx.is_file():
        empty["warnings"] = [_shorten_paths(f"课件文件不存在：{pptx or '（空路径）'}")]
        return empty
    values = {"_default": pptx, "pptx_path": pptx, "pptx": pptx,
              "unit_row": unit_row, "row": unit_row}
    try:
        result = _obj_fields(_adapt_call(fn, values))
    except Exception as e:
        log.exception("prepare_unit 失败")
        empty["warnings"] = [_shorten_paths(f"准备失败：{type(e).__name__}: {e}")]
        # 失败也要给得出数值：盘上可能已经有上一次的 deck/plan（"备过，但这次没跑"）
        empty["slides"], empty["est_sec"] = deck_totals(unit_row)
        return empty
    out = {
        "ok": bool(result.get("ok", False)),
        "deck_ready": bool(result.get("deck_ready", False)),
        "plan_ready": bool(result.get("plan_ready", False)),
        "plan_source": str(result.get("plan_source") or ""),
        "degraded": bool(result.get("degraded", False)),
        # 内容管线的警告里几乎必然带绝对路径（"课件不在盘上：C:\…"），
        # 它会被前端原样显示在单元页上 —— 压成文件名就够读了。
        "warnings": [_shorten_paths(w) for w in (result.get("warnings") or [])],
    }
    if "deck_id" in result:
        out["deck_id"] = result["deck_id"]
    # slides / est_sec **永远是数值**（前端拿它们显示"几页 / 讲多久"）。
    # 不能直接信上游：复用已备好的 deck 那条路径上，上游可能给 0/None
    # （它只反映"这次解析出了什么"），而盘上的 deck.json + plan.json 里明明有真数据。
    # 缺的用同一段计算补：build_outline 的口径（deck.json 页数 + plan.json 时长）。
    slides = result.get("slides")
    est_sec = result.get("est_sec")
    if not slides or not est_sec:
        fallback_slides, fallback_sec = deck_totals(unit_row)
        slides = slides or fallback_slides
        est_sec = est_sec or fallback_sec
    out["slides"] = int(slides or 0)
    out["est_sec"] = round(float(est_sec or 0.0), 1)
    return out


def deck_totals(unit_row: dict) -> tuple[int, float]:
    """从**已有产物**（deck.json / plan.json）读出页数与总时长。

    复用 `build_outline` 而不是另写一份解析：大纲接口与"备课结果回填"必须同一口径，
    两边各算一份的话，"大纲说 3 页、课程卡说 4 页"这种事迟早会发生。
    读不到就给 (0, 0.0)，绝不抛 —— 它在 `/prepare` 的成功路径上被调用。
    """
    try:
        totals = (build_outline(unit_row) or {}).get("totals") or {}
        return int(totals.get("slides") or 0), round(float(totals.get("est_sec") or 0.0), 1)
    except (TypeError, ValueError, AttributeError):
        return 0, 0.0


def build_outline(unit_row: dict) -> dict:
    """单元大纲。W3 未就绪时给一个诚实的空大纲（页面留空），不报错。"""
    fn = _w3("studio.outline", "build_outline")
    deck_dir = deck_dir_of(unit_row)
    totals = {
        "slides": int(unit_row.get("slides") or 0),
        "units": 1 if unit_row.get("title") else 0,
        "est_sec": float(unit_row.get("est_sec") or 0.0),
    }
    if fn is None:
        return {"totals": totals, "pages": []}
    values = {"_default": deck_dir, "deck_dir": deck_dir, "unit_row": unit_row,
              "row": unit_row}
    try:
        result = _obj_fields(_adapt_call(fn, values))
    except Exception as e:
        log.warning("build_outline 失败，返回空大纲：%s: %s", type(e).__name__, e)
        return {"totals": totals, "pages": []}
    result.setdefault("totals", totals)
    result.setdefault("pages", [])
    return {"totals": result["totals"], "pages": result["pages"]}


def deck_id_of(unit_row: dict) -> str:
    """单元 → deck_id。口径与 deck.store.make_deck_id 一致（同一份 pptx 只有一份 deck）。"""
    pptx = str(unit_row.get("pptx_path") or "")
    if not pptx:
        return ""
    try:
        from deck.store import make_deck_id

        return make_deck_id(pptx)
    except Exception:
        return ""


def deck_dir_of(unit_row: dict):
    deck_id = deck_id_of(unit_row)
    if not deck_id:
        return None
    try:
        from deck.store import deck_dir

        return deck_dir(deck_id)
    except Exception:
        return None


# ===========================================================================
# 杂项
# ===========================================================================

def dev_enabled() -> bool:
    """`/dev/*` 总开关（默认关）。每次请求读一次环境变量，测试里能立刻切。"""
    return os.environ.get("CLASSROOM_DEV", "") == "1"


def dist_root() -> Path:
    return DIST_ROOT
