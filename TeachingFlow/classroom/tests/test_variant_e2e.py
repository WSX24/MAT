"""变体切换的端到端证明 —— 从状态机主线真的把「换了个说法」播出去。

**架构纠偏之后的判据（本文件全部用例按这一条改写）**
J 是**选路者**不是补救者：它在单元 N 的边界上决定"N+1 用 v0 还是用另一条讲法"，
N+1 开播时就换上。所以——
  · 学生说没懂的那一格**一个字都不许重播**（当场重讲是 _gen_rephrase 的活，4 轮递进）；
  · 变体落在它的**下一格**（页尾的下一格是下一页第一个单元）；
  · "从第一句开始"的语义从"重播前先清零"变成"开播前就装上"（progress 本来就是 0）。
旧版这里断言的是"被切换的就是困惑过的那一格、并从第一句重播" —— 那正是"同一段被讲三遍"。

为什么非要走一遍真实的 machine 调度循环
--------------------------------------
Director 自己的五条行为已经由 test_director_decider.py 钉住了，但"能选出变体"和
"课堂上真的换了讲法"是两件事，中间隔着一串只存在于状态机里的判断：预取时机（单元第一句）、
位置键（不许用 state.seq）、装上时机（必须在本格第 1 句之前）、缓存失效（op 不许按旧句数
打点）、以及"同一格整节课只选一次路"。§12 阶段 1 的验收②③⑤全落在这条缝里。
所以这里一律走 next_speech → speak → on_speech_end 的真实链路，只把 LLM 与音频换成
确定性的替身：零网络、零真实 TTS、零真实 sleep。

逐条对应的验收（编号沿用任务书）：
  a) 一节完整的课跑完，variant_applied ≥ 3 次且落在 ≥ 3 个不同的**后继单元**上（§12 验收②）
  b) 下一格从变体第一句**开播**（progress 从 1 起），卡住的那一格只讲 v0 一遍（§12 验收③）
  c) 同一格整节课最多选一次路（防"选路→再选→再换"把池子和账目搅乱）
  d) director=None 时与"脚本里根本没有变体"逐字等价（§11 最后一行硬门槛）
  e) 单元播放中位置跳转 → 那条一格前的决策被判 stale 并用 v0（§6.5）
  f) Director.stats 自洽、RuleDecider 下 timeout 恒为 0（§12 验收⑤的离线口径）
  g) 逐句播放把 state.seq 推高，但不影响位置键（位置键不许用 seq）
  h) 变体的 ops 按**新句数**打点（缓存失效，§9.1 第 6 项）
  l) **不塞任何假信号**：只靠学生真的说「没懂」就能让下一格换讲法，且这一格不重播
  m) 学生全程不说话 → 一次都不许误切（§8.3 的默认路径）
  n) 同一格连说两次没懂 → 既有重讲链升级，但这一格仍然不重播、下一格仍然只换一次
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.variants import attach_variants  # noqa: E402
from director.decider import Director, RuleDecider  # noqa: E402
from director.pool import Candidate, CandidatePool  # noqa: E402
from director.route import Route, derive_route_id  # noqa: E402
from director.presets import (  # noqa: E402
    load_unit_presets,
    preset_candidates,
    preset_when,
)
from eval import MetricsTracker  # noqa: E402
from llm import split_sentences  # noqa: E402
from machine import Machine, PhaseHandlers, SpeechSeg  # noqa: E402
from models import LessonScript  # noqa: E402
from script_gen import build_lesson_script, plan_rule  # noqa: E402
from state import ClassroomState, Phase  # noqa: E402
from agent.variants import _EXAMPLE_LEADS, _REPHRASE_LEADS  # noqa: E402
from tests.fixtures import make_deck  # noqa: E402
from tests.test_machine import FakeLLM, make_settings  # noqa: E402

#: 变体首句的引导语（P 侧生成器加的那几个词，讲义原文里没有）。
#: 直接复用生成器那张表：两处各写一份，迟早一边加了新引导语、断言却还在认旧的那几个。
VARIANT_LEADS = _REPHRASE_LEADS + _EXAMPLE_LEADS


# ===========================================================================
# 夹具：真实脚本 + 无头课堂
# ===========================================================================

def make_variant_script(*, with_variants: bool = True) -> LessonScript:
    """3 页 6 个单元的真实脚本（rule 计划），变体由 P 侧生成器挂上。

    用 rule 计划而不是手写脚本：单元边界、ops、锚点、句数都是"课件真的会怎么切"，
    切换变体之后"op 打在哪个句子上"才有意义。with_variants=False 是 d) 的对照组。
    """
    deck = make_deck()
    plan = plan_rule(deck)
    if with_variants:
        attach_variants(plan, deck)      # 只挂通过体检的变体（v1 换说法 / v2 举例子）
    return build_lesson_script(deck, plan)


def make_director(*, grace_ms: int = 300) -> Director:
    """阶段 1 的口径：J 是 RuleDecider（不调模型），宽限用生产值 300ms。

    宽限刻意留生产值而不是调成 0：要证明的不是"关掉宽限就不等"，而是"预取真的赶上了"
    —— 所以 f) 里 timeout 这个计数必须是 0（§12 验收⑤的离线口径）。
    """
    return Director(CandidatePool(), RuleDecider(), grace_ms=grace_ms)


def seed_confusion(state: ClassroomState, unit_ids, *,
                   comprehension: str = "confused") -> None:
    """直接写 facts_from_state 读的那个字段，制造"这个单元没讲明白"的事实。

    为什么允许不经过 handle_input：学生的"没懂"要走完 回执 → 段末让位 → 重讲链 一整条路
    才会被记成 q_rounds，而一条链只推一轮、还要 3~4 句生成内容；要在 3 个单元上造出
    困惑信号，这条测试就会开始测 LLM 替身而不是测接线。facts 的派生规则另有
    test_director_facts.py 逐条钉住，这里只需要"事实成立"这个前提 —— 而"事实成立之后
    切换到底有没有被播出"，正是本文件要证明的东西。

    用 comprehension 而不是 q_rounds：翻页（_goto_slide）会清 q_rounds，而 comprehension
    只在学生明确回应或探针超时时才被改写，所以它能稳定地"持续困惑"到单元边界。
    """
    for uid in unit_ids:
        state.comprehension[uid] = comprehension


class Classroom:
    """无头课堂：自动回 speech_done，并记下每一句播出**那一刻**的状态快照。

    快照记的是瞬时事实（本单元进度、当时的 seq、_cur_unit_sents、本单元已经切过几次），
    因为"从第几句开始播""op 打在第几句""缓存有没有换"这类断言只有在那一个时刻才看得见。
    """

    def __init__(self, script: LessonScript, *, director: Director | None = None,
                 probe_window: float = 0.15) -> None:
        self.script = script
        self.settings = make_settings(probe_window)
        self.state = ClassroomState(course_id=script.course_id)
        self.llm = FakeLLM()
        self.metrics = MetricsTracker()
        self.director = director
        self.spoken: list[SpeechSeg] = []
        self.segs: list[dict] = []
        #: 测试注入学生行为的钩子：async (Classroom, SpeechSeg) -> None
        self.on_seg = None
        self.machine = Machine(script, self.state, self.settings, self.llm,
                               self.handlers(), self.metrics, director=director)

    def handlers(self) -> PhaseHandlers:
        async def speak(seg: SpeechSeg):
            # 与 web.py 的播放协议一致：先"播完"这一段，再通知状态机
            self.spoken.append(seg)
            self.segs.append({
                "kind": seg.kind,
                "text": seg.text,
                "unit_id": seg.unit_id,
                "progress": self.state.unit_progress.get(seg.unit_id, 0),
                "seq": self.state.seq,
                "applied": self.applied(seg.unit_id),
                "sents": list(self.machine._cur_unit_sents),
                "ops": [op.op for op in (seg.ops or [])],
            })
            await self.machine.on_speech_end(seg.kind)
            if self.on_seg is not None:
                await self.on_seg(self, seg)

        async def notify(level, text):
            return None

        async def phase_changed(state):
            return None

        async def state_changed():
            return None

        async def transcript(u):
            return None

        async def page_changed(idx):
            return None

        async def highlight(anchors):
            return None

        return PhaseHandlers(speak, notify, phase_changed, state_changed,
                             transcript, page_changed, highlight)

    # -- 观测辅助 ---------------------------------------------------------
    def applied(self, unit_id: str) -> int:
        """这个单元到目前为止被切换过几次（读的是 metrics 里的 variant_applied 事件）。"""
        return sum(1 for e in self.metrics.events
                   if e["kind"] == "variant_applied" and e.get("unit") == unit_id)

    def applied_units(self) -> list[str]:
        return [e["unit"] for e in self.metrics.events if e["kind"] == "variant_applied"]

    def unit_segs(self, unit_id: str) -> list[dict]:
        """这个单元播出过的讲解分块（不含回执/探针/回答那些）。"""
        return [s for s in self.segs if s["kind"] == "unit" and s["unit_id"] == unit_id]

    def variant_segs(self, unit_id: str) -> list[dict]:
        """这一格**被换成变体**之后播出的那几句 = 变体的实际播出内容。

        语义已按架构纠偏改写：变体落在"下一格"头上，所以这一格从头到尾都是变体，
        这里返回的就是它播出的全部内容（而不是"重播那一段"）。
        """
        return [s for s in self.unit_segs(unit_id) if s["applied"] >= 1]

    async def run(self, *, timeout: float = 30.0) -> "Classroom":
        # 刻意**不在这里**关 director：收尾是状态机自己的事（_finish_all / _maybe_finished），
        # 这里替它关就等于替它打分，j) 的断言也就证明不了接线真的存在。
        await asyncio.wait_for(self.machine.start(), timeout=timeout)
        await asyncio.wait_for(self.machine.wait_finished(), timeout=timeout)
        return self


# ===========================================================================
# a) 一节完整的课跑完，且至少 3 次切换发生在 3 个不同单元
# ===========================================================================

async def test_full_class_broadcasts_variants():
    """一节完整的课：≥3 次切换，且落点是**学生卡住的那一格的下一格**。

    纠偏后的判据与旧版正好相反：旧版断言"被切换的就是被造出困惑信号的那几个单元"，
    那正是"同一段被讲三遍"的来源。新版断言两件事：
      · 落点 = 后继单元（`script.next_after`）—— J 选的是"接下来那一格怎么讲"；
      · 学生卡住的那几格**一次都不许换**（它们已经讲完了，换讲法只会变成重播）。
    同时保留 §8.3 的默认路径：S02-U04 之后没有任何人困惑，它的后继（下一页第一个单元）
    就该用 v0 —— 这条等号在"预置变体挂空条件（恒真）"时立刻不成立。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    targets = ["S01-U01", "S02-U01", "S02-U02", "S02-U03"]
    seed_confusion(classroom.state, targets)
    await classroom.run()

    applied = classroom.applied_units()
    assert len(applied) >= 3, f"§12 验收②：至少 3 次真实切换被播出，实际 {applied}"
    assert len(set(applied)) >= 3, f"切换必须发生在不同单元上，否则只是同一段被反复改写：{applied}"
    successors = {script.next_after(t).unit_id for t in targets}
    assert set(applied) == successors, (
        f"切换必须落在'学生卡住那一格的下一格'上：{applied} vs {sorted(successors)}")
    # 铁律：讲过的格子一个字都不许重播。站点与它的下一格本来就相邻，两组集合有交集是
    # 正常的（S01-U01 的下一格 S02-U01 自己也说了一次没懂），所以不能拿"有交集"判重播，
    # 直接看**播出去的句子**有没有重复。
    for uid in {s["unit_id"] for s in classroom.segs if s["kind"] == "unit"}:
        texts = [s["text"] for s in classroom.unit_segs(uid)]
        assert len(set(texts)) == len(texts), f"{uid} 有句子被讲了两遍（重播）：{texts}"

    # 一节完整的课：讲到了最后一页，且没有卡死（run() 超时会直接失败）
    assert classroom.machine._finished.is_set(), "课堂必须正常收尾，不能停在半路"
    assert classroom.state.slide_index == script.slide_count - 1

    # 每一次切换都真的播出来了（不是"选出来但没落地"）
    for uid in set(applied):
        assert classroom.variant_segs(uid), f"{uid} 切换了却一句变体都没播"
    assert director.stats["applied"] == len(applied)

    print(f"[V-a] 切换 {len(applied)} 次 / {len(set(applied))} 个单元：{applied}"
          f"（学生卡住的是 {targets}，那几格一次都没换）")


