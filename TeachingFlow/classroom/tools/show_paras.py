"""看一眼某一页的段落锚点（核对低置信度兜底落在哪一行）。"""

import json
import sys
from pathlib import Path

deck_id = sys.argv[1]
page = int(sys.argv[2])
d = json.loads((Path("decks") / deck_id / "deck.json").read_text(encoding="utf-8"))
for e in d["slides"][page - 1]["elements"]:
    for p in ((e.get("text") or {}).get("paras") or []):
        if (p.get("text") or "").strip():
            print(f"{p['pid']:20s} {p['text'][:64]}")
