"""课件数据契约 —— 真实 PPT 解析结果与前端重绘之间的唯一接口。

为什么要有这一层
----------------
《课堂交互设计》§2.5 给了三条课件来源路线，本层服务第三条：
**教师上传 PPT + 前端重绘 → 能拿到完整的元素级控制力**。

要做到"元素级控制"，解析结果就必须是一次性算好、可序列化、与 Python/前端
双方解耦的**纯数据**：几何（px）、填充、字体、图片、段落、出场步骤。
解析器负责"把 pptx 变成它"，前端只负责"照着画"，两边不互相猜。

两个关键设计决定
----------------
1. **参考画布 1280×720 固定 px**：EMU 按 96 dpi 换算成 px（13.333in × 96 = 1280）。
   前端把整块画布 `transform: scale()` 自适应容器宽度，于是
   **文字换行是确定性的**（不会因为窗口大小改变而重排），
   字体 px = pt × 4/3，与 PowerPoint 在 96 dpi 下的排版一致。
2. **段落级 id**：真实的"单词解析"课件把 音标/释义/用法/搭配/例句 全塞在
   一个文本框里。若锚点只能到"整个文本框"，讲解就只能整块高亮 ——
   这正是 §2.5 说的"锚点粒度变粗"。
   既然是自己重绘，就把每个段落也做成独立节点，锚点可以细到一行：
       element_id = "s04-sh02"        （形状）
       para_id    = "s04-sh02-p03"    （形状内第 3 段）
   锚点索引与高亮对二者一视同仁。

单位约定
--------
- `box` / 坐标：px（参考画布坐标，相对页面左上角）
- 字号：pt（前端 ×4/3 转 px）
- 颜色：`#RRGGBB`；透明度单独用 `alpha`（0~1）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: 参考画布。EMU→px 用 96dpi：914400 EMU = 1in = 96px
EMU_PER_PX = 9525

#: 16:9 默认；解析器会按真实页面尺寸覆盖
DEFAULT_W = 1280
DEFAULT_H = 720


def emu_to_px(v: Any) -> float:
    """EMU → px（96 dpi）。None 当 0 处理 —— 形状缺几何时不该炸，只用警告标出。"""
    if v is None:
        return 0.0
    return round(float(v) / EMU_PER_PX, 2)


def px_to_emu(v: float) -> int:
    return int(round(v * EMU_PER_PX))


def sha1_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ===========================================================================
# 样式
# ===========================================================================

@dataclass
class GradientStop:
    pos: float = 0.0          # 0~1
    color: str = "#FFFFFF"
    alpha: float = 1.0


@dataclass
class Fill:
    """填充。kind: none | solid | gradient | image"""

    kind: str = "none"
    color: str = "#FFFFFF"
    alpha: float = 1.0
    gradient: list[GradientStop] = field(default_factory=list)
    angle: float = 0.0        # 度，0 = 从左到右
    #: kind == image 时：媒体文件名
    image: str = ""


@dataclass
class LineStyle:
    """描边。kind: none | solid | gradient"""

    kind: str = "none"
    color: str = "#000000"
    alpha: float = 1.0
    width_px: float = 0.0
    dash: str = ""            # "" | dash | dot | lgDash | sysDash
    cap: str = "flat"         # flat | round | square
    arrow: str = ""           # "" | triangle 等（线条类形状用）


@dataclass
class RunStyle:
    """一段连续同样式文字。"""

    text: str = ""
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    size_pt: float | None = None
    color: str | None = None
    font: str | None = None       # 西文字体
    font_ea: str | None = None    # 东亚字体
    baseline: str = ""            # "" | superscript | subscript
    spacing_pt: float | None = None   # 字间距


@dataclass
class Paragraph:
    """一段（PPT 里的一次回车）。para_id 是段落级锚点的落点。"""

    pid: str = ""
    runs: list[RunStyle] = field(default_factory=list)
    text: str = ""
    level: int = 0
    align: str = "left"           # left | center | right | justify
    bullet: str = "none"          # none | char | auto | inherit
    bullet_char: str = ""
    space_before_pt: float | None = None
    space_after_pt: float | None = None
    line_spacing: float | None = None      # 倍数；None = 继承
    indent_px: float = 0.0
    #: 左边距（第 2 行起的左边界）与首行偏移（负数 = 悬挂缩进）。
    #: 项目符号要靠这两个值才落得对位置：PowerPoint 的"• 文字"是
    #: 悬挂缩进排版，不是简单的 padding。
    margin_left_px: float = 0.0
    first_line_px: float = 0.0
    #: 段落级默认字号/颜色（run 没写时用它）—— 见 schema 注释：真实课件里
    #: 大量的字号样式写在段落 defRPr 或版式上，不在 run 上。
    default_size_pt: float | None = None
    default_color: str | None = None
    default_font: str | None = None
    default_bold: bool | None = None


@dataclass
class TextBody:
    """一个形状里的文字整体。"""

    paras: list[Paragraph] = field(default_factory=list)
    anchor: str = "top"           # top | middle | bottom
    wrap: bool = True
    autofit: str = "none"         # none | norm | shape
    font_scale: float = 1.0       # normAutofit 的 fontScale
    insets_px: list[float] = field(default_factory=lambda: [7.2, 3.6, 7.2, 3.6])
    vertical: str = "horz"        # horz | vert | vert270


@dataclass
class TableCell:
    text: str = ""
    #: 单元格的段落级锚点 id（`...-c03`）。
    #: 表格里也会逐格讲（目录页、练习答案页），所以表格同样要能细到格子。
    pid: str = ""
    paras: list[Paragraph] = field(default_factory=list)
    fill: Fill | None = None
    row_span: int = 1
    col_span: int = 1
    merged_into: str = ""         # 被合并时指向左上角单元格


@dataclass
class TableData:
    col_w_px: list[float] = field(default_factory=list)
    row_h_px: list[float] = field(default_factory=list)
    cells: list[list[TableCell]] = field(default_factory=list)
    first_row: bool = False
    banded: bool = False


# ===========================================================================
# 元素
# ===========================================================================

#: 形状类型（kind）—— 前端按此选择渲染器
KIND_TEXT = "text"            # 纯文本框
KIND_PLACEHOLDER = "placeholder"
KIND_RECT = "rect"
KIND_ROUND_RECT = "roundRect"
KIND_ELLIPSE = "ellipse"
KIND_FREEFORM = "freeform"    # custGeom，带 svg_path
KIND_LINE = "line"
KIND_CONNECTOR = "connector"
KIND_PICTURE = "picture"
KIND_TABLE = "table"
KIND_GROUP = "group"
KIND_UNKNOWN = "unknown"

#: 语义角色 —— 给"课堂脚本生成"用：让它知道哪个是标题、哪个是正文、哪个纯装饰
ROLE_TITLE = "title"
ROLE_BODY = "body"
ROLE_NOTE = "note"
ROLE_PICTURE = "picture"
ROLE_TABLE = "table"
ROLE_DECOR = "decor"          # 无文字的装饰形状（底色块/线条/圆形），LLM 不该锚它


@dataclass
class PictureData:
    src: str = ""                 # 相对 deck 的媒体路径，如 media/image12.jpeg
    external_url: str = ""        # 外链图片（原 pptx 用 r:link 引用网络图片）
    natural_w: int = 0
    natural_h: int = 0
    #: srcRect 裁剪（0~1 比例），对应 pptx 的 <a:srcRect l/t/r/b>
    crop: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    alt: str = ""


@dataclass
class Element:
    """页面上一个可锚定、可操作的元素。"""

    element_id: str               # 稳定 id：s04-sh05
    shape_id: int = 0             # pptx 内部 id（动画时序引用它）
    name: str = ""                # 原始形状名（审核时给人看）
    kind: str = KIND_UNKNOWN
    role: str = ROLE_BODY
    #: [x, y, w, h] px，相对页面左上角
    box: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    rot: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    z: int = 0
    fill: Fill | None = None
    line: LineStyle | None = None
    text: TextBody | None = None
    pic: PictureData | None = None
    table: TableData | None = None
    #: freeform / 线条的 SVG path（viewBox 与 box 等尺寸，左上为原点）
    svg_path: str = ""
    #: 形状预设名（rect/roundRect/ellipse/rightArrow…），前端做形状映射
    preset: str = ""
    #: pptx 里被标记 show="0"（作者手动隐藏）
    hidden: bool = False
    decorative: bool = False
    #: 是否在演讲者备注/版式里被当作标题
    is_title: bool = False
    #: 来自哪一层：slide | layout | master
    #: 版式/母版上的装饰形状（封面的大箭头、底部色条、像素小图）也会被解析出来，
    #: 但**只画不锚**：它们不能当锚点（不在 slide 上，讲课时不该指向它们），
    #: 也不该出现在"可选锚点"清单里误导模型。
    source: str = "slide"

    # -- 派生 ---------------------------------------------------------------
    @property
    def paragraphs(self) -> list[Paragraph]:
        return self.text.paras if self.text else []

    @property
    def plain_lines(self) -> list[str]:
        """段落纯文本（丢掉空段）。"""
        return [p.text for p in self.paragraphs if p.text.strip()]

    @property
    def plain_text(self) -> str:
        return "\n".join(self.plain_lines)

    @property
    def para_ids(self) -> list[str]:
        return [p.pid for p in self.paragraphs if p.text.strip()]

    def para(self, pid: str) -> Paragraph | None:
        for p in self.paragraphs:
            if p.pid == pid:
                return p
        return None

    @property
    def cell_ids(self) -> list[str]:
        if not self.table:
            return []
        return [c.pid for row in self.table.cells for c in row if c.pid]

    def cell(self, pid: str) -> TableCell | None:
        if not self.table:
            return None
        for row in self.table.cells:
            for c in row:
                if c.pid == pid:
                    return c
        return None


@dataclass
class Step:
    """分步（出场）单位 —— 视觉进度与听觉同步的最小单位。

    source:
      - ``anim``    ：pptx 自带入场动画，顺序是作者定的，最可信
      - ``planned`` ：课堂脚本生成时按教学逻辑重新规划
      - ``rule``    ：解析器按"段落/形状"顺序给的兜底建议
    """

    step_id: str = ""
    element_ids: list[str] = field(default_factory=list)
    source: str = "rule"
    trigger: str = "after"        # click | after
    delay_ms: int = 0


@dataclass
class SlideDoc:
    index: int = 0
    title: str = ""
    layout_name: str = ""
    elements: list[Element] = field(default_factory=list)
    #: 版式/母版带来的背景装饰（只画不锚）。
    #: 单独放一列而不是混进 `elements`：混进去会让锚点清单里出现
    #: "s04-la-sh01"这种讲课时说不出来的 id，模型会去锚它。
    decorations: list[Element] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    notes: str = ""
    background: Fill | None = None
    #: 页上所有文字（按阅读序），供 LLM 与审计用
    text_flat: str = ""

    # -- 索引 ---------------------------------------------------------------
    def element(self, element_id: str) -> Element | None:
        for e in self.elements:
            if e.element_id == element_id:
                return e
        return None

    def find_anchor(self, anchor_id: str) -> tuple[Element, Paragraph | None] | None:
        """锚点 id 可以是形状（s04-sh05）也可以是段落（s04-sh05-p03）。"""
        if not anchor_id:
            return None
        e = self.element(anchor_id)
        if e is not None:
            return e, None
        # 段落级：从右往左按 "-p<数字>" 拆
        if "-p" in anchor_id:
            head, _, tail = anchor_id.rpartition("-p")
            if tail.isdigit():
                e = self.element(head)
                if e is not None:
                    for p in e.paragraphs:
                        if p.pid == anchor_id:
                            return e, p
                    # 段落 id 对不上时按序号兜底（容忍 -p1 / -p01 / -p10）
                    idx = int(tail) - 1
                    ps = e.paragraphs
                    if 0 <= idx < len(ps):
                        return e, ps[idx]
        # 表格单元格（s03-sh03-c04）：单元格里的段落退化成一行，用第一个段落表示
        if "-c" in anchor_id:
            head, _, tail = anchor_id.rpartition("-c")
            if tail.isdigit():
                e = self.element(head)
                if e is not None and e.cell(anchor_id) is not None:
                    cell = e.cell(anchor_id)
                    return e, (cell.paras[0] if cell and cell.paras else None)
        return None

    @property
    def element_ids(self) -> list[str]:
        return [e.element_id for e in self.elements]

    @property
    def anchor_ids(self) -> list[str]:
        """所有可锚定 id（形状 + 段落 + 表格单元格）。"""
        out: list[str] = []
        for e in self.elements:
            out.append(e.element_id)
            out.extend(e.para_ids)
            out.extend(e.cell_ids)
        return out

    def anchor_index(self) -> dict[str, str]:
        """可锚定 id → 其文本（生成锚点与审核都用它）。"""
        idx: dict[str, str] = {}
        for e in self.elements:
            idx[e.element_id] = e.plain_text.replace("\n", " ⏎ ")[:80]
            for p in e.paragraphs:
                if p.text.strip():
                    idx[p.pid] = p.text[:80]
            if e.table:
                for row in e.table.cells:
                    for c in row:
                        if c.pid and c.text.strip():
                            idx[c.pid] = c.text[:80]
        return idx


@dataclass
class DeckDoc:
    """一整份课件。"""

    deck_id: str = "deck"
    title: str = ""
    source_path: str = ""
    source_sha1: str = ""
    width_px: int = DEFAULT_W
    height_px: int = DEFAULT_H
    slides: list[SlideDoc] = field(default_factory=list)
    #: 媒体文件名 → 相对路径（相对 deck 目录），如 "image12.jpeg" -> "media/image12.jpeg"
    media: dict[str, str] = field(default_factory=dict)
    ingested_at: str = ""
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    # -- 索引 ---------------------------------------------------------------
    def slide_at(self, index: int) -> SlideDoc | None:
        for s in self.slides:
            if s.index == index:
                return s
        return None

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    def anchor_index(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in self.slides:
            out.update(s.anchor_index())
        return out

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(indent=1), encoding="utf-8")
        return p

    @staticmethod
    def from_dict(d: dict) -> "DeckDoc":
        def _fill(x) -> Fill | None:
            if not x:
                return None
            d2 = dict(x)
            # 渐变色标是 dataclass，JSON 里是 dict。**必须还原**：
            # 不还原的话，`load` 回来的 fill.gradient 是 list[dict]，
            # 而刚解析出来的对象是 list[GradientStop] —— 同一个字段两种类型，
            # 谁写 `s.color` 谁在"刚解析时对、读盘后崩"，这是最难查的那类 bug。
            d2["gradient"] = [GradientStop(**g) if isinstance(g, dict) else g
                              for g in (d2.get("gradient") or [])]
            return Fill(**d2)

        def _line(x) -> LineStyle | None:
            return LineStyle(**x) if x else None

        def _para(x: dict) -> Paragraph:
            d2 = dict(x)
            d2["runs"] = [RunStyle(**r) for r in d2.get("runs", [])]
            return Paragraph(**d2)

        def _text(x) -> TextBody | None:
            if not x:
                return None
            d2 = dict(x)
            d2["paras"] = [_para(p) for p in d2.get("paras", [])]
            return TextBody(**d2)

        def _pic(x) -> PictureData | None:
            return PictureData(**x) if x else None

        def _table(x) -> TableData | None:
            if not x:
                return None
            d2 = dict(x)
            rows = []
            for row in d2.get("cells", []):
                rows.append([
                    TableCell(**{**c, "paras": [_para(p) for p in c.get("paras", [])],
                                 "fill": _fill(c.get("fill"))})
                    for c in row
                ])
            d2["cells"] = rows
            return TableData(**d2)

        def _el(x: dict) -> Element:
            d2 = dict(x)
            d2["fill"] = _fill(d2.get("fill"))
            d2["line"] = _line(d2.get("line"))
            d2["text"] = _text(d2.get("text"))
            d2["pic"] = _pic(d2.get("pic"))
            d2["table"] = _table(d2.get("table"))
            return Element(**d2)

        slides = []
        for s in d.get("slides", []):
            s2 = dict(s)
            s2["elements"] = [_el(e) for e in s2.get("elements", [])]
            s2["decorations"] = [_el(e) for e in (s2.get("decorations") or [])]
            s2["steps"] = [Step(**st) for st in s2.get("steps", [])]
            s2["background"] = _fill(s2.get("background"))
            slides.append(SlideDoc(**s2))

        d2 = dict(d)
        d2["slides"] = slides
        return DeckDoc(**d2)

    @staticmethod
    def load(path: str | Path) -> "DeckDoc":
        return DeckDoc.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ===========================================================================
# 可审核性（《课堂交互设计》§2.7）
# ===========================================================================

def deck_overview(deck: DeckDoc, limit: int | None = None) -> str:
    """按页列出结构，给人快速过一遍解析结果（也用于把页面喂给 LLM）。"""
    # deck_id 要出现在第一行：看到概览的人下一步就是拿它去备课/上课，
    # 让用户再去别处翻 id 是很无谓的一步。
    lines: list[str] = [f"《{deck.title}》 共 {deck.slide_count} 页 "
                        f"({deck.width_px}×{deck.height_px}px) · deck_id={deck.deck_id}"]
    for s in deck.slides[:limit] if limit else deck.slides:
        ntext = sum(1 for e in s.elements if e.plain_text.strip())
        npic = sum(1 for e in s.elements if e.kind == KIND_PICTURE)
        lines.append(f"  第 {s.index + 1} 页 · {s.title or '（无标题）'} "
                     f"· 元素 {len(s.elements)}（含文字 {ntext} / 图片 {npic}）"
                     f"· 步骤 {len(s.steps)}")
    return "\n".join(lines)