# ===========================================================================
# b) 切换后从变体第一句重播，不从中途续播
# ===========================================================================

async def test_switch_replays_variant_from_its_first_sentence():
    """下一格从变体**第一句**开始播，而且只播这一遍（不重播、也不从中途续播）。

    S02-U02 主单元 3 句、v1 只有 2 句 —— 一旦"从中途续播"或"先播一半再换"，
    句数和进度立刻对不上，一眼就露。同时钉住纠偏的核心：学生卡住的 S02-U01
    只播它自己的 v0 一遍（旧的"换讲法→整段重播"在这里就藏不住了）。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    site, target = "S02-U01", "S02-U02"
    seed_confusion(classroom.state, [site])
    await classroom.run()

    unit = script.unit(target)
    v1 = next(v for v in unit.variants if v["variant_id"] == "v1")
    expected = split_sentences(v1["text"])
    assert len(expected) != 3, "夹具前提：变体句数必须与主单元不同，否则看不出有没有续播"

    # 卡住的那一格：只讲一遍 v0，讲完就过（不换、不重播）
    site_sents = split_sentences(script.unit(site).text)
    assert [s["text"] for s in classroom.unit_segs(site)] == site_sents, \
        "学生卡住的那一格只许讲一遍 v0，不许重播"
    assert classroom.applied(site) == 0

    played = classroom.unit_segs(target)
    assert [s["text"] for s in played] == expected, "下一格必须从头播变体，且一句不差"
    assert [s["progress"] for s in played] == list(range(1, len(expected) + 1)), \
        f"这一格必须从第一句开播（progress 从 1 起）：{[s['progress'] for s in played]}"
    assert all(s["applied"] == 1 for s in played),         "变体是在第一句之前装上的 —— 这一格播出的每一句都该属于变体"
    # 首句必须真的换了个说法（这些引导语在讲义原文里不存在，是 P 侧生成器加的）：
    # 没有这一条，"换上了变体"可能只是把同一段原话又讲了一遍。
    assert played[0]["text"].startswith(VARIANT_LEADS), (
        f"变体首句必须带引导语（换句话说/换个说法/说白了/也就是说/举个例子/比如/打个比方）：{played[0]['text']!r}")

    print(f"[V-b] {site} 讲完 → {target} 用 v1（{v1['label']}）"
          f"从第 1 句开播：{' → '.join(s['text'] for s in played)}")


# ===========================================================================
# c) 同一单元整节课最多切一次（防"切换 → 重播 → 再切"死循环）
# ===========================================================================

async def test_same_unit_switches_at_most_once():
    """同一格整节课只换一次讲法：换过就标记下来，不会"换 → 再进第一句 → 再换"。

    纠偏后这条保护从"防重播死循环"变成"防重复选路"：一次装载就是一次池子重置
    （CAP=5），对同一格反复备货只会把别人的货挤掉、把 stats 的账翻倍。
    同时钉住"讲过的格子只讲一遍"——旧的"切完从头重播"在这里会立刻多出 3 句。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    # 让困惑**一直成立**：只要有人再为同一格选一次路，它就会被再切一次。
    seed_confusion(classroom.state, ["S02-U02"])
    await classroom.run()

    assert classroom.applied("S02-U02") == 0, \
        f"被造出困惑信号的这一格已经讲完，不许再换讲法重播：{classroom.applied('S02-U02')} 次"
    assert classroom.applied("S02-U03") == 1, \
        f"下一格整节课只许换一次讲法：{classroom.applied('S02-U03')} 次"
    assert director.stats["applied"] == 1

    # 讲过的格子只讲一遍：3 句就是 3 句，多一句都说明它被重播了
    assert [s["text"] for s in classroom.unit_segs("S02-U02")] \
        == split_sentences(script.unit("S02-U02").text), \
        "S02-U02 只该讲一遍（3 句），重播会让句数翻倍"

    # 课堂没有因为"换讲法 → 再进第一句"打转：后面的单元照常讲完
    assert classroom.machine._finished.is_set()
    assert classroom.state.slide_index == script.slide_count - 1

    print(f"[V-c] S02-U02 讲完一遍未重播；后继 S02-U03 切换 "
          f"{classroom.applied('S02-U03')} 次，stats={director.stats}")


