"""预置变体 → 候选池：P 课前写进 plan.json 的变体，开课时按单元灌进池子。

对应《双脑三体方案设计》§4.6（结构化触发条件）、§5.3（P 的变体）、§8.3（默认路径）、
§9.1 第 5 项（一格前瞻）。池子本身是"J 此刻能看的候选"，不是"整节课的变体库"，
所以这里做的事只有两件：给变体配一条说得清触发时机的条件，再把它按单元装进去。

**装的是哪一单元：下一格。** J 是选路者不是补救者 —— 它在单元 N 的边界上决定
"N+1 用 v0 还是用另一条讲法"，所以池子里此刻该有的货是 **N+1 的**变体，货上写的
target_unit 也是 N+1（不是空串：空串 = 任意单元，见 load_unit_presets）。
一次性装整页的变体在 CAP=5 下必然失败（一页就 5~8 个单元），这条纪律由调用方
（machine._prefetch_decision）保证。

为什么不能一律用空 when
------------------------
`matches({}, facts)` 恒为 True（见 facts.py）。如果每个单元的预置变体都挂空条件，
J 在每个单元边界都会选出一个变体 —— 那不是教学，是抖动：学生每讲完一段就听到
"这段话换个说法"，而"这段没听懂、所以要换"这个因果关系在课堂上根本不存在。
更要紧的是 §8.3 的默认路径会永远走不到：池子里永远有匹配的候选，
"条件不匹配 → 用 v0"这条最常见的分支就成了死代码，谁也不再验证它。

策略（设计决定，不是随手取的阈值）
----------------------------------
- **v1「换说法」← comprehension ∈ {vague, confused}**：理解信号偏弱时先换一种说法。
  confirmed 与空信号一律不动 —— 学生已经听懂了还换个讲法，只会让他以为自己听错了。
- **v2「举例子」← consecutive_confused ≥ 2**：连续没懂两次才上具体例子。
  第一次没懂先换说法（v1），第二次还没懂才值得把例子提上来；一上来就举例会把
  "讲清楚"和"给台阶"混在一起，学生反而分不清哪句是定义、哪句是例子。

预置路线（备课期 plan.json 的 routes）也装在这里
------------------------------------------------
就地变体停产之后，备课期产出的是**跨格的路线分支**（skip / revisit / finish，结构见
director/route.py，产出见 agent/routes.py），运行期由状态机的 _take_route 执行。
它们与变体走**同一个池、同一套纪律**：一个边界只服务眼前这一格、when 不许为空、
幂等键由结构决定、满了只有更远期的货能被挤掉。所以装载也放在同一个模块 ——
唯一不同的是"哪一格触发"：变体的窗口是**下一格**，路线的窗口是**岔出格 == 当前这一格**
（见 load_route_presets）。拆成两个模块，就等于把"空 when 恒真"那条教训挪到另一个
文件里重犯一次。
"""

from __future__ import annotations

import copy

from state import LectureUnit

from .pool import Candidate, CandidatePool, UnitVariant

#: candidate_id 前缀。形如 `c-<unit_id>-<variant_id>`：确定性 + 幂等（见 preset_candidates）。
CANDIDATE_PREFIX = "c"

#: 变体 id → 触发条件。**这就是"预置变体什么时候该上场"的全部策略**，
#: 只有登记在这里的变体才会被灌进池子（见 preset_candidates 的取舍）。
_PRESET_WHEN: dict[str, dict] = {
    "v1": {"comprehension": {"in": ["vague", "confused"]}},
    "v2": {"consecutive_confused": {">=": 2}},
}


