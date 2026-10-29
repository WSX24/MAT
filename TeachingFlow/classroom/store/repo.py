"""全部 sqlite 读写语句（设计文档 §3 的仓储层）。

分成"档案 / 课程 / 单元 / 会话 / 进度 / 事件"六段，函数名一律动词开头、第一参数是连接。

三条纪律：

1. **孤儿行在这里就地清掉**，不指望外键。sqlite 的外键默认关着，而且 `sessions`
   与 `units` 之间设计文档没有声明外键 —— 于是"删课程"必须显式删掉它的单元、
   这些单元的会话、这些会话的事件与进度。漏一处就会在库留下谁也读不到的行，
   而且下次导入同一门课时这些行会**跟着回来**（id 是稳定的），表现为"删了又出现"。
2. **事件有上限**：每个会话只留最近 `EVENTS_KEEP` 条。课堂一次节课几千条事件，
   不设上限的话库会按周增长，而 /dev 面板只翻最近几百条。
3. **时间一律 ISO 文本**（秒级）。sqlite 没有真日期类型，全库统一一种写法，
   排序即字典序，前端也不用再做时区转换。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

#: 每个会话最多保留的事件条数（设计文档 §4 /dev 面板只翻最近几条）
EVENTS_KEEP = 500


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _as_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _as_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ===========================================================================
# 档案
# ===========================================================================

def list_profiles(conn: sqlite3.Connection) -> list[dict]:
    # 排序键用 rowid 而不是 id：`created_at` 精确到秒，同一秒建的两个档案若按随机
    # id 排，列表顺序会在两次刷新之间跳。rowid 就是插入顺序，稳定且可解释。
    return _as_dicts(conn.execute(
        "SELECT id, name, created_at FROM profiles ORDER BY created_at, rowid"
    ).fetchall())


def get_profile(conn: sqlite3.Connection, profile_id: str) -> dict | None:
    return _as_dict(conn.execute(
        "SELECT id, name, created_at FROM profiles WHERE id=?", (profile_id,)
    ).fetchone())


def create_profile(conn: sqlite3.Connection, name: str) -> dict:
    row = {"id": _new_id("p"), "name": name, "created_at": _now()}
    conn.execute(
        "INSERT INTO profiles(id, name, created_at) VALUES(:id, :name, :created_at)", row
    )
    conn.commit()
    return row


def delete_profile(conn: sqlite3.Connection, profile_id: str) -> dict:
    """删档案 + 它的会话/事件/进度（换档案进度互不干扰，删档案也不许留渣）。"""
    session_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM sessions WHERE profile_id=?", (profile_id,))]
    for sid in session_ids:
        conn.execute("DELETE FROM events WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE profile_id=?", (profile_id,))
    conn.execute("DELETE FROM progress WHERE profile_id=?", (profile_id,))
    conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    if active_profile_id(conn) == profile_id:
        _set_meta(conn, ACTIVE_PROFILE_KEY, "")
    conn.commit()
    return {"sessions": len(session_ids)}


def ensure_default_profile(conn: sqlite3.Connection, name: str = "默认档案") -> dict:
    """空库时建一个默认档案并激活。

    为什么不留"零档案"这个状态：开课必须有 profile_id，而 `POST /sessions` 不该
    因为"前端还没画过档案选择器"就失败。空态在课程库那边表达就够了。
    """
    items = list_profiles(conn)
    if items:
        if not active_profile_id(conn):
            set_active_profile(conn, items[0]["id"])
            return items[0]
        return items[0]
    prof = create_profile(conn, name)
    set_active_profile(conn, prof["id"])
    return prof


ACTIVE_PROFILE_KEY = "active_profile_id"


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
    )


def active_profile_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (ACTIVE_PROFILE_KEY,)).fetchone()
    pid = str(row["value"] or "") if row is not None else ""
    if pid and get_profile(conn, pid) is None:
        # 档案被删了但 meta 还指着它（旧库/手改库）：兜底成第一个，别把浏览器带进 404
        items = list_profiles(conn)
        pid = items[0]["id"] if items else ""
    return pid


def set_active_profile(conn: sqlite3.Connection, profile_id: str) -> None:
    _set_meta(conn, ACTIVE_PROFILE_KEY, profile_id)
    conn.commit()


# ===========================================================================
# 课程与单元
# ===========================================================================

def get_course(conn: sqlite3.Connection, course_id: str) -> dict | None:
    return _as_dict(conn.execute(
        "SELECT * FROM courses WHERE id=?", (course_id,)).fetchone())


def upsert_course(conn: sqlite3.Connection, course: dict) -> str:
    """写入或更新课程，返回 'added' / 'updated'（扫描接口的计数就来自这里）。"""
    existed = get_course(conn, course["id"]) is not None
    now = _now()
    conn.execute(
        """INSERT INTO courses(id, title, subject, source, run_dir, source_path,
                               created_at, updated_at)
           VALUES(:id, :title, :subject, :source, :run_dir, :source_path, :created_at, :updated_at)
           ON CONFLICT(id) DO UPDATE SET
               title=excluded.title,
               subject=excluded.subject,
               source=excluded.source,
               run_dir=excluded.run_dir,
               source_path=excluded.source_path,
               updated_at=excluded.updated_at""",
        {
            "id": course["id"],
            "title": course.get("title") or "",
            "subject": course.get("subject") or "",
            "source": course.get("source") or "",
            "run_dir": course.get("run_dir") or "",
            "source_path": course.get("source_path") or "",
            "created_at": course.get("created_at") or now,
            "updated_at": now,
        },
    )
    conn.commit()
    return "updated" if existed else "added"


def upsert_unit(conn: sqlite3.Connection, unit: dict) -> None:
    """写入或更新单元。

    重新扫描**不许**清掉已备好的信息（plan_source / prepared_at）：
    扫描只是"磁盘上有些什么"，备课状态是另一件事，被扫描顺手抹掉的话
    表现是"昨天备好的课今天又要重新准备一遍"。
    """
    conn.execute(
        """INSERT INTO units(id, course_id, chapter, title, order_index, pptx_path,
                             slides, est_sec, plan_source, prepared_at)
           VALUES(:id, :course_id, :chapter, :title, :order_index, :pptx_path,
                  :slides, :est_sec, :plan_source, :prepared_at)
           ON CONFLICT(id) DO UPDATE SET
               course_id=excluded.course_id,
               chapter=excluded.chapter,
               title=excluded.title,
               order_index=excluded.order_index,
               pptx_path=excluded.pptx_path,
               slides=COALESCE(excluded.slides, units.slides),
               est_sec=COALESCE(excluded.est_sec, units.est_sec),
               plan_source=CASE WHEN excluded.plan_source <> '' THEN excluded.plan_source
                                ELSE units.plan_source END,
               prepared_at=COALESCE(excluded.prepared_at, units.prepared_at)""",
        {
            "id": unit["id"],
            "course_id": unit.get("course_id") or "",
            "chapter": unit.get("chapter") or "",
            "title": unit.get("title") or "",
            "order_index": int(unit.get("order_index") or 0),
            "pptx_path": unit.get("pptx_path") or "",
            "slides": unit.get("slides"),
            "est_sec": unit.get("est_sec"),
            "plan_source": unit.get("plan_source") or "",
            "prepared_at": unit.get("prepared_at"),
        },
    )
    conn.commit()


def mark_unit_prepared(conn: sqlite3.Connection, unit_id: str, *,
                        slides: int | None = None, est_sec: float | None = None,
                        plan_source: str = "", prepared_at: str | None = None) -> None:
    """备课成功后回填单元的"备过一次"信息（页数/时长/来源/时间）。"""
    conn.execute(
        """UPDATE units SET
               slides=COALESCE(?, slides),
               est_sec=COALESCE(?, est_sec),
               plan_source=CASE WHEN ?='' THEN plan_source ELSE ? END,
               prepared_at=?
           WHERE id=?""",
        (slides, est_sec, plan_source, plan_source, prepared_at or _now(), unit_id),
    )
    conn.commit()


def get_unit(conn: sqlite3.Connection, unit_id: str) -> dict | None:
    return _as_dict(conn.execute(
        "SELECT * FROM units WHERE id=?", (unit_id,)).fetchone())


def units_of_course(conn: sqlite3.Connection, course_id: str) -> list[dict]:
    return _as_dicts(conn.execute(
        "SELECT * FROM units WHERE course_id=? ORDER BY order_index, id", (course_id,)
    ).fetchall())


def course_unit_counts(conn: sqlite3.Connection, course_id: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN prepared_at IS NOT NULL THEN 1 ELSE 0 END) AS ready "
        "FROM units WHERE course_id=?", (course_id,)
    ).fetchone()
    return int(row["total"] or 0), int(row["ready"] or 0)


def course_ppt_counts(conn: sqlite3.Connection, course_id: str) -> tuple[int, int]:
    """(单元总数, 已有课件的单元数)。

    判据是 ``pptx_path`` 非空而不是"磁盘上文件此刻还在"：列表页每次刷新都去
    ``is_file()`` 一遍，等于把一次 SELECT 变成 N 次磁盘探测（课件在机械盘/网络盘
    上时肉眼可见地慢）。而 ``pptx_path`` 只在"这一课真的产出了片"时才写 ——
    它本身就是那份账本。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN COALESCE(TRIM(pptx_path), '') <> '' THEN 1 ELSE 0 END) AS with_ppt "
        "FROM units WHERE course_id=?", (course_id,)
    ).fetchone()
    return int(row["total"] or 0), int(row["with_ppt"] or 0)