# ===========================================================================
# d) director=None：与"脚本里根本没有变体"逐字等价（§11 最后一行硬门槛）
# ===========================================================================

async def test_director_none_is_verbatim_equivalent():
    plain = await Classroom(make_variant_script(with_variants=False)).run()
    wired = await Classroom(make_variant_script()).run()

    variant_script = make_variant_script()
    assert sum(len(u.variants) for u in variant_script.units) > 0, \
        "夹具前提：带变体的脚本必须真的有变体，否则这条对照是空的"

    def words(c: Classroom) -> list[tuple[str, str]]:
        return [(u.speaker, u.text) for u in c.state.transcript]

    assert words(wired) == words(plain), \
        "director=None 时，脚本里多出来的变体不许影响课堂上的任何一句话"
    assert wired.state.seq == plain.state.seq, "连句数都必须一样"

    # 接线在 director=None 时是**真的没走到**，不是"走到了但恰好没生效"
    assert wired.machine._variant_switched == set(), "没有 director 就不该有单元被标记成已切换"
    assert wired.machine._pos_epoch == 0, "没有 director 就不该动位置纪元"

    # 每个单元的正文都还是 v0
    for uid in [u.unit_id for u in plain.script.units]:
        assert wired.script.unit(uid).text == plain.script.unit(uid).text, f"{uid} 的正文被改过"

    print(f"[V-d] director=None：{len(words(wired))} 句逐字一致，变体一句都没播出")


# ===========================================================================
# e) 单元播放中位置跳转 → 一格前的决策作废，用 v0（§6.5）
# ===========================================================================

async def test_position_jump_invalidates_the_prefetched_decision():
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    seed_confusion(classroom.state, ["S02-U01"])
    jumped = {"n": 0}

    async def jump(c: Classroom, seg: SpeechSeg):
        # 单元播到第 1 句时学生翻走又翻回来。两次都在同一个 speak 回调里调用，
        # 中间不经过 _seg_unit，所以**不会重新预取** —— 边界上消费的仍是那次旧位置算出的决策。
        # 这正是 §6.5 要防的错位：页和段都回到了原值，只有"期间位置动过"这件事变了，
        # 所以位置键必须带纪元；不带，这条决策就会被当成有效的，在错误的位置换讲法。
        if seg.kind == "unit" and seg.unit_id == "S02-U01" and jumped["n"] == 0:
            jumped["n"] += 1
            await c.machine.handle_command("next")      # 手动推进到下一页
            await c.machine.handle_command("prev")      # 再翻回来

    classroom.on_seg = jump
    await classroom.run()

    assert jumped["n"] == 1, "用例没跑起来：位置跳转没有发生"
    assert director.stats["stale"] == 1, \
        f"位置动过的决策必须被判 stale，而不是照着旧位置生效：{director.stats}"
    assert classroom.applied("S02-U01") == 0, "作废之后这一格必须用 v0 讲完"
    v0 = split_sentences(script.unit("S02-U01").text)
    tail = [s["text"] for s in classroom.unit_segs("S02-U01")][1:]
    assert tail == v0[1:], "v0 的后半段必须原样播完，不许被换掉的讲法截断"

    print(f"[V-e] 位置跳转后：stale={director.stats['stale']}，S02-U01 用 v0 讲完 {len(tail) + 1} 句")


# ===========================================================================
# f) Director.stats 自洽；RuleDecider 下 timeout 恒为 0
# ===========================================================================

async def test_director_stats_add_up_to_boundary_calls():
    script = make_variant_script()
    director = make_director()
    calls = {"n": 0}
    original = director.apply_at_boundary

    async def counted(unit, state, **kw):
        # 收下 Director 契约里的全部关键字（position / target_unit），原样转发 ——
        # 这条用例只数"边界被消费了几次"，不关心参数怎么传
        calls["n"] += 1
        calls.setdefault("targets", []).append(kw.get("target_unit"))
        return await original(unit, state, **kw)

    director.apply_at_boundary = counted
    classroom = Classroom(script, director=director)
    seed_confusion(classroom.state, ["S01-U01", "S02-U01", "S02-U02"])
    await classroom.run()

    stats = director.stats
    landed = sum(stats[k] for k in ("applied", "none", "timeout", "stale", "error"))
    assert calls["n"] > 0, "边界一次都没被消费，这条测试就没意义"
    assert landed == calls["n"], f"每个边界恰好落进一个出口：{stats} vs {calls['n']} 次调用"
    assert stats["timeout"] == 0, f"RuleDecider 不该出现超时（§12 验收⑤）：{stats}"
    assert stats["error"] == 0, f"离线口径下不该有异常：{stats}"
    assert stats["stale"] == 0, "这一局没有位置跳转，不该有作废"
    assert stats["applied"] == len(classroom.applied_units())
    assert stats["prefetched"] >= stats["applied"], "应用过 N 次，至少预取过 N 次"
    # 每个边界都必须**点名**它这次选的是给哪一格用的货（这是纠偏后新增的契约维度）：
    # 不点名就等于退回"空串 = 任意单元"，C 上线后会把远期货装到下一格上。
    assert all(t for t in calls["targets"]), f"边界消费必须带 target_unit：{calls['targets']}"

    print(f"[V-f] 边界 {calls['n']} 次：{stats}")


