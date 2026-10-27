"""变体生产：给每个讲解单元造 v1（换说法）、v2（举例子）。

**已停产**（产品决策，代码暂留）
--------------------------------
就地变体与被学生喊"没懂"时状态机**实时调模型重讲**冲突，而且按产品口径它从来就不该算分支。
备课期现在产出的是**预置路线分支**（跨多格、有岔出与汇回，见 agent/routes.py 与
director/route.py），plan.json 的顶层 routes 是它落地的地方。
本模块**不删实现**：老的 plan.json 里可能还带着变体（向后兼容要能读），
`python run.py variants` 也保留（行为不变，只多打一句停产声明），老流程仍跑得动。
新代码不要再往这里加"再多造几档变体"这类功能 —— 分支的载体是 route，不是 variant。

对应《双脑三体方案设计》§4.1（`UnitVariant`）、§5.3 流程第 3 步、§5.4 硬约束。
v0 是 `PlanUnit.text` 本身，不复制进 `variants`（见 §4.1 的向后兼容理由）。

为什么是规则模板，而不是 LLM
----------------------------
§12 阶段 1 的门槛写在"能不能真跑起来"上，明确"不设质量门槛：变体可以是规则模板、
可以是原话复述、可以是粗糙改写，只要不是空的"。规则模板换来三件 LLM 换不来的东西：
确定性（同样输入同样输出，测试能断言）、零网络（离网也能备课）、零成本（每个单元都造）。
阶段 1.5 换成 LLM 生成时只替换本模块的实现，`attach_variants` 的调用方不用动。

变体只用**原文自己的句子**重排
------------------------------
凭空造句必然造出与课件无关的内容——学生听到的是"老师突然讲别的了"。所以这里的
改动面只有两件事：句子顺序 + 引导语。文本仍然是这一单元的句子，相关性由构造保证，
而"不得是空串"由 `_fit` 保证（预算再紧也要塞进第一句）。
"""

from __future__ import annotations

from deck.ops import DeckOp
from llm import split_sentences
from state import estimate_seconds

#: v1 / v2 的人读标签（§4.1 的 label 字段）
VARIANT_LABELS: tuple[str, str] = ("换说法", "举例子")

#: 分句口径与 `llm.split_sentences` 一致：它要求满 8 字才认一个句末标点，
#: 所以比这更短的文本根本构不成一句，也就没有"另一种讲法"可言。
_MIN_SOURCE_CHARS = 8

#: 生成侧自己守的字数上限。看起来和 `validate_variant(char_warn=250)` 重复，
#: 但生成侧不守的话，每个超长单元都会"生成 → 被拒"白跑一遍。
#: 留 10 字余量给拼接与引导语。
_BUDGET = 240

#: 句末标点（与 `llm.SENT_END` 同口径：这一句念完了）。
#: 还带上半角 .!? —— 课件原文常把例句写成英文句号，不认它就会拼出"trip.。"这种双标点。
_END_PUNCT = "。！？；….!?"
#: 句子被切在逗号/顿号上时先剃掉，否则补句号会得到"……，。"
_TAIL_STRIP = "，,、：: "

_REPHRASE_LEADS = ("换句话说，", "换个说法，", "说白了，", "也就是说，")
_EXAMPLE_LEADS = ("举个例子，", "比如，", "打个比方，")
#: 原文里像"例句"的句子——v2 把它提到最前，先给例子再给解释
_EXAMPLE_CUES = ("例句", "例如", "举例", "比如", "example", "Example")


def _pick(leads: tuple[str, ...], seed: str) -> str:
    """确定性地挑一个引导语：用字符码求和取模。

    刻意不用内置 `hash()` —— 它按进程加盐，同一份课件两次运行会给出不同的引导语，
    "确定性"这条验收就假了。
    """
    return leads[sum(ord(c) for c in seed) % len(leads)]


def _sentence_texts(text: str) -> list[str]:
    """原句列表（补齐句末标点，好直接拼接）。"""
    out: list[str] = []
    for s in split_sentences(text):
        s = s.strip().rstrip(_TAIL_STRIP)
        # 纯标点片段（规则兜底把"；"。这种残渣留在正文里）不是一句话，丢掉：
        # 留着会在变体开头拼出"比如，。"这种一眼能看出是机器干的东西。
        if not s or not any(c.isalnum() for c in s):
            continue
        out.append(s if s[-1] in _END_PUNCT else s + "。")
    return out


def _fit(lead: str, sents: list[str]) -> str:
    """把句子按顺序塞进字数预算。

    超预算的句子**整句**丢弃（切成半句念起来更别扭）；但连第一句都放不下时硬截断——
    变体不能只剩一句引导语，那不是"换了个说法"，是丢掉了内容。
    """
    out = lead
    for s in sents:
        if len(out) + len(s) <= _BUDGET:
            out += s
            continue
        if out == lead:
            out += s[: max(0, _BUDGET - len(lead) - 1)] + "…"
        break
    return out


def _rephrase(sents: list[str]) -> str:
    """v1（换说法）：用引导语起头，原句按原序跟上。

    引导语先让学生意识到"同一件事要换个说法了"——课堂上这句话就是让位点的提示词。
    """
    return _fit(_pick(_REPHRASE_LEADS, sents[0]), sents)


