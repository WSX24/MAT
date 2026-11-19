"""MAT 统一入口：`python app.py` 起课堂正式版服务。

只做四件事，别的都归 `TeachingFlow/classroom/app/`：

1. 把 `TeachingFlow/classroom` 放到 `sys.path[0]` —— 引擎模块都是**顶层模块名**
   （`import machine` / `from deck.store import ...`），不是子包；
2. 读仓库根的 `.env`（`config.load_repo_env`）。配置的唯一出处是仓库根，
   不再是 classroom 自己的 .env；
3. 读 `HOST`（默认 127.0.0.1）与 `PORT`（默认 8000）；
4. 起 uvicorn。

为什么入口要放在仓库根：完成标准 §8.1 是"新 clone：`python app.py` 起服务"。
把这件事埋进 classroom 目录的话，新来的人得先知道那个目录名才启动得起来。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLASSROOM = ROOT / "TeachingFlow" / "classroom"


def _bootstrap() -> None:
    """把引擎目录接进导入路径并读 .env（顺序不能反：config 本身就在那个目录里）。"""
    if not CLASSROOM.is_dir():
        raise SystemExit(f"找不到课堂目录：{CLASSROOM}")
    if str(CLASSROOM) not in sys.path:
        sys.path.insert(0, str(CLASSROOM))
    from config import ensure_utf8_console, load_repo_env

    ensure_utf8_console()
    env = load_repo_env()
    if env is None:
        print("提示：没找到 .env（模型与 TTS 会降级，但服务照常能起）")


def _tts_lines(configured: bool) -> list[str]:
    """语音那几行：说实话，而不是照着配置念。

    "配置里开着"和"真的能出声"是两件事 —— edge-tts 版本太旧时每一句都 403，
    配置照样是开的，学生听到的是没有声音（只有字幕）。启动横幅是启动期唯一的自述，
    它要是只报配置，这个故障就得等课堂上学生开口才发现。

    探测只用 import 进来的版本号，**不建实例、不碰磁盘**（和 /api/health 同理）。
    """
    try:
        from speech import UPGRADE_HINT, edge_tts_version, version_problem
    except Exception as e:                     # 语音模块本身坏了不该拦启动
        return [f"  语音(edge-tts) ?（读不出语音模块：{type(e).__name__}: {e}）"]

    version = edge_tts_version()
    if not version:
        return ["  语音(edge-tts) 关（没装，课堂只有字幕）",
                f"  提示          装它：{UPGRADE_HINT}"]
    problem = version_problem()
    if problem:
        return [f"  语音(edge-tts) 关（{problem}）",
                f"  提示          升级后重启：{UPGRADE_HINT}"]
    if not configured:
        return [f"  语音(edge-tts) {version}（TTS_ENABLED=0：只有字幕）"]
    return [f"  语音(edge-tts) {version}（开）"]


def _ready_lines(host: str, port: int, *, env_path) -> list[str]:
    """启动横幅的内容：地址、课程库、引擎、TTS、以及"缺什么、会降级成什么"。

    逐项**独立**取：库坏了不影响报引擎，key 没配不影响报课程数。任何一项取不到都
    只降级成一行说明 —— 这个横幅是启动期唯一的自述，它自己抛异常就等于服务起不来。
    """
    from app import deps                      # classroom/app/deps.py

    try:
        from store import db as db_mod

        db_file = str(db_mod.db_path())       # 实际生效的那份（CLASSROOM_DB 可覆盖）
    except Exception:
        db_file = str(deps.CLASSROOM_ROOT.parent / "state" / "classroom.sqlite")

    addr = f"http://{host}:{port}"
    lines = [
        "=" * 70,
        "  课堂（学生自学）· 正式版",
        "=" * 70,
        f"  地址          {addr}",
        f"  健康检查      {addr}/api/health",
        f"  课程库        {db_file}",
    ]

    courses = "?"
    try:
        from store import db as db_mod

        conn = db_mod.connect()
        try:
            deps.ensure_db(conn)
            courses = int(conn.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"] or 0)
        finally:
            conn.close()
    except Exception as e:                     # 库坏了：报原因，服务照起（健康检查也在降级）
        lines.append(f"  ⚠️ 课程库读不出来（{type(e).__name__}: {e}）—— 首页会是空态")
    if courses != "?":
        lines.append(f"  课程数        {courses} 门"
                     + ("（空库：进首页可「导入课件」或「重新扫描」）" if courses == 0 else ""))

    provider = model = "?"
    tts = False
    key_set = False
    try:
        info = deps.engine_info()
        provider, model, tts = info["provider"] or "?", info["model"] or "?", info["tts"]
        if provider != "?":
            key_set = bool(getattr(deps.get_settings().active, "api_key", ""))
    except Exception as e:
        lines.append(f"  ⚠️ 配置读不出来（{type(e).__name__}: {e}）—— 备课会走规则兜底")
    lines.append(f"  引擎          {provider} / {model}")
    lines.extend(_tts_lines(tts))

    if courses == "?" or provider == "?":
        lines.append("  提示          服务照常能起；上面那几行 ⚠️ 说明降级在哪一项")
    elif not key_set:
        lines.append("  提示          没有可用的模型 key —— 备课走规则兜底，内容照样能上，")
        lines.append("                只是讲解脚本是「照读课件」级别（好一点请配 .env 的 DEEPSEEK_API_KEY）")
    if env_path is None:
        lines.append("  提示          没找到 .env：把仓库根的 .env.example 复制成")
        lines.append("                PPT-Agent/.env 再填 DEEPSEEK_API_KEY（不配也能起）")
    lines.append("  Ctrl+C 停止；改了 .env 要重启才生效")
    lines.append("=" * 70)
    return lines


#: 认得的参数只有 `-h/--help`，其余一律忽略（不报错、不退出）。
#: "忽略"是刻意的：`python flow.py --classroom-serve -- <任意参数>` 会把参数原样透传到这里，
#: 而透传的语义就是"给了不认识的东西也别因此崩"。
USAGE = """\
课堂（学生自学）· 正式版

