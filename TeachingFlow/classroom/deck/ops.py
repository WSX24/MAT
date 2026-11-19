"""课件操作（DeckOp）—— 让 AI 讲师"真正操作 PPT"的那条通道。

为什么需要它
------------
原型原本只有一种课件动作：讲到哪个单元就高亮它的锚点（`anchors`）。
这对"系统生成的课件"勉强够用，但对**真实教师 PPT**不够 ——
真实课件一页里有装饰色块、标题、正文、例句、配图，
讲师要能：先藏住例句（别提前剧透）→ 讲到时再放出来 → 指给全班看 →
把无关元素压暗 → 讲图时放大一块 → 讲完复位。

于是课件操作被提升为一等数据：每个讲解单元带一串 `DeckOp`，
每个 op 声明**在第几句开始时执行**（`at_sentence`，1 起算），
由状态机在句边界下发、前端执行。这样"说什么"和"指哪里"是同一条时间轴。

与 `anchors` 的分工
-------------------
- `anchors`：**语义**绑定（这段讲解对应课件上哪些元素），用于归位、台账、审核、降级高亮。
- `ops`：**动作**序列（藏/显/指/聚焦/缩放），用于视觉节奏。
两者可以都指向同一批元素，但目的不同 —— 不要合并。

约束（诚实记下）
----------------
`at_sentence` 只能精确到**句**。因为播放协议本来就是按句切片
（`machine._seg_unit` 一次产出一句），句内更细的同步（比如"读到这个词的时候"）
需要 TTS 带时间戳，原型没做。所以生成侧应当把"一句话一个动作"作为默认，
而不是幻想逐字同步。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields

#: 允许的操作。故意保持小而正交 —— 每多一个 op，前端、审核、测试都要跟着长。
OPS = (
    "reveal",       # 让元素/段落出现（配 reveal 计划使用，讲到了才给看）
    "hide",         # 收起来
    "step",         # = reveal + 其余压暗（"讲新的一个点"的常用组合）
    "highlight",    # 高亮（形状：发光框；段落：底色+左侧色条）
    "focus",        # 把 target 之外的元素压暗
    "unfocus",      # 解除压暗
    "point",        # 讲师"指"过去（指示器移动）
    "zoom",         # 放大到 target（讲图表/密集文字时用）
    "reset_zoom",
    "clear",        # 清掉高亮/压暗/指示器（翻页或换话题时兜底）
)

#: 需要 target 的 op（没给 target 就是无意义的，生成侧写错时审核会报出来）
NEEDS_TARGET = ("reveal", "hide", "step", "highlight", "focus", "point", "zoom")


@dataclass
class DeckOp:
    """一次课件操作。"""

    op: str = "highlight"
    #: 第 N 句开始时执行（1 起算）。默认 1：这段话一开始就做。
    at_sentence: int = 1
    #: **按文字定位**：讲到"含这段文字的那一句"时执行。
    #:
    #: 为什么还要有它：真正的"句"不是语法句，而是 `llm.split_sentences` 切出来的
    #: 播放分块（≥8 字、按句末标点切、过长再二次切），**还会把过短的句子并进下一句**。
    #: 让模型去数"这是第 2 句"几乎必然数错；让它抄一段自己刚写的文字则稳得多。
    #: 因此：LLM 规划用 `at_text`，规则兜底用 `at_sentence`（句号是算出来的，准）。
    at_text: str = ""
    target: str = ""
    targets: list[str] = field(default_factory=list)
    #: highlight 的模式：glow（默认，发光框）| box（只画框）
    mode: str = ""
    #: zoom 用的倍数
    scale: float = 0.0
    #: 给人看的备注（审核页会显示，不参与执行）
    note: str = ""

    # -- 便利 ---------------------------------------------------------------
    @property
    def anchor_ids(self) -> list[str]:
        """这次操作打在哪些锚点 id 上（target 与 targets 合并、去重保序）。"""
        out: list[str] = []
        for x in ([self.target] if self.target else []) + list(self.targets):
            if x and x not in out:
                out.append(x)
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        # 空字段不落盘，plan.json 更可读
        return {k: v for k, v in d.items() if v not in ("", [], 0.0) or k in ("op", "at_sentence")}

    @staticmethod
    def from_dict(d: dict) -> "DeckOp":
        known = {f.name for f in fields(DeckOp)}
        kw = {k: v for k, v in (d or {}).items() if k in known}
        if "targets" in kw and isinstance(kw["targets"], str):
            kw["targets"] = [kw["targets"]]
        if "target" not in kw and kw.get("targets"):
            kw["target"] = kw["targets"][0]
        if kw.get("at_text"):
            kw["at_text"] = str(kw["at_text"]).strip()
        try:
            kw["at_sentence"] = max(1, int(kw.get("at_sentence") or 1))
        except (TypeError, ValueError):
            kw["at_sentence"] = 1
        return DeckOp(**kw)

    @staticmethod
    def parse_many(xs) -> list["DeckOp"]:
        return [x if isinstance(x, DeckOp) else DeckOp.from_dict(x) for x in (xs or [])]


# ===========================================================================
# 校验（§2.7 可审核性：课件+脚本错了，后面全套机制都会忠实地讲错）
# ===========================================================================

def validate_ops(ops: list[DeckOp], anchor_ids: set[str], *, unit_id: str = "") -> list[str]:
    """检查一批 op 是否合法。返回问题列表（空 = 没问题）。

    只做**确定性**检查：op 名不认识、target 指向不存在的元素、句号超出范围。
    "这个 op 教学上合不合适"不是代码能判的，交给审核页给人看。
    """
    msgs: list[str] = []
    tag = f"[{unit_id}] " if unit_id else ""
    for i, op in enumerate(ops, 1):
        where = f"{tag}第 {i} 个 op"
        if op.op not in OPS:
            msgs.append(f"❌ {where}：不认识的操作 {op.op!r}（可选：{'/'.join(OPS)}）")
            continue
        if op.at_sentence < 1:
            msgs.append(f"❌ {where}：at_sentence={op.at_sentence}，必须从 1 起算")
        if op.op in NEEDS_TARGET and not op.anchor_ids:
            msgs.append(f"❌ {where}（{op.op}）没有 target")
        for a in op.anchor_ids:
            if a not in anchor_ids:
                msgs.append(f"❌ {where}（{op.op}）target {a} 在本页不存在")
    return msgs


def ops_at_sentence(ops: list[DeckOp], sentence_no: int) -> list[DeckOp]:
    """第 sentence_no 句（1 起算）开始时该执行哪些 op（只看 at_sentence）。

    句号大于本单元句数的 op 不执行 —— 那是生成侧算错了，
    静默丢掉会让"藏起来的东西永远不出现"，所以另有 `check_ops_in_range` 把它报出来。
    """
    return [op for op in ops if not op.at_text and op.at_sentence == sentence_no]


def _loose(s: str) -> str:
    """去掉空白与标点后比较 —— 模型抄原文时经常少一个逗号、多一个空格。"""
    return re.sub(r"[\s，。、；：！？,.;:!?…—\-—“”\"'（）()《》\[\]]", "", s or "")


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组，O(len(a)*len(b))）。

    只用来判断"模型这句话是不是从我这段正文里抄来的"，
    输入都在几十字量级，够快。
    """
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def match_sentence(sentences: list[str], at_text: str) -> tuple[int, str]:
    """把 `at_text` 定位到句号。返回 (句号, 匹配方式)。

    三种情况，从严到松：
      exact —— 原文子串，最可信
      loose —— 忽略空白标点后能对上（模型少个逗号而已）
      near  —— 与某句的最长公共子串 ≥4 字（模型改写了措辞，但明显指这一句）
      none  —— 对不上，调用方应记 ⚠️/❌（一律不静默）
    """
    t = (at_text or "").strip()
    if not t:
        return 0, "none"
    for i, s in enumerate(sentences, 1):
        if t in s:
            return i, "exact"
    lt = _loose(t)
    if not lt:
        return 0, "none"
    for i, s in enumerate(sentences, 1):
        if lt in _loose(s):
            return i, "loose"
    best, best_i = 0, 0
    for i, s in enumerate(sentences, 1):
        n = _lcs_len(lt, _loose(s))
        if n > best:
            best, best_i = n, i
    if best >= 4:
        return best_i, "near"
    return 0, "none"


