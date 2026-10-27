# 课堂子系统（`TeachingFlow/classroom`）· 个人自学场景

对应 [`docs/课堂交互设计.md`](docs/课堂交互设计.md) 的**第一个可运行原型**：把文档第 3 章的状态机真正跑起来，并用实测数据回填 §8 的待验证清单。

本目录整体搬自原原型仓库（扁平布局、代码未改）；下面的路径与命令都已按 MAT 仓库重写。
新人先读 §1 的**「落位与运行」**和**「首次配置」**两节 —— 它们讲清"为什么必须在本目录跑"和"不配 `.env` 会发生什么"。

**它验证什么**

| 编号 | 待验证项 | 这个原型怎么回答 |
|---|---|---|
| **V1** | 讲解单元理想长度（15~30 秒假设） | 每个单元的实际字数/时长会打印出来；把单元切成长短不同的版本各跑一遍，看让位延迟与讲解连续性怎么变 |
| **V2** | 最大可接受等待时长 | **实测「让位延迟」**（学生提问 → 开始作答），这是段末让位方案的核心代价 |
| **V3** | 探针等待窗口 | `.env` 里改 `CLASSROOM_PROBE_WINDOW` 即可，配合「探针响应率」一起看 |
| **V6** | 重讲链上限 4 轮 | 连点「没听懂」到顶，观察是否真的走「暂放 + 承诺」而不是死循环 |
| **V7** | 翻页双门判定 | 走完整链路，看「自动翻页」是否真的被「讲完了 + 听懂了」两道门拦住 |

**它不做什么**（刻意的范围控制）

- 不做 PPT 解析：演示课件写在 [`script.py`](script.py) 里（对应文档 §2.5 的"系统生成课件"路线，锚点天然精确）——**§9 已补上真实课件接入**，这一条只描述最初的最小版本
- 不做课堂脚本生成：讲解单元是手写的，这样实测 V1/V2 时不会被 LLM 的随机长度污染
- 不做真实用户系统：每次连接就是一名学生的自学课堂

---

## 1. 快速开始

### 落位与运行：为什么必须在本目录跑

`TeachingFlow/classroom/` 是整体搬进来的原原型：**扁平布局、代码未改**。保持扁平不是偷懒，是代码的硬前提 ——
模块之间全是同级 `import`（`from config import load_settings`），`ROOT = Path(__file__).resolve().parent`
也按"本目录就是根"去找 `web/`、`decks/`、`.env`。塞进 `classroom/src/…` 这类包结构，这两个前提会同时失效。

所以：

- **所有命令都在 `TeachingFlow/classroom` 目录下执行**（仓库根的 `start-classroom.ps1` 已经替你 `Push-Location`）。
- 依赖由仓库根的 `setup.ps1` 统一装（第 5 步装的就是本目录的 `requirements.txt`），不必再建虚拟环境。
- 只在 **Python 3.12.4** 上验证过（依赖清单见 `requirements.txt`）。

`run.py` 子命令一览（`python run.py <命令>`；不带命令 = `serve`。以 `run.py` 顶部 docstring 与 `main()` 的命令表为准）：

| 子命令 | 干什么 |
|---|---|
| `serve` / 不带参数 | 起服务并开浏览器（`--no-browser` 不开；`--env <文件>` 换一份配置） |
| `start` | `serve` 的别名 |
| `pptx "<课件.pptx>"` | 解析 pptx → `decks/<deck_id>/`（`--max-slides N` / `--no-download`） |
| `decks` | 列出已解析的课件 |
| `plan <deck_id>` | 备课，写 `plan.json`（`--slides 1-6` / `--rule` / `--dry-run` / `--fix-ops`） |
| `audit [deck_id]` | 按真实课件体检（锚点命中率 / 单元长度 / 操作合法性） |
| `routes <deck_id>` | 把旧格式 `plan.json` 迁到带 `routes` 的新格式（`--dry-run` / `--no-polish`） |
| `summarize <deck_id>` | 只给已有计划补走向图卡面那句总结（`--dry-run` 不写盘） |
| `variants <deck_id>` | **已停产**，仅为老流程留档 |
| `test` | 内置离线自检：10 个测试模块各自的主函数（**不是 pytest**；完整用例见 §1.2 与 §11.1） |
| `warmup` | 预合成讲解音频，演示更顺 |
| `check` | 服务商连通性 |
| `mcp-prompt` | 只生成 MCP 接入提示词，不起服务 |
| `mcp-stdio [endpoint]` | 内置 stdio ↔ HTTP 桥接（给只认 stdio 的宿主） |

> ⚠ `python run.py --help` **不是帮助**：`main()` 里没有 `--help` 分支，未知的第一个参数会落到默认的 `serve`。
> 查子命令看上面这张表。

#### 两个"生成物"：`mcp-access-prompt.md` 与 `mcp-config.json`（不入库）

| 文件 | 是什么 | 谁写、什么时候写 |
|---|---|---|
| `mcp-access-prompt.md` | 一整段自然语言接入提示词，**整份发给 Claude Code / Codex**，对方照着自己接好 HTTP MCP | `run.py` 起服务时调用 `mcp_server.build_access_prompt()` 重写（`python run.py mcp-prompt` 只生成不起服务） |
| `mcp-config.json` | 现成的 `mcpServers` 片段：`classroom`（HTTP 直连）+ `classroom-stdio`（本机 stdio 桥接，免装 Node） | 同上，与提示词一起写盘 |

**为什么不入库**：两者都是按**本机现况**现算的 —— 提示词里写死了本次的 `http://<HOST>:<PORT>/mcp`，
配置片段的 `args` 指向本机 `TeachingFlow\classroom\run.py` 的绝对路径。入库会带来两个毛病：
① 任何人跑一次服务 `git status` 就脏；② 提交里那份路径/端口只对作者那台机器有效，对新用户是错的
（`.gitignore` 已按运行产物忽略它们）。

**想固定端口**：改 `.env` 的 `PORT`（`HOST` 同理），再起一次服务，两个文件会按新端点重新生成。
**想固定解释器**：`mcp-config.json` 里 `classroom-stdio.command` 有意写成 `python`（交给 PATH），
要指定某个解释器就把本地这份改成那个 `python.exe` 的绝对路径 —— 改生成物即可，别再提交回来。

### 首次配置：`.env` 与 `DECK_ID`

`web.py` 在**导入期**就按 `DECK_ID` 载课，所以配置不全时测试会先红一批（不是代码坏了）：

```powershell
Copy-Item .env.example .env      # setup.ps1 第 5 步已经生成过；手工恢复时用这条
```

`python -m pytest -q` 实测（`run.py test` 走的是另一套内置自检，数字不一样）：

| 状态 | 结果 |
|---|---|
| 没有 `classroom/.env`（全新克隆，直接跑） | **9 failed / 495 passed / 5 skipped** |
| 有 `.env`（`DECK_ID` 指向 `decks/` 里的示例课件） | **507 passed / 2 failed**（2 条是历史失败用例，见 §11） |

口径：**在 classroom 目录里跑**，共 509 条用例（44 个 `test_*.py`）；「有 `.env`」那行是
`cp .env.example .env` 之后把 `DECK_ID` 指到 `decks/` 里已有的那份课件。

`.env` 里最常动的是这几项（其余项都有默认值）：

```ini
DECK_ID=Unit_2_Sweet_Home…   # .env.example 已指向示例课件；留空 = 用 script.py 里的手写演示课件
LLM_PROVIDER=ollama          # 默认本地 ollama；改 deepseek 则复用 PPT-Agent\.env 的 DEEPSEEK_* 三项
HOST=127.0.0.1
PORT=8000
```

> 仓库根 `setup.ps1` 的第 5 步会自动做这两件事（装依赖 + 生成 `.env`），所以正常流程不用手敲上面的命令。

