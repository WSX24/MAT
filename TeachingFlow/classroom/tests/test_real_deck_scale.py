"""真实课件规模的端到端验证：42 页 / 177 单元跑一整节课。

为什么还要一条"规模"用例
------------------------
tests/fixtures 的 6 单元夹具在 3 页内就跑完了，而状态机的不少行为是**跨页累积**的：
页尾探针 → 自动翻页、seg_index 归零、_goto_slide 清 q_rounds、每页第一个单元的预取、
让位与归位的纪元累加……3 页跑通只证明"机制存在"，不证明"42 页跑一节不会在某一页悄悄
退化"。所以这里用 **decks/ 下那份真实课件**（已解析 + 已用 LLM 备过 plan.json）驱动
完整一节课。

口径变更（本棒：把真实课件迁到新格式）
--------------------------------------
就地变体（v1 换说法 / v2 举例子）**已停产**，分支改由备课期预置的**路线**承载
（plan.json 顶层 routes → LessonScript.routes，见 agent/routes.py）。真实课件已迁移：
它的 plan.json 现在有 routes 键、没有 units[*].variants，于是 course.py 里"旧格式才
在内存补挂变体"那条分支自然不再触发 —— 打开 /graph 也不会再看到 354 个「2 支」角标。

因此这条用例钉的三件事换了，但**用例本身没删**：
  ① 预置路线的数据口径：稀疏、每条过 Route.validate、fork/merge 落在 script.units 上、
     revisit 的岔出格就是要重讲的那一格；
  ② "变体停产"在真实数据上真的发生了：加载后的单元一个变体都没有（内存里也没有）；
  ③ 真实规模跑完整节课仍然收尾干净（42 次页尾探针、无悬挂 task、决策层零阻塞）。
学生说没懂的那几格不再可能换来"另一个讲法上台"—— 那是停产的定义，不是回归。

离线口径（不联网、不花额度）
--------------------------
LLM 换成 tests/test_machine.py 的 FakeLLM，播放是瞬时的；**唯一的真实等待是页尾探针窗口**，
这里压到 0.05s（177 单元 × 42 页 ≈ 3 秒跑完）。压小窗口只是让测试快，不改语义：
探针照旧发出 42 次、照旧全部超时。

decks/ 下的数据文件一个字节都不许动：进来先算 sha1，跑完再算一次，必须一致
（真实课件的 plan.json 是花钱备出来的，测试顺手改写它是最贵的一类事故）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings  # noqa: E402
from course import load_course  # noqa: E402
from deck.store import plan_json  # noqa: E402
from director.decider import Director, RuleDecider  # noqa: E402
from director.pool import CandidatePool  # noqa: E402
from machine import SpeechSeg  # noqa: E402
from tests.test_variant_e2e import Classroom  # noqa: E402

#: 注入「没懂」的单元个数。取 6 个而不是 177 个：要证明的是"跨页注入之后课堂照样跑完"，
#: 不是"每个单元都要被注入"（后者反而会把课堂节奏测成另一件事）。
WANT_TARGETS = 6

#: 一条 42 页的课在离线夹具下约 3 秒。这里给 120 秒的硬上界只为兜住"卡死"，
#: 不是性能指标 —— 写小了会在慢机器上假红，那是测试自己制造的不确定性。
TIMEOUT_S = 120.0

#: 预置路线的稀疏上界：177 格 × 2 条（skip/revisit 各一种可能）= 354 种结构，
#: 铺超过 10% 就不再是"稀疏"，而是把决策层淹掉（判据见 agent/routes.py 文件头）。
MAX_ROUTE_DENSITY = 0.10


def _real_course():
    """入库真实课件（**按固定 deck_id 选**，离线，不调模型）。

    不取 list_decks() 的第一份：decks/ 下多一份 import-xxxxxxx 就会换掉被测课件，
    规模断言（≥100 单元 / ≥20 页）会红在一份 4 页的试导入课件上。
    """
    from tests.fixtures import real_deck_id

    deck_id = real_deck_id()
    if deck_id:
        settings = dataclasses.replace(load_settings(), deck_id=deck_id, pptx_file="")
        course = load_course(settings)
        if course.mode == "deck" and course.script.units:
            return course
        pytest.skip(f"真实课件 {deck_id} 装配不出课堂（mode={course.mode}），跳过规模验证")
    pytest.skip("decks/ 下没有入库的真实课件（Unit_2_Sweet_Home… / 高一物理第一课…），"
                "跳过规模验证")


def _pick_targets(units, want: int) -> list[str]:
    """跨页挑注入点：等距取 want 页，每页取**页尾那个单元**。

    两个刻意的选择：
    - **等距**而不是取前 6 个：只有铺满整份课件，"跨页"才真的被覆盖（第 1 页和第 36 页之间
      隔着几十次翻页、几十次探针、几十次让位归位，也隔着几十次 q_rounds 的按页重置）。
    - **页尾单元**而不是每页第一个：页尾单元是"学生刚说没懂"与"探针超时写弱信号"撞在一起
      的地方（探针就在它后面发出），也是"决策点必须排在页尾探针之前"的那个边界。挑最难的
      那一类单元，才顺带把"强信号不许被弱信号抹掉"在真实规模上验一遍。

    这里不再按"有没有变体"挑（停产之后一个都没有）：注入的目的是跨页压一遍课堂，
    而不是给变体链找落点。
    """
    by_page = {u.slide: u for u in units if u.is_page_end}
    pages = sorted(by_page)
    step = max(1, len(pages) // want)
    return [by_page[p].unit_id for p in pages[::step][:want]]


def _assert_preset_routes_are_real(script, plan_doc: dict) -> list:
    """预置路线的四条结构要求（都在 plan.json → LessonScript 这条通道上验）。"""
    units = script.units
    total = len(units)
    routes = script.routes

    assert routes, "真实课件迁移后必须有预置路线（plan.json 顶层的 routes）"
    assert len(routes) / (total * 2) <= MAX_ROUTE_DENSITY, \
        f"预置路线必须稀疏：{len(routes)} 条 / {total * 2} 种可能"
    unit_ids = {u.unit_id for u in units}
    for route in routes:
        assert route.route_id.startswith("p-"), \
            f"预置路线的 id 必须带 p- 前缀（与运行期 C 造的 r-… 区分）：{route.route_id}"
        route.validate(total)                       # 越界/结构错在这里当场炸
        assert route.kind in ("skip", "revisit"), f"备课期不预置 {route.kind!r} 走向"
        assert 0 <= route.fork_node < route.merge_node <= total
        if route.kind == "revisit":
            assert route.steps, f"{route.route_id} 是补回头，必须给出要重讲的格"
            assert units[route.fork_node].unit_id == route.steps[0].unit_id, \
                "补回头的岔出格就是要重讲的那一格"
        for step in route.steps:
            assert step.unit_id in unit_ids, f"{route.route_id} 指向了不存在的格 {step.unit_id}"

    # plan.json 里存的必须就是这 N 条（course.parse_plan_routes 一条都没丢）
    entries = plan_doc.get("routes") or []
    assert [e["route_id"] for e in entries] == [r.route_id for r in routes], \
        "plan.json 的 routes 与 script.routes 必须是同一批、同序"
    return routes


async def test_real_deck_full_class_runs_at_scale_with_preset_routes():
    """真实课件跑一整节课：预置路线合法且稀疏、变体已停产、42 页仍然收尾干净。"""
    # 先算 sha1、再加载：这样"加载时不许改盘"与"上课不许改盘"一次就验完
    from tests.fixtures import real_deck_id

    deck_id = real_deck_id()
    if not deck_id:
        pytest.skip("decks/ 下没有入库的真实课件（Unit_2_Sweet_Home… / 高一物理第一课…），"
                    "跳过规模验证")
    plan_file = plan_json(deck_id)
    sha_before = hashlib.sha1(plan_file.read_bytes()).hexdigest()
    plan_doc = json.loads(plan_file.read_text(encoding="utf-8"))

    course = _real_course()
    script = course.script
    units = script.units

    # 夹具前提：这是**真实规模**，不是把 6 单元夹具换个名字
    assert len(units) >= 100, f"这条用例的意义在规模上，实际只有 {len(units)} 个单元"
    assert script.slide_count >= 20, f"至少要有 20 页才能覆盖翻页行为：{script.slide_count}"

    # ① 变体停产在真实数据上真的发生了：一个单元都没有（内存里的补挂也不再触发）
    assert sum(len(u.variants) for u in units) == 0, \
        "迁移后真实课件的 script.units 里不该再有任何变体（旧格式才补挂）"
    assert not any(u.variants for u in course.plan.units)
    assert "variants" not in plan_doc["units"][0], "plan.json 的单元里也不该再有 variants 键"
    assert any("不再补挂" in n for n in course.notes), course.notes

    # ② 分支改由预置路线承载：条数稀疏、结构合法、指向真实的格
    routes = _assert_preset_routes_are_real(script, plan_doc)
    fork_pages = sorted({units[r.fork_node].slide for r in routes})
    assert len(fork_pages) >= 4, f"预置路线要跨多页才有意义：{[p + 1 for p in fork_pages]}"

    # 注入点里有 1 个正好落在预置 skip 的岔出格上（#139）：skip 的 when 要求
    # comprehension == confirmed，而这条用例注入的是"没懂" —— 它不该被选中。
    targets = _pick_targets(units, WANT_TARGETS)
    assert len(targets) >= 4, f"跨页注入点太少，覆盖不到跨页：{targets}"
    target_pages = sorted({script.unit(t).slide for t in targets})
    assert len(target_pages) >= 4, f"注入点必须跨多页：{[(t, script.unit(t).slide) for t in targets]}"

    director = Director(CandidatePool(), RuleDecider(), grace_ms=300)
    classroom = Classroom(script, director=director, probe_window=0.05)
    asked: list[str] = []

    async def say_does_not_understand(c: Classroom, seg: SpeechSeg):
        # 每个注入点只说一次：同一单元连说两次会把课堂测成"学生一直在抱怨"。
        if seg.kind == "unit" and seg.unit_id in targets and seg.unit_id not in asked:
            asked.append(seg.unit_id)
            await c.machine.handle_input("没懂")

    classroom.on_seg = say_does_not_understand
    t0 = time.monotonic()
    await classroom.run(timeout=TIMEOUT_S)
    elapsed = time.monotonic() - t0

    applied = classroom.applied_units()
    stats = director.stats

    # ③ 课堂跑完了，而且停在中性态、没有悬挂 task
    assert classroom.machine._finished.is_set(), "177 个单元也必须能正常收尾"
    assert classroom.state.slide_index == script.slide_count - 1, \
        f"必须讲到最后一页：{classroom.state.slide_index}/{script.slide_count - 1}"
    for _ in range(3):
        await asyncio.sleep(0)
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=0.3)
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert others == [], f"课后不许留下悬挂 task：{others}"

    # ④ 注入照旧跑全了，但**不再有变体上台** —— 这是"停产"在运行期的定义
    assert asked == targets, f"注入没跑全：{asked} vs {targets}"
    # 只钉**变体**这条路：停产之后它一次都不该上台。预置路线是另一条通道
    # （执行时留 route_applied / route_merged），它的接线与验收不在这条用例里。
    assert applied == [], f"变体已停产：学生说没懂不该再换来一个变体（实际 {applied}）"
    ended = [e for e in classroom.metrics.events if e["kind"] == "variant_replay_end"]
    assert ended == [], f"没有变体就不该有重播结束记录：{ended}"

    # ⑤ 强信号在整节课里不被弱信号抹掉（42 次页尾超时之后，困惑仍是 confused）
    for uid in targets:
        assert classroom.state.comprehension.get(uid) == "confused", \
            f"{uid} 明说过没懂，42 次探针超时之后仍应是 confused：{classroom.state.comprehension.get(uid)!r}"
    # 学生喊没懂的那几格一个字都不许重播（重讲链只在原地递进，不重播已讲过的句子）
    for uid in targets:
        texts = [s["text"] for s in classroom.unit_segs(uid)]
        assert len(set(texts)) == len(texts), f"{uid} 有句子被讲了两遍"

    # ⑥ 决策层零阻塞：预取都赶上了（规模变大也不许在边界上等）
    assert stats["timeout"] == 0, f"预取必须真的赶在边界之前：{stats}"
    assert stats["error"] == 0, f"离线口径下不该有异常：{stats}"

    # ⑦ 数据文件不许被改写（真实 plan.json 是花钱备出来的）
    sha_after = hashlib.sha1(plan_file.read_bytes()).hexdigest()
    assert sha_before == sha_after, "测试改写了 decks/ 下的 plan.json"

    print(f"[E] 真实课件 {script.slide_count} 页 / {len(units)} 单元 / 变体 0 个 / "
          f"预置路线 {len(routes)} 条：注入 {len(asked)} 次 → 切换到变体 {len(applied)} 次（停产口径）")
    print(f"    预置路线岔出页：{[p + 1 for p in fork_pages]}")
    print(f"    注入点（单元@页）：{[f'{t}@{script.unit(t).slide + 1}' for t in targets]}")
    print(f"    探针 {classroom.metrics.probe_sent} 次 / 超时 {classroom.metrics.probe_timeout} 次 · "
          f"Director.stats={stats}")
    print(f"    耗时 {elapsed:.2f}s（其中页尾探针窗口 {script.slide_count}×0.05s ≈ "
          f"{script.slide_count * 0.05:.1f}s 是唯一的真实等待）· 播放 {len(classroom.segs)} 段 · "
          f"plan.json sha1 {sha_before[:12]} 未变")


# ---------------------------------------------------------------------------
# ② 迁移命令：把旧格式 plan.json 迁到新格式（本棒新增的 run.py routes）
# ---------------------------------------------------------------------------

def test_routes_migration_is_repeatable_and_keeps_units_verbatim(tmp_path, monkeypatch):
    """迁移命令的三条硬契约：units 一字不动、删掉 variants、重复跑两次逐字一致。

    用合成课件而不是 42 页那份真实课件跑：这里要钉的是**命令的契约**（写盘形状 / 幂等 /
    备份），与规模无关；真实数据由上面那条用例覆盖，而"每次测试都去改 125KB 的真实
    plan.json"本身就不该发生（它是花钱备出来的）。deck.store 的两个入口被指到 tmp 目录，
    于是命令读写的是 tmp 里那份旧格式文档。
    """
    import deck.store as deck_store
    import run as run_mod
    from script_gen import plan_rule
    from tests.fixtures import make_deck

    deck = make_deck()
    doc = json.loads(plan_rule(deck).to_json())
    # 旧格式的样子：单元里带就地变体（真实课件是加载时补挂的，这里显式写进文件，
    # 好让"迁移把 variants 删掉"这件事在盘上有据可查）
    for unit in doc["units"][:2]:
        unit["variants"] = [{"variant_id": "v1", "label": "换说法", "text": "另一种说法。"}]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    before_text = path.read_text(encoding="utf-8")
    before_units = json.loads(before_text)["units"]

    monkeypatch.setattr(deck_store, "plan_json", lambda deck_id: path)
    monkeypatch.setattr(deck_store, "load_deck", lambda deck_id: deck)

    # ① --dry-run：只统计，一个字节都不写（也不建备份）
    assert run_mod.cmd_routes([deck.deck_id, "--no-polish", "--dry-run"]) == 0
    assert path.read_text(encoding="utf-8") == before_text, "--dry-run 不许写盘"
    assert not (tmp_path / "plan.json.bak-routes").exists(), "--dry-run 不许建备份"

    # ② 真正迁移：顶层写 routes、单元里删掉 variants、正文/锚点/操作一字不动
    assert run_mod.cmd_routes([deck.deck_id, "--no-polish"]) == 0
    doc1 = json.loads(path.read_text(encoding="utf-8"))
    assert doc1["routes"] and all(e["when"] for e in doc1["routes"])
    assert all("variants" not in u for u in doc1["units"]), "迁移必须删掉 units[*].variants"
    for old, new in zip(before_units, doc1["units"]):
        for key in ("slide", "text", "anchors", "ops", "kp", "must_teach", "is_page_end", "note"):
            assert new[key] == old[key], f"迁移不许动单元的 {key}"
    assert doc1["coverage"] == doc["coverage"]

    # ③ 幂等：第二次跑逐字一致（route_id 不变、units 不变），且不再写盘
    first_bytes = path.read_bytes()
    assert run_mod.cmd_routes([deck.deck_id, "--no-polish"]) == 0
    assert path.read_bytes() == first_bytes, "重复跑两次必须逐字一致（幂等）"

    # ④ 可回退：备份就是迁移前那一份文档，复制回去即可恢复旧格式
    backup = tmp_path / "plan.json.bak-routes"
    assert backup.exists(), "第一次写盘前必须留下迁移前备份"
    assert json.loads(backup.read_text(encoding="utf-8")) == json.loads(before_text)

    # ⑤ 停产的 variants 子命令不许把刚写好的 routes 抹掉（LessonPlan 没有 routes 字段，
    #    直接 plan.save() 会让新格式悄悄退回旧格式，下次加载又开始补挂变体）
    routes_before = json.loads(path.read_text(encoding="utf-8"))["routes"]
    assert run_mod.cmd_variants([deck.deck_id]) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["routes"] == routes_before, \
        "variants 子命令必须原样保留已有 routes"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(test_real_deck_full_class_runs_at_scale_with_preset_routes()))