def resolve_ops(ops: list[DeckOp], sentences: list[str], *,
                anchor_text: dict[str, str] | None = None) -> list[DeckOp]:
    """把 `at_text` 解析成句号，返回**句号已确定**的新 op 列表。

    三级降级，每一级都比上一级差一点，但都比"什么都不做"好：
      1. `at_text` 对上（exact / loose / near）
      2. `at_text` 对不上时，改用**目标元素自己的文字**去对 ——
         模型经常会改写措辞（"后接物品名词"），但它指的通常是课件上的某一段，
         而那段文字就在本单元正文里。这一级能把大部分"编出来的定位"救回来。
      3. 还不行才退回 at_sentence（默认第 1 句）—— 宁可早点显示，
         也不要"藏起来的东西永远不出现"。

    失败一律由 `check_ops_text` 在审核里报出来，不静默。
    """
    out: list[DeckOp] = []
    for op in ops:
        if not op.at_text:
            out.append(op)
            continue
        idx, _how = match_sentence(sentences, op.at_text)
        if not idx and anchor_text:
            own = (anchor_text.get(op.target) or "").strip()
            for probe in (own[:16], own[-16:] if len(own) > 16 else ""):
                if not probe:
                    continue
                idx, _how2 = match_sentence(sentences, probe)
                if idx:
                    break
        new = DeckOp(**{**op.to_dict(), "at_text": "", "at_sentence": idx or op.at_sentence})
        out.append(new)
    return out


