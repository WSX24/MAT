"""课程（设计文档 §4「课程」）。

课程有两个来源，写在同一张表里靠 `source` 区分：

* `run`    —— 扫描 `TeachingFlow/runs/*/*/` 得来（W3 的 `studio.catalog` 负责认目录与命名）；
* `import` —— 学生上传的 pptx（落盘在 `TeachingFlow/state/imports/`，gitignored）。

扫描与导入都**只登记、不解析**（解析是 `POST /units/{id}/prepare` 的事）：
导入时顺手 prepare 一次是为了让列表里立刻有页数与时长，失败也不影响导入成功 ——
"上传了却什么都没发生"比"上传成功了但还没备好课"难受得多。
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from store import repo

from .. import deps
from ..errors import bad_request, not_found

log = logging.getLogger("classroom.app")

router = APIRouter(prefix="/api/v1", tags=["courses"])


def _unit_view(conn, unit: dict, profile_id: str) -> dict:
    return {
        "id": unit["id"],
        "title": unit["title"] or "",
        "slides": int(unit["slides"] or 0),
        "est_sec": round(float(unit["est_sec"] or 0.0), 1),
        "ready": deps.unit_ready(unit),
        "plan_source": unit["plan_source"] or "",
        "progress": repo.unit_progress_view(conn, profile_id, unit["id"]),
    }


@router.get("/courses")
def list_courses(profile_id: str = "", conn=Depends(deps.get_conn)) -> list[dict]:
    pid = (profile_id or "").strip() or repo.active_profile_id(conn)
    out: list[dict] = []
    for c in conn.execute("SELECT * FROM courses ORDER BY updated_at DESC, id").fetchall():
        total, ready = repo.course_unit_counts(conn, c["id"])
        progress = (repo.course_progress(conn, c["id"], pid) if pid
                    else {"started": 0, "completed": 0, "percent": 0.0})
        out.append({
            "id": c["id"],
            "title": c["title"] or "",
            "subject": c["subject"] or "",
            "source": c["source"] or "",
            "unit_count": total,
            "unit_ready_count": ready,
            "progress": progress,
            "updated_at": c["updated_at"] or c["created_at"] or "",
        })
    return out


@router.post("/courses/scan")
def scan_courses(conn=Depends(deps.get_conn)) -> dict:
    """扫描 runs 目录 → 入库。W3 未就绪时返回 0 条 + 一条人话（不是 500）。"""
    found, warnings = deps.scan_runs()
    added = updated = 0
    for course in found:
        cid = str(course.get("id") or "").strip()
        if not cid:
            warnings.append("有一条扫描结果没有 id，已跳过")
            continue
        status = repo.upsert_course(conn, {
            "id": cid,
            "title": course.get("title") or "",
            "subject": course.get("subject") or "",
            "source": course.get("source") or "run",
            "run_dir": course.get("run_dir") or "",
            "source_path": course.get("run_dir") or "",
        })
        added += status == "added"
        updated += status == "updated"
        for u in course.get("units") or []:
            uid = str(u.get("id") or "").strip()
            if not uid:
                continue
            repo.upsert_unit(conn, {
                "id": uid,
                "course_id": cid,
                "chapter": u.get("chapter") or "",
                "title": u.get("title") or "",
                "order_index": u.get("order_index") or 0,
                "pptx_path": u.get("pptx_path") or "",
                "slides": u.get("slides"),
                "est_sec": u.get("est_sec"),
            })
    log.info("扫描完成 added=%s updated=%s total=%s warnings=%s",
             added, updated, len(found), len(warnings))
    return {"added": added, "updated": updated, "total": len(found), "warnings": warnings}


def _looks_like_pptx(path: Path) -> bool:
    """真的是一份 pptx 吗（而不是改了后缀的 zip / docx / 半截下载）。

    只认 `ppt/presentation.xml` 这一个条目：它缺了就是打不开的表，
    早一点 400 比让学生等到"准备课件"里再去猜好。
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return "ppt/presentation.xml" in names


