"""测试夹具：合成课件与真实 pptx。

两个夹具的用途不同，都要保留：
- `make_deck()`：**手搭的 DeckDoc**。逐字段可控，用来测"脚本生成 / 操作 / 渲染载荷"
  这些与 pptx 解析无关的逻辑 —— 不依赖 python-pptx，也不依赖那份 16MB 的真实课件。
- `make_pptx()`：**真的生成一个 pptx**（文本/图形/图片/表格/多段落），
  用它测解析器。真实课件（42 页人教版英语）作为端到端口径，测试里不用它，
  免得"跑个单元测试要读 16MB"。
"""

from __future__ import annotations

from pathlib import Path

from deck.ops import DeckOp
from deck.schema import (
    Element,
    Fill,
    KIND_ELLIPSE,
    KIND_FREEFORM,
    KIND_PICTURE,
    KIND_RECT,
    KIND_TABLE,
    KIND_TEXT,
    LineStyle,
    Paragraph,
    PictureData,
    ROLE_BODY,
    ROLE_DECOR,
    ROLE_PICTURE,
    ROLE_TABLE,
    ROLE_TITLE,
    RunStyle,
    SlideDoc,
    TableCell,
    TableData,
    TextBody,
    DeckDoc,
)
from models import LessonScript, Slide, SlideElement
from state import Anchor, LectureUnit

#: 单词页的真实形态：一个文本框里 8 段落（音标/词性/释义/用法/搭配/例句×3）
PACK_PARAS = [
    "音标：/pæk/   词性：v.",
    "释义：打包；收拾",
    "用法：可单独使用或与 up 搭配，强调整理物品准备出行或存放。",
    "搭配：pack sth.（打包某物）；",
    "     pack up（打包；收拾行李）pack for（为…… 打包）",
    "例句：I need to pack my clothes for the trip.",
    "（我需要为旅行打包衣服。）",
    "    It's time to pack up and leave.",
]


def _text_body(paras: list[str], pid_prefix: str, *, size_pt: float = 14.0,
               anchor: str = "top") -> TextBody:
    out: list[Paragraph] = []
    for i, t in enumerate(paras, 1):
        out.append(Paragraph(
            pid=f"{pid_prefix}-p{i:02d}",
            text=t,
            runs=[RunStyle(text=t, size_pt=size_pt if i > 1 else 24.0)],
            default_size_pt=size_pt,
        ))
    return TextBody(paras=out, anchor=anchor,
                    insets_px=[9.6, 4.8, 9.6, 4.8])


