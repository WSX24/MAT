"""HTTP 错误：一律中文人话 + 统一 JSON 形状。

为什么不直接用 HTTPException：`detail` 里塞中文是可以的，但前端的处理会变成
"有时 detail 是字符串、有时是校验错误数组"，W2 得写两套。这里统一成
`{ok:false, message:str, code:str}`，成功响应则一律是文档 §4 的形状（不带 ok 字段），
两者靠 HTTP 状态区分，没有歧义。

另一个理由是可观测：每个 ApiError 都带 `code`（`not_found` / `bad_request` / …），
日志里能按 code 聚合，而中文 message 会随文案变。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("classroom.app")


class ApiError(Exception):
    """带 HTTP 状态的业务错误（路由里 raise，由下面的 handler 转成 JSON）。"""

    def __init__(self, status_code: int, message: str, code: str = ""):
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = message
        self.code = code or _default_code(self.status_code)


def _default_code(status: int) -> str:
    return {400: "bad_request", 404: "not_found", 409: "conflict",
            422: "invalid", 503: "unavailable"}.get(int(status), "error")


def not_found(what: str) -> ApiError:
    return ApiError(404, f"找不到{what}", "not_found")


def bad_request(message: str) -> ApiError:
    return ApiError(400, message, "bad_request")


def _payload(message: str, code: str) -> dict:
    return {"ok": False, "message": message, "code": code}


def install_error_handlers(app: FastAPI) -> None:
    """挂上三个 handler：业务错误、参数校验、兜底。"""

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        log.warning("api_error status=%s code=%s message=%s",
                    exc.status_code, exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=_payload(exc.message, exc.code))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # 参数不对是"你填错了"，把第一个错的位置说清楚就够了，整段 schema 对学生没意义
        first = (exc.errors() or [{}])[0]
        where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query"))
        detail = first.get("msg", "格式不对")
        message = f"请求参数不对{f'（{where}）' if where else ''}：{detail}"
        return JSONResponse(status_code=422, content=_payload(message, "invalid"))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：路由里没预料到的异常。中文提示 + 完整堆栈进日志。
        # 有这一层，"没配 key 也不许 500"这类要求才有一个能看见的落点。
        log.exception("unhandled path=%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content=_payload(f"服务器内部错误：{type(exc).__name__}: {exc}", "internal"),
        )