用法：
    python app.py                     起服务（读 PORT，默认 8000；HOST 默认 127.0.0.1）
    python app.py --help              打印这份说明后退出（其它参数一律忽略）

环境变量：
    PORT=8010                         端口
    HOST=0.0.0.0                      监听地址（要投屏给别的机器看时才需要）
    CLASSROOM_DB=path/to.sqlite       换一份课程库（默认 TeachingFlow/state/classroom.sqlite）
    CLASSROOM_DEV=1                   打开 /dev/* 诊断端点（默认一律 404）
    LOG_LEVEL=warning                 uvicorn 日志级别

前置：Python 3.12 · 依赖 python -m pip install -r TeachingFlow/classroom/requirements.txt
配置：仓库根 PPT-Agent/.env 的 DEEPSEEK_API_KEY（不配也能起：备课走规则兜底、只有文字）
一键：powershell -File ./start-app.ps1
"""


def main() -> None:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(USAGE, end="")
        return
    _bootstrap()
    import uvicorn

    # 注意导入的是 classroom/app/__init__.py：`sys.path[0]` 已经是 classroom 目录，
    # 而本文件以 __main__ 身份运行，不会和这个 app 包撞名。
    from app import app as fastapi_app

    host = os.environ.get("HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PORT", "8000") or 8000)
    except ValueError:
        print(f"PORT 不是数字（{os.environ.get('PORT')!r}），改用 8000")
        port = 8000
    from config import load_repo_env

    # flush=True：输出被接进管道时（nohup / IDE 任务 / start-app.ps1 的日志）默认是
    # 块缓冲，横幅会卡在缓冲区里，看起来像"什么都没打印就卡住了"。
    for line in _ready_lines(host, port, env_path=load_repo_env()):
        print(line, flush=True)
    uvicorn.run(fastapi_app, host=host, port=port,
                log_level=os.environ.get("LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
