"""切换"当前上课用哪份课件"（POST /api/deck/activate）的离线测试。

怎么测、为什么这么测
--------------------
* **绝不碰真实的 prototype/.env**：每个用例都把 `web.env_file` 指向 tmp_path 下的一份
  临时配置。切课件会写回 .env —— 忘了隔离，跑一次测试就把开发机上正在用的课件换掉了。
* **还原进程级单例**：`web.settings / web.course / web.script` 是全进程共用的（ws_endpoint
  每开一节课按当时的全局装配）。用例改了不还回去，后面所有用例都在"新的那份课件"上跑 ——
  单跑通过、一起跑挂，这类失败最难查。
* **不用真模型、不用真课件**：切课件这一路只读盘上文件（decks/<id>/deck.json + plan.json），
  假课件由 tests/fixtures.make_deck 造，plan.json 是最小可用的那份。
* 钉的是**准入与顺序**：课堂在跑不许切 / 失败必须回滚 / 写 .env 必须晚于装配成功。
  这三条坏掉的共同表现是"保存了但没生效"或"重启才炸"，现场没有任何报错。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402
import course as course_mod  # noqa: E402
import web  # noqa: E402
from deck import store as deck_store  # noqa: E402
from tests.fixtures import make_deck  # noqa: E402

#: 除了 DECK_ID 还有一行别的配置：切换只许改 DECK_ID，别的行一个字节都不能动
ENV_TEXT = """# 测试用配置（这两行是陪跑的，证明没被一起重写）
DECK_ID=deck_a
OTHER_KEY=keepme
"""

#: 最小可用的 plan.json：有 routes 键（新计划）就不走内存补挂变体那条老路
PLAN = {
    "source": "llm",
    "session_goals": ["看懂 pack 的用法"],
    "units": [
        {"slide": 0, "text": "第一页：Unit 2 Sweet Home 的封面，今天讲词汇。"},
        {"slide": 1, "text": "第二页：pack 的两个常见搭配。"},
    ],
    "routes": [],
}


def _write_deck(decks: Path, deck_id: str, title: str) -> None:
    """造一份能上课的假课件：deck.json + plan.json（都不含图片，落盘只有几 KB）。"""
    deck_store.save_deck(replace(make_deck(), deck_id=deck_id, title=title), root=decks)
    (decks / deck_id / "plan.json").write_text(
        json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def switchable(tmp_path, monkeypatch):
    """两份可切换的课件 + 一份临时 .env + 空的课堂登记表。"""
    decks = tmp_path / "decks"
    decks.mkdir()
    monkeypatch.setattr(deck_store, "DECKS_DIR", decks)
    _write_deck(decks, "deck_a", "甲课件：pack 的用法")
    _write_deck(decks, "deck_b", "乙课件：pack 的搭配")

    env_path = tmp_path / ".env"
    env_path.write_text(ENV_TEXT, encoding="utf-8")
    monkeypatch.setattr(web, "env_file", lambda: env_path)
    monkeypatch.setenv("DECK_ID", "deck_a")
    monkeypatch.setattr(web, "_LIVE_RUNNERS", [])

    saved = (web.settings, web.course, web.script)
    settings = config_mod.load_settings()
    web.settings, web.course = settings, course_mod.load_course(settings)
    web.script = web.course.script
    assert web.course.deck_id == "deck_a", "起点必须是甲课件"
    try:
        yield SimpleNamespace(env=env_path, decks=decks)
    finally:
        web.settings, web.course, web.script = saved


def _activate(deck_id: str):
    """(状态码, body)：端点可能返回 dict，也可能用 JSONResponse 表达失败。"""
    res = asyncio.run(web.api_deck_activate({"deck_id": deck_id}))
    if hasattr(res, "body"):
        return res.status_code, json.loads(res.body.decode("utf-8"))
    return 200, res


def _env_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class FakeRunner:
    """正在上课的 Runner 的替身：真要一个 Runner 就得建 ws、建状态机、起音频。"""

    def __init__(self, course) -> None:
        self._course = course


# ===========================================================================
# 准入：课堂在跑时不许换
# ===========================================================================

def test_activate_refused_while_class_is_running(switchable):
    """有课堂在跑 → 409，且 .env 与全局 course 一个字节都没动。

    为什么必须拦：技术上换得动（正在上的那节课自带 course/script/state），但老师讲到
    一半课件换了，学生看到的是课件中途消失。技术可行 + 产品不允许 = 只能在这一层拦。
    """
    before_env = _env_text(switchable.env)
    before_course = web.course
    monkeypatch_course = course_mod.load_course(config_mod.load_settings())
    web._LIVE_RUNNERS.append(FakeRunner(monkeypatch_course))
    web._LIVE_RUNNERS.append(FakeRunner(monkeypatch_course))

    code, body = _activate("deck_b")
    assert code == 409, body
    assert body["ok"] is False
    assert "课堂正在跑（2 个连接）" in body["error"], body["error"]
    assert _env_text(switchable.env) == before_env, "被拒时绝不许写 .env"
    assert web.course is before_course and web.course.deck_id == "deck_a"
    assert os.environ["DECK_ID"] == "deck_a"


def test_live_payload_reports_the_running_course(switchable):
    """library.live 要能说出"在跑的是哪一份、几个连接"（页面据此灰掉按钮）。"""
    assert web._live_payload() == {"running": 0, "deck_id": "", "title": ""}

    running = course_mod.load_course(config_mod.load_settings())
    web._LIVE_RUNNERS.append(FakeRunner(running))
    live = web._live_payload()
    assert live["running"] == 1
    assert live["deck_id"] == "deck_a" and live["title"] == "甲课件：pack 的用法"


# ===========================================================================
# 正常切换
# ===========================================================================

def test_activate_switches_and_writes_env(switchable):
    """200 + 全局换掉 + .env 里 DECK_ID 变了 + library.active 跟着变。"""
    code, body = _activate("deck_b")
    assert code == 200 and body["ok"] is True and body["changed"] is True, body
    assert body["active"]["deck_id"] == "deck_b"
    assert body["active"]["title"] == "乙课件：pack 的搭配"
    assert body["note"] == "已切换；上课页刷新后生效（.env 已写回，重启也保持）"

    assert web.course.deck_id == "deck_b" and web.course.mode == "deck"
    assert web.script is web.course.script, "全局 script 必须一起换，否则上课页还是旧脚本"
    assert web.settings.deck_id == "deck_b", "settings 也要换（Runner 拿的是它）"
    assert os.environ["DECK_ID"] == "deck_b", "进程环境没换 = 下次 load_settings 又回去了"

    text = _env_text(switchable.env)
    assert "DECK_ID=deck_b" in text
    assert "# 测试用配置（这两行是陪跑的，证明没被一起重写）" in text, "注释被重写丢了"
    assert "OTHER_KEY=keepme" in text, "只该改 DECK_ID，别的键不许动"

    assert web._active_payload()["deck_id"] == "deck_b"
    lib = asyncio.run(web.api_preprocess_library())
    assert lib["active"]["deck_id"] == "deck_b" and lib["active"]["mode"] == "deck"


def test_library_active_and_live_keys(switchable):
    """/api/preprocess/library 必须同时给 active 与 live（页面每个按钮都看它们）。"""
    lib = asyncio.run(web.api_preprocess_library())
    assert lib["ok"] is True
    assert set(lib["active"]) == {"deck_id", "title", "mode", "env_file"}
    assert lib["active"]["env_file"] == str(switchable.env)
    assert lib["live"] == {"running": 0, "deck_id": "", "title": ""}


def test_activate_simple_mode(switchable):
    """deck_id="" → 手写演示课件（DECK_ID 置空，重启后也是它）。"""
    code, body = _activate("")
    assert code == 200 and body["ok"] is True and body["changed"] is True, body
    assert web.course.mode == "simple" and web.course.deck_id == ""
    assert body["active"]["mode"] == "simple"
    assert body["active"]["title"] == "手写演示课件", "空标题会让页面显示一对空书名号"
    assert "DECK_ID=" in _env_text(switchable.env)
    assert "DECK_ID=deck_" not in _env_text(switchable.env), "置空要真的写空"
    assert os.environ["DECK_ID"] == ""


# ===========================================================================
# 拒绝：不存在 / 幂等
# ===========================================================================

def test_activate_unknown_deck_is_400_and_changes_nothing(switchable):
    before_env = _env_text(switchable.env)
    code, body = _activate("没有这份课件")
    assert code == 400, body
    assert body["ok"] is False and "没有这份课件" in body["error"]
    assert _env_text(switchable.env) == before_env
    assert web.course.deck_id == "deck_a"


def test_activate_same_deck_is_idempotent(switchable):
    """切到当前那份 → changed:false、不写 .env，课堂在跑时也不拦。"""
    before_env = _env_text(switchable.env)
    web._LIVE_RUNNERS.append(FakeRunner(web.course))       # 课堂在跑也不该拦

    code, body = _activate("deck_a")
    assert code == 200 and body["ok"] is True, body
    assert body["changed"] is False and body["note"] == "已经是当前课件"
    assert _env_text(switchable.env) == before_env, "幂等路径不许写盘"
    assert web.course.deck_id == "deck_a"


# ===========================================================================
# 失败回滚：装配炸了，旧课件必须照旧能用
# ===========================================================================

def test_activate_rolls_back_when_build_fails(switchable, monkeypatch):
    """load_course 抛异常 → ok:false、全局仍是旧的、os.environ 已回滚、.env 没写。"""
    before_env = _env_text(switchable.env)
    before_course = web.course

    def _boom(*a, **kw):
        raise RuntimeError("装配炸了")

    monkeypatch.setattr(web, "load_course", _boom)

    code, body = _activate("deck_b")
    assert code == 500, body
    assert body["ok"] is False and "装配炸了" in body["error"]
    assert web.course is before_course and web.course.deck_id == "deck_a"
    assert os.environ["DECK_ID"] == "deck_a", "环境变量不回滚 = 下次 load_settings 读到没装上的课件"
    assert _env_text(switchable.env) == before_env, "装配失败绝不许写 .env（否则重启才炸）"


def test_activate_rolls_back_when_env_file_is_not_writable(switchable, monkeypatch):
    """写 .env 失败 → 也算失败：全局不许变（不然界面说失败、实际已经换了）。"""
    before_course = web.course

    def _boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(web, "save_env_values", _boom)

    code, body = _activate("deck_b")
    assert code == 500 and body["ok"] is False and "磁盘满了" in body["error"], body
    assert web.course is before_course and web.course.deck_id == "deck_a"
    assert os.environ["DECK_ID"] == "deck_a"


# ===========================================================================
# 页面接线
# ===========================================================================

def test_page_has_activate_button_and_header():
    """页面上真的有一个「设为当前上课」按钮、打到 /api/deck/activate，且顶部读 active。"""
    html = (ROOT / "web" / "preprocess.html").read_text(encoding="utf-8")

    assert "设为当前上课" in html, "每行的入口没了"
    assert "'/api/deck/activate'" in html, "按钮没有打到切换端点"
    assert "JSON.stringify({ deck_id: deckId })" in html, "载荷字段名必须是 deck_id"
    # 顶部那一行与徽标
    assert "当前上课：" in html and 'id="active"' in html and 'id="live"' in html
    assert "不用真实课件（手写演示）" in html, "切到手写演示的入口没了"
    # 渲染当前课件读的是 active（不是旧的 /api/decks）
    assert "cur.active" in html and "paintActive" in html
    assert "json('/api/decks'" not in html, "旧的状态行已被 library.active 取代，别再拉第二份快照"
    # 课堂在跑就灰：同一个 live.running 决定按钮能不能点
    assert "cur.live.running" in html and "liveWhy(" in html
