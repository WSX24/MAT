"""课件仓库：一份 pptx 解析出来的所有东西都放在 decks/<deck_id>/ 下。

目录约定（和 .cache/ 分开放 —— 解析结果是**产物**，要能进版本库、能人工审核）：

    prototype/decks/<deck_id>/
    ├─ deck.json        解析结果（DeckDoc，即可渲染、可锚定的课件结构）
    ├─ plan.json        课堂脚本（讲解单元 + 锚点 + 操作），由 script_gen 生成
    ├─ plan.bak.json    上一版 plan.json（重新备课**覆盖写之前**自动留的底）
    ├─ media/           抽取/下载出来的图片（前端按 /api/deck/media/<deck_id>/<name> 取）
    └─ source.txt       源 pptx 的路径与 sha1（源文件副本统一放在 prototype/pptx/，
                        所以这里指向的是副本，不是用户上传时的原位置）

deck_id 由文件名派生并做稳定化处理（中文保留，空格与标点转下划线），
这样"同一份 pptx 重新解析"会落到同一个目录，不会越攒越多。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from config import ROOT
from deck.schema import DeckDoc

#: 课件根目录
DECKS_DIR: Path = ROOT / "decks"

_SLUG_BAD = re.compile(r"[^\w\u4e00-\u9fff]+")


def make_deck_id(source: str | Path, *, max_len: int = 48) -> str:
    """从文件名派生 deck_id。中文保留（人要看），其余非法字符压成下划线。"""
    stem = Path(source).stem
    slug = _SLUG_BAD.sub("_", stem).strip("_")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_")
    return slug or "deck"


def deck_dir(deck_id: str) -> Path:
    return DECKS_DIR / deck_id


def media_dir(deck_id: str) -> Path:
    return deck_dir(deck_id) / "media"


def deck_json(deck_id: str) -> Path:
    return deck_dir(deck_id) / "deck.json"


def plan_json(deck_id: str) -> Path:
    return deck_dir(deck_id) / "plan.json"


def plan_bak_json(deck_id: str) -> Path:
    return deck_dir(deck_id) / "plan.bak.json"


def backup_plan(deck_id: str) -> Path | None:
    """把现有 plan.json 备份成 plan.bak.json（覆盖写之前调用）；没有就返回 None。

    为什么文件名固定成一个、不带时间戳：这份备份是给"重备崩了"救场用的 ——
    目录里固定叫 plan.bak.json，一眼就知道哪一份是上一版；攒多份之后，
    "哪一份还能用"就变成了猜。要留更多历史，那是版本库该干的事。
    """
    src = plan_json(deck_id)
    if not src.exists():
        return None
    dst = plan_bak_json(deck_id)
    shutil.copy2(src, dst)
    return dst


def save_deck(deck: DeckDoc, *, root: Path | None = None) -> Path:
    base = (root / deck.deck_id) if root else deck_dir(deck.deck_id)
    base.mkdir(parents=True, exist_ok=True)
    (base / "media").mkdir(exist_ok=True)
    p = base / "deck.json"
    p.write_text(deck.to_json(indent=1), encoding="utf-8")
    src = base / "source.txt"
    src.write_text(f"{deck.source_path}\nsha1={deck.source_sha1}\n", encoding="utf-8")
    return p


def load_deck(deck_id: str, *, root: Path | None = None) -> DeckDoc:
    base = (root / deck_id) if root else deck_dir(deck_id)
    p = base / "deck.json"
    if not p.exists():
        raise FileNotFoundError(
            f"找不到课件 {deck_id}（期望 {p}）。先用 `python run.py pptx <文件>` 解析一份。"
        )
    deck = DeckDoc.load(p)
    # 目录名和课件内部 id 不一致时要说出来：课件里的 deck_id 才是"真身"，
    # 后面所有写盘（plan.json 等）都按它走。复制/改名目录做实验时，
    # 不说这一句就会"备了半天课，写进了另一份课件的目录"（实测踩过：
    # 复制出 <id>__v2 目录重备 42 页，结果覆盖了原课件的计划）。
    if base.name != deck.deck_id:
        print(f"[deck] ⚠️ 目录名 {base.name} 与课件 id {deck.deck_id} 不一致 —— "
              f"读写都会按课件 id 走（{deck_dir(deck.deck_id)}），不是你现在这个目录。",
              flush=True)
    return deck


def list_decks(*, root: Path | None = None) -> list[dict]:
    """列出已解析的课件，供 /api/decks 与审核页使用。"""
    base = root or DECKS_DIR
    out: list[dict] = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        p = d / "deck.json"
        if not (d.is_dir() and p.exists()):
            continue
        try:
            deck = DeckDoc.load(p)
        except Exception as e:  # 坏文件不该把列表整个带崩
            out.append({"deck_id": d.name, "error": f"{type(e).__name__}: {e}"})
            continue
        media = d / "media"
        out.append({
            "deck_id": deck.deck_id,
            "title": deck.title,
            "slides": deck.slide_count,
            "width_px": deck.width_px,
            "height_px": deck.height_px,
            "media": len(list(media.iterdir())) if media.exists() else 0,
            "has_plan": (d / "plan.json").exists(),
            "warnings": len(deck.warnings),
            "ingested_at": deck.ingested_at,
        })
    return out
