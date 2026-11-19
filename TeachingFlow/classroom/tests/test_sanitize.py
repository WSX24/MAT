"""净化器测试 —— 它守着"能被念出来"这条底线，必须确定性地工作。

用真实模型跑出来的脏输出当样本（见 dbg_live.py 的实测结果）。
运行：  python tests\\test_sanitize.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import clamp_spoken, drop_boilerplate, sanitize_spoken  # noqa: E402

#: 从 ds-ins 的实际输出里抄下来的样本 → (原始, 期望)
CASES: list[tuple[str, str]] = [
    # 标题整行丢弃（模型在模仿 prompt 结构）
    ("# 教师答\n判别式的符号决定了方程的根的性质。",
     "判别式的符号决定了方程的根的性质。"),
    ("# 约束\n这里的 a 是二次项系数，它不能等于零。",
     "这里的 a 是二次项系数，它不能等于零。"),
    ("# 学生问\n那为什么不直接看判别式为零的情况呢？",
     "那为什么不直接看判别式为零的情况呢？"),
    # 标题里带内容 → 保留内容
    ("# 教师答：判别式大于零时有两个实数根。", "判别式大于零时有两个实数根。"),
    ("**教师答**", ""),
    # markdown 强调与列表
    ("- 判别式大于零：两个不等实根。\n- 判别式等于零：两个相等实根。",
     "判别式大于零：两个不等实根。 判别式等于零：两个相等实根。"),
    ("1. 先算 Δ。\n2. 再看符号。", "先算 Δ。 再看符号。"),
    ("**判别式**决定根的个数。", "**判别式**决定根的个数。"),
    # 代码栅栏
    ("```python\nprint(1)\n```\n我们接着看这一页。", ""),
    # 空话式串场（状态机自己会负责重入话术，撞车就是复读）
    ("好，我们接着看这一页。", ""),
    ("好的，我们接着看这一页。", ""),     # 实测漏过一次：黑名单只认"好，"不认"好的，"
    ("嗯，我们接着往下看。", ""),
    ("我们接着往下讲。", ""),
    ("回到刚才的位置。", ""),
    # 正常内容不能被误删
    ("判别式 b² - 4ac 决定了一元二次方程有没有实数根。",
     "判别式 b² - 4ac 决定了一元二次方程有没有实数根。"),
    ("这个式子里，a 不能等于零。", "这个式子里，a 不能等于零。"),
    ("我们可以接着往下看具体例子。", "我们可以接着往下看具体例子。"),
    ("好的，先看一次项系数。", "好的，先看一次项系数。"),   # 有实义的"好的"不能删
    ("我们继续看判别式。", "我们继续看判别式。"),
    # 空输入
    ("", ""),
    ("   \n  \n", ""),
    # 实测样本：模型把 prompt 自身抄进正文（ds-ins 真实输出）
    ("当前任务：学生说没听懂，这是第 1 轮重讲，策略是「换说法」\n"
     "一元二次方程的判别式，你可以把它想象成一个天平。",
     "一元二次方程的判别式，你可以把它想象成一个天平。"),
    ("关键 只针对卡住的那一小块，不要重讲整页 2~4 句话，说完让学生有个着落",
     ""),
    # 模型给回答裹的直引号要去掉；中文引号是正经引用，必须保留
    ('"这里为什么要先看 Δ 的符号？', "这里为什么要先看 Δ 的符号？"),
    ("“判别式就是根号里的那一部分。”", "“判别式就是根号里的那一部分。”"),
    # 旁白式括号
    ("这个式子（停顿）决定了解的情况。", "这个式子决定了解的情况。"),
]


def test_sanitize_samples() -> None:
    bad = []
    for raw, want in CASES:
        got = sanitize_spoken(raw)
        if got != want:
            bad.append((raw, want, got))
    assert not bad, "净化结果不符:\n" + "\n".join(
        f"  输入 {r!r}\n  期望 {w!r}\n  实际 {g!r}" for r, w, g in bad
    )
    print(f"[S1] 净化样本 {len(CASES)} 条全部符合")


def test_clamp_respects_sentence_boundary() -> None:
    long = "第一句话在这里。第二句话稍微长一点点。第三句话更长了。"
    got = clamp_spoken(long, 14)
    assert got.endswith("。"), f"应在句末标点处截断: {got!r}"
    assert len(got) <= 14, f"超长: {got!r}"
    # 短文本原样返回
    assert clamp_spoken("很短。", 40) == "很短。"
    print(f"[S2] 长度截断 {got!r}")


def test_clamp_hard_cuts_when_no_punctuation() -> None:
    got = clamp_spoken("完全没有标点的一段很长很长的文字", 8)
    assert len(got) <= 8, f"超长: {got!r}"
    print(f"[S3] 无标点时硬截断 {got!r}")


def test_strict_one_liner_falls_back_on_fragments() -> None:
    """过渡/重入话术：宁可退回预置话术，也不放残句过去。

    实测 ds-ins 会给出 "接下来，我们讨论的方程式是 a(x²) +" 这种半句，
    截断之后听起来就是"老师说了半句话"。
    """
    from llm import LLM, Settings
    import asyncio

    fb = "这个问题问得好，先停一下。"
    llm = LLM.__new__(LLM)          # 不连模型，只测 _one_liner 的收尾逻辑

    async def fake_chat(*_a, **_k):
        return fake_chat.reply

    llm.chat = fake_chat
    cases = [
        # (模型回复, 期望输出)
        ("接下来，我们讨论的方程式是 a(x²) +", fb),   # 残句（悬空运算符）→ 预置话术
        ("我们先看这个" + "很长的补充说明" * 4 + "。", fb),  # 长度超限 → 预置话术
        ("这个 a 不能等于零", fb),                     # 没有句读 → 预置话术
        ("好，我先说说这个。", "好，我先说说这个。"),   # 正常，原样采用
        ("这个点先停一下，我们看看。", "这个点先停一下，我们看看。"),
        # 实测翻车样本（让位话术）：半个引号粘着句号，读出来是"…等于零引号"
        ('Δ 不能等于零"。', fb),
        ("判别式决定根的个数。", "判别式决定根的个数。"),   # 这句是好的，要放过
        ("我们先看这个（情况一。", fb),                 # 括号没闭
        ("这个问题问得好，。", fb),                     # 标点连排
        ("好，我们回到刚才那里，", fb),                 # 停在逗号
        ("好，我们把**这一段**看完。", fb),             # markdown 残留
        # "好，我们接着看。" 在套话黑名单里（重入话术由状态机自己说，
        # 模型若也说这句，学生听到的就是同一句话连着两遍）→ 净化后为空 → 预置话术
        ("好，我们接着看。", fb),
    ]
    bad = []
    for reply, want in cases:
        fake_chat.reply = reply
        got = asyncio.run(llm._one_liner("x", fb, max_chars=22, strict=True))
        if got != want:
            bad.append(f"  回复 {reply!r}\n    期望 {want!r}\n    实际 {got!r}")
    assert not bad, "严格模式不符:\n" + "\n".join(bad)
    print(f"[S4] 严格话术 {len(cases)} 条全部符合")


def test_drop_boilerplate_after_splitting() -> None:
    """按行过滤漏掉的情况：多行被拼成一行之后，套话句藏在句子中间。

    实测泄漏：回答首句是"好，我们接着看这一页。" —— 而重入话术由状态机自己说，
    学生听到的就是同一句话连着两遍。
    """
    got = drop_boilerplate([
        "好，我们接着看这一页。",
        "判别式的符号决定根的个数。",
        "我们接着往下讲。",
        "好的，先看一次项系数。",
    ])
    assert got == ["判别式的符号决定根的个数。", "好的，先看一次项系数。"], got
    print(f"[S5] 切句后去套话 剩 {len(got)} 句")


def test_degraded_llm_never_waits() -> None:
    """连不上模型时，不能让课堂去等一个注定失败的连接。

    实测：ollama 没起时，让位话术要等 8 秒才出来（就是等连接超时），
    学生看到的是"老师愣了半天"。
    正确行为：预检判定不可用之后，话术立刻用预置的，回答立刻走兜底；
    服务恢复后自动解除降级（`chat` 成功一次就 mark_up）。
    """
    from llm import Ctx, LLM
    from script import build_script

    ctx = Ctx(script=build_script(), slide_index=0, slide_title="测试",
              slide_text="", unit_id="S1-U01", unit_text="测试单元", anchors_hint="")

    llm = LLM.__new__(LLM)
    llm.degraded = False
    llm.down_reason = ""
    llm._last_probe = 0.0

    calls = {"n": 0}

    async def fake_chat(*_a, **_k):
        calls["n"] += 1
        return "好，我先说说这个。"

    llm.chat = fake_chat
    assert asyncio.run(llm.yield_phrase(ctx, "q")) == "好，我先说说这个。"
    assert calls["n"] == 1, "正常状态应当真的去问模型"

    llm.mark_down("x")
    assert llm.degraded

    # 降级后：话术走预置，且**一次都不去连**
    before = calls["n"]
    assert asyncio.run(llm.yield_phrase(ctx, "q")) == "这个问题问得好，先停一下。"
    assert asyncio.run(llm.resume_phrase(ctx, "q")) == "我们接着往下看。"

    async def drain(gen):
        return [x async for x in gen]

    try:
        asyncio.run(drain(llm.answer_stream("q", "clarify", ctx)))
        raise AssertionError("降级状态下 answer_stream 应当报错（让上层走兜底话术）")
    except Exception as e:
        assert "不可用" in str(e), f"错误信息要能看懂：{e}"
    assert calls["n"] == before, f"降级后不该再去连模型（实际又连了 {calls['n'] - before} 次）"

    # 课上到一半服务挂了：连接类错误要**立刻**进降级，
    # 否则接下来每一次提问都要再等一个完整超时
    from openai import APIConnectionError

    from config import ProviderProfile

    fresh = LLM.__new__(LLM)
    fresh.degraded = False
    fresh.down_reason = ""
    fresh._last_probe = 0.0
    fresh.profile = ProviderProfile("ollama", "http://127.0.0.1:59999/v1", "x", "m")
    assert not fresh.degraded
    fresh._hint(APIConnectionError(request=None))
    assert fresh.degraded, "连接类失败必须立刻进降级"

    # 非连接类（比如模型名不对）不该误判成"服务挂了"
    other = LLM.__new__(LLM)
    other.degraded = False
    other.down_reason = ""
    other._last_probe = 0.0
    other.profile = fresh.profile
    other._hint(ValueError("模型名不对"))
    assert not other.degraded, "非连接类错误不该进降级"

    print("[S6] 降级后不等模型 · 连接类失败立刻降级 · 其余错误不误判")


ALL = [
    test_sanitize_samples,
    test_clamp_respects_sentence_boundary,
    test_clamp_hard_cuts_when_no_punctuation,
    test_strict_one_liner_falls_back_on_fragments,
    test_drop_boilerplate_after_splitting,
    test_degraded_llm_never_waits,
]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
    print(f"\n净化层结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
