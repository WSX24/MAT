"""控制台编码回归 —— 中文 Windows 上"课堂起不来"的那次事故。

事故现场（修复前，环境里没有 PYTHONUTF8）：
    python -c "import web"
    → UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
    → 导入期就崩，服务连 bind 的机会都没有（课程笔记里全是 ✅❌⚠️）。

所以第一条用例**必须是子进程级**、环境里塞 PYTHONIOENCODING=gbk 并**删掉** PYTHONUTF8：
只有真开一个 GBK 控制台的进程才抓得住它。在 pytest 进程内 monkeypatch 编码是抓不到的 ——
pytest 自己早就把 sys.stdout 换成了内存流，那条路径根本不经过 GBK 编码器。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ensure_utf8_console  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _gbk_env() -> dict[str, str]:
    """模拟"默认中文 Windows"+ 输出被管道接走：只有 GBK，不给 UTF-8 模式。"""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONUTF8"}
    env["PYTHONIOENCODING"] = "gbk"
    return env


def test_import_web_survives_a_gbk_console() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "import web"],
        cwd=str(ROOT),                       # web.py 的扁平导入只在本目录成立
        env=_gbk_env(),
        capture_output=True,
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert proc.returncode == 0, (
        "GBK 控制台下 `import web` 崩了 —— 课堂会因为一行 emoji 打印起不来。\n"
        f"退出码 {proc.returncode}\n{out[-2000:]}"
    )


def test_ensure_utf8_console_is_idempotent_and_never_raises(monkeypatch) -> None:
    """幂等 + 绝不抛：这个函数是"兜底"，它自己出问题就等于把兜底变成了拦路虎。"""
    for _ in range(3):
        ensure_utf8_console()
    # pytest 的捕获流不一定有 reconfigure，有才断言（判断口径与函数本身一致）
    if hasattr(sys.stdout, "reconfigure"):
        assert (sys.stdout.encoding or "").lower().replace("-", "") == "utf8"

    class NoReconfigure:
        """宿主重定向出来的流：没有 reconfigure，必须被静默跳过。"""

        encoding = "gbk"

        def write(self, s: str) -> int:
            return len(s)

        def flush(self) -> None:
            pass

    class BrokenReconfigure(NoReconfigure):
        def reconfigure(self, **kwargs) -> None:
            raise OSError("流已经被关掉了")

    monkeypatch.setattr(sys, "stdout", NoReconfigure())
    ensure_utf8_console()                    # 不许抛
    monkeypatch.setattr(sys, "stderr", BrokenReconfigure())
    ensure_utf8_console()                    # 也不许抛
