"""指标采集与脚本体检。

对应《课堂交互设计》§7.6 可观测性 与 §8 待验证清单。

三个必须观测的指标：
  - **让位延迟** yield_latency_ms：学生提问 → 开始作答的间隔（段末让位方案的核心代价，V1/V2）
  - **归位成功率**：让位后是否成功回到 LECTURING（I5 是否被真正遵守）
  - **探针响应率**：探针发出后被回应的比例（判断探针是否值得保留，V8）
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from state import ClassroomState, Question, now


# ===========================================================================
# 指标采集
# ===========================================================================

@dataclass
class MetricsTracker:
    speech_ms: list[float] = field(default_factory=list)      # 每句话的播放+合成耗时
    yield_ms: list[float] = field(default_factory=list)       # 让位延迟（核心指标）
    resumed_ok: int = 0
    resumed_fail: int = 0
    probe_sent: int = 0
    probe_confirmed: int = 0
    probe_interrupted: int = 0
    probe_timeout: int = 0
    advance_auto: int = 0
    advance_manual: int = 0
    marks: dict[str, float] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    # -- 打点 -------------------------------------------------------------
    def mark(self, key: str) -> None:
        self.marks[key] = now()

    def event(self, kind: str, **kw) -> None:
        self.events.append(
            {"kind": kind, "t": round(now(), 3), "at": datetime.now().isoformat(timespec="seconds"), **kw}
        )

    def on_speech(self, ms: float) -> None:
        self.speech_ms.append(ms)

    def on_answer_start(self, q: Question) -> None:
        """段末让位：学生提问 → 老师开始作答。

        从**学生按下发送**算起（不是从段末让位点算起）—— 这才是学生真正感知到的
        等待时长，也是段末让位方案的代价（V1/V2）。让位点落在音频边界这件事本身
        不需要测量。
        """
        q.yielded_at = now()
        ms = (q.yielded_at - q.asked_at) * 1000.0
        self.yield_ms.append(ms)
        self.event("yield", qid=q.qid, raw=q.raw, ms=round(ms, 1))

    def on_resume(self, ok: bool) -> None:
        if ok:
            self.resumed_ok += 1
        else:
            self.resumed_fail += 1

    def on_probe_open(self) -> None:
        self.probe_sent += 1

    def on_probe_close(self, result: str) -> None:
        if result == "confirmed":
            self.probe_confirmed += 1
        elif result == "interrupted":
            self.probe_interrupted += 1
        elif result == "timeout":
            self.probe_timeout += 1

    # -- 汇总 -------------------------------------------------------------
    def summary(self, state: ClassroomState) -> dict:
        def p50(xs: list[float]) -> float | None:
            return round(statistics.median(xs), 1) if xs else None

        def p90(xs: list[float]) -> float | None:
            if len(xs) < 2:
                return None
            ordered = sorted(xs)
            idx = min(len(ordered) - 1, int(len(ordered) * 0.9))
            return round(ordered[idx], 1)

        from dataclasses import asdict

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "course": state.course_id,
            # —— V1 / V2：让位延迟 ——
            "yield_latency": {
                "count": len(self.yield_ms),
                "p50_ms": p50(self.yield_ms),
                "p90_ms": p90(self.yield_ms),
                "max_ms": round(max(self.yield_ms), 1) if self.yield_ms else None,
                "samples_ms": [round(x, 1) for x in self.yield_ms],
            },
            # —— 归位 ——
            "resume": {
                "ok": self.resumed_ok,
                "fail": self.resumed_fail,
                "ok_rate": round(self.resumed_ok / max(1, self.resumed_ok + self.resumed_fail), 3),
            },
            # —— V8：探针是否值得保留 ——
            "probe": {
                "sent": self.probe_sent,
                "confirmed": self.probe_confirmed,
                "interrupted": self.probe_interrupted,
                "timeout": self.probe_timeout,
                "response_rate": round(
                    (self.probe_confirmed + self.probe_interrupted) / max(1, self.probe_sent), 3
                ),
            },
            "advance": {"auto": self.advance_auto, "manual": self.advance_manual},
            "speech_ms": {"count": len(self.speech_ms), "p50": p50(self.speech_ms)},
            "comprehension": dict(state.comprehension),
            "deferred": list(state.deferred),
            "q_rounds": dict(state.q_rounds),
            "transcript": [asdict(u) for u in state.transcript],
            "events": self.events,
        }

    def dump(self, state: ClassroomState, path: Path) -> dict:
        s = self.summary(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        return s


# ===========================================================================
# 脚本体检
# ===========================================================================

def audit_script(script, char_warn: int = 250) -> list[str]:
    """检查讲解单元长度是否符合《课堂交互设计》§2.3 的硬约束。

    超过 char_warn 意味着「学生最坏等待时长」过大 —— 段末让位方案的核心风险。
    """
    msgs: list[str] = []

    for u in script.units:
        n = len(u.text)
        sec = u.est_sec
        flag = ""
        if n > char_warn:
            flag = "  ❌ 超出硬上限"
        elif n > 180:
            flag = "  ⚠️  超过建议区间(80~180)"
        elif n < 40:
            flag = "  ⚠️  偏短(<40字)"
        msgs.append(f"[{u.unit_id}] {n:>4} 字 · 约 {sec:>5.1f} 秒{flag}")

    total = script.total_est_sec
    longest = max((u.est_sec for u in script.units), default=0.0)
    msgs.append("")
    msgs.append(f"单元数 {len(script.units)} · 总时长约 {total:.0f} 秒（{total / 60:.1f} 分钟）")
    msgs.append(f"最长单元 {longest:.1f} 秒 —— 即学生最坏等待时长")
    msgs.append(f"页数 {script.slide_count} · 每页单元数 " + ", ".join(
        f"P{s.index + 1}:{len(script.units_of(s.index))}" for s in script.slides
    ))

    # 结构校验
    for s in script.slides:
        us = script.units_of(s.index)
        if not us:
            msgs.append(f"❌ 第 {s.index + 1} 页没有任何讲解单元")
        elif not us[-1].is_page_end:
            msgs.append(f"❌ 第 {s.index + 1} 页的最后一个单元没有标记 is_page_end（探针不会触发）")
        for u in us[:-1]:
            if u.is_page_end:
                msgs.append(f"❌ [{u.unit_id}] 不是本页最后单元却标记了 is_page_end")

    for u in script.units:
        slide = script.slide_at(u.slide)
        if slide is None:
            msgs.append(f"❌ [{u.unit_id}] 指向不存在的第 {u.slide + 1} 页")
            continue
        if not u.anchors:
            msgs.append(f"⚠️  [{u.unit_id}] 没有锚点 —— 语音无法与课件元素对应")
        for a in u.anchors:
            if slide.element(a.element_id) is None:
                msgs.append(f"❌ [{u.unit_id}] 锚点 {a.element_id} 在课件第 {u.slide + 1} 页上不存在")
        for r in u.reveal:
            if slide.element(r) is None:
                msgs.append(f"❌ [{u.unit_id}] reveal 元素 {r} 在课件第 {u.slide + 1} 页上不存在")

    return msgs
