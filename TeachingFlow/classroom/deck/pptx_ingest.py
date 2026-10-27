"""pptx → DeckDoc：把真实课件解析成「可渲染、可锚定、可操作」的元素级结构。

设计要点（与 `deck/schema.py` 的契约一一对应）
--------------------------------------------
* **单位**：全部落到 px（96 dpi，EMU_PER_PX=9525），EMU→px 一律走 `schema.emu_to_px`。
* **id 稳定可复现**：完全由「页号 + z 序」派生，不含随机数/时间/哈希序，
  所以同一份 pptx 解析两次，deck.json 字节一致。
* **样式继承链**：真实课件里绝大多数 `a:r` 连 `sz` 都没有 —— 标题 36pt 来自母版
  `p:titleStyle`，正文 18pt 来自 `p:bodyStyle` / `otherStyle`。所以本模块按
  run.rPr → 段落 pPr.defRPr → 形状 lstStyle → 版式占位符 → 母版占位符
  → 母版 txStyles → 主题字体 → 内置默认 逐层解析，
  并把「字号最终来自哪一层」记进 `stats["size_source"]` 供审计。

已知不覆盖（诚实记录）
--------------------
* `p:style`(fillRef/lnRef/effectRef/fontRef) 的主题引用：只当 hint，不参与取色。
* `a:arcTo`：custGeom 的 moveTo/lnTo/cubicBezTo/close 都还原成 SVG path，
  arcTo 目前按直线退化 + warning。
* `a:pattFill`(图案填充)、`hslClr`/`scrgbClr`：warning + 按无色处理。
* effectLst 阴影/发光、图片边框与阴影、OMML 公式、艺术字(textPath)：一律忽略。
* SmartArt / 图表 / 其它 graphicFrame：只落 kind="unknown" + warning，内容不解析。
* 表格：只吃 tcPr 填色 + 单元格文字，不吃 tblStyle/tblPr 的主题色带
  （first_row/banded 仅作标志位）；单元格 run 的继承落不到主题表，会退到内置 18pt。
* 动画：只解析 presetClass="entr" 的入场效果；退出/强调/路径动画、时长、重复次数不落库。
* `a:fld`：只取缓存文本，不做域值求值。
* 媒体：ppt/media 原样拷贝，不解析视频海报帧与 OLE 内嵌对象；EMF/WMF 拷了前端也画不出。
* 母版/版式上的装饰形状不进页面元素（只当背景与样式来源）——关键图形若只在母版，页面上看不到。
* 主题 `<a:font script="...">` 的 CJK 脚本字体：主题 `a:ea` 为空时留 None 交前端回退。
"""

from __future__ import annotations

import argparse
import colorsys
import datetime as _dt
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from lxml import etree
from pptx import Presentation

from deck import store as deck_store
from deck.schema import (
    EMU_PER_PX,
    KIND_CONNECTOR,
    KIND_ELLIPSE,
    KIND_FREEFORM,
    KIND_GROUP,
    KIND_LINE,
    KIND_PICTURE,
    KIND_PLACEHOLDER,
    KIND_RECT,
    KIND_ROUND_RECT,
    KIND_TABLE,
    KIND_TEXT,
    KIND_UNKNOWN,
    ROLE_BODY,
    ROLE_DECOR,
    ROLE_PICTURE,
    ROLE_TABLE,
    ROLE_TITLE,
    DeckDoc,
    Element,
    Fill,
    GradientStop,
    LineStyle,
    Paragraph,
    PictureData,
    RunStyle,
    SlideDoc,
    Step,
    TableCell,
    TableData,
    TextBody,
    emu_to_px,
    sha1_file,
)

# ---------------------------------------------------------------------------
# 命名空间 & 小工具
# ---------------------------------------------------------------------------

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_A = "{%s}" % NS_A
_P = "{%s}" % NS_P
_R = "{%s}" % NS_R

#: DrawingML 默认文本内边距：lIns=rIns=91440EMU=9.6px，tIns=bIns=45720EMU=4.8px
DEF_INSET_LR_EMU = 91440
DEF_INSET_TB_EMU = 45720

#: DrawingML 默认线宽 9525EMU = 0.75pt = 1px
DEF_LINE_W_EMU = 9525

#: 取不到 clrMap 时的标准默认映射
DEFAULT_CLR_MAP = {
    "bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2",
    "accent1": "accent1", "accent2": "accent2", "accent3": "accent3",
    "accent4": "accent4", "accent5": "accent5", "accent6": "accent6",
    "hlink": "hlink", "folHlink": "folHlink",
}

_SYS_COLORS = {
    "window": "FFFFFF", "windowtext": "000000", "scrollbar": "C8C8C8",
    "menu": "F0F0F0", "caption": "000000", "activecaption": "000000",
    "highlight": "0078D7", "btnface": "F0F0F0", "appworkspace": "FFFFFF",
    "btnshadow": "808080", "graytext": "808080", "inactivecaption": "BFBFBF",
    "infobackground": "FFFFFF", "infotext": "000000", "menuText": "000000",
}

#: 常见预设色（只查常见名；查不到记 warning）
_PRESET_COLORS = {
    "black": "000000", "white": "FFFFFF", "red": "FF0000", "green": "008000",
    "blue": "0000FF", "yellow": "FFFF00", "cyan": "00FFFF", "magenta": "FF00FF",
    "gray": "808080", "grey": "808080", "ltGray": "C0C0C0", "dkGray": "404040",
    "orange": "FFA500", "purple": "800080", "brown": "A52A2A", "pink": "FFC0CB",
    "silver": "C0C0C0", "gold": "FFD700", "darkBlue": "00008B",
    "darkRed": "8B0000", "darkGreen": "006400", "lightBlue": "ADD8E6",
    "lightGreen": "90EE90", "lightYellow": "FFFFE0", "lightGray": "D3D3D3",
    "darkGray": "A9A9A9", "aqua": "00FFFF", "lime": "00FF00", "navy": "000080",
    "teal": "008080", "olive": "808000", "maroon": "800000", "fuchsia": "FF00FF",
}

#: 认识的 prstGeom 预设 → kind。其余（如 chevron）归 rect，但保留 preset 并记 warning。
_KNOWN_PRST_KIND: dict[str, str] = {
    "rect": KIND_RECT,
    "roundRect": KIND_ROUND_RECT,
    "ellipse": KIND_ELLIPSE,
    "oval": KIND_ELLIPSE,
    "line": KIND_LINE,
    "straightConnector1": KIND_CONNECTOR,
    "rightArrow": "rightArrow",
    "leftArrow": "leftArrow",
    "upArrow": "upArrow",
    "downArrow": "downArrow",
    "leftRightArrow": "leftRightArrow",
    "upDownArrow": "upDownArrow",
    "triangle": "triangle",
    "rtTriangle": "rtTriangle",
    "diamond": "diamond",
}

_ALIGN_MAP = {"l": "left", "ctr": "center", "r": "right", "just": "justify",
              "justLow": "justify", "dist": "left", "thaiDist": "left"}

_CAP_MAP = {"flat": "flat", "rnd": "round", "sq": "square", "": "flat"}

#: 样式继承链的层名（stats 里按它统计「某个属性最终来自哪一层」）。
#: 与 _Resolver.resolve 收到的 sources 下标一一对应。
_STYLE_LAYERS = ("run", "para_def", "para_end", "shape_lst", "layout_lst",
                 "layout_para", "master_lst", "master_para", "txstyles")


def _local(el) -> str:
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""


def _kids(el, tag: str, ns: str = _P) -> list:
    if el is None:
        return []
    return [c for c in el if c.tag == ns + tag]


def _kid(el, tag: str, ns: str = _P):
    if el is None:
        return None
    for c in el:
        if c.tag == ns + tag:
            return c
    return None


def _attr(el, name: str, default=None):
    return default if el is None else el.get(name, default)


def _int(v, default=None):
    if v is None:
        return default
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _flag(v, default: bool = False) -> bool:
    """DrawingML 布尔属性：'1'/'true' → True，'0'/'false' → False。"""
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "on")


# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------

def _hex(r: float, g: float, b: float) -> str:
    f = lambda x: max(0, min(255, int(round(x))))
    return "#%02X%02X%02X" % (f(r), f(g), f(b))


def _hsl_apply(hexcolor: str, transforms: list[tuple[str, int]]) -> str:
    """lumMod / lumOff / tint / shade 的**近似**实现（在 HSL 亮度上做）。"""
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    hh, ll, ss = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    for name, val in transforms:
        f = val / 100000.0
        if name == "lumMod":
            ll *= f
        elif name == "lumOff":
            ll += f
        elif name == "tint":          # 向白靠
            ll = ll + (1.0 - ll) * f
        elif name == "shade":         # 向黑靠
            ll *= f
        ll = max(0.0, min(1.0, ll))
    rr, gg, bb = colorsys.hls_to_rgb(hh, max(0.0, min(1.0, ll)), ss)
    return _hex(rr * 255, gg * 255, bb * 255)


class _Theme:
    """一个 master 的主题：配色表 + 字体方案 + 背景填充样式表。"""

    def __init__(self, clr_scheme: dict, clr_map: dict, fonts: dict, bg_fills: list):
        self.clr_scheme = clr_scheme
        self.clr_map = clr_map
        self.fonts = fonts          # major_latin/major_ea/minor_latin/minor_ea
        self.bg_fills = bg_fills

    def resolve_font_ref(self, typeface: str | None) -> str | None:
        """主题字体引用：+mj-lt / +mj-ea / +mn-lt / +mn-ea / +mj-cs / +mn-cs。"""
        if not typeface:
            return None
        if not typeface.startswith("+"):
            return typeface
        ref = typeface[1:]
        key = ("major_" if ref.startswith("mj") else "minor_") + \
              ("latin" if ref.endswith("lt") or ref.endswith("cs") else "ea")
        return self.fonts.get(key) or None