### 方式 A：Ollama（本地，零成本）

本机已装的模型（`ollama list`）：

| 模型 | 说明 |
|---|---|
| `qwen3.5:9b` | **推荐** —— 但**必须配 `LLM_THINKING=off`**。它是思考型模型：不关思考时正文恒为空（详见下面「思考模式」）。关掉之后答得又快又口语化，实测首句 1.8~2.0 秒 |
| `ds-ins:latest` | 非思考模型，开不开思考都一样。答得偏"念公式"，**实测会把「判别式」答成机器学习里的判别器**，适合当"最弱模型"的对照 |
| `deepseek-coder:6.7b-instruct-q4_K_M` | 偏代码，课堂话术一般 |
| `ds-base:latest`、`deepseek-coder:6.7b-base-q4_K_M` | 基座模型，不会按指令回答，别用于课堂 |
| `qwen3-embedding:4b`、`dengcao/Qwen3-Embedding-4B:Q8_0` | 向量模型，与本原型无关 |

```ini
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_API_KEY=ollama
LLM_THINKING=off          # 思考型模型必须关，否则它一句话都说不出来
```

> Ollama 默认把模型放在系统盘。模型目录改过位置的话，启动服务前要让 `ollama` 知道在哪 ——
> 本机已把它设成**用户级持久环境变量**，所以不用每次设。先确认一下：
>
> ```powershell
> [Environment]::GetEnvironmentVariable("OLLAMA_MODELS","User")   # 有输出就说明已配好
> ```
>
> 若为空则设一次（设完要**新开终端**才生效）：
>
> ```powershell
> [Environment]::SetEnvironmentVariable("OLLAMA_MODELS","E:\AImodel","User")
> ```

### 启动 Ollama 服务（本机是 Scoop 安装，没有桌面托盘版）

```powershell
ollama serve
```

它会**占住这个终端**并持续输出日志 —— 演示时建议单开一个窗口跑，用完 `Ctrl+C` 停。
不想占终端就后台起（本仓库调试时用的就是这种）：

```powershell
Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
```

验证是否起来了：
```powershell
ollama list                                  # 能列出模型 = 服务正常
Invoke-WebRequest http://127.0.0.1:11434/api/tags -UseBasicParsing   # 另一种确认方式
```

停止：
```powershell
Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
```

> **课堂不需要重启**：服务中途挂掉再起回来，下一句提问会自动恢复正常
> （见下面「模型服务连不上」那条 FAQ）。

### 方式 B：OpenCode Zen

在控制台取 API Key 后，`.env`：
```ini
LLM_PROVIDER=opencode
OPENCODE_API_KEY=你的key
OPENCODE_MODEL=claude-sonnet-4-5
# OPENCODE_BASE_URL=https://opencode.ai/zen/v1   ← 与实际不符时改这一行
```

> 两个服务商都按 **OpenAI 兼容**接口调用，所以换服务商只改 `.env`。
> 先验证连通性：`python run.py check`
> **注意**：OpenCode Zen 这条路径在本机还未实测过（没有 API Key），只验证了配置读取与请求构造。

### 方式 C：OpenCode Go 套餐（DeepSeek V4.1）

Go 是 OpenCode 的**订阅套餐**（$10/月），和上面的 Zen（按量付费）**不是同一条通道**：
额度分开算，端点多一段 `/go`。所以它在代码里是**独立服务商** `opencode_go`，
而不是“在 opencode 上换个 model” —— 换 model 只会打到 Zen 那条更贵的通道上。

```ini
LLM_PROVIDER=opencode_go
OPENCODE_GO_API_KEY=你的key          # 留空则自动回落到 OPENCODE_API_KEY
OPENCODE_GO_MODEL=deepseek-v4.1-flash
# OPENCODE_GO_BASE_URL=https://opencode.ai/zen/go/v1   ← 与实际不符时改这一行
```

| 项 | Zen（`opencode`） | Go 套餐（`opencode_go`） |
|---|---|---|
| 端点 | `https://opencode.ai/zen/v1` | `https://opencode.ai/zen/go/v1` |
| 计费 | 按量付费 | 订阅额度（$10/月） |
| 默认模型 | `claude-sonnet-4-5` | `deepseek-v4.1-flash` |

> DeepSeek V4.1 在 Go 里的模型 id 是 `deepseek-v4.1-flash`（旧写法
> `deepseek-v4-flash` 目前仍被接受）。名字对不上时先跑 `run.py check` ——
> 它会把“缺 key”和“名字不对”分开报出来。

> ⚠ **Go 还强制要求一个请求头 `x-opencode-session`**（会话内稳定、会话间不同，
> 用于路由与 prompt-cache 亲和性）。**不带它连“key 对不对”都轮不到判断**：
> 服务端直接回 `400 MissingSessionID`。代码在 `config.py` 的 `opencode_go`
> 里默认每次启动生成一个 uuid，可用 `OPENCODE_GO_SESSION` 钉死；
> 这条是实测出来的 —— 加上之后同一个假 key 的报错从 400 变成了 401。

### 启动

```powershell
python run.py warmup   # 可选：预合成讲解音频，演示更顺
python run.py          # → http://127.0.0.1:8000
```

其它命令：
```powershell
python run.py audit    # 脚本体检：单元长度是否超限、锚点是否存在
python run.py test     # 内置离线自检：状态机 / 净化层 / 消息契约 / 语音缓存 / 思考模式 / 课件 / 备课 / MCP …
python -m pytest -q    # 完整 509 条用例（要 .env，见下一节）
python run.py check    # 服务商连通性
```

排查课堂流转用 `CLASSROOM_TRACE=1`（打印每次 enqueue / prepare / 状态转移 / 被丢掉的复读句）：

```powershell
$env:CLASSROOM_TRACE='1'; python run.py
```

> ⚠ **看不到日志就先加 `-u`**：标准输出重定向到文件时会被缓冲，
> 服务在跑的时候日志文件是 0 字节 —— 这一点排查问题时很坑。
> ```powershell
> python -u run.py
> ```

### 想做对照实验？用 `--env` 换一份配置

不用改 `.env`，也不会互相干扰（`--env` 指定的那份**覆盖**默认值）。它做的只有一件事：把环境变量
`DSH_ENV_FILE` 指向你给的这文件，所以对照配置长什么样由你自己定：

```powershell
Copy-Item .env .env.alt                 # 复现"模型连不上"：把 base_url 改成不存在的端口
python run.py serve --env .env.alt      # 端口也改开（如 8001），免得和主配置撞
Copy-Item .env .env.alt2                # 复现"服务抖动"：把超时调小
python run.py serve --env .env.alt2     # 端口 8002
```

> 原原型仓库里的 `env.broken` / `env.flaky` 两份现成对照配置**没有随迁移入库**（里面写的是原作者的
> 模型地址与端口）。要复现就照上面三行自己造一份，语义一样；`.env.*` 也别忘了加进本地忽略，别误提交。

---

## 2. 怎么操作

打开页面后课堂会自动开始。左侧是课件（**正在讲的那一处中心会亮起一个脉冲光圈，同一时刻只有一个；分步元素讲到时才出现**），下方是字幕。

> 光圈是"逐句推进"的：翻到一页时后端就把本页每个锚点排好（单元顺序 → 单元内锚点顺序），
> 讲到第几句就亮哪一个，换句即收灯。复述/归位沿用同一处的灯（说的还是同一行），
> 学生提问或老师应答时收灯（那是新话题，留着会张冠李戴）。以前的黄框 + 段落黄底 + 左色条已全部去掉。