def preset_when(variant_id: str) -> dict:
    """变体的结构化触发条件；认不出来的变体返回 `{}`（= 恒真），因此不会被装载。

    返回深拷贝而不是模块里的那个 dict：条件是 `matches` 的输入，调用方顺手改一下
    就等于在线改教学策略，而且这种改动不会报错，只会让课堂换错讲法。

    认不出来返回 `{}` 而不是编一个条件：空条件的语义在 §4.6 里就是"恒真"，
    本模块自己不去猜某个 `c1` 该在什么时候上场 —— 猜错的表现是课堂抖，
    而"不装"的表现只是这一段用 v0，后者是安全的默认值（§8.3）。
    """
    return copy.deepcopy(_PRESET_WHEN.get(str(variant_id or "").strip(), {}))


#: agent/producer.py 的模块缓存（见 _producer）。
_PRODUCER = None


def _producer():
    """agent/producer.py 模块（延迟 import）：只借它的两张表 —— ROUTE_WHEN 与
    EXPIRE_MARGIN_UNITS。

    为什么不在模块顶部 import：agent 层本来就 import director 层
    （producer → pool / route / facts），这里再反向 import 一次就把两层焊成环。
    今天不炸只因为 producer 恰好还没有 import presets；哪天有人在 producer 里加一行
    `from director.presets import ...`，import 顺序就取决于"谁先 import 谁"，
    而那种错只在某一条启动路径上出现（另一条路径上一切正常）。延迟到调用时取，
    就算真有环也断在运行时，import 期永远见不到半初始化的模块。

    为什么要借、不在本模块各写一份：when 的语义是"造它的判断就是 J 重算它时的判断"
    （见 ROUTE_WHEN 的注释），阈值复制一份 = 两份阈值迟早分叉，而分叉的表现是
    "预置的路线永远选不中"，账上只表现为"这节课没走岔路"。expires 的余量同理：
    预置路线与运行期路线必须"在岔出边界之后还能活同样几个边界"。
    """
    global _PRODUCER
    if _PRODUCER is None:
        from agent import producer as _p
        _PRODUCER = _p
    return _PRODUCER


def route_when(route) -> dict:
    """一条预置路线的触发条件（when）；返回 `{}` = 认不出来，于是**不装载**。

    两档来源，优先级从高到低：
      ① Route 实例上的附加属性 `when`：plan.json 的每条 route 都带它
         （agent/routes.py 的 route_entry），谁把 plan 解析成 Route 时顺手挂上就用它 ——
         这是"plan 侧透传"唯一需要的接口，course.py 今天还没挂，所以实际走的是 ②。
         走附加属性、而不是给 Route 加一个字段：Route 的字段是被建池、状态机、渲染
         三处对齐的冻结契约，为一个"只在装载时读一次"的条件动它，等于让每个读者都
         多一个可能为空的字段。
      ② agent/producer.py 的 ROUTE_WHEN[kind]：它就是运行期路线造货时查的那张表，
         与备课期的 when_for（agent/routes.py）是同一张（tests/test_plan_routes.py
         钉住两边相等）。所以今天 plan.json 里的 when 与本函数的兜底**逐字相同** ——
         兜底不是"猜一个条件"，是取那张唯一的表。

    **空 when 绝不进池**：`matches({}, facts)` 恒真，一条空条件的路线会在**每一个**
    边界上被 J 选中。认不出来就整条不装 —— 少一条岔路只是这一节照常讲，
    多一条恒真的岔路是每一节都在乱岔（与 preset_when 里那条理由是同一件事）。
    """
    raw = getattr(route, "when", None)
    if isinstance(raw, dict) and raw:
        return copy.deepcopy(raw)
    kind = str(getattr(route, "kind", "") or "")
    return copy.deepcopy(_producer().ROUTE_WHEN.get(kind) or {})