def _example_first(sents: list[str]) -> str:
    """v2（举例子）：把像例句的那句提前先念——先给例子，再回来说明。

    找不到例句线索就拿末句顶（规则兜底切出来的单元，末句多半是用法/结论，
    提到前面正好是"再具体一点"的落点）。全是原句，只是顺序变了。
    """
    idx = next((i for i, s in enumerate(sents) if any(c in s for c in _EXAMPLE_CUES)), -1)
    if idx < 0:                 # 原文没有例句线索：拿末句顶
        idx = len(sents) - 1
    ordered = [sents[idx]] + [s for i, s in enumerate(sents) if i != idx]
    return _fit(_pick(_EXAMPLE_LEADS, sents[0]), ordered)


def make_variants(text: str, *, kp: list[str] | None = None) -> list[dict]:
    """为一个单元造 v1、v2，返回 `UnitVariant` 的 dict 形式；造不出来就返回空列表。

    纯确定性函数：不读时钟、不联网、不用随机数、不用 `hash()`。

    `kp` 目前不参与文本生成：知识点 id 是机器串（`P02-pack-1`），念出来学生听不懂。
    参数留在签名里，是因为阶段 1.5 换成 LLM 生成时要用它做提示，那时不必再改调用方。
    """
    raw = (text or "").strip()
    if len(raw) < _MIN_SOURCE_CHARS:
        return []
    sents = _sentence_texts(raw)
    if not sents:
        return []
    out: list[dict] = []
    for i, (label, body) in enumerate(zip(VARIANT_LABELS, (_rephrase(sents), _example_first(sents))),
                                    start=1):
        body = body.strip()
        if not body:
            continue
        out.append({
            "variant_id": f"v{i}",
            "label": label,
            "text": body,
            "anchors": [],      # 空 = 沿用主单元（§4.1）
            "ops": [],          # 空 = 沿用主单元（§4.1）
            "est_sec": estimate_seconds(body),
        })
    return out


def validate_variant(v: dict, slide, *, char_warn: int = 250) -> list[str]:
    """用**和 v0 完全相同**的尺子体检一个变体（§5.4），返回问题列表（空 = 通过）。

    §5.4 点名的风险场景：v0 合规（≤250 字），某个变体却是 600 字的巨兽 ——
    选中它，学生就干等两分钟。所以三条一条都不能少，且口径与 `audit_plan` 一致：
    锚点集合取 `SlideDoc.anchor_index()`（段落 pid 也在里面，见 `slide_from_deck`），
    句数取 `llm.split_sentences`（= 状态机逐句下发的分块）。
    """
    msgs: list[str] = []
    where = f"变体 {v.get('variant_id') or '?'}（{v.get('label') or '无标签'}）"

    text = str(v.get("text") or "")
    n = len(text)
    if not text.strip():
        msgs.append(f"❌ {where} 文本为空 —— 选中它学生只能干等")
    elif n > char_warn:
        msgs.append(f"❌ {where} 文本 {n} 字，超出上限 {char_warn} 字（§5.4）")

    index = slide.anchor_index()
    for a in v.get("anchors") or []:
        aid = str(a.get("element_id") or "") if isinstance(a, dict) else ""
        if not aid:
            msgs.append(f"❌ {where} 有锚点没写 element_id")
        elif aid not in index:
            msgs.append(f"❌ {where} 锚点 {aid} 在本页不存在")

    try:
        ops = DeckOp.parse_many(v.get("ops") or [])
    except Exception:      # 变体是 dict，来路不正的 ops 不该把整份体检炸掉
        msgs.append(f"❌ {where} 的 ops 解析不了，无法核对句号")
        return msgs
    n_sent = len(split_sentences(text))
    for op in ops:
        if op.at_sentence > n_sent:
            msgs.append(f"❌ {where} 的 op {op.op} 在第 {op.at_sentence} 句执行，"
                        f"但本变体只有 {n_sent} 句 —— 这个操作永远不会发生")
    return msgs


def attach_variants(plan, deck, *, char_warn: int = 250) -> int:
    """为计划里每个单元生成并体检变体，只挂通过的那几个，返回实际挂上的总数。

    被拒的原因要进 `plan.warnings` 而不是静默丢弃：变体是课堂运行时"换个说法"的
    唯一来源，少了几个单元没人会发现 —— 但审核页会把 warnings 印出来，人就看得见。
    """
    attached = 0
    for u in plan.units:
        slide = deck.slide_at(u.slide)
        if slide is None:
            plan.warnings.append(f"[P{u.slide + 1:02d}] 课件里没有这一页，跳过变体生成")
            continue
        kept: list[dict] = []
        for v in make_variants(u.text, kp=list(u.kp)):
            problems = validate_variant(v, slide, char_warn=char_warn)
            if problems:
                plan.warnings.extend(f"[P{u.slide + 1:02d}] {m}" for m in problems)
                continue
            kept.append(v)
        u.variants = kept
        attached += len(kept)
    return attached
