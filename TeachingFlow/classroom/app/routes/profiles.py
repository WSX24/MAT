"""档案（设计文档 §4「档案」）。

本地多档案、无密码：`GET /profiles` 顺带把 active_id 发下去，前端顶栏不用再问一次。
空库时**自动建一个"默认档案"**：开课必须有 profile_id，让前端先画一次"建档案"
对话框才能听课，属于把实现细节漏给学生。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from store import repo

from .. import deps
from ..errors import bad_request, not_found

router = APIRouter(prefix="/api/v1", tags=["profiles"])


class ProfileIn(BaseModel):
    name: str = ""


@router.get("/profiles")
def list_profiles(conn=Depends(deps.get_conn)) -> dict:
    repo.ensure_default_profile(conn)
    return {"active_id": repo.active_profile_id(conn), "items": repo.list_profiles(conn)}


@router.post("/profiles")
def create_profile(body: ProfileIn, conn=Depends(deps.get_conn)) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise bad_request("档案名不能为空（例如「小明」）")
    if len(name) > 40:
        raise bad_request("档案名太长了（最多 40 个字）")
    prof = repo.create_profile(conn, name)
    if not repo.active_profile_id(conn):
        repo.set_active_profile(conn, prof["id"])
    return {"id": prof["id"], "name": prof["name"]}


@router.post("/profiles/{profile_id}/activate")
def activate(profile_id: str, conn=Depends(deps.get_conn)) -> dict:
    if repo.get_profile(conn, profile_id) is None:
        raise not_found("这个档案")
    repo.set_active_profile(conn, profile_id)
    return {"ok": True}


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str, conn=Depends(deps.get_conn)) -> dict:
    if repo.get_profile(conn, profile_id) is None:
        raise not_found("这个档案")
    repo.delete_profile(conn, profile_id)
    return {"ok": True}
