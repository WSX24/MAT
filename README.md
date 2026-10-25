# MAT · 多智能体教育系统

教学流水线：**一句话需求 → 学习方案 → 单元教学 → 课件大纲 → PPTX**。
每一步的模型输出都落盘，可复查、可续跑、可单独重跑某一步。

```bash
python flow.py "我想简单学一下《祥林嫂》这篇课文"
```

## 快速开始

```powershell
# 1) 依赖 + 拉取 ppt-master（出片 skill）
powershell -ExecutionPolicy Bypass -File .\setup.ps1

# 2) 填 Key
notepad PPT-Agent\.env        # DEEPSEEK_API_KEY=...

# 3) 跑
cd TeachingFlow
python flow.py "我今天突击一下生物的光合作用"
python flow.py "我想系统学习高中语文" --no-input
```

## 文档

- **使用说明（流程 / 产物路径 / 参数 / 调试 / 生产）**：[`TeachingFlow/docs/README.md`](TeachingFlow/docs/README.md)
- **设计契约与取舍（含写死清单、口径）**：[`TeachingFlow/CHAIN.md`](TeachingFlow/CHAIN.md)

## 目录

```
TeachingFlow/       一句话到课件的编排器（入口 flow.py）
├── core/           范围/强度/周期解析、蓝图、门控、落盘、LLM、命名
├── steps/          S0 S0a S0b S1 S2 S3 S4a S4 S5
├── promptpack/     自有模板（模块判定 / 周期估算 / 页数决策 / 模块切分）
├── fixtures/       --mock 用的假 LLM 回复
├── tools/          smoke_cases / prompt_lint / backfill_llm_returns / package_release
└── docs/README.md  使用文档
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

## 自检（不花 token）

```bash
cd TeachingFlow
python tools/smoke_cases.py     # 意图判定 16 条
python tools/prompt_lint.py     # 模板里不许出现示例页数
python flow.py "…" --mock --to plan
```

`ppt-master/` 需要单独拉取（setup.ps1 会做），也可设环境变量 `PPT_MASTER_SKILL_DIR` 指向已有副本。