- **提问**：底部输入框，或快捷按钮（`没听懂 / 举个例 / 再说一遍 / 明白了，继续`）
- **翻页**：`上一页 / 重复本页 / 下一页` —— 学生始终有显式控制权（文档 §5.5）
- **指标**：右下角实时显示，点「保存 JSON」导出完整记录
- **换服务商（不用改文件）**：点页头的「⚙ 服务商」→ 选一家（ollama / opencode /
  opencode_go）→ 改 Base URL / 模型 / API Key →「测试连接」验通 →「保存并生效」。
  保存会**写回 `.env`**（只改受管的那几个键，注释和其它配置一字不动）并当场热切换：
  新开的课堂立刻用新服务商，正在上的这一节不打断（刷新页面后生效）。
  API Key 只显示"有没有配"，**从不回显明文**；留空表示"保持原值"，不是清空。
- **全屏播放**：课件卡片下面点「⛶ 全屏播放」（或按 `F`）。全屏后课件按屏幕宽高
  **双约束**缩放并居中（比例不符时留黑边，不裁课件），光圈标记/指示器/课件操作照旧生效
  （光圈恒为 34px、不随课件缩放变形，全屏实测与目标中心偏差 0.01px）；
  底部叠加一行字幕，右上角是「上一页 / 下一页 / 退出全屏」，右下角「💬 提问」点开即可
  向老师提问或点快捷应答（老师举手等你回应时它会变成「✋ 老师等你回应」并闪烁）。
  **这个悬浮助手是常驻的**：普通页面也在（半透明，鼠标移过变实），所以按 `F11` 的
  浏览器全屏、或直接投屏时同样找得到 —— 它曾经只在"点了全屏按钮"时才出现，
  于是那两种场景下右下角什么都没有。
  `F11`/系统级全屏也会被识别成上课态（判据：没有浏览器外框且视口顶满屏幕高度），
  字幕和翻页按钮一并出来；这种全屏我们退不掉，点「退出全屏」只会先恢复常规布局，
  真正退出按一次 `F11`。

> **页头那个小版本号（如 `ui5`）是排查用的**：它来自 `/api/state`。看不到它、或数字
> 比代码旧，说明你打开的是浏览器缓存的旧页面 / 服务还是旧进程 —— 重启 `run.py`
> 再刷新一次即可（页面和静态资源现在都不缓存，且静态资源带 mtime 版本号）。
  `Esc` 退出，`←` / `→` 翻页。浏览器不允许全屏时自动退化为“铺满页面”，功能不丢；
  直接访问 `/?fs=1` 也能不进全屏就得到同一套版式（投屏 / 截图核对用）。

### 三个建议的观察场景

**① 段末让位（核心机制）**

在老师讲到某个单元**中途**时输入「没听懂」。你会看到：

```
立即回执「收到，老师讲完这一段就回答你」
   ↓ 老师继续把这！一！段！讲完（音频永远不会被硬切）
过渡「这个问题问得好，先停一下」
   ↓ 走重讲链：换说法（不是重复原话）
三步重入「好，我们回到刚才那里——<锚点>。我们接着往下看。」
   ↓ 回到讲课，从这个单元接着往下
```

盯右下角的 **让位延迟**：它等于"当前单元剩余时长"，这就是段末让位方案的全部代价。

**② 页尾探针与双门判定**

一页讲完会听到「到这里清楚吗？」然后进入等待（顶部显示倒计时）。
- 回一句「明白了」→ 记为 `confirmed` → 允许翻页
- 什么都不做 → 窗口超时 → 记为 `vague`（**弱信号，不计为理解**）→ 继续推进
- 直接提问 → 探针被打断，走让位链路

**③ 重讲链与暂放**

连续点「没听懂」，观察话术变化：

| 轮次 | 你会听到 |
|---|---|
| 1 | 换一种说法说明同一个意思 |
| 2 | 举一个具体例子 |
| 3 | 把这一小点拆成更细的步骤 |
| 4 | **暂放 + 承诺**「这个点确实有点绕，我们先把它放一放…」并记入待回看 |

第 4 轮是关键 —— 没有它，一页能卡十分钟。

---

## 3. 实现与文档的对应

| 文档位置 | 代码 |
|---|---|
| §3.2 状态定义（7 个状态） | [`state.py`](state.py) 的 `Phase` |
| §3.3 状态数据结构 | [`state.py`](state.py) 的 `ClassroomState` |
| §3.4 转移表、§2 规范的转移表 | [`machine.py`](machine.py) 的 `next_speech()` + `on_speech_end()` |
| **I5：`ANSWERING` 没有直通 `LECTURING` 的边** | `_back_to_lecturing()` 是唯一入口，`_dispatch()` 无法直接回到讲课 |
| §2 讲解单元与锚定 | [`state.py`](state.py) 的 `LectureUnit` / `Anchor`；[`script.py`](script.py) 的实例 |
| §2.3 单元长度硬约束 | [`eval.py`](eval.py) 的 `audit_script()`；`python run.py audit` |
| §4.4 指代解析所需的台账 | `Machine._enqueue()` 记下 `at_unit`（提问时正在播的单元） |
| §4.5 回答期间的新输入只入队 | `handle_input()` 的 `ACKNOWLEDGING/YIELDING/ANSWERING/RESUMING` 分支 |
| §4.6 三步重入 | `_seg_resume()`：认知层 + 定位层（状态机给定）+ 续接层（LLM 生成） |
| §4.7 重讲链 | `_gen_rephrase()` + `_enqueue()` 的 `REPHRASE_TRIGGERS` |
| §5.2 翻页双门判定 | `_seg_probe()` 的等待窗口 + `on_speech_end("unit")` 的让位/探针分派 |
| 「懂了 / 没懂」的判定 | [`machine.py`](machine.py) 的 `classify_reply()`：**否定优先**（`不明白` ≠ `明白`）、疑问句排除、长度上限。纯确认不是提问 —— 它不入队、也不让老师去"回答"一句"明白了" |
| §5.5 显式控制权 | `handle_command()`；跳过的未确认项进 `deferred` |
| §5.7 往回翻 = 重讲信号 | `handle_command("prev")` |
| §7.6 三个必观测指标 | [`eval.py`](eval.py) 的 `MetricsTracker` |
| §4.2 提问分流 | [`llm.py`](llm.py) 的 `prompt_classify()` |
| §7.3 预置话术音频 | `web.py` 的 `_startup()` warmup |

### 一个实现细节：让位边界就是音频边界

`web.py` 的 `_speak()` **会阻塞到这一段真的播完** —— 前端收到 `segment_end`，播完回 `speech_done`，后端才继续。所以：

```
段末让位 = 状态机的 speak() 天然停在音频边界上
```

这是"讲完这一段再答"最省事的实现方式：不需要处理音频打断、淡出、半句续接（文档 §1.3 说了这三个问题被这个决策消掉了）。

**播放协议带段序号**（这一条是踩坑踩出来的，别省）：

```
segment_start(seq) → audio_delta(seq) → segment_end(seq)
     → [前端真的播完] → speech_done(seq) → 状态机才认"这一段播完了"
```

没有 `seq` 的时候，一次错配（上一段的完成信号晚到、落在这一段上）就会让状态机把"播完了"
记到错误的段落上，然后**开始复读同一句话** —— 实测回执在 12 秒里被重复播了 41 次。
序号不匹配就不算播完。TTS 降级（没有音频）时前端也必须按估算时长自己收尾再回 `speech_done`，
否则"等播完"会永远等下去。

### 另一个实现细节：净化层（这个小模型逼出来的）

本地小模型会稳定地产出**不能直接念出来**的东西，而它不会因为 prompt 里写了"别这样"就改：

