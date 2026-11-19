"""课程数据模型：课件、幻灯片、课堂脚本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from state import Anchor, LectureUnit

if TYPE_CHECKING:  # 只用于类型标注：models 不真的 import director（依赖方向不许反过来）
    from director.route import Route


@dataclass
class SlideElement:
    """课件上的一个可锚定元素。element_id 是语音→高亮的挂载点。

    真实 PPT 接入后多了一类"段落元素"：`kind == "para"`，
    `element_id` 是段落级 id（`s04-sh02-p03`），`parent_id` 指向它所属的形状。
    这样锚点可以细到"例句那一行"，而体检与 LLM 上下文都不用特判两种粒度。
    """

    element_id: str
    kind: str          # title | text | formula | box | image | note | para | <pptx 形状类型>
    text: str = ""
    order: int = 0     # 视觉/展开顺序
    parent_id: str = ""  # 段落的宿主形状（形状元素为空）


@dataclass
class Slide:
    index: int
    title: str
    elements: list[SlideElement] = field(default_factory=list)

    def element(self, element_id: str) -> SlideElement | None:
        for e in self.elements:
            if e.element_id == element_id:
                return e
        return None

    def parent_of(self, element_id: str) -> str:
        """段落级 id → 宿主形状 id（形状本身则返回自己）。"""
        e = self.element(element_id)
        if e is None:
            return ""
        return e.parent_id or e.element_id


@dataclass
class LessonScript:
    """课堂脚本。阶段一由教师 Agent 生成，本原型用手写脚本替代（见 script.py 顶部说明）。"""

    course_id: str
    title: str
    session_goals: list[str] = field(default_factory=list)
    slides: list[Slide] = field(default_factory=list)
    units: list[LectureUnit] = field(default_factory=list)
    #: 备课期预置的路线分支（Route，见 agent/routes.py 与 director/route.py）。
    #: 为什么挂在脚本上：状态机与观测面拿到的都是 LessonScript（web.py 起 Machine 时只传它），
    #: 拿不到 plan.json —— 这一处是唯一一条通向运行期的通道。
    #: 空列表 = 这份脚本没有预置分支（手写课件 / 旧 plan.json / 规则兜底），永远不是 None。
    routes: list["Route"] = field(default_factory=list)

    # -- 索引 -------------------------------------------------------------
    def units_of(self, slide_index: int) -> list[LectureUnit]:
        return [u for u in self.units if u.slide == slide_index]

    def order_index(self, unit_id: str) -> int:
        for i, u in enumerate(self.units):
            if u.unit_id == unit_id:
                return i
        return -1

    def unit(self, unit_id: str) -> LectureUnit | None:
        for u in self.units:
            if u.unit_id == unit_id:
                return u
        return None

    def previous_of(self, unit_id: str) -> LectureUnit | None:
        i = self.order_index(unit_id)
        if i <= 0:
            return None
        return self.units[i - 1]

    def next_after(self, unit_id: str) -> LectureUnit | None:
        i = self.order_index(unit_id)
        if i < 0 or i + 1 >= len(self.units):
            return None
        return self.units[i + 1]

    def first_unit_of(self, slide_index: int) -> LectureUnit | None:
        us = self.units_of(slide_index)
        return us[0] if us else None

    def next_after_slide(self, slide_index: int) -> LectureUnit | None:
        """下一页的第一个单元（探针等待结束后用它推进）。"""
        return self.first_unit_of(slide_index + 1)

    def unit_of(self, slide_index: int, seg_index: int) -> LectureUnit | None:
        us = self.units_of(slide_index)
        if 0 <= seg_index < len(us):
            return us[seg_index]
        return None

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @property
    def total_est_sec(self) -> float:
        return round(sum(u.est_sec for u in self.units), 1)

    # -- 供 LLM 使用的上下文 ----------------------------------------------
    def outline(self) -> str:
        lines = [f"《{self.title}》目标：" + "；".join(self.session_goals)]
        for s in self.slides:
            lines.append(f"第{s.index + 1}页 {s.title}")
        return "\n".join(lines)

    def must_teach_kp(self, slide_index: int) -> list[str]:
        out: list[str] = []
        for u in self.units_of(slide_index):
            if u.must_teach:
                out.extend(u.kp)
        return out

    def anchors_hint(self, slide_index: int) -> str:
        s = self.slide_at(slide_index)
        if not s:
            return ""
        return "；".join(f"{e.element_id}={e.text[:18]}" for e in s.elements)

    def slide_at(self, slide_index: int) -> Slide | None:
        for s in self.slides:
            if s.index == slide_index:
                return s
        return None
