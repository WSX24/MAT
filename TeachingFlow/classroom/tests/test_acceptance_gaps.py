"""验收补齐 —— §12 里三条明写、但此前没有用例钉住的验收项。

为什么单独一个文件
------------------
这三条验收有一个共同点：它们都不是"某个函数对不对"，而是"跑完一整节课之后还能查到什么"。
缺口 1（阶段 1 验收④：切换前后 probe_result）问的是事件里有没有成对的证据；
缺口 2（阶段 2 验收④：连续 20 条学生输入零死锁）问的是被 20 条输入轮番打断的课还能不能收尾；
缺口 3（阶段 2 验收①：J 的 p50 < 300 ms）问的是那条延迟门槛到底钉在哪 —— 文档里还是测试里。
所以这里一律走真状态机 + 假 LLM：零网络、零真实 TTS、不靠 sleep 造延迟
（唯一的真实等待是探针窗口的既有口径，与既有用例一致）。

逐条对应（编号沿用任务书）：
  A) 阶段 1 验收④：variant_applied 带 probe_before / facts_before，
     variant_replay_end 带 probe_after / facts_after，两者按 unit 成对（a1 / a2）；
     重播被学生提问打断（a3）、被快速翻页打断（a4）同样记一次"重播结束"。
  B) 阶段 2 验收④：连续 20 条学生输入 —— 不卡死 / 异常不逃逸 / 每条都被处理 / 无悬挂 task（b1）。
  C) 阶段 2 验收①：J 的整条路径（事实块 + 池子筛选 + decide）p50 < 300 ms（c1）。
  D) 阶段 2 验收②：yield_latency 的离线代理口径 —— 决策点在边界上一次都没补等（d1）。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import director.decider as decider_mod  # noqa: E402
from director.decider import RuleDecider  # noqa: E402
from director.facts import MAX_PROMPT_CHARS, facts_from_state  # noqa: E402
from director.pool import Candidate, CandidatePool, UnitVariant  # noqa: E402
from director.presets import load_unit_presets  # noqa: E402
from state import ClassroomState  # noqa: E402
from tests.test_variant_e2e import (  # noqa: E402
    Classroom,
    make_director,
    make_variant_script,
    seed_confusion,
)

#: 方案 §12 阶段 2 验收① 的门槛。写死在这里而不是从 config 读：
#: 它是一条**验收口径**，不是可调参数 —— 可调的是 Director 的宽限（grace_ms）。
J_P50_BUDGET_MS = 300.0


# ===========================================================================
# 小工具：读事件
# ===========================================================================

def events(classroom: Classroom, kind: str) -> list[dict]:
    return [e for e in classroom.metrics.events if e["kind"] == kind]


def one_per_unit(events_: list[dict], unit_id: str) -> dict:
    got = [e for e in events_ if e.get("unit") == unit_id]
    assert len(got) == 1, f"{unit_id} 上应当恰好有一条事件，实际 {len(got)} 条：{got}"
    return got[0]


# ===========================================================================
# a1) §12 阶段 1 验收④：variant_applied 必须带"切换前"的快照
# ===========================================================================

async def test_variant_applied_carries_probe_and_facts_before():
    """"哪些单元被切换、切换前后 probe_result"—— 前半句早就有了，后半句一直缺。

    没有 probe_before 的后果不是"少一列数据"，而是这条验收无法回答它真正的问题：
    换了讲法之后学生有没有好转。只有切换后的值等于没有基线，两份数放在一起也对比不出来。
    所以这里钉两件事：字段必须在，且值出自**同一个算法**（facts_from_state）。
    """
    script = make_variant_script()
    classroom = Classroom(script, director=make_director())
    seed_confusion(classroom.state, ["S01-U01", "S02-U01", "S02-U02"])
    await classroom.run()

    applied = events(classroom, "variant_applied")
    assert applied, "夹具前提：这一局必须真的发生过切换，否则断言的是空集"

    for ev in applied:
        assert "probe_before" in ev and "facts_before" in ev, \
            f"variant_applied 必须带切换前的快照（§12 阶段 1 验收④）：{ev}"
        assert isinstance(ev["probe_before"], str), f"探针结果只可能是字符串或空串：{ev}"
        assert isinstance(ev["facts_before"], str)
        assert ev["facts_before"], "事实块算不出来要留空串，但正常课上它必须有内容"
        assert len(ev["facts_before"]) <= MAX_PROMPT_CHARS, \
            f"事实块有 100 字硬上界（§6.2），观测也不许把它顶破：{ev['facts_before']}"
        assert "探针" in ev["facts_before"], \
            f"事实块必须是 to_prompt_block 的产物（而不是另写一套）：{ev['facts_before']}"

    # 第一次切换发生在第一页唯一那个单元的**边界上**：被换的是它的下一格 S02-U01，
    # 而这次选路是在 S01-U01 讲完、页尾探针还没开的时候做出的 —— 此前**一次探针都没开过**，
    # 所以 probe_before 只能是空串。这条断言同时在证明"空串"不是缺省值偷懒：
    # 它表示"确实还没有任何探针结论"。
    first = applied[0]
    assert first["unit"] == "S02-U01", f"第一页那个单元的下一格才是 S02-U01：{first['unit']}"
    assert first["decided_at"] == "S01-U01", \
        f"选路必须发生在上一格的边界上（J 是选路者）：{first.get('decided_at')!r}"
    assert first["probe_before"] == "", \
        f"第一页末才有探针，此前不该有任何结论：{first['probe_before']!r}"

    # 之后的切换都发生在第二页，而第一页页尾的探针在无人回应时已经按超时收口。
    later = applied[1:]
    assert later, "夹具前提：必须还有后续页上的切换，否则'切换前有探针'这件事没被证明"
    assert all(ev["probe_before"] == "timeout" for ev in later), \
        f"页尾探针无人回应 → 记 timeout，后续切换的 probe_before 必须读到它：" \
        f"{[(e['unit'], e['probe_before']) for e in later]}"

    print(f"[A-1] {len(applied)} 次切换的切换前快照：" +
          " / ".join(f"{e['unit']} probe_before={e['probe_before']!r}" for e in applied))


# ===========================================================================
# a2) §12 阶段 1 验收④：切换前后必须**成对**可查
# ===========================================================================

async def test_variant_replay_end_pairs_with_applied():
    """一次切换至少要能回答两个问题：切之前学生什么状态、这一遍变体讲完之后什么状态。

    只在切换那一刻记快照，等于只记了问题的前半段。所以边界上还要记一次
    variant_replay_end —— 而且必须是**每一次切换恰好一条**：多了说明同一遍重播被记了两次
    （数字会虚高），少了说明有重播悄悄结束了没人知道（那几次恰恰可能是被学生打断的样本）。
    """
    script = make_variant_script()
    classroom = Classroom(script, director=make_director())
    seed_confusion(classroom.state, ["S01-U01", "S02-U01", "S02-U02", "S02-U03"])
    await classroom.run()

    applied = events(classroom, "variant_applied")
    ended = events(classroom, "variant_replay_end")
    assert applied, "夹具前提：这一局必须真的发生过切换"
    assert len(ended) == len(applied), \
        f"每一次切换都必须恰好有一条重播结束记录：切换 {len(applied)} 次 / 结束 {len(ended)} 条"

    # 成对：按 unit 一一对应，且 label / variant_id 必须指同一个变体（不许串台）
    for ev in applied:
        end = one_per_unit(ended, ev["unit"])
        assert end["label"] == ev["label"], f"{ev['unit']} 的成对事件指向了别的变体：{end}"
        assert end["variant_id"] == ev["variant_id"], f"{ev['unit']} 的成对事件指向了别的变体：{end}"
        assert end["probe_before"] == ev["probe_before"], \
            f"成对事件的切换前快照必须一致（否则两份数对不上）：{ev} vs {end}"
        assert "probe_after" in end and "facts_after" in end, \
            f"variant_replay_end 必须带切换后的快照（§12 阶段 1 验收④）：{end}"
        assert isinstance(end["probe_after"], str)
        assert end["facts_after"], "事实块算不出来要留空串，但正常课上它必须有内容"
        assert len(end["facts_after"]) <= MAX_PROMPT_CHARS
        assert "探针" in end["facts_after"]
        assert end["reason"] in ("done", "yield", "jump"), \
            f"重播的去向必须说清楚（讲完 / 让位 / 跳转）：{end['reason']}"
        assert end["sec"] >= 0.0

    # 第一页那个单元的**下一格**（S02-U01）：切换前还没开过探针（空串），
    # 讲到末尾时第一页页尾的探针已经按超时收口 —— 一前一后正好把
    # "换了讲法之后学生怎么样"的对比做出来。
    first_end = one_per_unit(ended, "S02-U01")
    assert first_end["probe_after"] == "timeout" and first_end["reason"] == "done"

    # 第二页上的重播：切换前读到的是第一页页尾的超时结论，重播结束时读到的还是它
    # （同一页内没有新的探针收口）—— 值相同是**正确**结果，不是"字段没填"：
    # probe_after / facts_after 非空已由上面的断言单独钉住。
    later = [e for e in ended if e["unit"] != "S02-U01"]
    assert later and all(e["probe_after"] == "timeout" for e in later), \
        f"页尾超时之后的重播，切换后必须读到同一条结论：{[(e['unit'], e['probe_after']) for e in later]}"

    print(f"[A-2] 成对记录 {len(ended)} 组：" +
          " / ".join(f"{e['unit']} {e['probe_before']!r}->{e['probe_after']!r}"
                     f"({e['reason']})" for e in ended))


# ===========================================================================
# a3) 重播被学生提问打断：照样记一次"结束"
# ===========================================================================

async def test_replay_end_recorded_when_question_interrupts_replay():
    """只在"正常讲完"时记，删掉的恰好是最该看的样本。

    §9.1 的顺序是学生提问 > 变体选择，所以重播的结尾很可能是"让位去回答问题"而不是
    讲到底。学生在那一段插话，说明那一段正在起作用 —— 这一类样本一条都不能丢。
    用例构造：变体已经落地、正在重播时插话，于是那个问题会一直挂到重播结束的边界。
    """
    script = make_variant_script()
    classroom = Classroom(script, director=make_director())
    site, target = "S02-U01", "S02-U02"   # 卡住的是 S02-U01，被换的是它的下一格
    seed_confusion(classroom.state, [site])
    asked = {"n": 0}

    async def interrupt(c: Classroom, seg):
        # applied(target) == 1 说明变体已经装上、这几句就是变体的重播。
        # 此刻插话不改变相位（LECTURING 只入队），问题会一直留到重播结束的那个边界。
        if (seg.kind == "unit" and seg.unit_id == target
                and c.applied(target) == 1 and asked["n"] == 0):
            asked["n"] += 1
            await c.machine.handle_input("没懂")

    classroom.on_seg = interrupt
    await classroom.run()

    assert asked["n"] == 1, "用例没跑起来：重播途中的插话没有发生"
    applied = one_per_unit(events(classroom, "variant_applied"), target)
    end = one_per_unit(events(classroom, "variant_replay_end"), target)

    assert end["reason"] == "yield", \
        f"被学生提问打断的重播必须记成 yield，而不是 done：{end['reason']!r}"
    assert end["probe_after"] is not None and "facts_after" in end, \
        "被打断的重播同样要带切换后的快照 —— 它并不比讲完的那次少任何信息"
    assert end["probe_before"] == applied["probe_before"]

    # 让位链路真的走完了：回执 → 让位话术 → 重讲/回答（否则上面的 yield 只是记账，课上没发生过）
    kinds = [s.kind for s in classroom.spoken]
    assert "ack" in kinds and "yield" in kinds, f"提问必须走完让位链路（I3）：{kinds}"
    assert "rephrase" in kinds or "answer" in kinds, f"问题必须被真的回应：{kinds}"
    # 记账被消费干净：不许课后还挂着"这个单元正在重播"
    assert classroom.machine._variant_replaying == {}, \
        f"重播记账必须被消费：{classroom.machine._variant_replaying}"
    assert classroom.machine._finished.is_set(), "插话与决策点共存不许把课堂卡住"

    print(f"[A-3] {target} 的重播被插话打断 → reason={end['reason']!r}，"
          f"probe {end['probe_before']!r}->{end['probe_after']!r}，让位链路正常走完")


# ===========================================================================
# a4) 重播被快速翻页打断：也记一次结束，且不留下悬挂记账
# ===========================================================================

async def test_replay_end_recorded_when_page_turn_interrupts_replay():
    """学生直接喊"下一页"时，重播根本走不到单元边界 —— 它被位置跳转打断了。

    这也是"一次重播结束"，而且是最容易漏的一种：没有一个自然的位置去记它。
    漏掉的表现很隐蔽 —— 课后账面上那个单元"正在重播"，实际课堂早就翻篇了。
    """
    script = make_variant_script()
    classroom = Classroom(script, director=make_director())
    site, target = "S02-U01", "S02-U02"
    seed_confusion(classroom.state, [site])
    jumped = {"n": 0}

    async def turn_page(c: Classroom, seg):
        if (seg.kind == "unit" and seg.unit_id == target
                and c.applied(target) == 1 and jumped["n"] == 0):
            jumped["n"] += 1
            await c.machine.handle_input("下一页")

    classroom.on_seg = turn_page
    await classroom.run()

    assert jumped["n"] == 1, "用例没跑起来：重播途中的翻页没有发生"
    end = one_per_unit(events(classroom, "variant_replay_end"), target)

    assert end["reason"] == "jump", f"被翻页打断的重播必须记成 jump：{end['reason']!r}"
    assert end["probe_after"] is not None and "facts_after" in end
    assert classroom.machine._variant_replaying == {}, \
        f"跳转之后不许还留着'正在重播'：{classroom.machine._variant_replaying}"
    assert classroom.machine._finished.is_set()
    assert classroom.state.slide_index == script.slide_count - 1, "课堂照常讲到最后"

    print(f"[A-4] {target} 的重播被翻页打断 → reason={end['reason']!r}，"
          f"课后记账清空、课堂正常收尾")


# ===========================================================================
# b1) §12 阶段 2 验收④：连续 20 条学生输入零死锁
# ===========================================================================

#: 20 条输入，覆盖既有入口：答疑（qkind 各不同）、重讲链（"没懂"类）、
#: 让位（有待答问题时在边界被让位）、快速翻页（快规则直翻，零模型调用）、
#: 插话（答疑/归位途中再开口）。每条都只走既有入口 handle_input —— 不发明新协议。
INPUT_PLAN: list[tuple[str, str]] = [
    ("question", "老师，这个为什么要这样？"),
    ("question", "我还是没懂"),
    ("question", "没听懂"),
    ("question", "等一下，我有个问题"),
    ("command", "下一页"),
    ("question", "这个例子能再讲一遍吗？"),
    ("question", "刚才那段是什么意思？"),
    ("question", "这个知识点会考吗？"),
    ("question", "我还是不明白"),
    ("question", "再讲一遍"),
    ("question", "老师我有个问题"),
    ("question", "为什么是这样？"),
    ("question", "能举个例子吗？"),
    ("command", "上一页"),
    ("question", "没懂"),
    ("question", "这一步是怎么来的？"),
    ("question", "这里我还是不清楚"),
    ("question", "那这个和刚才那个有什么关系？"),
    ("confirm", "明白了"),
    ("confirm", "清楚了"),
]


def _can_send(kind: str, slide: int, slide_count: int) -> bool:
    """这条输入此刻发出去，是否一定走它该走的那个入口。

    两条翻页命令各钉一个安全区间：上一页只在非首页发（首页那条快规则会被
    slide_index > 0 挡掉，落成一条普通提问）；下一页只在第一页发（否则可能把课堂
    推到末页直接收工，后面的输入就永远发不出去）。判断写在这里，而不是"祈祷时序刚好"。
    """
    if kind != "command":
        return True
    if slide <= 0:
        return True              # 首页也能发：下一页 → 第二页，一定不会收工
    return slide < slide_count - 1


async def test_twenty_consecutive_student_inputs_no_deadlock():
    """§12 阶段 2 验收④：连续 20 条学生输入，课堂不许死在这儿。

    验收原文只有五个字"零死锁"，但"没死"有三种样子，都要钉：
      ① 卡死 —— 主循环原地打转/等锁，课堂结束不了；
      ② 异常逃逸 —— 一条输入把循环抛进异常路径（_run 会把课堂静默停掉）；
      ③ 静默丢输入 —— 课还上着，但某一条输入既没进台账也没被回应。
    另外补一条"课后不留尾巴"：悬挂 task 是上面三条最晚才暴露的那个形态
    （进程还在跑，只是再也没人推进课堂）。
    """
    script = make_variant_script()
    classroom = Classroom(script, director=make_director())
    seed_confusion(classroom.state, ["S01-U01", "S02-U01", "S02-U02"])

    # 通知里带着"快速意图[...]：直接翻到下一页"，这是翻页命令唯一可见的处理证据
    notices: list[tuple[str, str]] = []

    async def record_notify(level, text):
        notices.append((str(level), str(text)))

    classroom.machine.h.notify = record_notify

    queue = list(INPUT_PLAN)
    sent: list[tuple[str, str]] = []

    async def pump_inputs(c: Classroom, seg):
        if c.machine._finished.is_set():
            return
        for i, (kind, text) in enumerate(queue):
            if _can_send(kind, c.state.slide_index, c.script.slide_count):
                queue.pop(i)
                sent.append((kind, text))
                await c.machine.handle_input(text)
                return

    classroom.on_seg = pump_inputs

    try:
        await classroom.run(timeout=60.0)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"§12 阶段 2 验收④：连续输入把课堂卡死了。已发 {len(sent)} 条，"
            f"phase={classroom.state.phase.value} slide={classroom.state.slide_index} "
            f"pending={len(classroom.state.pending_questions)}")

    # ① 不卡死：真的收尾了，而且不是靠异常/兜底停下
    assert len(sent) == len(INPUT_PLAN), f"20 条必须全部发出去：{len(sent)}/{len(INPUT_PLAN)}"
    assert classroom.machine._finished.is_set(), "课堂必须自己收尾"
    assert events(classroom, "class_end"), "收尾必须走 class_end 这条正常出口"
    assert not events(classroom, "fatal"), f"不许有循环异常：{events(classroom, 'fatal')}"
    assert not events(classroom, "stuck"), \
        f"不许靠'状态未继续变化'兜底停下：{events(classroom, 'stuck')}"
    assert [t for lv, t in notices if lv == "error"] == [], \
        f"学生不该看到任何 error 级提示：{[t for lv, t in notices if lv == 'error']}"
    assert classroom.state.slide_index == script.slide_count - 1, "课堂必须讲到最后一页"

    # 每条输入都进了台账，一条不少、一条不重（这是"没被静默丢掉"的唯一硬证据）
    student_lines = [u.text for u in classroom.state.transcript if u.speaker == "student"]
    assert sorted(student_lines) == sorted(t for _, t in INPUT_PLAN), \
        f"20 条输入必须逐条进台账：{student_lines}"
    assert len(events(classroom, "input")) == len(INPUT_PLAN), \
        "每条输入都必须被 handle_input 记一次 input 事件（入口没被绕过）"

    # 每条输入都**被处理**：按入口分别要证据，而不是"进了台账就算数"
    qa = " ".join(entry.get("q", "") for entry in classroom.state.qa_history)
    helped = " ".join(str(e.get("raw", "")) for e in events(classroom, "yield")) + " " + qa
    notice_text = " ".join(t for _, t in notices)
    for kind, text in INPUT_PLAN:
        if kind == "question":
            assert text in helped, \
                f"提问必须真的进过让位链路（答疑/重讲都算）：{text!r} 只在台账里出现过"
        elif kind == "command":
            assert "直接翻到下一页" in notice_text and "直接返回上一页" in notice_text, \
                "翻页命令必须命中快规则（零模型调用），而不是被当成提问排队"
        else:
            assert "收到，清楚就好" in notice_text, \
                f"确认类输入必须被认成'听懂了'（记强信号），而不是排进答疑：{text!r}"
    assert classroom.state.pending_questions == [], \
        f"课都上完了还有没人理的提问（静默丢弃）：{classroom.state.pending_questions}"

    # ② 异常不逃逸：事件循环层面也不许有一句"没人处理的异常"
    loop = asyncio.get_running_loop()
    loop_errors: list[object] = []

    def on_loop_error(_loop, context):
        loop_errors.append(context.get("exception") or context.get("message"))

    loop.set_exception_handler(on_loop_error)
    try:
        for _ in range(3):
            await asyncio.sleep(0)

        # ③ 课后无悬挂 task（与 test_variant_e2e 的 j 同一口径：给一个 tick 自己收干净）
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if others:
            await asyncio.wait(others, timeout=0.5)
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert others == [], f"课堂结束后不许有悬挂 task：{others}"
    finally:
        loop.set_exception_handler(None)
    assert loop_errors == [], f"事件循环里不许有没人处理的异常：{loop_errors}"

    # 附带：缺口 1 的记账在 20 条输入轮番打断之下也不许残留
    assert classroom.machine._variant_replaying == {}, \
        f"重播记账必须清空：{classroom.machine._variant_replaying}"

    print(f"[B-1] 20 条输入全部处理：让位 {len(events(classroom, 'yield'))} 次 / "
          f"qa_history {len(classroom.state.qa_history)} 条 / "
          f"切换 {len(events(classroom, 'variant_applied'))} 次 / 悬挂 task 0 / "
          f"Director {classroom.machine.director.stats}")


# ===========================================================================
# c1) §12 阶段 2 验收①：J 的 p50 延迟 < 300 ms
# ===========================================================================

def _p90(samples: list[float]) -> float:
    """p90：升序取样（样本少时不插值，直接取最近的实测点）。

    与 metrics.summary 的口径不同是有意的：那里报的是课上几十个点的中位数，
    这里报的是一条延迟分布的上沿，插值会造出一个从未发生过的数。
    """
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]


async def test_j_decision_path_p50_under_300ms():
    """§12 阶段 2 验收①的门槛，钉在测试里而不是钉在文档里。

    为什么测的是"事实块 → 池子筛选 → 选中"这一整段，而不是 decide() 那一行：
    方案说的 J 的延迟是学生在边界上多等的那一段，decide() 只是其中最后一行。
    facts_from_state（遍历 probe_log / q_rounds）与 candidates_for（全池筛选 + 排序 +
    截断）才是随课堂成长的两次开销，漏掉它们，测出来的数是假快。

    本轮是 RuleDecider（纯 Python，不调模型），实测必然是微秒级 —— 这个用例的价值不是
    "证明它现在很快"，而是**把 300 ms 这条门槛钉住**：将来换成 LlmDecider，同一条用例
    不改一个字就直接生效；它一旦开始失败，就说明"宽限 300 ms 根本盖不住一次模型往返"，
    那正是 §13 第 7 条要面对的问题。
    """
    script = make_variant_script()
    state = ClassroomState(course_id=script.course_id)
    unit = script.unit("S02-U02")
    seed_confusion(state, [unit.unit_id])

    pool = CandidatePool()
    load_unit_presets(pool, unit, seq=state.seq, now=time.monotonic())
    # 把池子填到硬上限（§6.2 的 5 条）：不填满，candidates_for 的"全池筛选 + 排序 + 截断"
    # 就是一次两三条的散步，测出来的数是假快。这三条挂一个永不成立的条件，
    # 于是它们只贡献筛选成本、不参与选中 —— J 的输入上界照样是 5 条。
    for i in range(3):
        pool.put(Candidate(
            candidate_id=f"c-bench-{i}", target_unit="",
            variant=UnitVariant(variant_id=f"b{i}", label=f"基准候选{i}", text="基准候选文本"),
            when={"q_rounds": {">=": 99}}, source="agent_c",
            created_at=time.monotonic() + i, created_at_unit="", expires_at_seq=10 ** 9,
        ))
    decider = RuleDecider()

    # 夹具前提：池子里必须真的有能命中的候选，否则测的是"没有候选"那条更快也更空的路径
    facts0 = facts_from_state(state, unit)
    candidates0 = pool.candidates_for(facts0.unit_id, facts0)
    assert candidates0, "夹具前提：池子里必须有命中的候选，否则这条基准没测到筛选成本"
    assert pool.alive_count() == CandidatePool.CAP,         f"夹具前提：池子必须填到硬上限，否则筛选取的是全池却没测到全池的量：{pool.alive_count()}"

    samples: list[float] = []
    chosen = None
    for _ in range(5):                      # 预热：别让第一次的 import/缓存冷启动进样本
        warm = facts_from_state(state, unit)
        await decider.decide(warm, pool.candidates_for(warm.unit_id, warm))
    for _ in range(300):
        t0 = time.perf_counter()
        facts = facts_from_state(state, unit)
        candidates = pool.candidates_for(facts.unit_id, facts)
        chosen = await decider.decide(facts, candidates)
        samples.append((time.perf_counter() - t0) * 1000.0)

    p50 = statistics.median(samples)
    p90 = _p90(samples)
    print(f"[C-1] J 决策路径 n={len(samples)} p50={p50:.4f}ms p90={p90:.4f}ms "
          f"max={max(samples):.4f}ms 门槛={J_P50_BUDGET_MS:.0f}ms "
          f"（池内 {CandidatePool.CAP} 条 / 命中 {len(candidates0)} 条 / 事实块 {MAX_PROMPT_CHARS} 字上界 / "
          f"decider={type(decider).__name__}）")

    assert chosen, "决策必须真的选中一个候选（否则测的是选不中的分支）"
    assert p50 < J_P50_BUDGET_MS, \
        f"§12 阶段 2 验收①：J 的 p50 必须 < {J_P50_BUDGET_MS:.0f}ms，实测 {p50:.2f}ms"


# ===========================================================================
# d1) §12 阶段 2 验收②：yield_latency 不劣于基线 10%（离线代理口径）
# ===========================================================================

class _WaitSpy:
    """替掉 director.decider 里的 asyncio，量边界上到底让学生多等了多少毫秒。

    为什么要动到模块属性：apply_at_boundary 的宽限等待是 asyncio.wait(task, timeout)，
    而 stats["timeout"] 只在**等满 300 ms 还没回来**时才 +1 —— "等到了、但等了 80 ms"
    这条路上它一个字都不会说，而正是这 80 ms 才是学生感知到的那部分。

    为什么记**耗时**而不是记次数：进入 asyncio.wait 并不等于等过。预取 task 要到下一个
    事件循环 tick 才会跑第一次，而单句单元的"第一句 → 边界"之间状态机可能一个 await
    都没做，于是边界上必然补一次 wait —— 这一次 wait 的实际耗时是一个 tick（微秒级）。
    把"进了几次"当成"多等了多久"，会把微秒级的调度让路记成一笔课堂停顿，那不是验收。
    """

    def __init__(self, real) -> None:
        self._real = real
        self.waits: list[float] = []    # 每次"真进宽限"实际花掉的毫秒

    def __getattr__(self, name):        # create_task / CancelledError / ... 原样透传
        return getattr(self._real, name)

    async def wait(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return await self._real.wait(*args, **kwargs)
        finally:
            self.waits.append((time.perf_counter() - t0) * 1000.0)


async def _run_with_questions(script, *, director, asked_units):
    """同一份"学生脚本"跑两遍（关/开决策层），拿到可比的 yield 延迟样本。

    为什么问句要落在固定位置上（某单元的**第一句**播出后）：两次跑的段落序列不同
    （开了决策层会多出变体重播），只有"某单元的第一个单元句"这种位置键在两边是同一个。
    """
    classroom = Classroom(script, director=director)
    seed_confusion(classroom.state, [u.unit_id for u in script.units])
    pending = list(asked_units)

    async def ask_once(c: Classroom, seg):
        if seg.kind == "unit" and seg.unit_id in pending:
            pending.remove(seg.unit_id)
            await c.machine.handle_input("没懂")

    classroom.on_seg = ask_once
    await classroom.run()
    return classroom


async def test_yield_latency_offline_proxy_no_extra_wait():
    """§12 阶段 2 验收②的离线口径：为什么这里不直接断言"≤10%"。

    yield_latency 量的是"学生按下发送 → 老师开口作答"，它由**替身决定**：
    离线夹具里 FakeLLM 零延迟、播放零耗时，这个数只剩调度噪声（实测零点几毫秒，
    正负都可能翻倍）。拿一个噪声当基线去卡 10%，红的绿的都是运气 —— 那不是在验收，
    是在给未来的自己留一条随机失败的用例。

    所以换成三条**能被证明**的代理指标，它们合起来说清了同一件事
    （"决策点没往课堂里塞任何额外等待"）：
      ① 边界上没有任何一次宽限等待真正走过时间（补等 300 ms 才是学生感知到的停顿）；
      ② Director.stats["timeout"] == 0（连"等满超时"都没有发生过）；
      ③ 单次边界决策自身的耗时（含事实派生 + 池子筛选）有上界。
    实测的 yield 延迟两遍都打印出来，只报告不断言 —— 结论写在报告里。
    """
    asked_units = ["S02-U02", "S02-U04"]

    plain = await _run_with_questions(make_variant_script(), director=None,
                                     asked_units=asked_units)

    director = make_director()
    spans: list[float] = []
    original = director.apply_at_boundary

    async def timed(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return await original(*args, **kwargs)
        finally:
            spans.append((time.perf_counter() - t0) * 1000.0)

    director.apply_at_boundary = timed
    spy = _WaitSpy(decider_mod.asyncio)
    decider_mod.asyncio = spy
    try:
        wired = await _run_with_questions(make_variant_script(), director=director,
                                          asked_units=asked_units)
    finally:
        decider_mod.asyncio = spy._real

    stats = director.stats
    assert stats["applied"] >= 1, \
        f"这一局必须真的切过，否则代理指标证明的是一个空转的决策点：{stats}"
    assert spans, "边界决策点必须真的被调用过"

    # ① 宽限里没有等待：即便边界上补过 wait，它也只是让出一次事件循环（微秒级）。
    #    真让学生等的那一刻，是"task 还没回来、宽限真的开始走时间"。
    slow = [ms for ms in spy.waits if ms > 5.0]
    assert slow == [], \
        f"边界上不许出现真正让学生等的宽限等待：{slow}ms（全部 {spy.waits}）"
    # ② 连"等满超时"都没有：与 test_variant_e2e 的 f) 同一口径
    assert stats["timeout"] == 0, f"RuleDecider 下不许出现边界超时：{stats}"
    # ③ 决策点自身耗时上界：这是 §12 阶段 2 验收③（无可感知停顿）的量化形态
    assert max(spans) < 5.0, f"单次边界决策不许有可感知耗时：max={max(spans):.3f}ms"

    # 实测（只报告）：同一个学生脚本，开/关决策层各一遍
    base = plain.metrics.yield_ms
    now_ = wired.metrics.yield_ms
    assert base and now_, f"两遍都必须真的有让位发生（否则没有可比的东西）：{base} / {now_}"
    base_p50 = statistics.median(base)
    now_p50 = statistics.median(now_)
    print(f"[D-1] 决策点额外等待 0 次（进宽限 {len(spy.waits)} 次 / 合计 "
          f"{sum(spy.waits):.3f}ms / 最长 {max(spy.waits or [0.0]):.3f}ms）/ stats {stats} / 边界耗时 "
          f"p50={statistics.median(spans):.3f}ms max={max(spans):.3f}ms；"
          f"yield_latency 关={base_p50:.4f}ms 开={now_p50:.4f}ms "
          f"（原始样本 关={[round(x, 4) for x in base]} 开={[round(x, 4) for x in now_]} —— "
          f"FakeLLM 下它们就是零，比值没有意义，只报告不断言）")
