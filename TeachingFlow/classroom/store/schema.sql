-- 课堂正式版 sqlite 结构（设计文档 §3 的逐字落地）
-- 口径：只加不删。新版本迁移只能追加 IF NOT EXISTS / 带条件判断的 ALTER，
-- 任何 DROP / 重建 / 改名都是破坏性迁移（学生会丢进度），明确禁止。
-- 迁移版本记在 meta.schema_version（见 db.py:migrate）。

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    subject     TEXT,
    source      TEXT,                        -- 'run' | 'import'
    run_dir     TEXT,
    source_path TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS units (
    id          TEXT PRIMARY KEY,
    course_id   TEXT REFERENCES courses(id) ON DELETE CASCADE,
    chapter     TEXT,
    title       TEXT,
    order_index INTEGER,
    pptx_path   TEXT,
    slides      INTEGER,
    est_sec     REAL,
    plan_source TEXT,                         -- 'llm' | 'rule' | ''
    prepared_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    unit_id        TEXT,
    profile_id     TEXT,
    status         TEXT,                      -- active | paused | ended
    started_at     TEXT,
    ended_at       TEXT,
    elapsed_sec    REAL,
    position_slide INTEGER,
    position_unit  INTEGER,
    comprehension  TEXT,                      -- JSON：unit_id -> confirmed|vague|confused
    qa_count       INTEGER
);

CREATE TABLE IF NOT EXISTS progress (
    profile_id TEXT,
    unit_id    TEXT,
    percent    REAL,
    last_slide INTEGER,
    last_unit  INTEGER,
    study_sec  REAL,
    updated_at TEXT,
    PRIMARY KEY (profile_id, unit_id)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts         TEXT,
    kind       TEXT,
    payload    TEXT
);

-- 索引只服务三类真实查询：按课程取单元、按"档案+单元"找可续学会话、按会话读事件。
CREATE INDEX IF NOT EXISTS idx_units_course ON units(course_id, order_index);
CREATE INDEX IF NOT EXISTS idx_sessions_resume ON sessions(profile_id, unit_id, status);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
