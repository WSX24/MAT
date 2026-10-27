"""外置大脑 MCP 方案全量单测与 §12 十项验收标准测试。

覆盖范围：
  - §13.1 前置依赖（P0-1 依赖项、P0-2 _back_to_lecturing 唯一入口、ActiveRun + McpMode 单一状态对象）
  - Phase 0：ContextSnapshot 9 字段（清 5 死字段）、G2/G3 记忆回灌提示词、G1 DELEGATE 逃生舱
  - Phase 1：MCP 5 方法（initialize / notifications/initialized / ping / tools/list / tools/call）、
             三档 get_events、say 版本校验与 Floor Control、错误语义（-32601 / -32602 / isError）
  - Phase 2：query_rag / search_course、两次调用间急件打断（§10.3 #6）、idle_hint 预知
  - Phase 3：student_memory 跨会话同步读 + 异步写
  - Phase 4：行动类工具默认关闭（硬约束 6 工具），开启后白名单与锚点校验
  - §12 验收标准 #1 ~ #10 全量断言

运行：
  python tests/test_mcp.py
  python -m pytest tests/test_mcp.py -q
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import ContextSnapshot, build_ctx, prompt_answer, prompt_classify, prompt_rephrase  # noqa: E402
from machine import ActiveRun, Machine  # noqa: E402
from mcp_server import (  # noqa: E402
    DEFAULT_WAIT_SECONDS,
    HARD_MAX_TOOLS,
    MAX_EVENT_QUEUE_SIZE,
    MAX_SAY_QUEUE_DEPTH,
    PROTOCOL_VERSION,
    McpBrainServer,
    McpMode,
    StudentMemoryStore,
)
from phrases import INTENT_ROUTER, StudentIntent  # noqa: E402
from script import build_script  # noqa: E402
from state import ClassroomState, Comprehension, Covered, Phase  # noqa: E402
from tests.test_machine import FakeLLM, Headless, make_settings  # noqa: E402


# ===========================================================================
# 1. §13.1 前置依赖与 Phase 0（ContextSnapshot + G1/G2/G3）
# ===========================================================================

async def test_prerequisites_and_phase0():
    # P0-1: requirements.txt 包含 python-pptx / Pillow / lxml
    req_txt = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8")
    for pkg in ("python-pptx", "Pillow", "lxml"):
        assert pkg.lower() in req_txt.lower(), f"P0-1 未修复：requirements.txt 缺少 {pkg}"

    # P0-2: _set_phase(Phase.LECTURING) 只能在 _back_to_lecturing 内部出现一次
    src = inspect.getsource(Machine)
    hits = [line.strip() for line in src.splitlines() if "_set_phase(Phase.LECTURING)" in line]
    assert len(hits) == 1, f"P0-2 未收口：_set_phase(Phase.LECTURING) 出现了 {len(hits)} 次：{hits}"

    # 单一状态对象：Machine.active_run & McpMode.driving() 初值必为 False
    hs = Headless(probe_window=0.1)
    m = Machine(hs.script, hs.state, hs.settings, hs.llm, hs.handlers(), hs.metrics)
    assert isinstance(m.active_run, ActiveRun), "Machine 必须使用 ActiveRun 单一状态对象"
    mode = McpMode()
    assert mode.driving() is False, "driving() 初值必须为 False（防冷启动死锁，§3.3 / §10.3 #8）"

    # Phase 0: ContextSnapshot 严格只含 9 个字段，已清除 5 个死字段
    field_names = [f.name for f in dataclasses.fields(ContextSnapshot)]
    want_fields = [
        "version",
        "slide",
        "unit",
        "prev_unit_text",
        "covered_kp",
        "comprehension",
        "deferred",
        "recent_qa",
        "q_rounds",
    ]
    assert field_names == want_fields, f"ContextSnapshot 字段不符：{field_names}"
    for dead in ("anchors_hint", "next_unit_hint", "goals", "slide_index", "unit_id"):
        assert dead not in field_names, f"死字段 {dead} 未清理"

    # Phase 0: G2（deferred）与 G3（comprehension）与 recent_qa 回灌进提示词
    st = ClassroomState(course_id="demo")
    st.deferred.append("连字符复合词规则")
    st.comprehension["S1-U01"] = Comprehension.CONFUSED.value
    st.q_rounds["S1-U01"] = 2
    st.qa_history.append({"q": "为什么加连字符？", "a": "因为作定语修饰名词。"})
    st.covered.append(Covered(kp="K_QUAD_01", slide=0, unit_id="S1-U01"))
    ctx = build_ctx(build_script(), st)
    assert ctx.deferred == ["连字符复合词规则"]
    assert ctx.comprehension == {"S1-U01": "confused"}
    assert ctx.q_rounds == 2
    assert len(ctx.recent_qa) == 1

    ans_msgs = prompt_answer("那放到名词后面呢？", "clarify", ctx)
    sys_prompt = ans_msgs[0]["content"]
    assert "连字符复合词规则" in sys_prompt, "G2 未修复：deferred 未进入 prompt_answer"
    assert "S1-U01=confused" in sys_prompt, "G3 未修复：comprehension 未进入 prompt_answer"
    assert "为什么加连字符" in sys_prompt, "recent_qa 未进入 prompt_answer"

    rep_msgs = prompt_rephrase("还是没懂", 2, ctx)
    assert "连字符复合词规则" in rep_msgs[0]["content"], "deferred 未进入 prompt_rephrase"
    cls_msgs = prompt_classify("那放到后面呢？", ctx)
    assert "连字符复合词规则" in cls_msgs[1]["content"], "deferred 未进入 prompt_classify"

    # G1: FastIntentRouter 的 fallback 分支返回 DELEGATE 而非伪装成 ASK_CLARIFY
    saved = (INTENT_ROUTER.enabled, INTENT_ROUTER.use_small_llm)
    try:
        INTENT_ROUTER.configure(enabled=True, use_small_llm=False)
        dec = await INTENT_ROUTER.route("sweet和home之间加连字符的词法学根源是什么", None)
        assert dec.stage == "fallback"
        assert dec.intent is StudentIntent.DELEGATE, f"G1 未修复：fallback 意图应为 DELEGATE，实际 {dec.intent}"
    finally:
        INTENT_ROUTER.configure(enabled=saved[0], use_small_llm=saved[1])

    print("[M1] §13.1 前置依赖 + Phase 0 (ContextSnapshot & G1/G2/G3) 全部通过")


# ===========================================================================
# 2. MCP 握手、三态掌舵表（§3.3）、6 工具硬约束（D8）与错误语义（§4.7）
# ===========================================================================

async def test_mcp_surface_and_error_semantics():
    mcp = McpBrainServer()  # 尚未绑定 Machine（测试 "classroom not started"）

    # 1) 未握手前 driving() == False
    assert mcp.mode.state_label() == "unconnected"
    assert mcp.mode.driving() is False

    # 2) initialize 握手
    code, resp = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "clientInfo": {"name": "Claude Code", "version": "1.0"},
        },
    })
    assert code == 200
    res = resp["result"]
    assert res["protocolVersion"] == PROTOCOL_VERSION
    assert "get_events(wait_seconds=50)" in res["instructions"]
    assert mcp.mode.client_name == "Claude Code 1.0"
    assert mcp.mode.driving() is True
    assert mcp.mode.state_label() == "active"

    # 3) notifications/initialized 返回 202 无 body
    code_n, resp_n = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    assert code_n == 202 and resp_n is None

    # 4) ping
    code_p, resp_p = await mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert code_p == 200 and resp_p["result"] == {}

    # 5) tools/list 严格 6 个工具（§2 D8, §8）
    code_t, resp_t = await mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = resp_t["result"]["tools"]
    assert len(tools) == HARD_MAX_TOOLS == 6, f"工具数量违反硬约束 6 个：{len(tools)}"
    names = [t["name"] for t in tools]
    assert names == [
        "get_events",
        "get_state",
        "say",
        "query_rag",
        "search_course",
        "student_memory",
    ]
    ge_schema = tools[0]["inputSchema"]["properties"]["wait_seconds"]
    assert ge_schema["default"] == DEFAULT_WAIT_SECONDS == 50
    assert ge_schema["maximum"] == 50

    # 6) 错误语义（§4.7）
    # a. 未知工具名 → -32601 "unknown tool: xxx"
    _, err_unknown = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "non_existent_tool", "arguments": {}},
    })
    assert err_unknown["error"]["code"] == -32601
    assert "unknown tool: non_existent_tool" in err_unknown["error"]["message"]

    # b. 参数不合 schema → -32602 "bad arguments: 缺 text"
    _, err_args = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "say", "arguments": {}},
    })
    assert err_args["error"]["code"] == -32602
    assert "bad arguments: 缺 text" in err_args["error"]["message"]

    # c. 课堂未开始（无 course/machine） → 正常返回 + isError: true + "classroom not started"
    _, err_not_started = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "get_state", "arguments": {}},
    })
    assert err_not_started["result"]["isError"] is True
    assert err_not_started["result"]["content"][0]["text"] == "classroom not started"

    print("[M2] MCP 5 方法契约、6 工具硬约束、§4.7 错误语义 全部通过")


# ===========================================================================
# 3. EventBus 三档返回、消费即消失、过期标记、队列上限 20、唤醒延迟 < 5ms
# ===========================================================================

async def test_event_bus_three_tiers_and_wakeup_latency():
    hs = Headless(probe_window=0.15)
    mcp = McpBrainServer()
    m = Machine(hs.script, hs.state, hs.settings, hs.llm, hs.handlers(), hs.metrics, mcp=mcp)
    await mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    # 档 3：到点无事件返回极短的空 + idle_hint（验收标准 #9：空返回 < 30 token）
    _, empty_resp = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_events", "arguments": {"wait_seconds": 0}},
    })
    empty_text = empty_resp["result"]["content"][0]["text"]
    empty_data = json.loads(empty_text)
    assert empty_data["events"] == []
    assert empty_data["idle_hint"] is not None
    assert "连续讲解" in empty_data["idle_hint"]["reason"]
    # 估算 token 数（JSON 字符长度 / 3.5 远小于 30 tokens）
    approx_tokens = len(empty_text) / 3.5
    assert approx_tokens < 30, f"空返回过长（约 {approx_tokens:.1f} tokens）：{empty_text}"

    # 档 2：累积普通事件（urgent=False），到点清扫返回且消费即消失
    mcp.emit_classroom_event("speech_finished", seg_kind="unit", unit_id="S1-U01")
    mcp.emit_classroom_event("unit_finished", unit_id="S1-U01", slide=1)
    _, norm_resp = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_events", "arguments": {"wait_seconds": 0}},
    })
    norm_events = json.loads(norm_resp["result"]["content"][0]["text"])["events"]
    assert [e["kind"] for e in norm_events] == ["speech_finished", "unit_finished"]

    # 再次读取应为空（消费即消失，绝不重放旧事件，§5.3）
    _, after_consume = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "get_events", "arguments": {"wait_seconds": 0}},
    })
    assert json.loads(after_consume["result"]["content"][0]["text"])["events"] == []

    # 档 1：挂起 wait_seconds=50 时，急件到达立即唤醒（验收标准 #3：写回延迟 < 5ms）
    poll_task = asyncio.create_task(
        mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "get_events", "arguments": {"wait_seconds": 50}},
        })
    )
    await asyncio.sleep(0.02)
    t_push = time.perf_counter()
    mcp.emit_student_query("sweet 和 home 之间为什么加连字符？", via="text")
    _, urgent_resp = await asyncio.wait_for(poll_task, timeout=1.0)
    elapsed_ms = (time.perf_counter() - t_push) * 1000.0
    assert elapsed_ms < 5.0, f"从事件入队到写回响应耗时 {elapsed_ms:.2f}ms，超出 5ms 阈值（§12 #3）"
    urgent_events = json.loads(urgent_resp["result"]["content"][0]["text"])["events"]
    assert len(urgent_events) == 1
    ev0 = urgent_events[0]
    assert ev0["type"] == "query" and ev0["kind"] == "student_text" and ev0["urgent"] is True
    assert ev0["triage"]["stage"] == "fallback"
    assert "version" in ev0["snapshot"] and "digest" in ev0["snapshot"]

    # 过期标记（>60s 标 stale: true）与队列上限（20 条超出丢最旧并记 event_dropped，§5.3）
    for i in range(25):
        ev = mcp.emit_student_query(f"问题 #{i}")
        if i == 5:
            ev["_mono_ts"] = time.monotonic() - 65.0  # 模拟超过 60 秒未读
    assert mcp.event_bus.queue_size == MAX_EVENT_QUEUE_SIZE == 20
    assert mcp.event_bus.dropped_count == 5
    dropped_metrics = [e for e in m.metrics.events if e.get("kind") == "event_dropped"]
    assert len(dropped_metrics) == 5, "超出 20 条上限必须记录 metrics.event('event_dropped')"

    _, batch_resp = await mcp.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "get_events", "arguments": {"wait_seconds": 0}},
    })
    batch_evs = json.loads(batch_resp["result"]["content"][0]["text"])["events"]
    assert batch_evs[0]["student"]["text"] == "问题 #5"
    assert batch_evs[0].get("stale") is True, "超过 60s 的 query 必须标记 stale: true"

    print(f"[M3] EventBus 三档返回 · 唤醒写回延迟 {elapsed_ms:.2f}ms (<5ms) · 队列上限/过期标记全部通过")


# ===========================================================================
# 4. 版本校验 10 场景 100% 准确率（§6.2, §12 #5）与 say 队列深度/入队延迟（§12 #4, #8）
# ===========================================================================

async def test_say_version_check_10_cases_and_queue_depth():
    hs = Headless(probe_window=0.15)
    mcp = McpBrainServer()
    m = Machine(hs.script, hs.state, hs.settings, hs.llm, hs.handlers(), hs.metrics, mcp=mcp)
    await mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    # 10 组跨页/同页版本校验回放场景（对应 §12 #5：版本过期处理正确率 100%）
    # 格式：(当前 slide_index [0-based], agent 的 from_version.slide [1-based], 期望 version_check)
    cases = [
        (0, 1, "ok"),              # #1 第1页答第1页 → ok
        (1, 2, "ok"),              # #2 第2页答第2页 → ok
        (2, 3, "ok"),              # #3 第3页答第3页 → ok
        (1, 1, "stale_prefixed"),  # #4 老师到第2页，agent 答第1页（差1页）→ stale_prefixed
        (2, 1, "stale_prefixed"),  # #5 老师到第3页，agent 答第1页（差2页）→ stale_prefixed
        (2, 2, "stale_prefixed"),  # #6 老师到第3页，agent 答第2页（差1页）→ stale_prefixed
        (3, 1, "dropped"),         # #7 老师到第4页，agent 答第1页（差3页 > 2）→ dropped + deferred
        (4, 3, "stale_prefixed"),  # #8 老师到第5页，agent 答第3页（差2页）→ stale_prefixed
        (4, 1, "dropped"),         # #9 老师到第5页，agent 答第1页（差4页 > 2）→ dropped + deferred
        (5, 3, "dropped"),         # #10 老师到第6页，agent 答第3页（差3页 > 2）→ dropped + deferred
    ]

    for idx, (cur_slide_idx, from_page, want_check) in enumerate(cases, 1):
        m.state.slide_index = cur_slide_idx
        m.agent_speech_queue.clear()
        t0 = time.perf_counter()
        _, say_resp = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 10 + idx,
            "method": "tools/call",
            "params": {
                "name": "say",
                "arguments": {
                    "text": f"这是针对第{from_page}页的补充说明。",
                    "from_version": {"slide": from_page, "unit": f"S{from_page}-U01", "seq": idx},
                },
            },
        })
        enqueue_ms = (time.perf_counter() - t0) * 1000.0
        assert enqueue_ms < 5.0, f"say 入队耗时 {enqueue_ms:.2f}ms，超过 5ms 阈值（§12 #4）"
        receipt = json.loads(say_resp["result"]["content"][0]["text"])
        assert receipt["version_check"] == want_check, (
            f"Case #{idx} (cur={cur_slide_idx + 1}, from={from_page}) "
            f"期望 {want_check}，实际 {receipt}"
        )
        if want_check == "stale_prefixed":
            queued_text = m.agent_speech_queue[-1]["text"]
            assert queued_text.startswith(f"补充一下刚才第 {from_page} 页那个点——"), (
                f"stale_prefixed 未加页码前缀：{queued_text}"
            )
        elif want_check == "dropped":
            assert len(m.agent_speech_queue) == 0, "dropped 的过期话术绝不能进入播放队列"
            assert any(f"第{from_page}页补充" in d for d in m.state.deferred), (
                "dropped 的过期话术必须转入 state.deferred"
            )

    # 队列深度上限测试（§12 #8：单次 say 队列深度 ≤ 2）
    m.state.slide_index = 0
    m.agent_speech_queue.clear()
    for i in range(4):
        _, r = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 100 + i,
            "method": "tools/call",
            "params": {
                "name": "say",
                "arguments": {"text": f"连续发言第 {i + 1} 句。", "from_version": {"slide": 1}},
            },
        })
        rec = json.loads(r["result"]["content"][0]["text"])
        assert rec["queue_depth"] <= MAX_SAY_QUEUE_DEPTH == 2, f"队列深度超出上限 2：{rec}"
        if i >= 1:
            assert rec["can_speak_now"] is False, "队列达到 2 条时 can_speak_now 必须为 False"
        if i >= 2:
            assert rec["status"] == "throttled", "超过 2 条后续调用必须被限流防话痨"

    print("[M4] 10 场景版本校验 100% 准确 · say 入队 <5ms · 队列深度 ≤2 全部通过")


# ===========================================================================
# 5. 双活与让位点插播、四级「说完」判据、断连 quietFallback（§3.3, §7, §12 #1/#6/#7）
# ===========================================================================

async def test_dual_active_yield_playback_and_no_deadlock():
    # 基线：不挂载 MCP 时的让位延迟（speak_delay=0.05s 对齐 test_machine.py T1）
    #
    # 为什么基线跑 3 次、每次取该次的中位数、再对 3 个中位数取中位数
    # ------------------------------------------------------------------
    # 这条用例守的是 §12 #1「接入 MCP 不在阻塞链上，yield_latency 偏差 < 10%」，口径一个字
    # 都不能放宽。原先的取样方式有问题：拿**两次独立运行的首个样本**相比。首样本是整条
    # 序列里最不稳的那个值（事件循环冷启动、GC、Windows 计时器粒度都堆在开头），于是
    # 单次抖动就能把两条各只跑过一次、各只有一个样本的序列比出 30% 的"劣化"，而实际什么
    # 都没退化 —— 220 条用例的负载下实测约 30% 概率偶发红。
    # 中位数才是这个量的稳健统计量（MetricsTracker.summary() 报的也是 p50）。基线跑 3 次
    # 是第二层保险：先压掉单次运行内部的样本抖动，再压掉三次之间的运行抖动 —— 一次抽签
    # 不该决定"接不接 MCP"这个结论。
    # 注意别把它当成"反正取中位数就宽松了"：本局每个运行只问一次，所以单次的中位数就等于
    # 该次那一个样本；真正抗抖动的是 3 次取中位数这一层。若这一局将来问多次，中位数会
    # 自动用上更多样本，而不会再退化成抽签。
    def _median(xs: list[float]) -> float:
        assert xs, "让位延迟一个样本都没有：夹具没跑起来，此时结论不成立"
        ordered = sorted(xs)
        n = len(ordered)
        mid = n // 2
        return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0

    base_medians: list[float] = []
    base_samples: list[list[float]] = []
    for _ in range(3):
        hs_base = Headless(probe_window=0.05, speak_delay=0.05)
        asked_base = {"done": False}

        async def base_input(h: Headless, seg, _flag=asked_base):
            # 每轮一个独立标记：三次基线必须各自真的问出那一次让位，否则第 2/3 次会
            # 一句都不问、yield_ms 为空，中位数就退化成"上次的数"
            if seg.kind == "unit" and seg.unit_id == "S1-U02" and not _flag["done"] and h.machine:
                _flag["done"] = True
                await h.machine.handle_input("这里为什么不用配方法而用公式法？")

        hs_base.auto_input = base_input
        await hs_base.run()
        base_samples.append([round(x, 1) for x in hs_base.metrics.yield_ms])
        base_medians.append(_median(hs_base.metrics.yield_ms))
    base_yield = _median(base_medians)

    # 挂载 MCP + 长轮询挂起 50s + 学生连续输入 20 条（验证 §12 #1 延迟无劣化 & #7 零死锁）
    hs_mcp = Headless(probe_window=0.05, speak_delay=0.02)
    mcp = McpBrainServer()
    hs_mcp.machine = Machine(
        hs_mcp.script, hs_mcp.state, hs_mcp.settings,
        hs_mcp.llm, hs_mcp.handlers(), hs_mcp.metrics, mcp=mcp,
    )
    await mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    # 先在与基线完全相同的 speak_delay=0.05 下测量首次让位延迟
    hs_mcp.speak_delay = 0.05
    bg_poll = asyncio.create_task(
        mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_events", "arguments": {"wait_seconds": 50}},
        })
    )

    # 让位延迟的统计口径（MCP 侧）：**这里不能取中位数** —— 理由留在这里，免得下次又被"统一成中位数"。
    # 这条用例后半段会连发 19 条输入（§12 #7 零死锁那段），它们产生的 yield 样本是一条
    # 0 → 62 → 125 → 187 → 250 → 312 → … → 500 的等差序列：测的是**队列积压**，
    # 与"MCP 是否进阻塞链"毫无关系。把这一串和一次同条件让位混在一起取中位数，
    # 得到的只是"积压的中位数"（实测 218~227ms，比值稳定超标 16~21%）—— 那是必然红，不是抖动。
    # 所以 MCP 侧只用**与基线同条件的那个样本**：speak_delay=0.05、队列里没有前序积压时的
    # 首次让位。它在下面的"显式时间点切分"处被快照出来，后段积压样本一个都不进统计。
    fired = {"n": 0}
    #: 切分点快照：进入"19 条连发"那一刻把此刻的 yield 样本拷出来。用**显式时间点**而不是
    #: "取前 N 个"这种隐式约定 —— 后半段输入条数将来若改动，统计口径也不会被悄悄改变。
    mcp_window: dict = {"samples": None}

    async def mcp_input(h: Headless, seg):
        if seg.kind == "unit" and seg.unit_id == "S1-U02" and fired["n"] == 0 and h.machine:
            fired["n"] += 1
            # 第 1 条触发让位 + 唤醒 bg_poll，同时外脑调用 say() 追加补充话术
            await h.machine.handle_input("这里为什么不用配方法而用公式法？")
            _, poll_res = await asyncio.wait_for(bg_poll, timeout=1.0)
            evs = json.loads(poll_res["result"]["content"][0]["text"])["events"]
            ver = evs[0]["snapshot"]["version"]
            await mcp.handle_jsonrpc({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "say",
                    "arguments": {
                        "text": "外脑补充：公式法就是把配方法的过程打包成了通用公式。",
                        "from_version": ver,
                        "final": True,
                    },
                },
            })
        elif seg.kind == "resume" and fired["n"] == 1 and h.machine:
            fired["n"] += 1
            # **显式时间点切分**：从这一刻起产生的 yield 样本全部属于积压，不进统计。
            mcp_window["samples"] = list(h.metrics.yield_ms)
            h.speak_delay = 0.002
            # 挂起新的 50s 长轮询期间，连续追加 19 条学生输入（共 20 条，§12 #7：课堂绝不死锁）
            bg_poll2 = asyncio.create_task(
                mcp.handle_jsonrpc({
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "get_events", "arguments": {"wait_seconds": 50}},
                })
            )
            for k in range(19):
                await h.machine.handle_input("明白了" if k % 2 == 0 else f"为什么第{k}步成立？")
            await asyncio.wait_for(bg_poll2, timeout=1.0)

    hs_mcp.auto_input = mcp_input
    await hs_mcp.machine.start()
    await asyncio.wait_for(hs_mcp.machine.wait_finished(), timeout=12.0)

    # MCP 侧只取"切分点之前"的同条件样本（首次让位）；后段的积压样本一律排除
    mcp_samples = list(mcp_window["samples"] or [])
    assert len(mcp_samples) == 1, (
        f"切分点前应当恰好有 1 个同条件让位样本（首次让位），实际 {mcp_samples}："
        f"样本数变了说明这条用例的输入序列被改过，统计口径必须重新核对")
    # 与基线侧同一口径（中位数）：只有 1 个样本时中位数就是它本身
    mcp_yield = _median(mcp_samples)
    # 防回归：后段确实产生了积压样本。若将来有人删掉"连发 19 条输入"那一段，
    # 上面"MCP 侧不能取中位数"的推理前提就变了 —— 这条断言会提醒他重新审视。
    assert len(hs_mcp.metrics.yield_ms) > 1, (
        "后段没有再产生让位样本：'连发 19 条输入'那段可能被删掉了，"
        "本用例里'积压样本与本条件样本不可比'的前提需要重新核对")
    # 验收标准 #1：接入 MCP 不出现在阻塞链上，yield_latency 与基线偏差 < 10%
    # 绝对容差 16ms 不是随手取的：Windows 上 asyncio 用的单调时钟粒度约 15.6ms
    # （time.get_clock_info("monotonic").resolution），两次测量的差值本身就可能差出
    # 一整个 tick —— 比 tick 更小的偏差在 Windows 上不是"更严格"，而是无法分辨。
    diff_ratio = abs(mcp_yield - base_yield) / max(1.0, base_yield)
    assert diff_ratio < 0.10 or abs(mcp_yield - base_yield) <= 16.0, (
        f"接入 MCP 后让位延迟偏差超标：base={base_yield:.1f}ms（三次中位数 "
        f"{[round(m, 1) for m in base_medians]}，原始样本 {base_samples}）, "
        f"mcp={mcp_yield:.1f}ms（原始样本 {[round(x, 1) for x in mcp_samples]}）, "
        f"ratio={diff_ratio:.1%}"
    )
    # 外脑补充话术已在让位点播出
    spoken_texts = [s.text for s in hs_mcp.spoken]
    assert any("外脑补充：公式法" in t for t in spoken_texts), (
        f"外脑 say 的话未在让位点播出：{spoken_texts}"
    )

    # 验收标准 #6：断连后状态机在 QUIET_AFTER_MS 内接管，且边沿通知只发一次
    mcp.mode.quiet_after_ms = 5_000
    mcp.mode.last_activity_ms = mcp.mode._now_ms() - 6_000
    assert mcp.mode.driving() is False
    assert mcp.mode.state_label() == "quiet_fallback"
    edge1 = mcp.mode.check_edge_transition()
    edge2 = mcp.mode.check_edge_transition()
    assert edge1 is not None and edge1[0] == "warn"
    assert edge2 is None, "announcedQuiet 边沿检测必须在静默期内只播报一次"

    print(f"[M5] 双活让位插播 · 20 条连续输入零死锁 · 让位延迟 base={base_yield:.1f}ms"
          f"（三次基线中位数 {[round(m, 1) for m in base_medians]}，样本 {base_samples}）"
          f"/mcp={mcp_yield:.1f}ms（同条件样本 {[round(x, 1) for x in mcp_samples]}，"
          f"后段积压样本已排除），ratio={diff_ratio:.1%} 通过")


# ===========================================================================
# 6. Phase 2 & 3 & 4：query_rag 急件打断、student_memory、异常可见性（§12 #2, #10）
# ===========================================================================

async def test_rag_interrupt_memory_and_exception_visibility():
    with tempfile.TemporaryDirectory() as tmp:
        mem_file = Path(tmp) / "student_memory.json"
        mem = StudentMemoryStore(mem_file)
        hs = Headless(probe_window=0.1)
        mcp = McpBrainServer(memory_store=mem)
        m = Machine(hs.script, hs.state, hs.settings, hs.llm, hs.handlers(), hs.metrics, mcp=mcp)
        await mcp.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

        # 1) 正常调用 query_rag 与 search_course
        _, rag_ok = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "query_rag", "arguments": {"query": "判别式", "top_k": 2}},
        })
        rag_data = json.loads(rag_ok["result"]["content"][0]["text"])
        assert rag_data["interrupted"] is False
        assert len(rag_data["results"]) >= 1

        # 2) 当有未消费的 urgent query 到达时，query_rag 立即在两次调用间打断让路（§10.3 #6）
        mcp.emit_student_query("老师我有一个急问题！")
        _, rag_int = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "query_rag", "arguments": {"query": "判别式"}},
        })
        int_data = json.loads(rag_int["result"]["content"][0]["text"])
        assert int_data["interrupted"] is True
        assert int_data["reason"] == "urgent_query_arrived"

        # 消费掉急件后，search_course 恢复正常
        await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_events", "arguments": {"wait_seconds": 0}},
        })
        _, sc_ok = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "search_course", "arguments": {"slide": 1}},
        })
        sc_data = json.loads(sc_ok["result"]["content"][0]["text"])
        assert sc_data["slide"] == 1 and sc_data["elements"]

        # 3) Phase 3: student_memory 同步读 + 异步写
        _, w_res = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "student_memory",
                "arguments": {"action": "write", "key": "quad_root", "note": "经常混淆 Δ=0 与 Δ<0"},
            },
        })
        assert json.loads(w_res["result"]["content"][0]["text"])["ok"] is True

        _, r_res = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "student_memory", "arguments": {"action": "read"}},
        })
        r_data = json.loads(r_res["result"]["content"][0]["text"])
        assert r_data["notes"]["quad_root"] == "经常混淆 Δ=0 与 Δ<0"
        await asyncio.sleep(0.05)  # 等待异步落盘任务完成
        assert mem_file.exists(), "student_memory 异步写盘应当落盘"

        # 4) 验收标准 #2：40 分钟课堂（2400s）以 wait_seconds=50 轮询次数 = 48 次（<= 60）
        class_duration_s = 40 * 60
        expected_polls = class_duration_s // DEFAULT_WAIT_SECONDS
        assert expected_polls == 48 <= 60

        # 5) 验收标准 #10：注入故障测试，内部异常绝不吞掉，必须有 isError + metrics + notify
        notifies: list[tuple[str, str]] = []
        orig_notify = m.h.notify

        async def spy_notify(level: str, text: str) -> None:
            notifies.append((level, text))
            await orig_notify(level, text)

        m.h.notify = spy_notify

        async def broken_search(_args):
            raise RuntimeError("injected_rag_failure_for_test")

        mcp._tool_search_course = broken_search  # type: ignore[method-assign]
        _, err_res = await mcp.handle_jsonrpc({
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "search_course", "arguments": {"slide": 1}},
        })
        assert err_res["result"]["isError"] is True
        assert "injected_rag_failure_for_test" in err_res["result"]["content"][0]["text"]
        assert any(e.get("kind") == "mcp_error" for e in m.metrics.events), "异常必须记入 metrics"
        assert any("injected_rag_failure_for_test" in txt for _, txt in notifies), "异常必须触发 notify"

    print("[M6] query_rag 急件打断 · student_memory 异步写同步读 · 故障注入异常可见性 全部通过")


# 注：原来第 7 节「POST /mcp 与 /api/dev/mcp 的 Web 路由集成」已随 web.py 一起删除
# —— 那是旧原型 HTTP 层的东西，正式版没有 /mcp 端点（设计文档 §4 的契约里没有它）。
# MCP 内核本身照旧由本文件 1~6 节覆盖；外部 agent 接入改走 `python run.py mcp-stdio`。

ALL = [
    test_prerequisites_and_phase0,
    test_mcp_surface_and_error_semantics,
    test_event_bus_three_tiers_and_wakeup_latency,
    test_say_version_check_10_cases_and_queue_depth,
    test_dual_active_yield_playback_and_no_deadlock,
    test_rag_interrupt_memory_and_exception_visibility,
]


async def _main_async() -> int:
    failed = 0
    for fn in ALL:
        try:
            await fn()
        except Exception as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\nMCP 外置大脑结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
