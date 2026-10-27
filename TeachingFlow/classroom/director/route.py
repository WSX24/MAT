"""路线分支：跨多格的一条走向（跳讲 / 补回头 / 提前收尾）。

为什么单独一个模块：讲到一半的"就地重讲"（v1 换说法 / v2 举例子）是状态机自己的
一条链，讲完还在原地；而路线是**跨格**的 —— 跳过几格、回头重讲几格、或者直接跳到
课尾换一个结局。后者要真的驱动状态机，就必须有一份"从第几格岔出、从第几格汇回、
中途走哪几格"的完整结构，且必须**一次性给全**（fork / steps / merge / est_sec /
when / reason 在同一次写入里）。分成几次补全是灾难：中途那份半成品结构一旦被 J 选中，
状态机就会在没有汇回点的情况下走出课程。

所以这里只放结构本身与它自己的自检，不放进池、不碰状态机：
route.py 是后面三棒（生成 / 执行 / 渲染）唯一的真相，谁都能读，写只在构造时一次。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

#: 三种走向。别处（C 的产出、J 的选择、状态机的执行）都按这三个字符串对齐。
ROUTE_KINDS = ("skip", "revisit", "finish")

#: 摘要里的人话前缀。写成常量表而不是散在 summary 里：三种走向的语气要统一，
#: 而"补回头"和"重新讲"这类说法一旦在两个地方各写一遍，观感就分叉了。
_KIND_LABELS = {"skip": "跳讲", "revisit": "补回头", "finish": "提前收尾"}


def _clip(text: str, limit: int) -> str:
    """截断补省略号。

    照抄 pool._clip 而不是 import：pool 要 import 本模块拿 Route，反向就是循环。
    """
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _num(value: object, default: float = 0.0) -> float:
    """脏数字兜底。

    结构来自模型的一次写入，字段类型没人保证；这里认下"读不出来就用默认值"，
    因为一条时长算错的路线还能讲，一条当场抛异常的路线会打断边界上的决策。
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(_num(value, float(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class RouteStep:
    """路线上的一步：要重讲的那一格，以及用它的哪一种讲法。

    四个字段都可以空着（variant_id / label / text / est_sec）：
    空 = 用该单元的主 text —— 同一个格子在路线上重现一次本身就是有意义的
    （"把这一格再讲一遍"），不要求导演非编出新的说法来。
    """

    unit_id: str
    variant_id: str = ""     # 空 = 用该单元主 text
    label: str = ""
    text: str = ""           # 空 = 用该单元主 text
    est_sec: float = 0.0

    @staticmethod
    def from_dict(d: dict) -> RouteStep:
        """缺字段一律给默认值：一条脏的步骤不该让整条路线不可用。"""
        return RouteStep(
            unit_id=str(d.get("unit_id") or ""),
            variant_id=str(d.get("variant_id") or ""),
            label=str(d.get("label") or ""),
            text=str(d.get("text") or ""),
            est_sec=_num(d.get("est_sec")),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def derive_route_id(kind: str, fork_node: int, merge_node: int) -> str:
    """按冻结的命名约定派生幂等键。

    route_id 是"同一条路线"的判等依据：C 在后台会反复重算同一条走向，
    每次算出新名字就等于每次都是一条新货，池子（CAP=5）会被同一件事填满。
    约定与冻结契约一致：r-skip-<fork>-<跨度> / r-revisit-<fork> / r-finish-<fork>。

    尾数那个数**是跨度（merge - fork），不是"跳过几格"**：从 #8 岔出、在 #11 汇回，
    跨度 3、真跳过 2 格（#9 #10）—— 摘要里说的是后者（见 summary），命名里存的是前者。
    为什么不把名字改成真跳过格数：它是**幂等键**，改口径就等于同一条走向算出新名字，
    池里多一条新货、旧的那条永远活着（CAP=5 被同一件事填满）。这个数在这里只承担一个
    作用 —— 区分"同 fork 不同汇回点"，取跨度还是取跳过格数都幂等；既然冻结契约已经
    写的是跨度，就不动它。人话口径的"跳过几格"统一由 Route.summary 与前端 routeSkip
    负责，两处不必一致、也不该互相追着改。
    """
    if kind == "revisit":
        return f"r-revisit-{fork_node}"
    if kind == "finish":
        return f"r-finish-{fork_node}"
    return f"r-skip-{fork_node}-{max(0, merge_node - fork_node)}"


@dataclass
class Route:
    """一条路线分支。结构在构造时一次给全，之后的执行与渲染都只读它。"""

    route_id: str            # 幂等键，见 derive_route_id
    kind: str                # 必须 ∈ ROUTE_KINDS，否则 ValueError
    fork_node: int           # 在第几格岔出（听完这一格之后）
    merge_node: int          # 汇回第几格；kind == "finish" 时 = 课程总格数
    steps: list[RouteStep]   # skip/finish 为空列表；revisit = 要重讲的格子序列
    #: 符号约定：负数 = 省下的时间（skip），正数 = 多花的时间（revisit），
    #: finish = 剩余未讲的时长取负。理由：J 与图上的详情都要拿一个数比"划不划算"，
    #: 拿两个字段（省/花）比大小就是在每个读者那里各写一遍符号判断。
    est_sec: float
    reason: str              # 一句人话，给 J 与图上详情用

    def __post_init__(self) -> None:
        """只在构造时做"不需要课程长度就能判"的那部分检查。

        为什么 kind 在这里判而同为结构问题的 fork/merge 不在：别的字段错了最多是
        这条路线不好用，kind 错了是"状态机不知道该怎么走"—— 后者必须在进池子之前炸。
        """
        if self.kind not in ROUTE_KINDS:
            raise ValueError(
                f"路线走向必须是 {ROUTE_KINDS} 之一，收到 {self.kind!r}"
            )
        self.fork_node = _int(self.fork_node)
        self.merge_node = _int(self.merge_node)
        self.est_sec = _num(self.est_sec)
        self.steps = [
            step if isinstance(step, RouteStep) else RouteStep.from_dict(step)
            for step in (self.steps or [])
            if isinstance(step, (RouteStep, dict))
        ]

    def validate(self, total_nodes: int) -> None:
        """结构自检：0 <= fork < merge <= total，三种走向各多一条。

        为什么是显式一步而不是塞进 __post_init__：越界要对着"课有多长"判，
        而 Route 构造时手上只有自己这一条路线 —— 课程长度属于调用方（C 知道 plan 有几格）。
        一次写入把结构给全，校验就必须在拿到 total_nodes 的那一处立刻做完。
        """
        total = _int(total_nodes)
        if self.fork_node < 0 or self.fork_node >= self.merge_node:
            raise ValueError(
                f"fork_node({self.fork_node}) 必须 >= 0 且小于 merge_node({self.merge_node})"
            )
        if self.merge_node > total:
            raise ValueError(
                f"merge_node({self.merge_node}) 超出课程总格数({total})"
            )
        if self.kind == "revisit" and not self.steps:
            raise ValueError("revisit 必须给出要重讲的格子（steps 不能为空）")
        if self.kind == "finish" and self.merge_node != total:
            raise ValueError(
                f"finish 的 merge_node({self.merge_node}) 必须等于课程总格数({total})"
            )

    def summary(self, *, max_chars: int = 30) -> str:
        """给 J 与图上详情看的一行摘要：走向 + 跨度 + 时间代价 + 理由截断。

        为什么先写格数与时间、理由放最后：J 要选的是"走哪条"，决策依据是
        "跳几格、省还是花"；reason 是解释，截掉了也不影响它选对。

        格数是**真跳过的格数**，不是 span：fork 与 merge 都是"格子"（从 #8 岔出、
        在 #11 汇回），中间真正被跳过的是 #9 #10 两格 —— 拿 span 当跳过格数会每一格
        都多算一格（课外的人一眼就能看出"跳过 3 格"其实只有两格没讲）。revisit 那档
        说的还是 len(steps)：它是"要重讲几格"，本来就不是跳过。
        """
        span = max(0, self.merge_node - self.fork_node)
        secs = int(round(abs(self.est_sec)))
        label = _KIND_LABELS.get(self.kind, self.kind)
        if self.kind == "revisit":
            head = f"{label}：重讲 {len(self.steps)} 格 · 多 {secs} 秒"
        else:
            head = f"{label}：跳过 {max(0, span - 1)} 格 · 省 {secs} 秒"
        if len(head) >= max_chars:
            return _clip(head, max_chars)
        reason = " ".join(self.reason.split())
        budget = max_chars - len(head) - 1        # 冒号占一格
        if not reason or budget <= 0:
            return head
        return f"{head}：{_clip(reason, budget)}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> Route:
        """从 C 的一次写入里还原。

        除 kind 外一律兜底：一条脏数据最多让这条路线不可用（validate 会拦），
        不该在边界上抛异常。kind 非法必须炸 —— 那是"怎么走"本身没定义。
        """
        kind = str(d.get("kind") or "")
        fork_node = _int(d.get("fork_node"))
        merge_node = _int(d.get("merge_node"))
        raw_steps = d.get("steps")
        steps = list(raw_steps) if isinstance(raw_steps, (list, tuple)) else []
        route_id = str(d.get("route_id") or "") or derive_route_id(kind, fork_node, merge_node)
        return Route(
            route_id=route_id,
            kind=kind,
            fork_node=fork_node,
            merge_node=merge_node,
            steps=steps,
            est_sec=_num(d.get("est_sec")),
            reason=str(d.get("reason") or ""),
        )
