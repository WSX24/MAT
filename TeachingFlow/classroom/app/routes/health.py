"""健康检查（设计文档 §4 `GET /api/health`）。

**这个路由永远 200**：没配 key、库还没建、TTS 没装，都要能如实说出来。
它是"服务活着吗"的唯一探针，自己先倒掉的话，前端顶栏那个状态点就永远红着，
而人根本分不清是"课堂坏了"还是"健康检查坏了"。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from store import db as db_mod

from .. import deps

log = logging.getLogger("classroom.app")

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health() -> dict:
    courses = 0
    try:
        conn = db_mod.connect()
        try:
            deps.ensure_db(conn)
            row = conn.execute("SELECT COUNT(*) AS n FROM courses").fetchone()
            courses = int(row["n"] or 0)
        finally:
            conn.close()
    except Exception as e:                     # 库坏了也只汇报，不改状态码
        log.warning("健康检查读库失败：%s: %s", type(e).__name__, e)
    return {
        "ok": True,
        "version": deps.APP_VERSION,
        "courses": courses,
        "engine": deps.engine_info(),
    }
