# -*- coding: utf-8 -*-
"""课堂建课线 → TeachingFlow 的 S0 范围解析（``core/scope.py``）。

为什么要有这个文件
------------------
一句话里的「学段 / 学科 / 水平 / 周期 / 强度」本来只有一处出处：``flow.py`` 的 S0
（``core/scope.py``）。课堂那条建课线（``course_plan``）早先自己写了一套正则，
于是同一个用户在两条线上得到两种排法 —— 「我想快速学习初中生物光合作用实验课」
走 ``flow.py`` 是 1 周，走课堂建课却是 6 周 18 课时。

这个桥只做两件事：**按文件把 core.scope 装起来**、**把 FlowSpec 摊成课堂要的字典**。
判断逻辑一行都不复制 —— 复制出来的第二份迟早和第一份说出不一样的答案。

怎么装：``core/scope.py`` 里写着 ``from . import config``，所以它必须作为**包**加载。
不做 ``sys.path.insert(TeachingFlow/)``：那时 ``tools``/``fixtures`` 这种通用名字
可能撞上别的包（``studio/catalog.py`` 为同一个理由按文件加载 ``core.naming``）。
这里给 ``core`` 建一个临时包名 ``_tf_core``，相对导入就落在自己家里。

拿不到 core.scope（文件被挪走、依赖缺失）时一律返回 ``None``，调用方退回自己的
旧规则 —— 课堂不能因为少了这个桥就开不了课。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger("classroom.scope_bridge")

_SCOPE = None
_LOADED = False

#: ``build_spec`` 认的 CLI 参数；课堂没有命令行，全部按"没说"处理
_NO_ARGS = SimpleNamespace(stage=None, subject=None, level=None, weeks=None, days=None,
                           term=None, pages=None, hours_per_week=None)


def _load():
    """按文件把 core.scope 当包加载一次；失败就记住失败（只试一次）。"""
    global _SCOPE, _LOADED
    if _LOADED:
        return _SCOPE
    _LOADED = True
    try:
        pkg_dir = Path(__file__).resolve().parents[1] / "core"     # TeachingFlow/core
        init = pkg_dir / "__init__.py"
        if not init.is_file():
            return None
        name = "_tf_core"                        # 私有包名：不占用 "core" 这个名字
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                name, init, submodule_search_locations=[str(pkg_dir)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        _SCOPE = importlib.import_module(name + ".scope")
    except Exception as exc:                     # noqa: BLE001 — 桥坏了不该让课堂起不来
        log.warning("scope_bridge：加载 core.scope 失败（将退回本地规则）：%s: %s",
                    type(exc).__name__, exc)
        _SCOPE = None
    return _SCOPE


def available() -> bool:
    return _load() is not None


def hints(text: str) -> dict | None:
    """一句话 → 课堂要的那几个字段（与 ``flow.py`` 同一次判断）。

    返回的键都是**只读提示**：模型说了就以模型为准，模型没说才拿它兜底。
    """
    scope = _load()
    if scope is None:
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        spec = scope.build_spec(raw, _NO_ARGS)
    except Exception as exc:                     # noqa: BLE001
        log.warning("scope_bridge：build_spec 失败：%s: %s", type(exc).__name__, exc)
        return None
    return {
        "stage": spec.stage,
        "stage_known": bool(spec.stage_known),
        "subject": spec.subject,
        "subject_known": bool(spec.subject_known),
        "level": spec.level,
        "weeks": int(spec.weeks),
        "weeks_source": spec.weeks_source,
        "term": spec.term,
        "term_source": spec.term_source,
        "intensity": spec.intensity,
        "effort": spec.effort,
        "single_text": bool(spec.single_text),
        "hours_per_week": int(spec.hours_per_week),
        "focus": spec.focus,
        "notes": list(spec.notes or []),
    }


def weeks_default(text: str) -> int | None:
    h = hints(text)
    return None if h is None else int(h["weeks"])
