"""课堂交互原型 —— 启动脚本。

用法：
    python run.py            一键启动：起课堂 + 写出 MCP 接入提示词 + 开浏览器
    python run.py serve --no-browser   只起服务，不开浏览器
    python run.py mcp-stdio [endpoint] 内置 stdio↔HTTP 桥接（给只认 stdio 的宿主）
    python run.py audit      只做脚本体检，不启动服务
    python run.py test       跑全部离线测试
    python run.py warmup     预合成全部讲解音频（让演示更顺）
    python run.py check      检查服务商连通性
    python run.py serve --env env.broken      换一份配置启动（复现故障/对照实验）

真实 PPT 接入（详见 README §9）：
    python run.py pptx "<课件.pptx>" [--max-slides N] [--no-download]
                                    解析 pptx → decks/<deck_id>/（含 media/）
    python run.py decks             列出已解析的课件
    python run.py plan <deck_id> [--slides 1-6] [--rule]
                                    生成课堂脚本 plan.json（默认让模型备课）
    python run.py audit <deck_id>   按真实课件体检（锚点命中率 / 长度 / 操作合法性）
    python run.py variants <deck_id> [--dry-run]
                                    只给已有 plan.json 补变体（**已停产**，仅为老流程保留）
    python run.py routes <deck_id> [--dry-run] [--no-polish]
                                    把已有 plan.json 迁到新格式：顶层写 routes、
                                    删 units[*].variants（正文/锚点/操作一格不动）

`--env` 改的是环境变量 `DSH_ENV_FILE`，**必须在 import config 之前设好** ——
所以 config 只能在函数内部惰性导入，模块顶部不能出现 `from config import ...`。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

#: 项目根目录（本文件所在目录）—— 写接入提示词、找 preview/static 都用它
ROOT = Path(__file__).resolve().parent


def cmd_audit(argv: list[str]) -> int:
    from config import load_settings

    s = load_settings()
    deck_id = argv[0] if argv else s.deck_id
    if deck_id:
        from deck.store import load_deck, plan_json
        from script_gen import LessonPlan, audit_plan

        deck = load_deck(deck_id)
        pj = plan_json(deck_id)
        if not pj.exists():
            print(f"[audit] {deck_id} 还没有 plan.json，先跑 `python run.py plan {deck_id}`")
            return 2
        plan = LessonPlan.load(pj)
        for line in audit_plan(deck, plan, s.unit_char_warn):
            print(line)
        return 0

    from eval import audit_script
    from script import build_script

    for line in audit_script(build_script(), s.unit_char_warn):
        print(line)
    return 0


def cmd_test() -> int:
    sys.path.insert(0, ".")
    from tests.test_config import main as config_main
    from tests.test_deck import main as deck_main
    from tests.test_machine import _main
    from tests.test_mcp import main as mcp_main
    from tests.test_ops import main as ops_main
    from tests.test_protocol import main as protocol_main
    from tests.test_sanitize import main as sanitize_main
    from tests.test_script_gen import main as plan_main
    from tests.test_thinking import main as thinking_main
    from tests.test_tts import main as tts_main

    rc = asyncio.run(_main())
    rc |= config_main()
    rc |= sanitize_main()
    rc |= protocol_main()
    rc |= tts_main()
    rc |= thinking_main()
    rc |= deck_main()
    rc |= ops_main()
    rc |= plan_main()
    rc |= mcp_main()
    return rc


def cmd_pptx(argv: list[str]) -> int:
    """解析真实 pptx → decks/<deck_id>/（deck.json + media/）。"""
    if not argv:
        print("用法：python run.py pptx \"<课件.pptx>\" [--max-slides N] [--no-download]")
        return 2
    src = argv[0]
    max_slides = 0
    download = "--no-download" not in argv
    if "--max-slides" in argv:
        i = argv.index("--max-slides")
        try:
            max_slides = int(argv[i + 1])
        except (IndexError, ValueError):
            print("--max-slides 需要一个整数")
            return 2
    from deck.pptx_ingest import ingest_pptx
    from deck.schema import deck_overview
    from preprocess import explain_ingest_error

    try:
        deck = ingest_pptx(src, max_slides=max_slides or None, download_external=download)
    except Exception as exc:
        # 坏文件以前是把 BadZipFile 的 traceback 甩到终端：命令行用户看到的是
        # "File is not a zip file" 加十几行栈，既不知道哪份文件坏了，也不知道该干什么。
        # 这里只说人话并返回 2（与参数错同一个码：这次调用什么也没产出）。
        print(f"[pptx] ❌ {explain_ingest_error(src, exc)}")
        return 2
    print(deck_overview(deck, limit=12))
    print(f"\n[pptx] 已解析 → decks/{deck.deck_id}/deck.json"
          f"（元素 {deck.stats.get('elements', '?')} · 媒体 {len(deck.media)}"
          f" · 告警 {len(deck.warnings)}）")
    for w in deck.warnings[:8]:
        print(f"   ⚠️  {w}")
    print(f"\n[pptx] 下一步：python run.py plan {deck.deck_id}")
    return 0


def cmd_decks() -> int:
    from deck.store import list_decks

    rows = list_decks()
    if not rows:
        print("[decks] 还没有解析过任何课件。用法：python run.py pptx \"<课件.pptx>\"")
        return 0
    for r in rows:
        if "error" in r:
            print(f"  ❌ {r['deck_id']}: {r['error']}")
            continue
        print(f"  {r['deck_id']}  《{r['title']}》 {r['slides']} 页 "
              f"{r['width_px']}×{r['height_px']} · 媒体 {r['media']} · "
              f"脚本 {'有' if r['has_plan'] else '无'} · 告警 {r['warnings']}")
    return 0


def _parse_slides_spec(spec: str | None, total: int) -> list[int] | None:
    """`--slides 1-6,9` → [0,1,2,3,4,5,8]（页号 1 起算，对外友好）。"""
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            out.extend(range(max(1, lo), min(total, hi) + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                out.append(n)
    return sorted({n - 1 for n in out}) or None


def _save_plan(path, plan, routes=None) -> None:
    """写 plan.json：顶层带 routes，且不再写空的 variants / summary。

    为什么不能直接 plan.save()：LessonPlan 是个 dataclass，asdict 出来的形状**只由字段决定** ——
    routes 不是它的字段（那份契约属于 plan.json 的顶层，不属于 LessonPlan），
    所以带 routes 的那一版必须在这里拼。反过来，空的 variants 也在这里去掉：
    就地变体已停产，留一个恒为 [] 的键只会让人以为它还在产出。

    空的 summary 同理去掉，但理由不同：空 summary 是**回填的信号**（"这段还没补过"）。
    留着 `"summary": ""` 就等于宣称"补过了，是空的"，下次 summarize 会把它当成已完成，
    而重写旧计划（--fix-ops / variants）还会让每个单元凭空多出一个空键 ——
    文件里看不出来，回填却不干活了。

    routes=None 表示"这份计划根本不谈 routes"（例如老的 fix-ops 就地清操作）：
    那种情况保持文件原样，不去凭空造一个 routes 键 —— 有键 = 新计划（不再补挂变体），
    没键 = 旧计划（沿用补挂），这两档的行为不一样，不能让一个空数组把旧计划悄悄升级。
    """
    data = json.loads(plan.to_json())
    for unit in data.get("units") or []:
        if not unit.get("variants"):
            unit.pop("variants", None)
        if not str(unit.get("summary") or "").strip():
            unit.pop("summary", None)
    if routes is not None:
        data["routes"] = list(routes)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _routes_brief(routes) -> str:
    """一行摘要：skip N 条（省时间）/ revisit M 条（多花时间）—— 落盘后人工审核先看这个。"""
    if not routes:
        return "这一份没有可预置的岔路"
    skip = [r for r in routes if r.kind == "skip"]
    revisit = [r for r in routes if r.kind == "revisit"]
    parts = []
    if skip:
        parts.append(f"skip {len(skip)} 条（共省 {abs(sum(r.est_sec for r in skip)):.0f} 秒）")
    if revisit:
        parts.append(f"revisit {len(revisit)} 条（共多花 {sum(r.est_sec for r in revisit):.0f} 秒）")
    pages = sorted({r.fork_node for r in routes})
    return "，".join(parts) + f"，分布在 {len(pages)} 个岔出点上"


def _struct_key(kind: str, fork, merge, unit_ids) -> tuple:
    """一条路线的**结构**判等键：走向 + 岔出 + 汇回 + 要重讲的格。

    刻意**不含文案与时长**：润色改的正是这两样（见 agent/routes._polish 会按新文案重算
    est_sec），把它们放进键里，每一次重跑都会被判成"变了"，幂等当场失效 ——
    而幂等正是迁移命令的硬契约。
    """
    try:
        fork_i, merge_i = int(fork or 0), int(merge or 0)
    except (TypeError, ValueError):
        fork_i, merge_i = 0, 0
    return (str(kind or ""), fork_i, merge_i, tuple(str(u or "") for u in unit_ids))


def _entry_struct_key(entry: dict) -> tuple:
    """plan.json 里一条 routes 的结构键（读盘那一侧）。"""
    steps = entry.get("steps") if isinstance(entry.get("steps"), list) else []
    return _struct_key(entry.get("kind"), entry.get("fork_node"), entry.get("merge_node"),
                       [s.get("unit_id") for s in steps if isinstance(s, dict)])


def _route_struct_key(route) -> tuple:
    """内存里一条 Route 的结构键（算出来那一侧）。"""
    return _struct_key(route.kind, route.fork_node, route.merge_node,
                       [s.unit_id for s in route.steps])


def _carry_over_text(routes, old_entries) -> list[str]:
    """把已有 routes 里**结构相同**那几条的文案搬回新算出来的路线上（返回搬过的 route_id）。

    为什么必须搬：迁移命令的硬契约是"重复跑两次结果一致"，而文案可能来自一次模型润色
    （同一条提示词问两次不会逐字相同）。搬过之后第二次跑既不漂、也不必再花一次额度。

    est_sec 跟着文案走（它是"念这段字要多久"）：沿用旧文案就必须沿用旧时长，否则 J
    拿一个不存在的数去比"划不划算"（与 agent/routes._polish 同一个口径）。
    """
    old: dict[tuple, dict] = {}
    for entry in old_entries or []:
        if isinstance(entry, dict):
            old.setdefault(_entry_struct_key(entry), entry)
    carried: list[str] = []
    for route in routes:
        entry = old.get(_route_struct_key(route))
        if entry is None:
            continue
        reason = " ".join(str(entry.get("reason") or "").split())
        if reason:
            route.reason = reason
        steps = {str(s.get("unit_id") or ""): s
                 for s in (entry.get("steps") or []) if isinstance(s, dict)}
        for step in route.steps:
            src = steps.get(step.unit_id)
            if src is None:
                continue
            label = " ".join(str(src.get("label") or "").split())
            text = " ".join(str(src.get("text") or "").split())
            if label:
                step.label = label
            if text:
                step.text = text
                sec = src.get("est_sec")
                step.est_sec = float(sec) if isinstance(sec, (int, float)) else step.est_sec
        if route.kind == "revisit":
            route.est_sec = round(sum(s.est_sec for s in route.steps), 1)
        carried.append(route.route_id)
    return carried


def _route_polisher(settings, tag: str = "plan"):
    """把异步 LLM 包成 plan_routes 要的**同步**可调用（备课是同步流程，这里没有事件循环）。

    拿不到模型就返回 None（= 只用规则文案）：润色是加分项，绝不该让备课失败。
    tag 只决定警告行的前缀（plan / routes），两条命令共用同一份降级逻辑。
    """
    try:
        from llm import LLM

        llm = LLM(settings)
    except Exception as exc:
        print(f"[{tag}] ⚠️ 路线文案润色不可用（{type(exc).__name__}: {exc}），改用规则文案")
        return None

    def _polish(prompt: str) -> str:
        return asyncio.run(llm.chat(
            [{"role": "system", "content": "你是备课助教，只输出 JSON。"},
             {"role": "user", "content": prompt}],
            max_tokens=1200, json_mode=True))

    return _polish


def cmd_plan(argv: list[str]) -> int:
    """生成课堂脚本（讲解单元）→ decks/<deck_id>/plan.json。"""
    from config import load_settings

    s = load_settings()
    if not argv:
        print("用法：python run.py plan <deck_id|课件.pptx> [--slides 1-6] [--rule]"
              " [--dry-run] [--fix-ops]\n"
              "  --rule      不调模型，用规则把课件文字切成单元（快，质量低，用于兜底）\n"
              "  --slides    只备这几页（**与已有计划合并**，不会覆盖其它页）\n"
              "  --dry-run   只体检、不写盘（试提示词/试模型时用）\n"
              "  --fix-ops   不重新备课，只把计划里的空操作清掉（不调模型）")
        return 2
    target = argv[0]
    rule_only = "--rule" in argv
    fix_ops = "--fix-ops" in argv
    dry_run = "--dry-run" in argv
    spec = None
    if "--slides" in argv:
        i = argv.index("--slides")
        spec = argv[i + 1] if i + 1 < len(argv) else None

    from pathlib import Path

    from deck.store import load_deck, make_deck_id, plan_json, save_deck

    if fix_ops:
        # 只做"清理"，不重新备课：把计划里那些空操作（没有前置状态可复原的
        # reset_zoom/unfocus）去掉，然后重新体检。不调模型，几秒钟就完。
        from script_gen import LessonPlan, audit_plan, measure_plan, prune_noop_ops

        p = plan_json(target)
        if not p.exists():
            print(f"[plan] 找不到 {p} —— 先跑 python run.py plan {target}")
            return 2
        deck = load_deck(target)
        raw_doc = json.loads(p.read_text(encoding="utf-8"))
        plan = LessonPlan.from_dict(raw_doc)
        # 只清空操作（正文与格号一个都没动）→ 已经备好的 routes 依然有效，原样写回。
        # 直接 plan.save() 会把它们抹掉：LessonPlan 里根本没有 routes 这个键。
        kept_routes = raw_doc.get("routes") if isinstance(raw_doc.get("routes"), list) else None
        dropped = prune_noop_ops(plan)
        # 体检里的"课件操作 N 个"读的是 plan.coverage —— 不管有没有删东西都重算，
        # 否则删完还显示删前的数字（踩过：删了 3 个还显示 163 个）
        plan.coverage = measure_plan(deck, plan)
        if not dropped:
            _save_plan(p, plan, kept_routes)
            print("[plan] 计划里没有空的复原类操作，无需修改"
                  "（体检数字已重算）")
            return 0
        _save_plan(p, plan, kept_routes)
        print(f"[plan] 去掉 {len(dropped)} 个空操作：")
        for d in dropped[:12]:
            print("  - " + d)
        print()
        for line in audit_plan(deck, plan, s.unit_char_warn):
            print(line)
        print(f"\n[plan] 已更新 → {p}")
        return 0

    if Path(target).is_file():
        from deck.pptx_ingest import ingest_pptx

        deck = ingest_pptx(target, deck_id=make_deck_id(target))
        save_deck(deck)
    else:
        deck = load_deck(target)
    slides = _parse_slides_spec(spec, deck.slide_count)
    print(f"[plan] 课件《{deck.title}》{deck.slide_count} 页"
          + (f"，本次只备第 {[i + 1 for i in slides]} 页" if slides else "，全量备课"))
    # 备课可能要跑几十分钟，写到哪里必须**先说**。
    # 课件内部 id 才是真身：复制目录做实验时，这里会写回原课件的目录（踩过）
    if target != deck.deck_id:
        print(f"[plan] ⚠️ 你给的是 {target}，但课件 id 是 {deck.deck_id} —— "
              f"计划写到 decks/{deck.deck_id}/plan.json")

    from script_gen import audit_plan, measure_plan, plan_llm, plan_rule

    if rule_only:
        plan = plan_rule(deck, slides=slides)
    else:
        from llm import LLM

        llm = LLM(s)

        def _progress(n, total, slide):
            print(f"   [{n}/{total}] 第 {slide.index + 1} 页 {slide.title or ''}"
                  f"（{len(slide.text_flat)} 字）…", flush=True)

        plan = asyncio.run(plan_llm(deck, llm, slides=slides, on_progress=_progress))
        if not plan.units:
            print("[plan] ❌ 模型没有产出任何单元 —— 检查模型服务后重试，或先用 --rule 兜底")
            return 1

    if dry_run:
        # 试提示词/试模型时只想看结果，不想动已经备好的那节课
        print()
        for line in audit_plan(deck, plan, s.unit_char_warn):
            print(line)
        print("\n[plan] --dry-run：只体检，**没有写盘**")
        return 0

    path = plan_json(deck.deck_id)
    if slides and path.exists():
        # 只备几页时**不能**把整份计划覆盖掉：以前 `--slides 4-4` 会把 42 页的计划
        # 写成只有第 4 页的版本（试一页 = 毁一节课）。现在改成合并：
        # 本次备过的页换成新单元，其余页原样保留。
        from script_gen import LessonPlan

        old = LessonPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))
        keep = [u for u in old.units if u.slide not in set(slides)]
        plan.units = sorted(keep + plan.units, key=lambda u: u.slide)
        plan.warnings.append(
            f"部分备课（第 {[i + 1 for i in slides]} 页）：其余 {len(keep)} 个单元沿用旧计划")
        plan.coverage = measure_plan(deck, plan)
        print(f"[plan] 合并旧计划：保留 {len(keep)} 个单元，"
              f"替换第 {[i + 1 for i in slides]} 页")

    # 就地变体（v1 换说法 / v2 举例子）**已停产**：它与"学生没懂时状态机实时调模型重讲"
    # 冲突，而且按产品口径它从来就不该算分支。备课期现在产出的是**预置路线分支**
    # （plan.json 顶层的 routes）：fork/merge/steps 由规则算（确定性、可测），
    # reason 与 steps 文案让模型润色一次并落盘可审，模型不可用时降级为规则文案。
    # 放在合并之后：`--slides 4-4` 合并进来的旧单元同样会落在这些岔路的格号里。
    from agent.routes import plan_routes, route_entry

    routes = plan_routes(plan, deck, llm=None if rule_only else _route_polisher(s),
                         char_warn=s.unit_char_warn)
    entries = [route_entry(r) for r in routes]
    print(f"[plan] 预置路线：{len(routes)} 条（{_routes_brief(routes)}）")
    for w in plan.warnings:
        if w.startswith("⚠️ [routes]"):
            print(f"[plan]   {w}")

    _save_plan(path, plan, entries)
    print()
    for line in audit_plan(deck, plan, s.unit_char_warn):
        print(line)
    print(f"\n[plan] 已保存 → {path}")
    print(f"[plan] 上课：DECK_ID={deck.deck_id} python run.py    （或写进 .env）")
    return 0


def cmd_variants(argv: list[str]) -> int:
    """把**已有** plan.json 就地补齐变体（不调模型、不重新备课）。

    为什么要有这条命令：`plan` 会重新备课（调模型、要花钱、几十分钟），而变体是
    确定性规则模板（`agent/variants.py`）。已经备好的那节课只差 variants 这一层时，
    没有任何理由再花一次钱重备。所以这条命令只做加法：给没有变体的单元补上，
    已有的（将来 LLM 生成的那批）原样保留。
    """
    from config import load_settings

    s = load_settings()
    if not argv:
        print("用法：python run.py variants <deck_id> [--dry-run]\n"
              "  --dry-run   只统计，不写盘")
        return 2
    # 停产声明先打：这条命令现在只服务老流程（行为保持不变，免得老脚本跑不动）
    print("[variants] 已停产：就地变体与实时重讲冲突，改由 routes 承载分支")
    deck_id = argv[0]
    dry_run = "--dry-run" in argv

    from agent.variants import attach_variants
    from deck.store import load_deck, plan_json
    from script_gen import LessonPlan

    deck = load_deck(deck_id)
    p = plan_json(deck.deck_id)
    if not p.exists():
        print(f"[variants] 找不到 {p} —— 先跑 python run.py plan {deck.deck_id}")
        return 2
    raw = json.loads(p.read_text(encoding="utf-8"))
    plan = LessonPlan.from_dict(raw)
    # 迁移过的计划（顶层有 routes）一旦被 plan.save() 写回，routes 键会被**悄悄删掉**
    # —— LessonPlan 里没有这个字段。那等于把新格式退回旧格式，下次加载又开始补挂变体。
    # 所以这里和 --fix-ops 用同一个手法：原样带上已有 routes。
    kept_routes = raw.get("routes") if isinstance(raw.get("routes"), list) else None
    if kept_routes is not None:
        print(f"[variants] ⚠️ 这份计划已有 routes（{len(kept_routes)} 条预置路线），"
              "补回来的变体与已停产的口径冲突，routes 会原样保留")
    # 与 course.py 的补挂同一个口径：已有的变体一律保留，只补缺的
    keep = {id(u): list(u.variants) for u in plan.units if u.variants}
    attach_variants(plan, deck, char_warn=s.unit_char_warn)
    for u in plan.units:
        if keep.get(id(u)):
            u.variants = keep[id(u)]

    covered = sum(1 for u in plan.units if u.variants)
    total = sum(len(u.variants) for u in plan.units)
    print(f"[variants] 课件《{deck.title}》{deck.slide_count} 页 / {len(plan.units)} 个单元"
          f" → {total} 个变体，覆盖 {covered} 个单元")
    if covered < len(plan.units):
        print(f"[variants] 另 {len(plan.units) - covered} 个单元文本过短，造不出变体"
              "（课上就按 v0 讲，不影响）")
    if dry_run:
        print("[variants] --dry-run：只统计，**没有写盘**")
        return 0
    _save_plan(p, plan, kept_routes)     # 不用 plan.save()：它会把 routes 键抹掉
    print(f"[variants] 已写回 → {p}")
    return 0


def cmd_routes(argv: list[str]) -> int:
    """把**已有** plan.json 迁到新格式：顶层写 routes、删 units[*].variants。

    为什么需要这条命令
    ------------------
    旧格式（没有 routes 键）在加载时会被 course.py 补挂 N×2 条就地变体 —— 产品口径上
    变体已停产，但"停产"只停在代码里：打开 /graph 仍能看到 354 个「2 支」角标。
    而 plan.json 是花钱备出来的（LLM 备课几十分钟），不能为了换个格式再备一次。

    所以这条命令**只动两处**：
      · 顶层写 routes（agent/routes.py::plan_routes 算出来的预置路线，带 when）
      · 删 units[*].variants
    units 的正文/锚点/课件操作一格都不碰 —— 它们是这份数据最贵的部分，也是"迁移"
    与"重备"的分界线（下面会把 units 数 / 总字数 / 预估时长再打一遍，肉眼即可对账）。

    幂等
    ----
    route_id 由结构派生（director/route.derive_route_id），同一条走向反复算得到同一个名字；
    已有 routes 里结构相同的几条会沿用旧文案（含润色），于是第二次跑逐字一致、也不再花
    一次模型额度。--no-polish = 一律用规则文案重写（会丢掉现有润色）。

    回退
    ----
    第一次真正写盘前把原文件备份成 plan.json.bak-routes；恢复 = 把它复制回 plan.json
    （备份已存在时不覆盖：回退点必须是最早那份迁移前数据）。
    """
    from config import load_settings

    s = load_settings()
    if not argv:
        print("用法：python run.py routes <deck_id> [--dry-run] [--no-polish]\n"
              "  --dry-run    只统计，不写盘（也不建备份）\n"
              "  --no-polish  不调模型润色文案（确定性、可离线复现）")
        return 2
    target = argv[0]
    dry_run = "--dry-run" in argv
    polish = "--no-polish" not in argv

    from agent.routes import plan_routes, route_entry
    from deck.store import load_deck, plan_json
    from script_gen import LessonPlan, build_lesson_script

    try:
        deck = load_deck(target)
    except FileNotFoundError as exc:        # 课件没解析过：说清楚下一步，不要甩 traceback
        print(f"[routes] {exc}")
        return 2
    path = plan_json(deck.deck_id)
    if not path.exists():
        print(f"[routes] 找不到 {path} —— 先跑 python run.py plan {deck.deck_id}")
        return 2
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("units"), list):
        print(f"[routes] {path} 不是一份 plan.json（顶层缺 units），不处理")
        return 2
    if target != deck.deck_id:
        print(f"[routes] ⚠️ 你给的是 {target}，但课件 id 是 {deck.deck_id} —— "
              f"读写 decks/{deck.deck_id}/plan.json")

    plan = LessonPlan.from_dict(raw)
    script = build_lesson_script(deck, plan)
    total = len(script.units)
    old_entries = raw.get("routes") if isinstance(raw.get("routes"), list) else []
    old_ids = [str(e.get("route_id") or "") for e in old_entries if isinstance(e, dict)]

    # ① 结构规则算一遍（llm=None：这一步只拿结构与规则文案，确定性、可离线）
    routes = plan_routes(plan, deck, llm=None, char_warn=s.unit_char_warn)
    # ② 已有 routes 里结构相同的那几条沿用旧文案（幂等 + 不重复花额度）
    carried = _carry_over_text(routes, old_entries)
    pending = [r.route_id for r in routes if r.route_id not in set(carried)]
    # ③ 只有新出现的走向才值得让模型润色一次（多花的是钱，少花的是稳定性）
    if polish and pending:
        polisher = _route_polisher(s, "routes")
        if polisher is not None:
            polished = {r.route_id: r for r in plan_routes(
                plan, deck, llm=polisher, char_warn=s.unit_char_warn)}
            routes = [polished.get(r.route_id, r) if r.route_id in set(pending) else r
                      for r in routes]

    entries = [route_entry(r) for r in routes]
    for route in routes:                    # 结构自检：坏路线写进盘里只会让课堂退回 v0
        route.validate(total)
    new_ids = [str(e["route_id"]) for e in entries]

    # -- 统计（人工审核先看这几行）-------------------------------------------
    print(f"[routes] 课件《{deck.title}》{deck.slide_count} 页 / {total} 格")
    print(f"[routes] 预置 {len(routes)} 条：{_routes_brief(routes)}")
    pages = sorted({script.units[r.fork_node].slide + 1 for r in routes if 0 <= r.fork_node < total})
    print(f"[routes] 岔出页（页号 1 起）：{pages if pages else '无'}")
    for w in plan.warnings:
        if w.startswith("⚠️ [routes]"):
            print(f"[routes]   {w}")
    chars = sum(len(u.text or "") for u in plan.units)
    est = round(sum(u.est_sec() for u in plan.units), 1)
    print(f"[routes] 正文未动：{len(plan.units)} 个单元 / {chars} 字 / 预估 {est} 秒"
          "（迁移前后必须一致）")

    # -- 删 units[*].variants -------------------------------------------------
    touched = dropped = 0
    for unit in raw["units"]:
        if isinstance(unit, dict) and "variants" in unit:
            touched += 1
            dropped += len(unit.get("variants") or [])
            unit.pop("variants", None)
    if touched:
        print(f"[routes] 删除 units[*].variants：{touched} 个单元 / {dropped} 条变体")
    else:
        print("[routes] units[*].variants：磁盘上本来就没有（旧格式那批是 course.py "
              "加载时补挂的；写入 routes 键之后不再补挂）")

    # -- 与文件中已有 routes 对账（幂等的证据）--------------------------------
    added = [i for i in new_ids if i not in set(old_ids)]
    gone = [i for i in old_ids if i not in set(new_ids)]
    kept = [i for i in new_ids if i in set(old_ids)]
    print(f"[routes] 与已有 routes 比较：新增 {len(added)} 条 / 保持 {len(kept)} 条 / "
          f"消失 {len(gone)} 条（沿用旧文案 {len(carried)} 条）")

    unchanged = entries == old_entries and not touched
    if dry_run:
        print("[routes] --dry-run：只统计，**没有写盘**")
        return 0
    if unchanged:
        print("[routes] 结果与文件中已有 routes 逐字一致（幂等），不写盘")
        return 0

    raw["routes"] = entries
    backup = path.with_name(path.name + ".bak-routes")
    if backup.exists():
        print(f"[routes] 备份已存在（保留最早那份迁移前数据）：{backup.name}")
    else:
        backup.write_bytes(path.read_bytes())
        print(f"[routes] 备份 → {backup.name}（回退：把它复制回 plan.json）")
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[routes] 已写回 → {path}")
    print(f"[routes] 上课：DECK_ID={deck.deck_id} python run.py")
    return 0


def cmd_summarize(argv: list[str]) -> int:
    """给**已有** plan.json 补走向图卡面上那句话总结（正文一格都不动）。

    为什么要有这条命令：`plan` 会重新备课（调模型、花钱、几十分钟），而
    "走向图的一格要一句话"是后来才有的需求 —— 已经备好的那节课只差这一层时，
    没有任何理由再花一次钱重备。这条命令只写 `units[*].summary`：
    正文 / 锚点 / 课件操作一个字都不碰，所以对一份上过课的定稿计划也是安全的。

    --force 连已有的也重写：实现上就是把 summary 清空再交给回填 ——
    "补哪些"因此只有**一个**判据（空的才补），不会出现两套口径各自为政。
    --dry-run 只数待补的单元数：**不调模型、不写盘**（试命令行参数时不烧额度）。
    """
    from config import load_settings

    s = load_settings()
    if not argv:
        print("用法：python run.py summarize <deck_id> [--slides 1-6] [--force] [--dry-run]\n"
              "  --slides    只补这几页（页号 1 起算）\n"
              "  --force     连已有的总结一起重写（默认只补空的）\n"
              "  --dry-run   只统计待补单元数，不调模型、不写盘")
        return 2
    target = argv[0]
    force = "--force" in argv
    dry_run = "--dry-run" in argv
    spec = None
    if "--slides" in argv:
        i = argv.index("--slides")
        spec = argv[i + 1] if i + 1 < len(argv) else None

    from deck.store import load_deck, plan_json
    from script_gen import LessonPlan

    try:
        deck = load_deck(target)
    except FileNotFoundError as exc:        # 课件没解析过：说清楚下一步，不要甩 traceback
        print(f"[summarize] {exc}")
        return 2
    path = plan_json(deck.deck_id)
    if not path.exists():
        print(f"[summarize] 找不到 {path} —— 先跑 python run.py plan {deck.deck_id}")
        return 2
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("units"), list):
        print(f"[summarize] {path} 不是一份 plan.json（顶层缺 units），不处理")
        return 2
    plan = LessonPlan.from_dict(raw)
    slides = _parse_slides_spec(spec, deck.slide_count)
    keep = set(slides) if slides is not None else None
    if force:
        for u in plan.units:
            if keep is None or u.slide in keep:
                u.summary = ""
    todo = [u for u in plan.units if (keep is None or u.slide in keep)
            and not str(u.summary or "").strip()]
    print(f"[summarize] 课件《{deck.title}》{deck.slide_count} 页 / {len(plan.units)} 个单元"
          + (f"，只看第 {[i + 1 for i in slides]} 页" if slides else "")
          + f" → 待补 {len(todo)} 个"
          + ("（--force：已有的也重写）" if force else "（已有总结的单元原样保留）"))
    if dry_run:
        print("[summarize] --dry-run：只统计，**没有调模型、没有写盘**")
        return 0
    if not todo:
        print("[summarize] 没有待补的单元，不写盘")
        return 0

    from llm import LLM
    from script_gen import backfill_summaries

    def _progress(n, total, slide):
        print(f"   [{n}/{total}] 第 {slide.index + 1} 页 {slide.title or ''}…", flush=True)

    res = asyncio.run(backfill_summaries(deck, plan, LLM(s), slides=slides,
                                        on_progress=_progress))
    for w in res["warnings"]:
        print(f"[summarize]   {w}")
    print(f"[summarize] 补上 {res['updated']} 个单元 / 跳过 {res['skipped']} 个")
    # 不用 plan.save()：它会把顶层 routes 键抹掉（与 variants / --fix-ops 同一个陷阱）
    _save_plan(path, plan, raw.get("routes") if isinstance(raw.get("routes"), list) else None)
    print(f"[summarize] 已写回 → {path}")
    print("[summarize] 看走向图：python run.py start，然后打开 /graph")
    return 0


def cmd_warmup(argv: list[str]) -> int:
    from speech import TTS

    async def _go() -> int:
        from course import load_course, print_course_notes
        from config import load_settings

        s = load_settings()
        tts = TTS(s.tts_enabled, s.tts_voice, s.tts_rate, s.cache_dir)
        if not tts.available:
            print(f"[warmup] TTS 未启用：{tts.status()['reason']}")
            return 0
        course = load_course(s)
        print_course_notes(course)
        script = course.script
        # 讲解按句播放 —— 逐句预合成才真正省下运行时的等待
        from llm import split_sentences

        sents: list[str] = []
        seen: set[str] = set()
        for u in script.units:
            for x in split_sentences(u.text):
                if x not in seen:
                    seen.add(x)
                    sents.append(x)
        limit = int(argv[0]) if argv and argv[0].isdigit() else 0
        if limit:
            sents = sents[:limit]
        est_min = len(sents) * 3 / 60
        print(f"[warmup] {len(script.units)} 个单元 / {len(sents)} 句待合成"
              f"（edge-tts 约 {est_min:.0f} 分钟，可先 Ctrl+C 用已缓存的部分）")
        ok, total = await tts.warmup(sents)
        print(f"[warmup] 完成 {ok}/{total} 句 → {tts.dir}")
        return 0

    return asyncio.run(_go())


def cmd_check() -> int:
    from llm import LLM, Ctx, prompt_answer  # noqa: F401

    async def _go() -> int:
        from config import load_settings

        s = load_settings()
        print(f"[check] 服务商：{s.describe()}")
        # 要 key 的服务商 → 该去 .env 设哪个变量。写全变量名，方便直接抄。
        key_env = {
            "opencode": "OPENCODE_API_KEY",
            "opencode_go": "OPENCODE_GO_API_KEY（或复用的 OPENCODE_API_KEY）",
        }
        if s.provider in key_env and not s.active.api_key:
            print(f"[check] ❌ {s.provider} 缺少 API Key："
                  f"在 .env 里设置 {key_env[s.provider]}")
            return 2
        try:
            llm = LLM(s)
            out = await llm.chat([{"role": "user", "content": "用一句话说明什么是判别式。"}],
                                 max_tokens=60)
            print(f"[check] ✅ 连通。返回：{out[:120]}")
            return 0
        except Exception as e:
            from machine import _explain_llm_failure

            print("[check] ❌ 失败：\n" + _explain_llm_failure(e))
            return 1

    return asyncio.run(_go())


def cmd_serve(argv: list[str] | None = None) -> int:
    import uvicorn

    from config import load_settings

    argv = argv or []
    open_browser = "--no-browser" not in argv
    s = load_settings()

    # 一键启动：把「外置大脑接入提示词」写盘，方便直接交给别的 agent 使用
    access_path = ensure_access_files(s)

    print("=" * 66)
    print("  课堂交互原型 · 个人自学场景（段末让位）")
    print("=" * 66)
    print(f"  服务商    {s.describe()}")
    print(f"  语音      {'edge-tts ' + s.tts_voice if s.tts_enabled else '未启用（纯文字）'}")
    print(f"  探针窗口  {s.probe_window}s    重讲上限 {s.max_rephrase} 轮")
    if s.deck_id or s.pptx_file:
        print(f"  课件      {s.deck_id or s.pptx_file}")
    else:
        print("  课件      原型自带手写课件（设 DECK_ID / PPTX_FILE 可换成真实 PPT）")
    print(f"  地址      http://{s.host}:{s.port}")
    print(f"  MCP 外脑  http://{s.host}:{s.port}/mcp")
    if access_path is not None:
        print(f"  接入提示词 {access_path}")
        print("            （把它整份发给 Claude Code / Codex，对方会自己接好）")
    print("=" * 66)
    print("  Ctrl+C 停止课堂")
    print("=" * 66)

    if open_browser:
        _open_browser_when_ready(f"http://{s.host}:{s.port}")

    uvicorn.run("web:app", host=s.host, port=s.port, reload=False, log_level="warning")
    return 0


def _open_browser_when_ready(url: str, timeout: float = 60.0) -> None:
    """后台等端口就绪再打开浏览器 —— 开早了只会看到「无法访问」。"""
    import socket
    import threading
    import webbrowser

    host, _, port_text = url.partition("://")[2].partition(":")
    port = int(port_text.split("/")[0] or 80)

    def _worker() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.4)
            try:
                with socket.create_connection((host, port), timeout=0.6):
                    time.sleep(0.4)
                    webbrowser.open(url)
                    return
            except OSError:
                continue

    threading.Thread(target=_worker, name="open-browser", daemon=True).start()


def ensure_access_files(s) -> Path | None:
    """写出接入提示词与 MCP 配置片段（一键启动的产物，给别的 agent 直接用）。"""
    endpoint = f"http://{s.host}:{s.port}/mcp"
    prompt_path = ROOT / "mcp-access-prompt.md"
    config_path = ROOT / "mcp-config.json"
    try:
        from mcp_server import build_access_prompt

        prompt_path.write_text(build_access_prompt(endpoint), encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "classroom": {
                            "type": "http",
                            "url": endpoint,
                        },
                        # command 交给 PATH 上的 python，不写 sys.executable：这份配置是
                        # 给**外部 MCP 客户端照着敲**的，绝对解释器路径只对生成它的那台机器
                        # 有效（还经常指向临时原型 venv），换人换机器直接报"找不到文件"。
                        # args 里的 run.py 反过来保留绝对路径：MCP 客户端是在**自己的**
                        # cwd 下 spawn 这条命令的，写相对路径必然找不到脚本。
                        # （mcp-stdio 只用到 urllib + config，不需要装课堂依赖，PATH 上的
                        #  python 就够跑。）
                        "classroom-stdio": {
                            "command": "python",
                            "args": [str(ROOT / "run.py"), "mcp-stdio", endpoint],
                        },
                    }
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # 写不出提示词不该拦着上课
        print(f"[start] ⚠ 接入提示词生成失败（不影响上课）：{exc}")
        return None
    return prompt_path



def cmd_mcp_stdio(argv: list[str]) -> int:
    """内置 stdio ↔ HTTP /mcp 桥接器（超时 65s > wait_seconds=50s，免装 Node/mcp-remote）。"""
    import urllib.request

    from config import load_settings

    s = load_settings()
    endpoint = argv[0] if argv else f"http://{s.host}:{s.port}/mcp"
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        req = urllib.request.Request(
            endpoint,
            data=line.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=65.0) as resp:
                if resp.status == 202:
                    continue
                body = resp.read().decode("utf-8", errors="replace").strip()
                if body:
                    sys.stdout.write(body + "\n")
                    sys.stdout.flush()
        except Exception as exc:
            try:
                msg = json.loads(line)
                req_id = msg.get("id")
            except Exception:
                req_id = None
            if req_id is not None:
                err = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": f"bridge error: {exc}"},
                }
                sys.stdout.write(json.dumps(err, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    return 0


def main() -> int:
    argv = sys.argv[1:]

    # `--env <文件>` 换一份配置（复现故障、对照实验）。
    # 必须是**本函数里的第一件事**：load_dotenv() 只在 config 首次 import 时跑一次。
    if "--env" in argv:
        i = argv.index("--env")
        if i + 1 >= len(argv):
            print("用法：run.py [命令] --env <配置文件>")
            return 2
        os.environ["DSH_ENV_FILE"] = argv[i + 1]
        del argv[i:i + 2]

    cmd = (argv[0] if argv else "serve").lower()
    rest = argv[1:]
    table = {
        "audit": lambda: cmd_audit(rest),
        "test": cmd_test,
        "warmup": lambda: cmd_warmup(rest),
        "check": cmd_check,
        "serve": lambda: cmd_serve(rest),
        "start": lambda: cmd_serve(rest),
        "pptx": lambda: cmd_pptx(rest),
        "decks": cmd_decks,
        "plan": lambda: cmd_plan(rest),
        "variants": lambda: cmd_variants(rest),
        "routes": lambda: cmd_routes(rest),
        "summarize": lambda: cmd_summarize(rest),
        "mcp-stdio": lambda: cmd_mcp_stdio(rest),
        "mcp-prompt": cmd_mcp_prompt,
    }
    fn = table.get(cmd, lambda: cmd_serve(rest))
    return fn()


def cmd_mcp_prompt() -> int:
    """只生成接入提示词与配置片段（不起服务），打印当前 HTTP MCP 端点信息。"""
    from config import load_settings
    from mcp_server import build_access_prompt

    s = load_settings()
    endpoint = f"http://{s.host}:{s.port}/mcp"
    path = ensure_access_files(s)
    print(f"[mcp] 端点：{endpoint}")
    if path is not None:
        print(f"[mcp] 接入提示词已写入：{path}")
    print("\n" + build_access_prompt(endpoint))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