| 模型的实际输出 | 为什么不能用 | 处理 |
|---|---|---|
| `# 教师答` / `# 约束` / `# 学生问` | 把 prompt 结构当正文，TTS 会把井号念出来 | 标题整行丢弃/取标题内容 |
| `**教师答**` | 同上 | 整行丢弃 |
| `当前任务：学生说没听懂……策略是「换说法」` | 把 prompt 指令抄进正文 | 命中指令特征词整句丢弃 |
| 把学生的原话抄一遍当回答 | 学生听到的是复读 | 与 `transcript` 相似度 ≥ 0.7 丢弃 |
| 把刚讲过的句子原样复述 | 同上 | 同上（`_collect_clean`） |
| 过渡话说成 `接下来，我们讨论的方程式是 a(x²) +` | 半句话，截断后无法入耳 | 严格模式：长度/句读/悬空连接词任一不合格就退回预置话术 |

实现分两层，都**不依赖模型自觉**：

- [`llm.py`](llm.py)：`sanitize_spoken()` 按行清洗 + `drop_boilerplate()` 切句后再滤一次套话 + `_one_liner(strict=True)` 守过渡/重入话术
- [`machine.py`](machine.py)：`_collect_clean()` 用**字面相似度**（bigram 重合）挡复读与抄问题

测试样本就是模型真实跑出来的脏输出：[`tests/test_sanitize.py`](tests/test_sanitize.py)。

---

## 4. 怎么读指标

右下角面板 + 导出的 JSON：

| 指标 | 看什么 |
|---|---|
| **让位延迟** p50 / max | **最重要的一个**。它 = 当前单元剩余时长 + 过渡话术/分流/首句生成（这三步已并行）。实测 p50 ≈ **5.7 秒**。若 p50 超过 ~20 秒，说明讲解单元太长或生成没并行 → 回去切短单元（V1/V2） |
| **探针 发出/回应/超时** | 回应率为 0 说明探针只是打断节奏，没有采集到信号 → 考虑减少密度或去掉（**V8：诚实测它有没有净收益**）。原型实测就是 0% |
| **归位 成功/失败** | 失败率非 0 就是 I5 被破坏，属于 bug，不是调参问题 |
| **翻页 自动/手动** | 手动占比过高，说明自动判定太保守或探针窗口太长 |
| **理解信号** | `vague` 只代表"没回应"，**不代表没听懂** —— 不要当掌握度证据 |

---

## 5. 相对完整设计的简化

诚实列出原型**故意没做**的部分，避免被误当成设计已实现：

| 简化项 | 完整设计 | 影响 |
|---|---|---|
| 指代解析（§4.4） | 时间性/内容性线索打分 + 歧义反问点选 | 原型只把 `at_unit` 作为上下文提示，不做回溯匹配 |
| 情绪型的专属处理 | 共情 + 降低门槛的下一步 | 原型把情绪型也走重讲链 |
| 元问题型的固定话术 | 独立话术策略 | 同上，走普通回答 |
| 上下文压缩（§7.5） | 近页详细 + 早期摘要 + 按需检索 | 原型直接拼当前单元的局部上下文 |
| 课末"最没懂的一页"票选 | 收集定位信息 | 未实现 |
| 探针的"复述式"变体 | 信号价值最高但最费时间 | 未实现 |
| 音频的**真**流式合成 | LLM 首句 → TTS 首句 → 立即播放 | **部分实现**：生成已是"边生成边播"（句子一好就播，不等整段），但 TTS 仍按句整段合成。实测首句延时已从 20 秒降到 6 秒级 |

这些不影响状态机的正确性 —— 状态机是骨架，上面这些是挂在骨架上的策略。

---

## 6. 目录

```
TeachingFlow/classroom/          ← 本目录就是模块根（保持扁平，理由见 §1）
├─ run.py            启动/解析/备课/体检/测试 入口（子命令见 §1 的表）
├─ config.py         .env 配置（ollama / opencode / opencode_go / deepseek 都在这里）
├─ state.py          Phase / 课堂状态数据结构
├─ models.py         课件、幻灯片、课堂脚本模型
├─ script.py         演示课件 + 手写讲解单元（DECK_ID 留空时用它）
├─ machine.py        ★ 状态机（纯逻辑，无 I/O，可脱网单测）
├─ llm.py            服务商统一调用 + 课堂 Prompt + 净化层
├─ speech.py         edge-tts 合成 + 缓存 + 降级
├─ eval.py           指标采集 + 脚本体检
├─ phrases.py        预置话术 + 学生意图识别
├─ web.py            FastAPI + WebSocket + 播放协议（带段序号）+ 全部页面/接口路由
├─ mcp_server.py     MCP server（`POST /mcp` + 接入提示词生成）
├─ mcp-config.json   现成的 `mcpServers` 片段（启动时自动刷新）
├─ mcp-access-prompt.md  交给外部 Agent 的整段接入提示词（启动时自动刷新）
├─ deck/              ★ 真实 PPT：schema（数据契约）· pptx_ingest（解析）· ops（操作词表）· store（读写）
├─ course.py         课件 / 课堂脚本 / 备课计划 三件套的装配（DECK_ID 决定用哪份）
├─ script_gen.py     ★ 备课：真实课件 → 讲解单元 + 锚点 + 课件操作（含体检指标）
├─ preprocess.py      ★ 预处理作业（解析→备课→预置路线 / 补总结）+ 课件源库，/preprocess 页的后端
├─ decks/            解析与备课产物（deck.json / plan.json / media/）—— **不入库**，见 §10
├─ pptx/             课件源库：上传或拷进来的 pptx 副本（.pptx 不入库，见 docs/预处理与课件库说明.md）
├─ tools/e2e_deck.py 端到端验收：真服务跑真课件，验证"翻页 + 操作真的发生"
├─ tools/shot_deck.py 重绘截图 vs PowerPoint 导出，拼对比图 + 像素差异
├─ tools/shot_ops.py  ★ 逐个施加每一种课件操作并截图，量"到底有没有变化"
├─ tools/compare_plans.py A/B 两份备课计划（改提示词/换模型前后比数字）
├─ tools/show_paras.py 打印某一页的段落锚点 id（手工核对某个 id 是谁）
├─ tests/            44 个文件 / 509 条离线用例：状态机 · 净化层 · 消息契约 · 语音缓存 ·
│                    思考模式 · 课件解析 · 课件操作 · 备课 · 预处理 · MCP · 导演/分支
├─ web/index.html    上课页（课件重绘 + 字幕 + 快捷反馈 + 指标面板 + 「在思考」指示）
├─ web/hub.html      启动台：各页面入口 + 课件列表
├─ web/preprocess.html 预处理操作台：上传 → 跑全链（阶段/进度/日志）→ 重跑 / 补生成总结
├─ web/preview.html  逐页预览：点锚点看 id、勾选分步、缩放/激光笔，用来核对解析与版式
├─ web/script.html   课堂脚本分支树
├─ web/graph.html    课堂走向图
├─ web/deck.js       ★ 课件重绘器（元素/段落/表格/图片 + 操作效果，上课页与预览页共用）
├─ web/classroom.css · graph.css · script.css    各页样式
├─ docs/             课堂设计文档（索引见 docs/README.md）
├─ requirements.txt · pytest.ini
└─ .env.example      配置模板（复制成 .env；.env 本身不入库）
```

> 审核页 `/review` **没有对应的 html 文件**：它在 `web.py` 里按当前计划现拼 HTML，
> 所以刷新一次就是最新产物，不存在"改了前端看到旧页面"的问题。

> `dbg_*`（8 个调试脚本）、`env.broken` / `env.flaky`（两份对照配置）、`_probe.mp3`（ASR 样本音频）
> **都没有随迁移入库** —— 它们绑的是原作者的模型、端口与本机路径。本文提到它们的地方都写了替代做法。

---

## 7. 思考模式（`LLM_THINKING`）

