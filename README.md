# MAT · 多智能体教育系统

教学流水线：**一句话需求 → 学习方案 → 单元教学 → 课件大纲 → PPTX**，出片之后可以接着**上课**。
每一步的模型输出都落盘，可复查、可续跑、可单独重跑某一步。

```bash
python flow.py "我想简单学一下《祥林嫂》这篇课文"
```

## 快速开始

```powershell
# 1) 依赖 + 拉取 ppt-master（出片 skill）+ 生成课堂配置
powershell -ExecutionPolicy Bypass -File .\setup.ps1

# 2) 填 Key（出片与课堂的 deepseek 服务商读同一份）
notepad PPT-Agent\.env        # DEEPSEEK_API_KEY=...

# 3) 跑
cd TeachingFlow
python flow.py "我今天突击一下生物的光合作用"
python flow.py "我想系统学习高中语文" --no-input

# 4) 上课（可选；流水线出片后默认已经把它备成课）
powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1
```

## 文档

- **使用说明（流程 / 产物路径 / 参数 / 调试 / 生产）**：[`TeachingFlow/docs/README.md`](TeachingFlow/docs/README.md)
- **设计契约与取舍（含写死清单、口径）**：[`TeachingFlow/CHAIN.md`](TeachingFlow/CHAIN.md)
- **课堂子系统（配置 / 页面 / 子命令 / 已知边界）**：[`TeachingFlow/classroom/README.md`](TeachingFlow/classroom/README.md)
- **课堂设计文档（状态机 / 交互 / 分支 / MCP / 预处理）**：[`TeachingFlow/classroom/docs/README.md`](TeachingFlow/classroom/docs/README.md)

## 目录

```
TeachingFlow/       一句话到课件的编排器（入口 flow.py）
├── core/           范围/强度/周期解析、蓝图、门控、落盘、LLM、命名
├── steps/          S0 S0a S0b S1 S2 S3 S4a S4 S5
├── promptpack/     自有模板（模块判定 / 周期估算 / 页数决策 / 模块切分）
├── fixtures/       --mock 用的假 LLM 回复
├── tools/          smoke_cases / prompt_lint / backfill_llm_returns / package_release
├── docs/README.md  使用文档
└── classroom/      课堂子系统：pptx → 备课 → 上课（入口 run.py + web.py）
    ├── deck/       pptx 解析与产物读写（deck.json / plan.json 的契约）
    ├── web/        页面（上课 / 启动台 / 预处理 / 预览 / 审核 / 脚本树 / 走向图）
    ├── decks/      解析与备课产物（不入库）
    ├── pptx/       课件源文件投放处（.pptx 不入库）
    ├── tools/      验收脚本（端到端 / 截图对比 / 两份计划 A/B）
    ├── tests/      状态机 · 净化层 · 消息契约 · 课件解析 · 备课 的离线测试
    └── docs/       课堂设计文档（状态机规范 / 交互设计 / 分支 / MCP / 预处理）
PromptTemplate/     提示词模板库（teacher_planner / teacher_unit / teacher_ppt + fewshot）
PPT-Agent/          产片引擎（pipeline.py：SVG → PPTX）
```

## 运行产物

`TeachingFlow/runs/<日期>/<课程>_<时分秒>/`：

```
LLM返回/                                  # 每次调用的「模型原文 + 提示词」
第1章-章名/第M单元-单元名.pptx             # 交付件
第1章-章名/第M单元-单元名-讲稿.md
第1章-章名/第M单元-单元名-页面说明.md
00_spec.json 01_blueprint.yaml 02_targets.json …   # 过程产物
manifest.json                             # 每步 ok / 校验 / 页数与周期依据
```

课堂子系统的产物是另一份，落在 `TeachingFlow/classroom/decks/<deck_id>/`：`deck.json`（解析结果）、
`plan.json`（讲解单元 + 锚点 + 课件操作）、`media/`（从 pptx 抽出的图片）。同样不入库。

## 课堂子系统