# ===========================================================================
# g) 逐句播放把 seq 推上去，但不影响位置键（不得用 state.seq 当位置）
# ===========================================================================

async def test_seq_growth_during_a_unit_does_not_invalidate_the_decision():
    """预取与消费之间，逐句播放会把 state.seq 推高几十次 —— 决策照样不许作废。

    纠偏后"预取在站点那一格开头、消费在它末尾"这条时间线没有变，所以这条保护
    依然是位置键的命门：拿 state.seq 当位置，每个单元都会 100% 判 stale。
    这里从事件里读回**做决定的那一格**（decided_at = 站点），验证它确实跨了多句。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    seed_confusion(classroom.state, ["S02-U01", "S02-U02"])
    await classroom.run()

    applied = classroom.applied_units()
    assert len(set(applied)) >= 2, f"这一局必须有多次切换，否则证明不了位置键在撑住：{applied}"

    for ev in applied_events(classroom):
        site = ev["decided_at"]
        # 站点整格的播出内容（站点自己也可能当过别人的"下一格"，所以不按 applied 过滤）
        site_segs = classroom.unit_segs(site)
        assert len(site_segs) >= 2, f"{site} 只有一句，seq 在预取与消费之间根本不会变"
        assert site_segs[-1]["seq"] > site_segs[0]["seq"], \
            "夹具前提：单元内逐句播放确实把 state.seq 推高了（否则这条测试什么都没证明）"

    assert director.stats["stale"] == 0, \
        "位置键不许用 state.seq：逐句播放会让每个单元都判 stale，J 永远退化成 v0"

    print(f"[V-g] state.seq {classroom.segs[0]['seq']} → {classroom.segs[-1]['seq']}，"
          f"stale={director.stats['stale']}，切换 {len(applied)} 个单元")


# ===========================================================================
# h) 变体带来的 ops 按新句数打点（缓存失效，§9.1 第 6 项）
# ===========================================================================

async def test_variant_ops_follow_the_new_sentence_count():
    script = make_variant_script()
    unit = script.unit("S02-U02")
    v0_text = unit.text
    v0_sents = split_sentences(v0_text)
    # 手造一个"句数 → op 落点"都和主单元不同的变体：主单元 3 句、ops 打在第 1/2 句；
    # 变体 2 句、唯一一个 op 打在第 2 句。只要状态机还拿着旧的句子表或旧的 op 列表，
    # 变体的第 1 句就会凭空多出一个 step —— 那就是"字幕与课件操作错位"。
    variant = {
        "variant_id": "v1", "label": "换说法",
        "text": "说白了，我们把这一条拆成两句讲。第二句才给操作。",
        "anchors": [],                                      # 空 = 沿用主单元（§4.1）
        "ops": [{"op": "highlight", "at_sentence": 2, "target": "s02-sh02-p03"}],
    }
    v0_ops_n = len(unit.ops)                 # 切换后 unit.ops 就被替换了，先记下来
    unit.variants = [variant]
    new_sents = split_sentences(variant["text"])
    assert len(new_sents) != len(v0_sents), "夹具前提：变体句数必须与主单元不同"
    assert len(variant["ops"]) != v0_ops_n, "夹具前提：op 条数也必须不同"

    director = make_director()
    classroom = Classroom(script, director=director)
    # 困惑造在**上一格**：换的是它的下一格 S02-U02，这才是 J 的位置
    seed_confusion(classroom.state, ["S02-U01"])
    await classroom.run()

    replay = classroom.variant_segs("S02-U02")
    assert [s["text"] for s in replay] == new_sents
    assert replay[0]["ops"] == [], "变体第 1 句不许带上主单元第 1 句的 op（句数不同 = 打点错位）"
    assert replay[1]["ops"] == ["highlight"], "变体自己的 op 必须落在它自己的第 2 句上"
    for s in replay:
        assert s["sents"] == new_sents, "_cur_unit_sents 必须换成变体的句子表（_ops_for 靠它定位）"

    # 直接验证缓存失效：按旧文本解析出来的 op 缓存不许留着
    stale_keys = [k for k in classroom.machine._ops_cache
                  if k[:1] == ("S02-U02",) and k[2] == v0_text]
    assert stale_keys == [], f"按旧文本算出的 op 缓存必须被清掉：{stale_keys}"

    print(f"[V-h] 变体 {len(new_sents)} 句 / op {len(variant['ops'])} 个："
          f"{[s['ops'] for s in replay]}（主单元 {len(v0_sents)} 句 / {v0_ops_n} 个 op 已被替换）")


# ===========================================================================
# j) 预置变体灌池的契约：确定性 id、幂等覆盖、无变体单元不报错
# ===========================================================================

def test_presets_load_idempotently():
    """池子 CAP 只有 5：装载必须幂等覆盖，否则"翻页回来重进单元开头"几次就把池子堆满。

    这里钉三件事：触发条件是策略表里那两条（不是空条件）、candidate_id 确定性
    （同单元重复装载写回同一条、版本由池维护）、没有变体的单元返回空列表而不是抛异常。
    """
    script = make_variant_script()
    unit = script.unit("S02-U01")
    pool = CandidatePool()

    assert preset_when("v1") == {"comprehension": {"in": ["vague", "confused"]}}
    assert preset_when("v2") == {"consecutive_confused": {">=": 2}}
    assert preset_when("c1") == {}, "认不出来的变体返回空条件，于是不会被装载（空条件恒真 = 抖动）"

    assert load_unit_presets(pool, unit, seq=3, now=1.0) == 2
    assert pool.alive_count() == 2
    cid = "c-S02-U01-v1"
    first = pool.get(cid)
    assert first is not None and first.source == "preset"
    assert first.target_unit == "S02-U01" and first.created_at_unit == "S02-U01"
    assert first.expires_at_seq == 3, "活到本单元边界即可：下一个单元装载时 state.seq 必然更大"

    assert load_unit_presets(pool, unit, seq=3, now=2.0) == 2, "重复装载必须幂等"
    assert pool.alive_count() == 2, "重复装载不许把池子堆满"
    assert pool.get(cid).version == 2, "同 id 重写由池维护版本号"

    empty = script.unit("S03-U01")          # 「目录。」太短，P 侧没给它造变体
    assert preset_candidates(empty, seq=3, now=1.0) == []
    assert load_unit_presets(pool, empty, seq=9, now=3.0) == 0
    assert pool.alive_count() == 0, "seq 前进到下一单元时，上一单元的残留必须被清掉"


async def test_presets_are_loaded_for_the_next_unit_with_a_real_target():
    """池里的预置货必须写着**下一格的真实 unit_id**，不是空串。

    空串在 §4.4 里的语义是"任意单元"（纯条件触发）。现在池子里只有一格的货，所以它
    恰好不出事；但 C 一上线，池子里会同时躺着给 N+5、N+8 备的货和一个空目标的预置货，
    candidates_for(N, facts) 会把它们一并匹配上 —— J 完全可能挑中一条本给远期某一格
    的变体、再被装到下一格上：学生听到的是另一段内容，而且**不会报错**。
    所以这条用例钉住纠偏后的契约：状态机为下一格备货时，货上写的就是下一格的 unit_id。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    site = "S02-U01"
    nxt = script.next_after(site).unit_id
    seen: dict = {}

    async def peek(c: Classroom, seg: SpeechSeg):
        # 站点第一句刚播完时池子里应当已经装好了"给下一格"的货（预取在 _seg_unit 里发）
        if seg.kind == "unit" and seg.unit_id == site and "alive" not in seen:
            seen["alive"] = [(cand.candidate_id, cand.target_unit)
                             for cand in director.pool._items.values()
                             if cand.status == "alive"]

    classroom.on_seg = peek
    await classroom.run()

    alive = seen.get("alive") or []
    assert alive, "夹具前提：站点开播时池子里必须已经有预置货"
    assert all(t == nxt for _, t in alive), (
        f"预置货的 target_unit 必须是下一格的真实 unit_id（{nxt}），不能是空串：{alive}")
    assert all(cid.startswith("c-" + nxt + "-") for cid, _ in alive), (
        f"candidate_id 也必须指向下一格：{alive}")

    print(f"[V-q] 站点 {site} 开播时池子里的货：{alive}")


