"""对比两份备课计划（A/B 试提示词、试模型时用）。

用法：
    .\\.venv\\Scripts\\python.exe tools/compare_plans.py <deck_id_A> <deck_id_B>

为什么要单独比：备课是**可以迭代的**（改提示词、换模型、重备某几页），
但"新的一定更好吗"不能凭感觉。这里把两次备课的关键数字并排打出来：

    单元数 / 总时长 / 最长单元 / 超硬上限数 / 锚点命中率 / 段落级占比
    / 操作总数 / 非法操作 / 操作类型分布

其中**操作类型分布**是最容易被忽略的一栏：第一版 160 个操作全是
step/highlight，`focus`/`zoom` 一个都没有 —— 不看分布就发现不了。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(deck_id: str) -> dict:
    p = ROOT / "decks" / deck_id / "plan.json"
    if not p.exists():
        raise SystemExit(f"找不到 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def summary(d: dict) -> dict:
    units = d.get("units") or []
    cov = d.get("coverage") or {}
    kinds: Counter[str] = Counter()
    at_text = at_sentence = 0
    for u in units:
        for op in u.get("ops") or []:
            kinds[str(op.get("op") or "")] += 1
            if op.get("at_text"):
                at_text += 1
            if op.get("at_sentence") is not None:
                at_sentence += 1
    anchors = sum(len(u.get("anchors") or []) for u in units)
    low = sum(1 for u in units for a in (u.get("anchors") or [])
              if float(a.get("confidence", 1.0)) < 0.6)
    chars = sum(len(u.get("text") or "") for u in units)
    est = sum(len(u.get("text") or "") for u in units) / 5.0
    longest = max((len(u.get("text") or "") for u in units), default=0) / 5.0
    return {
        "模型": d.get("model", ""),
        "单元数": len(units),
        "总字数": chars,
        "总时长(分)": round(est / 60, 1),
        "最长单元(秒)": round(longest, 1),
        "超硬上限": cov.get("units_over_hard_max", "?"),
        "锚点数": anchors,
        "段落级锚点": cov.get("para_level_anchors", "?"),
        "低置信锚点": low,
        "锚点命中率": (f"{cov.get('anchor_hit_rate', 0) * 100:.1f}%"
                       if cov else "?"),
        "操作总数": sum(kinds.values()),
        "非法操作": cov.get("ops_invalid", "?"),
        "at_text / at_sentence": f"{at_text} / {at_sentence}",
        "操作分布": " / ".join(f"{k} {v}" for k, v in kinds.most_common()) or "（无）",
        "体检告警": len(d.get("warnings") or []),
    }


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    a, b = summary(load(sys.argv[1])), summary(load(sys.argv[2]))
    width = max(len(k) for k in a)
    print(f"{'':<{width}}  {'A: ' + sys.argv[1][:28]:<34}  B: {sys.argv[2][:28]}")
    print("-" * (width + 72))
    for k in a:
        mark = "" if str(a[k]) == str(b[k]) else "  ←"
        print(f"{k:<{width}}  {str(a[k]):<34}  {str(b[k])}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
