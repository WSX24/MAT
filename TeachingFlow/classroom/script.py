"""演示课件 + 手写课堂脚本。

关于「课堂脚本」的说明
----------------------
《课堂交互设计》§1 的完整形态是：PPT + 课程大纲 → 教师 Agent 生成课堂脚本（讲解单元）。
本原型**先用手写脚本**，原因：

1. 原型要验证的是 **状态机 + 让位/归位 + 翻页判定 + 节奏指标（V1/V2）**，
   这些与讲解内容从哪来无关；
2. 手写脚本方差为零 —— 实测「让位延迟」时不会被 LLM 的随机长度污染；
3. 离线可跑，不依赖任何服务商。

脚本生成属于阶段一（另一个 Agent 的产物），不在本原型范围内。
讲解单元的字数已按《课堂交互设计》§2.3 控制在 80~180 字（约 15~30 秒语音）。
"""

from __future__ import annotations

from models import LessonScript, Slide, SlideElement
from state import Anchor, LectureUnit


# ===========================================================================
# 课件
# ===========================================================================

SLIDES: list[Slide] = [
    Slide(
        index=0,
        title="判别式是什么",
        elements=[
            SlideElement("title", "title", "一元二次方程的判别式"),
            SlideElement("data", "text", "一元二次方程 ax² + bx + c = 0 （a ≠ 0）"),
            SlideElement("formula", "formula", "Δ = b² − 4ac"),
            SlideElement("why", "text", "它决定方程有没有实数根"),
            SlideElement("note", "note", "a 是二次项系数，且不能为零"),
        ],
    ),
    Slide(
        index=1,
        title="三种情况",
        elements=[
            SlideElement("title", "title", "判别式的三种情况"),
            SlideElement("box", "box", "Δ > 0 两个不相等的实数根；Δ = 0 两个相等的实数根；Δ < 0 没有实数根"),
            SlideElement("case1", "text", "Δ > 0：抛物线与 x 轴有两个交点"),
            SlideElement("case2", "text", "Δ = 0：抛物线与 x 轴相切，只有一个交点"),
            SlideElement("case3", "text", "Δ < 0：抛物线与 x 轴没有交点"),
            SlideElement("img", "image", "三条抛物线示意图"),
        ],
    ),
    Slide(
        index=2,
        title="用判别式解方程",
        elements=[
            SlideElement("title", "title", "用判别式解方程"),
            SlideElement("lead", "text", "先算 Δ，再决定要不要往下算"),
            SlideElement("ex1", "text", "x² − 5x + 6 = 0，Δ = 25 − 24 = 1 > 0，所以有两个根"),
            SlideElement("ex2", "text", "x² − 2x + 1 = 0，Δ = 4 − 4 = 0，所以有两个相等的根"),
            SlideElement("take", "box", "判别式告诉我们方程有几个根，比直接解更快"),
        ],
    ),
]


# ===========================================================================
# 讲解单元
# ===========================================================================

