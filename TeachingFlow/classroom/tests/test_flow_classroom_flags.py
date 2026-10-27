"""flow.py 的课堂开关与 manifest 写入（不产片、不联网、不碰仓库 runs/）。

为什么用假的 Run 和假的 bridge，而不是真跑一次 flow：这里要钉的是"备课开关怎么算"
和"一份备课失败会不会拖垮整次运行"。那两件事与 S0~S5 无关，真跑一次要产片、要下载
配图，既慢又不稳 —— 而真正想防的错（开关算反了、失败把 produced 一起改了）都在
挂钩这一层。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# classroom/tests/ → classroom → TeachingFlow（flow.py 就在这儿，扁平导入要靠它）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import flow   # noqa: E402


class FakeRun:
    """只实现 _run_classroom 真正用到的那两个接口（manifest / note）。"""

    def __init__(self) -> None:
        self.dir = Path("runs") / "fake"
        self.manifest: dict = {"steps": {}, "notes": []}
        self.saved = 0

    def note(self, msg: str) -> None:
        self.manifest["notes"].append(msg)

    def save(self) -> None:
        self.saved += 1


def _args(**kv):
    base = {"mock": False, "render": False, "classroom": None}
    base.update(kv)
    return types.SimpleNamespace(**base)


def _stub_bridge(monkeypatch, results):
    """把 bridge 换成桩：记下每次调用，按文件名回预置结果。

    塞 sys.modules 就能生效，是因为挂钩处是**惰性** import bridge（这也是"不 import
    web.py、也就不需要 FastAPI"的前提）。
    """
    calls: list[tuple[str, dict]] = []
    mod = types.ModuleType("bridge")

    def preprocess_pptx_sync(src, **kw):
        calls.append((str(src), kw))
        return results.get(Path(src).name,
                           {"ok": True, "deck_id": "", "error": "", "stages": [], "stats": {}})

    mod.preprocess_pptx_sync = preprocess_pptx_sync
    monkeypatch.setitem(sys.modules, "bridge", mod)
    return calls


# ---------------------------------------------------------------------------
# 开关：三个参数 + auto 规则
# ---------------------------------------------------------------------------

def test_parser_has_the_three_classroom_flags() -> None:
    p = flow.build_parser()
    assert p.parse_args(["x"]).classroom is None, "不指定 = auto"
    assert p.parse_args(["x", "--classroom"]).classroom is True
    assert p.parse_args(["x", "--no-classroom"]).classroom is False
    assert p.parse_args(["x", "--classroom-serve"]).classroom_serve is True
    # 一句话需求变成可选：--classroom-serve 不需要文案
    assert p.parse_args(["--classroom-serve"]).text is None


def test_help_mentions_the_flags() -> None:
    text = flow.build_parser().format_help()
    for flag in ("--classroom", "--no-classroom", "--classroom-serve"):
        assert flag in text, f"帮助里看不到 {flag}"


@pytest.mark.parametrize("mock,render,forced,expect", [
    (True, False, None, False),     # auto：mock 默认不产片 → 没片子可送，不备课
    (True, True, None, True),       # auto：mock + --render → 会产片 → 备课
    (False, False, None, True),     # auto：真实调用 → 备课
    (True, False, True, True),      # --classroom 强制跑
    (False, False, False, False),   # --no-classroom 永不跑
])
def test_auto_rule_table(mock, render, forced, expect) -> None:
    args = _args(mock=mock, render=render, classroom=forced)
    assert flow._classroom_enabled(args) is expect


# ---------------------------------------------------------------------------
# 挂钩：manifest 写入 + 一份失败不阻断
# ---------------------------------------------------------------------------

def test_manifest_records_success_and_failure(tmp_path, monkeypatch, capsys) -> None:
    ok_pptx = str(tmp_path / "第1单元-成功.pptx")
    bad_pptx = str(tmp_path / "第2单元-失败.pptx")
    calls = _stub_bridge(monkeypatch, {
        "第1单元-成功.pptx": {"ok": True, "deck_id": "第1单元_成功", "error": "",
                             "stages": ["queued", "ingest", "done"],
                             "stats": {"slides": 12, "units": 9, "has_deck": True}},
        "第2单元-失败.pptx": {"ok": False, "deck_id": "",
                             "error": "源文件不存在：/nope/第2单元-失败.pptx",
                             "stages": [], "stats": {}},
    })
    run = FakeRun()
    run.manifest["produced"] = [ok_pptx, bad_pptx]
    units = {ok_pptx: "ch01-u1", bad_pptx: "ch01-u2"}

    records = flow._run_classroom([ok_pptx, bad_pptx], units, run, enabled=True)

    assert len(calls) == 2, "两份 PPTX 都要送到 bridge，一份失败不许中断循环"
    assert run.manifest["classroom"] is records, "manifest 上要能看到同一份记录"
    first, second = records
    assert first["unit_id"] == "ch01-u1" and first["pptx"] == ok_pptx
    assert first["ok"] is True and first["deck_id"] == "第1单元_成功"
    assert first["stages"] == ["queued", "ingest", "done"]
    assert first["stats"]["slides"] == 12
    assert second["unit_id"] == "ch01-u2"
    assert second["ok"] is False and "不存在" in second["error"]
    assert "课堂备课失败" in run.manifest["notes"][0], run.manifest["notes"]
    # 已产出的片一个字都没被改
    assert run.manifest["produced"] == [ok_pptx, bad_pptx]
    assert "✓" in capsys.readouterr().out


def test_bridge_crash_is_caught(tmp_path, monkeypatch) -> None:
    """哪怕 bridge 自己炸了（它承诺不抛），挂钩也必须记账完事，不往上冒。"""
    def boom(src, **kw):
        raise RuntimeError("模拟 bridge 内部炸了")

    mod = types.ModuleType("bridge")
    mod.preprocess_pptx_sync = boom
    monkeypatch.setitem(sys.modules, "bridge", mod)

    run = FakeRun()
    records = flow._run_classroom([str(tmp_path / "单元.pptx")], {}, run, enabled=True)
    assert len(records) == 1
    assert records[0]["ok"] is False
    assert "模拟 bridge 内部炸了" in records[0]["error"]
    assert any("课堂备课失败" in n for n in run.manifest["notes"])


def test_bridge_import_failure_is_only_a_warning(tmp_path, monkeypatch) -> None:
    """缺 python-pptx（import 就失败）也只是告警：本次运行的片照样是成功的。"""
    monkeypatch.setitem(sys.modules, "bridge", None)     # 让 import bridge 直接失败
    run = FakeRun()
    records = flow._run_classroom([str(tmp_path / "单元.pptx")], {}, run, enabled=True)
    assert records == []
    assert run.manifest["classroom"] == []
    assert any("课堂备课不可用" in n for n in run.manifest["notes"])


def test_no_pptx_means_no_bridge_call(tmp_path, monkeypatch, capsys) -> None:
    """mock 默认不产片：一份 PPTX 都没有时不该去调备课，但要说明一句。"""
    calls = _stub_bridge(monkeypatch, {})
    run = FakeRun()
    assert flow._run_classroom([], {}, run, enabled=True) == []
    assert calls == []
    assert run.manifest["classroom"] == []
    assert "没有产出 PPTX" in capsys.readouterr().out


def test_disabled_never_calls_bridge(tmp_path, monkeypatch, capsys) -> None:
    calls = _stub_bridge(monkeypatch, {})
    run = FakeRun()
    flow._run_classroom([str(tmp_path / "单元.pptx")], {}, run, enabled=False)
    assert calls == []
    assert run.manifest["classroom"] == []
    assert "--no-classroom" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --classroom-serve：透传参数、子进程启动
# ---------------------------------------------------------------------------

def test_pop_classroom_serve_takes_everything_after_it() -> None:
    assert flow._pop_classroom_serve(["--mock"]) is None, "没这个开关就不该动命令行"
    assert flow._pop_classroom_serve(["--classroom-serve"]) == []
    assert flow._pop_classroom_serve(
        ["--classroom-serve", "--", "serve", "--no-browser"]) == ["serve", "--no-browser"]
    assert flow._pop_classroom_serve(
        ["--classroom-serve", "decks"]) == ["decks"], "不带 -- 也照样透传"


def test_serve_classroom_uses_a_subprocess(monkeypatch) -> None:
    """必须是子进程：import 课堂的 web.py 会把 FastAPI/edge-tts 拖进这次运行。"""
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw

        class R:
            returncode = 7
        return R()

    monkeypatch.setattr(flow.subprocess, "run", fake_run)
    code = flow._serve_classroom(["serve", "--no-browser"])
    assert code == 7, "退出码要原样透传"
    cmd = seen["cmd"]
    assert Path(cmd[0]).name.lower().startswith("python"), cmd[0]
    assert Path(cmd[1]).name == "run.py"
    assert str(Path(cmd[1]).parent) == str(flow.CLASSROOM_DIR)
    assert cmd[2:] == ["serve", "--no-browser"]
    assert seen["kw"]["cwd"] == str(flow.CLASSROOM_DIR)


def test_main_routes_classroom_serve_before_parsing(monkeypatch) -> None:
    """--classroom-serve 必须在 argparse 之前被拦下，否则透传参数会报 unrecognized。"""
    seen: list = []

    def fake_serve(argv):
        seen.append(argv)
        return 5

    monkeypatch.setattr(flow, "_serve_classroom", fake_serve)
    assert flow.main(["--classroom-serve", "--", "decks"]) == 5
    assert seen == [["decks"]]


def test_empty_input_still_errors(capsys) -> None:
    """text 改成可选之后，"什么都不给"仍然要报缺输入，不是悄悄跑一遍空链路。"""
    assert flow.main([]) == 2
    assert "缺用户输入" in capsys.readouterr().out
