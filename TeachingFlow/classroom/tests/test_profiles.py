"""角色 Profile 测试 —— 白名单是权限边界，不是优化项。

两个断言值得单独说明：
1. P 的白名单正好是方案 §5.2 的 5 个，C 的正好是 §7.6 的 4 个 —— 多一个少一个都算回归。
2. `write_plan` 只在 P 那里出现。"运行期不改脚本"靠的就是这条：C 即使被提示词
   说服了要改脚本，也没有那个工具可调（§5.2 / §7.6）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.profiles import DIRECTING, PLANNING, Profile  # noqa: E402

#: 方案 §5.2（Agent-P）
PLANNING_TOOLS = ["read_deck", "read_memory", "write_plan", "audit_plan", "measure_plan"]
#: 方案 §7.6（Agent-C）
DIRECTING_TOOLS = ["get_state", "read_script", "read_memory", "write_candidate"]


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def test_planning_allowed_tools_match_design() -> None:
    """PLANNING.allowed_tools 等于方案 §5.2 的 5 个（AC6）。"""
    assert PLANNING.allowed_tools == PLANNING_TOOLS


def test_directing_allowed_tools_match_design() -> None:
    """DIRECTING.allowed_tools 等于方案 §7.6 的 4 个（AC6）。"""
    assert DIRECTING.allowed_tools == DIRECTING_TOOLS


def test_write_plan_only_in_planning() -> None:
    """write_plan 只在备课角色手里 —— 运行期不改脚本的机制保证（AC6）。"""
    assert "write_plan" in PLANNING.allowed_tools
    assert "write_plan" not in DIRECTING.allowed_tools


def test_profile_defaults_match_design() -> None:
    """§4.8 的默认值：provider 空 = 用主 provider；max_rounds = 8。"""
    p = Profile(name="x", system_prompt="y", allowed_tools=[], output_schema={})
    assert p.provider == ""
    assert p.max_rounds == 8
    assert PLANNING.provider == "" and DIRECTING.provider == ""
    assert PLANNING.max_rounds == 8 and DIRECTING.max_rounds == 8


def test_profiles_are_declared_with_prompts_and_schemas() -> None:
    for p in (PLANNING, DIRECTING):
        assert isinstance(p, Profile)
        assert p.name in ("planning", "directing")
        assert _has_cjk(p.system_prompt), "system_prompt 必须是中文说明"
        assert p.output_schema.get("type") == "object"
        assert p.output_schema.get("properties")
        assert all(isinstance(t, str) and t for t in p.allowed_tools)


def test_planning_schema_describes_units_with_variants() -> None:
    """P 的产出形状必须包含 variants，且把 §5.4 的 250 字上限写进 schema。"""
    units = PLANNING.output_schema["properties"]["units"]
    assert units["type"] == "array"
    props = units["items"]["properties"]
    assert set(props) >= {"slide", "text", "anchors", "ops", "kp", "variants"}
    assert props["text"]["maxLength"] == 250
    assert props["variants"]["items"]["properties"]["text"]["maxLength"] == 250


def test_directing_schema_is_a_candidate() -> None:
    """C 的产出是候选池里的一项（§4.4），when 只支持三种结构化 key（§4.6）。"""
    schema = DIRECTING.output_schema
    assert set(schema["required"]) == {"target_unit", "variant", "when"}
    assert set(schema["properties"]["when"]["properties"]) == {
        "consecutive_confused", "probe_result", "comprehension"}
    assert schema["properties"]["variant"]["properties"]["text"]["maxLength"] == 250


def test_profiles_package_reexports_submodules() -> None:
    """`agent.profiles` 与子模块指的是同一个对象（不会出现两份配置各改各的）。"""
    from agent.profiles.directing import DIRECTING as from_sub
    from agent.profiles.planning import PLANNING as from_sub_p
    assert from_sub is DIRECTING
    assert from_sub_p is PLANNING