思考型模型（如 `qwen3.5:9b`）会先"想"一大段再开口。原型把这个能力做成**可控的一等开关**，
默认**关闭** —— 不是保守，是实测结论（见下）。

```ini
LLM_THINKING=off      # 默认：所有调用都不思考
LLM_THINKING=answer   # 只有深度答疑/重讲思考，短话术和分流不思考
LLM_THINKING=all      # 全部思考（实验用，会让短话术也变慢）
```

### 三条纪律

1. **思考内容永远不会被念出来。**
   `reasoning` / `reasoning_content` / `thinking` 三种字段名都能识别，一律只进统计，
   **不进字幕、更不进 TTS**。学生在课堂上只该听到老师说的话，不该听到模型的自言自语。
   （思考指示只显示「老师在思考…」这个**状态**，不显示内容。）
2. **开思考会自动加 token 预算。**
   思考会把 `max_tokens` 吃光，正文一个 token 都剩不下 —— 表现是"模型连着、却一句话都说不出"。
   所以开思考时自动加上 `LLM_THINK_TOKEN_BUDGET`（默认 2048）。**这不是优化，是必需品。**
3. **宁可关掉短话术的思考。**
   过渡/重入/分流是"学生正在等十几个字"的环节，让它思考等于让学生干等半分钟。
   `answer` 模式就是为这件事设的：**答疑值得慢，短话术绝不能慢**。

### 实测代价（本机 `qwen3.5:9b`）

| 配置 | 首句延迟 | 让位延迟 | 思考量 | 答案质量 |
|---|---|---|---|---|
| `LLM_THINKING=off` | **1.8 ~ 2.0s** | 约 6s | 0 | 正确、口语化 |
| `LLM_THINKING=answer` | 33 ~ 55s | **63.7s** | 4200 ~ 6900 字 | 也正确，略简洁 |

**结论：思考没有换来质量提升，代价却是慢 20~30 倍。** 所以默认 `off`。
想试就改 `answer`，此时前端会显示带秒表的「老师在思考…」——
这一条是必要的：44 秒的思考如果没有指示，学生只会以为课堂卡死了。

> **踩过的坑**：`reasoning_effort` 实际**只有两端有效**。
> `none` 关得掉；`low` / `medium` / `minimal` 都被当成"非 none"照样全量思考，
> 并把预算吃光（`finish_reason=length`、正文为空）。别指望用它做"思考预算分级"。
> 另外 `extra_body={"think": False}` 走 OpenAI 兼容接口**无效**，只有原生 `/api/chat` 才认。

---

## 8. 常见问题

**Q：页面没声音？**
先看头部是否显示「纯文字(TTS 未启用)」。edge-tts 需要联网；装不上或断网时会自动降级，前端按估算时长推进，状态机不受影响。

**Q：模型答非所问 / 分流不准？**
推荐 `qwen3.5:9b` + `LLM_THINKING=off`（本机最快最准的组合）。分流失败时会回落到 `clarify` —— 最保守、最不会跑偏的一类。
如果模型把标题、prompt 指令、或刚讲过的话念出来，那是净化层该挡的，`CLASSROOM_TRACE=1` 能看到丢掉了哪些句子。

**Q：模型明明连着，却一句正文都不出？**
十有八九是**思考型模型没关思考**（它的输出全在 reasoning 里，正文恒为空）。
把 `LLM_THINKING` 设成 `off` 或 `answer`，重启课堂即可。
如果报的是「思考把 token 预算吃光了」，那是另一回事 —— 把 `LLM_THINK_TOKEN_BUDGET` 调大。

**Q：页面上报「模型服务当前不可用」/「连不上模型服务」？**
这不是课堂崩了，是**模型服务连不上**，最常见就是 ollama 没在运行。按提示做：

```powershell
ollama serve                              # 或启动 Ollama 桌面端
$env:OLLAMA_MODELS='E:\AImodel'; ollama serve    # 模型目录不在系统盘时还要设这个
```

课堂上会怎么表现（**这是设计的行为，不是故障**）：
- **讲解照常放**：讲解音频是预生成的，不受影响。只是答疑换成预置话术。
- **不会让你干等**：连不上时立刻说一句兜底话术（实测 3.7 秒），
  不会去等一个注定失败的连接超时。
- **服务起回来就自动恢复**：不用重启课堂、不用刷新页面。
  下一句提问会先去探一次服务（探测是后台跑的，不占你等待时间），成功后照常作答。

想自己复现/验证这条降级链路（模型故意写错地址）：

```powershell
Copy-Item .env .env.alt                 # 把 LLM 的 base_url 改成不存在的端口
python run.py serve --env .env.alt      # 端口与主配置错开
```

原原型里跑"断服 → 恢复"三段剧本的 `dbg_flaky.py` 未入库；手工复现只要三步：
服务正常时提一个问题 → 把 ollama 停掉再提一个（应立刻回兜底话术，不是干等超时）→ 重启 ollama 再提（应自动恢复）。

**Q：`check` 报 401 / 404？**
OpenCode 侧的 `base_url` 或 `model` 名字与实际不符，改 `.env` 里的 `OPENCODE_BASE_URL` /
`OPENCODE_MODEL`（Go 套餐对应 `OPENCODE_GO_BASE_URL` / `OPENCODE_GO_MODEL`）即可，代码不用动。
Go 套餐最常见的一处错配是把 `LLM_PROVIDER` 设成 `opencode` 却填了 Go 的模型名 ——
那样请求会打到 Zen（按量付费）而不是订阅通道。
`check` 现在会把失败原因翻译成可操作的提示，不再只丢一句 `APIConnectionError`。

**Q：有字幕，但没有声音？**
这是修过的一个**静默**故障，现在有结构性保证，正常不会再出现。三条一起管住了它：

1. **半截图不会进缓存**：合成先写 `.part` 临时文件，校验非空后才原子改名。
   进程在合成中途被杀（强杀/崩溃/刷新）只会留下 `.part`，启动时自动清掉。
2. **缓存命中也要校验大小**：0 字节的 mp3 会被判定为坏、删掉重合成，
   绝不会被当成"合成成功"发出去。
3. **偶发失败会重试**：edge-tts 是在线服务，`NoAudioReceived` 这类抖动很常见。
   现在单次合成会重试一次；失败也不是永久拉黑，60 秒后还会再试 ——
   以前一次抖动就能让那句话整节课都没声音。
4. **403 是版本问题，不是抖动**：微软把 `Sec-MS-GEC` 换成了 v2 令牌
   （`Sec-MS-GEC-Version: 2-…`，值是 `<16位>-<64位>` 两段摘要），
   而 7.2.x 及更早只会算 v1（纯 SHA256），握手**一律**被回 403。
   它的表现很好认：**每一句**都没声音（不是偶尔一句），重试也救不回来。
   修法是升级，`requirements.txt` 已经卡了下限：

   ```powershell
   python -m pip install -U "edge-tts>=7.3.1"
   ```

   > ⚠ **升级后必须重启课堂**（`Ctrl+C` 再 `run.py`）：正在跑的进程里
   > 装的是旧模块，光升级包不会生效。页面上的课件 CSS/JS 则会随刷新更新。

真出现"没声音"，页面会直接告诉你原因，控制台也会打印 `[tts] ...`，
不会再静默。自查：

```powershell
# 清掉可能存在的坏缓存（.cache/ 是运行时生成的，不入库；正常情况下也就没有坏的）
Get-ChildItem .cache\tts\*.mp3 | Where-Object { $_.Length -eq 0 } | Remove-Item -Force

# 逐段核对音频字节数：.cache\tts\ 下按句缓存，0 字节或明显过短的就要怀疑
Get-ChildItem .cache\tts\*.mp3 | Sort-Object Length | Select-Object -First 5 Name, Length
```

