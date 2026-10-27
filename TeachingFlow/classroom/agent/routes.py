"""备课期**预置路线分支**：fork / merge / steps 由结构规则定，文案可让模型润色一次。

为什么备课期要预置路线
----------------------
课堂上「学生没懂 → 当场换一种说法再讲一遍」是状态机自己的一条链（就地变体，讲完还在原地）。
**路线**是另一种东西 —— 它跨好几格：跳过几格、回头补一遍、或者直接收尾。运行期的导演 C
只能在讲到那一格时按**当下事实**造支线（见 agent/producer.py），而池子 CAP=5、路线只在
自己的岔出边界上可见，所以 C 根本没有窗口为「结构上就能预判的岔路」提前铺货 ——
「这一页两个长单元连着讲，学生十有八九在这里掉队」这种事，备课期看课件就知道。

于是分工是：**结构可预判的那部分在备课期铺好（本模块）**，需要看当下学生状态的那部分
留给 C。两者效果相同（都是跨格的岔出与汇回），区别只在「谁在什么时候算出来的」。

铺得稀疏
--------
不铺满（177 格 × 2 = 354 条不是「稀疏」，那是把决策层淹掉）。只在真的会走岔的格子上铺：
revisit 铺在「这一格讲得重」的地方，skip 铺在「这几格讲的是同一件事」的地方，每页至多一条。
实测（真实课件 42 页 / 177 格）见交付报告与 tests/test_plan_routes.py。

规则定结构 + LLM 润色文案
------------------------
fork / merge / steps 完全由下面的确定性规则算出来（可测、可复现、幂等：同一条走向反复算
必须得到同一个 route_id）。模型的唯一权限是把 reason 与 steps 的 label / text 写得更像
老师说的话 —— 模型给了不同的 fork/merge 一律无视：结构一旦让模型改，「这条路线从哪岔出」
就不再可复现，把它当锚点的测试与 J 的边界重算同时失效。
模型不可用（没配、超时、返回垃圾）时用规则文案，**绝不让备课失败**：讲得糙比备不出来强。

对外入口只有一个：plan_routes(plan, deck, *, llm=None, char_warn=250)。
when_for / route_entry 只负责把结果摊成 plan.json 的形状（序列化层，不做任何结构判断），
因为 Route 冻结的字段里没有 when（触发条件属于候选池契约）。
"""

from __future__ import annotations

import copy
import json
import statistics
from dataclasses import dataclass

from director.route import Route, RouteStep, derive_route_id
from state import estimate_seconds

# ===========================================================================
# 阈值与判据（改档位只改这张表）
# ===========================================================================

#: 预置路线一律带这个前缀，与运行期 C 造出来的 r-… 区分开：同一条走向两边都会造，
#: id 撞了会互相覆盖，而且「这条是预置的」这个信息就丢了。
ID_PREFIX = "p-"

#: 正文里像「举例子」的线索（与 agent/variants.py 的 _EXAMPLE_CUES 同口径）。
EXAMPLE_CUES: tuple[str, ...] = ("例句", "例如", "举例", "example", "Example")

#: 页标题里像「整页同一类内容」的线索：练习 / 习题 / 答案 / 解析。
#: 这些页是「同一类内容连讲」，讲得好时可以整页略过，把时间留给后面的讲解页；
#: 而「每个词一页」的讲解页每格讲的是**不同**内容，跳过就真的丢知识点，所以不认。
PRACTICE_CUES: tuple[str, ...] = (
    "练习", "习题", "答案", "解析", "Exercises", "exercises", "Practice", "practice",
)

