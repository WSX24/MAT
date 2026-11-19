"""上课时的决策层：事实 → 候选 → 选择 → 应用。

叫"导演"而不是"决策器"：它管的是**节奏**（这一格要不要换讲法、要不要改走另一条
线：跳讲 / 补回头 / 提前收尾），不产出知识内容 ——
内容由 P 备、C 造，J 只选。这条分工决定了三者的权限边界，也决定了本包的依赖方向：
director 可以 import state，反方向不行，因为状态机不该知道"决策"这回事存在。
"""

from .decider import DecisionProvider, Director, RuleDecider
from .facts import DecisionFacts, facts_from_state, matches
from .pool import Candidate, CandidatePool, UnitVariant
from .route import ROUTE_KINDS, Route, RouteStep

__all__ = [
    "Candidate",
    "CandidatePool",
    "DecisionFacts",
    "DecisionProvider",
    "Director",
    "ROUTE_KINDS",
    "Route",
    "RouteStep",
    "RuleDecider",
    "UnitVariant",
    "facts_from_state",
    "matches",
]
