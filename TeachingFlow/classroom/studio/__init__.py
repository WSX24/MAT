# -*- coding: utf-8 -*-
"""引擎适配层：把既有的课堂引擎（deck 解析 / 备课 / 状态机）包成 API 层能用的纯函数。

四个模块各管一段，都不做 HTTP、不碰数据库:

- catalog.py     扫 runs/ 的交付件 → 课程 / 章 / 单元（只读盘）
- materialize.py 一份 pptx → decks/<deck_id>/{deck.json,plan.json}（复用预处理作业）
- outline.py     decks/<deck_id>/ → 「这单元讲什么」的大纲（只读产物）
- runner.py      会话运行时（W1）
"""