def ops_due(resolved: list[DeckOp], sentence_no: int) -> list[DeckOp]:
    """已解析（无 at_text）的 op 列表里，第 sentence_no 句该执行哪些。"""
    return [op for op in resolved if op.at_sentence == sentence_no]


def check_ops_text(ops: list[DeckOp], text: str, *, unit_id: str = "") -> list[str]:
    """`at_text` 与正文对不上时报警。

    分成两档，因为两者的处理方式完全不同：
      ❌ none —— 这个操作永远不会发生（退回第 1 句执行，等于"一开始就做"）
      ⚠️ near/loose —— 能对上但不确定是不是同一句，值得人扫一眼
    """
    out: list[str] = []
    sents = _sentences(text)
    for op in ops:
        if not op.at_text:
            continue
        idx, how = match_sentence(sents, op.at_text)
        if how == "none":
            out.append(f"❌ [{unit_id}] op {op.op} 按文字定位到 {op.at_text!r}，"
                       f"本单元正文里找不到（已退回第 1 句执行）")
        elif how == "near":
            out.append(f"⚠️  [{unit_id}] op {op.op} 的定位文字 {op.at_text!r} "
                       f"不是原文子串，按近似匹配落到第 {idx} 句 —— 建议人工确认")
    return out


def _sentences(text: str) -> list[str]:
    try:
        from llm import split_sentences

        return split_sentences(text or "")
    except Exception:
        return [text or ""]


def check_ops_in_range(ops: list[DeckOp], n_sentences: int, *, unit_id: str = "") -> list[str]:
    """op 的句号超出单元句数 → 漏执行（通常是生成侧没数句子）。"""
    out: list[str] = []
    for op in ops:
        if op.at_sentence > n_sentences:
            out.append(f"❌ [{unit_id}] op {op.op} 在第 {op.at_sentence} 句执行，"
                       f"但本单元只有 {n_sentences} 句 —— 这个操作永远不会发生")
    return out


# ===========================================================================
# 常用组合（生成侧与规则兜底都用它，保证前后端口径一致）
# ===========================================================================

def reveal(targets: list[str], at_sentence: int = 1, *, focus: bool = True) -> DeckOp:
    return DeckOp(op="step" if focus else "reveal", at_sentence=at_sentence, targets=targets)


def highlight(targets: list[str], at_sentence: int = 1, *, mode: str = "") -> DeckOp:
    return DeckOp(op="highlight", at_sentence=at_sentence, targets=targets, mode=mode)


def point(target: str, at_sentence: int = 1) -> DeckOp:
    return DeckOp(op="point", at_sentence=at_sentence, target=target)


def focus(targets: list[str], at_sentence: int = 1) -> DeckOp:
    return DeckOp(op="focus", at_sentence=at_sentence, targets=targets)


def unfocus(at_sentence: int = 1) -> DeckOp:
    return DeckOp(op="unfocus", at_sentence=at_sentence)


def zoom(target: str, at_sentence: int = 1, *, scale: float = 1.7) -> DeckOp:
    return DeckOp(op="zoom", at_sentence=at_sentence, target=target, scale=scale)


def reset_zoom(at_sentence: int = 1) -> DeckOp:
    return DeckOp(op="reset_zoom", at_sentence=at_sentence)


def clear(at_sentence: int = 1) -> DeckOp:
    return DeckOp(op="clear", at_sentence=at_sentence)
