"""思考模式测试 —— 守住两条底线。

**底线一：思考内容永远不会被念出来。**
  模型的"自言自语"（reasoning / reasoning_content / thinking）只能进统计，
  绝不能进字幕、更不能进 TTS。学生在课堂上应该只听到老师说的话。
  各家服务商的字段名不一样，所以三种都要能认出来并丢掉。

**底线二：思考不能把正文饿死。**
  思考会吃 max_tokens。实测本机 9B 思考一次约 2900 字 ——
  预算给不够时正文一个 token 都剩不下，表现是"模型连着却一句话都说不出"。
  这种情况必须报错说清原因，绝不能静默退化成兜底话术。

运行：  python tests\\test_thinking.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings          # noqa: E402
from llm import Ctx, LLM, LLMError        # noqa: E402


def fake_ctx() -> Ctx:
    """最小的上下文：够 prompt 拼出来就行，测试不关心提示词内容。"""
    return Ctx(script=None, slide_index=0, slide_title="一元二次方程",
               slide_text="ax²+bx+c=0", unit_id="P1-U1",
               unit_text="判别式的符号决定根的个数。", anchors_hint="判别式")


# -- 假服务商 -------------------------------------------------------------
def _chunk(*, content: str = "", reasoning: str = "", **fields):
    """造一个流式增量。reasoning 字段名可以自定义，测各家兼容。"""
    d = {"content": content or None}
    d.update({k: v for k, v in fields.items()})
    if reasoning:
        d["reasoning"] = reasoning
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**d))])


def _msg(*, content: str = "", reasoning: str = "", **fields):
    d = {"content": content or None}
    d.update(fields)
    if reasoning:
        d["reasoning"] = reasoning
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(**d))])


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class FakeClient:
    """记录每次调用传了什么参数，并按剧本返回。"""

    def __init__(self, chunks=None, message=None):
        self.chunks = chunks or []
        self.message = message
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls.append(kw)
                if kw.get("stream"):
                    return FakeStream(outer.chunks)
                return outer.message

        self.chat = SimpleNamespace(completions=_Completions())


def make_llm(mode: str = "off", **over):
    """造一个 LLM：用真实 Settings（所以策略逻辑是真的），只换掉网络客户端。"""
    s = dataclasses.replace(load_settings(), llm_thinking=mode, **over)
    llm = LLM(s)
    llm.client = FakeClient()
    return llm


async def drain(agen) -> list[str]:
    return [x async for x in agen]


# -- K1: 思考内容绝不外泄 -------------------------------------------------
def test_reasoning_never_reaches_the_student() -> None:
    """最重要的一条：思考全丢掉，只留正文。"""
    llm = make_llm("answer", think_token_budget=512)
    llm.client = FakeClient(chunks=[
        _chunk(reasoning="我先想想……判别式是 b 平方减四 a c。"),
        _chunk(reasoning="嗯，还要考虑 a 不等于零的情况。"),
        _chunk(content="判别式大于零时有两个不相等的实数根。"),
        _chunk(reasoning="对了，还要补充一句。"),
        _chunk(content="等于零时两个根相等。"),
    ])
    got = asyncio.run(drain(llm.stream([{"role": "user", "content": "x"}], think=True)))
    joined = "".join(got)

    assert "我先想想" not in joined, f"思考内容泄漏到产出里了：{joined!r}"
    assert "判别式是 b 平方" not in joined, f"思考内容泄漏到产出里了：{joined!r}"
    assert "还要考虑 a 不等于零" not in joined, f"思考内容泄漏到产出里了：{joined!r}"
    assert joined == "判别式大于零时有两个不相等的实数根。等于零时两个根相等。", joined
    # 但统计要记下来（用来解释"这次为什么慢"）
    assert llm.last_reasoning_chars > 0, "思考了却没记统计"
    print("[K1] 思考内容一个字都没进产出，只进了统计")


def test_all_provider_field_names_are_recognized() -> None:
    """各家把思考放在不同字段：reasoning / reasoning_content / thinking。"""
    for field in ("reasoning", "reasoning_content", "thinking"):
        llm = make_llm("answer")
        llm.client = FakeClient(chunks=[
            _chunk(**{field: "这是模型的内心戏，不能外泄。"}),
            _chunk(content="这是老师要说的话。"),
        ])
        got = "".join(asyncio.run(
            drain(llm.stream([{"role": "user", "content": "x"}], think=True))))
        assert got == "这是老师要说的话。", f"{field} 没被认出来，泄漏了：{got!r}"
        assert llm.last_reasoning_chars > 0, f"{field} 没被记进统计"
    print("[K2] reasoning / reasoning_content / thinking 三种字段都能识别并丢弃")


# -- K3/K4: 预算与开关参数 ------------------------------------------------
def test_thinking_reserves_token_budget() -> None:
    """开思考必须给思考留预算，否则正文会被饿死。"""
    llm = make_llm("answer", think_token_budget=2048)
    llm.client = FakeClient(chunks=[_chunk(content="好。")])
    asyncio.run(drain(llm.stream([{"role": "user", "content": "x"}],
                                 max_tokens=500, think=True)))
    sent = llm.client.calls[0]
    assert sent["max_tokens"] == 500 + 2048, \
        f"开思考时预算没加大，正文会被思考饿死（实际 {sent['max_tokens']}）"
    assert "reasoning_effort" not in sent, \
        "开思考时不该传 reasoning_effort（本机实测只有 none 能关，其余值照样全量思考）"
    print(f"[K3] 开思考自动加预算：500 → {sent['max_tokens']}，且不传 reasoning_effort")


def test_no_thinking_keeps_budget_and_sends_off_switch() -> None:
    llm = make_llm("answer", think_token_budget=2048)
    llm.client = FakeClient(chunks=[_chunk(content="好。")])
    asyncio.run(drain(llm.stream([{"role": "user", "content": "x"}],
                                 max_tokens=500, think=False)))
    sent = llm.client.calls[0]
    assert sent["max_tokens"] == 500, "不开思考时不该动预算"
    assert sent.get("reasoning_effort") == "none", \
        "不开思考时必须显式传 reasoning_effort=none，否则思考型模型会默认全量思考"
    print("[K4] 关思考：预算不变，且显式传 reasoning_effort=none")


# -- K5: 角色策略 ---------------------------------------------------------
def test_mode_answer_only_thinks_for_answers() -> None:
    """`answer` 模式的全部意义：答疑值得慢，短话术绝不能慢。"""
    s = dataclasses.replace(load_settings(), llm_thinking="answer")
    assert s.think_for("answer") is True, "answer 模式下答疑应该思考"
    assert s.think_for("phrase") is False, "answer 模式下短话术绝不能思考（学生正在等）"

    s2 = dataclasses.replace(load_settings(), llm_thinking="off")
    assert s2.think_for("answer") is False and s2.think_for("phrase") is False

    s3 = dataclasses.replace(load_settings(), llm_thinking="all")
    assert s3.think_for("answer") is True and s3.think_for("phrase") is True
    print("[K5] 策略正确：answer 只让答疑思考；off 全关；all 全开")


def test_answer_stream_uses_answer_policy_and_reports_budget_death() -> None:
    """思考把预算吃光 → 必须报错说清原因，不能静默空手而归。"""
    llm = make_llm("answer")
    llm.client = FakeClient(chunks=[_chunk(reasoning="想啊想啊想啊……")])
    try:
        asyncio.run(drain(llm.answer_stream("问题", "clarify", fake_ctx())))
    except LLMError as e:
        assert "预算" in str(e), f"报错但没说是预算的问题：{e}"
        assert "连不上" not in str(e), "不能把预算问题误报成连不上模型（会把人带去查 ollama）"
    else:
        raise AssertionError("思考吃掉预算后竟然静默返回了空产出")
    # 答疑确实走了思考（预算被加大过）
    assert llm.client.calls[0]["max_tokens"] > 500, "答疑没走思考策略"
    print("[K6] 思考吃掉预算时报的是「预算」而不是「连不上」")


# -- K7: 思考指示灯 -------------------------------------------------------
def test_thinking_indicator_turns_on_then_off() -> None:
    """思考时点亮、开口时熄灭 —— 否则学生只看到屏幕不动，以为卡死。"""
    events: list[bool] = []

    async def on_think(on: bool) -> None:
        events.append(on)

    llm = make_llm("answer")
    llm.client = FakeClient(chunks=[
        _chunk(reasoning="嗯……"),
        _chunk(reasoning="再想想。"),
        _chunk(content="答案是两个根。"),
    ])
    asyncio.run(drain(llm.stream([{"role": "user", "content": "x"}], think=True,
                                 on_think=on_think)))
    assert events == [True, False], f"指示灯顺序不对：{events}（应为 思考→开、开口→关）"
    print("[K7] 思考指示灯：开始思考→亮，开口说话→灭")


def test_indicator_never_stays_stuck_when_nothing_is_said() -> None:
    """光想不说也必须灭灯 —— 否则指示灯会永远停在"在思考"。"""
    events: list[bool] = []

    async def on_think(on: bool) -> None:
        events.append(on)

    llm = make_llm("answer")
    llm.client = FakeClient(chunks=[_chunk(reasoning="想了很久，但一个字都没说。")])
    asyncio.run(drain(llm.stream([{"role": "user", "content": "x"}], think=True,
                                 on_think=on_think)))
    assert events and events[-1] is False, f"指示灯卡在「在思考」了：{events}"
    print("[K8] 光想不说也会灭灯，指示灯不会卡住")


def test_indicator_failure_does_not_break_the_class() -> None:
    """指示灯坏了不该影响上课。"""
    async def boom(on: bool) -> None:
        raise RuntimeError("前端断了")

    llm = make_llm("answer")
    llm.client = FakeClient(chunks=[
        _chunk(reasoning="想想。"),
        _chunk(content="这是正文。"),
    ])
    got = "".join(asyncio.run(drain(
        llm.stream([{"role": "user", "content": "x"}], think=True, on_think=boom))))
    assert got == "这是正文。", "指示灯抛异常把正文也带走了"
    print("[K9] 指示灯抛异常不影响正文产出")


# -- K10: 思考耗时统计 ----------------------------------------------------
def test_think_duration_is_measured() -> None:
    """要能回答"刚才那几秒去哪了"。"""
    llm = make_llm("answer")
    llm.client = FakeClient(chunks=[
        _chunk(reasoning="先想一会儿。"),
        _chunk(content="正文。"),
    ])
    asyncio.run(drain(llm.stream([{"role": "user", "content": "x"}], think=True)))
    assert llm.last_think_ms >= 0, "没记录思考耗时"
    assert llm.last_reasoning_chars > 0, "没记录思考字数"
    # 没有思考时不该留下上一次的数字
    llm.client = FakeClient(chunks=[_chunk(content="直接答。")])
    asyncio.run(drain(llm.stream([{"role": "user", "content": "x"}], think=False)))
    assert llm.last_reasoning_chars == 0, "关思考时残留了上次的思考统计"
    print("[K10] 思考耗时/字数有统计，且每次生成都会重置")


# -- K11: chat 路径同样不漏 -----------------------------------------------
def test_chat_never_returns_reasoning() -> None:
    """短话术走的是 chat（非流式），同样不能把思考当话说出来。"""
    llm = make_llm("off")
    llm.client = FakeClient(message=_msg(reasoning="我在想该怎么开场……", content="我们先看这一页。"))
    got = asyncio.run(llm.chat([{"role": "user", "content": "x"}]))
    assert got == "我们先看这一页。", f"chat 把思考当正文返回了：{got!r}"
    assert llm.last_reasoning_chars > 0, "chat 路径没记思考统计"
    print("[K11] chat（短话术）路径同样不返回思考内容")


ALL = [
    test_reasoning_never_reaches_the_student,
    test_all_provider_field_names_are_recognized,
    test_thinking_reserves_token_budget,
    test_no_thinking_keeps_budget_and_sends_off_switch,
    test_mode_answer_only_thinks_for_answers,
    test_answer_stream_uses_answer_policy_and_reports_budget_death,
    test_thinking_indicator_turns_on_then_off,
    test_indicator_never_stays_stuck_when_nothing_is_said,
    test_indicator_failure_does_not_break_the_class,
    test_think_duration_is_measured,
    test_chat_never_returns_reasoning,
]


def main() -> int:
    ok = 0
    for fn in ALL:
        try:
            fn()
            ok += 1
        except AssertionError as e:
            print(f"  ❌ {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n思考模式结果：{ok}/{len(ALL)} 通过")
    return 0 if ok == len(ALL) else 1


if __name__ == "__main__":
    raise SystemExit(main())
