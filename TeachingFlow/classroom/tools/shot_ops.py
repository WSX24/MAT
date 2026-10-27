"""把每一种课件操作**画出来**（验收用）。

用法：
    .\\.venv\\Scripts\\python.exe tools/shot_ops.py [deck_id] [页码]

为什么需要它：单元测试只证明"操作能序列化、能定位到某一句"，
证明不了"操作在页面上真的有效果"。而真实备课计划里 `step`/`highlight`
占了 96%，`focus`/`zoom`/`point` 几乎不出现 —— 等上课碰运气遇到才验证，
等于没验证。所以这里用 `?ops=` 入口逐个施加，每个操作各截一张图，
再和"什么都没做"的基准图算像素差异：

    差异 ≈ 0  → 这个操作**什么都没做**（要么没实现，要么目标写错了）
    差异 > 0  → 页面上确实出现了变化，再人眼看变化对不对

最后拼成一张 contact sheet（.cache/shot/ops_sheet.png），一眼看全。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "shot"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
DEFAULT_DECK = "Unit_2_Sweet_Home单词解析课件_2025_2026学年人教版_2024_八年级英"


def chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("找不到无头浏览器（Chrome/Edge）")


def shot(url: str, out: Path, w: int = 1280, h: int = 720) -> bool:
    cmd = [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=1", f"--window-size={w},{h}",
           "--virtual-time-budget=8000", f"--screenshot={out}", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return out.exists() and r.returncode == 0


def pick_targets(deck_id: str, slide_index: int) -> dict:
    """从解析好的 deck.json 里挑几个真实锚点。

    刻意覆盖**每一类**锚点：标题形状、正文形状、正文里的某一段、图片、表格单元格 ——
    因为操作的定位是按 id 走的，"形状级"和"段落级"走的是两条不同的解析路径。
    """
    p = ROOT / "decks" / deck_id / "deck.json"
    deck = json.loads(p.read_text(encoding="utf-8"))
    slide = deck["slides"][slide_index]
    out: dict[str, str] = {}
    for el in slide.get("elements") or []:
        kind = str(el.get("kind") or "")
        role = str(el.get("role") or "")
        paras = [x for x in ((el.get("text") or {}).get("paras") or [])
                 if (x.get("text") or "").strip()]
        if paras and "para" not in out and len(paras) >= 3:
            out["para"] = paras[1]["pid"]        # 正文里的第二段（第一段常是标题行）
        if paras and role in ("title", "ctrTitle") and "title" not in out:
            out["title"] = el["element_id"]
        if paras and kind in ("shape", "rect", "roundRect", "") and "shape" not in out:
            out["shape"] = el["element_id"]
        if kind == "picture" and "pic" not in out:
            out["pic"] = el["element_id"]
        for row in ((el.get("table") or {}).get("cells") or []):
            for c in row:
                if c.get("pid") and "cell" not in out:
                    out["cell"] = c["pid"]
    if "para" not in out:                        # 兜底：任何一段
        for el in slide.get("elements") or []:
            for x in ((el.get("text") or {}).get("paras") or []):
                if (x.get("text") or "").strip():
                    out["para"] = x["pid"]
                    break
            if "para" in out:
                break
    return out


def main() -> int:
    deck_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DECK
    page = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    idx = page - 1
    t = pick_targets(deck_id, idx)
    if not t:
        print(f"第 {page} 页没找到可用的锚点")
        return 1
    print(f"第 {page} 页可用锚点：" + " · ".join(f"{k}={v}" for k, v in t.items()))

    para = t.get("para") or t.get("shape") or next(iter(t.values()))
    title = t.get("title") or t.get("shape") or para
    shape = t.get("shape") or para
    pic = t.get("pic") or shape
    # (名字, 操作序列, 期望结果, 判定阈值 %)
    #  `expect="baseline"` 的几组是**成对操作**：做了再撤，就该回到原样。
    #  它们其实是最有价值的一组断言 —— 只测"有效果"会让"撤不回来"悄悄溜过去
    #  （撤不回来意味着放大/压暗会跟着学生翻到下一页，是真的会发生的坏体验）。
    cases: list[tuple[str, list[dict], str, float]] = [
        ("baseline", [], "baseline", 0.0),
        ("highlight_title", [{"op": "highlight", "target": title}], "change", 0.15),
        ("highlight_para", [{"op": "highlight", "target": para}], "change", 0.15),
        ("highlight_box", [{"op": "highlight", "target": shape, "mode": "box"}],
         "change", 0.15),
        ("point_para", [{"op": "point", "target": para}], "change", 0.01),
        ("hide_para", [{"op": "hide", "target": para}], "change", 0.10),
        ("step_para", [{"op": "step", "target": para}], "change", 1.0),
        ("focus_title", [{"op": "focus", "target": title}], "change", 1.0),
        ("zoom_para", [{"op": "zoom", "target": para, "scale": 1.8}], "change", 5.0),
        ("zoom_pic", [{"op": "zoom", "target": pic, "scale": 2.0}], "change", 5.0),
        ("unfocus_restores", [{"op": "focus", "target": title},
                              {"op": "unfocus"}], "baseline", 0.0),
        ("reset_zoom_restores", [{"op": "zoom", "target": para, "scale": 1.8},
                                 {"op": "reset_zoom"}], "baseline", 0.0),
        ("clear_restores", [{"op": "step", "target": para},
                            {"op": "clear"}], "baseline", 0.0),
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    port = 8000
    base = f"http://127.0.0.1:{port}/preview?deck={quote(deck_id)}&bare=1&slide={idx}"
    shots: list[tuple[str, Path, float]] = []
    bad: list[str] = []
    base_px = None
    for name, ops, expect, thresh in cases:
        url = base + (f"&ops={quote(json.dumps(ops))}" if ops else "")
        out = OUT / f"op_{name}.png"
        if not shot(url, out):
            print(f"  ❌ {name}: 截图失败（服务没起？端口 {port}）")
            bad.append(name)
            continue
        try:
            from PIL import Image

            img = Image.open(out).convert("RGB")
            px = list(img.getdata())
            if name == "baseline":
                base_px = px
                diff = 0.0
            elif base_px is not None:
                same = sum(1 for a, b in zip(px, base_px)
                           if abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) <= 60)
                diff = (1 - same / len(px)) * 100
            else:
                diff = -1.0
            shots.append((name, out, diff))
            if name == "baseline":
                verdict = "（基准）"
            elif expect == "baseline":
                verdict = ("✅ 正确复原" if diff <= 0.05
                           else f"❌ 没复原（{diff:.2f}%）—— 这类操作会残留到下一页")
            else:
                verdict = (f"✅ 有效果" if diff >= thresh
                           else f"❌ 变化只有 {diff:.2f}%，低于 {thresh}% —— 操作没生效？")
            if "❌" in verdict:
                bad.append(name)
            print(f"  {name:20s} 像素变化 {diff:5.2f}%  {verdict}")
        except ImportError:
            shots.append((name, out, -1.0))
            print(f"  {name:20s} → {out.name}（没装 Pillow，没算差异）")

    # 拼 contact sheet：2 列 × N 行，每格标上操作名
    try:
        from PIL import Image, ImageDraw

        cols, scale = 2, 0.34
        cells = [(n, p) for n, p, _ in shots]
        if cells:
            w = int(1280 * scale)
            h = int(720 * scale) + 18
            rows = (len(cells) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * w + (cols + 1) * 8,
                                      rows * h + (rows + 1) * 8), "white")
            d = ImageDraw.Draw(sheet)
            for i, (name, path) in enumerate(cells):
                r, c = divmod(i, cols)
                im = Image.open(path).convert("RGB").resize((w, h - 18))
                x = 8 + c * (w + 8)
                y = 8 + r * h
                sheet.paste(im, (x, y))
                d.text((x + 2, y + h - 16), name, fill=(180, 0, 0))
            sheet_path = OUT / "ops_sheet.png"
            sheet.save(sheet_path)
            print(f"\n效果图总览 → {sheet_path}")
    except ImportError:
        pass
    print(f"单张效果图在 {OUT}\\op_*.png")
    if bad:
        print(f"\n❌ 有 {len(bad)} 组没通过：" + "、".join(bad))
        return 1
    print("\n✅ 所有操作都有效果，且成对操作都能复原")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
