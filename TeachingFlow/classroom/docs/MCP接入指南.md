# AI 课堂「外置大脑」MCP 接入指南

> **读者**：将要接入这堂 AI 课堂的外部 Agent（Claude Code / Codex / Cursor / Cline / Claude Desktop / 其它 MCP 宿主）
> **服务端**：本仓库 `TeachingFlow/classroom/` 内的课堂状态机（MCP server，唯一权威）
> **设计依据**：原原型仓库的 `MCP方案设计-AI课堂外置大脑.md`（历史文档，未随迁移带入 MAT）
> **实现位置**：`TeachingFlow/classroom/mcp_server.py`（内核）、`TeachingFlow/classroom/machine.py`（状态机）、`TeachingFlow/classroom/phrases.py`（意图分流）

> ## ⚠️ 现状（正式版重构后，先用这一块判断能不能照做）
>
> **本文描述的 HTTP 传输已经不可用：**
> - `POST /mcp` 与 `GET/POST /api/dev/mcp` 挂在旧原型 UI 的 `web.py` 上，**已随整套旧 UI 删除**；
>   新应用（`TeachingFlow/classroom/app/`）**没有挂载任何 `/mcp` 路由**（`grep -rn mcp app/` 可自证）。
> - `python run.py mcp-stdio` 仍在，但它是 **stdio ↔ HTTP 的桥**，需要一个 `/mcp` 上游 ——
>   现在**没有上游可连**，所以它暂时也不可用（旧启动器写出的 `mcp-config.json` 同样指向不存在的端点）。
> - `mcp_server.py` 的**内核没有删**：JSON-RPC 分发、6 个工具、`EventBus`、`McpMode` 都在，
>   由 `tests/test_mcp.py` 的 6 条用例覆盖。
>
> 因此：**第 2 节（启动）、第 3 节（接入方式）、§7.3（自查清单）、附录 A 的操作步骤现在做不通**，
> 它们保留是为了"内核重新挂上之后照做即可"。**第 4~6 节、第 8 节讲的是内核语义，仍然准确。**
> 恢复办法：把 `McpBrainServer` 挂到新 app（设计文档 §4 的契约里目前没有 `/mcp`，属后续工作）。
>
> 现行课堂入口（与 MCP 无关）：`python app.py` 或 `powershell -File ./start-app.ps1`，见 [使用说明.md](使用说明.md)。

---

## 0. 三十秒版本

```
1) 启动课堂：仓库根 `powershell -ExecutionPolicy Bypass -File ./start-app.ps1`（或 `python app.py`）
   ⚠️ 这一步只起课堂本身：**HTTP MCP 端点 /mcp 目前不存在**（见文首「现状」）
2) 接入本机（**要等 /mcp 重新挂载后才成立**）：claude mcp add --transport http classroom http://127.0.0.1:8000/mcp
3) 开始值守：循环调用 get_events(wait_seconds=50)
   → 收到 type="query" → say(text, from_version=event.snapshot.version, final=True)
   → 立刻再调 get_events（这就是「我说完了」）
```

**你是这堂课的外置大脑，不是课堂的主人。** 状态机负责讲课和节奏，永不停摆；你只在状态机判不出学生意图（`triage.stage == "fallback"`）时被唤醒，你的话由状态机在「让位点」播出。

---

## 1. 拓扑与角色

```text
┌──────── 课堂状态机 = MCP server（唯一权威，双活，永不停摆）────────┐
│  HTTP POST /mcp （JSON-RPC 2.0）                                  │
│  tools: get_events / get_state / say / query_rag /               │
│         search_course / student_memory                            │
│  内部：EventBus 双车道（urgent / normal）、Floor Control、         │
│        triage(IntentDecision.stage)、McpMode 三态掌舵             │
└───────────────────────────────┬──────────────────────────────────┘
                                │ 你（外部 Agent）= MCP client
                                ▼
┌──────── 外部 Agent（Claude Code / Codex / ...）────────┐
│  initialize 握手拿到 instructions（行为策略的唯一注入点）│
│  loop: get_events(50s) → 推理 → 只读工具 → say → 回 loop │
└──────────────────────────────────────────────────────────┘
```

三条铁律（服务端保证，你也必须遵守）：

