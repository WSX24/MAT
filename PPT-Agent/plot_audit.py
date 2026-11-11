# -*- coding: utf-8 -*-
"""把「切线画错了」变成一次**算术**，而不是再花一次模型调用。

为什么需要它（2026-11-08 一堂课实测）
------------------------------------
第 8 页「例题：f(x)=x² 在 (1,1) 处的切线斜率」里，标注着「斜率 2」的那条蓝线：

    端点 (887,586.2)→(1215,422.6)  ⇒  像素斜率 −0.499
    该页比例：x 159.7 px/单位、y 47.8 px/单位  ⇒  折成数学斜率 **1.67**（不是 2）
    且在 x=1 处 y = 0.554（P(1,1) 在 0.554 之上 21px）—— 它压根没经过切点

同一份 deck 里第 11 页把"非极值点"画成了水平切线，第 12 页的切线整体偏了 33px。
根因不是转换器：pptx 里 `Line 36/37` 的 EMU 与 SVG 坐标逐一对应（1px = 9525EMU），
**错误在 SVG 就产生了**——那些坐标是模型逐页"手写"的，而 P4 的
`svg_quality_checker` 只查排版（越界/重叠/字号/对比度），**不查数学**。

所以这里做两件事，全程纯算术、不联网：
  · ``audit_svg``  —— 指出「这条线不是它标称的那条切线」（离切点多远、斜率差多少）
  · ``repair_svg`` —— 用**曲线自身的局部斜率**把线挪回去（过切点、斜率一致）

为什么不校验公式：SVG 里只有像素。切线在像素里唯一的正确解是
「过切点 + 斜率等于曲线在该点的局部斜率」，而局部斜率可以从曲线折线采样直接差分
出来——不需要知道 f(x) 是什么，也不需要知道坐标轴的换算比例。这条性质让审计对
「模型手写坐标」「代码预算坐标」两种来源都成立。

三个"别碰"的判据（都是实测出来的误报来源）
  · 灰线（#DCE3EC 一类）= 坐标轴/网格/分隔线；
  · 两端都落在曲线上的线 = **割线**，它本来就该与切线斜率不同（第 3 页的核心内容）；
  · 短于 90px 的线 = 图例样线、装饰短线。
另外属性会**从父 <g> 继承**（第 11 页的切线是 `plot-tangents` 组的 stroke/stroke-width），
所以按 XML 树解析而不是逐行正则。

不做的事：不改文字、不删元素、不判断"该不该有这条线"（那是内容问题）。
修不了（没曲线/没切点/线竖直）就原样返回并说明，绝不猜。
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

#: 标记点（<circle>）与直线的关联容差：垂距超过它就认为"不是这条线的切点"
ASSOC_TOL_PX = 45.0
#: 判定"这条线画错了"的阈值
SLOPE_REL_TOL = 0.06          # 相对斜率误差（6%）；小于它算画对
FLAT_SLOPE = 0.06             # 局部斜率小于它就算"水平"，只比绝对差
POINT_TOL_PX = 4.0            # 线到切点的垂距（px）
#: 候选直线的门槛：轴、网格、图例短线都不该被当成切线。
#: 注意**不能**按线宽筛：第 11 页的切线是 1px（线宽不是语义）。
MIN_LINE_PX = 90.0
#: 端点都落在曲线上的线 = 割线（连两点的平均变化率），不是切线，不能碰
ON_CURVE_TOL_PX = 18.0

_AXIS_GREY = re.compile(r"#(?:DC|E5|EE|F0|F4|CB|DD)[0-9A-Fa-f]{4}", re.I)
#: 浅色线 = 辅助线（坐标轴 #DCE3EC、Δx 构造线 #B9C4D2…）。主图形（曲线/切线）都是深色。
#: 只按"颜色名"列举会一直漏，改成按亮度判：相对亮度 > 0.62 就算浅色。
PALE_LUMINANCE = 0.62


def _luminance(color: str) -> float:
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", (color or "").strip())
    if not m:
        return 1.0 if not color else 0.0     # 无颜色（默认黑）→ 当深色
    r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _is_pale(color: str) -> bool:
    return _luminance(color) > PALE_LUMINANCE
_NUM = r"-?\d+(?:\.\d+)?"

_INHERIT = ("stroke", "stroke-width", "fill")


# ---------------------------------------------------------------------------
# 解析：按 XML 树走一遍，属性按父 <g> 继承
# ---------------------------------------------------------------------------

def _walk(elem, inherited: Dict[str, str], out: Dict[str, List]):
    style = dict(inherited)
    for key in _INHERIT:
        value = elem.get(key)
        if value is not None:
            style[key] = value
    tag = elem.tag.split("}")[-1]
    if tag == "path":
        out["paths"].append((elem, style))
    elif tag == "line":
        out["lines"].append((elem, style))
    elif tag == "circle":
        out["circles"].append((elem, style))
    elif tag == "text":
        out["texts"].append("".join(elem.itertext()))
    for child in elem:
        _walk(child, style, out)


def _parse(svg: str) -> Dict[str, List]:
    out: Dict[str, List] = {"paths": [], "lines": [], "circles": [], "texts": []}
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return out
    _walk(root, {}, out)
    return out


def _curves(tree: Dict[str, List]) -> List[List[Tuple[float, float]]]:
    """所有"多段折线 + 不填充"的 path —— 也就是画出来的函数曲线。"""
    curves: List[List[Tuple[float, float]]] = []
    for elem, style in tree["paths"]:
        if (style.get("fill") or "none") not in ("none", "transparent", "#FFF", "#FFFFFF"):
            continue
        d = elem.get("d") or ""
        nums = [(float(x), float(y)) for x, y in
                re.findall(rf"({_NUM})\s+({_NUM})", d)]
        if len(nums) >= 8:                       # 折线采样点；贝塞尔控制点不算曲线
            curves.append(nums)
    return curves


def _lines(svg: str, tree: Dict[str, List]) -> List[Dict]:
    """直线 + 它们在第 n 个 <line> 里的序号（修复时按序号改回文本）。"""
    lines: List[Dict] = []
    order = {id(elem): i for i, (elem, _style) in enumerate(tree["lines"])}
    for index, match in enumerate(re.finditer(r"<line\b(?P<attrs>[^>]*?)/>", svg, re.S)):
        attrs = match.group("attrs")
        got = dict(re.findall(rf'\b(x1|y1|x2|y2)="({_NUM})"', attrs))
        if not {"x1", "y1", "x2", "y2"} <= set(got):
            continue
        x1, y1, x2, y2 = (float(got[k]) for k in ("x1", "y1", "x2", "y2"))
        lines.append({"index": index, "match": match, "attrs": attrs,
                      "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                      "len": math.hypot(x2 - x1, y2 - y1)})
    # 把继承来的样式贴上去（按文档顺序一一对应）
    by_index = {}
    for i, (elem, style) in enumerate(tree["lines"]):
        by_index[i] = style
    for line in lines:
        line["style"] = by_index.get(line["index"], {})
    return lines


def _markers(tree: Dict[str, List]) -> List[Tuple[float, float]]:
    out = []
    for elem, _style in tree["circles"]:
        cx, cy, r = elem.get("cx"), elem.get("cy"), elem.get("r")
        if cx is None or cy is None:
            continue
        radius = float(r) if r else 4.0
        if 2.0 <= radius <= 12.0:                # 切点标记；排除大装饰圆/极小点
            out.append((float(cx), float(cy)))
    return out


# ---------------------------------------------------------------------------
# 几何
# ---------------------------------------------------------------------------

def _slope(x1: float, y1: float, x2: float, y2: float) -> Optional[float]:
    if abs(x2 - x1) < 1e-9:
        return None                              # 竖直线：本模块不处理
    return (y2 - y1) / (x2 - x1)


def _dist_point_to_line(px: float, py: float, x1: float, y1: float,
                        x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy)
    if norm < 1e-9:
        return math.hypot(px - x1, py - y1)
    return abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm


def _on_curve(curve: Sequence[Tuple[float, float]], px: float, py: float,
              tol: float = ON_CURVE_TOL_PX) -> bool:
    """这个点是不是落在曲线上（割线的两个端点都满足）。"""
    for (xa, ya), (xb, yb) in zip(curve, curve[1:]):
        dx, dy = xb - xa, yb - ya
        seg = dx * dx + dy * dy
        t = 0.0 if seg < 1e-12 else max(0.0, min(1.0, ((px - xa) * dx + (py - ya) * dy) / seg))
        if math.hypot(xa + t * dx - px, ya + t * dy - py) <= tol:
            return True
    return False


def _local_slope(curve: Sequence[Tuple[float, float]], cx: float) -> Optional[float]:
    """曲线在 x=cx 处的局部斜率（邻近采样点差分，窗口随曲线宽度自适应）。"""
    if len(curve) < 3:
        return None
    xs = [p[0] for p in curve]
    span = (max(xs) - min(xs)) or 1.0
    window = max(6.0, span * 0.02)
    near = [(x, y) for x, y in curve if abs(x - cx) <= window]
    if len(near) < 2:                            # 点落在采样间隙/端点外：取最近三点
        idx = min(range(len(curve)), key=lambda i: abs(curve[i][0] - cx))
        near = list(curve[max(0, idx - 1): idx + 2])
    if len(near) < 2:
        return None
    (xa, ya), (xb, yb) = near[0], near[-1]
    if abs(xb - xa) < 1e-6:
        (xa, ya), (xb, yb) = curve[0], curve[-1]
    return _slope(xa, ya, xb, yb)


def _span(curve: Sequence[Tuple[float, float]], x1: float, x2: float) -> Tuple[float, float]:
    """把线段的 x 范围夹到曲线的水平跨度内，太短就撑到 MIN_LINE_PX。"""
    xs = [p[0] for p in curve]
    lo, hi = min(xs), max(xs)
    a, b = sorted((x1, x2))
    a, b = max(a, lo - 8.0), min(b, hi + 8.0)
    if b - a < MIN_LINE_PX:
        mid = (a + b) / 2
        a, b = mid - MIN_LINE_PX / 2, mid + MIN_LINE_PX / 2
    return a, b


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

class Issue(dict):
    """一条"这条线不是切线"的说明书（可以直接 print / 进日志）。"""


def _candidates(svg: str, tree: Dict[str, List]) -> Tuple[List[Dict], List[Tuple[float, float]]]:
    """挑出「可能是切线」的线，并给每条配上一个切点。"""
    curves = _curves(tree)
    if not curves:
        return [], []
    curve = max(curves, key=len)
    markers = _markers(tree)
    out: List[Dict] = []
    for line in _lines(svg, tree):
        style = line.get("style") or {}
        stroke = style.get("stroke") or ""
        if _AXIS_GREY.match(stroke) or _is_pale(stroke):
            continue                             # 坐标轴 / 网格 / Δx 之类的辅助线
        if line["len"] < MIN_LINE_PX:
            continue                             # 图例样线、装饰短线
        if (_on_curve(curve, line["x1"], line["y1"])
                and _on_curve(curve, line["x2"], line["y2"])):
            continue                             # 割线：本来就该和切线斜率不同
        drawn = _slope(line["x1"], line["y1"], line["x2"], line["y2"])
        if drawn is None:
            continue
        lo, hi = sorted((line["x1"], line["x2"]))
        best = None
        for cx, cy in markers:
            if not (lo - 12 <= cx <= hi + 12):
                continue
            dist = _dist_point_to_line(cx, cy, line["x1"], line["y1"],
                                       line["x2"], line["y2"])
            if dist <= ASSOC_TOL_PX and (best is None or dist < best[0]):
                best = (dist, cx, cy)
        if best is None:
            continue
        dist, cx, cy = best
        want = _local_slope(curve, cx)
        if want is None:
            continue
        out.append({"line": line, "curve": curve, "point": (cx, cy),
                    "dist": dist, "drawn": drawn, "want": want})
    return out, markers


def _needs_fix(item: Dict) -> bool:
    drawn, want = item["drawn"], item["want"]
    if item["dist"] > POINT_TOL_PX:
        return True
    # 曲线在该点接近水平时，看相对误差会误判（0 与 0.011 差 100%），改用绝对阈值
    if abs(want) < FLAT_SLOPE:
        return abs(drawn) > FLAT_SLOPE
    return abs(drawn - want) / abs(want) > SLOPE_REL_TOL


def audit_svg(svg: str) -> Tuple[List[Issue], Dict]:
    """返回 (问题列表, 统计)。没有问题就是空列表——调用方据此决定是否 repair。"""
    tree = _parse(svg)
    curves = _curves(tree)
    stats = {"curve_points": len(max(curves, key=len)) if curves else 0,
             "markers": len(_markers(tree)), "candidate_lines": 0}
    if not curves:
        return [], stats
    items, markers = _candidates(svg, tree)
    stats["markers"] = len(markers)
    stats["candidate_lines"] = len(items)
    issues: List[Issue] = []
    for item in items:
        if not _needs_fix(item):
            continue
        cx, cy = item["point"]
        issues.append(Issue(
            kind="tangent_off",
            point=[round(cx, 1), round(cy, 1)],
            offset_px=round(item["dist"], 1),
            drawn_slope=round(item["drawn"], 4),
            expected_slope=round(item["want"], 4),
            message=(f"切线不过切点/斜率不对：过点 ({cx:.1f},{cy:.1f}) 的垂距 "
                     f"{item['dist']:.1f}px，画的斜率 {item['drawn']:.3f}，"
                     f"曲线局部斜率 {item['want']:.3f}")))
    return issues, stats


# ---------------------------------------------------------------------------
# 修复
# ---------------------------------------------------------------------------

def _fmt(v: float) -> str:
    return f"{v:.1f}"


def repair_svg(svg: str) -> Tuple[str, List[str]]:
    """把画错的切线挪回"过切点 + 局部斜率"，其余一个字节都不动。"""
    tree = _parse(svg)
    if not _curves(tree):
        return svg, []
    items, _markers_ = _candidates(svg, tree)
    notes: List[str] = []
    patches: List[Tuple[int, int, str]] = []
    for item in items:
        if not _needs_fix(item):
            continue
        line, (cx, cy), slope = item["line"], item["point"], item["want"]
        if abs(slope) > 200:                     # 近乎竖直：不硬拗
            continue
        a, b = _span(item["curve"], line["x1"], line["x2"])
        y_a = cy + slope * (a - cx)
        y_b = cy + slope * (b - cx)
        # 出了画布就按原线的纵向范围夹一下，避免修完把线画到页外
        for _ in range(6):
            if min(y_a, y_b) >= 0 and max(y_a, y_b) <= 720:
                break
            if max(y_a, y_b) > 720:
                k = (720 - cy) / slope if slope else 0
                if 0 < abs(k) < abs(b - cx):
                    b = cx + k
                    y_b = 720.0
            elif min(y_a, y_b) < 0:
                k = (0 - cy) / slope if slope else 0
                if 0 < abs(k) < abs(a - cx):
                    a = cx + k
                    y_a = 0.0
        attrs = line["attrs"]
        for name, value in (("x1", a), ("y1", y_a), ("x2", b), ("y2", y_b)):
            attrs = re.sub(rf'\b{name}="{_NUM}"', f'{name}="{_fmt(value)}"', attrs, count=1)
        patches.append((line["match"].start(), line["match"].end(), f"<line{attrs}/>"))
        notes.append(f"切线重画：过 ({cx:.1f},{cy:.1f})，斜率 "
                     f"{item['drawn']:+.3f} → {slope:+.3f}（原来偏 {item['dist']:.1f}px）")
    if not patches:
        return svg, []
    out = svg
    for start, end, text in sorted(patches, reverse=True):   # 从后往前替换，偏移不失效
        out = out[:start] + text + out[end:]
    return out, notes


# ---------------------------------------------------------------------------
# 自检（pipeline.py --self-check 也跑这一段）
# ---------------------------------------------------------------------------

#: 复刻第 8 页：曲线 y=x²（x 159.7 px/单位、y 47.8 px/单位）、P(927.5,544.7)、错误切线
_PAGE8_CURVE = [(767.8 + 159.7 * (-0.8 + 3.6 * i / 64),
                 592.5 - 47.8 * (-0.8 + 3.6 * i / 64) ** 2) for i in range(65)]
_PAGE8_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
    '<g id="plot-axes" stroke="#DCE3EC" stroke-width="2">'
    '<line x1="640" y1="592.5" x2="1215" y2="592.5"/>'
    '<line x1="767.8" y1="180" x2="767.8" y2="630"/></g>'
    '<path d="M' + " L".join(f"{x:.1f} {y:.1f}" for x, y in _PAGE8_CURVE)
    + '" fill="none" stroke="#1F2430" stroke-width="2.5"/>'
    '<g id="plot-tangents" stroke="#2F6FE0" stroke-width="3">'
    '<line x1="887" y1="586.2" x2="1215" y2="422.6"/></g>'
    '<circle cx="927.5" cy="544.7" r="7" fill="#F4F6F9" stroke="#2F6FE0" stroke-width="3"/>'
    '<text x="922.5" y="534.7" font-size="18">P(1,1)</text>'
    '</svg>')


def self_check() -> None:
    issues, stats = audit_svg(_PAGE8_SVG)
    assert stats["curve_points"] == 65, stats
    assert len(issues) == 1, f"第 8 页那种错线应该被查出来：{issues}"
    assert issues[0]["offset_px"] > 10, issues
    fixed, notes = repair_svg(_PAGE8_SVG)
    assert notes, "没有产生修复说明"
    after, _ = audit_svg(fixed)
    assert after == [], f"修完之后仍有问题：{after}"
    dists = [_dist_point_to_line(927.5, 544.7, ln["x1"], ln["y1"], ln["x2"], ln["y2"])
             for ln in _lines(fixed, _parse(fixed))]
    assert min(dists) < 0.5, f"修完还没过切点：{min(dists):.2f}px"
    assert _is_pale("#DCE3EC") and _is_pale("#B9C4D2") and not _is_pale("#2F6FE0")
    # 画对的页不能被改
    good = _PAGE8_SVG.replace('x1="887" y1="586.2" x2="1215" y2="422.6"',
                              'x1="887" y1="568.9" x2="1215" y2="372.6"')
    assert repair_svg(good)[0] == good, "画对的切线不该被动"
    # 割线不能被当成切线（两端都在曲线上）
    secant = _PAGE8_SVG.replace('x1="887" y1="586.2" x2="1215" y2="422.6"',
                                'x1="927.5" y1="544.7" x2="1007.4" y2="485.0"')
    assert repair_svg(secant) == (secant, []), "割线被误当成切线改了"
    # 纯文字页不该被碰
    plain = ('<svg xmlns="http://www.w3.org/2000/svg">'
             '<line x1="0" y1="0" x2="200" y2="200" stroke="#2F6FE0"/></svg>')
    assert repair_svg(plain) == (plain, []), "纯文字页被误改"
    print("   plot_audit OK：错线→过切点+局部斜率；画对的/割线/无曲线页原样不动")


if __name__ == "__main__":
    self_check()