def delete_course(conn: sqlite3.Connection, course_id: str) -> dict:
    """删课程 + 它的单元 + 这些单元的会话/进度/事件。返回各表删了多少（便于自检）。"""
    unit_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM units WHERE course_id=?", (course_id,))]
    session_ids: list[str] = []
    for uid in unit_ids:
        session_ids += [r["id"] for r in conn.execute(
            "SELECT id FROM sessions WHERE unit_id=?", (uid,))]
    for sid in session_ids:
        conn.execute("DELETE FROM events WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE unit_id IN "
                 "(SELECT id FROM units WHERE course_id=?)", (course_id,))
    conn.execute("DELETE FROM progress WHERE unit_id IN "
                 "(SELECT id FROM units WHERE course_id=?)", (course_id,))
    conn.execute("DELETE FROM units WHERE course_id=?", (course_id,))
    # 单课出片任务与建课会话也是这门课的孩子：
    #   · 任务行留着会变成"永远在跑"的孤儿 —— 它占着「一次只做一课」的名额，
    #     用户从此点不动「生成下一课」（对账只在服务启动时跑，救不了这一次）；
    #   · 会话留着会指向一门不存在的课，前端点「去课程页」就是 404。
    # 会话本身不删（聊天记录还在），只把 course_id 断掉。
    with _suppress_missing_table():
        conn.execute("DELETE FROM unit_ppt_jobs WHERE course_id=?", (course_id,))
        conn.execute("UPDATE course_builder_sessions SET course_id='' WHERE course_id=?",
                     (course_id,))
    conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
    conn.commit()
    return {"units": len(unit_ids), "sessions": len(session_ids)}


