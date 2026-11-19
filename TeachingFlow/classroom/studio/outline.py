# -*- coding: utf-8 -*-
"""单元大纲：decks/<deck_id>/ 的两份产物 → 「这单元讲什么」的一页纸。

形状严格照设计文档 §4 GET /units/{id}/outline：
    {totals:{slides,units,est_sec}, pages:[{index,title,units:[{preview,est_sec,kp}]}]}

数据从哪来（两处，缺一档就降级而不是报错）
------------------------------------------
- deck.json：有哪些页、每页标题、每页有哪些字（"页数与课件一致"只能由它回答）；
- plan.json：课件被切成了哪几个讲解单元、每单元讲什么、讲到哪几个知识点。
  plan.json **没有**的时候（只解析没备课，例如刚导入一份课件）用规则大纲：
  一页一个单元、preview 取这页正文首段。大纲页因此永远打得开 —— 它的职责是
  "先让人看到这单元讲什么"，不是"证明备课成功"。

页与单元怎么对上
----------------
plan.json 的单元用 slide（**0 基**页序）指自己属于哪一页，与 deck.json 的
slides[*].index 是同一套编号。对不上的（备课之后课件换了一版）不会硬塞进第 1 页，
而是老实变成"这一页没有单元"—— 塞进去的表现是"翻到第 5 页在听第 2 页的话"。

顺便说清 est_sec：plan.json 里 PlanUnit 没有 est_sec 字段（它是方法，
= 字数 / 5），所以这里按同一口径现算 —— 不是另立一套。
"""

from __future__ import annotations

import json
from pathlib import Path

from deck.schema import ROLE_TITLE

#: preview 截断长度：投屏上一行读得下、手机上一屏放得下（~60 字）
PREVIEW_CHARS = 60

#: 讲稿语速：5 字/秒 —— 与 script_gen.PlanUnit.est_sec() 同一口径，
#: 口径写死在这里（而不是 import script_gen）是为了让大纲只依赖产物文件：
#: 它要能在"引擎还没装好模型"的机器上打开（tests/test_studio_outline.py 里
#: 有一条用例专门把这份口径与 script_gen 对了一遍）。
CHARS_PER_SEC = 5.0


def build_outline(deck_dir: Path) -> dict:
    """decks/<deck_id>/ → 大纲（形状见模块 docstring），**绝不抛异常**。

    deck.json 缺失/坏掉时返回空大纲 + 一句 warnings：单元大纲是"点进去看"的页面，
    它裂开一次就等于这个单元打不开，而真正的原因（课件没解析）在 warnings 里说。
    """
    base = Path(deck_dir)
    out = {"totals": {"slides": 0, "units": 0, "est_sec": 0.0}, "pages": []}
    warnings: list[str] = []

    deck = _read_json(base / "deck.json")
    if not isinstance(deck, dict):
        out["warnings"] = [f"读不到课件解析结果（{base / 'deck.json'} 缺失或不是 JSON）"
                           " —— 先对这个单元跑一次 prepare"]
        return out
    slides = deck.get("slides") if isinstance(deck.get("slides"), list) else []

    plan = _read_plan(base, warnings)
    by_slide = _units_by_slide(plan)
    has_plan = plan is not None

    total_units = 0
    total_sec = 0.0
    for pos, slide in enumerate(slides):
        page = _page(slide, pos)
        views = [_unit_view(u) for u in by_slide.get(page["index"], [])]
        if not views and not has_plan:
            # 没有 plan.json：规则大纲（一页一个单元，preview 取正文首段）
            views = [_rule_unit(slide)]
        page["units"] = views
        out["pages"].append(page)
        total_units += len(views)
        total_sec += sum(v["est_sec"] for v in views)

    out["totals"] = {"slides": len(slides), "units": total_units,
                     "est_sec": round(total_sec, 1)}
    if warnings:
        # 只在真有问题时才多这一键：正常路径的形状严格等于 §4 的契约（W2 只读契约里的键）
        out["warnings"] = warnings
    return out


# ===========================================================================
# 内部
# ===========================================================================