#: revisit 的门槛：一页至少 3 格才谈得上「本页最重的那一格」（1~2 格的页没有可岔的结构，
#: 从唯一的那格岔出再汇回，等于把这一页讲两遍）。
REVISIT_MIN_UNITS = 3
#: 「讲得重」的主判据：该格时长 >= 同页中位数的 1.5 倍。用**比值**而不是绝对秒数，
#: 因为封面页和练习页的量级完全不同（实测真实课件单格 3.6~29 秒），绝对阈值会一边漏一边滥。
REVISIT_SEC_RATIO = 1.5
#: 三条信号的打分门槛（时长偏重 +2 / kp 多于 1 个 +1 / 像例句 +1）：>=2 意味着
#: **时长偏重是必要条件**，另两条只能加强。为什么不让「kp 多」或「例句多」单独入选：
#: 它们在人教版这种「一个词一页、每页都带例句」的课件里遍地都是（实测 177 格里 31 格带例句），
#: 单独就能入选的判据等于没有闸门。
REVISIT_SCORE_MIN = 2
#: skip 的「连续同质」下限：连 2 格讲同一件事才值得整段跳过（跳 1 格省不下什么，
#: 却白白让 J 在边界上多一个选项要算）。
SKIP_RUN_MIN = 2
#: 整页同质（练习/答案页）至少几格才值得整页跳过。
SKIP_PAGE_MIN_UNITS = 3
#: 「连续同质」段必须**从本页第 2 格起**（页内下标 >= 1）：fork 要落在本页内，跳过的是
#: 「本页剩下的同类内容」。段从页首开始的话 fork 就成了上一页最后一格 —— 决策点落在翻页缝上，
#: 而「这一页开头可以整块不讲」在备课期没有任何证据（学生还没见过这一页）。
#: 实测：放开这一条，真实课件会多铺 11 条（21 条 skip / 42 页），闸门明显太松。
SKIP_RUN_MIN_LOCAL = 1

#: 润色文案的字数上限（与 §5.4 的单句上限同源）：模型吐出一头 800 字的巨兽时，
#: 学生要干等两分钟，所以超限的文案直接丢掉、退回规则文案。
_LABEL_MAX = 8
#: 理由的上限：Route.summary 只留 30 字，写更长也只会被截断。
_REASON_MAX = 30

#: 各走向的**结构化触发条件**（when）。与 agent/producer.py 的 ROUTE_WHEN 是同一张表：
#: 造它的条件就是 J 在边界上重算的条件，两侧一旦分叉，预置的分支会「永远选不中」，
#: 而账上只表现为「这节课没走岔路」（tests/test_plan_routes.py 钉住两边相等）。
#: 为什么不许为空：空 when 恒真 = 这条路线在**每一个**边界都会被选中（director/presets.py 同一个道理）。
_PREROUTE_WHEN: dict[str, dict] = {
    "skip": {"comprehension": {"==": "confirmed"},
             "consecutive_confused": {"<=": 0}},
    "revisit": {"comprehension": {"in": ["vague", "confused"]}},
}

#: 三种走向里备课期只预置两种。
#: **finish 不预置**：提前收尾是课堂节奏判断 —— 看的是「当下还剩多少时间、这班学生跟不跟得上」，
#: 而备课期这两个数都还不存在（那时连这节课会不会拖堂都不知道）。预置一条 finish 等于用
#: 备课时的猜测去替教师决定「后面的内容不讲了」，代价不可逆（跳过的东西学生就是没听到）。
FINISH_WHY_NOT = "finish 是课堂节奏判断（看当下还剩多少时间），备课期无从得知，故不预置"


def when_for(kind: str) -> dict:
    """某走向的结构化触发条件（深拷贝：调用方改它不会污染这张表）。"""
    if kind not in _PREROUTE_WHEN:
        raise ValueError(
            f"备课期不预置 {kind!r} 走向（只预置 {tuple(_PREROUTE_WHEN)}；{FINISH_WHY_NOT}）")
    return copy.deepcopy(_PREROUTE_WHEN[kind])


def route_entry(route: Route) -> dict:
    """一条预置路线在 plan.json 里的形状：Route.to_dict() 逐字一致 + 一个 when。

    只做序列化，不做任何结构判断 —— 结构在 plan_routes 里已经由 Route.validate 过了一遍。
    """
    entry = route.to_dict()
    entry["when"] = when_for(route.kind)
    return entry


# ===========================================================================
# 计划 → 格（node）序列
# ===========================================================================

@dataclass(frozen=True)
class _Node:
    """一格：翻成状态机能跑的那个 node 序号与它自己的备课信息。"""

    node: int              # 全局格号（0 起算）= LessonScript.units 的下标
    slide: int
    unit_id: str
    text: str
    kp: tuple[str, ...]
    est_sec: float


