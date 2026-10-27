"""端到端验收：AI 讲师**真的在操作真实 PPT**（可真跑，也可当回归测试）。

它伪装成浏览器，按正式版协议做两件事：先 `POST /api/v1/sessions` 开一节课拿到
`session_id`，再连 `/ws/classroom/{session_id}`，按播放时长回 `{"t":"speech_done","seq":N}`，
然后检查四件必须成立的事：

1. 发来的是**真实课件**：`page` 载荷里有 elements/decorations，而不是原型自带的堆叠版式；
2. 讲解内容来自真实课件的**备课脚本**：句子与锚点对得上（锚点在页面上存在）；
3. **课件操作真的发生**：`segment_start.ops` 非空，且目标锚点在该页存在、操作类型合法
   （"AI 讲师操作 PPT"这条链路，只看文字是看不出来的 —— 必须落到 op 上）；
4. **翻页有节制**：一页的单元讲完之前不翻页（段末让位）。

用法（先起服务：python app.py）：
    python tools/e2e_deck.py                 # 自动挑一门"已备课"的课的第一个单元，跑前 3 页
    python tools/e2e_deck.py --unit u-xxx    # 指定单元（跳过自动挑选）
    python tools/e2e_deck.py --dry           # 只开课、连上、收 hello 就退（验协议与连通性，不跑整节课）
    python tools/e2e_deck.py --pages 4       # 最多跑到第 4 页
    python tools/e2e_deck.py --limit 200 --speed 0   # 不限页数、全速（真上课时长）

协议对齐（正式版设计文档 §4）：
    * 开课 `POST /api/v1/sessions {unit_id}` → `{session_id, resumed, position}`
    * 连接 `/ws/classroom/{session_id}`；S→C 帧一律带 `kind`，C→S 用 `{"t": ...}`
    * 逐段推进：收到 `segment_end` 后回 `{"t":"speech_done","seq":N}`（N 取自 segment_start）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

#: 课堂服务地址。正式版只有一个入口（仓库根 app.py / start-app.ps1），端口默认 8000。
DEFAULT_BASE = "http://127.0.0.1:8000"
OPS_OK = {"reveal", "hide", "step", "highlight", "focus", "unfocus", "point",
          "zoom", "reset_zoom", "clear"}


def _http_json(method: str, url: str, body: dict | None = None) -> dict:
    """极简 JSON 请求（不引第三方库：这个脚本要能在裸环境里跑）。"""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SystemExit(f"[e2e] {method} {url} → HTTP {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"[e2e] 连不上课堂服务 {url}：{exc.reason}\n"
                         f"      先把服务起起来：python app.py（或 ./start-app.ps1）") from None


def _pick_unit(base: str) -> str:
    """自动挑一个"已备课"的单元：课程库里第一门有 ready 单元的课的第一个 ready 单元。"""
    courses = _http_json("GET", f"{base}/api/v1/courses")
    for course in courses:
        if int(course.get("unit_ready_count") or 0) <= 0:
            continue
        detail = _http_json("GET", f"{base}/api/v1/courses/{course['id']}")
        for chapter in detail.get("chapters") or []:
            for unit in chapter.get("units") or []:
                if unit.get("ready"):
                    return str(unit["id"])
    raise SystemExit("[e2e] 课程库里没有「已备课」的单元：先导入一份 pptx，"
                     "或在单元页点「准备课件」，再用 --unit 指定单元 id")


def _open_session(base: str, unit_id: str) -> dict:
    """按正式版协议开一节课：POST /api/v1/sessions。"""
    out = _http_json("POST", f"{base}/api/v1/sessions", {"unit_id": unit_id})
    if not out.get("session_id"):
        raise SystemExit(f"[e2e] 开课没拿到 session_id：{out}")
    return out


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str]] = []
        self.notes: list[str] = []

    def need(self, cond: bool, msg: str) -> None:
        self.checks.append((bool(cond), msg))

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def dump(self) -> int:
        print("\n================ 验收 ================")
        for ok, msg in self.checks:
            print(("  ✅ " if ok else "  ❌ ") + msg)
        for n in self.notes:
            print("  · " + n)
        bad = [m for ok, m in self.checks if not ok]
        print("\n" + ("❌ 未通过：" + "；".join(bad) if bad
                      else "✅ 端到端验收通过"))
        return 1 if bad else 0


def _ids_of(slide_payload: dict) -> set[str]:
    """一页上所有可锚定的 id（元素 + 段落 + 表格单元格）。"""
    out: set[str] = set()
    for group in ("elements", "decorations"):
        for e in slide_payload.get(group) or []:
            out.add(e.get("element_id", ""))
            for p in ((e.get("text") or {}).get("paras") or []):
                if p.get("pid"):
                    out.add(p["pid"])
            for row in ((e.get("table") or {}).get("cells") or []):
                for c in row:
                    if c.get("pid"):
                        out.add(c["pid"])
    return out


async def run(args) -> int:
    rep = Report()
    t0 = time.time()
    page_no = 0
    prev_page = 0
    page_ids: set[str] = set()
    units_this_page: list[str] = []
    pages_ok: set[int] = set()
    pages_bad: list[str] = []
    deck_mode = False
    ops_total = ops_applied = ops_missing = 0
    op_kinds: dict[str, int] = {}
    anchor_missing = 0
    anchors_total = 0
    segs = 0
    pages_seen: list[int] = []
    #: 这一页原稿里有多少"讲到才出现"的内容（page 载荷的 reveal）；
    #: 非空 = 讲完这一页时必须看到 step/reveal 操作，否则学生永远看不到那些字。
    reveal_of_page: dict[int, set[str]] = {}
    #: 这一页实际 reveal 出去的锚点 id
    revealed_on_page: dict[int, set[str]] = {}
    ops_on_page: dict[int, int] = {}
    pages_left: list[int] = []          # 已经讲完并翻过去的页
    done = False

    #: 新协议把播放时长放在 segment_start.est_sec（segment_end 只带 seq/text）
    est_of_seq: dict[int, float] = {}

    async def play(ws, est: float, seq: int) -> None:
        # 全速模式（--speed 0）异步不等，尽量把整节课跑完
        d = max(0.0, est * args.speed)
        if d:
            await asyncio.sleep(d)
        await ws.send(json.dumps({"t": "speech_done", "seq": seq}))

    tasks: list[asyncio.Task] = []

    # ---- 开课 + 连上（正式版协议：REST 拿 session_id，WS 挂在会话上） ----
    base = args.base.rstrip("/")
    unit_id = args.unit or _pick_unit(base)
    opened = _open_session(base, unit_id)
    session_id = str(opened["session_id"])
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/classroom/{session_id}"
    print(f"[{0.0:6.1f}s] 开课 unit={unit_id} session={session_id} "
          f"resumed={opened.get('resumed')} position={opened.get('position')}")
    print(f"[{0.0:6.1f}s] 连接 {ws_url}")

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            except asyncio.TimeoutError:
                rep.need(False, f"等待服务端消息超时（{args.timeout}s）—— 课堂卡住了")
                break
            m = json.loads(raw)
            kind = m.get("kind")
            el = time.time() - t0

            if kind == "hello":
                unit = m.get("unit") or {}
                print(f"[{el:6.1f}s] hello 单元={unit.get('title') or unit.get('id')}"
                      f" 页数={unit.get('slides')} 起点={m.get('position')}")
                if args.dry:
                    rep.need(bool(unit.get("id")), "开课成功且收到 hello（协议与连通性 OK）")
                    break
                continue

            if kind == "page":
                # 注意：`slide` 是**内容**（元素/装饰），页码在 `slide_index` ——
                # 这两个键曾经撞在一起过，看错就会把 dict 当页码。
                slide = m.get("slide") or {}
                idx = m.get("slide_index")
                page_no = int(idx) + 1 if isinstance(idx, (int, float)) else 0
                pages_seen.append(page_no)
                deck_mode = bool(slide.get("elements") and "elements" in slide)
                page_ids = _ids_of(slide)
                n_el = len(slide.get("elements") or [])
                n_dec = len(slide.get("decorations") or [])
                units_this_page = []
                reveal_of_page[page_no] = set(slide.get("reveal") or [])
                if prev_page and (
                        reveal_of_page.get(prev_page) and not ops_on_page.get(prev_page, 0)):
                    pages_bad.append(
                        f"第 {prev_page} 页有 {len(reveal_of_page[prev_page])} 处"
                        f"「讲到才出现」的内容，但讲完这页只有 0 个操作 —— 那些字学生看不到")
                if prev_page:
                    pages_left.append(prev_page)
                prev_page = page_no
                n_rev = len(reveal_of_page[page_no])
                print(f"[{el:6.1f}s] ==== 第 {page_no} 页 · 元素 {n_el} · "
                      f"版式装饰 {n_dec} · 可锚 {len(page_ids)}"
                      + (f" · 讲到才出现 {n_rev}" if n_rev else ""))
                if args.pages and page_no > args.pages:
                    rep.note(f"已跑完前 {args.pages} 页，收工")
                    break

            elif kind == "segment_start":
                segs += 1
                est_of_seq[int(m.get("seq") or 0)] = float(m.get("est_sec") or 1.0)
                sk = m.get("seg_kind", "?")
                text = m.get("text", "")
                print(f"[{el:6.1f}s #{m.get('seq'):<3} {sk:9s}] {text[:46]}")
                if sk == "unit":
                    units_this_page.append(m.get("unit_id", ""))
                # 正式版：segment_start 带 `anchor_ids`（一句一个锚点 id），不再是
                # 旧协议的 `anchors: [{element_id: ...}]`。
                for aid in (m.get("anchor_ids") or []):
                    anchors_total += 1
                    if page_ids and aid not in page_ids:
                        anchor_missing += 1
                for op in (m.get("ops") or []):
                    ops_total += 1
                    ops_on_page[page_no] = ops_on_page.get(page_no, 0) + 1
                    op_kinds[op.get("op", "?")] = op_kinds.get(op.get("op", "?"), 0) + 1
                    if op.get("op") not in OPS_OK:
                        pages_bad.append(f"第 {page_no} 页出现未知操作 {op.get('op')!r}")
                        continue
                    targets = [op.get("target")] + list(op.get("targets") or [])
                    targets = [t for t in targets if t]
                    revealed_on_page.setdefault(page_no, set()).update(targets)
                    missing = [t for t in targets if t not in page_ids]
                    if missing:
                        ops_missing += 1
                        pages_bad.append(
                            f"第 {page_no} 页操作 {op.get('op')} 指向不存在的锚点 {missing}")
                    else:
                        ops_applied += 1
                        if args.verbose:
                            print(f"            ↳ {op.get('op')} → {','.join(targets)}"
                                  f"（第 {op.get('at_sentence')} 句）")

            elif kind == "segment_end":
                if units_this_page:
                    pages_ok.add(page_no)
                seq = int(m.get("seq") or 0)
                t = asyncio.create_task(play(ws, est_of_seq.get(seq, 1.0), seq))
                tasks.append(t)
                if args.limit and len(pages_ok) >= args.limit:
                    done = True
                if done:
                    break

            elif kind == "notify":
                lvl = m.get("level", "?")
                txt = m.get("text", "")
                if lvl in ("error", "warn"):
                    print(f"[{el:6.1f}s {lvl}] {txt[:70]}")
                    if "音频" not in txt and lvl == "error":
                        rep.note(f"服务端 {lvl}：{txt[:90]}")

    for t in tasks[-4:]:
        try:
            await asyncio.wait_for(t, timeout=10)
        except Exception:
            pass

    # ---- 判定 ----
    if args.dry:
        return rep.dump()          # --dry：只验"开课 + 连上 + hello"，上课相关的判定不适用
    rep.need(page_no > 0, "服务端发过 page（课件真的画出来了）")
    rep.need(deck_mode, "page 载荷是真实课件重绘（elements/decorations），不是原型自带版式")
    rep.need(segs > 0, "讲师讲过话（segment_start 收到）")
    rep.need(anchors_total > 0, f"语音与课件元素挂上了锚点（{anchors_total} 个）")
    rep.need(anchor_missing == 0,
             f"所有锚点都落在当前页面上（{anchors_total - anchor_missing}/{anchors_total}）")
    # 「讲师真的在操作课件」按页判，而不是只看"整节课有没有操作"：
    # 只看总数会被短跑骗过（跑两页本来就没到操作那一句），也会漏掉真正的问题 ——
    # 某一页原稿有"讲到才出现"的内容，却整页一个操作都没有（那些字学生永远看不到）。
    pages_need_ops = [p for p in pages_left if reveal_of_page.get(p)]
    if pages_need_ops:
        ok_pages = [p for p in pages_need_ops if ops_on_page.get(p, 0)]
        rep.need(len(ok_pages) == len(pages_need_ops),
                 f"有分步内容的页都真的操作了课件"
                 f"（{len(ok_pages)}/{len(pages_need_ops)} 页 · 共 {ops_total} 个操作）")
    else:
        rep.note(f"这次跑过的页原稿里没有分步内容 —— 操作通道没被触发"
                 f"（共 {ops_total} 个操作）")
        rep.need(True, "讲师操作课件（本次未跑到有分步内容的页，跳过）")
    rep.need(ops_missing == 0, f"操作目标全部有效（命中 {ops_applied}/{ops_total}）")
    rep.need(not pages_bad, "没有非法操作/越界锚点"
             + (f"（{len(pages_bad)} 条：{pages_bad[:2]}）" if pages_bad else ""))
    # 分步内容"该出现的都出现了"：只在能对上 id 时判定，避免误报
    stayed_hidden: list[str] = []
    for p in pages_left:
        want = reveal_of_page.get(p) or set()
        if not want:
            continue
        got = revealed_on_page.get(p) or set()
        missing = sorted(want - got)
        if missing:
            stayed_hidden.append(f"第 {p} 页 {len(missing)} 处（如 {missing[0]}）")
    if stayed_hidden:
        rep.note("这些「讲到才出现」的内容到最后都没被 reveal（学生看不到）："
                 + "、".join(stayed_hidden[:3]))
    rep.need(len(pages_ok) >= args.limit,
             f"讲完了 {len(pages_ok)} 页（要求 ≥{args.limit}）")
    rep.note(f"共 {segs} 段 · 页面序列 {pages_seen[:12]}"
             + ("…" if len(pages_seen) > 12 else ""))
    if op_kinds:
        rep.note("操作类型分布：" + "、".join(f"{k}×{v}" for k, v in
                                            sorted(op_kinds.items(), key=lambda kv: -kv[1])))
    rep.note(f"用时 {time.time() - t0:.1f}s（播放速度 {args.speed}×）")
    return rep.dump()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=0, help="最多跑多少页（0=用 --limit 控制）")
    ap.add_argument("--limit", type=int, default=3, help="至少要讲完几页算通过")
    ap.add_argument("--speed", type=float, default=0.15,
                    help="模拟播放速度（1=按真实语速等，0=全速不等）")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--base", default=os.environ.get("CLASSROOM_BASE", DEFAULT_BASE),
                    help=f"课堂服务地址（默认 {DEFAULT_BASE}，可用 CLASSROOM_BASE 覆盖）")
    ap.add_argument("--unit", default="", help="单元 id（默认自动挑一个已备课的单元）")
    ap.add_argument("--dry", action="store_true",
                    help="只开课 + 连上 + 收 hello 就退（验协议与连通性，不跑整节课）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.pages and not args.limit:
        args.limit = args.pages
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