def _read_json(path: Path):
    """读一份 JSON；读不动返回 None（调用方当"没有"处理，不抛）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_plan(base: Path, warnings: list[str]) -> dict | None:
    """读 plan.json：不存在 = None（正常的降级路径），坏了 = None + 一句人话。"""
    path = base / "plan.json"
    if not path.is_file():
        return None
    plan = _read_json(path)
    if not isinstance(plan, dict):
        warnings.append(f"{path.name} 读不动（不是 JSON 对象）—— 大纲退回规则口径（按页给单元）")
        return None
    if not isinstance(plan.get("units"), list) or not plan.get("units"):
        warnings.append(f"{path.name} 里没有讲解单元 —— 大纲只有页标题，"
                        "这单元的备课还没成")
    return plan


def _units_by_slide(plan: dict | None) -> dict[int, list[dict]]:
    """plan.json 的单元按 slide 分桶（slide 缺失/不是数的单元丢开：挂不上任何一页）。"""
    out: dict[int, list[dict]] = {}
    for u in (plan or {}).get("units") or []:
        if not isinstance(u, dict):
            continue
        try:
            idx = int(u.get("slide"))
        except (TypeError, ValueError):
            continue
        out.setdefault(idx, []).append(u)
    return out


def _page(slide, pos: int) -> dict:
    """deck.json 的一页 → 大纲里的一页（页序取课件自己的 index，没有才用位次）。"""
    s = slide if isinstance(slide, dict) else {}
    try:
        idx = int(s.get("index"))
    except (TypeError, ValueError):
        idx = pos
    title = str(s.get("title") or "").strip() or f"第 {pos + 1} 页"
    return {"index": idx, "title": title, "units": []}


def _unit_view(unit: dict) -> dict:
    """plan.json 的一个讲解单元 → 大纲里的一个单元视图。"""
    text = str(unit.get("text") or "")
    kp = unit.get("kp") if isinstance(unit.get("kp"), list) else []
    return {"preview": _clip(text),
            "est_sec": round(_sec(unit.get("est_sec"), text), 1),
            "kp": [str(k) for k in kp if str(k).strip()]}


def _rule_unit(slide) -> dict:
    """没有 plan.json 时的"规则大纲"：一页一个单元，preview 取正文首段。

    为什么 preview 取首段而不是整页文字：这一栏回答的是"这页讲什么"，
    一页几十段全塞进去会把版面撑爆；而首段正是作者写在这页上的第一句结论。
    时长按**整页**字数估（照读这一页要多久），不是首段那几十个字。
    """
    first, whole = _page_text(slide if isinstance(slide, dict) else {})
    return {"preview": _clip(first),
            "est_sec": round(len(whole) / CHARS_PER_SEC, 1),
            "kp": []}


def _page_text(slide: dict) -> tuple[str, str]:
    """(正文首段, 全页文字)。正文 = 非标题元素的段落，按 z 序（阅读序）。"""
    body: list[str] = []
    any_text: list[str] = []
    for el in slide.get("elements") or []:
        if not isinstance(el, dict):
            continue
        text = el.get("text")
        paras = text.get("paras") if isinstance(text, dict) else None
        for p in paras or []:
            line = str((p or {}).get("text") or "").strip() if isinstance(p, dict) else ""
            if not line:
                continue
            any_text.append(line)
            if str(el.get("role") or "") != ROLE_TITLE:
                body.append(line)
    if not any_text:
        # 老/异形 deck.json 只留了 text_flat 时也能给出一句话（大纲不该因此空白）
        flat = [ln.strip() for ln in str(slide.get("text_flat") or "").splitlines() if ln.strip()]
        any_text, body = flat, flat
    first = body[0] if body else (any_text[0] if any_text else "")
    return first, "".join(any_text)


def _sec(given, text: str) -> float:
    """单元时长：有正的 est_sec 就用它，否则按字数估（与 PlanUnit.est_sec 同口径）。"""
    try:
        v = float(given)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return len(text) / CHARS_PER_SEC


def _clip(text: str) -> str:
    """压成一行并截到 ~60 字（截断了才加省略号：短 preview 多一个点反而怪）。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[:PREVIEW_CHARS].rstrip() + "…"