@router.post("/courses/import")
async def import_course(file: UploadFile = File(...),
                        conn=Depends(deps.get_conn)) -> dict:
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".pptx"):
        raise bad_request("只支持 .pptx 课件（PDF / Keynote / 旧版 .ppt 请先另存为 pptx）")
    data = await file.read()
    if not data:
        raise bad_request("这个文件是空的（0 字节）")
    if len(data) > 200 * 1024 * 1024:
        raise bad_request("课件太大了（超过 200MB）")

    digest = hashlib.sha1(data).hexdigest()[:12]
    course_id = f"import-{digest}"
    title = Path(filename).stem or "导入的课件"
    dest_dir = deps.STATE_ROOT / "imports" / course_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{course_id}.pptx"
    dest.write_bytes(data)
    if not _looks_like_pptx(dest):
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise bad_request("这不是有效的 PPTX 文件（缺少 ppt/presentation.xml，可能已损坏）")

    warnings: list[str] = []
    unit, unit_warnings = deps.unit_from_pptx(dest, course_id=course_id, title=title,
                                              chapter="导入", order=0)
    warnings += unit_warnings
    unit_id = str(unit.get("id") or "").strip() or f"u-{digest}"
    repo.upsert_course(conn, {
        "id": course_id, "title": title, "subject": "", "source": "import",
        "run_dir": "", "source_path": str(dest),
    })
    repo.upsert_unit(conn, {
        "id": unit_id, "course_id": course_id, "chapter": unit.get("chapter") or "导入",
        "title": unit.get("title") or title, "order_index": int(unit.get("order_index") or 0),
        "pptx_path": str(dest), "slides": unit.get("slides"), "est_sec": unit.get("est_sec"),
    })

    # 顺手准备一次：列表里立刻有页数/时长，进单元也能看大纲。
    # 备课要模型、可能要几十秒（还没配 key 时是规则兜底，秒级）——
    # 失败只记 warnings，导入本身已经成功了。
    row = repo.get_unit(conn, unit_id)
    prep = deps.prepare_unit(row) if row else {}
    warnings += list(prep.get("warnings") or [])
    if row and (prep.get("deck_ready") or prep.get("plan_ready")):
        repo.mark_unit_prepared(conn, unit_id, slides=prep.get("slides"),
                                est_sec=prep.get("est_sec"),
                                plan_source=prep.get("plan_source") or "")
    after = repo.get_unit(conn, unit_id) or {}
    slides = int(after.get("slides") or prep.get("slides") or 0)
    log.info("导入课件 course=%s unit=%s slides=%s warnings=%s",
             course_id, unit_id, slides, len(warnings))
    return {"course_id": course_id, "unit_id": unit_id, "slides": slides,
            "warnings": warnings}


@router.get("/courses/{course_id}")
def course_detail(course_id: str, profile_id: str = "",
                  conn=Depends(deps.get_conn)) -> dict:
    course = repo.get_course(conn, course_id)
    if course is None:
        raise not_found("这门课程")
    pid = (profile_id or "").strip() or repo.active_profile_id(conn)
    chapters: list[dict] = []
    by_chapter: dict[str, list[dict]] = {}
    for unit in repo.units_of_course(conn, course_id):
        chapter = unit["chapter"] or "未分章"
        if chapter not in by_chapter:
            by_chapter[chapter] = []
            chapters.append({"title": chapter, "units": by_chapter[chapter]})
        by_chapter[chapter].append(_unit_view(conn, unit, pid))
    return {
        "id": course["id"],
        "title": course["title"] or "",
        "subject": course["subject"] or "",
        "source": course["source"] or "",
        "chapters": chapters,
    }


@router.delete("/courses/{course_id}")
def delete_course(course_id: str, conn=Depends(deps.get_conn)) -> dict:
    if repo.get_course(conn, course_id) is None:
        raise not_found("这门课程")
    counts = repo.delete_course(conn, course_id)
    log.info("删除课程 course=%s 连带删除 units=%s sessions=%s",
             course_id, counts["units"], counts["sessions"])
    return {"ok": True}
