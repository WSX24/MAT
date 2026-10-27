"""Agent-C 的真实模型接入 —— §7.2 ④ 的验收（proposer.py / config.py / web.py 的接线）。

为什么单独一个文件
------------------
`agent/producer.py` 的流水线（门卫 / 窗口 / 校验 / 幂等写池）已由 test_producer.py
逐条钉住，那里的 proposer 是离线的 `TemplateProposer`。但"C 生产出来的东西真的能
走到 J 手里、真的被播出"这一整条，之前没有任何一个用例覆盖：
`test_runtime_assembly` 的 TemplateProposer 用例只看 tick 次数，`written` 一直是空
（6 单元的夹具在 `current_seq>0` 时上货窗口 [+5,+15] 恒为空）。
所以这里补的正是那一段：**假模型 → C 写池 → J 在目标单元边界选中 → 那一格真的用变体播出**。

逐条对应的验收（编号沿用任务书）：
  A1) `build_proposer` 开关关闭 → 返回 None、零构造、零网络
  A2) `LlmProposer` 用 json_mode / think=False / 独立 LLM 实例 / 可覆盖 provider 与 model
  A3) 三种 JSON 形态都能解析；坏输入抛异常而不是静默返回空
  A4) **端到端**：C 造的货被 J 选中，该单元真的用变体播出（事件 + 播出内容双断言）
  A5) C 全程失败 → 课堂逐字无感、循环正常返回、无悬挂 task
  A6) 时钟回归：C 给"当前+5"备的货必须活到目标格边界（两套 seq 不许混用）
  A7) web.py 默认关零执行：逐字一致的课堂、不多一个 task、不多 import 一个模块
  A8) json_mode 下模型仍不听话（回显 response_format）→ 脱掉 response_format 重试一次
      （恰好 2 次）；正常路径不重试（恰好 1 次）；两次都垃圾 → ProposalError 照原样抛

全部离线：零网络、零真实 LLM（一律注入假 client）、零真实 TTS、零 60s 等待。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.producer import ProposalRequest, Snapshot  # noqa: E402
from agent.proposer import (  # noqa: E402
    LlmProposer,
    ProposalError,
    build_proposer,
    parse_proposals,
)
from director.assembly import serve_agent_c, wire  # noqa: E402
from director.pool import CandidatePool  # noqa: E402
from llm import LLM, split_sentences  # noqa: E402
from models import LessonScript, Slide  # noqa: E402
from state import LectureUnit  # noqa: E402
from tests.fixtures import make_deck  # noqa: E402
from tests.test_machine import FakeLLM, make_settings  # noqa: E402
from tests.test_runtime_assembly import (  # noqa: E402
    assert_no_dangling_tasks,
    run_with_agent_c,
)

#: C 的唤醒周期。60s 会让"跑完一整节课"和"一次都没醒"变成同一件事。
INTERVAL_S = 0.01


# ===========================================================================
# 假 client：唯一允许的"模型"
# ===========================================================================

class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


#: 假 client 能吐的一份返回：模型正文，或一个"调到就炸"的异常。
Payload = str | Exception


class FakeCompletions:
    """替掉 `llm.client.chat.completions.create` —— 本文件所有用例的网络边界。

    记下每一次调用的 kwargs：A2 要断言的"用了哪个模型 / 有没有开 json_mode /
    有没有开思考 / 超时与预算传没传"全都只能从这里看（`LLM.chat` 不返回值以外的东西）。
    """

    def __init__(self, payload: Payload | list[Payload]) -> None:
        #: 依次吐出的一份或多份返回。给 list 时按调用序取，用尽后重复最后一份 ——
        #: 降级重试用例要"第一次垃圾、第二次正常"，而"次数"本身也是断言对象
        #: （恰好 2 次 / 恰好 1 次），所以队列比"可调用对象"更好读。
        self.payloads = payload if isinstance(payload, list) else [payload]
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        if isinstance(payload, Exception):
            raise payload
        return _Resp(payload)


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeClient:
    def __init__(self, payload: Payload | list[Payload]) -> None:
        self.completions = FakeCompletions(payload)
        self.chat = FakeChat(self.completions)


def inject(llm, payload: Payload | list[Payload]) -> FakeCompletions:
    """把假 client 装到 LLM 实例上，返回那次调用的记账本。"""
    llm.client = FakeClient(payload)
    return llm.client.completions


def make_request(prompt: str = "【课堂快照】这一格的另一种讲法") -> ProposalRequest:
    """一份最小的自包含输入（prompt 之外的结构化字段是给离线 proposer 用的）。"""
    return ProposalRequest(
        prompt=prompt,
        snapshot=Snapshot(current_unit="S01-U02", current_seq=1, slide=0,
                          total_units=16, remaining_units=14, confused_count=1),
        window=(),
        pool_status={"alive": 0, "consumed": 0, "expired": 0},
    )


def settings_c(**kw):
    """打开 C 的开关（其余沿用离线夹具的 Settings）。

    `make_settings()` 的 provider 是 "fake"、temperature=0.0、thinking_off_value 用默认
    "none" —— 正好用来断言"C 的 temperature 不跟课堂走"和"think=False 真的被翻成了
    服务商认的那个值"。
    """
    return replace(make_settings(), agent_c_enabled=True, **kw)


# ===========================================================================
# A2) 调用参数：json_mode / think=False / 单条 user 消息 / 超时与预算
# ===========================================================================

async def test_propose_uses_json_mode_no_think_and_one_user_message():
    settings = settings_c(agent_c_timeout=7.5, agent_c_max_tokens=321)
    proposer = build_proposer(settings)
    payload = '[{"target_unit": "S01-U07", "variant": {"variant_id": "c1"}, "when": {}}]'
    calls = inject(proposer.llm, payload)

    request = make_request()
    got = await proposer.propose(request)

    assert got == [{"target_unit": "S01-U07", "variant": {"variant_id": "c1"}, "when": {}}]
    assert len(calls.calls) == 1, "一次唤醒只该有一次模型调用"
    kw = calls.calls[0]

    # ① json_mode=True：靠 response_format 落地（不是靠提示词里写"只输出 JSON"）
    assert kw["response_format"] == {"type": "json_object"}, "C 要的是结构化的候选数组"
    # ② think=False：两个可见后果都要在 —— 思考预算没被撑大、思考参数是"关"的那个值
    assert kw["max_tokens"] == 321, (
        "think=True 时 _budget 会把 max_tokens 撑大（+think_token_budget）；"
        f"没被撑大才说明 think=False 真的传下去了：{kw['max_tokens']}")
    assert kw.get("reasoning_effort") == settings.thinking_off_value, \
        f"think=False 必须翻成服务商认的『关』：{kw.get('reasoning_effort')!r}"
    # ③ 超时 / 预算按配置传
    assert kw["timeout"] == 7.5
    # ④ 温度是 C 自己的口径（造分支要多样性），不跟课堂的 0.0 走
    assert kw["temperature"] == 0.7, f"C 的温度不该被课堂那份盖掉：{kw['temperature']}"
    # ⑤ 只发一条 user 消息，内容就是自包含输入（系统提示已经拼在 request.prompt 里）
    assert kw["messages"] == [{"role": "user", "content": request.prompt}], (
        "request.prompt 里已经含 DIRECTING.system_prompt，再补一条 system 等于发两遍")

    print(f"[A2] 一次调用：model={kw['model']} json_mode=on think=off "
          f"timeout={kw['timeout']} max_tokens={kw['max_tokens']} "
          f"messages={len(kw['messages'])} 条")


async def test_model_and_provider_can_be_overridden_without_touching_the_classroom():
    """`chat()` 没有 model 形参，模型来自 profile —— 覆盖只能靠换一份 Settings。

    这条用例钉的正是那个陷阱：**不许**顺手改 `llm.profile.model`。
    `Settings` 是 frozen，`profile` 又是 `settings.providers[name]` 那个对象本身，
    就地改它会让课堂上那个 LLM（共享同一份 Settings）的模型一起变，而且静默。
    """
    base = make_settings()
    other = replace(base.providers["fake"], name="other", model="other-model")
    settings = replace(base, agent_c_enabled=True, agent_c_provider="other",
                       providers={**base.providers, "other": other})
    proposer = build_proposer(settings)
    calls = inject(proposer.llm, "[]")

    assert proposer.llm.profile.name == "other", "provider 覆盖没生效"
    assert proposer.llm.profile.model == "other-model", "新 provider 自带的模型该被沿用"
    assert settings.provider == "fake", "原始的 Settings 一个字节都不许动（课堂还在用它）"

    await proposer.propose(make_request())
    assert calls.calls[0]["model"] == "other-model"

    # 只覆盖 model（provider 留空）：沿用当前 provider，只换模型名
    settings2 = settings_c(agent_c_model="big-model")
    proposer2 = build_proposer(settings2)
    calls2 = inject(proposer2.llm, "[]")
    await proposer2.propose(make_request())
    assert calls2.calls[0]["model"] == "big-model"
    assert settings2.active.model == "fake-model", "覆盖模型不许改到课堂那一份 Settings"

    # 服务商名字写错：不许把课堂炸掉（settings.active 会 SystemExit），退回当前服务商
    proposer3 = build_proposer(settings_c(agent_c_provider="typo_provider"))
    assert proposer3.llm.profile.name == settings.provider, \
        "未知的 AGENT_C_PROVIDER 该退回当前服务商，而不是让开课失败（§11）"

    print("[A2] provider=other/model=other-model 生效且课堂 Settings 未变；"
          "写错的 provider 退回当前服务商")


# ===========================================================================
# A3) 三种 JSON 形态 + 坏输入必须抛
# ===========================================================================

def test_parse_accepts_the_three_shapes_a_real_model_returns():
    """纯数组 / 对象包裹 / json 围栏 —— 三种都是真实观测到的形态。

    对象包裹那条不是妥协：`response_format={"type":"json_object"}` 会让不少服务商
    **拒绝顶层数组**，模型于是自己加一层 `{"proposals": [...]}`。
    """
    shapes = {
        "纯数组": '[{"target_unit": "S01-U07"}]',
        "对象包裹": '{"proposals": [{"target_unit": "S01-U07"}]}',
        "json 围栏": '```json\n[{"target_unit": "S01-U07"}]\n```',
        "围栏+对象+前后废话":
            '好的，结果如下：\n```json\n{"proposals": [{"target_unit": "S01-U07"}]}\n'
            '```\n以上。',
    }
    for name, raw in shapes.items():
        got = parse_proposals(raw)
        assert isinstance(got, list), f"{name} 必须解析成 list，实际 {type(got).__name__}"
        assert got == [{"target_unit": "S01-U07"}], f"{name} 解析结果不对：{got}"
    print(f"[A3] 三种形态全部解析成 list：{' / '.join(shapes)}")


@pytest.mark.parametrize("raw", [
    "",                      # 空（模型什么都没吐）
    "   \n ",                # 只有空白
    '[{"target_unit": ',     # 坏 JSON（半截）
    '{"total": 2}',          # 不是数组，也没有提案数组
    '"好"',                  # 标量
])
def test_parse_raises_on_bad_payloads(raw):
    """坏输入必须抛，绝不返回空 list 冒充成功。

    为什么这条是硬要求：`run_tick` 把 proposer 的异常记成 `proposer_error`，
    而"返回空数组"会被记成一次**成功但没造货**的唤醒 —— 前者要查，后者不用查，
    两者混在一起，C 什么时候坏的没人知道。
    """
    with pytest.raises(ProposalError):
        parse_proposals(raw)


async def test_propose_lets_the_exception_escape():
    """proposer 内部不许自己兜底：异常交回 `run_tick` 去记成 proposer_error。"""
    proposer = build_proposer(settings_c())
    inject(proposer.llm, "[这个不是 JSON")
    with pytest.raises(ProposalError):
        await proposer.propose(make_request())

    # 模型侧报错（连不上 / 400）同样不许被吞：LLM 把它包成 LLMError 抛出来
    proposer2 = build_proposer(settings_c())
    inject(proposer2.llm, RuntimeError("模型服务整个挂了"))
    with pytest.raises(Exception) as ei:
        await proposer2.propose(make_request())
    assert not isinstance(ei.value, ProposalError), "模型侧失败要保留它本来的类型（LLMError）"

    print("[A3] 坏 JSON → ProposalError 逃出；模型侧异常 → LLMError 逃出，都留给 run_tick 记账")


# ===========================================================================
# A8) 降级重试：模型在对象契约下仍不听话时的兜底
# ===========================================================================

#: 线上真的吐回来的那段垃圾（真实 key + 真实课件窗口，json_mode=True）。
#: 为什么用例里必须用它、而不是随手编一个坏输入：编出来的只能证明"坏输入会重试"，
#: 证明不了"这一类坏输入会被重试救回来" —— 而后者才是这条降级存在的理由。
ECHOED_RESPONSE_FORMAT = '{"type": "json_object", "content": []}'

#: 去掉 json_mode 之后模型给的正常形态：顶层纯数组（对照组实测就是这个）。
FREETEXT_ARRAY = '[{"target_unit": "S01-U07", "variant": {"variant_id": "c1"}, "when": {}}]'


async def test_propose_retries_once_without_json_mode_on_the_real_garbage():
    """模型回显 `response_format` 时：脱掉 json_mode 再问一次，这一次解析成功。

    两条断言缺一不可：① **恰好 2 次**调用（多了就不是"一次降级"而是重试循环）；
    ② 两次的唯一差异是 `response_format`（差异多一个，重试就不再是同一个请求 ——
    比如少了 `timeout`，降级那一次会比正常那一次慢一倍，而离线用例全绿）。
    """
    proposer = build_proposer(settings_c())
    calls = inject(proposer.llm, [ECHOED_RESPONSE_FORMAT, FREETEXT_ARRAY])

    got = await proposer.propose(make_request())

    assert got == [{"target_unit": "S01-U07", "variant": {"variant_id": "c1"}, "when": {}}], \
        f"降级那一次的结果必须被正常解析：{got}"
    assert len(calls.calls) == 2, f"失败一次只许重试一次（恰好 2 次调用）：{len(calls.calls)}"

    first, second = calls.calls
    assert first["response_format"] == {"type": "json_object"}, "第一次必须照原样带 json_mode"
    assert "response_format" not in second, "重试那一次必须去掉 response_format（= json_mode=False）"
    assert {k: v for k, v in first.items() if k != "response_format"} == second, (
        "降级只该改 json_mode，其余参数（prompt/温度/预算/超时/think）必须逐字一致："
        f"{sorted(first)} vs {sorted(second)}")

    print(f"[A8] json_mode 下解析失败 → 无 response_format 重试成功（调用 {len(calls.calls)} 次）")


async def test_propose_does_not_retry_when_the_first_answer_parses():
    """正常路径**一次调用**：合法对象进来就不再花第二次钱（成本不变）。

    为什么单独一条：降级重试最容易写错的地方是"顺手改成两次都跑"或者"catch 写得太宽"。
    前者让每次唤醒的成本翻倍（§12 阶段 3 数的正是调用次数），后者会把模型侧故障
    也变成一次多余的重试。
    """
    proposer = build_proposer(settings_c())
    calls = inject(proposer.llm, '{"proposals": [{"target_unit": "S01-U07"}]}')

    got = await proposer.propose(make_request())

    assert got == [{"target_unit": "S01-U07"}]
    assert len(calls.calls) == 1, f"解析成功就不许重试（恰好 1 次调用）：{len(calls.calls)}"
    assert calls.calls[0]["response_format"] == {"type": "json_object"}, \
        "正常路径仍然用 json_mode（提示词已经与它同形，两边不再矛盾）"

    print("[A8] 正常路径 1 次调用、带 response_format，无重试")


async def test_propose_raises_when_both_attempts_are_garbage():
    """两次都是垃圾 → 照原样抛 `ProposalError`，交给 `run_tick` 记成 `bad_payload`。

    为什么不返回 `[]` 兜底：空数组会被记成"一次成功但没造货"，而这两件事的处置完全
    不同（前者要查、后者不用查）。降级的边界是"多给一次机会"，不是"把失败改写成成功"。
    """
    proposer = build_proposer(settings_c())
    calls = inject(proposer.llm, ECHOED_RESPONSE_FORMAT)

    with pytest.raises(ProposalError):
        await proposer.propose(make_request())

    assert len(calls.calls) == 2, f"重试上限是 1 次、不许循环（恰好 2 次调用）：{len(calls.calls)}"
    assert "response_format" not in calls.calls[1], "第二次必须是降级形态，否则这不叫降级"

    print("[A8] 两次都垃圾 → ProposalError 逃出，调用恰好 2 次")


async def test_propose_does_not_degrade_on_a_model_side_failure():
    """模型侧报错（连不上 / 400）**不触发**降级重试：降级只对"解析失败"生效。

    为什么 `LLM.chat` 内部就占了 2 次调用：它自己有一层"服务商不认 response_format
    就脱掉重发"的兜底（见 llm.py 的 `chat`）。所以这里的 2 次全部来自那一层 ——
    重试一个"连不上"的模型毫无意义，只是把 20s 的预算烧光（§11：超时就放弃、下个周期再来）。

    `propose` 把 `_ask` 留在 `try` **之外**，正是为了让这件事在结构上成立；这条用例
    把那个结构锁成可观测结果：异常类型照旧是 `LLMError`、调用数还是 2。哪天有人把
    模型调用挪进 `try` 并放宽 `except`，调用数就会变（3 次起）——用例立刻红。
    """
    proposer = build_proposer(settings_c())
    calls = inject(proposer.llm, RuntimeError("模型服务整个挂了"))

    with pytest.raises(Exception) as ei:
        await proposer.propose(make_request())
    assert not isinstance(ei.value, ProposalError), "模型侧失败要保留它本来的类型（LLMError）"

    assert len(calls.calls) == 2, (
        "这 2 次都来自 LLM.chat 自己的 response_format 兜底；"
        f"propose 若再重试就会是 3 次起：{len(calls.calls)}")
    assert "response_format" not in calls.calls[1], "LLM.chat 的兜底形态"

    print("[A8] 模型侧故障不触发降级重试（调用 2 次，全部来自 LLM.chat 内部兜底）")


# ===========================================================================
# A1) 默认关：不构造 LLM、不联网
# ===========================================================================

def test_build_proposer_builds_no_llm_when_disabled(monkeypatch):
    """开关关着时 `build_proposer` 必须返回 None 且**一次都不构造 LLM**。

    为什么连"构造一下先放着"都不行：`LLM.__init__` 会建 `AsyncOpenAI`（连接池）。
    默认课堂根本不认识这套东西（§11 最后一行），仅仅是多造一个客户端就已经改变了
    进程的资源形状 —— 默认路径必须一个字节都不动。
    """
    import agent.proposer as mod

    built: list[tuple] = []

    class CountingLLM:
        def __init__(self, *args, **kwargs):
            built.append((args, kwargs))
            raise AssertionError("默认关的时候不许构造 LLM")

    off = make_settings()
    assert off.agent_c_enabled is False, "agent_c_enabled 的默认值必须是关"
    with monkeypatch.context() as patch:
        patch.setattr(mod, "LLM", CountingLLM)
        assert build_proposer(off) is None
        assert built == [], f"关着时构造了 LLM：{built}"

    # 打开后才会构造（证明上面那条不是因为"根本没走到"）
    on = build_proposer(settings_c())
    assert isinstance(on, LlmProposer), "开关打开时必须给出真 proposer"
    assert on.llm is not None

    print("[A1] 关：返回 None 且零构造；开：给出 LlmProposer")


def test_proposer_owns_a_private_llm_instance():
    """§3.2 D2：provider 代码共用、客户端实例隔离。

    共用课堂那个客户端的后果很具体：C 的一次唤醒要跑 20 秒，课堂的长流式生成会排在
    它后面；两边还会共享 `llm.degraded` —— C 的模型挂了会把课堂判成降级，
    学生于是听到兜底话术，而课堂上那个模型一直是好的。
    """
    settings = settings_c()
    classroom_llm = LLM(settings)          # 课堂那个（本用例里只是被比较的对象）
    proposer = build_proposer(settings)

    assert proposer.llm is not classroom_llm, "C 必须自己有 LLM 实例（§3.2 D2）"
    assert proposer.llm.client is not classroom_llm.client, "连客户端都不许共用"

    # 降级状态各自独立：C 挂了不许把课堂判成降级
    proposer.llm.mark_down("模拟 C 的模型服务挂掉")
    assert classroom_llm.degraded is False, "C 的失败不许污染课堂的降级标志"

    print("[A2] C 的 LLM/客户端与课堂的都不是同一个对象；降级标志互不影响")


# ===========================================================================
# 夹具：足够长的课（窗口 [+5,+15] 才有内容）
# ===========================================================================

#: 目标单元被换上的那条变体。首句的引导语是断言"真的换了讲法"的抓手
#: （讲义原文里没有这几个字）。
VARIANT_LEAD = "换句话说"
VARIANT_TEXT = "换句话说，这一格讲的是同一件事，我们换一个角度来看它。"


def make_long_script(n: int = 16, *, presets: bool = False) -> LessonScript:
    """一节课 n 个单元（每格一句），够长到 C 的上货窗口有内容。

    为什么必须够长（≥16）：C 的窗口是「当前+5 起、+15 止」（§7.4）。6 单元的夹具只在
    课首那一刹那有窗口（且只剩最后那一格"目录。"，P 的生成器都不给它造变体），
    于是 `written` 永远是空 —— 用例会"看起来通过但什么都没测"。
    这也是既有 `test_runtime_assembly` 里 TemplateProposer 那条用例的盲区。

    `presets=True` 时给每个单元挂一条 v1（"换说法"）预置变体：池子里于是同时有
    P 的预置货和 C 的货，装载时的淘汰（expire_stale）才有东西可淘汰 —— A6 要证明的
    "preset 的旧货照旧按每次装载推进一格被淘汰"需要它。
    """
    units: list[LectureUnit] = []
    for i in range(n):
        uid = f"S01-U{i + 1:02d}"
        variants = []
        if presets:
            variants = [{"variant_id": "v1", "label": "换说法",
                         "text": f"换个说法，{uid} 讲的是同一件事，我们从另一边看。"}]
        units.append(LectureUnit(
            unit_id=uid, slide=0, index_in_slide=i,
            text=f"{uid} 讲的是这一点，我们把它说清楚。",
            anchors=[], is_page_end=(i == n - 1), kp=["K1"], variants=variants,
        ))
    return LessonScript(
        course_id="long_fixture", title="长课夹具", session_goals=["跑完一整节课"],
        slides=[Slide(index=0, title="第一页", elements=[])], units=units,
    )


class ScriptedProposer:
    """假模型：给窗口里**最靠前**的那一格造一条变体。

    `when` 用空条件（§4.6 里就是"恒真"），而不是"连续没懂 ≥2"：这条用例要证的是
    **链路**（C 写进去的货真的被 J 选中并播出），不是条件策略 —— 后者由
    test_director_facts / director/presets 各自钉住。空条件正好把"选中"这一格
    从条件求值里摘出来，红了就只可能是接线断了。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.targets: list[str] = []

    async def propose(self, request: ProposalRequest) -> list[dict]:
        self.calls += 1
        unit = request.window[0]
        self.targets.append(unit.unit_id)
        return [{
            "target_unit": unit.unit_id,
            "variant": {"variant_id": "c1", "label": "换个说法", "text": VARIANT_TEXT},
            "when": {},
            "reason": "假模型：给窗口第一格换一种讲法",
        }]