_UNITS: list[LectureUnit] = [
    # ---------------- 第 1 页 ----------------
    LectureUnit(
        unit_id="S1-U01",
        slide=0,
        index_in_slide=0,
        text="上一节我们已经学过，一元二次方程的一般形式是 ax² + bx + c = 0，其中 a 不等于零。"
             "这个式子看上去很简单，但它有没有实数根、有几个根，其实不用解出来也能提前知道。",
        anchors=[Anchor("data", "一元二次方程 ax² + bx + c = 0")],
        kp=["K_QUAD_01"],
    ),
    LectureUnit(
        unit_id="S1-U02",
        slide=0,
        index_in_slide=1,
        text="先提醒一个容易出错的地方：这里的 a 是二次项系数，它不能等于零。"
             "只要 a 等于零，方程就退化成一次方程了，我们后面所有关于判别式的结论都不成立。",
        anchors=[Anchor("note", "a 是二次项系数，且不能为零")],
        kp=["K_QUAD_01"],
    ),
    LectureUnit(
        unit_id="S1-U03",
        slide=0,
        index_in_slide=2,
        text="判别这样一个方程有没有根，靠的就是判别式。"
             "它的写法是 Δ 等于 b 平方减去 4ac，也就是把一次项系数平方，再减去四倍的二次项系数乘常数项。"
             "很多同学会把它和求根公式记混，其实判别式只是求根公式里根号里面的那一部分。",
        anchors=[Anchor("formula", "Δ = b² − 4ac")],
        reveal=["formula"],
        kp=["K_QUAD_02"],
    ),
    LectureUnit(
        unit_id="S1-U04",
        slide=0,
        index_in_slide=3,
        text="所以判别式的作用就一句话：它决定方程有没有实数根，有几个实数根。"
             "也就是说，我们只要算出 Δ 这一个数，就能回答「这个方程能不能解」这个问题，暂时不用真的去解它。",
        anchors=[Anchor("why", "它决定方程有没有实数根")],
        reveal=["why"],
        kp=["K_QUAD_03"],
        is_page_end=True,
    ),

    # ---------------- 第 2 页 ----------------
    LectureUnit(
        unit_id="S2-U01",
        slide=1,
        index_in_slide=0,
        text="那具体怎么判断呢？判别式的符号和实数根的个数之间，有三种对应关系。"
             "这一段是整个知识点的核心，我们一起看一遍。",
        anchors=[Anchor("title", "判别式的三种情况")],
        kp=["K_QUAD_03"],
    ),
    LectureUnit(
        unit_id="S2-U02",
        slide=1,
        index_in_slide=1,
        text="当 Δ 大于零的时候，方程有两个不相等的实数根。"
             "从图像上看，就是抛物线 y 等于 ax² + bx + c 和 x 轴有两个交点，所以我们能取到两个不同的 x。",
        anchors=[Anchor("case1", "Δ > 0"), Anchor("img", "三条抛物线示意图")],
        reveal=["case1"],
        pause={"after_sentence": 1, "sec": 1.5},
        kp=["K_QUAD_04"],
    ),
    LectureUnit(
        unit_id="S2-U03",
        slide=1,
        index_in_slide=2,
        text="当 Δ 等于零的时候，方程有两个相等的实数根。"
             "注意，是两个「相等的」根，不是没有根。这一点丢分最多，因为很多同学一看到判别式等于零，就以为方程没解。",
        anchors=[Anchor("case2", "Δ = 0")],
        reveal=["case2"],
        pause={"after_sentence": 2, "sec": 1.5},
        kp=["K_QUAD_05"],
    ),
    LectureUnit(
        unit_id="S2-U04",
        slide=1,
        index_in_slide=3,
        text="最后，当 Δ 小于零的时候，方程没有实数根。"
             "因为抛物线整个都在 x 轴上方，或者整个都在 x 轴下方，它根本不和 x 轴相交，所以我们找不到让方程成立的 x。",
        anchors=[Anchor("case3", "Δ < 0"), Anchor("img", "三条抛物线示意图")],
        reveal=["case3"],
        pause={"after_sentence": 1, "sec": 2.0},
        kp=["K_QUAD_06"],
    ),
    LectureUnit(
        unit_id="S2-U05",
        slide=1,
        index_in_slide=4,
        text="把三种情况放在一起看，你会发现判别式其实是在回答一个问题：抛物线和 x 轴到底有几个交点。"
             "这两个问题完全是同一件事，只是换了种说法。所以以后看到「有没有根」，你就可以直接想「有没有交点」。",
        anchors=[Anchor("box", "三种情况汇总")],
        reveal=["box"],
        kp=["K_QUAD_04", "K_QUAD_05", "K_QUAD_06"],
        is_page_end=True,
    ),

    # ---------------- 第 3 页 ----------------
    LectureUnit(
        unit_id="S3-U01",
        slide=2,
        index_in_slide=0,
        text="我们一起来看两个例子。第一个是 x² 减 5x 加 6 等于零，这里 a 等于 1，b 等于负 5，c 等于 6，"
             "所以 Δ 等于 25 减 24，也就是 1，大于零，因此方程有两个不相等的实数根。",
        anchors=[Anchor("ex1", "x² − 5x + 6 = 0")],
        kp=["K_QUAD_04"],
    ),
    LectureUnit(
        unit_id="S3-U02",
        slide=2,
        index_in_slide=1,
        text="第二个是 x² 减 2x 加 1 等于零，算一下 Δ 等于 4 减 4，等于零，"
             "所以它有两个相等的实数根。你可以顺手验证一下，左边正好是 x 减 1 的平方，根就是 1 和 1。",
        anchors=[Anchor("ex2", "x² − 2x + 1 = 0")],
        kp=["K_QUAD_05"],
    ),
    LectureUnit(
        unit_id="S3-U03",
        slide=2,
        index_in_slide=2,
        text="所以你会发现，判别式最大的价值在这里：它让我们在做题一开始就知道方程有几个根，"
             "不用先埋头解一遍。如果算出 Δ 小于零，那就直接可以下结论说无实数根，后面的求根公式根本不用写。",
        anchors=[Anchor("take", "判别式告诉我们方程有几个根")],
        reveal=["take"],
        pause={"after_sentence": 1, "sec": 2.0},
        kp=["K_QUAD_03"],
        is_page_end=True,
    ),
]

# 自动补全锚点的文本提示（手写脚本里的简写不再重复维护）
for _u in _UNITS:
    _slide = next((s for s in SLIDES if s.index == _u.slide), None)
    for _a in _u.anchors:
        if not _a.text_hint and _slide:
            _el = _slide.element(_a.element_id)
            if _el:
                _a.text_hint = _el.text[:24]


def build_script() -> LessonScript:
    return LessonScript(
        course_id="demo_math_g8",
        title="一元二次方程的判别式",
        session_goals=[
            "知道判别式 Δ = b² − 4ac 的形式，并能正确代入计算",
            "能由 Δ 的符号判断方程实数根的个数（三种情况）",
            "理解「根的个数」与「抛物线与 x 轴交点个数」是同一件事",
        ],
        slides=SLIDES,
        units=list(_UNITS),
        # 手写这 3 页没有跨格的岔路：预置路线只由备课流水线（agent/routes.py）产出，
        # 状态机与观测面照旧从 script.routes 读（这里就是"没有 = 空列表"的那一档）。
        routes=[],
    )