@contextlib.contextmanager
def _suppress_missing_table():
    """老库还没跑到建课那一步时，这两张表可能不存在 —— 删除课程不该因此失败。"""
    try:
        yield
    except sqlite3.OperationalError:
        pass


# ===========================================================================
# 会话
# ===========================================================================

def create_session(conn: sqlite3.Connection, unit_id: str, profile_id: str,
                   *, position_slide: int = 0, position_unit: int = 0) -> dict:
    row = {
        "id": _new_id("s"),
        "unit_id": unit_id,
        "profile_id": profile_id,
        "status": "active",
        "started_at": _now(),
        "ended_at": None,
        "elapsed_sec": 0.0,
        "position_slide": int(position_slide),
        "position_unit": int(position_unit),
        "comprehension": "{}",
        "qa_count": 0,
    }
    conn.execute(
        """INSERT INTO sessions(id, unit_id, profile_id, status, started_at, ended_at,
                                elapsed_sec, position_slide, position_unit, comprehension, qa_count)
           VALUES(:id, :unit_id, :profile_id, :status, :started_at, :ended_at,
                  :elapsed_sec, :position_slide, :position_unit, :comprehension, :qa_count)""",
        row,
    )
    conn.commit()
    return row


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    return _as_dict(conn.execute(
        "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())


def resumable_session(conn: sqlite3.Connection, profile_id: str, unit_id: str) -> dict | None:
    """最近一条可以续学的会话（active / paused），没有就 None。"""
    return _as_dict(conn.execute(
        "SELECT * FROM sessions WHERE profile_id=? AND unit_id=? "
        "AND status IN ('active','paused') ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (profile_id, unit_id),
    ).fetchone())


def update_session(conn: sqlite3.Connection, session_id: str, **fields: Any) -> None:
    allowed = {"status", "ended_at", "elapsed_sec", "position_slide", "position_unit",
               "comprehension", "qa_count"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"session 没有这一列：{key}")
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(session_id)
    conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()


def latest_session(conn: sqlite3.Connection, profile_id: str, unit_id: str) -> dict | None:
    return _as_dict(conn.execute(
        "SELECT * FROM sessions WHERE profile_id=? AND unit_id=? "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1", (profile_id, unit_id)).fetchone())


def delete_session(conn: sqlite3.Connection, session_id: str) -> dict:
    cur = conn.execute("DELETE FROM events WHERE session_id=?", (session_id,))
    events = cur.rowcount or 0
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    return {"events": events}


# ===========================================================================
# 进度
# ===========================================================================

def upsert_progress(conn: sqlite3.Connection, profile_id: str, unit_id: str, *,
                    percent: float, last_slide: int = 0, last_unit: int = 0,
                    study_sec: float = 0.0) -> None:
    """写进度。percent 只增不减 —— 回看前面几页不该把"学了多少"打回去。"""
    conn.execute(
        """INSERT INTO progress(profile_id, unit_id, percent, last_slide, last_unit,
                                study_sec, updated_at)
           VALUES(?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(profile_id, unit_id) DO UPDATE SET
               percent=MAX(progress.percent, excluded.percent),
               last_slide=excluded.last_slide,
               last_unit=excluded.last_unit,
               study_sec=MAX(progress.study_sec, excluded.study_sec),
               updated_at=excluded.updated_at""",
        (profile_id, unit_id, round(float(percent), 1), int(last_slide), int(last_unit),
         round(float(study_sec), 1), _now()),
    )
    conn.commit()


def get_progress(conn: sqlite3.Connection, profile_id: str, unit_id: str) -> dict | None:
    return _as_dict(conn.execute(
        "SELECT * FROM progress WHERE profile_id=? AND unit_id=?", (profile_id, unit_id)
    ).fetchone())


def course_progress(conn: sqlite3.Connection, course_id: str, profile_id: str) -> dict:
    """一门课对某个档案的进度。

    口径（`started` / `completed` 是**单元数**，不是布尔）：
      · started   = 有进度行或有过会话的单元数；
      · completed = percent >= 99.5 的单元数；
      · percent   = 已开始单元的平均 percent（没开始的单元按 0 计入分母）。
    """
    total, _ready = course_unit_counts(conn, course_id)
    rows = conn.execute(
        "SELECT p.unit_id, p.percent FROM progress p JOIN units u ON u.id = p.unit_id "
        "WHERE u.course_id=? AND p.profile_id=?", (course_id, profile_id)
    ).fetchall()
    by_unit = {r["unit_id"]: float(r["percent"] or 0.0) for r in rows}
    session_units = {r["unit_id"] for r in conn.execute(
        "SELECT DISTINCT s.unit_id FROM sessions s JOIN units u ON u.id = s.unit_id "
        "WHERE u.course_id=? AND s.profile_id=?", (course_id, profile_id)).fetchall()}
    started_units = set(by_unit) | session_units
    completed = sum(1 for uid in started_units if by_unit.get(uid, 0.0) >= 99.5)
    percent = round(sum(by_unit.values()) / total, 1) if total else 0.0
    return {
        "started": len(started_units),
        "completed": completed,
        "percent": percent,
    }


def unit_progress_view(conn: sqlite3.Connection, profile_id: str, unit_id: str) -> dict:
    """单元进度（课程详情/单元详情里的 progress 字段）。"""
    row = get_progress(conn, profile_id, unit_id)
    sess = latest_session(conn, profile_id, unit_id)
    percent = float(row["percent"]) if row else 0.0
    return {
        "percent": round(percent, 1),
        "last_slide": int(row["last_slide"]) if row else 0,
        # status 直接取最近一次会话的状态（active|paused|ended）；
        # 没有会话但有进度 = 学过但没开过课（导入/预置的进展），给 "started"。
        "status": (sess or {}).get("status") or ("started" if row else ""),
    }


# ===========================================================================
# 事件
# ===========================================================================

def add_event(conn: sqlite3.Connection, session_id: str, kind: str,
              payload: dict | None = None) -> None:
    """写一条事件，并把超出上限的老事件删掉（`EVENTS_KEEP` 条/会话）。"""
    conn.execute(
        "INSERT INTO events(session_id, ts, kind, payload) VALUES(?, ?, ?, ?)",
        (session_id, _now(), kind,
         json.dumps(payload or {}, ensure_ascii=False, default=str)),
    )
    # 只留最近 N 条：按 id 倒序取一个子查询，删掉不在里面的。
    # 一次 DELETE 一条语句，比"先 COUNT 再决定"少一次往返。
    conn.execute(
        "DELETE FROM events WHERE session_id=? AND id NOT IN "
        "(SELECT id FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?)",
        (session_id, session_id, EVENTS_KEEP),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, session_id: str, limit: int = EVENTS_KEEP) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, kind, payload FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, int(limit)),
    ).fetchall()
    out = []
    for r in reversed(rows):        # 时间正序返回，前端不用再翻
        item = dict(r)
        item["payload"] = _loads(item.get("payload"), {})
        out.append(item)
    return out


def latest_event(conn: sqlite3.Connection, session_id: str, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT id, ts, kind, payload FROM events WHERE session_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1", (session_id, kind),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.get("payload"), {})
    return item


def count_events(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE session_id=?",
                       (session_id,)).fetchone()
    return int(row["n"] or 0)
