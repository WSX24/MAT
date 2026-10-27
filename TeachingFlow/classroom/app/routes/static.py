"""静态资源与 SPA 兜底（设计文档 §4「健康与静态」）。

`webapp/dist` **不存在时也要 200**：W2 与后端并行开发，没人愿意因为"前端还没 build"
就看到 500 —— 那会让人以为后端坏了。这里返回一个说明性页面（中文、带命令），
既保住了"能起"，又把"接下来该干什么"说清楚。

顺带服务两类真正需要的二进制资源（设计文档 §4 没有这两条路由，但它们是
WS 帧 `audio.url` 与课件重绘 `media_base` 的落点，不服务的话那两个字段就是死链）：

* `/assets/audio/<hash>.mp3` —— TTS 缓存（`speech.TTS` 的产物，文件名即内容哈希）
* `/assets/media/<deck_id>/<name>` —— 课件里的图片/媒体

两个路由都做了路径收敛（只认缓存目录 / 课件媒体目录里的文件），
因为这两个参数直接来自浏览器。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response

from .. import deps
from ..errors import not_found

log = logging.getLogger("classroom.app")

router = APIRouter(tags=["static"])

#: TTS 缓存文件名 = sha1 前 20 位 + .mp3（见 speech.TTS.key）
_AUDIO_NAME = re.compile(r"^[0-9a-f]{20}\.mp3$")

MISSING_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>课堂 · 前端还没构建</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{color-scheme:light dark}
 body{font:16px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;
      display:grid;place-items:center;min-height:100vh;background:#0f1115;color:#e7e9ee}
 main{max-width:46rem;padding:2.5rem}
 h1{font-size:1.5rem;margin:0 0 1rem}
 code{background:#1e222b;padding:.15rem .4rem;border-radius:.3rem}
 pre{background:#1e222b;padding:1rem;border-radius:.5rem;overflow:auto}
 .ok{color:#5fd18b} .dim{color:#9aa3b2}
</style></head>
<body><main>
 <h1>后端已经起来了 <span class="ok">✓</span></h1>
 <p>这个地址是前端页面（<code>webapp/dist</code>），但构建产物还不存在。
    后端 API 与课堂 WebSocket 都正常工作。</p>
 <p class="dim">要看到界面，构建一次前端：</p>
 <pre>cd TeachingFlow/classroom/webapp
npm install
npm run build</pre>
 <p class="dim">健康检查：<a href="/api/health">/api/health</a></p>
</main></body></html>
"""


def _dist() -> Path:
    return deps.dist_root()


def _serve_or_placeholder() -> Response:
    index = _dist() / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html; charset=utf-8")
    return HTMLResponse(MISSING_HTML, status_code=200)


@router.get("/assets/audio/{name}")
def audio(name: str) -> Response:
    if not _AUDIO_NAME.match(name):
        raise not_found("这段音频")
    settings = deps.get_settings()
    if settings is None:
        raise not_found("这段音频（配置读不出来）")
    path = Path(settings.cache_dir) / "tts" / name
    if not path.is_file():
        raise not_found("这段音频（可能已被清理，不影响继续听讲）")
    return FileResponse(path, media_type="audio/mpeg")


@router.get("/assets/{asset_path:path}")
def asset(asset_path: str) -> Response:
    base = (_dist() / "assets").resolve()
    path = (base / asset_path).resolve()
    if base != path and base not in path.parents:
        raise not_found("这个文件")
    if not path.is_file():
        raise not_found("这个文件")
    return FileResponse(path)


@router.get("/", response_class=HTMLResponse)
def index() -> Response:
    return _serve_or_placeholder()


@router.get("/{spa_path:path}", response_class=HTMLResponse)
def spa(spa_path: str) -> Response:
    """SPA 深链兜底（`/courses/:id`、`/learn/:unitId` 刷新后也要能打开）。

    API / WS / dev 路径**不许**落到这里：一条写错的接口地址如果返回首页，
    排查的人会以为"接口存在但返回 HTML"，比 404 难查得多。
    """
    # 用第一段路径判断而不是 startswith("api/")：`/api`（没有尾斜杠）也是一条接口地址，
    # 它落到首页的话，排查的人会以为"接口存在但返回 HTML"。
    if spa_path.split("/", 1)[0] in ("api", "ws", "dev", "assets"):
        raise not_found("接口")
    return _serve_or_placeholder()