def _parse_theme(master_el, theme_root) -> _Theme:
    clr_map = dict(DEFAULT_CLR_MAP)
    cm = _kid(master_el, "clrMap") if master_el is not None else None
    if cm is not None:
        for k, v in cm.attrib.items():
            clr_map[k] = v

    clr_scheme: dict[str, Any] = {}
    fonts: dict[str, Any] = {"major_latin": None, "major_ea": None,
                             "minor_latin": None, "minor_ea": None}
    bg_fills: list = []
    if theme_root is not None:
        els = _kid(theme_root, "themeElements", ns=_A)
        cs = _kid(els, "clrScheme", ns=_A)
        for c in (cs if cs is not None else []):
            clr_scheme[_local(c)] = c
        fs = _kid(els, "fontScheme", ns=_A)
        for which in ("majorFont", "minorFont"):
            fnt = _kid(fs, which, ns=_A)
            pre = "major_" if which == "majorFont" else "minor_"
            fonts[pre + "latin"] = (_attr(_kid(fnt, "latin", ns=_A), "typeface") or None)
            fonts[pre + "ea"] = (_attr(_kid(fnt, "ea", ns=_A), "typeface") or None)
        fmt = _kid(els, "fmtScheme", ns=_A)
        bfl = _kid(fmt, "bgFillStyleLst", ns=_A)
        bg_fills = list(bfl) if bfl is not None else []
    return _Theme(clr_scheme, clr_map, fonts, bg_fills)


def _color_transforms(el) -> list[tuple[str, int]]:
    out = []
    for c in el:
        n = _local(c)
        if n in ("lumMod", "lumOff", "tint", "shade"):
            v = _int(c.get("val"))
            if v is not None:
                out.append((n, v))
    return out


_COLOR_TAGS = ("srgbClr", "schemeClr", "sysClr", "prstClr", "hslClr", "scrgbClr")


def _resolve_color(el, theme: _Theme, warn, *, phclr=None, _depth: int = 0):
    """颜色元素 → (hex|None, alpha)。

    支持 srgbClr / schemeClr（走 clrMap + clrScheme）/ sysClr / prstClr，
    并叠加 lumMod/lumOff/tint/shade。解析不出来的一律返回 (None, 1.0) + warning，不抛。
    """
    if el is None or _depth > 8:
        return None, 1.0
    name = _local(el)
    if name not in _COLOR_TAGS:
        # clrScheme 的条目（<a:dk1>/<a:lt1>/<a:accent1>…）只是容器，
        # 真正的颜色在它第一个子元素里（<a:sysClr>/<a:srgbClr>）
        inner = _first_color_child(el)
        if inner is None:
            warn(f"元素 <a:{name}> 里没有可识别的颜色")
            return None, 1.0
        return _resolve_color(inner, theme, warn, phclr=phclr, _depth=_depth + 1)

    alpha = 1.0
    for c in el:
        if _local(c) == "alpha":
            v = _int(c.get("val"), 100000)
            alpha = max(0.0, min(1.0, (v or 0) / 100000.0))

    if name == "srgbClr":
        base = "#" + str(el.get("val", "")).strip().lstrip("#").upper()
        if len(base) != 7:
            warn(f"颜色 srgbClr val={el.get('val')!r} 不合法")
            return None, alpha
        return _hsl_apply(base, _color_transforms(el)), alpha

    if name == "schemeClr":
        val = str(el.get("val") or "")
        if val == "phClr":                       # 主题填充样式表里的占位色
            if phclr is None:
                return None, alpha
            hexv, a2 = _resolve_color(phclr, theme, warn, _depth=_depth + 1)
            if hexv is None:
                return None, alpha
            return _hsl_apply(hexv, _color_transforms(el)), alpha * a2
        target = theme.clr_map.get(val, val)
        base_el = theme.clr_scheme.get(target)
        if base_el is None:
            warn(f"颜色 schemeClr val={val!r}（clrMap 映射到 {target!r}）在主题里找不到")
            return None, alpha
        hexv, a2 = _resolve_color(base_el, theme, warn, _depth=_depth + 1)
        if hexv is None:
            return None, alpha
        return _hsl_apply(hexv, _color_transforms(el)), alpha * a2

    if name == "sysClr":
        last = el.get("lastClr")
        if last and len(str(last).strip()) == 6:
            return "#" + str(last).strip().upper(), alpha
        sysname = el.get("val") or ""
        v = _SYS_COLORS.get(sysname) or _SYS_COLORS.get(sysname.lower())
        if v:
            return "#" + v, alpha
        warn(f"颜色 sysClr val={sysname!r} 未知且没有 lastClr")
        return None, alpha

    if name == "prstClr":
        v = _PRESET_COLORS.get(str(el.get("val") or ""))
        if v is None:
            warn(f"颜色 prstClr val={el.get('val')!r} 不在常见预设表里")
            return None, alpha
        return _hsl_apply("#" + v, _color_transforms(el)), alpha

    warn(f"不支持的取色方式 <a:{name}>（按无色处理）")
    return None, alpha


def _first_color_child(el):
    if el is None:
        return None
    for c in el:
        if _local(c) in _COLOR_TAGS:
            return c
    return None


# ---------------------------------------------------------------------------
# 填充 / 描边
# ---------------------------------------------------------------------------

def _parse_fill(parent, theme: _Theme, warn, *, phclr=None) -> Fill | None:
    """spPr / tcPr / bgPr 的直接填充子元素 → Fill。

    没有填充子元素 → None（区别于 kind="none"）。
    """
    if parent is None:
        return None
    el = None
    for tag in ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill"):
        c = _kid(parent, tag, ns=_A)
        if c is not None:
            el = c
            break
    if el is None:
        return None
    tag = _local(el)

    if tag == "noFill":
        return Fill(kind="none")

    if tag == "solidFill":
        cel = _first_color_child(el)
        if cel is None:
            warn("solidFill 里没有颜色元素，按白色处理")
            return Fill(kind="solid", color="#FFFFFF")
        hexv, alpha = _resolve_color(cel, theme, warn, phclr=phclr)
        return Fill(kind="solid", color=hexv or "#FFFFFF", alpha=alpha)

    if tag == "gradFill":
        return _parse_grad_fill(el, theme, warn, phclr=phclr)

    if tag == "blipFill":
        return Fill(kind="image")          # image 名由调用方补（需要 r:embed 上下文）

    if tag == "pattFill":
        warn("pattFill（纹理填充）不支持，按无填充处理")
        return Fill(kind="none")

    warn("grpFill 不支持，按无填充处理")
    return None


def _parse_grad_fill(el, theme: _Theme, warn, *, phclr=None) -> Fill:
    stops: list[GradientStop] = []
    gsl = _kid(el, "gsLst", ns=_A)
    for gs in (gsl if gsl is not None else []):
        pos = _int(gs.get("pos"), 0) or 0
        cel = _first_color_child(gs)
        if cel is None:
            warn("渐变停靠点没有颜色，跳过")
            continue
        hexv, alpha = _resolve_color(cel, theme, warn, phclr=phclr)
        if hexv is None:
            continue
        stops.append(GradientStop(pos=max(0.0, min(1.0, pos / 100000.0)),
                                  color=hexv, alpha=alpha))
    # 真实文件里 pos 有乱序（实测某处 59000 在 0 前面），先排好给前端用
    stops.sort(key=lambda s: s.pos)

    angle = 0.0
    lin = _kid(el, "lin", ns=_A)
    if lin is not None:
        ang = _int(lin.get("ang"), 0) or 0
        # ── 角度换算（别改错）──────────────────────────────────────────────
        # <a:lin ang="X"> 的单位是 **1/60000 度**，且 0 = 从左到右（+x 轴），
        # 角度按**顺时针**增长（屏幕坐标 y 轴向下）。
        # CSS 的 linear-gradient(θ) 里 0deg = 从下到上（+y 轴向上），同样顺时针。
        # 两者只是 y 轴方向相反，所以正好差 90°：
        #     CSS 角度 = ang / 60000 + 90
        # 自检：ang=0（左→右）→ 90deg = 左→右 ✓
        #       ang=5400000（90° 下→上）→ 180deg = 下→上 ✓
        #       本课件实测 ang=4200000（70°）→ 160deg
        # 末尾 % 360 只是把角度归一到 [0,360)：真实文件里出现过 ang=20990744（349.85°）
        # 这种越界值，加 90 后会得到 439.85°，方向其实等价于 79.85°。
        angle = round((ang / 60000.0 + 90.0) % 360.0, 3)
    elif _kid(el, "path", ns=_A) is not None:
        warn("径向渐变 <a:path> 降级为线性渐变（角度按 0°）")
    else:
        warn("渐变既没有 <a:lin> 也没有 <a:path>，角度按 0°")

    if len(stops) < 2:
        warn(f"渐变停靠点只有 {len(stops)} 个，退化为纯色填充")
        return Fill(kind="solid", color=stops[0].color if stops else "#FFFFFF")
    return Fill(kind="gradient", gradient=stops, angle=angle)


