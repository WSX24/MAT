"""sqlite 仓储层（设计文档 §3）。

db.py 连库与迁移、repo.py 全部读写语句、schema.sql 是结构的唯一出处。
本包不 import 任何引擎模块 —— 仓储层不认识"课堂"，只认识行。
"""