class ExplodingProposer:
    """每次调用都抛 —— §11"C 整个挂掉"的最坏形态，但只挂 C。

    刻意不在外层再包 try：要证明的是**循环宿主**吞掉了它。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, request: ProposalRequest) -> list[dict]:
        self.calls += 1
        raise RuntimeError("模拟 C 的模型服务整个挂掉")


def say_confused_once(classroom, unit_id: str) -> dict:
    """让学生在那个单元说一次「没懂」（走真实输入，不写 state）。

    为什么必须走 `handle_input`：门卫认的是 `state.comprehension` 里的 confused，
    而那个字段只由真实输入（或探针超时）产生。直接写 state 等于把前提当结论 ——
    "事实成立之后链路能通" ≠ "事实根本会产生"。
    """
    said = {"n": 0}

    async def hook(c, seg):
        if seg.kind == "unit" and seg.unit_id == unit_id and said["n"] == 0:
            said["n"] += 1
            await c.machine.handle_input("没懂")

    classroom.on_seg = hook
    return said


def agent_c_candidates(director) -> list:
    return [c for c in director.pool._items.values() if c.source == "agent_c"]


def classroom_with_director(script: LessonScript):
    """建无头课堂并把决策层装上（`wire` 走的是生产同一条路）。"""
    from tests.test_variant_e2e import Classroom

    classroom = Classroom(script)
    wire(classroom.machine, settings=replace(make_settings(), director_enabled=True))
    return classroom


# ===========================================================================
# A4) 端到端：C 造的货被 J 选中，那一格真的用变体播出
# ===========================================================================

async def test_c_candidate_is_picked_by_j_and_really_aired():
    """**本任务的核心验收**：C 写 → J 选 → 播出，一条链走通。

    三件事缺一不可，而且必须分别断言（只看 `variant_applied` 会漏掉"C 根本没写进去"，
    只看池子里有货会漏掉"选出来却没人播"）：
      ① 池子里有一条 `source == "agent_c"` 的候选，`target_unit` 是真的 unit_id；
      ② J 在**那个单元**的边界上选中了它（`variant_applied` 的 candidate_id / unit 对得上）；
      ③ 那一格播出的第一句就是变体的第一句（带引导语 —— 讲义原文里没有这几个字）。
    """
    script = make_long_script(16)
    classroom = classroom_with_director(script)
    director = classroom.machine.director
    said = say_confused_once(classroom, "S01-U01")
    proposer = ScriptedProposer()

    ticks = await run_with_agent_c(classroom, proposer=proposer,
                                   interval_s=INTERVAL_S, deck=make_deck())

    assert said["n"] == 1, "用例没跑起来：学生没真的说没懂"
    assert proposer.calls > 0, "门卫一次都没开闸，这条用例证明不了任何事"
    assert classroom.machine._finished.is_set(), "课堂必须正常跑完"

    # ① C 真的写了候选进池
    written = agent_c_candidates(director)
    assert written, (f"C 一次都没写进池子（proposer 调了 {proposer.calls} 次，"
                     f"窗口={proposer.targets[:3]}）")
    cand = written[0]
    unit_ids = {u.unit_id for u in script.units}
    assert cand.target_unit in unit_ids, f"target_unit 必须是真实 unit_id：{cand.target_unit!r}"
    assert cand.candidate_id == f"c-{cand.target_unit}-c1", \
        f"幂等键必须由提案本身派生（§7.2 ⑥）：{cand.candidate_id}"

    # ② 它被 J 在那个单元的边界选中（不是"过期了"、也不是"选了别的"）
    events = [e for e in classroom.metrics.events
              if e["kind"] == "variant_applied"
              and e.get("candidate_id") == cand.candidate_id]
    assert events, (f"C 造的 {cand.candidate_id} 一次都没被选中；"
                    f"池子={[(c.candidate_id, c.status) for c in director.pool._items.values()]}"
                    f" stats={director.stats}")
    ev = events[0]
    assert ev["unit"] == cand.target_unit, \
        f"选中的货必须用在它写的那一格上：{ev['unit']} vs {cand.target_unit}"
    assert director.pool.get(cand.candidate_id).status == "consumed", "选中即消费（§4.5）"

    # ③ 那一格真的用变体播出，且首句带引导语
    played = classroom.variant_segs(cand.target_unit)
    expected = split_sentences(VARIANT_TEXT)
    assert [s["text"] for s in played] == expected, \
        f"这一格必须整格播变体，一句不差：{[s['text'] for s in played]}"
    assert played[0]["text"].startswith(VARIANT_LEAD), \
        f"首句必须带引导语（真的换了讲法）：{played[0]['text']!r}"

    print(f"[A4] tick {ticks} 次 / 调模型 {proposer.calls} 次")
    print("     C 写的候选："
          f"{[(c.candidate_id, c.target_unit, c.source, c.status) for c in written]}")
    print(f"     J 选中：candidate_id={ev['candidate_id']} variant_id={ev.get('variant_id')} "
          f"unit={ev['unit']}（边界发生在 {ev.get('decided_at')}）")
    print(f"     {ev['unit']} 播出首句：{played[0]['text']!r}"
          f"（共 {len(played)} 句：{' → '.join(s['text'] for s in played)}）")


# ===========================================================================
# A6) 时钟回归：C 的货必须活到目标格边界（state.seq ≠ 单元序号）
# ===========================================================================

async def test_agent_c_candidate_survives_until_its_target_boundary(monkeypatch):
    """钉住两套时钟：`state.seq` 是**句子计数器**，`expires_at_seq` 是**单元序号**（§4.4）。

    这条用例是那条真实 bug 的回归：`machine._prefetch_decision` 曾经用 `seq=st.seq`
    去装载，而 `load_unit_presets` 第一行就是 `pool.expire_stale(seq)`，
    C 的候选按 `ordinals[target] + 余量` 记账（个位数），于是**每一条 C 的候选都会在
    到货之前被判过期** —— 课堂上只表现为"后面再也没换过讲法"，没有任何报错。
    把调用点改回 `st.seq` 时，这条应当立刻红。

    同时钉住淘汰纪律没有被削弱：preset 的旧货照旧在**下一次装载**时被清掉
    （池子永远只服务于眼前这一格，CAP=5 不被摊薄）。
    """
    script = make_long_script(16, presets=True)
    classroom = classroom_with_director(script)
    director = classroom.machine.director
    say_confused_once(classroom, "S01-U01")

    # 观察装载点：装载前记下 C 的候选还活着没有，装载后记下旧 preset 被没被清掉
    import machine as machine_mod

    loads: list[dict] = []
    original = machine_mod.load_unit_presets

    def spy(pool, unit, *, seq, now, target_unit=None):
        before = {c.candidate_id: c.status for c in agent_c_candidates(director)}
        n = original(pool, unit, seq=seq, now=now, target_unit=target_unit)
        loads.append({
            "target": unit.unit_id, "seq": seq, "n": n, "agentC_before": before,
            "presets": {c.candidate_id: c.status
                        for c in pool._items.values() if c.source == "preset"},
        })
        return n

    monkeypatch.setattr(machine_mod, "load_unit_presets", spy)

    proposer = ScriptedProposer()
    await run_with_agent_c(classroom, proposer=proposer, interval_s=INTERVAL_S, deck=make_deck())

    assert loads, "一次装载都没发生，这条用例什么都没测到"
    # ① 装载点传的必须是单元序号（两套时钟不许混用）
    for item in loads:
        assert item["seq"] == script.order_index(item["target"]), (
            f"装载 {item['target']} 时传的 seq={item['seq']}，"
            f"而它的单元序号是 {script.order_index(item['target'])} —— "
            "传错时钟会让 C 的候选在到货前全部过期")
    assert [i["seq"] for i in loads] == sorted(i["seq"] for i in loads), "装载时钟必须单调推进"

    # ② C 给"当前+5"备的货，在它自己那一格的装载点上仍然 alive
    written = agent_c_candidates(director)
    assert written, "C 一次都没写进池子，这条断言无从谈起"
    cand = written[0]
    at_target = [i for i in loads if i["target"] == cand.target_unit]
    assert at_target, f"{cand.target_unit} 的装载没发生：{[i['target'] for i in loads]}"
    assert at_target[0]["agentC_before"].get(cand.candidate_id) == "alive", (
        f"C 的 {cand.candidate_id} 在 {cand.target_unit} 的装载点上已经不是 alive 了："
        f"{at_target[0]['agentC_before']}")

    # ③ 淘汰纪律没被削弱：装载第 j 格时，第 j-1 格的 preset 旧货不许还占着 alive 名额
    #    （它要么已作废、要么已被 J 选中消费 —— 两种都算"不再占位"，这才是不变量）
    checked = 0
    for item in loads:
        idx = script.order_index(item["target"])
        if idx <= 0:
            continue
        prev = f"c-{script.units[idx - 1].unit_id}-v1"
        if prev in item["presets"]:
            assert item["presets"][prev] != "alive", (
                f"装载 {item['target']} 时上一格的 preset {prev} 还活着 —— "
                f"CAP=5 会被整节课的变体摊薄")
            checked += 1
    assert checked >= 3, f"至少要跨过 3 个单元才证明得了'每次装载推进一格'：{checked}"
    assert all(item["n"] <= CandidatePool.CAP for item in loads)
    # 作废这件事本身确实在发生（不是"全靠 J 选中"把池子腾空的）
    expiries = sum(1 for item in loads for status in item["presets"].values()
                   if status == "expired")
    assert expiries >= 3, f"旧货必须真的在装载时被作废：expired {expiries} 条"

    # ④ 这条候选最终真的到了 J 手里（不是"活着但没人理"）
    assert director.pool.get(cand.candidate_id).status == "consumed", (
        "alive 却没被选中，说明时钟只是没死、链路仍然断着："
        f"{director.pool.status()}")

    print(f"[A6] 装载 {len(loads)} 次，seq 序列 {[i['seq'] for i in loads]}"
          "（= 单元序号，非句子计数器）")
    print(f"     {cand.candidate_id} 在 {cand.target_unit} 的装载点上仍 alive → 随后被选中"
          f"（状态 {director.pool.get(cand.candidate_id).status}）")
    print(f"     preset 旧货跨 {checked} 次装载都不再占位、累计作废 {expiries} 条，"
          f"池子 {director.pool.status()}")


# ===========================================================================
# A5) C 全程失败：课堂逐字无感，循环正常返回，无悬挂 task
# ===========================================================================

async def test_c_dead_the_whole_class_changes_nothing_on_the_stage():
    """§11：C 整个挂掉 → 学生"无感"。这里用**逐字一致**来定义"无感"。

    对照组是"装配了 J、但没有 C 的循环"那一节课：两者的区别只该是 C 有没有上货，
    既然 C 每次都抛，两边说出来的每一句话都必须一模一样。
    """
    control = classroom_with_director(make_long_script(16))
    say_confused_once(control, "S01-U01")       # 对照组的"学生"必须一模一样，否则逐字一致无从谈起
    await control.run()

    script2 = make_long_script(16)
    classroom = classroom_with_director(script2)
    say_confused_once(classroom, "S01-U01")     # 门卫只认真实输入产生的困惑信号
    proposer = ExplodingProposer()

    ticks = await run_with_agent_c(classroom, proposer=proposer,
                                   interval_s=INTERVAL_S, deck=make_deck())

    assert proposer.calls > 0, "门卫至少开过一次闸，否则异常路径根本没走到"
    assert ticks > proposer.calls, "一次失败之后循环必须继续（§11：下个周期重试）"
    assert classroom.machine._finished.is_set(), "C 挂掉不许把课堂停在半路"
    assert classroom.state.slide_index == script2.slide_count - 1

    def words(c) -> list[tuple[str, str]]:
        return [(u.speaker, u.text) for u in c.state.transcript]

    assert words(classroom) == words(control), "C 全挂与没有 C 的课堂必须逐字一致"
    assert classroom.state.seq == control.state.seq, "连句数都必须一样"
    assert agent_c_candidates(classroom.machine.director) == [], \
        "proposer 每次抛异常，池子里不该有 C 的货"

    # 失败必须落进 metrics：课堂上没人看得见 C，事后只能靠这几个事件复盘
    failed = [e for e in classroom.metrics.events
              if e["kind"] == "agent_c_tick_failed" and e.get("reason") == "proposer_error"]
    dead = [e for e in classroom.metrics.events if e["kind"] == "agent_c_dead"]
    assert failed, f"C 的每次失败都要记进 metrics：{classroom.metrics.events[-5:]}"
    assert not dead, "单次失败不算整个挂掉（§11：60s 后重试）"

    await assert_no_dangling_tasks()
    print(f"[A5] proposer 抛异常 {proposer.calls} 次（tick {ticks} 次），课堂 "
          f"{len(words(classroom))} 句逐字一致，metrics 记下 {len(failed)} 次失败，"
          "课后悬挂 task 0")



# 注：本文件末尾原来还有 5 条用例（A7）测的是旧 web.py 里 Runner 的 opt-in 装配与
# start()/close() 收尾。Runner 随 web.py 一起删除后，这几条没有可测的对象了 ——
# 同样的三件事（默认关零执行 / 只开 J 只装 J / C 的循环起得来又收得干净）由
# tests/test_runtime_assembly.py 的 A/B/C/D 用 director/assembly.py 与真状态机覆盖。
