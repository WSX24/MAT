"""P2-1：坏 pptx 的失败信息必须是给人看的。

现实里的那份"坏 pptx"是后缀对、内容是垃圾（别的软件另存到一半、上传被打断、
邮件附件被截断）。解析器对它只会说 "File is not a zip file"，而用户要做的动作
恰恰只有一个 —— 在 PowerPoint 里另存一次。所以这里钉三件事：

  1. 作业 error 与 bridge 返回值里不能出现 BadZipFile 这种类型名；
  2. 要有那句中文提示（"打不开" + "另存"）；
  3. 不能抛到调用方（bridge 的硬约束：它站在一串已产出的 PPTX 中间）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge                                    # noqa: E402
import preprocess                                # noqa: E402
from deck import store as deck_store             # noqa: E402


@pytest.fixture()
def decks(tmp_path, monkeypatch) -> Path:
    """把课件仓库挪到 tmp —— 解析失败也不该在仓库的 decks/ 里留下半个目录。"""
    root = tmp_path / "decks"
    root.mkdir()
    monkeypatch.setattr(deck_store, "DECKS_DIR", root)
    return root


def garbage_pptx(tmp_path: Path, name: str = "坏掉的课件.pptx") -> Path:
    """后缀正确、内容是垃圾 —— 就是那种"看起来是课件"的文件。"""
    p = tmp_path / name
    p.write_bytes(b"this is not a zip container at all" * 40)
    return p


# ---------------------------------------------------------------------------
# 解析阶段：异常被翻译成人话
# ---------------------------------------------------------------------------

def test_stage_ingest_translates_a_broken_file(tmp_path, decks) -> None:
    """直接调阶段函数：翻成 IngestError，且它本身就是那句人话。"""
    src = garbage_pptx(tmp_path)
    deck_id = deck_store.make_deck_id(src.name)
    rec = preprocess._new_record("ingest", src.name, deck_id)

    with pytest.raises(preprocess.IngestError) as excinfo:
        asyncio.run(preprocess._stage_ingest(rec, src, deck_id))

    msg = str(excinfo.value)
    assert "BadZipFile" not in msg, msg
    assert "打不开" in msg and "另存" in msg, msg
    assert src.name in msg, "得说清是哪一份文件坏了"
    # 作业的 error 存的是 repr(e)：repr 也必须是那句人话，不能套上类名
    assert repr(excinfo.value) == msg


def test_other_ingest_errors_keep_the_type_name_but_add_context() -> None:
    """其它异常留类型名（维护者要拿它搜 issue），但补一句"先查什么"。"""
    msg = preprocess.explain_ingest_error("x.pptx", ValueError("说不清"))
    assert "ValueError" in msg and "x.pptx" in msg, msg
    assert "确认" in msg, "得给一个能动手的动作"


# ---------------------------------------------------------------------------
# 作业：error 字段就是界面上看到的那一句
# ---------------------------------------------------------------------------

async def test_job_error_is_readable(tmp_path, decks) -> None:
    src = garbage_pptx(tmp_path)
    job = await preprocess.start_job(src.name, action="ingest", source_path=src)
    snap = await preprocess.wait_job(job["id"], timeout=30)

    assert snap["status"] == "failed", snap
    error = snap["error"] or ""
    assert "BadZipFile" not in error, error
    assert "打不开" in error and "另存" in error, error
    assert snap["finished_at"], "失败也要有时间戳"
    assert snap["stage"] == "failed"


async def test_bridge_reports_it_without_raising(tmp_path, decks) -> None:
    """bridge 的调用方（flow.py）不该收到异常，只该拿到一条可读的 error。"""
    src = garbage_pptx(tmp_path)
    res = await bridge.preprocess_pptx(src, action="ingest")

    assert res["ok"] is False
    assert "BadZipFile" not in (res["error"] or ""), res["error"]
    assert "打不开" in res["error"], res["error"]
