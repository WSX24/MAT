"""每个用例一条临时课程库：任何测试都不许读开发者本机的真实库。

为什么必须有这份 conftest：课堂的模型配置现在是「库 > env > 内置默认」，
只要某个用例没把 CLASSROOM_DB 指到 tmp，它就会读到你本机真库里那套服务商，
于是"env 决定 provider"这类断言会在别人设过默认之后莫名其妙地红 ——
实测过一次：本机把 deepseek 设为默认后，9 个用例集体失败在
"assert deepseek-flash == deepseek-chat" 这种地方。

旧代码不读库，所以这个坑一直藏着。隔离做成**默认行为**，而不是逐条用例自觉。
用例里再 monkeypatch.setenv("CLASSROOM_DB", …) 会覆盖这里（后设者生效），
所以需要自备库的用例照旧。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_classroom_db(tmp_path, monkeypatch):
    """每个用例一条全新的 sqlite（tmp_path 下），跑完即弃。"""
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "classroom.sqlite"))
