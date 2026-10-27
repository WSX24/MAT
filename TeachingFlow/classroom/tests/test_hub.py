"""启动台页（/hub）的测试。

为什么单开一个文件
------------------
这一页是"双击之后落到哪里"的入口，它坏了的表现**不是报错，而是把功能藏起来**：
五个页面里少链一个，那一页就等于不存在 —— /graph 最初就是这么被当成"没做"的
（页面早就跑得起来，只是没有任何入口链接到它）。

所以这里钉的不是排版，而是三件事：

  1. **五个页面 + MCP 端点，一个都不能少**（少一个就有一个功能点不到）；
  2. **路由真的注册了** —— 光有 web/hub.html 躺在磁盘上不算，得能 GET 到；
  3. **不许有副作用、不许轮询** —— 这页加载时只读一次 live 用来显示"课堂在跑没有"，
     每秒轮询的活儿已经由 /graph 与 /script 各自在做，这里再开一条只是白烧 CPU。

调用方式沿用 tests/test_script_tree.py：项目没装 httpx（fastapi.testclient 用不了），
端点本来就是纯 async 函数，asyncio.run(web.hub_page()) 直接 await 即可。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web  # noqa: E402

#: 启动台上必须能点到的全部人类入口。加一条页就加一条到这里 —— 这是本文件的全部意义。
ENTRIES = ("/", "/graph", "/script", "/review", "/preview", "/preprocess", "/mcp")


def _hub_html() -> str:
    return asyncio.run(web.hub_page()).body.decode("utf-8")


def test_hub_route_is_registered():
    """路由真注册了，且启动台链的每个页面在 app 里都有对应路由。"""
    paths = {getattr(route, "path", "") for route in web.app.routes}
    assert "/hub" in paths, "启动台本身没有路由"
    for entry in ENTRIES:
        assert entry in paths, f"启动台链到了 {entry}，但 app 里没有这条路由"


def test_hub_links_every_entry():
    """五个页面 + MCP 端点，一个都不能少：漏一个就等于那个功能点不到。"""
    html = _hub_html()
    for entry in ENTRIES:
        assert f'href="{entry}"' in html, f"启动台上没有 {entry} 的入口"


def test_hub_page_is_not_cached_and_assets_are_versioned():
    """页面本身 no-store；引用的 CSS 带 ?v=（否则改了样式用户还看旧的一版）。"""
    response = asyncio.run(web.hub_page())
    assert "no-store" in response.headers.get("cache-control", "")
    html = response.body.decode("utf-8")
    assert 'href="/static/classroom.css?v=' in html


def test_hub_static_references_all_exist():
    """页里写到的每个 /static 资源都必须真的存在。

    为什么要钉：这一页的样式写在页内（不单开 hub.css），但一旦有人以后往里加
    <link rel="stylesheet" href="/static/hub.css"> 而忘了建文件，浏览器只会
    404 掉样式，页面照样能开 —— 看起来"就是丑一点"，没人会去查网络面板。
    """
    html = _hub_html()
    referenced = set(re.findall(r'(?:href|src)="/static/([^"?]+)', html))
    assert referenced, "启动台没有引用任何静态资源，检查是不是把 classroom.css 弄丢了"
    for name in referenced:
        assert (ROOT / "web" / name).exists(), f"启动台引用了不存在的静态资源：{name}"


def test_hub_reads_live_once_and_never_polls():
    """只读一次、不轮询：setInterval 出现即视为违反（这页不是观测面）。"""
    html = _hub_html()
    assert "/api/script/live" in html, "表明「课堂在跑没有」的那一次读取不见了"
    assert "setInterval" not in html, "启动台不该轮询：每秒轮询的活儿已由 /graph 与 /script 在做"
