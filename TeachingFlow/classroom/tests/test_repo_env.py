"""仓库根 .env 的"补缺"判定 —— 一个空的 DEEPSEEK_API_KEY= 曾把真 key 挡在门外。

事故链：setup.ps1 从 .env.example 生成 classroom/.env，里面含 `DEEPSEEK_API_KEY=`（空串）
→ dotenv 把它写成 `os.environ["DEEPSEEK_API_KEY"] = ""`
→ 旧判定 `k not in os.environ` 认为"已配置"
→ 仓库根 PPT-Agent/.env 里的真 key 永远补不进来 → 深集成每次退化成只解析。

所以"已配置"的定义只能是**非空**：空串 / 纯空白一律算没配；用户显式写了非空值就必须被尊重。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import deepseek_profile, load_repo_env  # noqa: E402

#: 用带前缀的假键，避免碰到真实模型配置
KEY = "CLASSROOM_REPOENV_TEST_KEY"
REPO_VALUE = "sk-real-from-repo"


def _repo_env(tmp_path: Path) -> Path:
    f = tmp_path / "repo.env"
    f.write_text(f"{KEY}={REPO_VALUE}\n", encoding="utf-8")
    return f


def test_blank_env_does_not_mask_the_repo_value(tmp_path, monkeypatch) -> None:
    """① 预置空串（dotenv 读 `KEY=` 的产物）→ 必须被仓库文件里的真值补上。"""
    monkeypatch.setenv(KEY, "")
    assert load_repo_env(_repo_env(tmp_path)) is not None
    assert os.environ[KEY] == REPO_VALUE


def test_explicit_value_is_never_overwritten(tmp_path, monkeypatch) -> None:
    """② 用户显式设了非空值 → 一个字都不许改；纯空白则与空串同权。"""
    monkeypatch.setenv(KEY, "sk-user-explicit")
    load_repo_env(_repo_env(tmp_path))
    assert os.environ[KEY] == "sk-user-explicit"

    monkeypatch.setenv(KEY, "   ")
    load_repo_env(_repo_env(tmp_path))
    assert os.environ[KEY] == REPO_VALUE


def test_absent_file_is_quiet_and_leaves_environ_alone(tmp_path, monkeypatch) -> None:
    """③ 仓库根那份没入库，缺了是常态：安静返回 None，不抛，也不动环境变量。"""
    monkeypatch.setenv(KEY, "")
    before = dict(os.environ)
    assert load_repo_env(tmp_path / "这份不存在.env") is None
    assert os.environ == before


def test_loading_twice_is_idempotent(tmp_path, monkeypatch) -> None:
    """④ 幂等：config 里每次 load_settings 之前都可能再读一遍，读法必须稳定。"""
    monkeypatch.setenv(KEY, "")
    f = _repo_env(tmp_path)
    load_repo_env(f)
    first = os.environ[KEY]
    load_repo_env(f)
    assert os.environ[KEY] == first == REPO_VALUE


def test_blank_deepseek_key_reads_as_no_key(monkeypatch) -> None:
    """deepseek_profile() 也得把空白 key 看成"没 key"，否则 `if not api_key` 那些
    回退判断会被一串空格骗过去。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")
    assert deepseek_profile().api_key == ""