# ===========================================================================
# i) 学生提问优先：单元末尾有待答问题时，边界上先让位、本单元不切换（§9.1 顺序）
# ===========================================================================

async def test_pending_question_wins_over_variant_switch():
    """§9.1 的顺序：学生提问 > 外脑插播 > 变体选择。

    学生的问题若刚好落在单元末尾，这一格必须**先让位**：老师的下一句话是"回答你"，
    而不是"换一种讲法重讲一遍"（那样学生要干等一整段才被回应，而"换个说法"本来是为了帮他）。
    纠偏之后这条顺序由**播出顺序**直接证明，不再靠"一次都不许切"这个代理：
      · 学生说话的那一格（S02-U02）一次都不许换 —— 它已经/正在讲，换讲法等于重播；
      · 换发生在它的**下一格**（S02-U03），而那一格的第一句必须排在
        回执 → 让位 → 重讲 整条链路之后才播出。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    seed_confusion(classroom.state, ["S02-U02"])      # 条件成立：换成旧顺序就会切
    asked = {"n": 0}

    async def ask(c: Classroom, seg: SpeechSeg):
        # 单元讲到一半时提问，问题一直挂到这个单元的末尾 —— 让位点就落在那个边界上。
        if seg.kind == "unit" and seg.unit_id == "S02-U02" and asked["n"] == 0:
            asked["n"] += 1
            await c.machine.handle_input("没懂")

    classroom.on_seg = ask
    await classroom.run()

    assert asked["n"] == 1, "用例没跑起来：提问没有发生"
    assert classroom.machine._finished.is_set(), "提问与决策点共存不许把课堂卡住"

    # §9.1 的顺序仍然是"学生提问 > 变体选择"，而且现在由**播出顺序**直接证明，不再靠
    # "一次都不许切"这个代理：让位链路（回执 → 让位 → 重讲）必须整条排在变体之前。
    # 让位之后那个决策点（学生刚说没懂、老师刚重讲完）是另一个时机，允许换 —— 但整节课
    # 仍然只许换一次（同一单元最多切一次），换完还得从变体第一句重播。
    assert classroom.applied("S02-U02") == 0, \
        f"学生说话的那一格不许换讲法（那只会变成重播）：{classroom.applied('S02-U02')} 次"
    nxt = script.next_after("S02-U02").unit_id
    first_next_at = next((i for i, s in enumerate(classroom.segs) if s["unit_id"] == nxt), None)
    assert first_next_at is not None, f"夹具前提：下一格 {nxt} 必须被讲到"
    before = {s["kind"] for s in classroom.segs[:first_next_at]}
    assert {"ack", "yield", "rephrase"} <= before, \
        f"下一格必须排在让位链路（回执 → 让位 → 重讲）之后才开播（§9.1 的顺序）：{sorted(before)}"
    assert classroom.applied(nxt) == 1, \
        f"选路发生在让位链路之后，所以下一格仍然该被换上变体：{classroom.applied_units()}"

    kinds = [s.kind for s in classroom.spoken]
    assert "ack" in kinds and "yield" in kinds, f"提问必须走完让位链路（I3）：{kinds}"
    assert "rephrase" in kinds or "answer" in kinds, f"问题必须被真的回应：{kinds}"
    # 课后半段照常推进：讲到了最后一页
    assert classroom.state.slide_index == script.slide_count - 1

    print(f"[V-i] 提问优先：S02-U02 切换 {classroom.applied('S02-U02')} 次（排在让位链路之后），"
          f"让位链路 {kinds.count('rephrase')} 句重讲，stale={director.stats['stale']}，课堂正常收尾")


# ===========================================================================
# j) 收尾把决策层关干净：close 真的被调用，且课后没有悬挂 task
# ===========================================================================

async def test_director_closed_at_class_end_without_dangling_tasks():
    """§11 的收尾不是为了省内存，是为了**可观测性**：课结束后还有一次调用在飞，
    退出时那条 "Task was destroyed but it is pending" 会把真正的错误淹掉。
    """
    script = make_variant_script()
    director = make_director()
    closed = {"n": 0}
    original = director.close

    def spy() -> None:
        closed["n"] += 1
        original()

    director.close = spy
    classroom = Classroom(script, director=director)
    seed_confusion(classroom.state, ["S01-U01", "S02-U01"])
    await classroom.run()

    assert closed["n"] >= 1, "收尾出口（_finish_all / _maybe_finished）必须真的关过一次"
    assert director._slot is None, "槽位必须已清空：课都结束了，那一格没人再消费"
    assert director._tasks == set(), "不许留下还在飞的决策 task"

    for _ in range(3):
        await asyncio.sleep(0)
    # 零例外：课后一个 task 都不许剩 —— 包括 probe 的 _wait_student 轮询。
    # 它原先只靠"相位离开 PAUSING"退出，而课堂收工时相位恰好停在 PAUSING，
    # 于是会活到事件循环关闭（详见用例 k 的注释与 _wait_student 的 docstring）。
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        # _wait_student 是 50ms 一次的有界轮询，条件已经成立（_finished 已 set、相位已离开
        # PAUSING），但最多要再走一个 tick 才真的退出。这里**等这些 task 结束**（最多 0.2s），
        # 不是"假装播放"的 sleep：要证明的正是它们会自己收干净，而不是靠进程退出收尸。
        await asyncio.wait(others, timeout=0.2)
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert others == [], f"课堂自然结束后不许有悬挂 task（含 _wait_student）：{others}"
    # 终态必须是中性态：停在 PAUSING 等于告诉学生"老师还在等你回答"，而课已经结束了
    assert classroom.state.phase is Phase.LECTURING, \
        f"收工相位必须收回 LECTURING，不许停在 {classroom.state.phase.value}"

    stats = director.stats
    assert stats["applied"] >= 2, f"这一局确实切过，断言才有意义：{stats}"
    landed = sum(stats[k] for k in ("applied", "none", "timeout", "stale", "error"))
    assert landed >= stats["applied"] and stats["prefetched"] >= stats["applied"]

    print(f"[V-j] close 调用 {closed['n']} 次，悬挂 task 0，stats 已结算：{stats}")


# ===========================================================================
# k) 探针没被削弱：等待窗口照旧能超时、也照旧能被学生插话打断
# ===========================================================================

async def test_probe_window_still_ends_on_timeout_or_input():
    """改 _wait_student 的退出条件，不能顺手把探针的等待削弱。

    探针窗口本来就有两个出口：没人回应 → 超时（记弱信号并自动翻页），有人插话 →
    立刻结束窗口走让位链路。这条用例一次跑出两个出口，证明两种都还在。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    spoke = {"n": 0}

    async def answer_probe(c: Classroom, seg: SpeechSeg):
        # 探针话术（"到这里清楚吗？"）一播完就开口 —— 这是学生在等待窗口里的回应。
        # 只回应第一次，后面几页的窗口留空，让它们照旧超时。
        if seg.kind == "ask" and spoke["n"] == 0:
            spoke["n"] += 1
            await c.machine.handle_input("没懂")

    classroom.on_seg = answer_probe
    await classroom.run()

    metrics = classroom.metrics
    assert spoke["n"] == 1, "用例没跑起来：探针窗口里没有回话"
    assert metrics.probe_interrupted >= 1, "学生插话必须打断这个窗口（_close_probe('interrupted')）"
    assert metrics.probe_timeout >= 1, "其余页尾的窗口必须照旧超时 —— 窗口本身没被削弱"
    assert "vague" in classroom.state.comprehension.values(), "超时仍要记弱信号（§6.3）"

    kinds = [s.kind for s in classroom.spoken]
    assert "ack" in kinds and "yield" in kinds, f"插话后的让位链路必须仍在（I3）：{kinds}"
    assert classroom.machine._finished.is_set()
    assert classroom.state.slide_index == script.slide_count - 1, "课堂照常讲到最后一页"

    print(f"[V-k] 探针：打断 {metrics.probe_interrupted} 次 / 超时 {metrics.probe_timeout} 次，"
          f"让位链路与自动翻页都正常")


