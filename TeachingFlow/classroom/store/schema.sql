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

-- 出片任务（课堂这边的"发起 → 跑 flow.py → 登记课程"账本）。
-- stage 是进度阶段（S0…S5），scope_stage 才是用户选的学段 —— 两件事同名会让人在
-- 排障时把 "S3" 当成学段去找，所以列名分开写。
CREATE TABLE IF NOT EXISTS generations (
    id          TEXT PRIMARY KEY,
    goal        TEXT,                        -- 用户当初写下的那一句话
    scope_stage TEXT,                        -- 学段（flow.py --stage）
    subject     TEXT,                        -- 学科（留空 = 让链路按原话推断）
    term        TEXT,                        -- long | short
    level       TEXT,                        -- beginner | intermediate（入库保留接口语义值）
    status      TEXT,                        -- queued|running|succeeded|failed|cancelled|interrupted
    mode        TEXT,                        -- real | mock
    stage       TEXT,                        -- 进度阶段 S0…S5
    stage_label TEXT,
    percent     REAL,
    log_tail    TEXT,                        -- JSON 数组：最后 40 行，首行是命令行原文
    created_at  TEXT,
    started_at  TEXT,
    finished_at TEXT,
    elapsed_sec REAL,
    result      TEXT,                        -- JSON：course_id / course_title / pptx / scan
    error       TEXT
);

-- 模型服务商中枢（服务商 / 密钥 / 默认模型的本地真源）。
-- 为什么不继续用 .env：.env 只能描述"此刻是哪一家"，而页面要的是"列出我配过的全部 +
-- 选一家当默认"，那是多行数据。api_key 存明文是必须的（请求头和出片子进程都要原文），
-- 对外一律由 modelconfig.public_provider() 掩成后 4 位 —— 明文绝不出 HTTP。
CREATE TABLE IF NOT EXISTS model_providers (
    id            TEXT PRIMARY KEY,          -- 'pv-xxxxxxxx'
    name          TEXT NOT NULL,             -- 界面上显示的名字
    kind          TEXT NOT NULL,             -- deepseek|openai|ollama|openrouter|custom
    base_url      TEXT NOT NULL,             -- OpenAI 兼容端点（拉模型时再拼 /models）
    api_key       TEXT,                      -- 明文：只给请求头与子进程用
    models_json   TEXT,                      -- JSON 数组：缓存的模型清单
    models_source TEXT,                      -- provider（拉回来的）| manual（手填）
    created_at    TEXT,
    updated_at    TEXT
);

-- 全局单选的设置项（目前就两条：default_provider_id / default_model）。
-- 做成键值表而不是给 model_providers 加一列 is_default：默认是"库这一级的设置"，
-- 挂在服务商行上会让"删掉默认那家"变成一次跨行更新，漏一处就留下两个默认。
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 索引只服务三类真实查询：按课程取单元、按"档案+单元"找可续学会话、按会话读事件，
-- 以及出片页那一句"现在有没有任务在跑"（全局单并发的判据）。
CREATE INDEX IF NOT EXISTS idx_units_course ON units(course_id, order_index);
CREATE INDEX IF NOT EXISTS idx_sessions_resume ON sessions(profile_id, unit_id, status);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_generations_status ON generations(status, created_at);
