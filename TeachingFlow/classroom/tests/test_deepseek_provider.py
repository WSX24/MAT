"""DeepSeek 服务商 + 仓库根 .env 的接入测试。

为什么单独立一个文件：这两样是"课堂与 TeachingFlow 共用同一份模型配置"的全部底座。
一旦哪一边写死、或哪一次读取覆盖了已有变量，表现是"课堂能跑、出片链路跑不了"
（或反过来）—— 两边都不报错，只是不吭声地换了一家服务商、换了一把 key。

三条约定（与 test_config.py 同一套口径）：
  - 每个用例自己钉住相关环境变量，退出时原样恢复，不依赖本机 .env；
  - 绝不打印任何 key 的明文；
  - 目录路径一律从 `Path(__file__)` 推导，不写死盘符。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    PROVIDER_ENV_KEYS,
    REPO_ENV_FILE,
    deepseek_profile,
    load_repo_env,
    load_settings,
)

#: 与 deepseek 有关、每个用例都要能钉住的那几个键
DS_KEYS = ("DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY",
           "DEEPSEEK_THINKING_STYLE")


@contextmanager
def env(**kv):
    """临时改环境变量（None = 删掉），退出时原样恢复。"""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_deepseek_profile_has_the_documented_defaults() -> None:
    """默认值就是文档里写的那三样：端点 /v1、模型 deepseek-chat、key 取自环境变量。"""
    with env(LLM_PROVIDER="deepseek", DEEPSEEK_BASE_URL=None, DEEPSEEK_MODEL=None,
             DEEPSEEK_API_KEY=None, DEEPSEEK_THINKING_STYLE=None):
        p = load_settings().providers["deepseek"]
    assert p.name == "deepseek"
    assert p.base_url == "https://api.deepseek.com/v1", p.base_url
    assert p.model == "deepseek-chat", p.model
    assert p.api_key == "", "没配 key 时必须是空串（不是占位值，那会让探活判成已配置）"
    assert p.thinking_style == "off", (
        "默认不往请求里塞 reasoning_effort —— DeepSeek 的 chat 接口不认这个参数")
    assert not p.headers, "deepseek 不该带 opencode_go 那套专用请求头"


def test_env_overrides_reach_the_profile() -> None:
    """四个 DEEPSEEK_* 键都能覆盖默认值（换端点/换模型/换 key/换思考口径）。"""
    with env(LLM_PROVIDER="deepseek",
             DEEPSEEK_BASE_URL="http://127.0.0.1:9/ds/v1",
             DEEPSEEK_MODEL="deepseek-test",
             DEEPSEEK_API_KEY="sk-ds-test",
             DEEPSEEK_THINKING_STYLE="reasoning_effort"):
        p = load_settings().providers["deepseek"]
    assert p.base_url == "http://127.0.0.1:9/ds/v1"
    assert p.model == "deepseek-test"
    assert p.api_key == "sk-ds-test"
    assert p.thinking_style == "reasoning_effort"


def test_active_profile_hits_deepseek_when_selected() -> None:
    """LLM_PROVIDER=deepseek 时，Settings.active 命中的就是它（不是 ollama）。"""
    with env(LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="sk-ds-test",
             DEEPSEEK_MODEL=None, DEEPSEEK_BASE_URL=None):
        s = load_settings()
    assert s.provider == "deepseek"
    assert s.active is s.providers["deepseek"], "active 取到的应是 providers 里那一个对象"
    assert s.active.api_key == "sk-ds-test"
    assert "deepseek" in s.describe()


def test_default_provider_is_still_ollama() -> None:
    """代码级默认值**没有**被这次改动动过：不配就还是 ollama。

    顺带钉住"按需出现"这条：没选中 deepseek 时它不出现在 providers 里。
    课堂 GUI 把 providers 逐行渲染成服务商面板（tests/test_modelcfg.py 钉住了那三行），
    无条件塞进去等于悄悄改掉它的对外契约 —— 要 construct 就用 deepseek_profile()。
    """
    with env(LLM_PROVIDER=None):
        s = load_settings()
    assert s.provider == "ollama"
    assert set(s.providers) == {"ollama", "opencode", "opencode_go"}


def test_deepseek_profile_helper_works_without_selection() -> None:
    """没选中也能取到 deepseek 的配置 —— bridge 靠它在主 provider 不可用时换过去。"""
    with env(LLM_PROVIDER="ollama", DEEPSEEK_MODEL=None, DEEPSEEK_API_KEY=None):
        assert "deepseek" not in load_settings().providers
        p = deepseek_profile()
    assert p.name == "deepseek" and p.model == "deepseek-chat" and p.api_key == ""


def test_gui_writeback_keys_cover_deepseek() -> None:
    """面板写回 .env 用的键名表也得有它，否则"保存了没生效"会原样复现一次。"""
    assert PROVIDER_ENV_KEYS["deepseek"] == (
        "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY")


def test_repo_env_path_is_derived_from_this_file() -> None:
    """仓库根 .env 的路径只从 classroom/ 推导：上两级 + PPT-Agent/.env，不写死盘符。"""
    classroom = Path(__file__).resolve().parent.parent
    assert classroom.name == "classroom"
    assert REPO_ENV_FILE == classroom.parent.parent / "PPT-Agent" / ".env"


def test_repo_env_loader_fills_missing_only(tmp_path) -> None:
    """补缺，不覆盖 —— 与 TeachingFlow/core/config.py 的 load_env_file 同口径。

    这条是硬约束：本目录 .env（或 --env 指定的那份）说话，仓库根那份只补缺口。
    反过来的话，用户在课堂侧改过的模型会被仓库根那份悄悄盖回去。
    """
    f = tmp_path / "repo.env"
    f.write_text("CLASSROOM_ENV_TEST_KEEP=from-file\n"
                 "CLASSROOM_ENV_TEST_NEW=from-file\n"
                 "# 注释行不算配置\n"
                 "CLASSROOM_ENV_TEST_EMPTY=\n", encoding="utf-8")
    os.environ["CLASSROOM_ENV_TEST_KEEP"] = "from-env"
    os.environ.pop("CLASSROOM_ENV_TEST_NEW", None)
    os.environ.pop("CLASSROOM_ENV_TEST_EMPTY", None)
    try:
        assert load_repo_env(f) == f
        assert os.environ["CLASSROOM_ENV_TEST_KEEP"] == "from-env", "已存在的变量被覆盖了"
        assert os.environ["CLASSROOM_ENV_TEST_NEW"] == "from-file", "缺的键没补进来"
        assert "CLASSROOM_ENV_TEST_EMPTY" not in os.environ, "空值不该写进环境变量"
        # 幂等：再读一遍什么都不改（每次 load_settings 前都可能被调一次）
        assert load_repo_env(f) == f
        assert os.environ["CLASSROOM_ENV_TEST_KEEP"] == "from-env"
        assert os.environ["CLASSROOM_ENV_TEST_NEW"] == "from-file"
    finally:
        for k in ("CLASSROOM_ENV_TEST_KEEP", "CLASSROOM_ENV_TEST_NEW",
                  "CLASSROOM_ENV_TEST_EMPTY"):
            os.environ.pop(k, None)


def test_repo_env_loader_is_quiet_when_the_file_is_absent(tmp_path) -> None:
    """仓库根那份没入库 —— 缺了是常态，必须安静地返回 None，不许抛。"""
    assert load_repo_env(tmp_path / "没有这份.env") is None
