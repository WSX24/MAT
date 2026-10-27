"""Agent-P（备课角色）的 Profile —— 方案 §5.1~§5.4。

它的一次运行把"课件 + 学员画像"变成"带变体的 LessonPlan"，落盘 plan.json。
白名单只有 5 个工具，其中 `write_plan` **只在这里出现**：脚本只能课前写，
这是"运行期不改脚本"的一半机制（另一半是 C 的白名单里没有它，见 §7.6）。
"""

from __future__ import annotations

from . import Profile

#: 单元变体的 JSON Schema：与 `UnitVariant`（§4.1）逐字段对应，
#: 并把 §5.4 的三条硬约束写进 schema —— 模型能在生成时就看见尺子，而不是等体检被打回。
VARIANT_SCHEMA: dict = {
    "type": "object",
    "required": ["variant_id", "label", "text"],
    "properties": {
        "variant_id": {"type": "string", "description": "v1 = 换说法，v2 = 举例子"},
        "label": {"type": "string", "description": "给人看的标签：换说法 / 举例子"},
        "text": {"type": "string", "maxLength": 250,
                 "description": "这个变体的完整讲解文本，≤250 字；必须是能直接念的话"},
        "anchors": {
            "type": "array",
            "description": "空数组 = 沿用主单元的锚点",
            "items": {
                "type": "object",
                "required": ["element_id"],
                "properties": {
                    "element_id": {"type": "string", "description": "必须存在于本页"},
                    "text_hint": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "ops": {
            "type": "array",
            "description": "空数组 = 沿用主单元的课件操作；at_sentence 不得超过本变体句数",
            "items": {"type": "object"},
        },
        "est_sec": {"type": "number", "description": "派生字段：len(text)/5.0"},
    },
}

#: 一个讲解单元（对应 `script_gen.PlanUnit`，§4.2）
UNIT_SCHEMA: dict = {
    "type": "object",
    "required": ["slide", "text"],
    "properties": {
        "slide": {"type": "integer", "description": "0 起算的页号"},
        "text": {"type": "string", "maxLength": 250,
                 "description": "v0：老师会说的话，建议 80~180 字，硬上限 250 字"},
        "anchors": {
            "type": "array",
            "description": "只能从 read_deck 给的锚点清单里挑，优先锚到段落 id",
            "items": {
                "type": "object",
                "required": ["element_id"],
                "properties": {
                    "element_id": {"type": "string"},
                    "text_hint": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "ops": {"type": "array", "items": {"type": "object"}},
        "kp": {"type": "array", "items": {"type": "string"}},
        "must_teach": {"type": "boolean"},
        "is_page_end": {"type": "boolean"},
        "note": {"type": "string"},
        "summary": {"type": "string", "maxLength": 30,
                    "description": "走向图卡面上的一句话：这一格在讲什么（≤30 字，不是念给学生的话）"},
        "variants": {"type": "array", "items": VARIANT_SCHEMA,
                     "description": "v1 / v2；与 v0 同一把尺子（§5.4）"},
    },
}

#: 整份计划（对应 `script_gen.LessonPlan`）
PLAN_SCHEMA: dict = {
    "type": "object",
    "required": ["units"],
    "properties": {
        "session_goals": {"type": "array", "items": {"type": "string"}},
        "units": {"type": "array", "items": UNIT_SCHEMA},
    },
}

PLANNING = Profile(
    name="planning",
    system_prompt=(
        "你是备课角色（Agent-P）。输入是课件结构与学员画像，输出一份能直接上课的讲解计划。\n"
        "\n"
        "规则：\n"
        "1. 每个单元的 text 就是老师会说的话：口语、有过渡、80~180 字，硬上限 250 字。\n"
        "2. anchors 只能从课件给出的锚点清单里挑，优先锚到段落（形如 s04-sh02-p03），"
        "不要锚装饰元素；锚点指向哪里，讲到哪里就高亮哪里。\n"
        "3. ops 的 at_sentence 从 1 起算，且不得超过该单元实际句数（否则这个操作永远不会发生）。\n"
        "4. 每个单元另给 v1（换说法）、v2（举例子）。变体与 v0 过同一把尺子："
        "不超过 250 字、锚点在本页存在、ops 的 at_sentence 不超过本变体句数。\n"
        "5. 变体与 v0 讲同一个知识点，只是讲法不同；anchors / ops 留空表示沿用主单元。\n"
        "6. 讲法宁朴实勿花哨：学生要听懂，而且任何一个单元都不能长到让他干等两分钟。\n"
        "\n"
        "交给你的工具里只有 write_plan 能落盘；写完先自己 audit_plan 一遍再交。"
    ),
    allowed_tools=[
        "read_deck",
        "read_memory",
        "write_plan",
        "audit_plan",
        "measure_plan",
    ],
    output_schema=PLAN_SCHEMA,
    provider="",
);
