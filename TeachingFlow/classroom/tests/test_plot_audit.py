# -*- coding: utf-8 -*-
"""函数图像页的「切线几何」审计与自动重画（PPT-Agent/plot_audit.py）。

回归的 bug（2026-11-08 导数课）：
  · 第 8 页标着「斜率 2」的切线，端点折成数学斜率 1.67，还整体掉到切点下方 21px；
  · 同册第 11/12 页也有同类偏移。
根因：SVG 坐标是模型逐页手写的，而 P4 的 svg_quality_checker 只查排版、不查数学。
修法：用曲线自身的局部斜率把线挪回「过切点 + 斜率一致」——纯算术，0 次模型调用。

这里按文件路径加载（plot_audit 在 PPT-Agent/ 下，不在课堂的 sys.path 上），
和 studio/catalog.py 加载 core.naming 是同一手法。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[3] / "PPT-Agent" / "plot_audit.py"


@pytest.fixture(scope="module")
def plot_audit():
    spec = importlib.util.spec_from_file_location("_tf_plot_audit", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_check(plot_audit):
    plot_audit.self_check()


def test_axis_and_construction_lines_are_skipped(plot_audit):
    """轴是灰色、辅助线是浅色，都不能被当成切线（第 3 页 Δx 构造线的误报来源）。"""
    assert plot_audit._is_pale("#DCE3EC")      # 坐标轴
    assert plot_audit._is_pale("#B9C4D2")      # Δx/Δy 构造线
    assert not plot_audit._is_pale("#2F6FE0")  # 强调色（曲线/切线）


def test_secant_is_not_touched(plot_audit):
    """割线两端都在曲线上，斜率本来就该和切线不同 —— 不能改。"""
    svg = plot_audit._PAGE8_SVG.replace(
        'x1="887" y1="586.2" x2="1215" y2="422.6"',
        'x1="927.5" y1="544.7" x2="1007.4" y2="485.0"')
    assert plot_audit.repair_svg(svg) == (svg, [])


def test_reports_offset_and_slope_error(plot_audit):
    issues, stats = plot_audit.audit_svg(plot_audit._PAGE8_SVG)
    assert stats["curve_points"] >= 60 and stats["markers"] == 1
    assert len(issues) == 1
    issue = issues[0]
    assert issue["offset_px"] > 10          # 偏了十几像素
    assert 0.45 < abs(issue["drawn_slope"]) < 0.55
    assert 0.55 < abs(issue["expected_slope"]) < 0.65   # 曲线局部斜率
    assert "切线" in issue["message"]


def test_repair_makes_tangent_touch_point(plot_audit):
    fixed, notes = plot_audit.repair_svg(plot_audit._PAGE8_SVG)
    assert notes and "切线重画" in notes[0]
    assert plot_audit.audit_svg(fixed)[0] == []
    lines = plot_audit._lines(fixed, plot_audit._parse(fixed))
    dists = [plot_audit._dist_point_to_line(927.5, 544.7, ln["x1"], ln["y1"],
                                           ln["x2"], ln["y2"]) for ln in lines]
    assert min(dists) < 0.5, "修完必须过切点"