def _parse_line(sp_pr, theme: _Theme, warn) -> LineStyle | None:
    """<a:ln> → LineStyle。没有 <a:ln> → None（区别于 kind="none"）。"""
    ln = _kid(sp_pr, "ln", ns=_A) if sp_pr is not None else None
    if ln is None:
        return None
    w = _int(ln.get("w"), DEF_LINE_W_EMU)
    ls = LineStyle(width_px=emu_to_px(w if w is not None else DEF_LINE_W_EMU),
                   cap=_CAP_MAP.get(ln.get("cap") or "", "flat"))

    pd = _kid(ln, "prstDash", ns=_A)
    if pd is not None:
        v = str(pd.get("val") or "")
        ls.dash = "" if v == "solid" else v

    head = _kid(ln, "headEnd", ns=_A)
    tail = _kid(ln, "tailEnd", ns=_A)
    htyp = _attr(head, "type") if head is not None else None
    ttyp = _attr(tail, "type") if tail is not None else None
    if ttyp and ttyp != "none":
        ls.arrow = ttyp
    elif htyp and htyp != "none":
        ls.arrow = "head:" + htyp          # 只有头端箭头时加前缀区分

    if _kid(ln, "noFill", ns=_A) is not None:
        ls.kind = "none"
        return ls
    solid = _kid(ln, "solidFill", ns=_A)
    grad = _kid(ln, "gradFill", ns=_A)
    if solid is not None:
        cel = _first_color_child(solid)
        hexv, alpha = _resolve_color(cel, theme, warn) if cel is not None else (None, 1.0)
        ls.kind = "solid"
        ls.color = hexv or "#000000"
        ls.alpha = alpha
    elif grad is not None:
        f = _parse_grad_fill(grad, theme, warn)
        ls.kind = "gradient"
        if f.gradient:
            ls.color = f.gradient[0].color
            ls.alpha = f.gradient[0].alpha
    else:
        ls.kind = "solid"                  # 有 <a:ln> 但没写填充 → DrawingML 默认实线
    return ls


# ---------------------------------------------------------------------------
# 样式继承链
# ---------------------------------------------------------------------------

def _level_ppr(container, level: int):
    """从 lstStyle / txStyles 这类容器里取 lvl{N}pPr（该层缺失则退回 lvl1pPr）。"""
    if container is None:
        return None
    for lv in (level + 1, 1):
        ppr = _kid(container, f"lvl{lv}pPr", ns=_A)
        if ppr is not None:
            return ppr
    return None


def _level_defrpr(container, level: int):
    return _kid(_level_ppr(container, level), "defRPr", ns=_A)


def _own_defrpr(ppr):
    """段落自己的 <a:pPr><a:defRPr>（注意不是 lvlNpPr）。"""
    return _kid(ppr, "defRPr", ns=_A)


def _para_defrpr(tx_body):
    """版式/母版占位符的样式常写在 <a:p><a:pPr><a:defRPr> 里。"""
    for p in _kids(tx_body, "p", ns=_A):
        d = _own_defrpr(_kid(p, "pPr", ns=_A))
        if d is not None:
            return d
    return None


class _Resolver:
    """把继承链走一遍，并记录每个属性最终命中的层。"""

    def __init__(self, theme: _Theme, is_title: bool, provenance: dict, warn):
        self.theme = theme
        self.is_title = is_title
        self.prov = provenance
        self.warn = warn

    # -- 属性 ---------------------------------------------------------------
    def resolve(self, sources: list, prop: str, hit: str = ""):
        for idx, el in enumerate(sources):
            if el is None:
                continue
            v = self._extract(el, prop)
            if v is not None:
                if hit:
                    layer = _STYLE_LAYERS[min(idx, len(_STYLE_LAYERS) - 1)]
                    self.prov.setdefault(hit, {}).setdefault(layer, 0)
                    self.prov[hit][layer] += 1
                return v
        # 内置默认：18pt / 黑色 / 主题字体（标题 majorFont、其余 minorFont）/ 左对齐
        if prop == "size_pt":
            if hit:
                self.prov.setdefault(hit, {}).setdefault("builtin", 0)
                self.prov[hit]["builtin"] += 1
            return 18.0
        if prop == "color":
            return "#000000"
        if prop == "font":
            return self.theme.resolve_font_ref("+mj-lt" if self.is_title else "+mn-lt")
        if prop == "font_ea":
            return self.theme.resolve_font_ref("+mj-ea" if self.is_title else "+mn-ea")
        return None

    def _extract(self, el, prop: str):
        if prop == "size_pt":
            v = _int(el.get("sz"))
            return None if v is None else round(v / 100.0, 2)
        if prop == "bold":
            return None if el.get("b") is None else _flag(el.get("b"))
        if prop == "italic":
            return None if el.get("i") is None else _flag(el.get("i"))
        if prop == "underline":
            u = el.get("u")
            return None if u is None else u not in ("none", "0", "false")
        if prop == "strike":
            s = el.get("strike")
            return None if s is None else s not in ("noStrike", "none", "0", "false")
        if prop == "baseline":
            b = _int(el.get("baseline"))
            if b is None or b == 0:
                return None
            return "superscript" if b > 0 else "subscript"
        if prop == "spacing_pt":
            v = _int(el.get("spc"))
            return None if v is None else round(v / 100.0, 2)
        if prop == "color":
            sf = _kid(el, "solidFill", ns=_A)
            cel = _first_color_child(sf) if sf is not None else None
            if cel is None:
                return None
            hexv, _a = _resolve_color(cel, self.theme, self.warn)
            return hexv
        if prop == "font":
            tf = _attr(_kid(el, "latin", ns=_A), "typeface")
            return (self.theme.resolve_font_ref(tf) or tf) if tf else None
        if prop == "font_ea":
            tf = _attr(_kid(el, "ea", ns=_A), "typeface")
            return (self.theme.resolve_font_ref(tf) or tf) if tf else None
        return None

    def align(self, holders: list) -> str:
        for h in holders:
            if h is None:
                continue
            algn = _attr(h, "algn")
            if algn:
                return _ALIGN_MAP.get(algn, "left")
        return "left"


def _expand_entries(entries: list, level: int) -> list:
    """把「容器或 defRPr」混排的条目按 level 摊平成 defRPr 列表。"""
    out = []
    for e in entries:
        if e is None:
            out.append(None)
        elif e.tag == _A + "defRPr":
            out.append(e)
        else:
            out.append(_level_defrpr(e, level))
    return out


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------

def _parse_para_body(tx_body, el_id: str, res: _Resolver, entries: list) -> list[Paragraph]:
    """entries = [形状 lstStyle, 版式 lstStyle, 版式 pPr.defRPr, 母版 lstStyle,
                  母版 pPr.defRPr, 母版 txStyles 的对应 style]，按优先级从高到低。"""
    paras: list[Paragraph] = []
    for j, p in enumerate(_kids(tx_body, "p", ns=_A)):
        ppr = _kid(p, "pPr", ns=_A)
        level = max(0, min(8, _int(_attr(ppr, "lvl"), 0) or 0))
        flat = _expand_entries(entries, level)
        para_sources = [_own_defrpr(ppr), _kid(p, "endParaRPr", ns=_A)] + flat
        holders = [ppr] + [_level_ppr(e, level) for e in entries if e is not None]

        para = Paragraph(
            pid=f"{el_id}-p{j + 1:02d}",
            level=level,
            align=res.align(holders),
            bullet="inherit",
            default_size_pt=res.resolve(para_sources, "size_pt"),
            default_color=res.resolve(para_sources, "color"),
            default_font=res.resolve(para_sources, "font"),
            default_bold=res.resolve(para_sources, "bold"),
        )

        # -- 缩进：marL（左边距）/ indent（首行相对左边距的偏移，负数=悬挂缩进）--
        # 和项目符号一样要逐级往下找：真实课件的缩进也常写在母版 bodyStyle 上。
        marL = _int(_attr(ppr, "marL")) if ppr is not None else None
        first = _int(_attr(ppr, "indent")) if ppr is not None else None
        for holder in holders:
            if holder is None:
                continue
            if marL is None and _attr(holder, "marL") is not None:
                marL = _int(_attr(holder, "marL"), 0)
            if first is None and _attr(holder, "indent") is not None:
                first = _int(_attr(holder, "indent"), 0)
            if marL is not None and first is not None:
                break
        marL = marL or 0
        first = first or 0
        para.margin_left_px = emu_to_px(marL)
        para.first_line_px = emu_to_px(first)
        para.indent_px = emu_to_px(marL + first)      # 兼容旧字段：首行左边界

        # -- 段前 / 段后（spcPts 单位 1/100 pt；spcPct 依赖字号，留 None）--
        for tag, name in (("spcBef", "space_before_pt"), ("spcAft", "space_after_pt")):
            pts = _kid(_kid(ppr, tag, ns=_A), "spcPts", ns=_A)
            v = _int(_attr(pts, "val")) if pts is not None else None
            if v is not None:
                setattr(para, name, round(v / 100.0, 2))

        # -- 行距：pPr → 形状 → 版式 → 母版 → txStyles，取第一个写了的 --
        lnspc = None
        for holder in ([ppr] + [_level_ppr(e, level) for e in entries if e is not None]):
            lnspc = _kid(holder, "lnSpc", ns=_A)
            if lnspc is not None:
                break
        pct = _kid(lnspc, "spcPct", ns=_A) if lnspc is not None else None
        v = _int(_attr(pct, "val")) if pct is not None else None
        if v is not None:
            para.line_spacing = round(v / 100000.0, 3)

        # -- 项目符号：逐级往下找第一个**明确写了**的（buNone / buChar / buAutoNum）--
        # 只认段落的 pPr 是不够的：真实课件里项目符号几乎都写在母版的
        # txStyles（bodyStyle 的 lvl1pPr buChar）上，那样一个符号都读不到，
        # 重绘出来就是"PowerPoint 有圆点、我们没有"的差别（实测第 4 页就是这样）。
        for holder in holders:
            if holder is None:
                continue
            if _kid(holder, "buNone", ns=_A) is not None:
                para.bullet = "none"
            elif _kid(holder, "buChar", ns=_A) is not None:
                para.bullet = "char"
                para.bullet_char = str(_attr(_kid(holder, "buChar", ns=_A), "char", "") or "")
            elif _kid(holder, "buAutoNum", ns=_A) is not None:
                para.bullet = "auto"
            if para.bullet != "inherit":
                break
        # 纯文本框/普通形状的默认是"无符号"；占位符与形状级 lstStyle 才是"继承"。
        # 都不写就走 none，行为与 PowerPoint 一致。
        if para.bullet == "inherit":
            para.bullet = "none"

        # -- runs（a:br 也当成一个 "\n" run，样式同样走继承链）--
        texts: list[str] = []
        for node in p:
            n = _local(node)
            if n in ("r", "fld", "br"):
                rpr = _kid(node, "rPr", ns=_A)
                src = [rpr] + para_sources
                if n == "br":
                    txt = "\n"
                else:
                    t = _kid(node, "t", ns=_A)
                    txt = (t.text or "") if t is not None else ""
                para.runs.append(RunStyle(
                    text=txt,
                    bold=res.resolve(src, "bold"),
                    italic=res.resolve(src, "italic"),
                    underline=res.resolve(src, "underline"),
                    strike=res.resolve(src, "strike"),
                    size_pt=res.resolve(src, "size_pt", "size_pt"),
                    color=res.resolve(src, "color", "color"),
                    font=res.resolve(src, "font", "font"),
                    font_ea=res.resolve(src, "font_ea"),
                    baseline=res.resolve(src, "baseline") or "",
                    spacing_pt=res.resolve(src, "spacing_pt"),
                ))
                texts.append(txt)
        para.text = "".join(texts)
        paras.append(para)
    return paras


