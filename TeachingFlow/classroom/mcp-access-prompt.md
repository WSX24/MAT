我正在运行「AI 课堂交互原型」，请作为这堂课的「外置大脑（External Brain）」接入它的 MCP Server 并开始值守。

## 1. MCP 端点信息
- HTTP MCP Endpoint: `http://127.0.0.1:8000/mcp`
- 若你的宿主支持 HTTP / Streamable-HTTP MCP，请直接连接 `http://127.0.0.1:8000/mcp`；
- 若只支持 stdio，请使用 `npx -y mcp-remote http://127.0.0.1:8000/mcp`，或直接运行本仓库内置桥接器：
  `python run.py mcp-stdio http://127.0.0.1:8000/mcp`

## 2. 接入后立即执行的值守循环（铁律）
连接成功后，请立即开始以下循环，不要等待我再次催促：
1. 调用 `get_events(wait_seconds=50)` 挂起监听课堂事件（学生提问会微秒级立即唤醒返回）。
2. 当收到 `type="query"` 的学生提问事件时：
   - 查看 `handled_by`：若为 `"state_machine"`，说明内置老师已在 4 秒内给过快答，你只在需要关键补充或纠错时发言；若为 `null`，说明内置模型离线，由你负责解答。
   - 如需查阅课件细节或学生薄弱点，先调用只读工具 `get_state`、`query_rag`、`search_course` 或 `student_memory`。
   - 调用 `say(text="你的补充话术（口语化、2~3句内）", from_version=event.snapshot.version, final=True)` 将话术排入课堂让位点队列。
   - 检查 `say` 返回的 `receipt`：若 `queue_depth >= 2` 或 `can_speak_now == false`，停止继续 `say`。
3. 完成本轮补充（或收到空事件 `events: []`）后，**立即再次调用 `get_events(wait_seconds=50)`**（这同时会告诉课堂状态机「我说完了」）。