1. **你永不出现在状态机的阻塞链上。** 老师先用内置模型 4 秒内快答，你的话是「补充/纠正」。
2. **`get_events` 是长轮询，不是循环检测。** 它是一个挂起 50 秒的 HTTP 请求，学生说话会微秒级唤醒它。
3. **发言权（floor）永远在状态机手上。** 你的 `say` 只是入队，播出时机由状态机在让位点决定。

---

## 2. 启动课堂（服务端）

> ⚠️ **本节只负责把课堂起起来；它不再提供 MCP 端点**（`/mcp` 已随旧 UI 移除，见文首「现状」）。

| 方式 | 命令 | 说明 |
|---|---|---|
| **一键启动（推荐）** | 仓库根 `powershell -ExecutionPolicy Bypass -File ./start-app.ps1` | 查依赖 / 查配置 / 起服务 /（默认）开浏览器 |
| 直接起服务 | 仓库根 `python app.py` | 最直接；只起服务不开浏览器。端口用 `PORT`（默认 8000） |
| 预热 / 查连通性 | 在 `TeachingFlow/classroom` 里 `python run.py warmup` / `python run.py check` | 预合成讲解语音；只测模型，都不起服务 |

启动成功后你会看到（正式版的横幅 —— **注意没有 MCP 那一行**）：

```text
  地址          http://127.0.0.1:8000
  健康检查      http://127.0.0.1:8000/api/health
  课程库        ...\TeachingFlow\state\classroom.sqlite
  引擎          deepseek / deepseek-chat
  语音(edge-tts) 开
```

接入提示词与配置片段不再由"起服务"顺手写出（那段代码随 `run.py serve` 一起删除了）。
需要的话显式生成一份 —— 但要清楚它指向的 `/mcp` 目前不存在：

```powershell
cd TeachingFlow\classroom
python run.py mcp-prompt      # 只写文件，不起服务
```

- `TeachingFlow/classroom/mcp-access-prompt.md` —— 自然语言接入提示词（生成物，不入库）
- `TeachingFlow/classroom/mcp-config.json` —— `mcpServers` 片段（生成物，不入库）

> 端口以 `HOST` / `PORT` 为准（`.env` 或环境变量）。下文一律按 `http://127.0.0.1:8000/mcp` 说明，
> 但请记住：**这个端点在当前版本里不存在**。

---

## 3. 接入方式（按宿主类型选择）

> ⚠️ **本节全部依赖 `POST /mcp`**，而它在新应用里没有挂载 —— 下面三条路现在都走不通。
> 保留原文是为了"内核重新挂上之后照做即可"。若只想先确认端点是否存在：
> `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/mcp` → 会得到 404。

### 3.1 原生 HTTP / Streamable-HTTP 宿主（Claude Code、Codex、Cursor 等）

**Claude Code**（一行命令）：

```bash
claude mcp add --transport http classroom http://127.0.0.1:8000/mcp
claude
```

**通用 `mcpServers` 配置**（`.mcp.json` / `~/.cursor/mcp.json` / Cline 设置等）：