def make_deck() -> DeckDoc:
    """一份 3 页的合成课件：封面 / 单词讲解页（多段落文本框 + 配图）/ 目录页（含表格）。"""
    slides: list[SlideDoc] = []

    # ---- 第 1 页：封面 ----
    s0 = SlideDoc(index=0, title="Unit 2 Sweet Home 重点词汇短语解析", layout_name="标题幻灯片")
    s0.elements.append(Element(
        element_id="s01-sh01", shape_id=2, name="标题", kind=KIND_TEXT, role=ROLE_TITLE,
        box=[104, 349, 1007, 216], z=0, is_title=True,
        text=_text_body(["Unit 2 Sweet Home 重点词汇短语解析"], "s01-sh01", size_pt=36.0,
                        anchor="middle"),
        fill=Fill(kind="none"),
    ))
    s0.elements.append(Element(
        element_id="s01-sh02", shape_id=3, name="署名", kind=KIND_TEXT, role=ROLE_BODY,
        box=[114, 574, 380, 76], z=1,
        text=_text_body(["人教版八上（2024）"], "s01-sh02", size_pt=18.0),
        fill=Fill(kind="none"),
    ))
    s0.text_flat = "Unit 2 Sweet Home 重点词汇短语解析\n人教版八上（2024）"
    slides.append(s0)

    # ---- 第 2 页：单词讲解页（真实课件的核心形态）----
    s1 = SlideDoc(index=1, title="1.pack 用法讲解", layout_name="标题和内容")
    s1.elements.append(Element(
        element_id="s02-sh01", shape_id=5, name="标题 5", kind=KIND_TEXT, role=ROLE_TITLE,
        box=[73, 38, 1134, 76], z=0, is_title=True,
        text=_text_body(["1.pack 用法讲解"], "s02-sh01", size_pt=28.0),
        fill=Fill(kind="none"),
    ))
    s1.elements.append(Element(
        element_id="s02-sh02", shape_id=6, name="内容占位符 2", kind=KIND_RECT, role=ROLE_BODY,
        box=[73, 137, 786, 512], z=1,
        text=_text_body(PACK_PARAS, "s02-sh02"),
        fill=Fill(kind="none"),
    ))
    s1.elements.append(Element(
        element_id="s02-sh03", shape_id=7, name="配图", kind=KIND_PICTURE, role=ROLE_PICTURE,
        box=[845, 71, 413, 579], z=2,
        pic=PictureData(src="media/image01.jpeg", natural_w=800, natural_h=1120,
                        alt="打包行李的图片"),
    ))
    s1.elements.append(Element(
        element_id="s02-sh04", shape_id=8, name="装饰条", kind=KIND_RECT, role=ROLE_DECOR,
        box=[0, 0, 1280, 12], z=3, fill=Fill(kind="solid", color="#4874CB"), decorative=True,
    ))
    s1.text_flat = "1.pack 用法讲解\n" + "\n".join(PACK_PARAS)
    s1.steps = []
    slides.append(s1)

    # ---- 第 3 页：目录页（含表格与自由多边形）----
    s2 = SlideDoc(index=2, title="目录", layout_name="目录")
    s2.elements.append(Element(
        element_id="s03-sh01", shape_id=9, name="标题", kind=KIND_TEXT, role=ROLE_TITLE,
        box=[119, 90, 292, 97], z=0, is_title=True,
        text=_text_body(["目录"], "s03-sh01", size_pt=28.0),
        fill=Fill(kind="none"),
    ))
    s2.elements.append(Element(
        element_id="s03-sh02", shape_id=10, name="形状 17", kind=KIND_FREEFORM, role=ROLE_DECOR,
        box=[307, 105, 442, 63], z=1, preset="",
        svg_path="M 0 0 L 442 0 L 442 63 L 0 63 Z",
        fill=Fill(kind="solid", color="#EE822F"), decorative=True,
    ))
    s2.elements.append(Element(
        element_id="s03-sh03", shape_id=11, name="目录表", kind=KIND_TABLE, role=ROLE_TABLE,
        box=[60, 300, 1160, 300], z=2,
        table=TableData(
            col_w_px=[580, 580], row_h_px=[100, 100, 100],
            cells=[[TableCell(text="黑体单词用法详解", pid="s03-sh03-c01"),
                    TableCell(text="词组搭配剖析", pid="s03-sh03-c02")],
                   [TableCell(text="词汇练习题", pid="s03-sh03-c03"),
                    TableCell(text="答案解析与总结", pid="s03-sh03-c04")],
                   [TableCell(text="", pid="s03-sh03-c05"),
                    TableCell(text="", pid="s03-sh03-c06")]],
            first_row=True,
        ),
        fill=Fill(kind="none"),
    ))
    s2.elements.append(Element(
        element_id="s03-sh04", shape_id=12, name="圆点", kind=KIND_ELLIPSE, role=ROLE_DECOR,
        box=[1180, 640, 60, 60], z=3, fill=Fill(kind="solid", color="#F2BA02"),
        line=LineStyle(kind="solid", color="#333333", width_px=1.5), decorative=True,
    ))
    s2.text_flat = "目录\n黑体单词用法详解\n词组搭配剖析\n词汇练习题\n答案解析与总结"
    slides.append(s2)

    return DeckDoc(
        deck_id="fixture_deck", title="Unit 2 Sweet Home 重点词汇短语解析",
        source_path="fixture.pptx", source_sha1="0" * 40,
        width_px=1280, height_px=720, slides=slides,
        media={"image01.jpeg": "media/image01.jpeg"},
    )


def make_script_with_ops() -> LessonScript:
    """两页、每页 2 个单元的脚本，带课件操作 —— 用来测"操作在句边界下发"。"""
    slides = [
        Slide(index=0, title="第一页", elements=[
            SlideElement("s01-sh01", "title", "标题一"),
            SlideElement("s01-sh01-p01", "para", "第一段", parent_id="s01-sh01"),
            SlideElement("s01-sh02", "text", "第二块文字"),
        ]),
        Slide(index=1, title="第二页", elements=[
            SlideElement("s02-sh01", "title", "标题二"),
        ]),
    ]
    # S01-U01：2 个播放分块（"这是第一句话。"不足 8 字，会被并进下一句）
    u1 = LectureUnit(
        unit_id="S01-U01", slide=0, index_in_slide=0,
        text="这是第一句话。这是第二句话，要放出一段内容。这是第三句话。",
        anchors=[Anchor("s01-sh01", "标题一")],
        ops=[DeckOp(op="hide", at_sentence=1, target="s01-sh01-p01"),
             DeckOp(op="step", at_sentence=1, target="s01-sh01-p01"),
             DeckOp(op="point", at_sentence=1, target="s01-sh01-p01"),
             DeckOp(op="highlight", at_sentence=2, target="s01-sh02")],
        is_page_end=False, kp=["K1"],
    )
    # S01-U02：页尾单元，无 ops
    u2 = LectureUnit(
        unit_id="S01-U02", slide=0, index_in_slide=1,
        text="页尾这一段只有一句话。", anchors=[Anchor("s01-sh02", "第二块文字")],
        is_page_end=True, kp=["K2"],
    )
    u3 = LectureUnit(
        unit_id="S02-U01", slide=1, index_in_slide=0,
        text="第二页的第一句。第二页的第二句。", anchors=[Anchor("s02-sh01", "标题二")],
        is_page_end=True, kp=["K3"],
        # 故意写一个超出句数的 op：它永远不该被执行（审核会报出来）
        ops=[DeckOp(op="reveal", at_sentence=9, target="s02-sh01")],
    )
    return LessonScript(
        course_id="fixture", title="操作测试课",
        session_goals=["把操作下发到正确的句子边界"],
        slides=slides, units=[u1, u2, u3],
    )