**干什么的**：把流水线产出的 pptx 变成一节能上的课。
pptx → 解析成可锚定的结构（每个段落一个 id）→ 让模型备课（讲解单元 + 锚点 + 课件操作）→
学生对着重绘的课件上课：字幕、提问、段末让位、翻页。它自己不出片，只消费已有的 pptx。

**配置**（`setup.ps1` 已从 `.env.example` 生成 `TeachingFlow\classroom\.env`，正常只需确认两项）

```powershell
copy TeachingFlow\classroom\.env.example TeachingFlow\classroom\.env   # setup.ps1 已生成；手工恢复用
# DECK_ID=             # 填 decks/ 里的课件 id = 上真实课件；留空 = 手写演示课件
# LLM_PROVIDER=ollama  # 默认走本地 ollama；改 deepseek 则复用 PPT-Agent\.env 的 DEEPSEEK_* 三项
```

**启动**

```powershell
powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1           # → http://127.0.0.1:8000
powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1 decks     # 参数原样透传给 run.py
```

**页面**（同一个服务，`/hub` 是入口）

| 路径 | 页面 | 干什么 |
|---|---|---|
| `/` | `web/index.html` | 上课页：课件重绘 + 字幕 + 提问 |
| `/hub` | `web/hub.html` | 启动台：各页面入口与课件列表 |
| `/preprocess` | `web/preprocess.html` | 预处理操作台：上传 pptx → 解析 → 备课 → 补总结 |
| `/preview` | `web/preview.html` | 逐页预览：核对解析与版式、查锚点 id |
| `/review` | 服务端拼 HTML（`web.py`） | 课堂脚本审核页：按页列单元 / 锚点 / 时长 / 操作 |
| `/script` | `web/script.html` | 课堂脚本分支树 |
| `/graph` | `web/graph.html` | 单元与分支的走向图 |
| `/mcp` | `mcp_server.py` | 外部 Agent 的接入端点（POST） |

**和流水线怎么接**

- 出片后**默认自动备课**（`--classroom`，默认开）：`pptx` 解析 → 备课 → 写进 `classroom/decks/<deck_id>/`。
- `--mock` 且没加 `--render` 时不跑 —— 假产物不值得备一次课。
- `--no-classroom` 只出片、不备课。
- `--classroom-serve`：起课堂 Web 服务后退出，`--` 之后的参数原样透传给 `classroom/run.py`（不需要一句话需求）。

**已知边界**（细节见 [`TeachingFlow/classroom/README.md`](TeachingFlow/classroom/README.md) 第 10、11 节）

- `decks/*/media/` 与源 `.pptx` 不入库 → 仓库里那两份示例课件预览图 404、`run.py decks` 显示「媒体 0」。
  自备源 pptx 放进 `classroom/pptx/` 重跑一次预处理即可。
- `_probe.mp3` 不入库 → 页面上 ASR 的「测试」按钮会提示找不到样本音频。
- 课堂测试基线：配好 `.env` 时 `507 passed / 2 failed`（共 509 条；两条历史失败用例：真实课件页数 38 vs 42）。
  没有 `.env` 的全新克隆是 `9 failed / 495 passed / 5 skipped`；口径见 classroom README 第 11 节。

## 自检（不花 token）

```bash
cd TeachingFlow
python tools/smoke_cases.py     # 意图判定 16 条
python tools/prompt_lint.py     # 模板里不许出现示例页数
python flow.py "…" --mock --to plan
```

课堂侧（任意目录；脚本按自身位置找 classroom）：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1 test   # 内置离线自检（10 个模块，不是 pytest）
```

完整用例（44 个文件 / 509 条）要直接跑 pytest；从仓库根跑也一样（根目录 `pytest.ini` 已把收集范围钉在课堂测试上，不会扫进 `ppt-master/`）：

```powershell
cd TeachingFlow\classroom
python -m pytest -q
```

`ppt-master/` 需要单独拉取（`setup.ps1` 会做），也可设环境变量 `PPT_MASTER_SKILL_DIR` 指向已有副本。
