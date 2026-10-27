# pptx/ —— 课件源文件放这里，不入库

把要解析的 .pptx 课件投进本目录，两条路都从这里取源：

```bash
python run.py pptx pptx/<课件.pptx>   # 只解析 → decks/<deck_id>/deck.json（+ media/）
python run.py plan <deck_id>          # 再备课 → plan.json（换提示词重备也走这条）
```

> ⚠ `pptx` 子命令收的是**文件系统路径**，不是课件名：从 classroom 目录起跑就写
> `pptx/xxx.pptx`，给**任意路径**也行（例如 `D:\课件\x.pptx`）——
> 解析器从不复制原文件，只把它记进 `decks/<deck_id>/source.txt`。
> 只写课件名（如 `"词汇.pptx"`）会被当成相对当前目录的路径，找不到就报"文件不存在"。

网页端的 /preprocess 是同一件事的操作台（上传 / 解析 / 备课 / 补总结）。

本目录下的 .pptx 已在 MAT/.gitignore 中忽略（几十 MB 的源文件换一版就整份重来，不入库），
只提交这个 README 与 .gitkeep。