# ===========================================================================
# l) 真实学生输入驱动：说「没懂」→ 重讲完 → 真的换了一种讲法（不塞任何假信号）
# ===========================================================================

def applied_events(classroom: Classroom) -> list[dict]:
    """每一句变体短语被播出前记下的那条事件（§12 验收②的原始证据）。"""
    return [e for e in classroom.metrics.events if e["kind"] == "variant_applied"]


def variant_text_of(script: LessonScript, unit_id: str, variant_id: str) -> str:
    unit = script.unit(unit_id)
    return next(v["text"] for v in unit.variants if v["variant_id"] == variant_id)


async def test_real_student_input_drives_variant_switch():
    """**只靠学生真的说「没懂」**就能触发切换 —— 不调用 seed_confusion。

    这条用例补的是既有用例证不到的那半个事实：seed_confusion 直接把"这个单元没讲明白"
    写进 state.comprehension，等于把前提当结论（"事实成立之后能切换" ≠ "事实根本不会成立"）。
    真实课堂上要让 J 有得选，必须是这样一条链：
      学生说没懂 → 状态机记 confused → 既有机制当场重讲（_gen_rephrase，一个字不改）
      → 归位 → 这一格讲完 → **它的下一格**开播时用换上来的另一种讲法。
    中间任何一环断掉，这条用例都红。注意"这一格自己绝不被重播"也在断言里。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    #: 三个单元各说一次没懂（同一单元说第二次会触发"只选一次路"的保护，见用例 n）
    sites = ["S01-U01", "S02-U01", "S02-U02"]
    targets = [script.next_after(s).unit_id for s in sites]
    asked: set[str] = set()

    async def say_does_not_understand(c: Classroom, seg: SpeechSeg):
        # 每个单元只"说一次"，否则就变成"学生一直在抱怨"了
        if seg.kind == "unit" and seg.unit_id in sites and seg.unit_id not in asked:
            asked.add(seg.unit_id)
            await c.machine.handle_input("没懂")

    classroom.on_seg = say_does_not_understand
    await classroom.run()

    assert asked == set(sites), f"用例没跑起来：这三个单元没被真的问到：{asked}"
    applied = classroom.applied_units()
    assert len(applied) >= 3, f"真实输入必须真的把讲法换掉（≥3 次）：{applied}"
    assert set(applied) == set(targets), \
        f"切换该落在这三个说过没懂的单元的**下一格**上，别处不许误切：{applied} vs {targets}"
    # 铁律：讲过的格子一个字都不许重播（直接看播出去的句子，不看集合关系：
    # 这里的站点与下一格本来就相邻，"有交集"证明不了任何事）
    for uid in {s["unit_id"] for s in classroom.segs if s["kind"] == "unit"}:
        texts = [s["text"] for s in classroom.unit_segs(uid)]
        assert len(set(texts)) == len(texts), f"{uid} 有句子被讲了两遍（重播）：{texts}"

    for site, uid in zip(sites, targets):
        ev = next(e for e in applied_events(classroom) if e["unit"] == uid)
        # ① 信号真的被产生出来、并且真的抵达了决策（缺陷③：confused 全仓没人写）。
        #    这里读的是**做决定那一刻**（上一格）J 看到的事实块，而不是结束时的 state：
        #    页尾探针零回应时仍会把这个单元改记成 vague（弱信号，语义不动），
        #    那不影响这条断言 —— 决策成立与否只看它在那个时刻看到了什么。
        assert ev["decided_at"] == site, f"{uid} 的选路该发生在 {site} 的边界上：{ev}"
        assert "理解confused" in ev["facts_before"], \
            f"{site} 学生说了没懂，J 在边界上看到的理解信号必须是 confused：{ev['facts_before']!r}"
        # ② 同一格整节课只换一次；换的是下一格，不是说过没懂的那一格
        assert classroom.applied(uid) == 1, f"{uid} 只许换一次：{classroom.applied(uid)}"
        # 站点自己也可能当过别人的"下一格"（S01-U01 的下一格就是 S02-U01），所以这里不判
        # "站点没被换"，只判"换它的是**它的上一格**"——重播由上面的句子重复检查兜住。
        # ③ 下一格从变体**第一句**开播（§12 验收③ 的新口径：不是"重播"，是"开播"）
        expected = split_sentences(variant_text_of(script, uid, ev["variant_id"]))
        played = classroom.variant_segs(uid)
        assert [s["text"] for s in played] == expected, f"{uid} 必须从头播变体，且一句不差"
        assert [s["progress"] for s in played] == list(range(1, len(expected) + 1)), \
            f"{uid} 必须从第 1 句开播：{[s['progress'] for s in played]}"

    # ④ 切换前后成对可查，且没有被记成"刚换上就被打断"（§12 阶段 1 验收④）
    ended = [e for e in classroom.metrics.events if e["kind"] == "variant_replay_end"]
    assert len(ended) == len(applied), \
        f"每次切换恰好一条重播结束记录：切换 {len(applied)} 次 / 结束 {len(ended)} 条"
    assert all(e["reason"] in ("done", "yield") for e in ended), \
        f"变体是开播前装上的，不该在没播一句时就被记成 jump：{[e['reason'] for e in ended]}"

    # ⑤ 决策层不阻塞：预取都赶上了，没有一次是在边界上等出来的（§12 验收⑤）
    stats = director.stats
    assert stats["timeout"] == 0, f"预取必须真的赶在边界之前：{stats}"
    assert stats["error"] == 0, f"离线口径下不该有异常：{stats}"
    assert stats["applied"] == len(applied), f"Director 与 metrics 的账必须对得上：{stats}"

    assert classroom.machine._finished.is_set(), "课堂必须正常收尾"
    assert classroom.state.slide_index == script.slide_count - 1

    seen = {e["unit"]: e["facts_before"] for e in applied_events(classroom)}
    print(f"[V-l] 真实输入驱动：{len(applied)} 次切换 / 单元 {sorted(set(applied))}，stats={stats}")
    for uid in targets:
        print(f"        {uid} 的切换前事实块：{seen[uid]}")


async def test_silent_student_never_triggers_a_switch():
    """学生全程不说话（只让探针超时）：课堂照常跑完，且一次都不许误切。

    这是上一条的反面：如果切换是靠"探针超时"这类弱信号撑起来的，安静的一节课就会满堂
    换讲法 —— 学生什么都没说，老师却一直在换说法。§8.3 的默认路径（条件不匹配 → 用 v0）
    说的就是这条：不该换的时候，一句变体都不许出口。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    v0 = {u.unit_id: u.text for u in script.units}
    await classroom.run()

    assert classroom.applied_units() == [], \
        f"没人说话就不该换讲法：{classroom.applied_units()}"
    assert director.stats["applied"] == 0, f"{director.stats}"
    assert "confused" not in classroom.state.comprehension.values(), \
        f"confused 只能由学生明说没懂产生：{classroom.state.comprehension}"
    # 探针照旧超时（弱信号还在，只是单独不足以驱动切换）
    assert "vague" in classroom.state.comprehension.values(), \
        f"页尾没人回应仍记弱信号（§6.3）：{classroom.state.comprehension}"
    assert classroom.machine._finished.is_set(), "课堂必须正常跑完"
    assert classroom.state.slide_index == script.slide_count - 1
    for uid, text in v0.items():
        assert script.unit(uid).text == text, f"{uid} 的正文被换成了变体（不该换）"

    print(f"[V-m] 全程不说话：切换 0 次，探针超时 {classroom.metrics.probe_timeout} 次，课堂跑完")


