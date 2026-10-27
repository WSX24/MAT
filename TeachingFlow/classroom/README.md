# 课堂子系统（`TeachingFlow/classroom`）· 学生自学正式版

> 契约唯一事实源：[docs/正式版设计.md](docs/正式版设计.md)（目录 / 数据模型 / HTTP+WS 契约 / 完成标准）。
> 面向使用者的最短路径：[docs/使用说明.md](docs/使用说明.md)。
> 本 README 只讲**这一层怎么跑、目录怎么分工、坏了好查哪儿**，不复述契约。

原型期的那一层（`web.py` + `web/*.html|css|js`，以及它暴露的 `/hub` `/graph` `/script`
`/review` `/preprocess` 五个页面和 `/api/*` 旧端点）**已整体删除**：正式版只有一套入口
（仓库根 `app.py` / `start-app.ps1`）和一套前端（`webapp/`）。想查那些页面的行为，
去 git 历史里看；这一层的现状以本文与设计文档为准。

---

## 1. 这一层的分工

```
TeachingFlow/classroom/
├─ app/        【层】FastAPI 应用：routes/ · ws.py · deps.py · errors.py
│              只做接线与契约翻译 —— 出现一行"课堂规则"就说明分层写漏了
├─ store/      【层】sqlite：db.py(连接+迁移) · schema.sql · repo.py(全部 SQL)
│              档案/课程/单元/会话/进度/事件的唯一落盘处
├─ studio/     【层】引擎适配：catalog(扫描 runs) · materialize(备课) · outline(大纲) · runner(会话运行时)
│              把既有引擎（deck/ · script_gen · machine · speech …）装进正式版的领域模型
├─ webapp/     【层】Vite + React + TS 前端（源码 + `dist/` 入库）；只依赖设计文档 §4 的 HTTP/WS 契约
└─ （引擎原位保留，行为未改）machine.py · script_gen.py · course.py · speech.py · deck/ · director/
   · agent/ · preprocess.py · mcp_server.py · llm.py · config.py · phrases.py · eval.py · run.py
```

依赖方向是单向的：`app/` → `studio/` → 引擎；`store/` 谁也不依赖（只有 `repo` 的 SQL）。
改之前先看一眼设计文档 §5「三方接口」——那里写着每个函数被谁调用、返回什么。

**入口只有两个**，都在仓库根：

```powershell
python app.py                      # 看端口/库/引擎/语音就绪信息后起 uvicorn
powershell -File ./start-app.ps1    # 一键：查依赖 → 查 .env → 起服务 →（默认）开浏览器
```

`PORT`（默认 8000）· `HOST`（默认 127.0.0.1）· `CLASSROOM_DB`（换一份库）· `CLASSROOM_DEV=1`（打开 `/dev/*`）。

## 2. 前后端开发

**只改前端**（后端已经在 8000 跑着）：

```bash
cd TeachingFlow/classroom/webapp
npm install              # 首次
npm run dev              # http://127.0.0.1:5173，/api 与 /ws 代理到 127.0.0.1:8000
```

后端没起也能开发：`VITE_MOCK=1 npm run dev` 用内置假数据，`?mock=empty|error|slow|unprepared`
切场景（详见 [webapp/README.md](webapp/README.md)）。

**改完要重新构建 dist**（`dist/` 入库，`python app.py` 直接服务它）：

```bash
cd TeachingFlow/classroom/webapp
npm run build            # tsc --noEmit && vite build → webapp/dist/
```

**只改后端**：`python app.py` 起的是真服务，改完重启即可（没有热重载）。
契约用例：`PYTHONUTF8=1 python -m pytest tests/test_api_contract.py`。

## 3. 引擎侧命令（`run.py`，备课与运维用，**不是课堂入口**）

```powershell
cd TeachingFlow\classroom
python run.py                      # 打印帮助与子命令清单（不会起服务）
```

| 子命令 | 干什么 |
|---|---|
| `pptx "<课件.pptx>"` | 解析 pptx → `decks/<deck_id>/`（`--max-slides N` / `--no-download`） |
| `decks` | 列出已解析的课件 |
| `plan <deck_id>` | 备课，写 `plan.json`（`--slides 1-6` / `--rule` / `--dry-run` / `--fix-ops`） |
| `audit [deck_id]` | 按真实课件体检（锚点命中率 / 单元长度 / 操作合法性） |
| `routes <deck_id>` | 把旧格式 `plan.json` 迁到带 `routes` 的新格式（`--dry-run` / `--no-polish`） |
| `summarize <deck_id>` | 只给已有计划补走向图卡面那句总结（`--dry-run` 不写盘） |
| `variants <deck_id>` | **已停产**，仅为老流程留档 |
| `test` | 内置离线自检（几个模块各自的主函数；完整用例见 §6 的 pytest） |
| `warmup` | 预合成讲解音频，演示更顺 |
| `check` | 服务商连通性 |
| `mcp-prompt` | 只生成 MCP 接入提示词，不起服务 |
| `mcp-stdio [endpoint]` | 内置 stdio ↔ HTTP 桥接（给只认 stdio 的宿主） |

`--env <文件>` 可换一份配置跑（复现故障 / 对照实验）：它设的是 `DSH_ENV_FILE`，
**必须在 `import config` 之前**，所以放在任意子命令后面都行。