@dataclass
class _Draft:
    """规则算出的一条候选（还没过 Route.validate）。"""

    kind: str
    fork: int
    merge: int
    steps: tuple[int, ...]     # 要走一遍的格（只有 revisit 用得上）
    reason: str


def _field(obj, name: str, default=None):
    """LessonPlan 的 PlanUnit 与它的 dict 形式都认（测试常直接喂 dict）。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _unit_sec(unit) -> float:
    """一格讲多久：PlanUnit.est_sec() 是方法，LectureUnit 是属性，两处口径一致（5 字/秒）。"""
    raw = _field(unit, "est_sec")
    if callable(raw):
        try:
            return float(raw())
        except Exception:
            raw = None
    if isinstance(raw, (int, float)):
        return float(raw)
    return estimate_seconds(str(_field(unit, "text", "") or ""))


def _unit_id(slide_index: int, index_in_slide: int) -> str:
    """格号 → unit_id。**必须与 script_gen.build_lesson_script 的编号逐字一致**：
    路线里的 steps[].unit_id 是状态机执行时唯一的抓手，差一个字符就等于指向不存在的格。
    """
    return f"S{slide_index + 1:02d}-U{index_in_slide + 1:02d}"


def _nodes_of(plan, deck) -> list[_Node]:
    """按**上课顺序**把计划摊成格序列：先按 deck 的页序，再按页内原序。

    为什么按 deck.slides 而不是按 plan.units 的原序：build_lesson_script 就是这么走的
    （for s in deck.slides: for i, pu in enumerate(plan.units_of(s.index))）。
    两处只要有一处口径不同，fork/merge 就会指向另一格 —— 而这种错法在课堂上表现为
    「跳过的是别的内容」，不会报错。
    """
    by_slide: dict[int, list] = {}
    for u in getattr(plan, "units", None) or []:
        by_slide.setdefault(int(_field(u, "slide", -1) or 0), []).append(u)
    slide_order = [int(_field(s, "index", i) or 0)
                   for i, s in enumerate(getattr(deck, "slides", None) or [])]
    if not slide_order:                      # 没有课件就按计划自己的页序（离线/测试用）
        slide_order = sorted(by_slide)
    out: list[_Node] = []
    for slide in slide_order:
        for i, u in enumerate(by_slide.get(slide, [])):
            out.append(_Node(
                node=len(out),
                slide=slide,
                unit_id=_unit_id(slide, i),
                text=str(_field(u, "text", "") or ""),
                kp=tuple(str(k) for k in (_field(u, "kp", None) or [])),
                est_sec=_unit_sec(u),
            ))
    return out


def _pages_of(nodes: list[_Node]) -> list[tuple[int, list[int]]]:
    """[(页号, [该页的格下标])]，页序稳定。"""
    pages: list[tuple[int, list[int]]] = []
    for nd in nodes:
        if pages and pages[-1][0] == nd.slide:
            pages[-1][1].append(nd.node)
        else:
            pages.append((nd.slide, [nd.node]))
    return pages


def _slide_title(deck, slide: int) -> str:
    for s in getattr(deck, "slides", None) or []:
        if int(_field(s, "index", -1) or 0) == slide:
            return str(_field(s, "title", "") or "")
    return ""


# ===========================================================================
# ① revisit：铺在「这一格讲得重」的地方
# ===========================================================================

def _has_example(text: str) -> bool:
    return any(cue in text for cue in EXAMPLE_CUES)


def _revisit_score(nd: _Node, page_median: float) -> int:
    """三条信号打分（判据/阈值见文件头的常量，理由写在常量旁边）。"""
    score = 0
    if page_median > 0 and nd.est_sec >= REVISIT_SEC_RATIO * page_median:
        score += 2
    if len(nd.kp) >= 2:
        score += 1
    if _has_example(nd.text):
        score += 1
    return score


def _heaviest_reason(nd: _Node, page_median: float) -> str:
    """规则文案（LLM 不可用时的兜底，也是给模型看的草稿）。"""
    n = nd.slide + 1
    sec = nd.est_sec
    if page_median > 0 and sec >= REVISIT_SEC_RATIO * page_median:
        return f"第{n}页讲得最长的一格（约{sec:.0f}秒），学生容易在这里掉队"
    if len(nd.kp) >= 2:
        return f"第{n}页这一格同时讲{len(nd.kp)}个知识点，学生容易漏掉一个"
    return f"第{n}页这一格例子最密（约{sec:.0f}秒），学生容易跟不上例子"


def _revisit_drafts(nodes: list[_Node], pages: list[tuple[int, list[int]]]) -> list[_Draft]:
    """每页至多一条：取「最重」的那一格，从它岔出、重讲它自己、在下一格汇回。

    为什么 fork = 那一格本身：revisit 的语义是「这一格讲完学生没跟上 → 回头把它再讲一遍」。
    岔出点必须**正在那一格**（讲完它才谈得上「再讲一遍」），汇回点是紧接着的下一格。
    """
    out: list[_Draft] = []
    for _slide, idxs in pages:
        if len(idxs) < REVISIT_MIN_UNITS:
            continue
        median = statistics.median([nodes[i].est_sec for i in idxs])
        best = -1
        best_key = None
        for i in idxs:
            key = (_revisit_score(nodes[i], median), nodes[i].est_sec, -i)
            if best_key is None or key > best_key:      # 同分取更长的，再同上取靠前的
                best, best_key = i, key
        if best < 0 or int(best_key[0]) < REVISIT_SCORE_MIN:
            continue
        out.append(_Draft("revisit", best, best + 1, (best,),
                          _heaviest_reason(nodes[best], median)))
    return out


# ===========================================================================
# ② skip：铺在「连续几格讲的是同一件事」的地方
# ===========================================================================

def _is_practice_page(title: str) -> bool:
    return any(cue in title for cue in PRACTICE_CUES)


def _longest_run(nodes: list[_Node], idxs: list[int]) -> tuple[int, int] | None:
    """页内最长的「连续同质」段：相邻两格共享至少一个 kp（= 讲的是同一类内容）。

    为什么用 kp 而不用页标题：同一页里页标题是**常量**，它分不出「连续几格讲同一件事」；
    kp 是备课期唯一标了「这几格在讲哪一类内容」的字段。判据落在相邻两格的**交集非空**上，
    所以「讲 3 格同一件事」和「讲 2 格 A、2 格 B」都能正确断段。
    """
    best: tuple[int, int] | None = None
    i = 0
    while i < len(idxs):
        j = i
        while j + 1 < len(idxs):
            if set(nodes[idxs[j]].kp) & set(nodes[idxs[j + 1]].kp):
                j += 1
            else:
                break
        if j - i + 1 >= SKIP_RUN_MIN and (best is None or (j - i) > (best[1] - best[0])):
            best = (i, j)
        i = max(j + 1, i + 1)
    return best


def _skip_drafts(nodes: list[_Node], pages: list[tuple[int, list[int]]], deck) -> list[_Draft]:
    """每页至多一条 skip，两种看法合一（整页同质优先，因为那一条最划算）。

    为什么 fork 取**同质段的前一格**、merge 取**段后一格**：skip 的语义是「听完 fork 那一格
    发现学生跟得上 → 整段跳过 → 到 merge 那一格接着讲」，所以被跳过的是 fork+1..merge-1。
    把 fork 落在段内任何一格都会留下「段首那格还得讲」的尾巴，等于没跳过。
    """
    out: list[_Draft] = []
    for slide, idxs in pages:
        title = _slide_title(deck, slide)
        if _is_practice_page(title) and len(idxs) >= SKIP_PAGE_MIN_UNITS:
            out.append(_Draft(
                "skip", idxs[0] - 1, idxs[-1] + 1, (),
                f"第{slide + 1}页整页是{title or '练习'}，跟得上就整页略过，把时间留给后面的讲解"))
            continue
        run = _longest_run(nodes, idxs)
        if run is None or run[0] < SKIP_RUN_MIN_LOCAL:
            continue
        a, b = run
        out.append(_Draft(
            "skip", idxs[a] - 1, idxs[b] + 1, (),
            f"第{slide + 1}页连续{b - a + 1}格讲的都是同一件事，讲透了可以整段跳过"))
    return out


# ===========================================================================
# ③ 入口：规则算结构 → 过 validate →（可选）模型润色文案
# ===========================================================================

def plan_routes(plan, deck, *, llm=None, char_warn: int = 250) -> list[Route]:
    """把一份备好的计划摊成**预置路线**（纯函数，可离线；不读时钟、不用随机数）。

    llm 可注入一个**同步**可调用（接收 prompt、返回模型原文）或带 .polish(prompt) 方法的
    对象；None = 只用规则文案。测试用假 llm，零网络。
    """
    nodes = _nodes_of(plan, deck)
    total = len(nodes)
    if total < 2:                       # 一格根本没有「岔出与汇回」可言
        return []
    pages = _pages_of(nodes)
    drafts = _revisit_drafts(nodes, pages) + _skip_drafts(nodes, pages, deck)
    drafts.sort(key=lambda d: (d.fork, d.merge, d.kind))

    routes: list[Route] = []
    seen: set[str] = set()
    dropped = 0
    for draft in drafts:
        steps = [RouteStep(unit_id=nodes[i].unit_id, est_sec=nodes[i].est_sec)
                 for i in draft.steps]
        if draft.kind == "skip":
            # 符号约定：负数 = 省下的时间（被跳过那几格的时长之和）
            est = -round(sum(nodes[i].est_sec for i in range(draft.fork + 1, draft.merge)
                             if 0 <= i < total), 1)
        else:
            est = round(sum(s.est_sec for s in steps), 1)
        route = Route(
            # 幂等键：同一条走向反复算必须给出同一个 id，
            # 否则池子（CAP=5）会被同一件事填满
            route_id=ID_PREFIX + derive_route_id(draft.kind, draft.fork, draft.merge),
            kind=draft.kind,
            fork_node=draft.fork,
            merge_node=draft.merge,
            steps=steps,
            est_sec=est,
            reason=draft.reason,
        )
        try:
            route.validate(total)
        except ValueError:
            # 规则也可能算出结构不合法的（例如第一页就从第 0 格之前岔出）：
            # 丢弃并计数，绝不让一条坏路线把备课整体带崩
            dropped += 1
            continue
        if route.route_id in seen:
            dropped += 1
            continue
        seen.add(route.route_id)
        routes.append(route)

    if dropped:
        _warn(plan, f"⚠️ [routes] 丢弃 {dropped} 条结构不合法的预置路线（通过 {len(routes)} 条）")
    if llm is not None and routes:
        _polish(routes, llm, plan, nodes, total, char_warn=char_warn)
    return routes


# ===========================================================================
# ④ LLM 润色文案（只碰 reason / steps.label / steps.text）
# ===========================================================================

def _warn(plan, message: str) -> None:
    """记账走 plan.warnings（与 agent/variants.py 同一口径：审核页会把它们印出来）。"""
    warnings = getattr(plan, "warnings", None)
    if isinstance(warnings, list):
        warnings.append(message)


def _build_prompt(routes: list[Route], nodes: list[_Node], deck, total: int,
                  char_warn: int) -> str:
    """给模型的活儿只有文案：结构已经定了，它只需要把这些岔路写成老师会说的话。"""
    title = str(_field(deck, "title", "") or "")
    lines = [
        "你在帮一位老师把已经定好的**课堂岔路**写成一句人话理由。结构不许改，只写文案。",
        f"课件《{title}》共 {total} 格（格 = 一次讲解单元，按上课顺序编号，从 0 开始）。",
        "",
        "下面每一条路线都已经由规则算好了岔出/汇回位置，你只能写 reason 与 steps 的文案：",
        "",
    ]
    for route in routes:
        kind_cn = {"skip": "跳讲（整段略过）",
                   "revisit": "补回头（重讲一遍）"}.get(route.kind, route.kind)
        lines.append(f"- route_id={route.route_id}  {kind_cn}：第 {route.fork_node} 格岔出、"
                     f"第 {route.merge_node} 格汇回，时间代价 {route.est_sec:+.0f} 秒")
        lines.append(f"  规则草稿理由：{route.reason}")
        for step in route.steps:
            nd = next((n for n in nodes if n.unit_id == step.unit_id), None)
            if nd is not None:
                lines.append(f"  要重讲的格 {step.unit_id}（约 {nd.est_sec:.0f} 秒）原文："
                             f"{nd.text[:60]}")
        lines.append("")
    skeleton = {
        "routes": [{
            "route_id": "<原样抄回上面的 route_id>",
            "reason": "<不超过30字：学生为什么会在这里走岔>",
            "steps": [{"unit_id": "<要重讲的格>",
                       "label": f"<不超过{_LABEL_MAX}字的短标签>",
                       "text": f"<不超过{char_warn}字、用老师口吻把这一格重讲一遍的话>"}],
        }]
    }
    lines.append("只输出 JSON，不要解释、不要代码块围栏：")
    lines.append(json.dumps(skeleton, ensure_ascii=False))
    lines.append(f"硬约束：route_id 必须原样抄回；reason 不超过 {_REASON_MAX} 字；"
                 f"label 不超过 {_LABEL_MAX} 字；text 不超过 {char_warn} 字；"
                 "跳讲类没有要重讲的格，steps 给空数组 []；"
                 "除了 reason 与 steps 里的 label/text，任何字段都不要动。")
    return "\n".join(lines)


def _json_of(raw: str):
    """从模型原文里取出 JSON（容忍前后废话与代码块围栏）。"""
    text = (raw or "").strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        lo, hi = text.find(opener), text.rfind(closer)
        if 0 <= lo < hi:
            try:
                return json.loads(text[lo:hi + 1])
            except Exception:
                continue
    return None


def _call_polisher(llm, prompt: str) -> str:
    """llm 的两种形态：带 .polish(prompt) 的对象，或同步可调用。"""
    fn = getattr(llm, "polish", None)
    if callable(fn):
        return fn(prompt)
    if callable(llm):
        return llm(prompt)
    raise TypeError("llm 必须可同步调用（或带 .polish(prompt) 方法）："
                    "plan_routes 是离线纯函数，不接受需要事件循环的客户端")


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if 0 < len(text) <= limit else ""


def _polish(routes: list[Route], llm, plan, nodes: list[_Node], total: int, *,
            char_warn: int) -> None:
    """模型润色：**只**改 reason / steps.label / steps.text，失败一律退回规则文案。

    为什么文案变了要重算 est_sec：时长是「念这段字要多久」（5 字/秒）。文案换了一套字却
    留着旧时长，J 拿它比「划不划算」时比的就是一个不存在的数。
    """
    by_id = {r.route_id: r for r in routes}
    try:
        raw = _call_polisher(llm, _build_prompt(routes, nodes, plan, total, char_warn))
    except Exception as exc:            # 报错 / 超时 / 客户端形态不对：降级，不抛
        _warn(plan, f"⚠️ [routes] LLM 润色不可用（{type(exc).__name__}: {exc}），"
                    "已降级为规则文案")
        return
    if not isinstance(raw, str) or not raw.strip():
        _warn(plan, "⚠️ [routes] LLM 润色返回为空，已降级为规则文案")
        return
    data = _json_of(raw)
    entries = data.get("routes") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        _warn(plan, "⚠️ [routes] LLM 润色返回的不是合法 JSON，已降级为规则文案")
        return

    applied = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        route = by_id.get(str(entry.get("route_id") or ""))
        if route is None:
            continue                     # 模型编了个不存在的 route_id：无视（结构不归它管）
        touched = False
        reason = _clean_text(entry.get("reason"), _REASON_MAX)
        if reason:
            route.reason = reason
            touched = True
        for raw_step in (entry.get("steps") or []):
            if not isinstance(raw_step, dict):
                continue
            uid = str(raw_step.get("unit_id") or "")
            step = next((s for s in route.steps if s.unit_id == uid), None)
            if step is None:
                continue                 # 模型想加/换要重讲的格：结构不许改
            label = _clean_text(raw_step.get("label"), _LABEL_MAX)
            text = _clean_text(raw_step.get("text"), char_warn)
            if label:
                step.label = label
                touched = True
            if text:
                step.text = text
                step.est_sec = estimate_seconds(text)
                touched = True
        if touched:
            if route.kind == "skip":
                route.est_sec = -round(sum(nodes[i].est_sec for i in
                                           range(route.fork_node + 1, route.merge_node)
                                           if 0 <= i < total), 1)
            else:
                route.est_sec = round(sum(s.est_sec for s in route.steps), 1)
            applied += 1
    if not applied:
        _warn(plan, "⚠️ [routes] LLM 润色没有一条可用（route_id 对不上或文案超限），"
                    "已全部降级为规则文案")
