# pptx/ —— 课件源文件放这里，不入库

把要解析的 .pptx 课件投进本目录，两条路都从这里取源：

```bash
python run.py pptx "<课件.pptx>"     # 只解析 → decks/<deck_id>/deck.json（+ media/）
python run.py plan <deck_id>          # 再备课 → plan.json（换提示词重备也走这条）
```

网页端的 /preprocess 是同一件事的操作台（上传 / 解析 / 备课 / 补总结）。
也可以把任意路径的 pptx 直接交给 `pptx` 子命令，解析器从不复制原文件。

本目录下的 .pptx 已在 MAT/.gitignore 中忽略（几十 MB 的源文件换一版就整份重来，不入库），
只提交这个 README 与 .gitkeep。
