# 让 `tests` 成为**常规包**而不是命名空间包。
# 为什么需要：用例里大量写法是 `from tests.fixtures import make_pptx`；本目录没有
# __init__.py 时 `tests` 只能作为命名空间包解析，而命名空间包会输给 sys.path 里
# 任何一个真实存在的常规包（例如某些第三方库往 site-packages 里塞了一个顶层
# `tests/`），于是 collect 阶段 18 个文件一起报 No module named 'tests.fixtures'。
# 有 __init__.py 之后本目录作为常规包先命中，跑 pytest 不再依赖机器上装了什么。
