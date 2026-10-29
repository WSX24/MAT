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
powershell -ExecutionPolicy Bypass -File .\start-app.ps1   # → http://127.0.0.1:8000
# 或者：python app.py（只起服务，不开浏览器）
```

> 课堂只有这两个入口，**都在仓库根**。`TeachingFlow/classroom/run.py` 是备课/运维脚本，
> 不再起网页服务（原来的 `run.py serve` / `start-classroom.ps1` 已删除）。

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
└── classroom/      课堂子系统：pptx → 备课 → 上课（网页入口是仓库根的 app.py）
    ├── app/        FastAPI 应用：HTTP/WS 契约、路由、依赖装配
    ├── store/      sqlite：档案 / 课程 / 单元 / 会话 / 进度 / 事件 / 建课会话 / 单课出片任务
    ├── studio/     引擎适配：扫描 runs · 备课 · 大纲 · 会话运行时
    ├── course_plan.py    对话式建课的纯函数层（需求 / 大纲 / 蓝图 / 单元设计 / 10 模块渲染）
    ├── course_builder.py 建课状态机 + 模型工具调用 + 后台任务 + 落盘入库 + 逐课出片
    ├── webapp/     前端（Vite + Vue + TS，dist 入库）
    ├── deck/       pptx 解析与产物读写（deck.json / plan.json 的契约）
    ├── decks/      解析与备课产物（不入库）
    ├── pptx/       课件源文件投放处（.pptx 不入库）
    ├── tools/      验收脚本（端到端 / 截图对比 / 两份计划 A/B）
    ├── tests/      契约 · 端到端主线 · 建课 · 状态机 · 备课 · 课件解析 的离线测试
    └── docs/       课堂设计文档（正式版设计 / 课程生成 / 使用说明 / 状态机 / 分支 / MCP / 预处理）
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
outline.json / outline.md                 # 对话式建课写出的结构化大纲（章 → 单元 + 知识点）
unit_design_<uid>.json                    # 单元课程安排设计（学生看不到；出片与备课读它）
manifest.json                             # 每步 ok / 校验 / 页数与周期依据
```

课堂子系统的产物是另一份，落在 `TeachingFlow/classroom/decks/<deck_id>/`：`deck.json`（解析结果）、
`plan.json`（讲解单元 + 锚点 + 课件操作）、`media/`（从 pptx 抽出的图片）。同样不入库。

## 课堂（学生自学）

**干什么的**：一门课 → 单元 → 一节课，学生自己对着重绘的课件学：字幕逐句出来、能提问、
能暂停/翻页、下次从上次那页接着上。pptx 由流水线产出，或直接导入。

**交互主线**

```
课程库（首页 /）
   → 生成课堂 /app/generate（和模型聊：想学到什么程度 / 有什么基础 / 学多久）
        → 模型提问卡 → 需求确认卡 →（符合才继续）→ 课程大纲（章 → 单元，可编辑）
        → 手动确认 → 逐章设计每一课 → 存入课程库
   → 课程详情 /courses/<id>（章 → 单元行：课件进度 / 时长 / 进度 / 继续 / 大纲）
        → 「生成下一课 PPT」：上到哪一课，生成哪一课（也可单点某一课的「生成课件」）
   → 单元大纲 /units/<id>/outline（这单元讲什么，可从某页开始）
   → 上课 /learn/<unitId>（课件重绘 + 字幕 + 提问 + 暂停/翻页/语速）
   → 课后小结 /sessions/<id>/summary（时长 / 覆盖页数 / 提问数 / 继续学）
   → 再进同一单元：从上次位置**续学**；换档案则进度互不干扰
```

> 没有 PPT 的那一课不会报错：页面给一个「生成这节课的 PPT」的入口，生成完自动进课堂。
> 设计细节（工具、状态机、落盘形状、降级）见
> [`TeachingFlow/classroom/docs/课程生成与渐进式课件.md`](TeachingFlow/classroom/docs/课程生成与渐进式课件.md)。

**课程从哪来**（三条路，最后落到同一批产物）

