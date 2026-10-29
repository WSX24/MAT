"""模型服务商中枢：服务商 / 密钥 / 默认模型的唯一真源（库里的，不是环境变量里的）。

为什么把真源从 .env 搬进本地库：

1. .env 只能描述"此刻用的是哪一家"，而这一层要回答的是"我配了哪几家、
   默认哪一家、默认哪个模型" —— 那是多行数据，键值对表达不了；
2. 界面要能增删改。写 .env 的路径已经有两份（config.save_env_values 与 GUI 面板），
   再让"服务商清单"也掺进去，"改一处不生效"这类故障会翻倍；
3. 出片子进程只认 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 三个键，
   所以库里的默认值最后还得有一层**出口**把它翻译回去 —— 见
   env_for_subprocess()（给子进程）与 export_env_file()（落盘给别的工具读）。

明文 key 的边界（本模块是**唯一**允许它出现的地方）：

* get_provider() / get_default() 带明文，因为它们服务的是"发请求"与"拼子进程环境"；
* 其它一切对外形状都过 public_provider() / public_default()，只留后 4 位；
* HTTP 层只准把 public_*() 的结果塞进响应体 —— 明文永远不出进程。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

#: 认得的服务商类型。kind 只影响界面上的图标与提示，不影响请求 —— 它们全是
#: OpenAI 兼容端点（拉模型都是 GET {base_url}/models），所以这里不做分支。
KINDS: tuple[str, ...] = ("deepseek", "openai", "ollama", "openrouter", "custom")

#: 模型清单的来源：拉回来的（provider）/ 手填的（manual）/ 还没填过（''）
MODELS_SOURCES: tuple[str, ...] = ("provider", "manual")

#: flow.py 唯一认识的三个键（见 env_for_subprocess / export_env_file）
ENV_KEYS: tuple[str, str, str] = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
)

DEFAULT_PROVIDER_KEY = "default_provider_id"
DEFAULT_MODEL_KEY = "default_model"

#: TeachingFlow/classroom → TeachingFlow → <仓库根>
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def repo_env_file() -> Path:
    """默认落盘的 .env：仓库根那份（PPT-Agent/.env）。

    做成函数而不是常量，是为了让测试能把它指到不存在的位置 ——
    否则跑一次 pytest 就会把真正的那份配置改掉。
    """
    return _REPO_ROOT / "PPT-Agent" / ".env"


class ModelFetchError(Exception):
    """拉模型清单失败（message 是能直接给用户看的一句话，含 base_url 与失败原因）。"""


# ===========================================================================
# 小工具
# ===========================================================================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"pv-{uuid.uuid4().hex[:8]}"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def mask_key(api_key: str) -> str:
    """只露后 4 位；短于 8 位一律 ****。

    为什么连前缀都不留（例如 sk-）：前缀也是明文的一部分，而"掩码"的验收口径
    只有一条 —— 泄漏的信息越少越好。短 key 露 4 位等于露掉一半，8 位以下直接不给。
    """
    key = str(api_key or "")
    if not key:
        return ""
    if len(key) < 8:
        return "****"
    return "\u2026" + key[-4:]


def _loads_models(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(m) for m in data if isinstance(m, str) and m]


def _normalize_models(models: Any) -> list[str]:
    """规整模型清单：去空白、去重、保序。非字符串直接拒（None 视为空清单）。"""
    if models is None:
        return []
    if not isinstance(models, (list, tuple)):
        raise ValueError("models 必须是一个字符串数组")
    out: list[str] = []
    seen: set[str] = set()
    for item in models:
        name = _text(item)
        if not name:
            raise ValueError("models 里每一项都得是非空字符串")
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _check_fields(*, name: str, kind: str, base_url: str) -> None:
    """入参自检。抛 ValueError（中文人话），由 HTTP 层翻译成 400。

    放这里而不是只放路由里：另一条工作流会直接 import 本模块写库，
    "只有 HTTP 入口才校验"等于库能被写进半条记录。
    """
    if not name:
        raise ValueError("服务商名称不能为空（例如「DeepSeek」）")
    if len(name) > 60:
        raise ValueError("服务商名称太长了（最多 60 个字）")
    if kind not in KINDS:
        raise ValueError(f"不认识的服务商类型：{kind or '（空）'}（可选：{' / '.join(KINDS)}）")
    if not base_url:
        raise ValueError("base_url 不能为空（例如 https://api.deepseek.com/v1）")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"base_url 必须是以 http:// 或 https:// 开头的地址：{base_url}")


def _internal(row: Any) -> dict:
    """库里的行 → 内部形状（**含明文 api_key**，只给本进程用）。"""
    data = dict(row)
    return {
        "id": str(data.get("id") or ""),
        "name": str(data.get("name") or ""),
        "kind": str(data.get("kind") or ""),
        "base_url": str(data.get("base_url") or ""),
        "api_key": str(data.get("api_key") or ""),
        "models": _loads_models(data.get("models_json")),
        "models_source": str(data.get("models_source") or ""),
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def public_provider(provider: dict) -> dict:
    """内部形状 → 对外形状（掩码 key）。**HTTP 与内部共用这一个形状。**"""
    api_key = str(provider.get("api_key") or "")
    return {
        "id": str(provider.get("id") or ""),
        "name": str(provider.get("name") or ""),
        "kind": str(provider.get("kind") or ""),
        "base_url": str(provider.get("base_url") or ""),
        "api_key_masked": mask_key(api_key),
        "has_key": bool(api_key),
        "models": list(provider.get("models") or []),
        "models_source": str(provider.get("models_source") or ""),
        "created_at": str(provider.get("created_at") or ""),
        "updated_at": str(provider.get("updated_at") or ""),
    }


def public_default(default: dict | None) -> dict | None:
    """get_default() → 能上 HTTP 的形状（provider 重新掩码）。

    单独一个函数是为了让"哪个入口掩码"没有第二种写法：路由里拿到的默认值
    只可能是这个函数的产物，漏掉掩码要改的地方只有一处。
    """
    if not default:
        return None
    return {
        "provider_id": str(default.get("provider_id") or ""),
        "model": str(default.get("model") or ""),
        "provider": public_provider(default.get("provider") or {}),
    }


# ===========================================================================
# 服务商 CRUD
# ===========================================================================

def list_providers(conn: sqlite3.Connection) -> list[dict]:
    """全部服务商（对外形状，按创建顺序）。

    排序键用 rowid 而不是 id：created_at 只精确到秒，同一秒建的两家若按随机 id 排，
    列表顺序会在两次刷新之间跳 —— 用户会以为"有一条没了"。
    """
    rows = conn.execute(
        "SELECT * FROM model_providers ORDER BY created_at, rowid"
    ).fetchall()
    return [public_provider(_internal(r)) for r in rows]


def get_provider(conn: sqlite3.Connection, provider_id: str) -> dict | None:
    """一家服务商（**含明文 api_key**，仅后端内部用：拼请求头 / 拼子进程环境）。"""
    row = conn.execute(
        "SELECT * FROM model_providers WHERE id=?", (str(provider_id or ""),)
    ).fetchone()
    return None if row is None else _internal(row)


def create_provider(conn: sqlite3.Connection, *, name: str, kind: str, base_url: str,
                    api_key: str, models: Any = None,
                    models_source: str = "") -> dict:
    name, kind, base_url = _text(name), _text(kind), _text(base_url)
    _check_fields(name=name, kind=kind, base_url=base_url)
    now = _now()
    row = {
        "id": _new_id(),
        "name": name,
        "kind": kind,
        "base_url": base_url,
        "api_key": api_key if isinstance(api_key, str) else "",
        "models_json": json.dumps(_normalize_models(models), ensure_ascii=False),
        "models_source": _text(models_source),
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(
        "INSERT INTO model_providers"
        "(id, name, kind, base_url, api_key, models_json, models_source,"
        " created_at, updated_at)"
        " VALUES(:id, :name, :kind, :base_url, :api_key, :models_json,"
        " :models_source, :created_at, :updated_at)",
        row,
    )
    conn.commit()
    return public_provider(_internal(row))


def update_provider(conn: sqlite3.Connection, provider_id: str, *, name: Any = None,
                    kind: Any = None, base_url: Any = None, api_key: Any = None,
                    models: Any = None, models_source: Any = None) -> dict:
    """改一家服务商：**None = 不改**，api_key 传空串才是清空。

    为什么 None 与空串必须分开：界面上"我没碰密钥这一格"和"我要把密钥删掉"
    是两件完全不同的事，混成一个值会让后者永远做不到（或让前者每次清空）。
    """
    current = get_provider(conn, provider_id)
    if current is None:
        raise KeyError(provider_id)
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = _text(name)
    if kind is not None:
        fields["kind"] = _text(kind)
    if base_url is not None:
        fields["base_url"] = _text(base_url)
    if api_key is not None:
        fields["api_key"] = api_key if isinstance(api_key, str) else ""
    if models is not None:
        fields["models_json"] = json.dumps(_normalize_models(models), ensure_ascii=False)
    if models_source is not None:
        fields["models_source"] = _text(models_source)
    # 校验用的是**合并后**的值：只改 kind 的时候，name / base_url 得按库里那份一起过
    _check_fields(name=fields.get("name", current["name"]),
                  kind=fields.get("kind", current["kind"]),
                  base_url=fields.get("base_url", current["base_url"]))
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k}=:{k}" for k in fields)
    conn.execute(f"UPDATE model_providers SET {assignments} WHERE id=:id",
                 {**fields, "id": str(provider_id)})
    conn.commit()
    return public_provider(get_provider(conn, provider_id) or current)


def delete_provider(conn: sqlite3.Connection, provider_id: str) -> None:
    """删一家服务商；它正好是默认时，默认一并清空。

    清默认是必须的：留着悬空引用，页面会拿一个查不到的 id 去渲染"当前默认"，
    表现是默认那栏一直空着 —— 而用户明明刚设过。
    """
    pid = str(provider_id or "")
    conn.execute("DELETE FROM model_providers WHERE id=?", (pid,))
    if _setting(conn, DEFAULT_PROVIDER_KEY) == pid:
        _clear_default(conn)
    conn.commit()


# ===========================================================================
# 默认服务商 / 默认模型
# ===========================================================================

def _setting(conn: sqlite3.Connection, key: str) -> str:
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    return "" if row is None else str(row["value"] or "")


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _clear_default(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM app_settings WHERE key IN (?, ?)",
                 (DEFAULT_PROVIDER_KEY, DEFAULT_MODEL_KEY))


def set_default(conn: sqlite3.Connection, provider_id: str, model: str) -> dict:
    """设默认服务商与默认模型，返回**对外形状**的 provider_id / model / provider。

    这里刻意不带明文 key（与 get_default 相反）：它的返回值就是
    POST /api/v1/model-default 的响应体，少一次掩码转换就少一次泄漏机会。
    """
    provider = get_provider(conn, provider_id)
    if provider is None:
        raise KeyError(provider_id)
    chosen = _text(model)
    if not chosen:
        raise ValueError("默认模型不能为空（先选一个模型再保存）")
    _set_setting(conn, DEFAULT_PROVIDER_KEY, str(provider_id))
    _set_setting(conn, DEFAULT_MODEL_KEY, chosen)
    conn.commit()
    return public_default({"provider_id": str(provider_id), "model": chosen,
                           "provider": provider}) or {}


def get_default(conn: sqlite3.Connection) -> dict | None:
    """当前默认（**provider 里含明文 key**，内部用），没有就 None。

    指针悬空时**自愈**：服务商被删掉了就顺手把库里的默认清掉再返回 None。
    不自愈的话，页面每次刷新都拿到一个查不到的 provider_id，
    表现为"默认模型那栏永远空着"，而库里的脏指针谁也看不见。
    """
    provider_id = _setting(conn, DEFAULT_PROVIDER_KEY)
    if not provider_id:
        return None
    provider = get_provider(conn, provider_id)
    if provider is None:
        _clear_default(conn)
        conn.commit()
        return None
    # 默认模型留空但服务商有清单时，用清单第一条顶上：flow.py 必须拿到一个模型名，
    # 给空字符串回来的结果是"出片跑到一半说没配模型"。
    model = _setting(conn, DEFAULT_MODEL_KEY) or next(iter(provider["models"]), "")
    return {"provider_id": provider_id, "model": model, "provider": provider}


# ===========================================================================
# 拉模型清单
# ===========================================================================

#: 各家把它叫什么：OpenAI 叫 id，Ollama 叫 name，还有的叫 model
_MODEL_ID_KEYS: tuple[str, ...] = ("id", "name", "model")

_STATUS_HINTS: dict[int, str] = {
    400: "请求被拒，常见原因是模型名不被该服务商支持",
    401: "认证失败，检查 API Key",
    402: "账户余额不足（Insufficient Balance）",
    403: "没有权限，Key 可能不对或没有这个模型",
    404: "路径不对，base_url 通常要以 /v1 结尾",
    429: "请求太频繁或额度已用尽，等一下再试",
}

#: 每个状态码“下一步按哪个键”。和 _STATUS_HINTS 分开，是因为两句读者不同：
#: 上面解释“发生了什么”，下面告诉“去哪做什么”。
_STATUS_ACTIONS: dict[int, str] = {
    400: "到「设置」把这家的默认模型改成服务商支持的模型名（从拉取清单里挑）",
    401: "到「设置 · 编辑」更新这家的 API Key",
    402: "去服务商后台充值，或在「设置」新增另一家有额度的服务商并设为默认",
    403: "确认这把 Key 有该模型的权限，或换一把有权限的 Key",
    404: "确认 base_url 以 /v1 结尾、模型名与服务商一致",
    429: "稍后再试，或换一把 Key / 另一家服务商",
}

#: 探测对话的超时：比拉清单长一点，但要短到“点了按钮就能知道结果”
PROBE_TIMEOUT = 15.0


def _models_from(payload: Any) -> list[str]:
    """从三种见过的返回形状里取出模型 id，去重排序。

    三种形状都是实测会遇到的真事：
    data 套 {'id': …}（OpenAI 系）、models 套 {'name': …}（Ollama）、
    models 直接是 ['a', 'b']（简化实现）。
    """
    buckets: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                buckets.append(value)
        if not buckets and isinstance(payload.get("id"), str):
            buckets.append([payload])              # 只返回一条 id 的也认
    elif isinstance(payload, list):
        buckets.append(payload)
    out: list[str] = []
    seen: set[str] = set()
    for items in buckets:
        for item in items:
            name = ""
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                for key in _MODEL_ID_KEYS:
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        name = value.strip()
                        break
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return sorted(out)


def probe_chat(conn: sqlite3.Connection, provider_id: str, model: str = "") -> dict:
    """真发一次最小对话，判断这把 key 现在到底能不能用。

    为什么不复用 /models：实测 `GET /models` 在**余额为 0 时照样 200**
    （它只验认证、不验额度），于是“拉模型成功”给了人一切正常的错觉，
    真正的问题要等出片跑到 S0 才以 402 砸出来。只有真发一次对话，
    才能提前撞上 402/429 这类“配置对、账户不对”的失败。

    结论一律走 HTTP 200（它是“结果”不是“错误”）：status=0 表示连不上。
    """
    provider = get_provider(conn, provider_id)
    if provider is None:
        raise KeyError(provider_id)
    target = _text(model) or (provider["models"][0] if provider["models"] else "")
    if not target:
        return {"ok": False, "status": 0,
                "message": "这家还没有模型：先拉取或手填一个模型名再测",
                "hint": "点这张卡片上的「拉取模型」，或在「编辑」里手填模型名"}
    base = _text(provider["base_url"]).rstrip("/")
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "status": 0,
                "message": f"base_url 不像一个地址（{provider['base_url'] or '（空）'}）",
                "hint": "填形如 https://api.deepseek.com/v1 的地址"}
    url = base + "/chat/completions"
    headers = {"Accept": "application/json", "Accept-Encoding": "identity",
               "Content-Type": "application/json"}
    if provider["api_key"]:
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    body = {"model": target, "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1, "stream": False}
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=PROBE_TIMEOUT)
    except httpx.TimeoutException:
        return {"ok": False, "status": 0,
                "message": f"等 {PROBE_TIMEOUT:g} 秒没有响应（{url}）",
                "hint": "换一家服务商，或检查网络/代理"}
    except Exception as e:                  # 连不上 / DNS / TLS
        return {"ok": False, "status": 0,
                "message": f"连不上 {url}：{type(e).__name__}: {e}",
                "hint": "检查 base_url、网络或代理"}
    status = resp.status_code
    if status >= 400:
        return {"ok": False, "status": status,
                "message": f"服务商返回 {status} —— {_STATUS_HINTS.get(status, '服务商拒绝了这次请求')}",
                "hint": _STATUS_ACTIONS.get(status, "看服务商返回体里的 error 字段")}
    try:
        payload = resp.json()
    except Exception:
        payload = None
    # 200 不等于成功：有些中转会 200 + {"error": …}。认“像不像一次对话”，不认状态码。
    if isinstance(payload, dict) and ("choices" in payload or payload.get("id")
                                      or str(payload.get("object") or "").startswith("chat")):
        return {"ok": True, "status": status,
                "message": f"这条 key 能正常对话（模型 {target}）", "hint": ""}
    sample = str(getattr(resp, "text", "") or "")[:120]
    return {"ok": False, "status": status,
            "message": f"服务商返回 200，但返回体不像一次对话：{sample}",
            "hint": "确认 base_url 指向 OpenAI 兼容端点（通常是 …/v1）"}


def fetch_models(base_url: str, api_key: str, *, timeout: float = 10.0) -> list[str]:
    """实时拉 GET {base_url}/models。失败一律抛 ModelFetchError（message 给人看）。

    两个请求头都不是随手加的：
    * Accept-Encoding: identity —— 实测某些网关对压缩响应处理得很糟，拿到的是一段
      解不开的字节，报出来的错会指向"代码坏了"而不是"服务商挂了"；
    * Authorization 只在有 key 时才带 —— Ollama 本地不需要，硬塞一个空的 Bearer
      会被某些实现判成 401。
    """
    url = _text(base_url).rstrip("/") + "/models"
    if not url.startswith(("http://", "https://")):
        raise ModelFetchError(f"拉取模型列表失败：base_url 不像一个地址（{base_url or '（空）'}）")
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    key = _text(api_key)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as e:
        raise ModelFetchError(
            f"拉取模型列表超时（{url}，等了 {timeout:g} 秒）：{type(e).__name__}")
    except Exception as e:                  # 连不上 / DNS / TLS：都算"上游不可用"
        raise ModelFetchError(f"连不上 {url}：{type(e).__name__}: {e}")
    if resp.status_code >= 400:
        hint = _STATUS_HINTS.get(resp.status_code, "服务商拒绝了这次请求")
        raise ModelFetchError(
            f"拉取模型列表失败（{url}）：HTTP {resp.status_code} —— {hint}")
    try:
        payload = resp.json()
    except Exception:
        sample = str(getattr(resp, "text", "") or "")[:120]
        raise ModelFetchError(f"拉取模型列表失败（{url}）：返回的不是 JSON：{sample}")
    if not isinstance(payload, (dict, list)):
        raise ModelFetchError(f"拉取模型列表失败（{url}）：返回体认不出模型清单")
    return _models_from(payload)


def save_models(conn: sqlite3.Connection, provider_id: str, models: Any,
                source: str) -> dict:
    """把模型清单存进缓存列（拉回来的 / 手填的都走这里），返回对外形状。"""
    current = get_provider(conn, provider_id)
    if current is None:
        raise KeyError(provider_id)
    normalized = _normalize_models(models)
    now = _now()
    conn.execute(
        "UPDATE model_providers SET models_json=?, models_source=?, updated_at=? WHERE id=?",
        (json.dumps(normalized, ensure_ascii=False), _text(source), now, str(provider_id)),
    )
    conn.commit()
    return public_provider(get_provider(conn, provider_id) or current)


# ===========================================================================
# 出口：给子进程 / 给 .env
# ===========================================================================

def env_for_subprocess(conn: sqlite3.Connection) -> dict[str, str]:
    """把默认服务商翻译成 flow.py 认识的三个键；没有默认就返回 {}。

    **这是兼容映射，不是欺骗**：flow.py（以及它下面的 TeachingFlow/core/config.py）
    全链路只读 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL / DEEPSEEK_API_KEY，而那三个键
    描述的东西其实是"一个 OpenAI 兼容端点 + 一个模型名"。所以 ollama、openrouter、
    openai 这些也走这套键 —— 它们本身就是 OpenAI 兼容端点，把 base_url 指过去就能用。
    改口径要连着改 flow.py，不是在这里换个变量名就行。
    """
    default = get_default(conn)
    if default is None:
        return {}
    provider = default["provider"]
    env: dict[str, str] = {}
    if provider["api_key"]:
        env["DEEPSEEK_API_KEY"] = provider["api_key"]
    if provider["base_url"]:
        env["DEEPSEEK_BASE_URL"] = provider["base_url"]
    if default["model"]:
        env["DEEPSEEK_MODEL"] = default["model"]
    return env


#: .env 里的一行 key（与 config.save_env_values 同一口径，只认 KEY=VALUE）
_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _upsert_env_text(raw: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """只改 values 里那几个键：存在则换值，不存在则追加，其它行一个字节都不动。

    换行符必须原样保留（newline="" 读写）：Windows 上 write_text() 会把每个 \n
    翻译成 CRLF，于是"只改了 3 行"的一次保存让整份 .env 全变 —— diff 一片红，
    真正改掉的那 3 行反而看不见。
    """
    eol = "\r\n" if "\r\n" in raw else "\n"
    pending = dict(values)
    written: list[str] = []
    out: list[str] = []
    for line in raw.splitlines():
        match = _ENV_LINE.match(line)
        key = match.group(1) if match else ""
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
            written.append(key)
        else:
            out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        for key, value in pending.items():
            out.append(f"{key}={value}")
            written.append(key)
    text = eol.join(out)
    if not raw or raw.endswith(("\n", "\r\n")):
        text += eol
    return text, written


def export_env_file(conn: sqlite3.Connection, path: str | Path | None = None) -> dict:
    """把默认服务商写回 .env（默认 <仓库>/PPT-Agent/.env），幂等。

    返回写到的键与跳过的键。跳过的两种情况都不补空值：
    没有默认（什么都不该动）、默认那家没配 key（补一个空的 DEEPSEEK_API_KEY=
    会把文件里本来能用的那把 key 冲掉 —— 这正是曾经让课堂静默退化的那个坑）。
    """
    target = Path(path) if path is not None else repo_env_file()
    default = get_default(conn)
    if default is None:
        return {"path": str(target), "written": [], "skipped": list(ENV_KEYS)}
    provider = default["provider"]
    values: dict[str, str] = {}
    skipped: list[str] = []
    for env_key, value in (("DEEPSEEK_API_KEY", provider["api_key"]),
                           ("DEEPSEEK_BASE_URL", provider["base_url"]),
                           ("DEEPSEEK_MODEL", default["model"])):
        if value:
            values[env_key] = value
        else:
            skipped.append(env_key)
    if not values:
        return {"path": str(target), "written": [], "skipped": skipped}
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = ""
    if target.is_file():
        with target.open("r", encoding="utf-8", newline="") as f:
            raw = f.read()
    text, written = _upsert_env_text(raw, values)
    with target.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
    return {"path": str(target), "written": written, "skipped": skipped}
