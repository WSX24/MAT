"""真实 pptx 解析测试。

口径：**用现场生成的 pptx 测解析器**（字段级、可重复），
真实课件只做一次宽松的冒烟检查 —— 它是端到端验收的输入，
不该成为"跑个单元测试要先读 16MB"的理由。

解析器是整条链的地基：它错一点，后面全是漂亮的错。所以这里盯四件事：
1. 结构对：段落级 id、图片、表格、装饰形状都要被认出来
2. 几何对：所有元素都在画布内、没有零尺寸（零尺寸会让前端算不出指示位置）
3. 稳定：同一份 pptx 解析两次，字节完全一致（否则每次重新解析都像换了份课件，
   锚点会整体错位）
4. 往返：save → load → 再序列化，JSON 完全一致（否则审核页看到的是另一份数据）
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deck.schema import KIND_PICTURE, KIND_RECT, KIND_TABLE, KIND_TEXT  # noqa: E402
from deck.store import list_decks, load_deck, make_deck_id, save_deck  # noqa: E402
from tests.fixtures import make_pptx  # noqa: E402

REAL_DECK = Path(__file__).resolve().parent.parent.parent / (
    "Unit 2 Sweet Home单词解析课件 2025-2026学年人教版（2024）八年级英语上册 .pptx")


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="deck_test_"))


def test_ingest_generated_pptx() -> None:
    from deck.pptx_ingest import ingest_pptx

    tmp = _tmpdir()
    try:
        pptx = make_pptx(tmp / "demo.pptx")
        # decks_root 一定要给：不给就写进**真实** decks/ 目录，
        # 跑几次测试就多出 decks/test_demo、decks/d 这些垃圾课件
        deck = ingest_pptx(pptx, deck_id="test_demo", decks_root=tmp / "decks",
                           download_external=False)
        assert deck.slide_count == 2, deck.slide_count
        assert deck.width_px == 1280 and deck.height_px == 720, \
            f"16:9 应换算成 1280×720，实际 {deck.width_px}×{deck.height_px}"

        s1 = deck.slides[0]
        kinds = {e.kind for e in s1.elements}
        assert KIND_TEXT in kinds and KIND_PICTURE in kinds, kinds

        # 文本框：3 段都要在，并且是**段落级**可锚定的（真实课件的核心粒度）
        title = next(e for e in s1.elements if e.is_title or "用法讲解" in e.plain_text)
        assert title.plain_text.startswith("1.pack"), title.plain_text
        multi = next(e for e in s1.elements if len(e.paragraphs) >= 3)
        assert len(multi.paragraphs) == 3, [p.text for p in multi.paragraphs]
        assert multi.paragraphs[0].pid.endswith("-p01"), multi.paragraphs[0].pid
        assert "音标" in multi.paragraphs[0].text
        assert "例句" in multi.paragraphs[2].text
        # 段落文字要非空，且带上字号（字号写在 run 上）
        for p in multi.paragraphs:
            assert p.text.strip(), p
        sizes = [r.size_pt for p in multi.paragraphs for r in p.runs]
        assert sizes and all(s and 8 <= s <= 96 for s in sizes), sizes

        # 图片抽取到 media/，并且引用得能对上
        pics = [e for e in s1.elements if e.kind == KIND_PICTURE]
        assert pics, "图片没被解析出来"
        assert pics[0].pic and pics[0].pic.src, "图片没有媒体引用"
        assert deck.media, "media 映射为空"

        # 表格页：格子要能被锚定
        s2 = deck.slides[1]
        table = next(e for e in s2.elements if e.kind == KIND_TABLE)
        assert table.table is not None
        assert len(table.table.cells) == 3 and len(table.table.cells[0]) == 2
        assert table.table.cells[0][0].text == "黑体单词用法详解"
        assert all(c.pid for row in table.table.cells for c in row), \
            "表格单元格没有段落级 id（目录/答案表就锚不到格子）"
        assert table.cell_ids, "cell_ids 为空"

        # 锚点索引：形状 + 段落 + 单元格
        idx = s2.anchor_index()
        assert table.element_id in idx
        assert table.table.cells[0][0].pid in idx
        print(f"[D1] 生成 pptx 解析 OK：{deck.slide_count} 页 · "
              f"每页元素 {[len(s.elements) for s in deck.slides]} · 媒体 {len(deck.media)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_geometry_is_sane() -> None:
    from deck.pptx_ingest import ingest_pptx

    tmp = _tmpdir()
    try:
        deck = ingest_pptx(make_pptx(tmp / "demo.pptx"), decks_root=tmp / "decks",
                           download_external=False)
        for s in deck.slides:
            for e in s.elements:
                x, y, w, h = e.box
                assert w >= 0 and h >= 0, f"{e.element_id} 负尺寸 {e.box}"
                assert x >= -2 and y >= -2, f"{e.element_id} 跑到画布外 {e.box}"
                assert x <= deck.width_px and y <= deck.height_px, \
                    f"{e.element_id} 起点超出画布 {e.box}"
                if e.kind == KIND_PICTURE:
                    assert w > 0 and h > 0, f"{e.element_id} 图片零尺寸（会算不出指示位置）"
        print("[D2] 几何全部落在画布内，图片无零尺寸")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ingest_is_deterministic_and_round_trips() -> None:
    """两次解析必须字节一致；save→load→序列化必须完全相同。

    不稳定的解析会让"锚点"变成流沙：同一页昨天是 `s02-sh02-p03`，
    今天变成 `s02-sh03-p02`，而脚本里存的是旧 id —— 上课时安静地指错地方。
    """
    from deck.pptx_ingest import ingest_pptx

    tmp = _tmpdir()
    try:
        pptx = make_pptx(tmp / "demo.pptx")
        a = ingest_pptx(pptx, deck_id="d", decks_root=tmp / "decks",
                        download_external=False)
        b = ingest_pptx(pptx, deck_id="d", decks_root=tmp / "decks",
                        download_external=False)
        assert a.to_json() == b.to_json(), "同一份 pptx 两次解析结果不一致"

        p = save_deck(a, root=tmp / "decks")
        back = load_deck("d", root=tmp / "decks")
        assert back.to_json() == a.to_json(), "save→load 之后数据变了"
        assert json.loads(back.to_json())["slides"][0]["elements"], "反序列化后元素丢了"
        print("[D3] 解析确定性 + save/load 往返一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_deck_id_from_chinese_filename() -> None:
    """中文文件名要能原样变成目录名（人要在 decks/ 里认得出哪份是哪份）。"""
    name = "Unit 2 Sweet Home单词解析课件 2025-2026学年人教版（2024）八年级英语上册 .pptx"
    did = make_deck_id(name)
    assert "Unit" in did and "Sweet_Home" in did, did
    assert " " not in did and "/" not in did and did == did.strip("_"), did
    assert len(did) <= 48
    assert make_deck_id("") == "deck"
    print(f"[D4] 中文文件名 → deck_id：{did}")


def test_real_deck_smoke() -> None:
    """真实课件冒烟：结构规模在合理范围，且每页都有可锚定的东西。"""
    if not REAL_DECK.exists():
        print(f"[D5] 跳过：没找到真实课件 {REAL_DECK.name}")
        return
    from deck.pptx_ingest import ingest_pptx

    tmp = _tmpdir()
    try:
        # 解析到临时目录：测试只该**读**真实课件，不该顺手重写用户的 decks/<id>/
        deck = ingest_pptx(REAL_DECK, decks_root=tmp / "decks",
                           download_external=False)
        assert 30 <= deck.slide_count <= 60, deck.slide_count
        assert deck.width_px == 1280
        n_pic = sum(1 for s in deck.slides for e in s.elements if e.kind == KIND_PICTURE)
        assert n_pic >= 20, f"图片太少（{n_pic}），媒体抽取可能没生效"
        chars = len("".join(s.text_flat for s in deck.slides))
        assert chars > 5000, f"总字数只有 {chars}，文字可能没解析出来"
        empty = [s.index + 1 for s in deck.slides if not s.elements]
        assert not empty, f"这些页一个元素都没有：{empty}"
        # 单词页应当有段落级锚点（一个字一个字讲就是靠它）
        para = sum(1 for s in deck.slides for e in s.elements for _ in e.para_ids)
        assert para > 100, f"段落级锚点只有 {para} 个，解析太粗"
        assert deck.media, "没有抽出任何媒体"
        print(f"[D5] 真实课件《{deck.title[:22]}…》{deck.slide_count} 页 · "
              f"图片 {n_pic} · 段落锚点 {para} · 媒体 {len(deck.media)} · 告警 {len(deck.warnings)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_store_lists_decks() -> None:
    from deck.pptx_ingest import ingest_pptx

    tmp = _tmpdir()
    try:
        root = tmp / "decks"
        deck = ingest_pptx(make_pptx(tmp / "demo.pptx"), deck_id="listed",
                           decks_root=root, download_external=False)
        save_deck(deck, root=root)
        rows = list_decks(root=root)
        assert len(rows) == 1 and rows[0]["deck_id"] == "listed", rows
        assert rows[0]["slides"] == deck.slide_count
        assert "error" not in rows[0]
        print("[D6] decks 列表可用于审核页/CLI")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_copied_deck_dir_warns_about_deck_id() -> None:
    """目录名 ≠ 课件 id 时必须出警告 —— 否则"复制目录做实验"会写坏原课件。

    实测踩过：复制出 `<id>__v2` 目录重新备课 42 页，结果 plan.json 覆盖了原课件的计划
    （课件里的 deck_id 才是真身，所有写盘都按它走）。这一句警告就是那次事故的护栏。
    """
    import io
    from contextlib import redirect_stdout

    from deck.pptx_ingest import ingest_pptx

    tmp = _tmpdir()
    try:
        root = tmp / "decks"
        deck = ingest_pptx(make_pptx(tmp / "demo.pptx"), deck_id="real_id",
                           decks_root=root, download_external=False)
        save_deck(deck, root=root)
        # 复制一份目录，但内容里的 id 还是 real_id
        copy = root / "real_id__v2"
        shutil.copytree(root / "real_id", copy)
        buf = io.StringIO()
        with redirect_stdout(buf):
            got = load_deck("real_id__v2", root=root)
        out = buf.getvalue()
        assert got.deck_id == "real_id", got.deck_id
        assert "不一致" in out and "real_id" in out, "没有提示目录名与课件 id 不一致：\n" + out
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            load_deck("real_id", root=root)
        assert buf2.getvalue().strip() == "", "正常目录不该有警告：" + buf2.getvalue()
        print("[D7] 目录名与课件 id 不一致时会警告（复制目录做实验的护栏）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


ALL = [
    test_ingest_generated_pptx,
    test_geometry_is_sane,
    test_ingest_is_deterministic_and_round_trips,
    test_deck_id_from_chinese_filename,
    test_store_lists_decks,
    test_real_deck_smoke,
    test_copied_deck_dir_warns_about_deck_id,
]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
        except Exception as e:                     # noqa: BLE001 —— 解析器抛异常也要算失败而不是炸掉
            failed += 1
            print(f"  ❌ {fn.__name__} 抛异常：{type(e).__name__}: {e}")
    print(f"\n课件解析结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