async def test_same_unit_still_switches_at_most_once_under_real_input():
    """同一单元连说两次没懂：既有重讲链照旧逐级升级，但**这一格一个字都不许重播**，
    变体仍然只落在它的下一格，且只落一次。

    最容易出的毛病是"学生每抱怨一次就被换一次讲法"—— 那会让同一段内容被切成碎片；
    纠偏之后还有更硬的一条：抱怨发生的那一格已经讲完，任何"换讲法"都只能是重播。
    所以这里断言 q_rounds 真的升到了 2（证明第二次确实发生了），下一格仍然只有一条切换。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    site = "S02-U02"
    target = script.next_after(site).unit_id
    asked = {"n": 0}

    async def complain_twice(c: Classroom, seg: SpeechSeg):
        if seg.kind == "unit" and seg.unit_id == site and asked["n"] < 2:
            asked["n"] += 1
            await c.machine.handle_input("没懂")

    classroom.on_seg = complain_twice
    await classroom.run()

    assert asked["n"] == 2, "用例没跑起来：这个单元没被抱怨两次"
    assert classroom.state.q_rounds.get(site, 0) >= 2, \
        f"第二次没懂必须让重讲链升到第 2 轮：{classroom.state.q_rounds}"
    assert classroom.applied(site) == 0, \
        f"被抱怨的那一格不许换讲法（换就是重播）：{classroom.applied(site)} 次"
    assert classroom.applied(target) == 1, \
        f"下一格整节课只许换一次：{classroom.applied(target)} 次"
    assert director.stats["applied"] == 1, f"{director.stats}"
    assert classroom.machine._finished.is_set(), "课堂必须正常收尾"
    assert classroom.state.slide_index == script.slide_count - 1

    print(f"[V-n] {site} 被抱怨 2 次（q_rounds={classroom.state.q_rounds.get(site)}），"
          f"本身 0 次重播；下一格 {target} 切换 {classroom.applied(target)} 次")


async def test_probe_timeout_does_not_downgrade_confused_signal():
    """页尾探针超时是弱信号，不许把学生**明说过的**没懂（强信号）覆盖掉（§12 阶段 1 验收④）。

    两个事实必须同时留在台账里，缺一条课后结论就会反过来：
      · probe_log 记 timeout —— 这是"这个探针没人回应"；
      · comprehension 仍是 confused —— 这是"学生刚刚明说没懂"。
    真实时序恰好把它们叠在一起：学生说没懂 → 老师重讲 → 换成另一种讲法重播 → 页尾探针
    没人回应。若超时把 confused 改写成 vague，"切换前后的理解信号"就只剩弱信号，
    课后再也看不出这次换讲法有没有用 —— 验收④要的对比直接失效。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    site = "S01-U01"                   # 第一页唯一的单元，页尾探针正落在它身上
    target = script.next_after(site).unit_id
    said = {"n": 0}

    async def say_does_not_understand(c: Classroom, seg: SpeechSeg):
        if seg.kind == "unit" and seg.unit_id == site and said["n"] == 0:
            said["n"] += 1
            await c.machine.handle_input("没懂")

    classroom.on_seg = say_does_not_understand
    await classroom.run()

    assert said["n"] == 1, "用例没跑起来：没在页尾探针之前说没懂"
    assert classroom.applied(target) == 1, "夹具前提：下一格确实被换了讲法"
    probes = [p for p in classroom.state.probe_log if p.unit_id == site]
    assert probes and probes[-1].result == "timeout", \
        f"探针自己的结论不许丢，必须照旧记成 timeout：{[(p.unit_id, p.result) for p in classroom.state.probe_log]}"
    assert classroom.state.comprehension.get(site) == "confused", \
        f"强信号被弱信号覆盖了（课后对比会得出相反结论）：{classroom.state.comprehension}"
    # 反面也要在：没有强信号的单元，没人回应时仍记弱信号（既有语义不许被这条闸顺手改掉）
    assert "vague" in classroom.state.comprehension.values(), \
        f"没有强信号的地方，探针超时仍然要记 vague：{classroom.state.comprehension}"

    print(f"[V-o] 页尾超时后 {site} 仍是 {classroom.state.comprehension[site]}，"
          f"probe={probes[-1].result}，别处仍记 vague；下一格 {target} 换上变体")


