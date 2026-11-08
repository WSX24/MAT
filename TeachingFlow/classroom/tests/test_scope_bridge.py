# -*- coding: utf-8 -*-
"""课堂建课线必须走 TeachingFlow 的 S0（core/scope）：范围/周期/强度只认一处口径。

回归的 bug（2026-11-07）：
  · 「我想快速学习初中生物光合作用实验课」被排成 6 周 × 3 课时 = 18 课时、3 章 12 单元
    —— 因为课堂自己写了一套只要「N 周/月/天」的正则，把「快速」当成了没说。
  · 判读口径本该只有一份（core/scope.py，flow.py 走的就是它）。
"""
from __future__ import annotations

import course_plan
import scope_bridge

QUICK = "我想快速学习初中生物光合作用实验课"
SYSTEMATIC = "我想系统学习高中语文"


def test_bridge_loads_teachingflow_scope():
    assert scope_bridge.available(), "core/scope.py 应该能被装起来"


def test_quick_words_are_short_term():
    hint = scope_bridge.hints(QUICK)
    assert hint is not None
    assert hint["weeks"] == 1, "「快速」必须按最小闭环（1 周）排"
    assert hint["term"] == "短期"
    assert hint["effort"] == "轻"
    assert hint["stage"] == "初中"
    assert hint["subject"] == "生物" and hint["subject_known"]


def test_requirements_follow_scope():
    req = course_plan.normalize_requirements({"course_title": "初中生物光合作用实验课"}, QUICK)
    assert req["weeks"] == 1
    assert req["hours_per_week"] <= course_plan.LIGHT_HOURS_PER_WEEK
    assert req["term"] == "短期"
    assert req["effort"] == "轻"
    assert req["subject"] == "生物"
    assert "1 周" in req["weeks_source"]


def test_explicit_weeks_still_wins():
    req = course_plan.normalize_requirements({}, "我想快速学习光合作用，给我 3 周")
    assert req["weeks"] == 3, "用户明说的周期优先于强度词"


def _long_outline(weeks: int = 6, per_week: int = 3, **_ignored) -> dict:
    chapters = []
    for title in ("实验设计基础", "经典光合作用实验分析", "实验结论的规范表述与应试"):
        chapters.append({
            "title": title, "summary": "",
            "units": [{"title": f"{title}·第{i}课", "type": "新授", "hours": 1.0,
                       "summary": "", "knowledge_points": [f"{title}点{i}"]}
                      for i in range(1, 5)],
        })
    return {"course_title": "初中生物光合作用实验课", "subject": "生物", "stage": "初中",
            "weeks": weeks, "hours_per_week": per_week, "chapters": chapters}


def test_outline_is_capped_by_budget():
    req = course_plan.normalize_requirements({}, QUICK)
    out = course_plan.normalize_outline(_long_outline(), req)
    cap = course_plan.unit_capacity(req["weeks"], req["hours_per_week"])
    assert out["unit_count"] <= cap, f"课时预算只够 {cap} 个单元，却排了 {out['unit_count']} 个"
    assert out["units_merged"] > 0
    # 合并后 id 必须重编成 chNN-um（flow.py --unit 认的就是这个）
    ids = [u["id"] for c in out["chapters"] for u in c["units"]]
    assert ids == [f"ch{c['number']:02d}-u{u['number']}" for c in out["chapters"] for u in c["units"]]
    assert len(set(ids)) == len(ids)
    # 内容不丢：知识点都还在
    points = [p for c in out["chapters"] for u in c["units"] for p in u["knowledge_points"]]
    assert len(points) == 12


def test_long_course_is_not_capped():
    req = course_plan.normalize_requirements({}, SYSTEMATIC)
    out = course_plan.normalize_outline(_long_outline(weeks=10, hours_per_week=3), req)
    assert out["unit_count"] == 12
    assert not out.get("units_merged")


def test_offline_outline_respects_quick():
    req = course_plan.normalize_requirements({"course_title": "初中生物光合作用实验课"}, QUICK)
    out = course_plan.offline_outline(req)
    assert out["weeks"] == 1
    assert out["unit_count"] <= course_plan.unit_capacity(req["weeks"], req["hours_per_week"])
