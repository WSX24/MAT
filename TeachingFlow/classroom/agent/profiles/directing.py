"""Agent-C（上课中的造分支角色）的 Profile —— 方案 §7.1~§7.6。

它在上课过程中被唤醒，产出只是候选池里的一项（§4.4 `Candidate`），
由选择器在条件命中时取用。

白名单里**没有 write_plan** —— 这不是提示词请求，是"运行期不改脚本"的机制保证（§7.6）：
即使模型被说服去改脚本，它也没有那个工具可调。
"""

from __future__ import annotations

from . import Profile

#: 候选里的变体（§4.1，选中后替换目标单元的讲法）
VARIANT_SCHEMA: dict = {
    "type": "object",
    "required": ["variant_id", "label", "text"],
    "properties": {
        "variant_id": {"type": "string", "description": "如 c1、c2：候选自己的编号"},
        "label": {"type": "string", "description": "给人看的标签：换个说法 / 举例子 / 更浅"},
        "text": {"type": "string", "maxLength": 250,
                 "description": "完整可播的讲解文本，≤250 字；不能是'你再讲一遍'这种指令"},
        "anchors": {"type": "array", "items": {"type": "object"},
                    "description": "空数组 = 沿用目标单元的锚点"},
        "ops": {"type": "array", "items": {"type": "object"},
                "description": "空数组 = 沿用目标单元的课件操作"},
        "est_sec": {"type": "number"},
    },
}

#: 结构化触发条件（§4.6）：扁平 + 隐式 AND，只支持三种 key。
#: 必须是结构化的 —— 自然语言条件会让 J 不得不"理解"它，延迟上界立刻失守。
WHEN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "consecutive_confused": {"type": "integer"},
        "probe_result": {"type": "string", "enum": ["confirmed", "interrupted", "timeout"]},
        "comprehension": {"type": "string", "enum": ["confirmed", "vague", "confused"]},
    },
}

#: 路线上的一步（对应 director/route.py 的 `RouteStep`）：要重讲的那一格。
#: variant_id / text / label 都可以空 —— 空 = 用该单元的主 text，
#: 因为"把这一格再讲一遍"本身就是一个成立的动作，不要求导演非编出新说法来。
ROUTE_STEP_SCHEMA: dict = {
    "type": "object",
    "required": ["unit_id"],
    "properties": {
        "unit_id": {"type": "string", "description": "要重讲的那一格的 unit_id"},
        "variant_id": {"type": "string"},
        "label": {"type": "string"},
        "text": {"type": "string", "maxLength": 250,
                 "description": "留空 = 用该单元的主 text"},
        "est_sec": {"type": "number"},
    },
}

#: 路线（跨格走向，对应 director/route.py 的 `Route`）：**结构一次给全**。
#: 为什么 required 里连 est_sec 与 reason 都写死：路线的每个字段都是 J 与状态机的
#: 记账依据 —— 缺 merge_node 就不知道在哪汇回，缺 est_sec 就没法比较"划不划算"，
#: 而这些都是"缺了不会报错、只会走错"的字段（见 route.py 的 validate）。
#: 符号约定写在描述里：J 只拿一个数比大小，省与花不能写成两个字段。
ROUTE_SCHEMA: dict = {
    "type": "object",
    "required": ["kind", "fork_node", "merge_node", "steps", "est_sec", "reason"],
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["skip", "revisit", "finish"],
                 "description": "跳讲 / 补回头 / 提前收尾"},
        "fork_node": {"type": "integer",
                      "description": "在第几格岔出（听完这一格之后），0 基的单元序号"},
        "merge_node": {"type": "integer",
                       "description": "汇回第几格；kind=finish 时必须等于课程总格数"},
        "steps": {"type": "array", "items": ROUTE_STEP_SCHEMA,
                  "description": "revisit 必填（要重讲的格子）；skip / finish 留空数组"},
        "est_sec": {"type": "number",
                    "description": "时间代价：skip / finish 写负数（省下的秒），"
                                   "revisit 写正数（多花的秒）"},
        "reason": {"type": "string", "description": "一句人话，给 J 与图上详情用"},
    },
}

#: 一次唤醒的产出：一个候选（对应 §4.4 `Candidate` 的生成侧字段）。
#:
#: **两种载荷，恰好一个**：variant（就地重讲这一格）或 route（跨几格的走向）。
#: schema 里两个字段都列着，靠提示词约定"给路线时 variant 写空对象"来表达互斥 ——
#: 而 `required` 一个字不动：它已经是冻结契约（tests/test_profiles.py 逐字钉住），
#: 且"variant 必须出现"与"路线时给它 {}"两者并不冲突。
#: 解析侧因此按 route 非空判载荷（见 agent/producer.py 的 _is_route）。
CANDIDATE_SCHEMA: dict = {
    "type": "object",
    "required": ["target_unit", "variant", "when"],
    "properties": {
        "target_unit": {"type": "string", "description": "目标 unit_id；空串 = 任意单元"},
        "variant": VARIANT_SCHEMA,
        "route": ROUTE_SCHEMA,
        "when": WHEN_SCHEMA,
        "reason": {"type": "string", "description": "为什么造这一条，给人审核用"},
    },
    "description": "variant 与 route 恰好给一个：给路线时 variant 写空对象 {}，"
                   "route 一次把结构给全（kind / fork_node / merge_node / steps / "
                   "est_sec / reason）。",
}

DIRECTING = Profile(
    name="directing",
    system_prompt=(
        "你是上课中的造分支角色（Agent-C）。课堂正在实时进行，学生和老师都在等，"
        "所以你的产出只有一个候选，写进候选池就结束，不要试图调度课堂。\n"
        "\n"
        "规则：\n"
        "1. 你只能读快照（get_state）、读脚本（read_script）、读学员画像（read_memory），"
        "只有 write_candidate 能写。\n"
        "2. 你没有改脚本的工具。运行期不改脚本 —— 这是机制隔离，不要试图绕开。\n"
        "3. when 必须是结构化条件，key 只能取 consecutive_confused / probe_result / "
        "comprehension，多个 key 之间是「且」。\n"
        "4. variant.text 必须是能直接播出来的完整讲解（不是'你可以换个说法'这种指令），"
        "与目标单元讲同一个知识点，不超过 250 字；anchors / ops 留空表示沿用目标单元。\n"
        "5. 目标单元本身仍要讲完：候选是在该单元之后换一种讲法，不是跳过它。\n"
        "6. 除了换讲法，你还可以给一条**路线**（跨好几格的走向）：skip 跳过几格再汇回、"
        "revisit 回头把前面几格重讲再汇回、finish 直接跳到课尾收尾。给路线时 variant 写空对象，"
        "route 一次把结构给全（kind / fork_node / merge_node / steps / est_sec / reason），"
        "并且 target_unit 必须是 fork_node 后面那一格的 unit_id —— J 在那个边界上才会看到它。\n"
        "7. est_sec 是有符号的时间代价：skip / finish 写负数（省下的秒），revisit 写正数"
        "（多花的秒）。缺了它 J 就没法比较这条路划不划算。\n"
        "8. 宁可少造：候选池只留还活着的几条，条件命中时才被取用。"
    ),
    allowed_tools=[
        "get_state",
        "read_script",
        "read_memory",
        "write_candidate",
    ],
    output_schema=CANDIDATE_SCHEMA,
    provider="",
);