def preset_candidates(unit: LectureUnit, *, seq: int, now: float,
                      target_unit: str | None = None) -> list[Candidate]:
    """把一个单元的预置变体变成候选（**不写池**，写池见 load_unit_presets）。

    candidate_id 取 `c-<unit_id>-<variant_id>`：确定性，于是同一单元重复装载会在池里
    **覆盖**同一条（CandidatePool.put 幂等、version+1），而不是无限堆积 ——
    "重放这一段"、让位归位、翻页回来都会重进单元开头，堆一次的代价是 CAP 5 被同一单元占满。

    `created_at` 用调用方给的 now（monotonic）：池按 `-created_at` 排序（§8.2），
    自己取时钟会让同一批候选的先后取决于装载顺序之外的噪声。

    `target_unit`：候选的 target_unit 字段，None = 就是 unit 自己。状态机装载"下一格"
    的货时传的就是下一格的 unit_id —— 货上写的和它真正要用的地方必须一致
    （理由见 load_unit_presets）。candidate_id 里的 unit_id 也来自同一个 unit，两者天然对得上。

    跳过两类变体：文本为空（选中它学生只能干等）与没有触发策略的（空条件恒真 → 抖动）。
    """
    if unit is None:
        return []
    target = unit.unit_id if target_unit is None else target_unit
    out: list[Candidate] = []
    seen: set[str] = set()
    for raw in getattr(unit, "variants", None) or []:
        variant = UnitVariant.from_dict(raw) if isinstance(raw, dict) else raw
        vid = str(getattr(variant, "variant_id", "") or "").strip()
        if not vid or vid in seen:      # 同单元内 id 重复：取第一个，后一个覆盖没有意义
            continue
        when = preset_when(vid)
        if not when:
            continue                    # 没有触发策略 → 不装（理由见 preset_when）
        if not str(variant.text or "").strip():
            continue
        seen.add(vid)
        out.append(Candidate(
            candidate_id=f"{CANDIDATE_PREFIX}-{unit.unit_id}-{vid}",
            target_unit=target,
            variant=variant,
            when=when,
            source="preset",
            created_at=now,
            created_at_unit=unit.unit_id,
            expires_at_seq=seq,
        ))
    return out


