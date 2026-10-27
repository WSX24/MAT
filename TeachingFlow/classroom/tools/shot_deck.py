"""把重绘结果与 PowerPoint 导出图并排截图（验收用）。

用法：
    .\.venv\Scripts\python.exe tools\shot_deck.py <deck_id> <页码...>

它做三件事：
1. 用无头 Chrome 打开 `/preview?deck=..&slide=..&bare=1`，窗口正好 1280×720 截图
   —— 截出来就是一整页课件，可以和 PowerPoint 导出的同名页**逐页对照**。
2. 把"重绘 / PowerPoint"两张图横向拼成一张对比图，放 .cache/shot/compare_pNN.png
3. 打印每张图的像素差异比例（只作参考：字体渲染差异不可能为零，
   真正要人眼确认的是版式、字号层级、元素位置和图片）

为什么要有这个脚本：重绘对不对，靠断言是证不出来的 —— 只能看。
所以把"看一眼"这件事固化成一条命令，改完渲染随手就能跑。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "shot"
REF = ROOT / ".cache" / "ref"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("找不到无头浏览器（Chrome/Edge）")


def shot(url: str, out: Path, w: int = 1280, h: int = 720) -> bool:
    exe = chrome()
    # `--virtual-time-budget` 不能省：页面是 fetch 之后才渲染的，
    # 不加它 Chrome 会在 load 事件后立刻截图 —— 截到的是一张白页（踩过）。
    cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=1", f"--window-size={w},{h}",
           "--virtual-time-budget=8000",
           f"--screenshot={out}", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return out.exists() and r.returncode == 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    deck_id = sys.argv[1]
    pages = [int(x) for x in sys.argv[2:]]
    OUT.mkdir(parents=True, exist_ok=True)
    port = 8000
    from urllib.parse import quote

    base = f"http://127.0.0.1:{port}/preview?deck={quote(deck_id)}&bare=1&slide="

    ok = 0
    for n in pages:
        mine = OUT / f"mine_p{n:02d}.png"
        if not shot(base + str(n - 1), mine):
            print(f"  ❌ 第 {n} 页截图失败（服务没起？端口 {port}）")
            continue
        ref = REF / f"ref_p{n:02d}.png"
        try:
            from PIL import Image, ImageDraw

            a = Image.open(mine).convert("RGB")
            if ref.exists():
                b = Image.open(ref).convert("RGB")
                canvas = Image.new("RGB", (a.width + b.width + 12, max(a.height, b.height)), "white")
                canvas.paste(a, (0, 0))
                canvas.paste(b, (a.width + 12, 0))
                d = ImageDraw.Draw(canvas)
                d.text((8, 8), "OURS", fill=(200, 0, 0))
                d.text((a.width + 20, 8), "POWERPOINT", fill=(0, 100, 200))
                cmp_path = OUT / f"compare_p{n:02d}.png"
                canvas.save(cmp_path)
                # 像素差异比例（仅供参考的粗指标）
                diff = sum(1 for pa, pb in zip(a.getdata(), b.resize(a.size).getdata())
                           if abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) + abs(pa[2] - pb[2]) > 60)
                print(f"  第 {n:>2} 页 → {cmp_path.name}  像素差异 {diff / (a.width * a.height) * 100:.1f}%")
                ok += 1
            else:
                a.save(OUT / f"mine_p{n:02d}.png")
                print(f"  第 {n:>2} 页 → mine_p{n:02d}.png（没有 PowerPoint 参考图，先跑参考导出）")
                ok += 1
        except ImportError:
            print(f"  第 {n:>2} 页 → {mine}（没装 Pillow，没拼对比图）")
            ok += 1
    print(f"\n截图完成 {ok}/{len(pages)} 页 → {OUT}")
    print("参考图（PowerPoint 导出）放在 .cache/ref/，用 tools/export_ref.ps1 生成。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