async def test_confusion_in_probe_window_switches_and_keeps_the_ledger():
    """在**页尾探针窗口**里说没懂：下一格照样被换上变体，且"选出来的次数 = 真装上的次数"。

    这条路与"单元讲到一半说没懂"不是同一条：探针窗口里插话走的是 ACKNOWLEDGING 分支，
    它**不设 resume_point**，于是重讲结束后的归位走的是 `_seg_resume` 里 rp 为 None 的
    兜底出口，归位之后本单元已经讲完（_seg_unit 走 _unit_exhausted）。真实课件（42 页）上
    正是在这个出口把"选中的变体"丢掉过一次：stats 记了一次 applied，课堂上一句变体都没播
    —— 账面换了、课没换，事后最难查的一类。纠偏后这个出口是**唯一**能消费决策的地方
    （_unit_exhausted 里的迟到边界），所以更要把账目本身钉住。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    site = "S01-U01"                   # 第一页唯一的单元：探针话术就在它后面
    target = script.next_after(site).unit_id
    spoke = {"n": 0}

    async def answer_probe(c: Classroom, seg: SpeechSeg):
        if seg.kind == "ask" and spoke["n"] == 0:
            spoke["n"] += 1
            await c.machine.handle_input("没懂")

    classroom.on_seg = answer_probe
    await classroom.run()

    assert spoke["n"] == 1, "用例没跑起来：没在探针窗口里回话"
    assert classroom.applied(target) == 1, \
        f"探针窗口里说没懂，它的下一格同样要换上变体：{classroom.applied_units()}"
    assert classroom.applied(site) == 0, f"{site} 已经讲完，不许重播"
    assert director.stats["applied"] == len(classroom.applied_units()), \
        f"选出来的次数必须等于真的装上的次数（这条路上丢过一次）：" \
        f"{director.stats} vs {classroom.applied_units()}"
    played = classroom.variant_segs(target)
    assert played and played[0]["progress"] == 1, \
        f"必须从变体第一句开播：{[s['progress'] for s in played]}"
    assert classroom.machine._finished.is_set()

    print(f"[V-p] 探针窗口里说没懂 → {target} 换讲法并从第 1 句开播，"
          f"Director.stats={director.stats}")


# 路线分支的端到端接线（S3）：选中 → 交接 → 状态机真的按它走
#
# 决定层把路线交出去（test_director_decider.py）和课堂真的跳过去（test_machine.py）
# 是两件事，中间隔着状态机自己的判断：何时消费交接单、推进怎么收口、被跳过的格子
# 有没有被误当作"讲完了"。所以这一条一律走 next_speech → speak → on_speech_end
# 的真实链路，只把 LLM 与音频换成确定性替身。
# ===========================================================================


async def test_route_candidate_runs_in_the_real_state_machine() -> None:
    """同池里有变体也有路线：路线被选中后课堂**真的跳讲**，变体照旧落地。

    这是 S1 → S3 那个接缝的证明：决定层交出的路线交接单真的被状态机执行了，
    而不是像上一棒那样"选中了却什么也没发生，还把后面的候选堵在池里"。
    同时钉住两件事：
      · 两种载荷共用一张排序表却互不干扰 —— 路线被消费之后，预置变体照旧被选中并装上；
      · 路线只被执行一次、只进台账一次（revisit 重复执行就是两倍的课堂时间）。
    """
    script = make_variant_script()
    director = make_director()
    classroom = Classroom(script, director=director)
    units = [u.unit_id for u in script.units]
    fork, merge = script.order_index(units[0]), script.order_index(units[3])
    skipped = units[fork + 1:merge]
    seed_confusion(classroom.state, [units[3]])   # 汇回那一格"卡住" → 它的下一格换讲法

    route = Route(route_id=derive_route_id("skip", fork, merge), kind="skip",
                  fork_node=fork, merge_node=merge, steps=[], est_sec=-45.0,
                  reason="这两格是同一道例题的重复")
    cand = Candidate(candidate_id=f"c-route-{route.route_id}", target_unit="",
                     when={}, source="agent_c", created_at=1.0,
                     created_at_unit=units[0], expires_at_seq=99, route=route)
    assert director.pool.put(cand) is True, "路线候选没进池子"

    await classroom.run()

    played = [s["unit_id"] for s in classroom.segs if s["kind"] == "unit"]
    spoken = [uid for i, uid in enumerate(played) if i == 0 or played[i - 1] != uid]
    assert spoken[:2] == [units[0], units[3]], f"skip 之后必须在汇回那一格开口：{spoken}"
    for uid in skipped:
        assert uid not in spoken, f"被跳过的 {uid} 一句都不该念：{spoken}"

    assert director.pool.get(cand.candidate_id).status == "consumed", \
        "交出去的路线必须是 consumed（否则它会在每个边界上被反复选中）"
    applied_events = [e for e in classroom.metrics.events if e["kind"] == "route_applied"]
    assert len(applied_events) == 1, f"同一条路线只执行一次：{applied_events}"
    assert applied_events[0]["route_kind"] == "skip", applied_events[0]
    assert applied_events[0]["to"] == units[3], applied_events[0]
    assert classroom.state.route_log[0]["skipped"] == skipped, classroom.state.route_log

    # 两种载荷互不干扰：路线走完之后，预置变体照旧落在汇回那一格的**下一格**上
    assert classroom.applied(units[3]) == 0, \
        f"{units[3]} 是落点，不该再换讲法：{classroom.applied_units()}"
    assert classroom.applied(units[4]) == 1, \
        f"路线与变体必须能共处一节课：{classroom.applied_units()}"
    assert director.stats["applied"] == 1 + len(classroom.applied_units()), \
        f"applied 记的是\"边界上落实的决策\"（两种载荷共用同一个出口）：{director.stats}"
    assert classroom.machine._finished.is_set(), "跳讲之后课堂必须照常上完"
    print(f"[V-r] 路线 skip：{skipped} 被跳过 → {units[3]}；变体落在 {units[4]}；"
          f"stats={director.stats}")


# ===========================================================================
# 入口（python tests\\test_variant_e2e.py）
# ===========================================================================

ALL = [test_full_class_broadcasts_variants,
       test_switch_replays_variant_from_its_first_sentence,
       test_same_unit_switches_at_most_once,
       test_director_none_is_verbatim_equivalent,
       test_position_jump_invalidates_the_prefetched_decision,
       test_director_stats_add_up_to_boundary_calls,
       test_seq_growth_during_a_unit_does_not_invalidate_the_decision,
       test_variant_ops_follow_the_new_sentence_count,
       test_pending_question_wins_over_variant_switch,
       test_director_closed_at_class_end_without_dangling_tasks,
       test_probe_window_still_ends_on_timeout_or_input,
       test_presets_load_idempotently,
       test_presets_are_loaded_for_the_next_unit_with_a_real_target,
       test_real_student_input_drives_variant_switch,
       test_silent_student_never_triggers_a_switch,
       test_same_unit_still_switches_at_most_once_under_real_input,
       test_probe_timeout_does_not_downgrade_confused_signal,
       test_confusion_in_probe_window_switches_and_keeps_the_ledger,
       test_route_candidate_runs_in_the_real_state_machine]


async def _main() -> int:
    failed = 0
    for fn in ALL:
        try:
            await fn()
            print(f"  ✅ {fn.__name__}\n")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}\n")
        except Exception as e:
            failed += 1
            print(f"  💥 {fn.__name__}\n     {type(e).__name__}: {e}\n")
    total = len(ALL)
    print(f"结果：{total - failed}/{total} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
