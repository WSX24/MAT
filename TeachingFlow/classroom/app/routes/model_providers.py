"""模型服务商中枢的 HTTP 面（服务商 / 密钥 / 默认模型 / 拉模型 / 导出 .env）。

三条纪律：

1. **明文 key 不出 HTTP**：所有 provider 载荷一律过 modelconfig.public_provider()，
   默认值一律过 modelconfig.public_default()。响应体里只有一个掩码字段
   （api_key_masked），没有 api_key —— 这条不是靠"记得别 dump"，是靠这里
   从来没有出现过明文对象；
2. **路由不干活**：读写、校验、拉模型都在 modelconfig 里，这里只做入参搬运与
   错误码翻译（ValueError → 400、KeyError → 404、ModelFetchError → 502）；
3. **export-env 必须先声明**：FastAPI 按声明顺序匹配，排在 /model-providers/{id}
   后面的话，"export-env" 会被当成一个服务商 id，页面拿到 404 而不是导出结果。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Response

import modelconfig

from .. import deps
from ..errors import ApiError, bad_request, not_found

router = APIRouter(prefix="/api/v1", tags=["model-providers"])


def _body(payload: dict | None) -> dict:
    """请求体：没传 / 传了个 null 都当空对象，别让 422 抢在 400 前面。"""
    return payload if isinstance(payload, dict) else {}


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _provider_body(payload: dict | None, *, partial: bool) -> dict:
    """把请求体收敛成 modelconfig 的关键字实参。

    partial=True（PATCH）时**只搬出现过的键**：缺省与空串在密钥上语义相反
    （不改 vs 清空），所以这里必须能分清"没给"与"给了空"。
    """
    body = _body(payload)
    fields: dict = {}
    for key in ("name", "kind", "base_url", "api_key", "models", "models_source"):
        if key not in body:
            continue
        # partial 时显式 null 也算"没给"：前端把整份表单一起发上来是常事，
        # 一个 null 就把密钥清掉、得重新填一遍，是最不该发生的那种事故
        if partial and body[key] is None:
            continue
        if key == "models":
            fields[key] = body[key]
        else:
            fields[key] = _str(body[key])
    if partial:
        return fields
    fields.setdefault("name", "")
    fields.setdefault("kind", "")
    fields.setdefault("base_url", "")
    fields.setdefault("api_key", "")
    fields.setdefault("models", None)
    fields.setdefault("models_source", "")
    return fields


@router.get("/model-providers")
def list_providers(conn=Depends(deps.get_conn)) -> dict:
    """全部服务商 + 当前默认（没有默认就是 null）。"""
    return {
        "items": modelconfig.list_providers(conn),
        # 走 public_default：get_default() 手里的 provider 带明文 key，
        # 掩码只在这一个函数里发生，路由不接触明文。
        "default": modelconfig.public_default(modelconfig.get_default(conn)),
    }


@router.post("/model-providers", status_code=201)
def create_provider(payload: dict | None = Body(None),
                    conn=Depends(deps.get_conn)) -> dict:
    fields = _provider_body(payload, partial=False)
    try:
        return modelconfig.create_provider(
            conn,
            name=fields["name"], kind=fields["kind"], base_url=fields["base_url"],
            api_key=fields["api_key"], models=fields["models"],
            models_source=fields["models_source"],
        )
    except ValueError as e:
        raise bad_request(str(e))


@router.post("/model-providers/export-env")
def export_env(conn=Depends(deps.get_conn)) -> dict:
    """把默认服务商写回仓库根的 PPT-Agent/.env（幂等，只改那 3 个键）。"""
    return modelconfig.export_env_file(conn)


@router.patch("/model-providers/{provider_id}")
def patch_provider(provider_id: str, payload: dict | None = Body(None),
                   conn=Depends(deps.get_conn)) -> dict:
    fields = _provider_body(payload, partial=True)
    try:
        return modelconfig.update_provider(conn, provider_id, **fields)
    except KeyError:
        raise not_found("这个服务商")
    except ValueError as e:
        raise bad_request(str(e))


@router.delete("/model-providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, conn=Depends(deps.get_conn)) -> Response:
    if modelconfig.get_provider(conn, provider_id) is None:
        raise not_found("这个服务商")
    modelconfig.delete_provider(conn, provider_id)
    # 显式给空 Response：204 不该有响应体，让框架去序列化一个 null 就成了脏数据
    return Response(status_code=204)


@router.post("/model-providers/{provider_id}/models")
def fetch_models(provider_id: str, conn=Depends(deps.get_conn)) -> dict:
    """实时拉 GET {base_url}/models 并缓存回来。"""
    provider = modelconfig.get_provider(conn, provider_id)
    if provider is None:
        raise not_found("这个服务商")
    try:
        models = modelconfig.fetch_models(provider["base_url"], provider["api_key"])
    except modelconfig.ModelFetchError as e:
        # 502 而不是 500：错在服务商那头，课堂这边没坏。code 固定 upstream_error，
        # 前端据此分流（提示里已经有 base_url 与失败原因，够用户自己改）
        raise ApiError(502, str(e), "upstream_error")
    saved = modelconfig.save_models(conn, provider_id, models, "provider")
    return {"models": saved["models"], "models_source": saved["models_source"],
            "fetched_at": saved["updated_at"]}


@router.put("/model-providers/{provider_id}/models")
def save_models(provider_id: str, payload: dict | None = Body(None),
                conn=Depends(deps.get_conn)) -> dict:
    """手填模型清单（models_source 固定 'manual'）。"""
    body = _body(payload)
    if not isinstance(body.get("models"), list):
        raise bad_request("models 必须是一个字符串数组（可以传空数组表示清空）")
    try:
        return modelconfig.save_models(conn, provider_id, body["models"], "manual")
    except KeyError:
        raise not_found("这个服务商")
    except ValueError as e:
        raise bad_request(str(e))


@router.post("/model-providers/{provider_id}/probe")
def probe(provider_id: str, payload: dict | None = Body(None),
          conn=Depends(deps.get_conn)) -> dict:
    """真发一次最小对话（「测试对话」按钮）：结论一律 200，只有服务商不存在才 404。

    为什么不复用 /models：余额为 0 时它照样 200，用户会以为配好了，
    直到出片跑到 S0 才吃到 402。这条才是"现在到底能不能用"的判据。
    """
    try:
        return modelconfig.probe_chat(conn, provider_id, _str(_body(payload).get("model")))
    except KeyError:
        raise not_found("这个服务商")


@router.post("/model-default")
def set_default(payload: dict | None = Body(None),
                conn=Depends(deps.get_conn)) -> dict:
    """设默认服务商与默认模型。"""
    body = _body(payload)
    provider_id = _str(body.get("provider_id"))
    model = _str(body.get("model"))
    if not provider_id:
        raise bad_request("provider_id 不能为空（先选一家服务商）")
    if not model:
        raise bad_request("model 不能为空（先选一个模型再保存）")
    try:
        result = modelconfig.set_default(conn, provider_id, model)
    except KeyError:
        raise not_found("这个服务商")
    except ValueError as e:
        raise bad_request(str(e))
    # warning 只在"库里已知该服务商的模型清单、而选的这一个不在里面"时说话：
    # 这是最常见的手滑（模型 id 少写一个 -），等出片时才报错就太晚了。
    warning = ""
    known = (result.get("provider") or {}).get("models") or []
    if known and model not in known:
        warning = (f"这个模型不在服务商的清单里（{model}），大概率调不通："
                   f"先点「拉取模型」确认一下")
    return {**result, "warning": warning}
