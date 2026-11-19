# -*- coding: utf-8 -*-
"""单元备料：一份 pptx → 课堂能上课的 decks/<deck_id>/{deck.json,plan.json}。

为什么是"调 bridge"而不是自己解析 + 自己备课
--------------------------------------------
解析（deck/pptx_ingest.py）、备课（script_gen）、预置路线（agent/routes.py）、
备份与写盘（run._save_plan）这四段都在既有引擎里，bridge 已经把"任意路径的 pptx
接进课堂预处理链"这件事做完了 —— 包括没有可用模型时的规则兜底、单飞（同一份源
只许一个作业）、坏文件翻成人话。本模块再写一遍，两份产物迟早会分叉成
"命令行备的课和课程库备的课不是同一节"。

它只做四件事：把路径交进去、等作业结束、把结果折成 API 契约的形状、顺手记下
落盘的规模（几页 / 多久 / 规则还是模型）。**一行解析逻辑都不复制。**

幂等：已备好就不重跑
--------------------
解析一份真实课件要几十秒、备课要模型（花钱）。课程库的"进入单元"是高频动作，
每次进都重解析一遍既慢又贵。所以 deck.json + plan.json 都在时直接读盘返回，
只有显式 force=True 才重跑 —— 重跑的口径也交给 bridge（它自己会先备份旧 plan.json）。

degraded 是什么意思
-------------------
"这节课没有模型备过课，是规则兜底（照读课件）出来的"。它同时来自两个证据：
bridge 的返回值（这一次真的走了兜底）与 plan.json 里的 source（盘上那份计划是谁做的）。
两者只要有一个说是规则，就是 degraded —— 用户点进课堂听到的是照读，该被告知。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import bridge
from deck import store as deck_store

#: 讲稿语速：5 字/秒 —— 与 script_gen.PlanUnit.est_sec() 和 agent/routes 同一口径
CHARS_PER_SEC = 5.0


def prepare_unit(pptx_path: Path, *, deck_root: Path | None = None,
                 force: bool = False, settings=None) -> dict:
    """把一份 pptx 备成能上课的课（解析 + 备课 + 落盘），**绝不抛异常**。

    返回 {ok, deck_id, deck_ready, plan_ready, plan_source, degraded,
          slides, est_sec, warnings}：
      - ok：真的可以开课了（deck.json 与 plan.json 都在）；失败原因在 warnings
      - plan_source：plan.json 顶层的 source（rule / llm / manual），空 = 还没有计划
      - slides / est_sec：这节课几页、讲多久（est_sec 按讲稿字数估，缺计划就是 0）

    deck_root 与 settings 是给测试与"导入沙箱"留的钩子（W1 常规调用不传）：
      - deck_root：产物落点临时改到别处，**退出一定还原**（见 _deck_root）
      - settings：这一次用哪份模型配置；None = 照仓库根 .env（bridge 的老口径）
    """
    out = _blank()
    try:
        path = Path(pptx_path).expanduser()
        if not path.is_file():
            out["warnings"].append(f"课件不在盘上：{path} —— 先确认出片/导入那一步落了盘")
            return out
        if path.suffix.lower() != ".pptx":
            out["warnings"].append(f"只认 .pptx（这份是 {path.name!r}）—— 解析器读不了别的格式")
            return out
        # deck_id 由文件名派生（deck/store.py 的稳定化口径），与 source_path 无关：
        # 同一份课件重备多少次都落在同一个目录，不会越攒越多。
        deck_id = deck_store.make_deck_id(path.name)
        out["deck_id"] = deck_id

        with _deck_root(deck_root):
            base = deck_store.deck_dir(deck_id)
            state = _state(base)
            if not force and state["deck_ready"] and state["plan_ready"]:
                _fill(out, state)
                out["ok"] = True
                out["warnings"].append(
                    f"已有 deck.json + plan.json（{state['slides']} 页 / {state['units']} 个单元）"
                    "，直接复用，没有重新解析")
                return out

            res = bridge.preprocess_pptx_sync(path, action="full", settings=settings)
            for line in (res.get("error"), res.get("note")):
                if line:
                    out["warnings"].append(str(line))

            state = _state(base)
            _fill(out, state)
            if not state["slides"]:
                # bridge 说解析成功、盘上却数不出页数：以盘上的为准，别报一个空课件
                out["slides"] = int((res.get("stats") or {}).get("slides") or 0)
            out["degraded"] = bool(res.get("degraded")) or state["plan_source"] == "rule"
            out["ok"] = bool(state["deck_ready"] and state["plan_ready"])
            if state["deck_ready"] and not state["plan_ready"]:
                out["warnings"].append(
                    "课件解析好了，但没备出 plan.json —— 这节课还不能开"
                    "（配好模型后重跑一次 action='plan'，不必重新解析）")
            return out
    except Exception as exc:      # 契约第一条：任何失败都是 ok=False + 一句人话
        out["warnings"].append(f"{type(exc).__name__}: {exc}")
        return out


# ===========================================================================
# 内部
# ===========================================================================

def _blank() -> dict:
    """返回值的空壳 —— 每个出口都从它出发，契约里的字段一个都不缺。"""
    return {"ok": False, "deck_id": "", "deck_ready": False, "plan_ready": False,
            "plan_source": "", "degraded": False, "slides": 0, "est_sec": 0.0,
            "warnings": []}


def _fill(out: dict, state: dict) -> None:
    """把盘上的状态填进返回值（成功/失败两条路共用，字段口径只有一处）。"""
    out["deck_ready"] = state["deck_ready"]
    out["plan_ready"] = state["plan_ready"]
    out["plan_source"] = state["plan_source"]
    out["degraded"] = state["plan_source"] == "rule"
    out["slides"] = state["slides"]
    out["est_sec"] = state["est_sec"]


def _state(base: Path) -> dict:
    """读盘上已有的产物：有没有 deck/plan、几页、几单元、多久、计划是谁做的。

    只读、只认"文件在不在 + 能不能解析"：一份坏 plan.json 只该让这份缓存失效，
    不该让"进入单元"直接 500。
    """
    state = {"deck_ready": (base / "deck.json").is_file(),
             "plan_ready": (base / "plan.json").is_file(),
             "plan_source": "", "slides": 0, "est_sec": 0.0, "units": 0}
    if state["deck_ready"]:
        deck = _read_json(base / "deck.json")
        slides = deck.get("slides") if isinstance(deck, dict) else None
        state["slides"] = len(slides) if isinstance(slides, list) else 0
    if state["plan_ready"]:
        plan = _read_json(base / "plan.json")
        if isinstance(plan, dict):
            state["plan_source"] = str(plan.get("source") or "")
            units = plan.get("units") if isinstance(plan.get("units"), list) else []
            state["units"] = len(units)
            state["est_sec"] = round(sum(_unit_sec(u) for u in units
                                         if isinstance(u, dict)), 1)
    return state


def _read_json(path: Path):
    """读一份 JSON；读不动返回 None（调用方当"没有"处理）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _unit_sec(unit: dict) -> float:
    """一个讲解单元讲多久：plan.json 里有 est_sec 就用它，没有就按字数估。

    plan.json 现在**不写** est_sec（PlanUnit 那边是方法不是字段），
    所以正常路径走的都是"字数 / 5"；留着前者是为了将来真的写进去时不用改这里。
    """
    try:
        given = float(unit.get("est_sec"))
        if given > 0:
            return given
    except (TypeError, ValueError):
        pass
    return len(str(unit.get("text") or "")) / CHARS_PER_SEC


@contextlib.contextmanager
def _deck_root(root: Path | None):
    """把 deck 落点临时指到 root，退出**一定**还原。

    为什么改模块全局而不是给 bridge 加参数：解析 → 备课 → 落盘整条链路只经
    deck/store.py 的 deck_dir()，那也是既有测试唯一的隔离点（tests/test_preprocess.py）。
    给 bridge 加一条"deck 根"参数意味着把它整条链路的落点参数化一遍 —— 为一次
    回归测试改四个引擎文件，不划算。这里是 try/finally，异常退栈也还原。
    """
    if root is None:
        yield
        return
    old = deck_store.DECKS_DIR
    deck_store.DECKS_DIR = Path(root)
    try:
        yield
    finally:
        deck_store.DECKS_DIR = old