## 4. 配置（唯一出处：仓库根的 `PPT-Agent/.env`）

课堂与出片流水线读**同一份** `.env`：`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` /
`DEEPSEEK_MODEL` 三项共用。没有这份文件也能起 —— 备课走规则兜底、只有文字没有语音。

```powershell
Copy-Item .env.example PPT-Agent\.env    # 然后在 PPT-Agent\.env 里填 DEEPSEEK_API_KEY
```

服务商在 `config.py` 里：`ollama`（本地，默认）/ `opencode` / `opencode_go` / `deepseek`。
选哪家由 `LLM_PROVIDER` 决定，`LLM_MODEL` / `<PROVIDER>_BASE_URL` 覆盖细节。
`.env` 只在启动时读一次，改完要**重启服务**。

语音：`edge-tts`（免费）。装不上或被墙时自动降级成"只有字幕"，课堂照上。

## 5. 课程（单元）从哪来

1. **TeachingFlow 的 run**：`TeachingFlow/runs/<日期>/<课程名>/` 里有出片产物时，
   首页点「重新扫描」（或 `POST /api/v1/courses/scan`）就进课程库。
2. **导入 pptx**：首页「导入课件」上传一份 `.pptx`（或 `POST /api/v1/courses/import`），
   立刻出现在课程库，并顺手备一次课。

两条路最后都落在同一批产物上：

```
TeachingFlow/classroom/pptx/          课件源库（上传的副本；.pptx 不入库）
TeachingFlow/classroom/decks/<id>/    deck.json（解析） + plan.json（备课） + media/（图片）
TeachingFlow/state/classroom.sqlite   课程/单元/会话/进度/事件（gitignored）
TeachingFlow/state/imports/<id>/      导入的原文件
```

覆盖写 `plan.json` 之前一律先备份成 `plan.bak.json`。`decks/` 下的**入库课件**
（`Unit_2_Sweet_Home…` / `高一物理第一课…`）是真实课件的断言基准，别手改。

## 6. 测试

```powershell
cd TeachingFlow\classroom
$env:PYTHONUTF8=1
python -m pytest                 # 全量（离线；不联网、不调模型）
python -m pytest tests/test_api_contract.py   # 只跑 API/WS 契约
```

| 关注点 | 文件 |
|---|---|
| API + WS 契约（设计文档 §4 逐条） | `tests/test_api_contract.py` |
| 端到端主线（导入 → 上课 → 小结 → 续学） | `tests/test_end_to_end_flow.py` |
| 内容管线（扫描 / 备课 / 大纲） | `tests/test_studio_*.py` |
| 引擎（状态机 / 备课 / 课件 / 导演 / 预处理 / MCP） | 其余 `tests/test_*.py` |

跑用例时 `decks/` 不该变脏：新用例一律把 `DECK_DIR`/`CLASSROOM_DB` 指到 tmp_path。

## 7. 坏了好查哪儿

| 现象 | 先看 |
|---|---|
| 首页 404 / 空白 | `webapp/dist/index.html` 在不在（`npm run build`）；`/api/health` 返回什么 |
| 课程库为空 | 有没有 `TeachingFlow/runs/*/` 出片产物；`.env` 的 `RUNS` 是否指对；直接导入一份 pptx 试 |
| 单元"未准备"、大纲空 | `decks/<deck_id>/` 里 deck.json / plan.json 是否生成；`python run.py plan <deck_id>` 手动跑一次 |
| 只有字幕没有语音 | `edge-tts` 版本（≥7.3.1）与网络；`/api/health` 的 `engine.tts` |
| 讲得干巴巴 | 没配模型 key → 规则兜底（照读课件）。配 `DEEPSEEK_API_KEY` 后 `python run.py plan` 重备 |
| 诊断事件（备课计划 / 会话事件） | `CLASSROOM_DEV=1` 起服务，再访问 `/dev/units/{id}/plan`、`/dev/sessions/{id}/events` |

日志走 stderr（`level=... logger=classroom.* ...`），`CLASSROOM_LOG_LEVEL` 调级别。

## 8. 文档索引

| 文档 | 讲什么 |
|---|---|
| [docs/正式版设计.md](docs/正式版设计.md) | **唯一事实源**：目录 / 数据模型 / 契约 / 完成标准 |
| [docs/使用说明.md](docs/使用说明.md) | 面向使用者：启动 → 导入 → 上课 → 续学 → 换档案 |
| [docs/课堂状态机规范.md](docs/课堂状态机规范.md) | 状态机与阶段回调 |
| [docs/分支机制说明.md](docs/分支机制说明.md) | 预置路线（skip / revisit）与停产的就地变体 |
| [docs/双脑三体方案设计.md](docs/双脑三体方案设计.md) | J（导演决策）+ C（候选生成） |
| [docs/预处理与课件库说明.md](docs/预处理与课件库说明.md) | pptx 解析、源库与产物、锚点 |
| [docs/MCP接入指南.md](docs/MCP接入指南.md) | 把外部 agent 接到课堂（`run.py mcp-stdio`） |
| [webapp/README.md](webapp/README.md) | 前端开发、mock 场景、构建 |
