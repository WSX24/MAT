"""sqlite 连接与幂等迁移（设计文档 §3）。

三条口径，改之前先读：

1. **路径只有一个来源**：环境变量 `CLASSROOM_DB` 优先，否则
   `TeachingFlow/state/classroom.sqlite`（目录不存在就建）。测试必须把
   CLASSROOM_DB 指到 tmp_path —— 否则跑一次 pytest 就把真库写脏，
   下一次跑测试的结果取决于上一次跑过什么。
2. **迁移只加不减**：schema.sql 全是 `IF NOT EXISTS`；新版本只能追加
   "先判断再 ALTER/建表"的语句。DROP / 重建 = 学生丢进度，明文禁止。
   版本号记在 `meta.schema_version`，启动时幂等升级。
3. **每个请求一条连接**：sqlite 连接默认不能跨线程，而 FastAPI 的同步路由跑在线程池里，
   共享一条连接会在并发请求下随机抛 `ProgrammingError`。连接很便宜，别省这一个。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

#: 当前 schema 版本。加一条迁移就 +1，并在 migrate() 里补 {旧版本: 语句}。
#: 2 = 出片任务表 generations（见 schema.sql）
#: 3 = 模型服务商中枢两张表 model_providers / app_settings（见 schema.sql 末尾）
#: 4 = 交互式课程生成会话 course_builder_sessions 与渐进式单课 PPT 生成任务 unit_ppt_jobs
SCHEMA_VERSION = 4

HERE = Path(__file__).resolve().parent
#: TeachingFlow/classroom/store → TeachingFlow/classroom
CLASSROOM_ROOT = HERE.parent
#: TeachingFlow/state/classroom.sqlite
DEFAULT_DB = CLASSROOM_ROOT.parent / "state" / "classroom.sqlite"

SCHEMA_FILE = HERE / "schema.sql"


def db_path(path: str | Path | None = None) -> Path:
    """解析数据库路径并保证父目录存在。"""
    raw = path or os.environ.get("CLASSROOM_DB") or DEFAULT_DB
    p = Path(raw)
    if not p.is_absolute():
        p = (CLASSROOM_ROOT / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """开一条连接（row_factory = sqlite3.Row，外键打开）。"""
    # check_same_thread=False：FastAPI 的**同步依赖**（`get_conn`）跑在线程池里，
    # 而少数 `async def` 路由（例如 `POST /sessions/{id}/end`，它要 await 运行器收尾）
    # 跑在事件循环线程上 —— 同一请求的两段代码因此可能落在不同线程。
    # 每条请求独占一条连接、且两段不会同时使用它，所以关掉线程检查是安全的；
    # 不关的话表现是随机的 ProgrammingError（"created in thread X and this is thread Y"）。
    conn = sqlite3.connect(str(db_path(path)), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 外键约束必须每条连接单独打开：sqlite 默认关，关着的时候
    # "删课程顺手清单元"这类语句不会报错，只会悄悄留下孤儿行。
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass          # 只读介质 / 网络盘上 WAL 开不了，退回默认日志模式即可
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """当前库里的 schema 版本；空库返回 0。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return int(str(row["value"]))
    except (TypeError, ValueError):
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """幂等迁移：把库升到 SCHEMA_VERSION 并返回版本号。

    做法是"先跑全量 schema.sql（全 IF NOT EXISTS），再按版本补增量"。
    全新库和旧库走同一条路径，因此不存在"只在老库上成立"的分支 ——
    这类分支正是迁移脚本最常见的静默失效点。
    """
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    current = schema_version(conn)
    if current >= SCHEMA_VERSION:
        conn.commit()
        return current if current else SCHEMA_VERSION

    # 版本 1 = 初始结构（schema.sql 已建好）。以后新增版本时在这里补：
    #     if current < 3:
    #         conn.execute("ALTER TABLE units ADD COLUMN ...")
    #
    # 版本 2（出片任务表）与版本 3（模型服务商中枢的表）**都没有增量语句**：
    # 它们全是整张新表，而上面那句 executescript 已经把 schema.sql 全文跑过了
    # （全 IF NOT EXISTS），老库到这一行时表已经在了。
    # 增量区留给"改既有表"那种必须 ALTER 的情况 —— 那种才需要在版本分支里写。
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return SCHEMA_VERSION


def init_db(path: str | Path | None = None) -> sqlite3.Connection:
    """开库并迁移（app 启动时用一次）。"""
    conn = connect(path)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    """库里的用户表清单（证据/自检用：`schema_version` + 表名一次看全）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