> 原原型的 `dbg_audio.py`（对着跑着的服务逐段统计）**未入库**，因为它依赖一个在真机上跑着的服务 +
> 一份真实模型配置。上面的 `Get-ChildItem` 只读缓存目录，够判断"这段到底有没有音频"。

> **停服务请用 `Ctrl+C`**，不要用 `Stop-Process -Force`。强杀虽已不会污染缓存，
> 但会让正在播的那一段断在半截。

**Q：第一次提问特别慢（十几秒）？**
第一次请求要等模型加载进内存。启动时的预检（`[preflight] ✅ 模型连通（已预热）`）
已经把这部分时间提前花掉了 —— 如果看到的是 `⚠ 模型暂时连不上`，说明那会儿服务就不可用。
本机 `ds-ins:latest` 回答一句大约 10~13 秒。这是模型自身速度，不是代码问题。

**Q：课堂停住了 / 同一句话反复播？**
这曾经是个真 bug（`speech_done` 错配导致状态机卡在原地复读），现在协议带 `seq`，正常不会再出现。
如果你改过前端，请确认 `speech_done` 里带回了 `curSeq`。看右上角状态徽标的 phase：
- 卡在 `PAUSING` → 在等你回应（或等窗口超时），正常
- 卡在 `ANSWERING` → LLM 调用慢或失败，日志区会有提示
- 卡在 `LECTURING` 但没动 → 按 F5 刷新，浏览器自动重连会重开一节课

**Q：想改讲解内容？**
改 [`script.py`](script.py) 里的 `_UNITS`，然后 `python run.py audit` 体检（会检查长度、锚点是否指向存在的元素、页尾标记是否正确）。

---

## 9. 接入真实 PPT（上传课件场景）

原型现在能吃**老师自己的 pptx**：解析成可锚定的结构 → 前端按原稿重绘 → 讲师在讲的同时**真的操作这份课件**（分步显示、光圈标记、指认、放大）。手写演示课件仍然可用，`DECK_ID` 留空就是它。

```powershell
# 1. 解析（EMU→px 换算、段落级切分、样式继承链全部拉平）
python run.py pptx "E:\课件\Unit 2 Sweet Home.pptx"
#    → decks/<deck_id>/deck.json + media/（图片一起搬过来）

# 2. 备课：让模型按"每段 80~180 字"切讲解单元，并规划课件操作
$env:OLLAMA_MODEL='qwen3.5:9b'
python run.py plan <deck_id>            # 全量
python run.py plan <deck_id> --slides 4-6   # 先试几页（**与已有计划合并**）
python run.py plan <deck_id> --slides 4-4 --dry-run   # 试提示词/试模型，不写盘
python run.py plan <deck_id> --rule     # 不调模型，规则兜底
python run.py plan <deck_id> --fix-ops  # 不重新备课，只清掉计划里的空操作

# 3. 体检（§8 V7 锚点命中率 / 最长单元 / 非法操作 / **操作类型分布** 都在这里）
python run.py audit <deck_id>

# 4. 上课
$env:DECK_ID='<deck_id>'; python run.py
```

### 怎么看"它真的在操作 PPT"

| 想确认的事 | 看哪里 |
| --- | --- |
| 这一页是**原稿重绘**而不是示意图 | 页面顶部「真实课件重绘 · 元素 N」；`/preview?deck=<id>&slide=4&bare=1` 能与 PowerPoint 逐页对照 |
| 讲师真的在动课件 | 右侧日志「课件操作 下发/命中」；`--verbose` 跑 `tools/e2e_deck.py` 能看到 `step → s02-sh03-p01（第 4 句）` |
| **每一种操作**在页面上真的有效果 | `tools/shot_ops.py` —— 逐个施加并截图，还会算"和什么都没做差多少" |
| 某个锚点 id 到底是哪一行 | `tools/show_paras.py <deck_id> <页号>`；或直接看 `/preview` 页下方的「本页可锚定的元素」 |
| 两次备课哪次更好 | `tools/compare_plans.py <deckA> <deckB>`（单元数/时长/命中率/操作分布并排比） |
| 讲到哪、指到哪对得上 | op 带的 `at_text` 会解析成"含这段文字的那一句"；`audit` 会把对不上的**报出来** |
| 该页有没有"讲了但学生看不到"的内容 | `audit` 的「这些页没有任何讲解单元」；分步隐藏的锚点没人 reveal 时也会在审核页标出来 |

`tools/shot_ops.py` 的输出长这样（第 4 页，13 组）：

```
highlight_title      像素变化  0.66%  ✅ 有效果
point_para           像素变化  0.06%  ✅ 有效果      ← 激光点本来就小，这里只看"有没有变化"
step_para            像素变化 15.68%  ✅ 有效果
zoom_pic             像素变化 45.66%  ✅ 有效果
unfocus_restores     像素变化  0.00%  ✅ 正确复原    ← 成对操作必须撤得回来
reset_zoom_restores  像素变化  0.00%  ✅ 正确复原
clear_restores       像素变化  0.00%  ✅ 正确复原
```

它拼出的总览图在 `.cache/shot/ops_sheet.png`（`.cache/` 是运行时目录，不入库）。**"能撤回来"是最容易被漏掉的一组断言**：
撤不回来意味着放大/压暗会跟着学生翻到下一页 —— 只测"有效果"永远测不出这类坏体验。

### 设计要点（踩过的坑都在这）

1. **锚点细到段落，不是形状。** 真实课件常把"音标/释义/用法/搭配/例句"塞进**一个**文本框，
   只在形状级锚定等于"指着一整块" —— 讲师说不出"我们看第三行"。所以解析器给每个段落
   一个 id（`s04-sh02-p03`），表格细到单元格（`s03-sh03-c04`）。
2. **"句"是播放分块，不是语法句。** 讲解单元按 `split_sentences` 切块（过短的句子会并入下一块），
   所以操作支持两种定位：`at_text`（**模型规划用**：按"讲到含这段文字的那一句"）与
   `at_sentence`（规则兜底用：句号由代码算准）。
3. **模型编定位文字时要有兜底。** 模型爱写"我们来看第一题""后接物品名词"这类**自己造的措辞**，
   正文里根本没有。三级降级：原文子串 → 近似匹配 → 用**目标元素自己的文字**去对 → 退回本单元第 1 句。
   每一级都会在 `audit` 里留痕（`❌`/`⚠️`），不静默。
   锚点这边同理：模型一个锚点都没给时，兜底锚**整块**（正文最多的那个形状），
   而不是"随便某一行" —— 兜底曾经写的是 `next(iter(para_ids))`，而那是 set 的哈希顺序：
   同一页两次备课会锚到不同的行，实测还正好锚到"community center（社区中心）"那一行，
   可老师说的是"记一下这些搭配"。**粗但不错，比精确地指错行重要。**
4. **版式/母版的装饰也要画，但只画不锚。** 封面大箭头、底部色条、页角小图都在 layout/master 上；
   不收的话，重绘出来会"干净但少了东西"（最坏是白字白底 —— 字在 slide 上、蓝底在版式上）。
   但装饰不能进锚点清单：学生看不见 `s04-la-sh01` 这种 id，模型会去锚它。
   层序必须是 母版 < 版式 < 本页元素。
5. **浏览器画不出 SVG 的 `fill: linear-gradient(...)`。** 传进去是非法值，浏览器按黑色实心处理 ——
   母版上的浅色箭头会变成两道黑带。渐变必须建成真的 `<linearGradient>`。
6. **翻页时不能把整页藏起来。** 分步显示是"讲到才出现"，但学生**自己按下一页**的意图是
   "我要看这一页" —— 所以手动翻页整页直接显示全，否则翻过去是一片空白，看着像课件坏了。
