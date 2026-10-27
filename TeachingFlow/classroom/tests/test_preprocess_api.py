"""预处理 Web 接口（/api/preprocess/*）与 /preprocess 页的测试。

怎么测、为什么这么测
--------------------
* **不调真模型、不用那份 16MB 真课件**：解析、写盘、备份、路线全是确定性代码，只把
  plan_llm 换成规则版、把 LLM 换成回固定 JSON 的替身。解析用 tests/fixtures.make_pptx
  现场生成的小 pptx（真解析），因为"source.txt 指向哪条路径"这类接线错误只有真解析
  才测得出来。
* **目录隔离**：SOURCES_DIR 与 DECKS_DIR 都指向 tmp_path。不隔离的话跑一次测试就会往
  真实的 prototype/pptx/、prototype/decks/ 里塞垃圾课件，而 decks/ 是要进版本库的。
* **端点怎么调**：项目没装 httpx（fastapi.testclient 用不了），端点本来就是纯 async
  函数，asyncio.run(web.api_xxx(...)) 直接 await（照 tests/test_hub.py:16-17）。
  返回 dict 的端点是 200，返回 JSONResponse 的才带 .status_code —— _payload 抹平这层。
* 本文件钉的是**形状与接缝**：library 的字段、upload 的 400、start 的"被拒仍同形"、
  job 的 404，以及三条真会静默出事的路径 —— 补总结丢 routes、source.txt 回退、
  **上传后立刻点「预处理（全链）」**（前端发显示名 × 后端只认带后缀的全名，两边一撞
  就是"源库里没有"，而这条主路径此前一条用例都没有）。前端那一半用 node 真跑
  pickName 实测，不做"HTML 里有这行字"这种软断言。
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402
import llm as llm_mod  # noqa: E402
import preprocess as pp  # noqa: E402
import run as run_mod  # noqa: E402
import script_gen  # noqa: E402
import web  # noqa: E402
from deck import store as deck_store  # noqa: E402
from tests.fixtures import make_deck, make_pptx  # noqa: E402


# ===========================================================================
# 夹具与替身
# ===========================================================================

@pytest.fixture(autouse=True)
def _fresh_jobs():
    """作业表是模块级 dict：不清会漏到下一条用例里。"""
    pp._jobs.clear()
    yield
    pp._jobs.clear()


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """源库、课件库都挪进 tmp_path；模型那一路换成离线替身。"""
    sources = tmp_path / "pptx"
    sources.mkdir()
    monkeypatch.setattr(pp, "SOURCES_DIR", sources)
    monkeypatch.setattr(deck_store, "DECKS_DIR", tmp_path / "decks")
    monkeypatch.setattr(config_mod, "load_settings", lambda: SimpleNamespace(
        unit_char_warn=250, describe=lambda: "离线替身"))
    monkeypatch.setattr(llm_mod, "LLM", lambda settings: SimpleNamespace(settings=settings))
    # 路线文案润色要真调模型；离线测里给 None = 只用规则文案（plan_routes 的正常降级档）。
    monkeypatch.setattr(run_mod, "_route_polisher", lambda settings, tag="plan": None)
    return SimpleNamespace(sources=sources, decks=tmp_path / "decks", root=tmp_path)


def _stub_plan_llm(monkeypatch) -> None:
    """把 plan_llm 换成"规则版"：真跑 plan_rule（确定性、零网络）。"""
    async def fake(deck, llm, *, slides=None, on_progress=None, char_warn=250):
        return script_gen.plan_rule(deck, slides=slides)

    monkeypatch.setattr(script_gen, "plan_llm", fake)


class FakeLLM:
    """总结模型替身：只回答一句"这一页在讲什么"。

    backfill_summaries 只关心 chat 的返回串；给一句合法 JSON 就够了。
    """

    def __init__(self, reply: str = '{"summaries":[{"i":1,"summary":"这一页在讲 pack 的搭配"}]}',
                 *, raises: Exception | None = None) -> None:
        self.reply, self.raises, self.calls = reply, raises, 0

    async def chat(self, messages, *, max_tokens=800, json_mode=False, **kw):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.reply


def _upload(filename: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, size=len(data))


def _payload(res):
    """端点可能返回 dict（FastAPI 自动 200）或 JSONResponse（带状态码），这里抹平。"""
    if hasattr(res, "body"):
        return res.status_code, json.loads(res.body)
    return 200, res


def _run_full(sandbox, name: str = "demo.pptx", action: str = "full", **kw) -> dict:
    """开一个作业并等它跑完（作业本体是后台任务，测试里自己等）。"""
    async def _go():
        job = await pp.start_job(name, action=action, **kw)
        assert job["status"] in ("running", "failed"), job
        if job["status"] == "failed":
            return job
        return await pp.wait_job(job["id"], timeout=120)

    return asyncio.run(_go())


def _plan_with_routes(sandbox, monkeypatch, *, routes: int = 3) -> tuple[str, Path, int]:
    """跑一次全链拿到 plan.json，再改造成"存量课件"：有 routes、一条 summary 都没有。"""
    make_pptx(sandbox.sources / "demo.pptx")
    _stub_plan_llm(monkeypatch)
    done = _run_full(sandbox)
    assert done["status"] == "done", (done["status"], done["error"])
    path = deck_store.plan_json(done["deck_id"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["routes"] = [{"kind": "skip", "from": i, "to": i + 1, "label": f"支线{i}"}
                     for i in range(routes)]
    for u in raw["units"]:
        u.pop("summary", None)          # 存量课件：0 条总结（走向图卡面是空的）
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return done["deck_id"], path, len(raw["units"])


# ===========================================================================
# 页面
# ===========================================================================

def test_page_serves_preprocess_html():
    """GET /preprocess：经 _page 处理（no-store + 静态资源带 ?v=），三个区块都在。"""
    res = asyncio.run(web.preprocess_page())
    html = res.body.decode("utf-8")
    assert res.status_code == 200
    assert "no-store" in res.headers.get("cache-control", "")
    assert 'href="/static/classroom.css?v=' in html, "样式必须带版本号，否则新页面配旧样式"
    assert 'accept=".pptx"' in html, "上传框要限定 pptx"
    for marker in ('id="rows"', 'id="log"', 'id="bar"', 'id="active"'):
        assert marker in html, f"页面缺了 {marker}"
    # 每秒轮询只在作业 running 时发生（setTimeout 链）；无条件定时器会空转烧 CPU
    assert "setInterval" not in html


#: 页面里那个纯函数的名字：按钮的 data-name 由它决定（见 test_page_pick_name_*）
PICK = "pickName"


def _page_html() -> str:
    return (ROOT / "web" / "preprocess.html").read_text(encoding="utf-8")


def _js_function(html: str, name: str) -> str:
    """把页面里的一个纯函数（含函数体）原样抠出来，交给 node 跑。

    为什么按花括号配平而不是写死行号：行号一改就失效，配平至少能在被抠坏时报错。
    """
    start = html.index(f"function {name}(")
    i = html.index("{", start)
    depth = 0
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start:j + 1]
    raise AssertionError(f"{name} 的花括号没闭合 —— 页面被改坏了")


def test_page_buttons_send_payload_per_action():
    """三个按钮的 data-name 必须由 pickName 按 action 算出来。

    上传后点「预处理（全链）」必失败的第一半就在这里：行是 merge 出来的（显示名来自
    deck.title，已经去掉了 .pptx），而 full/ingest 要的是源文件全名。这条只钉接线
    ——"pickName 到底返回什么"由下面那条 node 用例真跑。
    """
    html = _page_html()
    assert f"function {PICK}(r, act)" in html, "按 action 取字段的纯函数没了"
    for act in ("plan", "full", "summarize"):
        line = [ln for ln in html.splitlines() if f"'{act}', {PICK}(r, '{act}')" in ln]
        assert len(line) == 1 and "btn(" in line[0], \
            f"{act} 那个按钮没有走 {PICK}(r, '{act}')（一半的失败根因就在这里）"
    # full 那一行尤其不许回退成 deck_id：上传件未解析时 deck_id 指向的文件不存在
    full_line = [ln for ln in html.splitlines() if "'full', pickName(r, 'full')" in ln][0]
    assert "deckId" not in full_line, "full 又发回 deck_id 了 —— 就是那条'源库里没有'"


def test_page_pick_name_runs_in_node(tmp_path):
    """真跑一遍 pickName：按 action 取不同字段，四种行形态各自的答案。

    node 实测而不是浏览器：这条钉的是纯字符串分支，起浏览器只会让 CI 偶尔红。
    """
    node = shutil.which("node")
    if node is None:                       # 没装 node 的机器上退化成"只跑结构断言"
        pytest.skip("没有 node，跳过页面 JS 实测")

    row_upload = {"deck": None,                   # 上传了还没解析：只有 sources 那一条
                  "src": {"name": "高一物理第一课：运动的描述.pptx",
                          "deck_id": "高一物理第一课_运动的描述"}}
    row_deck = {"deck": {"deck_id": "高一物理第一课_运动的描述",
                         "title": "高一物理第一课：运动的描述",
                         "source_name": "高一物理第一课：运动的描述.pptx",
                         "source_in_library": True},
                "src": {"name": "高一物理第一课：运动的描述.pptx",
                        "deck_id": "高一物理第一课_运动的描述"}}
    row_src_txt = {"deck": {"deck_id": "仓库根目录那份",
                            "title": "仓库根目录那份",
                            "source_name": "老位置.pptx",
                            "source_in_library": False,
                            "source_resolved": True},
                   "src": None}                 # 源不在 pptx/：只能靠 source.txt
    cases = [
        (row_upload, "full", "高一物理第一课：运动的描述.pptx", "未解析也要发源文件全名"),
        (row_upload, "ingest", "高一物理第一课：运动的描述.pptx", "ingest 与 full 同档"),
        (row_upload, "plan", "高一物理第一课_运动的描述", "plan 只认 deck_id"),
        (row_upload, "summarize", "高一物理第一课_运动的描述", "summarize 只认 deck_id"),
        (row_deck, "full", "高一物理第一课：运动的描述.pptx", "已解析行同样发全名"),
        (row_deck, "plan", "高一物理第一课_运动的描述", "已解析行 plan 发 deck_id"),
        (row_src_txt, "full", "仓库根目录那份", "源不在库：交回 deck_id 让后端查 source.txt"),
    ]
    script = tmp_path / "pick_name.js"
    js_cases = json.dumps([{"row": c[0], "act": c[1]} for c in cases], ensure_ascii=False)
    script.write_text(
        _js_function(_page_html(), PICK) + "\n"
        f"const cases = {js_cases};\n"
        "console.log(JSON.stringify(cases.map(c => pickName(c.row, c.act))));\n",
        encoding="utf-8")
    out = subprocess.run([node, str(script)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == [c[2] for c in cases], \
        [(g, c[2], c[3]) for g, c in zip(got, cases) if g != c[2]]


# ===========================================================================
# /api/preprocess/library
# ===========================================================================

def test_library_lists_decks_even_when_sources_is_empty(sandbox):
    """pptx/ 空、decks/ 有课件 → sources=[] 但 decks 非空。

    这就是仓库里那份真实课件的处境（源 pptx 在仓库根目录）：只读源库的页面会在
    decks/ 明明有一份能上课的课件时显示"空课件库"，而补总结只有它能点。
    """
    deck = make_deck()
    deck_store.save_deck(deck)
    deck_store.plan_json(deck.deck_id).write_text(json.dumps({
        "units": [{"slide": 0, "text": "a", "summary": "一句话"},
                  {"slide": 1, "text": "b"},
                  {"slide": 2, "text": "c"}],
        "session_goals": ["整节课的总览"],
    }, ensure_ascii=False), encoding="utf-8")

    code, lib = _payload(asyncio.run(web.api_preprocess_library()))
    assert code == 200 and lib["ok"] is True
    assert lib["sources"] == [], "pptx/ 是空的"
    assert len(lib["decks"]) == 1

    row = lib["decks"][0]
    assert row["deck_id"] == deck.deck_id
    assert row["units"] == 3 and row["summarized"] == 1
    assert row["has_goals"] is True, "session_goals 非空 = 整节课有总览"
    assert row["has_bak"] is False
    assert row["source_in_library"] is False and row["source_resolved"] is False
    assert "has_summary" not in row, "单元级 summary 上线后，老名字必须彻底消失"
    assert lib["jobs"] == []


def test_library_bad_plan_only_fills_error(sandbox):
    """坏 plan.json 只填 error，不许把整页课件库带崩。"""
    deck = make_deck()
    deck_store.save_deck(deck)
    deck_store.plan_json(deck.deck_id).write_text("{这不是 json", encoding="utf-8")

    _, lib = _payload(asyncio.run(web.api_preprocess_library()))
    row = lib["decks"][0]
    assert row["units"] == 0 and row["summarized"] == 0
    assert "plan.json 读不动" in row["error"]


# ===========================================================================
# /api/preprocess/upload
# ===========================================================================

def test_upload_ok_lands_in_source_library(sandbox):
    src = make_pptx(sandbox.root / "demo.pptx")
    data = src.read_bytes()

    code, body = _payload(asyncio.run(web.api_preprocess_upload(_upload("demo.pptx", data))))
    assert code == 200 and body["ok"] is True
    assert body["row"]["name"] == "demo.pptx" and body["row"]["replaced"] is False
    assert (sandbox.sources / "demo.pptx").read_bytes() == data
    assert [r["name"] for r in pp.library()] == ["demo.pptx"]


def test_upload_rejects_non_pptx_with_400(sandbox):
    """后缀不对 → 400 + 人话，且**一个字节都不落盘**。"""
    code, body = _payload(asyncio.run(web.api_preprocess_upload(_upload("讲义.doc", b"PK\x03\x04"))))
    assert code == 400, "这不是 500：用户传错文件是常态"
    assert body["ok"] is False and ".pptx" in body["error"]
    assert list(sandbox.sources.iterdir()) == []


def test_upload_rejects_oversize_with_400(sandbox, monkeypatch):
    """超限 → 400，且不落盘（真实上限 200MB，这里把常量压小来测同一条路）。"""
    monkeypatch.setattr(pp, "MAX_UPLOAD_MB", 1)
    code, body = _payload(asyncio.run(
        web.api_preprocess_upload(_upload("demo.pptx", b"x" * (1024 * 1024 + 1)))))
    assert code == 400
    assert body["ok"] is False and "上限" in body["error"]
    assert list(sandbox.sources.iterdir()) == []


# ===========================================================================
# /api/preprocess/start 与 /api/preprocess/job/{id}
# ===========================================================================

def test_start_single_flight_is_ok_false_with_failed_job(sandbox, monkeypatch):
    """同一份源正在跑 → ok=false，但 job 形状不变、状态 failed（不是 500）。"""
    make_pptx(sandbox.sources / "demo.pptx")
    gate = asyncio.Event()

    async def fake(deck, llm, *, slides=None, on_progress=None, char_warn=250):
        await gate.wait()
        return script_gen.plan_rule(deck, slides=slides)

    monkeypatch.setattr(script_gen, "plan_llm", fake)

    async def _go():
        first = await web.api_preprocess_start({"name": "demo.pptx", "action": "full"})
        await asyncio.sleep(0)                 # 让作业真跑起来
        dup = await web.api_preprocess_start({"name": "demo.pptx", "action": "full"})
        gate.set()
        await pp.wait_job(first["job"]["id"], timeout=120)
        return first, dup

    first, dup = asyncio.run(_go())
    assert first["ok"] is True and first["job"]["status"] == "running"
    assert dup["ok"] is False
    assert dup["job"]["status"] == "failed" and "正在预处理" in dup["job"]["error"]


def test_start_missing_source_is_ok_false(sandbox):
    code, body = _payload(asyncio.run(
        web.api_preprocess_start({"name": "没有这个.pptx", "action": "full"})))
    assert code == 200, "被拒不是请求错误"
    assert body["ok"] is False and "源库里没有" in body["job"]["error"]


def test_start_full_right_after_upload_without_deck(sandbox, monkeypatch):
    """**上传之后立刻点「预处理（全链）」**：必须能起。

    这条是用户实测踩到的那条主路径，此前一条用例都没有：上传件还没解析，decks/<id>/
    根本不存在，于是"按文件名找"与"按 deck.json 找"两条路同时不成立 —— 而页面当时
    发的又正是显示名（去掉 .pptx 的 title）。两边各修一处，这里钉的是它们对上之后的行为。
    """
    src = make_pptx(sandbox.root / "高一物理第一课：运动的描述.pptx")
    _stub_plan_llm(monkeypatch)
    _, up = _payload(asyncio.run(web.api_preprocess_upload(
        _upload("高一物理第一课：运动的描述.pptx", src.read_bytes()))))
    assert up["ok"] is True, up
    name = up["row"]["name"]
    row = pp.library()[0]
    assert (row["name"], row["has_deck"], row["has_plan"]) == (name, False, False), row

    async def _go():
        res = await web.api_preprocess_start({"name": name, "action": "full"})
        return res, await pp.wait_job(res["job"]["id"], timeout=120)

    res, done = asyncio.run(_go())
    assert res["ok"] is True, res
    assert res["job"]["status"] == "running" and res["job"]["error"] is None, res["job"]
    assert done["status"] == "done", (done["status"], done["error"])
    assert done["deck_id"] == "高一物理第一课_运动的描述"
    assert deck_store.deck_json(done["deck_id"]).exists(), "解析产物真的落到 decks/ 了"


def test_start_full_accepts_display_name_without_extension(sandbox, monkeypatch):
    """只给显示名（后缀没了、全角标点还在）也能起：_resolve 补后缀/按派生 id 认人。

    显示名是 deck.title 的形态（`高一 物理：第一课`），和盘上的 `高一 物理：第一课.pptx`
    只差一个后缀。前端已经改成一律发全名，这条钉的是后端不再"一字之差就当没这个文件"。
    """
    make_pptx(sandbox.sources / "高一 物理：第一课.pptx")
    _stub_plan_llm(monkeypatch)

    async def _go():
        res = await web.api_preprocess_start({"name": "高一 物理：第一课", "action": "full"})
        return res, await pp.wait_job(res["job"]["id"], timeout=120)

    res, done = asyncio.run(_go())
    assert res["job"]["status"] != "failed", res["job"]
    assert res["ok"] is True, res
    assert done["status"] == "done", (done["status"], done["error"])


def test_job_endpoint_reports_real_job_then_404_for_unknown(sandbox):
    make_pptx(sandbox.sources / "demo.pptx")

    async def _go():
        job = (await web.api_preprocess_start({"name": "demo.pptx", "action": "ingest"}))["job"]
        live = await web.api_preprocess_job(job["id"])
        done = await pp.wait_job(job["id"], timeout=120)
        gone = await web.api_preprocess_job("不存在的作业")
        return live, done, gone

    live, done, gone = asyncio.run(_go())
    assert live["ok"] is True and live["job"]["id"] == done["id"]
    assert done["status"] == "done", done
    code, body = _payload(gone)
    assert code == 404 and body["ok"] is False and body["error"]


# ===========================================================================
# summarize：写盘必须透传 routes（丢了没有任何报错，只是支线凭空消失）
# ===========================================================================

def test_summarize_fills_summary_and_keeps_routes(sandbox, monkeypatch):
    did, path, nunits = _plan_with_routes(sandbox, monkeypatch)
    before = json.loads(path.read_text(encoding="utf-8"))
    assert len(before["routes"]) == 3 and nunits > 0
    assert all("summary" not in u for u in before["units"])

    fake = FakeLLM()
    monkeypatch.setattr(llm_mod, "LLM", lambda settings: fake)

    async def _sum():
        job = await pp.start_job(did, action="summarize")
        assert job["error"] is None, job
        return await pp.wait_job(job["id"], timeout=120)

    done = asyncio.run(_sum())
    assert done["status"] == "done", done
    assert done["backup"] == "plan.bak.json", "覆盖写之前必须留底"

    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["units"]) == nunits, "补总结不许动单元数"
    assert [r["label"] for r in after["routes"]] == ["支线0", "支线1", "支线2"], \
        "routes 被丢掉了 —— 走向图上那几条支线会凭空消失，而且没有任何报错"
    assert all(str(u.get("summary") or "").strip() for u in after["units"])
    assert fake.calls > 0, "真去问过模型（而不是只做规则截断）"

    bak = json.loads(deck_store.plan_bak_json(did).read_text(encoding="utf-8"))
    assert bak["routes"] == before["routes"], "备份必须是补之前那份（routes 也在）"
    assert all("summary" not in u for u in bak["units"])


def test_summarize_without_force_keeps_existing_summary(sandbox, monkeypatch):
    """不 force：已有总结原样保留（人工润色过的那句不能被规则截断顶掉）。"""
    did, path, _ = _plan_with_routes(sandbox, monkeypatch)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["units"][0]["summary"] = "人工写过的总结，不许被覆盖"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(llm_mod, "LLM", lambda settings: FakeLLM())

    done = _run_full(sandbox, did, "summarize")
    assert done["status"] == "done", done
    units = json.loads(path.read_text(encoding="utf-8"))["units"]
    assert units[0]["summary"] == "人工写过的总结，不许被覆盖"


def test_summarize_force_rewrites_existing_summary(sandbox, monkeypatch):
    """force=True：照 run.cmd_summarize 的口径先清空再回填。"""
    did, path, _ = _plan_with_routes(sandbox, monkeypatch)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["units"][0]["summary"] = "人工写过的总结，这次可以覆盖"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(llm_mod, "LLM", lambda settings: FakeLLM())

    done = _run_full(sandbox, did, "summarize", force=True)
    assert done["status"] == "done", done
    units = json.loads(path.read_text(encoding="utf-8"))["units"]
    assert units[0]["summary"] != "人工写过的总结，这次可以覆盖"


# ===========================================================================
# 源文件回退：pptx/ 空了也要能"重新解析+备课"
# ===========================================================================

def test_full_rerun_finds_source_via_source_txt(sandbox, monkeypatch):
    """源不在 pptx/ 时回退查 decks/<id>/source.txt 第一行。

    仓库里那份 177 单元的真实课件就是这个处境（源 pptx 在仓库根目录）。没有这条回退，
    "重新解析+备课"在最要紧的那份课件上永远是灰的。
    """
    src = make_pptx(sandbox.sources / "demo.pptx")
    _stub_plan_llm(monkeypatch)
    first = _run_full(sandbox)
    assert first["status"] == "done", first

    moved = sandbox.root / "别处" / "demo.pptx"
    moved.parent.mkdir()
    shutil.copy2(src, moved)
    (sandbox.sources / "demo.pptx").unlink()
    (deck_store.deck_dir(first["deck_id"]) / "source.txt").write_text(
        f"{moved}\nsha1=0\n", encoding="utf-8")

    assert pp._resolve("demo.pptx")[1] == moved, "source.txt 回退没生效"

    again = _run_full(sandbox)
    assert again["error"] is None, again
    assert again["status"] == "done", again
    assert again["deck_id"] == first["deck_id"]
    assert json.loads(deck_store.plan_json(again["deck_id"]).read_text(
        encoding="utf-8"))["units"], "重跑真的重新解析并备课了"


def test_resolve_backs_off_to_derived_deck_id(sandbox):
    """补后缀也找不到时，按派生 id 认人：`demo 课件` 与盘上的 `demo-课件.pptx` 是同一份。

    为什么需要第二条回退：全角/半角标点、空格与连字符在 deck_id 里都被压成下划线，
    文件名逐字比对认不出它们。返回的 source/deck_id 必须取**盘上那份**的名字 ——
    作业记录、单飞判定、产物落点全跟着它走。
    """
    make_pptx(sandbox.sources / "demo-课件.pptx")

    source, path, did = pp._resolve("demo 课件")
    assert path is not None and path.name == "demo-课件.pptx"
    assert source == "demo-课件.pptx", "发出去的 source 要是盘上那份的全名"
    assert did == deck_store.make_deck_id("demo-课件.pptx") == "demo_课件"


def test_plan_and_summarize_still_work_with_deck_id_only(sandbox, monkeypatch):
    """plan / summarize 只靠 deck_id —— 源文件删掉之后它们必须照旧能跑。

    新加的两条回退只该帮 full/ingest 找源文件，不许把这两档拽回"必须有源文件"。
    """
    make_pptx(sandbox.sources / "demo.pptx")
    _stub_plan_llm(monkeypatch)
    first = _run_full(sandbox)
    assert first["status"] == "done", first
    (sandbox.sources / "demo.pptx").unlink()          # 源文件没了，deck_id 还在

    monkeypatch.setattr(llm_mod, "LLM", lambda settings: FakeLLM())
    plan = _run_full(sandbox, first["deck_id"], "plan")
    assert plan["status"] == "done", (plan["status"], plan["error"])
    summ = _run_full(sandbox, first["deck_id"], "summarize")
    assert summ["status"] == "done", (summ["status"], summ["error"])
    assert summ["deck_id"] == first["deck_id"]


def test_resolve_gives_up_when_source_txt_path_is_gone(sandbox):
    """source.txt 记的路径也不在了 → 还是"找不到源"，不许凭空编一条路出来。"""
    make_pptx(sandbox.sources / "demo.pptx")
    done = _run_full(sandbox)
    assert done["status"] == "done", done
    (sandbox.sources / "demo.pptx").unlink()
    (deck_store.deck_dir(done["deck_id"]) / "source.txt").write_text(
        "/nope/真的没有了.pptx\nsha1=0\n", encoding="utf-8")

    assert pp._resolve("demo.pptx")[1] is None
    job = _run_full(sandbox)
    assert job["status"] == "failed" and "源库里没有" in job["error"]
    assert "source.txt" in job["error"], "要告诉用户这两个地方都找过了"