def load_unit_presets(pool: CandidatePool, unit: LectureUnit, *, seq: int,
                      now: float, target_unit: str | None = None) -> int:
    """把一个单元的预置变体灌进池子，返回**实际写进去**的条数（挤不动就少写）。

    为什么顺手 `pool.expire_stale(seq)`：CAP 只有 5（§6.2 是给 J 的输入长度封顶），
    而预置变体是按**单元**装载的，上一次装载的货在这一次装载时已经没有任何读者。
    不在这里清掉，五条之后的新货全部被拒收 —— 课堂会安静地在第三个单元之后
    再也不切换，而这件事在日志里只表现为"后面没换过讲法"。

    为什么 `expires_at_seq` 直接取 seq（§4.4 的"单元序号超过它即作废"）：
    这条候选唯一的读者是**接下来那个边界**，而边界消费根本不查 expires_at_seq
    —— 只有 expire_stale 会。而下一次 expire_stale 一定来自下一次装载，
    那时调用方传进来的 `seq` 必然更大（每次装载的目标都往后推一格），
    于是这里留下的旧货会在下一次装载时被清掉。留得刚好，池子的容量就永远
    只服务于眼前这一格，而不是被整节课的变体摊薄。

    **`seq` 是单元序号，不是句子计数器 —— 这个约定必须钉死在调用方。**
    两个数长得都像"进度"，混用却不会报错，只会让池子悄悄出问题：
      · 预置变体的货是"给下一格用的"，按单元序号记账时，来回都是同一把尺子，
        表现与句子计数器完全一样（都是单调推进），所以历史上这里一直传的是
        `state.seq`（每吐一句 +1）而没人察觉；
      · 但 C 的候选按 `agent/producer.py` 的 `ordinals[target] + 余量` 记账，
        单位是**第几格**。若装载仍拿句子计数器去比，C 给"当前+5"备的货（expires 是
        个位数）会在几百句话之内被判过期，于是**每一条 C 的候选都活不到目标格**，
        而课堂上只表现为"后面再也没换过讲法"。
    所以 `machine._prefetch_decision` 现在传的是 `script.order_index(target.unit_id)`，
    两边的时钟因此是同一个（回归用例见 tests/test_proposer.py 的时钟那一条）。

    **上货纪律（架构纠偏后）**：状态机只为**下一格**备货 —— 单元 N 开始时装 N+1 的变体，
    不是装 N 的、更不是装整页的。原因是 CAP=5：一整页的变体一装就把池子占满，
    后面的单元再也装不进来（真实课件一页 5~8 个单元）。

    为什么每一批货都写着**下一格的真实 unit_id**：target_unit 在 §4.4 里是"这条候选给哪个
    单元用"，空串则等于"任意单元"。状态机装载时传的 unit 是下一格，target_unit 也是
    下一格 —— 货上写的和它真正要用的地方必须一致。**不能**为了让筛选命中就留空：
    池子里现在只有一格的货（上一次装载的货已被上面的 expire_stale 清掉），空串恰好不出事；
    但 C 一上线，池子里会同时躺着给 N+5、N+8 备的货和一个空目标的预置货，
    candidates_for 会把它们一并匹配上 —— J 挑中一条本给远期某一格的变体、再装到下一格上，
    学生听到的是另一段内容，而且不报错。

    **满池不再一律饿死这一格的 v1/v2（实测缺陷）**：预置变体的 expires 是**下一格的
    序号**（本节最后一段的记账约定），也就是池子里最靠当期的那一档；CandidatePool.put
    因此允许它们挤掉一条**最远的未来备货**（判据见 pool._eviction_victim，与门卫共用）。
    这是本次修复最直接的收益：C 的远期备货把 CAP=5 占满时，下一格的"换说法 / 举例子"
    照样装得进来 —— 改动前这里返回 0，J 在那个边界上什么都看不见，连续七八格只能讲 v0。
    一次装载最多挤 2 条（v1 + v2），所以不需要"一次唤醒一次驱逐"那道闸（那是 producer
    的纪律，见 agent/producer.py 的两遍写池）。

    那筛选怎么办？**事实来源与候选目标在 Director 上是两个参数**：unit 给 facts
    （"这格学生卡没卡"，事实仍从当前单元派生），target_unit 给筛选键（"下一格用哪种讲法"）。
    见 director/decider.py 的 prefetch / apply_at_boundary。
    """
    pool.expire_stale(seq)          # 上一次装载的残留先腾位，再装这一批
    n = 0
    for cand in preset_candidates(unit, seq=seq, now=now, target_unit=target_unit):
        # allow_evict 用默认值（开）：满了就挤掉最远的那条远期备货（理由见本节最后一段）。
        if pool.put(cand):
            n += 1
    return n