1. **对话式建课**：`/app/generate` 里聊出来的课，直接写进课程库（`source='builder'`）。
2. **TeachingFlow 的 run**：`TeachingFlow/runs/<日期>/<课程名>/` 有出片产物时，
   首页「重新扫描」（`POST /api/v1/courses/scan`）就进课程库。
3. **导入 pptx**：首页「导入课件」上传一份 `.pptx`（`POST /api/v1/courses/import`），
   立刻进课程库并顺手备一次课。

**数据落位**

| 东西 | 位置 |
|---|---|
| 档案 / 课程 / 单元 / 会话 / 进度 / 事件 | `TeachingFlow/state/classroom.sqlite`（不入库） |
| 解析 + 备课产物 | `TeachingFlow/classroom/decks/<id>/`（deck.json / plan.json / media/，不入库） |
| 导入的原文件 | `TeachingFlow/state/imports/<id>/` |
| 语音缓存 | `TeachingFlow/classroom/.cache/tts/` |

**配置**（唯一出处是仓库根的 `PPT-Agent/.env`，课堂与出片读同一份）

```powershell
Copy-Item .env.example PPT-Agent\.env    # 然后填 DEEPSEEK_API_KEY=...
```

**没有 key 会怎样**：服务照常起、课照常上，两处降级 ——
① 备课走**规则兜底**（讲解基本是照读课件）；② 只有文字、没有语音（`edge-tts` 也连不出去时同样降级）。
启动横幅会把这些逐项写清楚，不用猜。

**和流水线怎么接**

- 出片后**默认自动备课**（`--classroom`，默认开）：`pptx` 解析 → 备课 → 写进 `classroom/decks/<deck_id>/`。
- `--mock` 且没加 `--render` 时不跑 —— 假产物不值得备一次课。
- `--no-classroom` 只出片、不备课。
- `--classroom-serve`：`--` 之后的参数原样透传给 `classroom/run.py`（运维子命令）。
  **注意**：课堂网页入口已统一到仓库根，起服务请用 `python app.py` / `start-app.ps1`；
  `run.py serve` 随旧 UI 一起删除，透传 `serve` 只会得到一份说明。

**已知边界**（细节见 [`TeachingFlow/classroom/README.md`](TeachingFlow/classroom/README.md)）

- `decks/*/media/` 与源 `.pptx` 不入库 → 仓库里那两份入库课件的图片会 404、`run.py decks` 显示「媒体 0」。
  自备源 pptx 放进 `classroom/pptx/` 重跑一次预处理即可。
- 旧的六个页面（`/hub` `/graph` `/script` `/review` `/preprocess` `/preview`）与 `web.py` 已删除，
  诊断信息改走 `CLASSROOM_DEV=1` 下的 `/dev/units/{id}/plan`、`/dev/sessions/{id}/events`。
- 课堂测试基线（离线，不联网）：`613 passed`。其中两条「真实课件」用例**按固定 deck_id 选**入库课件
  （`Unit_2_Sweet_Home…` 42 页 / 177 格；`高一物理第一课…` 38 页 / 167 格），选不到就 skip ——
  往 `decks/` 里导入新课件不会再让它们变红。

## 自检（不花 token）

```bash
cd TeachingFlow
python tools/smoke_cases.py     # 意图判定 16 条
python tools/prompt_lint.py     # 模板里不许出现示例页数
python flow.py "…" --mock --to plan
```

课堂侧内置离线自检（几段脚本各自的主函数，不是 pytest）：

```powershell
cd TeachingFlow\classroom
python run.py test
```

完整用例（613 条）要直接跑 pytest；从仓库根跑也一样（根目录 `pytest.ini` 已把收集范围钉在课堂测试上，不会扫进 `ppt-master/`）：

```powershell
cd TeachingFlow\classroom
python -m pytest -q
```

`ppt-master/` 需要单独拉取（`setup.ps1` 会做），也可设环境变量 `PPT_MASTER_SKILL_DIR` 指向已有副本。
