"""角色 Profile（方案 §4.8）—— 一个角色的全部配置。

`allowed_tools` 不是"优化项"，是**权限边界**：P 能写脚本、C 不能，
靠的是白名单里有没有 `write_plan`，而不是提示词里写"请不要改脚本"。
提示词是请求，白名单是机制 —— 这个区别决定了运行期改动会不会发生。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Profile:
    """一个 Agent 角色的声明：它是谁、能调什么、产出什么形状。"""

    name: str                   # "planning" | "directing"
    system_prompt: str
    allowed_tools: list[str]    # ← 权限边界，不是优化
    output_schema: dict         # JSON Schema
    provider: str = ""          # 空 = 用主 provider
    max_rounds: int = 8


# 放在类定义之后：子模块要 `from . import Profile`，循环导入才解得开
from .planning import PLANNING  # noqa: E402
from .directing import DIRECTING  # noqa: E402

__all__ = ["Profile", "PLANNING", "DIRECTING"]