7. **主循环异常必须喊出来。** 状态机里一个 AttributeError 就足以让整节课安静下来，
   而日志里可能一行都没有（实测：第一次自动翻页时抛错，课堂从此不再说话）。
   现在主循环会打印堆栈 + 给学生一句"这是程序问题，不是你的操作问题"。
8. **"发下去什么都不做"的操作要清掉。** 模型很爱写"讲完复原"，但那一页根本没放大/压暗过 ——
   这种 `reset_zoom`/`unfocus` 照样计入"课件操作 下发 N"，把真正没命中的操作淹掉。
   `--fix-ops` 会按页清掉它们（真实计划里清掉 3 个），并且**只在真的做过 focus/zoom 之后**才留复原。
9. **提示词里的示例会被照抄。** JSON 骨架写成 `"at_text":"讲到这句时出现"`，
   模型就真的把这句占位文字当成定位原文填进来（同一页 3 个 op 全废）。
   骨架里只能放 `"ops":[]`，示例文字必须写成 `<你正文里的 6~15 字>` 这种**明显不可复制**的占位。
10. **验收工具自己也会坏，而且坏得很安静。** `?bare=1` 模式（所有对比截图都用它）里，
    一个被删掉的 DOM 节点让 `render()` 中途抛 `Cannot set properties of null`，
    把它后面的逻辑整段跳过 —— 表现是"操作不生效"，实际是页面早就不往下走了。
    验收脚本必须把**明确的失败**和"什么都没有发生"分开：这次就是靠"像素变化 0.00%"报出来的。

### 实测数据（这份 42 页课件）

| 指标 | 数值 |
| --- | --- |
| 解析 | 180 元素 / 33 图片 / 1190 个 run 100% 拿到字号与颜色（继承链兜底 0 次） |
| 锚点 | 单元 177 个 · **锚点命中率 317/317 = 100%**（段落级 304） |
| 时长 | 全课约 44.7 分钟 · 最长单元 29 秒（硬上限 250 字，0 个超限） |
| 课件操作 | 160 个 · 目标全部落在当页锚点上（`step 89 / highlight 67 / point 2 / reveal 2`） |
| 操作效果 | 13 组逐个截图核对：全部有效果，且 `unfocus`/`reset_zoom`/`clear` 都能正确复原 |
| 重绘保真 | 与 PowerPoint 同页导出对比：封面 7.5% / 目录 3.9% / 单词页 10.7% / 练习页 8.6% 像素差异（余量主要是字体渲染与母版标题栏） |

> **这份计划只用到了 4 种操作。** `focus`（压暗其余）和 `zoom`（放大）在整份计划里是 0 个 ——
> 原因已定位：提示词里只写了 `step/highlight/point/zoom`，`focus` 压根没告诉模型。
> 现在提示词补齐了 `focus`/`unfocus` 与配对要求（`--dry-run` 试备一页已验证：非法操作 3 → 0）。
> 整份计划要重新备一遍才会用上它们。

### A/B：换提示词重备一遍，到底该不该换

补完提示词后把**整份课件重备了一遍**做对照（42 页，约 25 分钟），并用
[`tools/compare_plans.py`](tools/compare_plans.py) 把两次备课摆在一起：

```powershell
python tools/compare_plans.py <deck_id> <另一份计划所在的 deck_id>
```

| 指标 | A：现在用的这份 | B：补完提示词重备的 | 判断 |
| --- | --- | --- | --- |
| 单元数 / 总时长 | 177 个 / 44.7 分钟 | 158 个 / 41.7 分钟 | 差不多 |
| 最长单元（学生最坏等待） | 29.0 秒 | 32.6 秒 | A 略好 |
| 锚点命中率 | 317/317 = 100%（段落级 304） | 293/293 = 100%（段落级 292） | 都满分 |
| 低置信度锚点 | 5 个 | **2 个** | B 好 |
| 非法操作（模型自己编定位文字） | 34 个 | **2 个** | B 明显好 |
| 操作总数 | 160 | 94 | — |
| 操作类型分布 | `step 89 / highlight 67 / point 2 / reveal 2` | `focus 56 / unfocus 25 / highlight 7 / step 5 / reveal 1` | **各有各的问题** |

结论是**两边都不该直接用**：B 证明了提示词那两处修对了（非法操作 34 → 2，`focus` 第一次被用上），
但它**矫枉过正** —— `step` 从 89 掉到 5 个，而"讲到才出现、其余压暗"正是这个原型最核心的机制；
同时 56 个 `focus` 只配了 25 个 `unfocus`（成对关系没配齐）。

所以：**A 继续当默认**（它是端到端验收过的那一份），B 只作为对照结论留在本节，**没有入库**。

想把 B 重新生产出来、再换过去看，三步（都花 token 与时间，约 25 分钟）：

```powershell
Copy-Item "decks\<deck_id>\plan.json" "decks\<deck_id>\plan.bak.json"   # 先留一份 A
python run.py plan <deck_id>                                     # 用当前提示词重备一遍 = 新的 B
# 想换过去：备份好 A 之后，把新产的 plan.json 覆盖回去即可（plan.bak.json 里就是 A）
```

> 一个方法论上的提醒：**这是一次一样本的对比**，而 9b 模型的输出本来就会抖。
> 所以这里不写"新提示词更好"这种结论，只说"哪几个数字动了、动了多少" ——
> 想下结论得跑三五次看分布。`tools/compare_plans.py` 就是为这件事留的。

### 一个自己踩进去的坑：目录名不是课件 id

做上面那次 A/B 时，我把课件目录复制了一份 `…__v2`，在里面重备 —— 结果 42 页备完，
**计划写回了原课件的目录**：课件里的 `deck_id` 才是真身，`plan.json` 一律按它写。
（随后已恢复，并留了护栏：`load_deck` 发现目录名与课件 id 不一致会当场警告，
`run.py plan` 也会在**开跑前**先打印"这次会写到哪"，测试 `[D7]` 盯着这条警告。）

顺便修掉了测试的老毛病：几个 `[D*]` 用例没给 `decks_root`，跑一次就在真实
`decks/` 里留下 `test_demo`、`d`、`real_id` 这些垃圾课件 —— 现在全部落到临时目录，
实测"跑测试前后目录数不变"。

### 已知边界（不做，或做得不精确）

- **操作词汇表没被用满**：`focus`/`zoom` 需要重新备课才会出现（见上）；`hide`/`clear` 目前只在预览页手工演示里有。
- **动画**：只取"进入类"分步（`step`/`reveal`），**退出/强调动画不重放**；原稿分步数会显示在页面上（`原稿分步 N`），供对照。
- **认不出的几何预设**：按矩形渲染并保留 `preset` 字段（前端已内置 chevron/箭头/三角形/星形等的近似轮廓）。
- **渐变/图片描边**：CSS `border` 表达不了，不画（**不会描成黑色**），日志区说明原因。
- **外链图片**：指向作者本机路径（`file:///D:\...`）的一律取不到；http(s) 的会让浏览器兜底加载。
- **需要"鼠标拖拽/输入"才能推进的页**：原型只做"讲解 + 指认"，不代替学生做题。

### 也可以在浏览器里做：`/preprocess`（预处理操作台）

服务起来后打开 `/hub` →「预处理 · 课件库」，或直接 `/preprocess`。这一页把上面那串命令收成按钮，
并把**后台作业**的进度与日志放在旁边（备课可能跑几十分钟，命令行那条路照旧可用，两者写同一份产物）。