def _parse_text_body(tx_body, el_id: str, res: _Resolver, entries: list) -> TextBody:
    tb = TextBody()
    bp = _kid(tx_body, "bodyPr", ns=_A)
    if bp is not None:
        tb.anchor = {"t": "top", "ctr": "middle", "b": "bottom"}.get(bp.get("anchor") or "t", "top")
        tb.wrap = (bp.get("wrap") or "square") != "none"
        naf = _kid(bp, "normAutofit", ns=_A)
        if naf is not None:
            tb.autofit = "norm"
            fs = _int(_attr(naf, "fontScale"))
            tb.font_scale = round((fs if fs is not None else 100000) / 100000.0, 4)
        elif _kid(bp, "spAutoFit", ns=_A) is not None:
            tb.autofit = "shape"
        else:
            tb.autofit = "none"
        tb.insets_px = [
            emu_to_px(_int(bp.get("lIns"), DEF_INSET_LR_EMU)),
            emu_to_px(_int(bp.get("tIns"), DEF_INSET_TB_EMU)),
            emu_to_px(_int(bp.get("rIns"), DEF_INSET_LR_EMU)),
            emu_to_px(_int(bp.get("bIns"), DEF_INSET_TB_EMU)),
        ]
        vert = bp.get("vert") or "horz"
        tb.vertical = vert
        if vert not in ("horz", "horzOverflowing"):
            res.warn(f"{el_id} 的文字方向 vert={vert!r} 非 horz，前端可能显示不对")
    tb.paras = _parse_para_body(tx_body, el_id, res, entries)
    return tb


# ---------------------------------------------------------------------------
# 自由形状：custGeom → SVG path
# ---------------------------------------------------------------------------

def _custgeom_to_svg(cust_geom, box_w: float, box_h: float, warn) -> str:
    """<a:custGeom><a:pathLst> → SVG path 字符串。

    path 里的坐标在 path 自己的 w/h 空间里，这里**缩放到 box 的 w/h**，
    前端把 svg 的 viewBox 设成 box 尺寸即可直接用。
    """
    pl = _kid(cust_geom, "pathLst", ns=_A)
    if pl is None:
        return ""
    parts: list[str] = []
    for path in _kids(pl, "path", ns=_A):
        pw = _int(path.get("w"), 0) or 0
        ph = _int(path.get("h"), 0) or 0
        sx = (box_w / pw) if (pw and box_w) else 1.0
        sy = (box_h / ph) if (ph and box_h) else 1.0
        f = lambda v, s: round(float(v) * s, 3)
        seg: list[str] = []
        ok = True
        for cmd in path:
            n = _local(cmd)
            pts = [(f(_int(pt.get("x"), 0) or 0, sx), f(_int(pt.get("y"), 0) or 0, sy))
                   for pt in _kids(cmd, "pt", ns=_A)]
            if n == "moveTo" and pts:
                seg.append(f"M {pts[0][0]} {pts[0][1]}")
            elif n == "lnTo" and pts:
                seg.append(f"L {pts[0][0]} {pts[0][1]}")
            elif n == "cubicBezTo" and len(pts) == 3:
                seg.append("C " + " ".join(f"{x} {y}" for x, y in pts))
            elif n == "quadBezTo" and len(pts) == 2:
                seg.append("Q " + " ".join(f"{x} {y}" for x, y in pts))
            elif n == "close":
                seg.append("Z")
            else:
                warn(f"custGeom 命令 <a:{n}> 不支持，跳过该子路径")
                ok = False
                break
        if ok and seg:
            parts.append(" ".join(seg))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------

def _ph_of(sp):
    """<p:sp> 上的 <p:ph>（注意路径是 nvSpPr/nvPr/ph，nvPr 与 cNvSpPr 是兄弟节点）。"""
    return _kid(_kid(_kid(sp, "nvSpPr", ns=_P), "nvPr", ns=_P), "ph", ns=_P)


def _layout_placeholder_elements(holder_el) -> list:
    """版式/母版里所有占位符 <p:sp>，按文档顺序。"""
    if holder_el is None:
        return []
    return [sp for sp in holder_el.iter(_P + "sp") if _ph_of(sp) is not None]


def _match_ph(sps: list, idx, ptype):
    """在版式/母版占位符里找与页面占位符对应的那个：先 idx，再 type。"""
    cands = []
    for sp in sps:
        ph = _ph_of(sp)
        cands.append((sp, _int(ph.get("idx")), (ph.get("type") or "body")))
    if idx is not None:
        for sp, i, _t in cands:
            if i == idx:
                return sp
    if ptype is not None:
        for sp, _i, t in cands:
            if t == ptype:
                return sp
        alt = {"ctrTitle": "title", "title": "ctrTitle"}.get(ptype)
        if alt:
            for sp, _i, t in cands:
                if t == alt:
                    return sp
    if ptype is None:
        for sp, i, t in cands:
            if i == 1 or t == "body":
                return sp
    return None


def _find_merge_anchor(raw: list, ri: int, ci: int) -> str:
    """返回吞并了 (ri, ci) 的左上角单元格 pid（找不到返回 ""）。"""
    for r0 in range(ri, -1, -1):
        row = raw[r0]
        for c0 in range(min(ci, len(row) - 1), -1, -1):
            c = row[c0]
            if c.merged_into:
                continue
            if 0 <= ri - r0 < max(1, c.row_span) and 0 <= ci - c0 < max(1, c.col_span):
                return c.pid
    return ""


def _parse_table(graphic_frame, el_id: str, res: _Resolver, entries: list,
                 theme: _Theme, warn) -> TableData:
    td = TableData()
    # 注意：p:graphicFrame 里是 a:graphic/a:graphicData（不是 p:graphic）
    gd = graphic_frame.find(f"{_A}graphic/{_A}graphicData")
    tbl = _kid(gd, "tbl", ns=_A)
    if tbl is None:
        warn(f"{el_id} 的 graphicFrame 里没有 <a:tbl>")
        return td
    tblpr = _kid(tbl, "tblPr", ns=_A)
    td.first_row = _flag(_attr(tblpr, "firstRow"))
    td.banded = _flag(_attr(tblpr, "bandRow"))

    for gc in _kids(_kid(tbl, "tblGrid", ns=_A), "gridCol", ns=_A):
        td.col_w_px.append(emu_to_px(_int(gc.get("w"), 0) or 0))

    raw: list[list[TableCell]] = []
    k = 0
    for ri, tr in enumerate(_kids(tbl, "tr", ns=_A)):
        td.row_h_px.append(emu_to_px(_int(tr.get("h"), 0) or 0))
        row: list[TableCell] = []
        for tc in _kids(tr, "tc", ns=_A):
            k += 1
            cell = TableCell()
            # 单元格锚点：行优先顺序编号 c01、c02…（schema 的 s03-sh03-c04 形式，
            # SlideDoc.find_anchor / anchor_index 都按这个约定找）
            cell.pid = f"{el_id}-c{k:02d}"
            tx = _kid(tc, "txBody", ns=_A)
            if tx is not None:
                cell.paras = _parse_para_body(tx, cell.pid, res, entries)
            cell.text = "\n".join(p.text for p in cell.paras)
            cell.col_span = _int(tc.get("gridSpan"), 1) or 1
            cell.row_span = _int(tc.get("rowSpan"), 1) or 1
            cell.fill = _parse_fill(_kid(tc, "tcPr", ns=_A), theme, warn)
            # 被合并掉的格子（hMerge/vMerge=1）先打标，第二遍再指向左上角
            cell.merged_into = "?" if (_flag(tc.get("hMerge")) or _flag(tc.get("vMerge"))) else ""
            row.append(cell)
        raw.append(row)

    for ri, row in enumerate(raw):
        for ci, cell in enumerate(row):
            if cell.merged_into:
                cell.merged_into = _find_merge_anchor(raw, ri, ci)
                if not cell.merged_into:
                    warn(f"{cell.pid} 标了 hMerge/vMerge 但找不到左上角单元格，merged_into 留空")
    td.cells = raw
    return td