```json
{
  "mcpServers": {
    "classroom": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### 3.2 只支持 stdio 的宿主（Claude Desktop 等）

**方案 A：仓库内置 Python 桥接器（零依赖，超时 65s，已大于 50s 长轮询）**

（`python` 指 PATH 里的解释器 —— MAT 由仓库根 `setup.ps1` 把依赖装进当前解释器，没有虚拟环境；
下面的绝对路径按自己 clone 的位置改。）

```json
{
  "mcpServers": {
    "classroom": {
      "command": "python",
      "args": [
        "E:\\GitRepo\\MAT\\TeachingFlow\\classroom\\run.py",
        "mcp-stdio",
        "http://127.0.0.1:8000/mcp"
      ]
    }
  }
}
```

**方案 B：`npx mcp-remote`（需要 Node.js）**

```json
{
  "mcpServers": {
    "classroom": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]
    }
  }
}
```

> 桥接层若报超时：把 `wait_seconds` 从 50 降到 30（见 §6.1），或改用方案 A。

### 3.3 一个关键前提

**必须先启动课堂，再接 MCP。** 旧实现里服务端就是课堂进程自己（`/mcp` 挂在同一个 FastAPI 上）；
正式版把 `web.py` 整个删掉之后，**这个路由不存在**：课堂起没起，`/mcp` 都是 404
（能访问到的只有 `/api/health` 与 `/api/v1/*`）。

---

## 4. 协议表面：只有 5 个方法、6 个工具

### 4.1 JSON-RPC 方法

| 方法 | 职责 |
|---|---|
| `initialize` | 握手；**返回 `instructions`（行为策略的唯一注入点）** |
| `notifications/initialized` | 握手完成（服务端返回 HTTP 202，无 body） |
| `tools/list` | 返回 **6 个**工具的 schema（硬约束，不要期待更多） |
| `tools/call` | 全部业务逻辑都在这里 |
| `ping` | 保活（同时刷新活跃时间） |

协议版本：`2025-06-18`。传输：`POST /mcp`，`Content-Type: application/json`，响应体是同一条 JSON-RPC 回应；通知类请求返回 `202`。

### 4.2 工具清单（6 个，不可增减）

| 工具 | 阻塞 | 作用 |
|---|---|---|
| `get_events` | **是（长轮询）** | 取课堂事件。急件（学生提问）立即返回，到点清扫普通事件或返回空 |
| `get_state` | 否 | 读取 `ContextSnapshot`（进度、待回看、理解信号、最近问答、floor） |
| `say` | 否 | 提交你要说的话，返回 receipt（入队，不代表已播出） |
| `query_rag` | 否 | 在当前课件/讲稿/知识点里检索 |
| `search_course` | 否 | 按页码或关键词看课件元素与单元大纲 |
| `student_memory` | 否 | 跨会话学员画像（同步读、异步写） |

> 行动类工具（`next_unit` / `next_page` / `deck_op` 等）**默认不开放**。即使开启，也只允许在学生明确要求时使用。

---

## 5. 你要跑的核心循环

### 5.1 状态机（伪代码）

```python
while True:
    result = get_events(wait_seconds=50)          # 挂起，急件立即唤醒
    for ev in result["events"]:
        if ev["type"] == "query":
            # 1) 看清为什么叫醒你
            if ev["triage"]["stage"] != "fallback":
                continue                          # 不是给你处理的
            # 2) 看清老师是否已经答过
            if ev["handled_by"] == "state_machine" and 已有把握:
                continue                          # 别为了说话而说话
            # 3) 需要细节就查只读工具
            ctx = get_state() if 需要 else None
            # 4) 说话（必须带版本）
            r = say(text="……", from_version=ev["snapshot"]["version"], final=True)
            if r["queue_depth"] >= 2 or not r["can_speak_now"]:
                break                             # 停，等下一个 get_events
    # 空返回也要立刻回到循环顶部 —— 这就是「我说完了」
    continue
```

### 5.2 事件类型

| type | kind | urgent | 何时产生 |
|---|---|---|---|
| `query` | `student_text` / `student_voice` | ✅ | 学生输入且 triage 为 `fallback`（**只有这类会叫醒你**） |
| `event` | `speech_finished` | ❌ | 一段话播完 |
| `event` | `unit_finished` | ❌ | 讲解单元播完 |
| `event` | `page_changed` | ❌ | 翻页 |
| `event` | `probe_opened` / `probe_timeout` | ❌ | 页尾探针开始 / 超时 |
| `event` | `deferred_added` | ❌ | 新增待回看项 |

### 5.3 事件信封（`query` 示例）

```json
{
  "type": "query",
  "kind": "student_text",
  "urgent": true,
  "seq": 42,
  "at": "2026-10-22T16:00:00",
  "phase": "LECTURING",
  "student": { "text": "sweet 和 home 之间为什么加连字符？", "via": "text" },
  "triage": { "stage": "fallback", "verdict": "unknown",
              "reason": "规则未命中，小模型也未给出 qkind", "llm_saved": false },
  "handled_by": "state_machine",
  "floor": { "can_speak": false, "next_window_ms": 1800, "reason": "当前讲解单元未播完" },
  "snapshot": { "version": { "slide": 3, "unit": "s03-u2", "seq": 17 },
                "digest": "第 3 页第 2 段：连字符复合词" },
  "stale": false
}
```

字段含义：

- `triage.stage` —— **为什么叫醒你**。`"fallback"` 才是给你的；其它 stage 说明状态机自己能处理。
- `handled_by` —— `"state_machine"` 表示内置老师已经答过（你只需补充/纠错）；`null` 表示没人答（内置模型离线，由你负责）。
- `floor` —— 此刻能否说话，以及下一个窗口大概多久。
- `snapshot.version` —— **你 `say` 时必须原样带回的版本**。
- `stale` —— 超过 60 秒未读会被标 `true`，此时建议只答最后一条并致歉跳过旧的。

---

## 6. 工具契约细节

### 6.1 `get_events`

```json
{ "name": "get_events",
  "arguments": { "wait_seconds": 50 } }
```

- `wait_seconds`：`0 ~ 50`，默认 **50**。只有急件（学生提问）会提前唤醒；普通事件静静累积到点后一起返回。
- **三档返回，缺一不可**：

```json
// 1) 有急件：立即返回（< 1ms 唤醒）
{ "events": [ … ], "idle_hint": null }

// 2) 到点清扫普通事件
{ "events": [ … ], "idle_hint": null }

// 3) 到点没事：明确返回空（约 20 token，很短）
{ "events": [], "idle_hint": { "next_useful_poll_ms": 120000,
                               "reason": "接下来 3 个单元是连续讲解，无探针" } }
```

- **消费即消失**：读过的事件不会再返回。不要把它当日志。
- `idle_hint.next_useful_poll_ms` 是服务端用离线备课计划算出来的「下次有意义的时间」，可以据此放宽轮询。

### 6.2 `get_state`

```json
{ "name": "get_state", "arguments": { "slide": 3 } }   // slide 可选，1 起算
```

返回 `ContextSnapshot` + `phase` + `floor`：

```json
{
  "version": { "slide": 3, "unit": "s03-u2", "seq": 17 },
  "slide": { "index": 2, "total": 42, "title": "…", "text": "…" },
  "unit": { "id": "s03-u2", "text": "…" },
  "prev_unit_text": "…",
  "covered_kp": ["…"],
  "comprehension": { "s03-u1": "confused" },
  "deferred": ["连字符复合词规则"],
  "recent_qa": [ { "q": "…", "a": "…" } ],
  "q_rounds": 1,
  "phase": "LECTURING",
  "floor": { "can_speak": false, "next_window_ms": 1800, "reason": "…" }
}
```

`comprehension` / `deferred` / `recent_qa` 就是「老师记着的那些事」—— 回答前先看它们，能避免自相矛盾和追问链断裂。

### 6.3 `say`（重点）

```json
{ "name": "say",
  "arguments": {
    "text": "补充一下：sweet home 作定语修饰 house 时，通常写成 sweet home 不带连字符；只有作形容词性复合词整体修饰名词时才加，比如 well-known。",
    "from_version": { "slide": 3, "unit": "s03-u2", "seq": 17 },
    "final": true
  } }
```

返回 receipt（**不是裸的 "said"**）：

```json
{
  "status": "queued",
  "seq": 43,
  "queue_depth": 1,
  "can_speak_now": false,
  "speak_after_ms": 1800,
  "reason": "当前讲解单元未播完",
  "version_check": "ok"
}
```

| 字段 | 你怎么用 |
|---|---|
| `queue_depth` | `>= 2` 立刻停止继续 `say`（防话痨，服务端超过 2 条也会拒收） |
| `can_speak_now` | `false` 时不要连续 `say`，先回去 `get_events` |
| `speak_after_ms` | 大概多久后才会轮到播出 |
| `version_check` | 见下表；**状态机的版本裁决是最终裁决，不要自己猜** |
| `status` | `queued` / `throttled`（队列满）/ `dropped`（版本过期） |

**版本裁决规则（`version_check`）**

| 情况 | 结果 | 服务端行为 |
|---|---|---|
| 页码一致 | `"ok"` | 正常入队 |
| 页码变了但相差 ≤ 2 页 | `"stale_prefixed"` | 自动加前缀「补充一下刚才第 X 页那个点——」 |
| 页码相差 > 2 页 | `"dropped"` | 丢弃，并自动记入 `deferred`（待回看） |

不要在跨度很大之后还去答旧页 —— 那样老师已经讲到别处，你的话会成为「说梦话」。读到 `dropped` 就重新 `get_state()` 跟上进度。

### 6.4 只读工具

**`query_rag`** —— 在课件正文、讲稿单元、知识点、锚点里检索：

```json
{ "name": "query_rag", "arguments": { "query": "连字符", "top_k": 3 } }
```

返回 `{"interrupted": false, "query": "…", "results": [{ "slide": 3, "unit_id": "…", "kp": [...], "text": "…", "elements": [...] }]}`。

> **急件打断**：检索过程中若有新的学生提问到达，会在两次子调用之间返回
> `{"interrupted": true, "reason": "urgent_query_arrived", "message": "…请立即调用 get_events"}`。
> 看到 `interrupted: true` **立刻回到 `get_events`**，不要继续检索。

**`search_course`** —— 看某一页的元素与单元，或按关键词列出相关页：

```json
{ "name": "search_course", "arguments": { "slide": 3 } }
{ "name": "search_course", "arguments": { "keyword": "连字符" } }
```

**`student_memory`** —— 跨会话学员画像：

```json
{ "name": "student_memory", "arguments": { "action": "read" } }
{ "name": "student_memory", "arguments": { "action": "read", "key": "quad_root" } }
{ "name": "student_memory", "arguments": { "action": "write", "key": "hyphen", "note": "常混淆定语复合词要不要加连字符" } }
{ "name": "student_memory", "arguments": { "action": "delete", "key": "hyphen" } }
```

`read` 同时返回本课的 `session_comprehension` 与 `session_deferred`；`write` 是异步落盘的，**不会阻塞课堂**。

---

## 7. 错误语义与排障

### 7.1 错误码

| 场景 | JSON-RPC | 说明 |
|---|---|---|
| 未知工具名 | `-32601` | `"unknown tool: xxx"` —— 检查工具名，只有 6 个 |
| 参数不合 schema | `-32602` | 例如 `"bad arguments: 缺 text"` |
| 课堂未开始（无 course） | 正常返回 | `content: "classroom not started"` + `isError: true` |
| 内部异常 | 正常返回 | `isError: true` + 原因；服务端同时打 traceback、记 metrics、发课堂提示 |

### 7.2 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 连接被拒绝 | 课堂没启动 | 先启动课堂（`start-app.ps1` / `python app.py`） |
| `POST /mcp` 回 404 | **预期**：`/mcp` 已随旧 UI 移除（见文首「现状」） | 目前无解；恢复要先把内核挂到新 app |
| `get_events` 总是立刻返回空 | 正常现象（无急件时确实很快到点或无事） | 看 `idle_hint`，按提示放宽间隔 |
| 桥接层报超时 | 宿主/桥接的单请求超时 < 50s | 把 `wait_seconds` 降到 30，或改用 `run.py mcp-stdio` |
| `say` 总是 `throttled` | 队列已有 2 条 | 先回去 `get_events` 等播出 |
| `say` 返回 `dropped` | 版本相差 > 2 页 | 重新 `get_state()` 跟上当前进度 |
| 一直不被叫醒 | 学生输入都被规则/小模型判定了（stage ≠ fallback） | 正常 —— 只有判不出来的输入才找你 |
| `handled_by` 一直是 `null` | 内置模型服务不可用 | 你的回答就是主答案，务必查证后再 `say` |

### 7.3 自查清单

接入完成后，按顺序确认：

1. ~~`http://127.0.0.1:8000/api/dev/mcp` 的 `mode.state` 由 `unconnected` 变成 **`active`**~~
   —— **这条现在查不了**：`/api/dev/mcp` 已随旧 UI 移除。重新挂载后用 `McpMode.status_dict()` 看同一份数据。
2. `tools/list` 返回 **恰好 6 个**工具。
3. `get_events(wait_seconds=0)` 返回结构正常的空结果（含 `idle_hint`）。
4. 在课堂页面发一句开放问题（如「sweet 和 home 之间为什么加连字符？」），应收到 `type="query"`、`urgent=true`、`triage.stage="fallback"` 的事件。
5. `say` 一次，receipt 为 `status="queued"`、`version_check="ok"`，随后在课堂听到/看到这句话在段末被播出。
6. 长时间不活动（默认 120 秒）后，`mode.state` 变 `quiet_fallback`，课堂提示「状态机全权接管」；再调一次 `get_events` 应立即恢复 `active`。

---

## 8. 行为纪律（写给 Agent 自己看）

1. **循环调用 `get_events(wait_seconds=50)`**，它同时承担「听」和「我说完了」两个职责。
2. `handled_by="state_machine"` 时，**没有关键补充就不要说话**。沉默是合法的。
3. `say` 的文本控制在 2~3 句口语（≤300 字），不要写 Markdown、标题、列表。
4. 永远带 `from_version`；读完 `receipt` 再决定要不要继续。
5. 不确定就查（`query_rag` / `search_course` / `student_memory`），查不到就承认不知道。
6. 你只回答，不推进课堂。不要试图翻页或跳单元。
7. `interrupted: true` 的返回意味着有更紧急的学生提问，立刻回到 `get_events`。

---

## 9. 参考：目标形态与验收口径

| 指标 | 阈值 | 说明 |
|---|---|---|
| 事件唤醒到返回 | < 5 ms | 服务端实测约 0.2~0.4 ms |
| `say` 入队 | < 5 ms | 实测 < 0.5 ms |
| 40 分钟课堂 `get_events` 往返 | ≈ 48 次（≤ 60） | 由 `wait_seconds=50` 决定 |
| 空返回成本 | < 30 token/次 | 实测约 20 token |
| 单次发言队列深度 | ≤ 2 | 超出会被拒收（`throttled`） |
| 版本过期处理 | 100% 按规则裁决 | 不出现「答第 3 页而老师在讲第 5 页」 |
| 断连接管 | < 120 s | 超过后状态机全权接管，不依赖你 |

---

## 附录 A：接入提示词与配置片段

> ⚠️ 起服务**不再自动写出**这两个文件（写出的那段代码随 `run.py serve` 一起删除）；
> 现在只有显式跑 `python run.py mcp-prompt` 才会写。而且它们指向的 `/mcp` 当前不存在。

`python run.py mcp-prompt` 会写出：

- `TeachingFlow/classroom/mcp-access-prompt.md` —— 交给别的 Agent 的整段接入提示词
- `TeachingFlow/classroom/mcp-config.json` —— 现成 `mcpServers` 片段（HTTP + stdio 各一份）

```powershell
cd TeachingFlow\classroom
python run.py mcp-prompt      # 只写文件，不起服务
```

## 附录 B：服务端源码索引

| 内容 | 位置 |
|---|---|
| JSON-RPC 分发与 5 个方法 | `TeachingFlow/classroom/mcp_server.py` `McpBrainServer.handle_jsonrpc` |
| 工具 schema（6 个硬约束） | `TeachingFlow/classroom/mcp_server.py` `_tool_definitions` |
| 长轮询与双车道 | `TeachingFlow/classroom/mcp_server.py` `EventBus` |
| 三态掌舵 / quietFallback | `TeachingFlow/classroom/mcp_server.py` `McpMode` |
| 版本校验与 Floor Control | `TeachingFlow/classroom/mcp_server.py` `check_version` / `compute_floor` |
| 挂载点 `POST /mcp` | **已删除**（原 `web.py` `mcp_endpoint`；新 app 未挂载 → 恢复时挂这里） |
| 状态查询面板 | **已删除**（原 `web.py` `GET /api/dev/mcp`；改用 `McpMode.status_dict()`） |
| 急救室舱 / 意图分流 | `TeachingFlow/classroom/phrases.py` `StudentIntent.DELEGATE` |
| 上下文快照 | `TeachingFlow/classroom/llm.py` `ContextSnapshot` / `build_ctx` |
| 让位点插播 | `TeachingFlow/classroom/machine.py` `_yield_for_agent` |
| stdio 桥接 | `TeachingFlow/classroom/run.py` `cmd_mcp_stdio`（需 `/mcp` 上游，当前无可用上游） |
| 接入提示词生成 | `TeachingFlow/classroom/mcp_server.py` `build_access_prompt` |