| 在页面上做什么 | 等价于 |
|---|---|
| 上传 `.pptx` 到源库 | 把副本放进 `TeachingFlow/classroom/pptx/`（源与产物分离；`.pptx` 不入库） |
| 预处理（全链） | `run.py pptx` → `run.py plan` → `run.py routes`，后台跑 |
| 只重备课 | `run.py plan <deck_id>`（复用已解析的 deck.json） |
| 重新解析+备课 | 先重新解析再备课（源文件找不到时按钮是灰的，见 `source.txt` 回退） |
| 补生成总结 | `run.py summarize <deck_id>`：只补走向图卡面那句话，正文一格不动 |
| 设为当前上课 | 换掉 `.env` 的 `DECK_ID` 并立刻生效（下一节课就用它；课堂在跑时禁止） |

覆盖写 `plan.json` 之前一律先备份成 `plan.bak.json`（同目录），重跑崩了手里还有上一版。

```powershell
# 只想给已有计划补那句总结（不重新备课，不写盘先看数量）
python run.py summarize <deck_id> --dry-run
python run.py summarize <deck_id>
```

相关代码：[`deck/pptx_ingest.py`](deck/pptx_ingest.py)（解析）、[`deck/schema.py`](deck/schema.py)（契约）、
[`deck/ops.py`](deck/ops.py)（操作词表 + 按句/按文字定位）、[`script_gen.py`](script_gen.py)（备课 + 体检）、
[`course.py`](course.py)（课件/脚本/计划装配）、[`web/deck.js`](web/deck.js)（重绘器）、
[`web/preview.html`](web/preview.html)（逐页对照）、[`tools/e2e_deck.py`](tools/e2e_deck.py)（端到端验收）、
[`tools/shot_deck.py`](tools/shot_deck.py)（重绘 vs PowerPoint 对比图）。

---

## 10. 媒体与示例课件（`media/` 未入库）

`decks/<deck_id>/` 里除了 `deck.json` / `plan.json`，还有一个 `media/`：从 pptx 里抽出来（或联网下载）的图片，
上课页按相对路径取它。**`media/` 与源 `.pptx` 都不入库**（二进制，且随时能从源重新生成）。

后果（都可预期，不是坏了）：

| 现象 | 原因 |
|---|---|
| `python run.py decks` 显示「媒体 0」 | `media/` 不存在 |
| 预览页 / 上课页的图片 404（`GET /api/deck/media/<deck_id>/<name>`） | 同上 |
| 图片位置是空白 | 元素还在、几何也对，只是 `media/` 里没有那份像素（不报错） |

补齐办法（用你自己的课件，或者把示例课件的源 pptx 找回来）：

```powershell
# 1) 源文件放进源库（.pptx 已被忽略，不会误提交）
Copy-Item "D:\课件\高一物理第一课.pptx" "pptx\"       # 文件名随意；deck_id 由课件内容决定

# 2) 解析：图片会一起搬进 decks/<deck_id>/media/
python run.py pptx "pptx\高一物理第一课.pptx"

# 3) 备课（媒体齐了再备课，操作才会被规划到图片上）
python run.py plan <deck_id>

# 4) 核对：「媒体 N」应该大于 0
python run.py decks
```

浏览器里做同一件事：`/hub` →「预处理 · 课件库」，上传 → 全链（见 §9 最后一段）。

> `decks/` 下现在有两份示例课件：`deck.json` / `plan.json` 齐，但 `media/` 与 `source.txt` 不在，
> 所以「媒体 0」是正常现象。上课、体检、A/B 这些不依赖图片的功能照常可用；
> 只是"重绘保真度"这类断言在补齐媒体之前没法复核。

---

## 11. 已知边界：测试基线、样本音频、与 TeachingFlow 的接线

### 11.1 测试基线：两条历史失败用例（与 MAT 合并无关）

在 classroom 目录里、配好 `.env`（`DECK_ID` 指向 `decks/` 里的示例课件）后
`python -m pytest -q` 实测 **507 passed / 2 failed**（共 509 条）。失败的这两条是原原型就有的历史用例，
钉的是**作者那份 42 页课件**的常量，跟仓库里换过的示例课件对不上：

| 失败用例 | 断言 | 实际 |
|---|---|---|
| `tests/test_script_tree.py::test_tree_real_deck_counts_and_no_variants` | `meta["total_slides"] == REAL_SLIDES`（常量 42） | `assert 38 == 42` |
| `tests/test_script_graph.py::test_graph_real_deck_mainline_matches_order_index` | `len(routes) == 14` | `assert 8 == 14` |

根因是 `.env` 的 `DECK_ID`：`.env.example` 默认指向 38 页的 `高一物理第一课_运动的描述`，
而这两个常量来自 42 页的 `Unit_2_Sweet_Home…`。把 `DECK_ID` 换成 42 页那份，这两条立刻过：

```powershell
$env:DECK_ID='Unit_2_Sweet_Home单词解析课件_2025_2026学年人教版_2024_八年级英'
python -m pytest -q tests/test_script_tree.py tests/test_script_graph.py   # 34 passed
```

（进程环境变量优先于 `.env` —— `config.py` 的 `load_dotenv()` 不覆盖已存在的变量。想长期这样就在 `.env` 里改。）

> 这条也解释了"为什么换了份课件就红两条"：不是合并引入的问题，是**真实课件的规模断言天然绑课件**。
> 要彻底消掉，得把常量改成"按当前样例算"，那是改测试，不在文档范围。

### 11.2 `_probe.mp3` 未入库：ASR 的「测试」按钮会提示找不到样本

`web.py` 的 `_cfg_test_asr()` 用本目录下的 `_probe.mp3` 真走一次转写，这个样本音频是二进制、没有入库。所以：

- 「设置 → ASR」点「测试」会返回 `找不到样本音频 …，无法真实走一次转写（放一段课堂录音的 mp3 到这个路径再测）`。
- 这不是配置错。想真测就放一段 mp3 到 `TeachingFlow/classroom/_probe.mp3`。
- 不测也能用：ASR 只影响「学生语音提问」这一路，按钮失败不影响上课。

### 11.3 与 TeachingFlow 流水线的接线

课堂子系统自己不产片，只消费铺好的 pptx。上游用三个开关接它（`TeachingFlow/flow.py`）：

| 开关 | 行为 |
|---|---|
| （默认）`--classroom` | 出片后自动备课：解析 pptx → `run.py plan`，产物进 `classroom/decks/<deck_id>/` |
| `--no-classroom` | 只出片，不备课 |
| `--classroom-serve` | 起课堂 Web 服务后退出；`--` 之后的参数原样透传给 `classroom/run.py`（不需要一句话需求） |
| `--mock`（且没加 `--render`） | 不跑备课 —— 假产物不值得备一次课 |

`deepseek` 服务商复用 `PPT-Agent/.env` 里的 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`：
"出片"和"上课"共用同一条 key。想彻底分开，就在 `classroom/.env` 里显式写 `LLM_PROVIDER=ollama`
（或另外填一份 `OPENCODE_*`）。

> `deepseek` 在设置面板里**只在你把 `LLM_PROVIDER` 设成 `deepseek` 之后才出现**，面板本身不能
> 直接切过去 —— 这条是刻意的：它的 key 归 `PPT-Agent/.env` 所有，若面板也能写，就会在
> `classroom/.env` 里多出一份同名 key，而 `classroom/.env` 优先级更高，之后在 `PPT-Agent/.env`
> 里换 key 会"看起来没生效"。要切就在 `classroom/.env` 里改一行 `LLM_PROVIDER=deepseek`。
> 出片后的自动备课不受这条限制：当前服务商用不了而仓库根有 `DEEPSEEK_API_KEY` 时，
> `bridge` 会临时切到 `deepseek`（不动 `os.environ`，同进程正在上课也不受影响）。

> 反过来，课堂子系统**不改动** TeachingFlow 的任何产物：它只读 `runs/` 里的 pptx，写自己 `decks/` 下的东西。