# ---------------------------------------------------------------------------
# 主解析器
# ---------------------------------------------------------------------------

class _Ingest:
    def __init__(self, prs: Presentation, media_dir: Path, *,
                 download_external: bool, external_timeout: float,
                 download_dir: Path | None, media_index: dict, media_bytes: dict):
        self.prs = prs
        self.media_dir = media_dir
        self.download_external = download_external
        self.external_timeout = external_timeout
        self.download_dir = download_dir or media_dir
        self.media_index = media_index
        self.media_bytes = media_bytes
        self.warnings: list[str] = []
        self._theme_cache: dict[str, _Theme] = {}
        self._warn_seen: set[str] = set()
        self.spid_map: dict[int, str] = {}
        self.provenance: dict[str, dict[str, int]] = {}

    # -- 基础 ---------------------------------------------------------------
    def warn(self, msg: str, *, dedup: bool = False) -> None:
        if dedup and msg in self._warn_seen:
            return
        self._warn_seen.add(msg)
        self.warnings.append(msg)

    def theme_for(self, slide) -> _Theme:
        master = slide.slide_layout.slide_master
        key = str(getattr(master.part, "partname", "master"))
        t = self._theme_cache.get(key)
        if t is not None:
            return t
        root = None
        for rel in master.part.rels.values():
            if "theme" in rel.reltype:
                try:
                    root = etree.fromstring(rel.target_part.blob)
                except Exception:
                    root = None
                break
        if root is None:
            self.warn(f"母版 {key} 找不到 theme part，schemeClr 只能用默认映射近似")
        t = _parse_theme(master._element, root)
        self._theme_cache[key] = t
        return t

    @staticmethod
    def _tx_styles(slide):
        return _kid(slide.slide_layout.slide_master._element, "txStyles", ns=_P)

    # -- 媒体 ---------------------------------------------------------------
    def media_for_rid(self, part, rid: str) -> tuple[str, bytes | None]:
        """r:embed → (src, 字节)。媒体已整包抽到 media/，这里只做索引。"""
        try:
            target = part.rels[rid].target_partname
            blob = part.rels[rid].target_part.blob
        except Exception:
            self.warn(f"r:embed={rid} 解析失败，图片跳过")
            return "", None
        name = PurePosixPath(str(target)).name
        if name not in self.media_index:
            self.media_dir.mkdir(parents=True, exist_ok=True)
            (self.media_dir / name).write_bytes(blob)
            self.media_index[name] = f"media/{name}"
        self.media_bytes.setdefault(name, blob)
        return self.media_index[name], blob

    def download(self, url: str) -> str:
        """下载外链图片到媒体目录；失败只记 warning，不抛。

        `file:///` 一律不试：那是指向**作者本机**的路径（实测这份课件里的
        `file:///D:\\qq文件\\...\\Image2\\xxx.png`），在这台机器上不存在，
        试也是白试，只会在体检报告里刷屏。前端拿 external_url 兜底。
        """
        if url.startswith("file:"):
            self.warn(f"外链图片指向作者本机路径，无法取到（已保留 external_url 供前端兜底）："
                      f"{url}", dedup=True)
            return ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deck-ingest/1.0"})
            with urllib.request.urlopen(req, timeout=self.external_timeout) as resp:
                blob = resp.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # 同一张图可能被很多页引用（页眉/版式装饰尤其如此），去重后再报，
            # 否则几十条同样的告警会把真正的问题淹掉
            self.warn(f"外链图片下载失败（external_url 已保留，可由前端兜底）：{url} → "
                      f"{type(exc).__name__}: {exc}", dedup=True)
            return ""
        name = PurePosixPath(urllib.parse.urlparse(url).path).name or "download.img"
        if not Path(name).suffix:
            name += ".img"
        stem, suffix = Path(name).stem, Path(name).suffix
        cand, i = name, 1
        while (self.download_dir / cand).exists():
            cand = f"{stem}_{i}{suffix}"
            i += 1
        self.download_dir.mkdir(parents=True, exist_ok=True)
        (self.download_dir / cand).write_bytes(blob)
        self.media_bytes[cand] = blob
        self.media_index[cand] = f"media/{cand}"
        return self.media_index[cand]

    # -- 几何 ---------------------------------------------------------------
    @staticmethod
    def _xfrm_box(el) -> tuple | None:
        if el is None:
            return None
        xfrm = _kid(_kid(el, "spPr", ns=_P), "xfrm", ns=_A)
        if xfrm is None:
            return None
        off, ext = _kid(xfrm, "off", ns=_A), _kid(xfrm, "ext", ns=_A)
        return (_int(_attr(off, "x"), 0) or 0, _int(_attr(off, "y"), 0) or 0,
                _int(_attr(ext, "cx"), 0) or 0, _int(_attr(ext, "cy"), 0) or 0)

    def _ph_lineage(self, slide, idx, ptype) -> list:
        """[版式占位符 <p:sp>, 母版占位符 <p:sp>]，用于几何与文字样式的继承。

        实测注意：课件里绝大多数 <p:ph> **不写 idx**（只写 type="title"），
        所以不能只按 idx 匹配 —— 先按 idx，再按 type（ctrTitle ⇄ title 互认）。
        """
        out = []
        for holder in (slide.slide_layout, slide.slide_layout.slide_master):
            out.append(_match_ph(_layout_placeholder_elements(holder._element), idx, ptype))
        return out

    @staticmethod
    def _apply_tf(box, tf):
        ox, oy, sx, sy = tf
        x, y, w, h = box
        return (ox + x * sx, oy + y * sy, w * sx, h * sy)

    # -- 页 ---------------------------------------------------------------
    def parse_slide(self, slide, idx: int) -> SlideDoc:
        self.spid_map = {}
        theme = self.theme_for(slide)
        sdoc = SlideDoc(index=idx, layout_name=(slide.slide_layout.name or ""))
        sdoc.background = self._bg_for(slide, theme)
        if slide.has_notes_slide:
            try:
                sdoc.notes = slide.notes_slide.notes_text_frame.text or ""
            except Exception:
                sdoc.notes = ""

        sp_tree = _kid(_kid(slide._element, "cSld", ns=_P), "spTree", ns=_P)
        sdoc.elements = self._walk(sp_tree, slide, theme, idx,
                                   prefix=f"s{idx + 1:02d}", top=True,
                                   tf=(0.0, 0.0, 1.0, 1.0), counter=[0])
        sdoc.decorations = self._parse_decorations(slide, theme, idx)

        sdoc.steps = self._parse_timing(slide._element, idx, sdoc.elements)
        sdoc.text_flat = "\n".join(e.plain_text for e in self._reading_order(sdoc.elements)
                                   if e.plain_text.strip())
        title = ""
        for e in sdoc.elements:
            if e.is_title and e.plain_text.strip():
                title = e.plain_text.strip().splitlines()[0]
                break
        if not title:      # 兜底：本页最上方有文字的元素，截断 40 字
            for e in self._reading_order(sdoc.elements):
                if e.plain_text.strip():
                    title = e.plain_text.strip().splitlines()[0][:40]
                    break
        sdoc.title = title
        return sdoc

    @staticmethod
    def _reading_order(elements: list[Element]) -> list[Element]:
        return sorted(elements, key=lambda e: (round(e.box[1], 2), round(e.box[0], 2), e.z))

    # -- 版式 / 母版上的装饰形状 -------------------------------------------
    class _Holder:
        """把 layout / master 伪装成 slide，好复用 `_walk`。

        `_walk` 一路要 `slide.slide_layout` / `slide.slide_master` / `slide.part`，
        而版式对象自己没有 `.slide_layout`（它本身不是 slide）。这个小壳子补齐这些；
        顺带让图片的关系解析走对 part（版式里的图片在版式自己的 rels 里）。
        """

        def __init__(self, holder, master, layout) -> None:
            self._element = holder._element
            # 注意：`.slide_layout` 要始终指向**版式**（它才有 `.slide_master`）。
            # 走母版自己的 spTree 时如果把 `.slide_layout` 指向母版，
            # 解析里那句 `slide.slide_layout.slide_master` 就会炸。
            self.slide_layout = layout or holder
            self.slide_master = master or holder
            self.part = holder.part

    def _parse_decorations(self, slide, theme: _Theme, idx: int) -> list[Element]:
        """版式/母版上的装饰形状 —— **只画不锚**。

        为什么必须收：这份课件的封面大箭头、"人教版八上（2024）"背后的蓝色块、
        每页底部的色条、页角的像素小图，全都在版式上。不收的话重绘出来是"干净但少了东西"
        的封面（甚至出现"白字白底"—— 文字在 slide 上、蓝色底在版式上）。
        （实测：封面与 PowerPoint 的像素差异里，绝大部分就是这些形状。）

        收哪些：
          - 非占位符形状：全收（版式的图形装饰通常就是这类）
          - 占位符：只收**没有文字**且**有可见外观**的（页脚色条、页码底板就是这类）；
            带文字的占位符是"单击此处编辑母版标题样式"这种提示文字，
            真的内容在 slide 自己的占位符里，收了会重复/串味
        不收哪些：
          - `showMasterSp="0"` 的层级（PowerPoint 语义就是"这层不显示母版形状"）
        """
        out: list[Element] = []
        layout = getattr(slide, "slide_layout", None)
        master = getattr(layout, "slide_master", None)
        if layout is None:
            return out

        def _shows_master(el) -> bool:
            return str(_attr(el, "showMasterSp", "1") or "1") not in ("0", "false")

        # 层序：母版在最下、版式压住它、slide 元素在最后 —— 所以要**先收母版**。
        # （反过来收会把母版的大色块盖在版式的白底上：实测第 4/34 页因此从
        #   "基本一致"变成"多出一块大箭头"。）
        layers = [("ms", master, master is not None and _shows_master(slide._element)
                   and _shows_master(layout._element)),
                  ("la", layout, _shows_master(slide._element))]
        for tag, holder, enabled in layers:
            if not enabled:
                continue
            sp_tree = _kid(_kid(holder._element, "cSld", ns=_P), "spTree", ns=_P)
            if sp_tree is None:
                continue
            try:
                els = self._walk(sp_tree, self._Holder(holder, master, layout), theme,
                                 idx, prefix=f"s{idx + 1:02d}-{tag}", top=True,
                                 tf=(0.0, 0.0, 1.0, 1.0), counter=[0])
            except Exception as e:      # 版式结构千奇百怪，装饰画不出来不该毁掉整页
                self.warn(f"第 {idx + 1} 页解析版式装饰失败（{tag}）："
                          f"{type(e).__name__}: {e}", dedup=True)
                continue
            for e in els:
                if e.hidden or (e.box[2] <= 0 and e.box[3] <= 0):
                    continue
                # 占位符：只留"没有文字、但有外观"的（页脚色条、页码底板）。
                # 带文字的是"单击此处编辑母版标题样式"这类提示，真正的内容在 slide 上。
                if e.kind == KIND_PLACEHOLDER:
                    if e.plain_text.strip():
                        continue
                    if not (e.fill or e.line or e.pic or e.svg_path):
                        continue
                e.source = "layout" if tag == "la" else "master"
                e.decorative = True
                e.is_title = False
                out.append(e)
        return out

    # -- 递归遍历 -----------------------------------------------------------
    def _walk(self, container, slide, theme: _Theme, idx: int, prefix: str,
              top: bool, tf, counter: list) -> list[Element]:
        out: list[Element] = []
        j = 0                                  # 容器内**形状**序号（跳过 nvGrpSpPr/grpSpPr）
        for el in container:
            tag = _local(el)
            if tag not in ("sp", "pic", "graphicFrame", "cxnSp", "grpSp"):
                continue
            j += 1
            if top:
                counter[0] += 1
                eid = f"{prefix}-sh{counter[0]:02d}"
            else:
                eid = f"{prefix}-g{j:02d}"
                counter[0] += 1
            z = counter[0]

            if tag == "grpSp":
                e = self._element(el, eid, z, tf, theme, idx, slide, KIND_GROUP)
                if e is None:
                    continue
                out.append(e)
                child_tf = self._group_tf(el, tf)
                kids = self._walk(el, slide, theme, idx, eid, False, child_tf, counter)
                out.extend(kids)
                # 组合自己没写 xfrm（python-pptx 建的组合就是这样）时，
                # box 退化成 0×0 —— 用子形状的并集补上，前端才知道该画在哪
                if (e.box[2] <= 0 and e.box[3] <= 0) and kids:
                    xs = [k.box for k in kids]
                    x0 = min(b[0] for b in xs)
                    y0 = min(b[1] for b in xs)
                    x1 = max(b[0] + b[2] for b in xs)
                    y1 = max(b[1] + b[3] for b in xs)
                    e.box = [x0, y0, round(x1 - x0, 2), round(y1 - y0, 2)]
                if not e.plain_text.strip() and any(k.plain_text.strip() for k in kids):
                    e.role = ROLE_BODY
                    e.decorative = False
                continue
            e = self._element(el, eid, z, tf, theme, idx, slide, None)
            if e is not None:
                out.append(e)
        return out

    @staticmethod
    def _group_tf(grp_el, tf):
        """组合的子坐标 → 页面 EMU 的仿射变换（off/ext/chOff/chExt）。"""
        xfrm = _kid(_kid(grp_el, "grpSpPr", ns=_P), "xfrm", ns=_A)
        if xfrm is None:
            return tf
        off, ext = _kid(xfrm, "off", ns=_A), _kid(xfrm, "ext", ns=_A)
        coff, cext = _kid(xfrm, "chOff", ns=_A), _kid(xfrm, "chExt", ns=_A)
        cx0 = _int(_attr(ext, "cx"), 0) or 0
        cy0 = _int(_attr(ext, "cy"), 0) or 0
        chcx = _int(_attr(cext, "cx"), 0) or 0
        chcy = _int(_attr(cext, "cy"), 0) or 0
        gsx = (cx0 / chcx) if chcx else 1.0
        gsy = (cy0 / chcy) if chcy else 1.0
        gox = (_int(_attr(off, "x"), 0) or 0) - (_int(_attr(coff, "x"), 0) or 0) * gsx
        goy = (_int(_attr(off, "y"), 0) or 0) - (_int(_attr(coff, "y"), 0) or 0) * gsy
        ox, oy, sx, sy = tf
        return (ox + gox * sx, oy + goy * sy, sx * gsx, sy * gsy)

    # -- 单个元素 -----------------------------------------------------------
    def _element(self, el, eid: str, z: int, tf, theme: _Theme, slidx: int,
                 slide, kind_hint) -> Element:
        tag = _local(el)
        nv = None
        for cand in ("nvSpPr", "nvPicPr", "nvGraphicFramePr", "nvCxnSpPr", "nvGrpSpPr"):
            nv = _kid(el, cand, ns=_P)
            if nv is not None:
                break
        cnv = _kid(nv, "cNvPr", ns=_P)
        shape_id = _int(_attr(cnv, "id"), 0) or 0
        name = _attr(cnv, "name", "") or ""
        alt = _attr(cnv, "descr", "") or ""
        show = _attr(cnv, "show")
        hidden = (_flag(show) is False and show is not None) or _flag(_attr(cnv, "hidden"))
        e = Element(element_id=eid, shape_id=shape_id, name=name, z=z, hidden=hidden)
        if shape_id:
            self.spid_map[shape_id] = eid

        sp_pr = _kid(el, "spPr", ns=_P)
        ph = el.find(f".//{_P}ph")
        ph_idx = _int(_attr(ph, "idx")) if ph is not None else None
        ph_type = _attr(ph, "type") if ph is not None else None
        lineage = self._ph_lineage(slide, ph_idx, ph_type) if ph is not None else [None, None]

        # ---- 几何：显式 xfrm → 版式占位符 → 母版占位符 ----
        box_emu = self._xfrm_box(el)
        if box_emu is None and tag == "graphicFrame":
            xfrm = _kid(el, "xfrm", ns=_P)
            if xfrm is None:
                xfrm = _kid(el, "xfrm", ns=_A)
            off, ext = _kid(xfrm, "off", ns=_A), _kid(xfrm, "ext", ns=_A)
            box_emu = (_int(_attr(off, "x"), 0) or 0, _int(_attr(off, "y"), 0) or 0,
                       _int(_attr(ext, "cx"), 0) or 0, _int(_attr(ext, "cy"), 0) or 0)
        if box_emu is None and ph is not None:
            for holder in lineage:
                box_emu = self._xfrm_box(holder)
                if box_emu is not None:
                    break
        if box_emu is None:
            box_emu = (0, 0, 0, 0)
            if tag == "sp" and kind_hint != KIND_GROUP:
                self.warn(f"第 {slidx + 1} 页 {eid}（{name!r}）没有几何信息，按 0 处理",
                          dedup=True)
        x, y, w, h = self._apply_tf(box_emu, tf)
        e.box = [emu_to_px(x), emu_to_px(y), emu_to_px(w), emu_to_px(h)]

        # 宽或高为 0 的图片（真实课件里有这种残余对象）→ 跳过 + warning
        if tag == "pic" and (e.box[2] <= 0 or e.box[3] <= 0):
            self.warn(f"第 {slidx + 1} 页 {eid}（{name!r}）宽或高为 0"
                      f"（{e.box[2]}×{e.box[3]}px，pptx 里的残余对象），跳过")
            return None

        xfrm = _kid(sp_pr, "xfrm", ns=_A)
        e.rot = (_int(_attr(xfrm, "rot"), 0) or 0) / 60000.0
        e.flip_h = _flag(_attr(xfrm, "flipH"))
        e.flip_v = _flag(_attr(xfrm, "flipV"))

        cust_geom = _kid(sp_pr, "custGeom", ns=_A)
        prst_geom = _kid(sp_pr, "prstGeom", ns=_A)
        is_title_ph = ph_type in ("title", "ctrTitle")
        res = _Resolver(theme, is_title_ph, self.provenance, self.warn)

        # ---- kind / preset ----
        if kind_hint == KIND_GROUP:
            e.kind = KIND_GROUP
        elif tag == "pic":
            e.kind = KIND_PICTURE
        elif tag == "graphicFrame":
            gd = el.find(f"{_A}graphic/{_A}graphicData")
            if _kid(gd, "tbl", ns=_A) is not None:
                e.kind = KIND_TABLE
            else:
                uri = _attr(gd, "uri", "") or ""
                e.kind = KIND_UNKNOWN
                self.warn(f"第 {slidx + 1} 页 {eid} 是 graphicFrame 但类型 {uri!r} 不支持"
                          f"（图表/SmartArt？），按 unknown 处理")
        elif tag == "cxnSp":
            e.kind = KIND_CONNECTOR
            e.preset = _attr(prst_geom, "prst", "") or ""
        elif tag == "sp":
            cnvsp = _kid(_kid(el, "nvSpPr", ns=_P), "cNvSpPr", ns=_P)
            if ph is not None:
                e.kind = KIND_PLACEHOLDER
                e.preset = _attr(prst_geom, "prst", "") or ""
            elif _flag(_attr(cnvsp, "txBox")):
                e.kind = KIND_TEXT
                e.preset = _attr(prst_geom, "prst", "") or ""
                if e.preset and e.preset != "rect":
                    # 文本框带了非矩形几何（本课件有 4 个 chevron 的序号牌）：
                    # kind 仍是 text（TEXT_BOX→text），但几何画不出来，得记一笔
                    self.warn(f"第 {slidx + 1} 页 {eid}（{name!r}）是文本框但几何是 "
                              f"prstGeom={e.preset!r}，按矩形文本框渲染（preset 字段保留）")
            elif cust_geom is not None:
                e.kind = KIND_FREEFORM          # preset 留空
            elif prst_geom is not None:
                prst = _attr(prst_geom, "prst", "") or "rect"
                e.preset = prst
                if prst in _KNOWN_PRST_KIND:
                    e.kind = _KNOWN_PRST_KIND[prst]
                else:
                    e.kind = KIND_RECT
                    self.warn(f"第 {slidx + 1} 页 {eid} 的 prstGeom={prst!r} 不认识，"
                              f"按 rect 渲染（preset 字段保留）")
            else:
                e.kind = KIND_RECT
        else:
            e.kind = KIND_UNKNOWN
            self.warn(f"第 {slidx + 1} 页 {eid} 的 <p:{tag}> 不支持，按 unknown 处理")

        # ---- 填充 / 描边 ----
        if e.kind != KIND_GROUP:
            e.fill = _parse_fill(sp_pr, theme, self.warn)
            if e.fill is not None and e.fill.kind == "image" and not e.fill.image:
                blip = sp_pr.find(f".//{_A}blip") if sp_pr is not None else None
                rid = _attr(blip, _R + "embed") if blip is not None else None
                if rid:
                    src, _ = self.media_for_rid(slide.part, rid)
                    e.fill.image = src
            e.line = _parse_line(sp_pr, theme, self.warn)

            # 占位符自己没有填充/描边时，按占位符链往下继承（版式 → 母版）。
            # PowerPoint 就是这么画的：封面"人教版八上（2024）"那行**白字**下面的
            # 蓝色箭头块，其实写在版式的占位符上；不继承的话，字是白的、底是空的，
            # 页面上就是"这一行消失了"（比画错更难发现）。
            if ph is not None and e.fill is None and e.line is None:
                for holder in lineage:
                    if holder is None:
                        continue
                    h_sp_pr = _kid(holder, "spPr", ns=_P)
                    if h_sp_pr is None:
                        continue
                    hf = _parse_fill(h_sp_pr, theme, self.warn)
                    hl = _parse_line(h_sp_pr, theme, self.warn)
                    if hf is not None or hl is not None:
                        e.fill, e.line = hf, hl
                        # 几何也从同一个占位符继承（同一块底 + 同一段文字才对得上）
                        if self._xfrm_box(_kid(el, "spPr", ns=_P)) is None:
                            hb = self._xfrm_box(holder)
                            if hb is not None:
                                x, y, w, h = self._apply_tf(hb, tf)
                                e.box = [emu_to_px(x), emu_to_px(y),
                                         emu_to_px(w), emu_to_px(h)]
                        # 轮廓预设也继承：版式上的占位符形状可能是箭头/圆角矩形，
                        # 不继承的话这块底会变成直角矩形（形状对不上）
                        if not e.preset and e.kind != KIND_FREEFORM:
                            h_preset = _attr(_kid(h_sp_pr, "prstGeom", ns=_A), "prst", "")
                            if h_preset and h_preset != "rect":
                                e.preset = h_preset
                        break
        if e.kind == KIND_FREEFORM and cust_geom is not None:
            e.svg_path = _custgeom_to_svg(cust_geom, e.box[2], e.box[3], self.warn)

        # ---- 文字（继承链）----
        tx_body = _kid(el, "txBody", ns=_P)
        if tx_body is not None and e.kind != KIND_GROUP:
            lph, mph = lineage
            ltb = _kid(lph, "txBody", ns=_P)      # 版式占位符的 txBody
            mtb = _kid(mph, "txBody", ns=_P)      # 母版占位符的 txBody
            entries = [
                _kid(tx_body, "lstStyle", ns=_A),                    # 形状 lstStyle
                _kid(ltb, "lstStyle", ns=_A),                        # 版式占位符 lstStyle
                _para_defrpr(ltb),                                   # 版式占位符 pPr.defRPr
                _kid(mtb, "lstStyle", ns=_A),                        # 母版占位符 lstStyle
                _para_defrpr(mtb),                                   # 母版占位符 pPr.defRPr
            ]
            tx_styles = self._tx_styles(slide)
            style_name = "titleStyle" if is_title_ph else (
                "bodyStyle" if ph is not None else "otherStyle")
            entries.append(_kid(tx_styles, style_name, ns=_P) if tx_styles is not None else None)
            e.text = _parse_text_body(tx_body, eid, res, entries)

        # ---- 图片 ----
        if e.kind == KIND_PICTURE:
            e.pic = self._parse_picture(el, slide, alt)

        # ---- 表格 ----
        if e.kind == KIND_TABLE:
            lst = _kid(_kid(el, "txBody", ns=_P), "lstStyle", ns=_A)
            e.table = _parse_table(el, eid, res, [lst], theme, self.warn)

        # ---- role ----
        if e.kind == KIND_PICTURE:
            e.role = ROLE_PICTURE
        elif e.kind == KIND_TABLE:
            e.role = ROLE_TABLE
        elif is_title_ph or "标题" in name:
            e.role = ROLE_TITLE
            e.is_title = True
        elif e.kind == KIND_GROUP:
            e.role = ROLE_DECOR if not e.plain_text.strip() else ROLE_BODY
            e.decorative = e.role == ROLE_DECOR
        elif not e.plain_text.strip():
            e.role = ROLE_DECOR              # 没文字又非图片/表格 → 装饰（LLM 不该锚它）
            e.decorative = True
        else:
            e.role = ROLE_BODY
        if is_title_ph:
            e.is_title = True
        return e

    # -- 图片 ---------------------------------------------------------------
    def _parse_picture(self, el, slide, alt: str) -> PictureData | None:
        blip = el.find(f".//{_A}blip")
        if blip is None:
            self.warn(f"图片 {alt or ''} 没有 <a:blip>，跳过")
            return None
        pd = PictureData(alt=alt)
        name = ""
        embed = blip.get(_R + "embed")
        if embed:
            src, blob = self.media_for_rid(slide.part, embed)
            if src:
                pd.src = src
                name = PurePosixPath(src).name
                if blob is not None:
                    self.media_bytes.setdefault(name, blob)
        link = blip.get(_R + "link")
        if link:
            try:
                pd.external_url = str(slide.part.rels[link].target_ref)
            except Exception:
                pd.external_url = ""
            if not pd.external_url:
                self.warn(f"r:link={link} 解析不出外链地址")
            elif self.download_external:
                src = self.download(pd.external_url)
                if src:
                    pd.src = src
                    name = PurePosixPath(src).name
        sr = el.find(f".//{_A}srcRect")
        if sr is not None:
            pd.crop = [round((_int(sr.get(k), 0) or 0) / 100000.0, 5)
                       for k in ("l", "t", "r", "b")]
        pd.natural_w, pd.natural_h = self._natural_size(pd.src, name)
        return pd

    def _natural_size(self, src: str, name: str) -> tuple[int, int]:
        data = self.media_bytes.get(name)
        try:
            from PIL import Image
            if data is not None:
                with Image.open(BytesIO(data)) as im:
                    return int(im.width), int(im.height)
            if src:
                with Image.open(self.media_dir.parent / src) as im:
                    return int(im.width), int(im.height)
        except Exception:
            pass          # 失败留 0（svg 之类本来就读不了）
        return 0, 0

    # -- 背景 ---------------------------------------------------------------
    def _bg_for(self, slide, theme: _Theme) -> Fill | None:
        for holder in (slide._element, slide.slide_layout._element,
                       slide.slide_layout.slide_master._element):
            bg = _kid(_kid(holder, "cSld", ns=_P), "bg", ns=_P)
            if bg is None:
                continue
            f = self._parse_bg(bg, theme)
            if f is not None:
                return f
        return None

    def _parse_bg(self, bg, theme: _Theme) -> Fill | None:
        bgpr = _kid(bg, "bgPr", ns=_P)
        if bgpr is not None:
            f = _parse_fill(bgpr, theme, self.warn)
            if f is not None:
                return f
        ref = _kid(bg, "bgRef", ns=_P)
        if ref is None:
            return None
        idx = _int(ref.get("idx"), 1001) or 1001
        cel = _first_color_child(ref)
        i = (idx - 1001) if idx >= 1001 else (idx - 1)
        if 0 <= i < len(theme.bg_fills):
            # bgRef 指向主题 fmtScheme/bgFillStyleLst，里面的 phClr 由 bgRef 的颜色填上
            f = _parse_fill(theme.bg_fills[i], theme, self.warn, phclr=cel)
            if f is not None:
                return f
        if cel is not None:
            hexv, alpha = _resolve_color(cel, theme, self.warn)
            if hexv:
                return Fill(kind="solid", color=hexv, alpha=alpha)
        self.warn(f"背景 bgRef idx={idx} 解析失败，按无背景处理")
        return None

    # -- 分步 ---------------------------------------------------------------
    def _parse_timing(self, slide_el, idx: int, elements: list[Element]) -> list[Step]:
        steps: list[Step] = []
        timing = slide_el.find(f"{_P}timing")
        if timing is not None:
            # 注意：<p:timing> 元素存在但里面一个 cTn 都没有**不算有动画**
            # （本课件 42 页里 38 页都是这种空壳），所以下面全程"取不到就走兜底"。
            for seq in timing.iter(f"{_P}seq"):
                ctn = _kid(seq, "cTn", ns=_P)
                if _attr(ctn, "nodeType") != "mainSeq":
                    continue
                for par in _kids(_kid(ctn, "childTnLst", ns=_P), "par", ns=_P):
                    gp = _kid(par, "cTn", ns=_P)
                    if gp is None:
                        continue
                    node_types = [_attr(x, "nodeType") for x in gp.iter(f"{_P}cTn")]
                    effects = [x for x in gp.iter(f"{_P}cTn")
                               if _attr(x, "presetClass") == "entr"
                               or _attr(x, "nodeType") in ("clickEffect", "withEffect",
                                                           "afterEffect")]
                    spids: list[int] = []
                    for eff in effects:
                        for tgt in eff.iter(f"{_P}spTgt"):
                            sid = _int(tgt.get("spid"))
                            if sid is not None and sid not in spids:
                                spids.append(sid)
                    eids = [self.spid_map[s] for s in spids if s in self.spid_map]
                    if not eids:
                        continue
                    delay = 0
                    for eff in effects:
                        for cond in _kids(_kid(eff, "stCondLst", ns=_P), "cond", ns=_P):
                            d = _int(cond.get("delay"))     # 1/1000 秒
                            if d:
                                delay = d
                                break
                        if delay:
                            break
                    steps.append(Step(
                        step_id=f"s{idx + 1:02d}-step{len(steps) + 1:02d}",
                        element_ids=eids,
                        source="anim",
                        trigger="click" if "clickPar" in node_types else "after",
                        delay_ms=delay,
                    ))
                break
        if steps:
            return steps
        # 兜底规则：没有动画的页 → 阅读序，把「有文字的元素」每个作为一步
        for e in self._reading_order(elements):
            if e.plain_text.strip():
                steps.append(Step(step_id=f"s{idx + 1:02d}-step{len(steps) + 1:02d}",
                                  element_ids=[e.element_id], source="rule",
                                  trigger="after", delay_ms=0))
        return steps