def route_candidates(routes, *, fork_node: int, fork_unit: str, target_unit: str,
                     now: float) -> list[Candidate]:
    """把"岔出格就是当前这一格"的预置路线变成候选（**不写池**，写池见 load_route_presets）。

    窗口只有一条，也就是唯一的过滤条件：`route.fork_node == fork_node`（当前正在讲的格）。
    为什么是它：J 在**听完这一格之后**才决定"下一格往哪走"（§6.4 ①），所以一条路线只在
    它的岔出格正好是眼前这一格时有意义。岔出格已经过去的路线**一条都不装**：
    它们此刻进池也选不中（J 的候选按 target_unit 过滤，而它们的 target 是已经讲过的格子），
    占着 CAP=5 里的一格只会把当期刚需挤走 —— 这就是"不抢跑的窗口"要挡的那件事。
    反过来"岔出格在将来"的路线也不会在这里出现：本次装载只问 fork == 当前格。

    candidate_id 直接取 `route.route_id`：course.parse_plan_routes 只放行 `p-` 前缀的
    （运行期 C 的是 `r-...`，见 director/route.py 的 derive_route_id），于是两边天然不撞 id，
    而"同一条路线重复装载"命中同一个 candidate_id → 池按 id 覆盖、version+1，**补齐而不是堆新的**。

    expires_at_seq 与运行期路线**同一把尺子**（agent/producer.py 的 _to_route_candidate）：

        expires = fork_node + 1 + EXPIRE_MARGIN_UNITS

    也就是"岔出边界之后还能活 EXPIRE_MARGIN_UNITS 个边界"。不借变体装载的那个 seq
    （= 目标格序号）：两处口径一旦各写一份，预置路线与运行期路线就会有一份先过期，
    而课堂上只表现为"这一边再也没被选中过"。

    target_unit 写的是**下一格的真实 unit_id**（不是空串）：空串在 §4.4 里是"任意单元"，
    池子里一旦同时有给别的格备的货，J 就可能挑中一条本给另一格备的路线再从这个边界岔出去
    （理由与 preset_candidates 那一处完全相同）。
    """
    if not routes or not target_unit or int(fork_node) < 0:
        return []
    expires = int(fork_node) + 1 + _producer().EXPIRE_MARGIN_UNITS
    out: list[Candidate] = []
    seen: set[str] = set()
    for route in routes:
        rid = str(getattr(route, "route_id", "") or "").strip()
        if not rid or rid in seen:      # 没有幂等键的候选装进去也删不掉；同批重复取第一个
            continue
        try:
            fork = int(getattr(route, "fork_node", -1))
        except (TypeError, ValueError):
            continue
        if fork != int(fork_node):      # 窗口纪律：只有"岔出格 == 当前这一格"的路线
            continue
        when = route_when(route)
        if not when:
            continue                    # 空 when 恒真 → 不装（理由见 route_when）
        seen.add(rid)
        out.append(Candidate(
            candidate_id=rid,
            target_unit=target_unit,
            route=route,
            when=when,
            source="preset",
            created_at=now,
            created_at_unit=fork_unit,
            expires_at_seq=expires,
        ))
    return out


def load_route_presets(pool: CandidatePool, routes, *, fork_node: int, fork_unit: str,
                       target_unit: str, now: float) -> int:
    """把预置路线灌进池子，返回**实际写进去**的条数（挤不动就少写）。

    调用点是状态机那个既有的装载点（machine._prefetch_decision，紧接着 load_unit_presets），
    于是两种预置货走同一条通道：同一个 CandidatePool、同一套 CAP / 幂等 / 三态 / 驱逐、
    同一把 candidates_for 过滤、同一个 _take_route 执行。**没有为预置另开通道** ——
    J 在边界上看到的永远是一个有序列表，而不是"变体一路、路线一路"。

    **本函数不扫过期**（不调 pool.expire_stale）：同一次边界上 load_unit_presets 刚用同一个
    单元序号扫过一遍，再扫一次是同一件事做两次；而"谁负责扫"写在一处，才不会出现
    "两个装载器各扫一次、游标各推一格"的经典分歧。

    **满了怎么办：走池子既有的那把尺子**（allow_evict 用默认值 = 开）。判据只看 expires
    的单调性，于是预置路线的行为与运行期路线逐字一致：
      · 池里有一格**更远的未来备货**（C 的 [+5,+15] 那些）→ 挤掉最远的那条，路线照装；
      · 池里全是**当期货**（本边界的变体、刚装上的一批）→ 它们的 expires 比路线更小，
        按纪律不许动，于是 `put` 拒收、本函数返回 0 —— 这一格照常讲，下一格照常用 v0。
    这条"不挤当期变体"不是本函数自己加的闸，而是池子本来就这么判；在这里不额外开洞的理由：
    另一份容量纪律就是第二个真相，而"当期优先"这件事只该有一个地方说了算。
    """
    if pool is None:
        return 0
    n = 0
    for cand in route_candidates(routes, fork_node=fork_node, fork_unit=fork_unit,
                                 target_unit=target_unit, now=now):
        # allow_evict 用默认值（开）：判据在池子里，与运行期路线同一把尺子（理由见上一段）。
        if pool.put(cand):
            n += 1
    return n
