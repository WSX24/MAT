"""预处理作业管理器（preprocess.py）的离线测试。

怎么测的、为什么这么测
----------------------
* **不调模型、不用那份 16MB 真课件**：整条链里只有 plan_llm 是模型，其余（解析、预置
  路线、写盘、备份）全是确定性代码 —— 所以只把 plan_llm 换成"规则版"，其余全真跑。
  解析用 tests/fixtures.make_pptx 现场生成的 2 页小 pptx：**真解析**而不是假造一份
  deck.json，因为假造的 deck.json 测不出"source.txt 指向 pptx/ 里的副本"这类接线错误
  （而那正是这一棒的产品口径）。
* **目录隔离**：SOURCES_DIR 与 DECKS_DIR 都指向 tmp_path。不隔离的话，跑一次测试就会往
  真实的 prototype/pptx/、prototype/decks/ 里塞进垃圾课件，而 decks/ 是要进版本库的
  产物目录（踩过一次，多出十几个 decks/test_*）。
* **过程也要断言**：只断言"最后 plan.json 有了"是不够的 —— 单飞、备份、stage 顺序、
  进度回调全都能在"结果正确"的表象下坏掉。所以 _enter / _progress 这两个钩子会被抓
  下来逐条核对。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as config_mod  # noqa: E402
import llm as llm_mod  # noqa: E402
import preprocess as pp  # noqa: E402
import run as run_mod  # noqa: E402
import script_gen  # noqa: E402
from deck import store as deck_store  # noqa: E402
from tests.fixtures import make_pptx  # noqa: E402


# ===========================================================================
# 夹具
# ===========================================================================

@pytest.fixture(autouse=True)
def _fresh_jobs():
    """作业表是模块级 dict：不清会漏到下一条用例里（list_jobs 的顺序断言会飘）。"""
    pp._jobs.clear()
    yield
    pp._jobs.clear()


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """源库、课件库都挪进 tmp_path；模型那一路换成离线替身。"""
    sources = tmp_path / "pptx"
    sources.mkdir()
    monkeypatch.setattr(pp, "SOURCES_DIR", sources)
    # preprocess 全程走 deck_store.deck_dir()/plan_json()，所以改这里的 DECKS_DIR 就够
    # （deck.pptx_ingest 也是同一个模块对象，解析产物跟着落到 tmp）。
    monkeypatch.setattr(deck_store, "DECKS_DIR", tmp_path / "decks")
    # describe() 是 plan_llm 记 model 字段用的（`llm.settings.describe()`），替身也要有，
    # 否则一旦某条用例没换掉 plan_llm，它会在记 model 那一行就崩掉，看着像"作业莫名失败"。
    monkeypatch.setattr(config_mod, "load_settings", lambda: SimpleNamespace(
        unit_char_warn=250, describe=lambda: "离线替身"))
    monkeypatch.setattr(llm_mod, "LLM", lambda settings: SimpleNamespace(settings=settings))
    # 路线文案润色要真调模型；离线测里给它 None = 只用规则文案（plan_routes 的正常降级档）。
    monkeypatch.setattr(run_mod, "_route_polisher", lambda settings, tag="plan": None)
    return SimpleNamespace(sources=sources, decks=tmp_path / "decks")


def _stub_plan_llm(monkeypatch, *, gate: asyncio.Event | None = None,
                   boom: bool = False) -> None:
    """把 plan_llm 换成"规则版"：真跑 plan_rule（确定性、零网络）。

    gate 用来把作业卡在备课这一格上（测单飞）；boom 用来模拟模型挂掉（测失败记录）。
    进度回调照 plan_llm 的真实顺序调，好让 _progress 那条断言有意义。
    """
    async def fake(deck, llm, *, slides=None, on_progress=None, char_warn=250):
        todo = [s for s in deck.slides if slides is None or s.index in slides]
        for n, s in enumerate(todo, 1):
            if on_progress:
                on_progress(n, len(todo), s)
        if gate is not None:
            await gate.wait()
        if boom:
            raise RuntimeError("模型挂了")
        return script_gen.plan_rule(deck, slides=slides)

    monkeypatch.setattr(script_gen, "plan_llm", fake)


def _collect(monkeypatch, name: str) -> list:
    """抓内部钩子的实参（_enter 的 stage / _progress 的 done,total）看过程。"""
    seen: list = []
    orig = getattr(pp, name)

    def wrapper(*args, **kwargs):
        seen.append(args[1:])
        return orig(*args, **kwargs)

    monkeypatch.setattr(pp, name, wrapper)
    return seen


# ===========================================================================
# 源库与上传
# ===========================================================================

def test_safe_name_keeps_chinese_and_strips_paths() -> None:
    """上传文件名安全化：目录部分、`..`、非法字符都要压掉，中文一个不能少。

    中文丢了不是"难看"而已：deck_id 是从文件名派生的，中文一丢所有课件都撞成同一个 deck。
    """
    assert pp._safe_name("../../etc/passwd") == "passwd"
    assert pp._safe_name("C:\\temp\\词汇 解析.pptx") == "词汇 解析.pptx"
    assert pp._safe_name("a..b.pptx") == "a.b.pptx"
    assert pp._safe_name("   ") == "upload.pptx"


def test_save_upload_overwrites_and_flags_replaced(sandbox) -> None:
    row = pp.save_upload("../../词汇 解析.pptx", b"PK\x03\x04v1")
    assert row["name"] == "词汇 解析.pptx"
    assert row["size"] == 6 and row["mtime"] > 0
    assert row["replaced"] is False and row["error"] is None
    assert (sandbox.sources / "词汇 解析.pptx").is_file()

    again = pp.save_upload("词汇 解析.pptx", b"v2")
    assert again["replaced"] is True
    assert (sandbox.sources / "词汇 解析.pptx").read_bytes() == b"v2"
    assert [r["name"] for r in pp.library()] == ["词汇 解析.pptx"]


def test_library_aggregates_deck_and_plan_state(sandbox) -> None:
    """library() 一眼看出：解析了没有、备了没有、几个单元、坏文件也不抛。"""
    (sandbox.sources / "good.pptx").write_bytes(b"x")
    (sandbox.sources / "bad.pptx").write_bytes(b"x")
    (sandbox.sources / "别的东西.png").write_bytes(b"x")     # 非 pptx 不进源库

    good = deck_store.deck_dir(deck_store.make_deck_id("good.pptx"))
    good.mkdir(parents=True)
    (good / "deck.json").write_text("{}", encoding="utf-8")
    (good / "plan.json").write_text(json.dumps(
        {"units": [{"slide": 0}, {"slide": 1}], "session_goals": ["看懂 pack 的用法"]},
        ensure_ascii=False), encoding="utf-8")

    bad = deck_store.deck_dir(deck_store.make_deck_id("bad.pptx"))
    bad.mkdir(parents=True)
    (bad / "plan.json").write_text("{这不是 json", encoding="utf-8")

    rows = {r["name"]: r for r in pp.library()}
    assert set(rows) == {"good.pptx", "bad.pptx"}
    assert rows["good.pptx"]["has_deck"] is True
    assert rows["good.pptx"]["has_plan"] is True
    assert rows["good.pptx"]["units"] == 2
    assert rows["good.pptx"]["has_goals"] is True
    assert rows["good.pptx"]["error"] is None
    assert rows["bad.pptx"]["units"] == 0 and rows["bad.pptx"]["has_goals"] is False
    assert "plan.json 读不动" in rows["bad.pptx"]["error"]


# ===========================================================================
# 三段流水线
# ===========================================================================

async def test_full_job_runs_three_stages(sandbox, monkeypatch) -> None:
    src = make_pptx(sandbox.sources / "demo 课件.pptx")
    _stub_plan_llm(monkeypatch)
    enters = _collect(monkeypatch, "_enter")
    progress = _collect(monkeypatch, "_progress")

    job = await pp.start_job("demo 课件.pptx")
    assert job["stage"] == "queued" and job["status"] == "running"
    done = await pp.wait_job(job["id"], timeout=60)

    assert done["status"] == "done", done
    assert done["stage"] == "done"
    assert [s for s, _ in enters] == ["ingest", "plan", "routes"], enters
    assert (1, 2) in [(d, t) for d, t, _ in progress], "备课的 (done,total) 必须进 progress"
    assert done["error"] is None
    assert done["backup"] is None, "第一次跑没有旧计划，不该凭空造一份备份"
    assert done["progress"] == {"done": 2, "total": 2, "label": "完成"}, done["progress"]
    assert 0 < len(done["log_tail"]) <= pp.LOG_LINES
    assert done["finished_at"] and done["started_at"]

    plan_path = deck_store.plan_json(done["deck_id"])
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert data["units"], "备课产物必须有单元"
    assert data["routes"] == [], "预置路线这一步必须真跑过（这份课件算不出岔路 → 空数组）"
    source_txt = (deck_store.deck_dir(done["deck_id"]) / "source.txt").read_text(encoding="utf-8")
    assert str(src) in source_txt, "source.txt 要指向源库里的副本，而不是用户上传前的位置"


async def test_ingest_only_stops_after_parse(sandbox, monkeypatch) -> None:
    make_pptx(sandbox.sources / "demo.pptx")
    job = await pp.start_job("demo.pptx", action="ingest")
    done = await pp.wait_job(job["id"], timeout=60)
    assert done["status"] == "done", done
    assert deck_store.deck_json(done["deck_id"]).exists()
    assert not deck_store.plan_json(done["deck_id"]).exists(), "ingest 不许顺手备课"


async def test_plan_action_needs_existing_deck(sandbox) -> None:
    (sandbox.sources / "demo.pptx").write_bytes(b"not really a pptx")
    job = await pp.start_job("demo.pptx", action="plan")
    assert job["status"] == "failed" and "deck.json" in job["error"]
    assert pp.get_job(job["id"]) is not None, "没跑起来的调用也要查得到（同形 dict）"


async def test_unknown_action_and_missing_source(sandbox) -> None:
    bad = await pp.start_job("x.pptx", action="乱来")
    assert bad["status"] == "failed" and "action" in bad["error"]
    miss = await pp.start_job("没有这个.pptx")
    assert miss["status"] == "failed" and "源库里没有" in miss["error"]


# ===========================================================================
# 三条硬约束：单飞 / 备份 / 失败记录
# ===========================================================================

async def test_single_flight(sandbox, monkeypatch) -> None:
    """同一份源同时只能有一个 running 作业：两个作业抢同一个 plan.json 必出事。"""
    make_pptx(sandbox.sources / "demo.pptx")
    gate = asyncio.Event()
    _stub_plan_llm(monkeypatch, gate=gate)

    first = await pp.start_job("demo.pptx")
    await asyncio.sleep(0)              # 让作业真跑起来，卡在备课那一格
    dup = await pp.start_job("demo.pptx")

    assert dup["status"] == "failed" and dup["error"], "重复点击必须返回带 error 的 dict，不抛"
    assert "正在预处理" in dup["error"] and first["id"] in dup["error"]
    assert pp.get_job(first["id"])["status"] == "running", "拒绝新作业不能把在跑的那个作废"

    gate.set()
    assert (await pp.wait_job(first["id"], timeout=60))["status"] == "done"
    again = await pp.start_job("demo.pptx", action="plan")
    assert again["error"] is None, "上一个跑完之后必须能再点"
    assert (await pp.wait_job(again["id"], timeout=60))["status"] == "done"


async def test_rerun_backs_up_old_plan(sandbox, monkeypatch) -> None:
    """覆盖写 plan.json 之前，旧的那份必须先落到 plan.bak.json。"""
    make_pptx(sandbox.sources / "demo.pptx")
    _stub_plan_llm(monkeypatch)
    first = await pp.wait_job((await pp.start_job("demo.pptx"))["id"], timeout=60)
    assert first["status"] == "done", first
    plan_path = deck_store.plan_json(first["deck_id"])
    assert plan_path.exists()

    # 换成一份"一看就知道是旧的"内容再重备：它必须先被备份下来
    plan_path.write_text(json.dumps({"marker": "旧计划"}, ensure_ascii=False), encoding="utf-8")
    again = await pp.wait_job(
        (await pp.start_job("demo.pptx", action="plan"))["id"], timeout=60)

    assert again["status"] == "done", again
    assert again["backup"] == "plan.bak.json", again["backup"]
    bak = deck_store.plan_bak_json(first["deck_id"])
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8")) == {"marker": "旧计划"}, "备份必须是旧内容"
    assert json.loads(plan_path.read_text(encoding="utf-8"))["units"], "新计划要真的覆盖上去"


async def test_failure_is_recorded_not_raised(sandbox, monkeypatch) -> None:
    """解析炸了也只能变成 status=failed：进程、事件循环、别的作业都得活着。"""
    from deck import pptx_ingest

    make_pptx(sandbox.sources / "demo.pptx")

    real = pptx_ingest.ingest_pptx

    def _boom(*args, **kwargs):
        raise RuntimeError("解析器炸了")

    monkeypatch.setattr(pptx_ingest, "ingest_pptx", _boom)
    _stub_plan_llm(monkeypatch)
    job = await pp.start_job("demo.pptx")
    done = await pp.wait_job(job["id"], timeout=60)

    assert done["status"] == "failed" and done["stage"] == "failed"
    assert "RuntimeError" in done["error"] and "解析器炸了" in done["error"]
    assert done["finished_at"], "失败也要有结束时间，否则前端永远当它在跑"
    assert done["log_tail"] and "❌" in done["log_tail"][-1]

    # 失败之后单飞锁必须放开：用户改好文件要能直接再点一次
    monkeypatch.setattr(pptx_ingest, "ingest_pptx", real)
    again = await pp.start_job("demo.pptx")
    assert again["error"] is None, again
    assert (await pp.wait_job(again["id"], timeout=60))["status"] == "done"


# ===========================================================================
# 部分备课与 job 查询
# ===========================================================================

async def test_partial_slides_keep_other_pages(sandbox, monkeypatch) -> None:
    """只备第 1 页时，别的页的旧单元必须活着 —— 否则"试一页"就是毁一节课。"""
    make_pptx(sandbox.sources / "demo.pptx")
    _stub_plan_llm(monkeypatch)
    first = await pp.wait_job((await pp.start_job("demo.pptx"))["id"], timeout=60)
    assert first["status"] == "done", first

    plan_path = deck_store.plan_json(first["deck_id"])
    old = json.loads(plan_path.read_text(encoding="utf-8"))
    old["units"].append({"slide": 1, "text": "第二页的旧单元，不许被抹掉。"})
    plan_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    part = await pp.wait_job(
        (await pp.start_job("demo.pptx", action="plan", slides=[0]))["id"], timeout=60)
    assert part["status"] == "done", part
    units = json.loads(plan_path.read_text(encoding="utf-8"))["units"]
    assert [u["slide"] for u in units] == sorted(u["slide"] for u in units), "单元要按页排好"
    assert any(u["slide"] == 1 and "不许被抹掉" in u["text"] for u in units)


async def test_plan_by_deck_id_after_source_removed(sandbox, monkeypatch) -> None:
    """源文件被删/改名后，用 deck_id 也要能重备课（否则"重跑"就彻底没入口了）。"""
    make_pptx(sandbox.sources / "demo.pptx")
    _stub_plan_llm(monkeypatch)
    first = await pp.wait_job((await pp.start_job("demo.pptx"))["id"], timeout=60)
    assert first["status"] == "done", first

    (sandbox.sources / "demo.pptx").unlink()
    job = await pp.start_job(first["deck_id"], action="plan")
    assert job["error"] is None, job
    done = await pp.wait_job(job["id"], timeout=60)
    assert done["status"] == "done", done
    assert done["backup"] == "plan.bak.json", "重备前必须留底"


async def test_job_lookup_and_list_order(sandbox, monkeypatch) -> None:
    assert pp.get_job("不存在") is None
    assert pp.list_jobs() == []

    make_pptx(sandbox.sources / "demo.pptx")
    _stub_plan_llm(monkeypatch)
    a = await pp.start_job("demo.pptx", action="ingest")
    b = await pp.start_job("demo.pptx", action="ingest")   # 撞单飞 → 一条 failed 记录

    assert b["status"] == "failed"
    assert [j["id"] for j in pp.list_jobs()] == [b["id"], a["id"]], "新的在前"
    assert len(pp.list_jobs(limit=1)) == 1
    assert pp.get_job(a["id"])["id"] == a["id"]
    assert (await pp.wait_job(a["id"], timeout=60))["status"] == "done"