# ---------------------------------------------------------------------------
# 媒体抽取
# ---------------------------------------------------------------------------

def _extract_media(src: Path, media_dir: Path):
    """把 pptx 包内 ppt/media/* 原样抽到 media/，文件名保持 pptx 内的原名。"""
    index: dict[str, str] = {}
    blobs: dict[str, bytes] = {}
    media_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zf:
        for name in sorted(zf.namelist()):
            if not name.startswith("ppt/media/"):
                continue
            base = name.rsplit("/", 1)[-1]
            if not base:
                continue
            try:
                data = zf.read(name)
            except KeyError:
                continue
            (media_dir / base).write_bytes(data)
            blobs[base] = data
            index[base] = f"media/{base}"
    return index, blobs


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _build_stats(deck: DeckDoc, prov: dict) -> dict:
    kinds: dict[str, int] = {}
    fills: dict[str, int] = {}
    roles: dict[str, int] = {}
    n_el = n_pic = n_grad = n_free = n_svg = 0
    n_steps = n_anim = 0
    runs = runs_sz = runs_col = 0
    for s in deck.slides:
        n_steps += len(s.steps)
        n_anim += sum(1 for st in s.steps if st.source == "anim")
        for e in s.elements:
            n_el += 1
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
            roles[e.role] = roles.get(e.role, 0) + 1
            if e.kind == KIND_PICTURE:
                n_pic += 1
            if e.kind == KIND_FREEFORM:
                n_free += 1
            if e.svg_path:
                n_svg += 1
            if e.fill is not None:
                fills[e.fill.kind] = fills.get(e.fill.kind, 0) + 1
                if e.fill.kind == "gradient":
                    n_grad += 1
            for p in e.paragraphs:
                for r in p.runs:
                    runs += 1
                    if r.size_pt is not None:
                        runs_sz += 1
                    if r.color is not None:
                        runs_col += 1
            if e.table is not None:           # 表格单元格里的 run 也算
                for row in e.table.cells:
                    for cell in row:
                        for p in cell.paras:
                            for r in p.runs:
                                runs += 1
                                if r.size_pt is not None:
                                    runs_sz += 1
                                if r.color is not None:
                                    runs_col += 1
    return {
        "slides": deck.slide_count,
        "elements": n_el,
        "kinds": dict(sorted(kinds.items())),
        "roles": dict(sorted(roles.items())),
        "pictures": n_pic,
        "gradients": n_grad,
        "fills": dict(sorted(fills.items())),
        "freeforms": n_free,
        "svg_paths": n_svg,
        "steps": n_steps,
        "anim_steps": n_anim,
        "rule_steps": n_steps - n_anim,
        "runs": runs,
        "runs_with_size": runs_sz,
        "runs_with_color": runs_col,
        "size_source": prov.get("size_pt", {}),      # 字号最终来自哪一层
        "color_source": prov.get("color", {}),
        "media_files": len(deck.media),
        "warnings": len(deck.warnings),
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def ingest_pptx(path, *, deck_id: str | None = None, decks_root=None,
                max_slides: int | None = None, download_external: bool = True,
                external_timeout: float = 10.0, download_dir=None) -> DeckDoc:
    """解析 pptx → 填 DeckDoc → 落盘 decks/<deck_id>/ → 返回 DeckDoc。"""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"找不到 pptx：{src}")

    did = deck_id or deck_store.make_deck_id(src)
    root = Path(decks_root) if decks_root else None
    base = (root / did) if root else deck_store.deck_dir(did)
    media_dir = base / "media"

    media_index, media_bytes = _extract_media(src, media_dir)
    prs = Presentation(str(src))
    ing = _Ingest(prs, media_dir,
                  download_external=download_external,
                  external_timeout=external_timeout,
                  download_dir=Path(download_dir) if download_dir else None,
                  media_index=media_index, media_bytes=media_bytes)

    deck = DeckDoc(
        deck_id=did,
        title=(prs.core_properties.title or "").strip() or src.stem.strip(),
        source_path=str(src),
        source_sha1=sha1_file(src),
        width_px=int(round(float(prs.slide_width) / EMU_PER_PX)),
        height_px=int(round(float(prs.slide_height) / EMU_PER_PX)),
        media=dict(sorted(media_index.items())),
    )

    slides = list(prs.slides)
    if max_slides is not None:
        slides = slides[:max(0, int(max_slides))]
    for i, slide in enumerate(slides):
        deck.slides.append(ing.parse_slide(slide, i))

    deck.warnings = list(ing.warnings)
    # ingested_at 故意取**源文件 mtime** 的 ISO 时间而不是 wall-clock：
    # 验收要求「两次解析同一文件，deck.json 的 sha1 一致」，wall-clock 会让它必然不同。
    try:
        ts = _dt.datetime.fromtimestamp(src.stat().st_mtime).astimezone()
    except OSError:
        ts = _dt.datetime.now().astimezone()
    deck.ingested_at = ts.isoformat(timespec="seconds")
    deck.stats = _build_stats(deck, ing.provenance)

    deck_store.save_deck(deck, root=root)
    return deck


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(deck: DeckDoc, report: bool, elapsed: float) -> None:
    st = deck.stats
    print(f"《{deck.title}》  deck_id={deck.deck_id}")
    print(f"输出：decks/{deck.deck_id}/deck.json + media/（{st.get('media_files', 0)} 个媒体文件）"
          f"  画布 {deck.width_px}×{deck.height_px}px")
    print(f"页数 {st.get('slides', 0)} · 元素 {st.get('elements', 0)} · "
          f"图片 {st.get('pictures', 0)} · 渐变 {st.get('gradients', 0)} · "
          f"自由形状 {st.get('freeforms', 0)} · 步骤 {st.get('steps', 0)}"
          f"（anim {st.get('anim_steps', 0)} / rule {st.get('rule_steps', 0)}）· "
          f"警告 {st.get('warnings', 0)} · 耗时 {elapsed:.2f}s")
    print(f"kind: {st.get('kinds')}")
    print(f"fill: {st.get('fills')}")
    print(f"字号来源: {st.get('size_source')}")
    if report:
        print("-" * 72)
        for s in deck.slides:
            print(f"  第 {s.index + 1:>2} 页 · {(s.title[:28] or '（无标题）'):<30} "
                  f"元素 {len(s.elements):>2} · 文字 {len(s.text_flat):>4} 字 · "
                  f"步骤 {len(s.steps):>2} · 背景 {(s.background.kind if s.background else 'None')}")
        print("-" * 72)
        for w in deck.warnings[:20]:
            print("  ⚠ " + w)
        if len(deck.warnings) > 20:
            print(f"  … 其余 {len(deck.warnings) - 20} 条见 deck.json 的 warnings")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m deck.pptx_ingest",
        description="把 pptx 解析成 decks/<deck_id>/deck.json（DeckDoc）")
    ap.add_argument("pptx", help="pptx 文件路径")
    ap.add_argument("--out", default=None, help="decks 根目录（默认 prototype/decks）")
    ap.add_argument("--max-slides", type=int, default=None, help="只解析前 N 页")
    ap.add_argument("--no-download", action="store_true", help="不下载 r:link 外链图片")
    ap.add_argument("--report", action="store_true", help="打印逐页明细与警告")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    deck = ingest_pptx(args.pptx, decks_root=args.out, max_slides=args.max_slides,
                       download_external=not args.no_download)
    _print_summary(deck, args.report, time.perf_counter() - t0)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