def make_pptx(path: str | Path) -> Path:
    """真的生成一个 pptx：多段落文本框（run 级字号）+ 实心矩形 + 图片 + 表格。

    刻意构造出"真实课件"的形态：字号写在 run 上、形状用形状名、
    段落比形状更细 —— 解析器必须把这三件事都拿出来。
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu, Pt

    prs = Presentation()
    prs.slide_width = Emu(12192000)      # 16:9 → 1280×720 px
    prs.slide_height = Emu(6858000)

    # --- 第 1 页：标题 + 正文文本框 ---
    s1 = prs.slides.add_slide(prs.slide_layouts[6])      # Blank
    title = s1.shapes.add_textbox(Emu(952500), Emu(476250), Emu(7620000), Emu(762000))
    tf = title.text_frame
    tf.text = "1.pack 用法讲解"
    tf.paragraphs[0].runs[0].font.size = Pt(28)

    body = s1.shapes.add_textbox(Emu(952500), Emu(1524000), Emu(7620000), Emu(3810000))
    tb = body.text_frame
    for i, line in enumerate([
        "音标：/pæk/   词性：v.",
        "释义：打包；收拾",
        "例句：I need to pack my clothes for the trip.",
    ]):
        p = tb.paragraphs[0] if i == 0 else tb.add_paragraph()
        r = p.add_run()
        r.text = line
        r.font.size = Pt(14)

    rect = s1.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(0), Emu(0),
                               Emu(12192000), Emu(127000))   # 顶端装饰条
    rect.fill.solid()
    rect.fill.fore_color.rgb = RGBColor(0x48, 0x74, 0xCB)
    rect.line.fill.background()

    # 图片：用 Pillow 现场造一张，避免依赖外部素材
    from PIL import Image

    tmp_png = Path(path).parent / "_fixture_pic.png"
    Image.new("RGB", (400, 300), (200, 210, 230)).save(tmp_png)
    s1.shapes.add_picture(str(tmp_png), Emu(8382000), Emu(1143000), Emu(3000000), Emu(2250000))

    # --- 第 2 页：表格 ---
    s2 = prs.slides.add_slide(prs.slide_layouts[6])
    rows, cols = 3, 2
    table = s2.shapes.add_table(rows, cols, Emu(571500), Emu(2000000),
                                Emu(11060000), Emu(2200000)).table
    data = [["黑体单词用法详解", "词组搭配剖析"],
            ["词汇练习题", "答案解析与总结"],
            ["", ""]]
    for r in range(rows):
        for c in range(cols):
            table.cell(r, c).text = data[r][c]

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out


# ===========================================================================
# 入库真实课件的**固定选择**（见 REAL_DECK_PREFIXES 的注释）
# ===========================================================================

#: 入库的两份真实课件（decks/ 下 deck.json + plan.json 齐全）。**按固定前缀选**，
#: 绝不"取 list_decks() 的第一份"：decks/ 下随时会多出 import-xxxxxxx（导入课件、手工
#: 试跑都会留下），而 list_decks() 按目录名排序 —— 一次导入就能插到前面，把真实课件类
#: 用例一起打红，且红的是"42 页对不上 38 页"这种谁也看不懂的断言。
#: 顺序即优先级；第一份正是这些用例的断言口径（42 页 / 177 格 / 14 条 routes）。
REAL_DECK_PREFIXES = ("Unit_2_Sweet_Home", "高一物理第一课_运动的描述")


def real_deck_id(*, require_plan: bool = True) -> str:
    """按 REAL_DECK_PREFIXES 的顺序挑一份入库真实课件；一份都没有返回 ""（调用方 skip）。"""
    from deck.store import list_decks

    rows = sorted(list_decks(), key=lambda r: str(r.get("deck_id") or ""))
    for prefix in REAL_DECK_PREFIXES:
        for row in rows:
            deck_id = str(row.get("deck_id") or "")
            if not deck_id or not deck_id.startswith(prefix):
                continue
            if require_plan and not row.get("has_plan"):
                continue
            return deck_id
    return ""
