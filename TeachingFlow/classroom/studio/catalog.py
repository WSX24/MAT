# -*- coding: utf-8 -*-
"""课程库的读侧：把 TeachingFlow/runs/ 上的交付件扫成「课程 → 章 → 单元」。

为什么由课堂来扫、而不是出片时顺手写库
--------------------------------------
出片（TeachingFlow/flow.py）与课堂是两个进程，中间还隔着时间：一份上周的 run
今天才被打开，或者有人手工把交付件拷进 runs/。所以课程库的事实源只能是**盘上的
目录布局**，每次「重新扫描」都重读一遍。本模块因此只读盘，一个字都不往盘上写
（写库是 store 层的事）。

id 的口径（upsert 的前提）
--------------------------
课程 id = sha1(课程目录相对 runs 父目录的路径)[:12]，
单元 id = sha1(course_id/章/文件名)[:12]。
用**相对**路径而不是绝对路径：同一份 run 换个挂载点搬过来，id 必须还是那个 id ——
否则「重新扫描」会把同一门课再插一遍，学生的进度跟着丢。反过来，改名就换了 id，
这是对的：改完名它就是另一份交付件。

命名的唯一出处是 core/naming.py
------------------------------
章目录 第N章-章名、单元文件 第M单元-单元名.pptx 由 core/naming.py 造。
本模块要的是反向操作（把名字读回 序号 + 标题），所以：
  - **造**名字只用 naming，这里绝不自己拼；
  - **读**名字用下面三条正则，读完再用 naming 重建一遍比对，对不上就记一句
    警告 —— 手改过名字的目录只有在"说出来"之后才可能被修好，而直接崩掉或
    静默跳过都不行。

为什么返回的是 list 的子类
--------------------------
契约要的是 list[ScannedCourse]，而上层日志还想知道「跳过几门、为什么跳过」。
换成包装对象会逼调用方多解一层包；list 子类对调用方**就是**一个 list
（len / for / 切片 / json 序列化都对），却把 ignored 与 warnings 一并带了出来。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: 课程目录名：标题_HHMMSS（core/naming.py 里 runs/<日期>/<用户原话>_<时分秒>/）
_COURSE_DIR = re.compile(r"^(?P<title>.+)_(?P<stamp>\d{6})$")

#: 章目录名，与 core/naming.py::chapter_dirname 同一口径（第1章-散文阅读入门）
_CHAPTER_DIR = re.compile(r"^第(?P<no>\d+)章(?:-(?P<title>.+))?$")

#: 单元文件名（不含后缀），与 core/naming.py::unit_label 同一口径
_UNIT_FILE = re.compile(r"^第(?P<no>\d+)单元-(?P<title>.+)$")

#: manifest.json 里 subject 的两个取处：新 run 在 spec 里，老 run 只在 args 里
_SUBJECT_PATHS = (("spec", "subject"), ("args", "subject"))

#: core/naming 的缓存：None = 还没试过，False = 试过了但拿不到，其余 = 模块本身
_NAMING = None


@dataclass
class ScannedUnit:
    """一个单元 = 一份交付件 pptx（章内的一个格）。"""

    id: str
    title: str
    chapter: str
    order_index: int
    pptx_path: str
    #: 扫描时顺手读出来的页数；读不动就是 None（列表里显示"—"，不该让扫描崩）
    slides: int | None = None
    #: 讲这页要多久 —— **扫描阶段一律 None**：没解析过就估不出时长，
    #: 猜一个数只会让课程库显示一个之后再也不会变的假值。备课后由 materialize 填。
    est_sec: float | None = None


@dataclass
class ScannedCourse:
    """一门课 = 一个 run 目录里能上课的那几份 pptx。"""

    id: str
    title: str
    subject: str
    source: str
    run_dir: str
    units: list[ScannedUnit] = field(default_factory=list)


class ScanResult(list):
    """scan_runs 的返回：**就是一个 list[ScannedCourse]**，另带扫描账本。

    ignored 是"看着像课程、但没有交付件所以没产出"的目录数（mock 运行是常态）；
    warnings 是给人看的那些句子。两者都只供上层写日志，不参与入库。
    """

    def __init__(self, courses=(), *, runs_root="", warnings=(), ignored: int = 0):
        super().__init__(courses)
        self.runs_root = str(runs_root)
        self.warnings: list[str] = list(warnings)
        self.ignored = int(ignored)


# ===========================================================================
# 对外：扫描
# ===========================================================================

def scan_runs(runs_root: Path) -> list[ScannedCourse]:
    """扫 runs/<YYYYMMDD>/<课程目录>/，返回**有交付件**的课程。

    只认有 pptx 的课程目录：mock 运行在盘上留着一整套 prompt/result 的 md/json，
    却没有一份 pptx —— 把它算成课程，课程库里就会出现一排点进去一片空白的卡片。
    被跳过的数目与原因放 ScanResult.ignored / warnings。

    两种布局都认：runs/<日期>/<课程>/（现在）与 runs/<课程>/（更早的口径）。
    绝不抛异常：runs 根目录不存在、某个课程目录读崩、manifest 是坏的，
    都只是"这一门没读到"，不该让整次扫描失败 —— 重新扫描是页面上的一个按钮，
    它失败一次就等于课程库白屏。
    """
    root = Path(runs_root)
    warnings: list[str] = []
    ignored = 0
    courses: list[ScannedCourse] = []
    if not root.is_dir():
        return ScanResult([], runs_root=root, ignored=0, warnings=[
            f"runs 根目录不存在：{root} —— 课程库是空的（先出一门课，或确认路径）"])

    for course_dir in _course_dirs(root, warnings):
        try:
            course = _scan_course(course_dir, root, warnings)
        except Exception as exc:      # 单个目录读崩只是少一门课
            warnings.append(f"{course_dir.name}：扫描出错 {type(exc).__name__}: {exc}"
                            " —— 已跳过这个目录")
            course = None
        if course is None:
            ignored += 1
        else:
            courses.append(course)
    return ScanResult(courses, runs_root=root, warnings=warnings, ignored=ignored)


def unit_from_pptx(pptx: Path, course_id: str, title: str, chapter: str, order: int) -> ScannedUnit:
    """一份 pptx → 一个 ScannedUnit（**不解析**，只数页数）。

    为什么这里不读 deck.json / 不备课：扫描必须秒级返回，而解析一份真实课件要几十秒。
    页数是列表上"3 页 / 12 页"那一栏，用 python-pptx 打开数一遍最便宜；
    打开失败（文件坏了、被占着）就填 None —— 扫描的职责是"有什么"，不是"能不能解析"。
    """
    path = Path(pptx)
    return ScannedUnit(
        id=_stable_id(course_id, chapter, path.name),
        title=str(title or path.stem),
        chapter=str(chapter or ""),
        order_index=int(order),
        pptx_path=str(path),
        slides=_count_slides(path),
        est_sec=None,
    )


# ===========================================================================
# 内部：目录遍历
# ===========================================================================

def _course_dirs(root: Path, warnings: list[str]) -> list[Path]:
    """找出所有"课程目录"，两代布局都认。

    判据是"这一层长得像课程吗"（里面有章目录或 manifest.json），而不是"名字是不是
    八位日期"：日期目录的口径改过一次，按名字认会把整棵树漏掉。
    """
    out: list[Path] = []
    for child in _subdirs(root, warnings):
        if _looks_like_course(child):
            out.append(child)
        else:
            out.extend(_subdirs(child, warnings))
    return out


def _subdirs(base: Path, warnings: list[str]) -> list[Path]:
    """子目录（跳过隐藏目录）；读不动只记一句，不抛。"""
    try:
        return [p for p in sorted(base.iterdir())
                if p.is_dir() and not p.name.startswith(".")]
    except OSError as exc:
        warnings.append(f"列不出 {base} 下的目录（{type(exc).__name__}: {exc}）—— 已跳过")
        return []


def _looks_like_course(d: Path) -> bool:
    """这一层是课程目录吗：有章目录，或者有 manifest.json（mock 运行也有）。"""
    if (d / "manifest.json").is_file():
        return True
    try:
        return any(c.is_dir() and _CHAPTER_DIR.match(c.name) for c in d.iterdir())
    except OSError:
        return False


def _chapters(course_dir: Path, warnings: list[str]) -> list[tuple[int, Path, str]]:
    """课程目录 → [(章序, 章目录, 章标题)]，按章序排。

    不是章目录的子目录**默认不理**：一个 run 里还有 LLM返回、05_ppt_ch01-u1
    这类正常的中间产物目录，全报一句会把警告栏淹掉。只有当它**确实躺着 pptx**
    时才说话 —— 那时它是"没被认出来的交付件"，是真问题。
    """
    found: list[tuple[int, Path, str]] = []
    for child in _subdirs(course_dir, warnings):
        m = _CHAPTER_DIR.match(child.name)
        if m is None:
            stray = sorted(p.name for p in _pptx_files(child))
            if stray:
                warnings.append(f"{child.name}/ 下有 {len(stray)} 份 pptx，但目录名不是"
                                "「第N章-章名」口径 —— 已忽略（旧版布局或手改过名字）")
            continue
        no = int(m.group("no"))
        title = (m.group("title") or "").strip()
        _check_label(child.name, _canonical_chapter(no, title), "章目录", warnings)
        found.append((no, child, title))
    found.sort(key=lambda t: (t[0], t[1].name))
    return found


def _units_of(chapter_dir: Path, warnings: list[str]) -> list[tuple[int, Path, str]]:
    """章目录 → [(单元序, pptx, 单元标题)]，按单元序排。"""
    found: list[tuple[int, Path, str]] = []
    for f in _pptx_files(chapter_dir):
        m = _UNIT_FILE.match(f.stem)
        if m is None:
            warnings.append(f"{chapter_dir.name}/{f.name} 不是「第M单元-单元名」口径"
                            " —— 已忽略（不猜它是哪一节）")
            continue
        no = int(m.group("no"))
        title = (m.group("title") or "").strip() or f.stem
        _check_label(f.name, _canonical_unit(no, title), "单元文件", warnings)
        found.append((no, f, title))
    found.sort(key=lambda t: (t[0], t[1].name))
    return found


def _pptx_files(base: Path) -> list[Path]:
    """目录里**直属**的 pptx（不递归：交付件就躺在章目录下，递归只会把旧版产物也算进来）。"""
    try:
        return [p for p in base.glob("*.pptx")
                if p.is_file() and not p.name.startswith("~$")]   # ~$ = Office 打开时的锁文件
    except OSError:
        return []


# ===========================================================================
# 内部：单个课程目录
# ===========================================================================

def _scan_course(course_dir: Path, runs_root: Path, warnings: list[str]) -> ScannedCourse | None:
    """一个课程目录 → ScannedCourse；没有交付件就返回 None（调用方记 ignored）。"""
    dir_title = _dir_title(course_dir, warnings)
    title, subject = _manifest_facts(course_dir, warnings)
    course_id = _stable_id(_rel_key(course_dir, runs_root))

    units: list[ScannedUnit] = []
    for _ch_no, ch_dir, ch_title in _chapters(course_dir, warnings):
        for _u_no, pptx, u_title in _units_of(ch_dir, warnings):
            # order_index 是**全课连续的**：上层按它排一遍就能同时还原章序与章内单元序，
            # 不必再记一份章序（章序已折进这里的扫描顺序）。
            units.append(unit_from_pptx(pptx, course_id=course_id, title=u_title,
                                        chapter=ch_title, order=len(units)))
    if not units:
        warnings.append(f"{_rel_key(course_dir, runs_root)}：目录里没有"
                        "「第N章-章名/第M单元-单元名.pptx」—— 已跳过（mock 运行？）")
        return None
    return ScannedCourse(id=course_id,
                         title=title or dir_title,
                         subject=subject,
                         source="run",
                         run_dir=str(course_dir),
                         units=units)


def _dir_title(course_dir: Path, warnings: list[str]) -> str:
    """目录名里的标题（去掉 _HHMMSS 尾）；不合口径就整名当标题并说一句。"""
    m = _COURSE_DIR.match(course_dir.name)
    if m is not None:
        return m.group("title").strip()
    warnings.append(f"课程目录名 {course_dir.name!r} 不带「_时分秒」后缀 —— 按整个目录名当标题")
    return course_dir.name


def _manifest_facts(course_dir: Path, warnings: list[str]) -> tuple[str, str]:
    """manifest.json → (标题, 科目)；缺字段留空串。

    标题优先取 input（用户当初那一句原话，出片链路的真名），没有就让调用方
    退回目录名；科目取 spec.subject，老 run 只在 args.subject 里有。
    读不动只记一句 —— 一份坏 manifest 不该让课程消失，它只是少了个更准的名字。
    """
    path = course_dir / "manifest.json"
    if not path.is_file():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(f"{course_dir.name}/manifest.json 读不动"
                        f"（{type(exc).__name__}: {exc}）—— 标题改用目录名")
        return "", ""
    if not isinstance(data, dict):
        warnings.append(f"{course_dir.name}/manifest.json 不是对象 —— 标题改用目录名")
        return "", ""
    title = str(data.get("input") or "").strip()
    subject = ""
    for head, key in _SUBJECT_PATHS:
        block = data.get(head)
        if isinstance(block, dict):
            subject = str(block.get(key) or "").strip()
            if subject:
                break
    return title, subject


# ===========================================================================
# 内部：id / 名字
# ===========================================================================

def _stable_id(*parts: str) -> str:
    """把若干个标识串折成一个稳定 id。

    用 sha1 而不是自增号：扫描是"从盘上重读"，没有账本可依，唯一能算出来的
    稳定量就是路径本身。12 位十六进制（48 位）对课程这个量级足够，短一半便于日志。
    """
    raw = "/".join(str(p).replace("\\", "/").strip("/") for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _rel_key(target: Path, runs_root: Path) -> str:
    """相对路径键：先相对 runs 的**父目录**（这样键里带着 runs/<日期>/），再退一级。"""
    for base in (runs_root.parent, runs_root):
        try:
            return target.relative_to(base).as_posix()
        except ValueError:
            continue
    return target.name


def _canonical_chapter(no: int, title: str) -> str | None:
    naming = _naming()
    if naming is None:
        return None
    return naming.chapter_dirname({"chapter_order": no, "chapter_title": title})


def _canonical_unit(no: int, title: str) -> str | None:
    naming = _naming()
    if naming is None:
        return None
    return f"{naming.unit_label({'unit_index': no - 1, 'unit_title': title})}.pptx"


def _check_label(on_disk: str, canonical: str | None, what: str, warnings: list[str]) -> None:
    """盘上的名字与 naming 重建的名字比对；对不上就记一句（已按盘上的名字读）。"""
    if canonical is not None and canonical != on_disk:
        warnings.append(f"{what} {on_disk!r} 与命名口径 {canonical!r} 对不上"
                        " —— 已按盘上的名字读")


def _naming():
    """core.naming —— 命名口径的唯一出处，拿不到返回 None（只试一次）。

    怎么拿到它是个有代价的选择，值得写清楚：
      - import core.naming 要求 TeachingFlow/ 在 sys.path 上，而本模块会被
        app.py 导入，那时的 sys.path 长什么样谁也说不准；
      - 为此在导入期往 sys.path 里加一条，等于让"课堂导不导得到自己的模块"
        依赖一个库的副作用（TeachingFlow/ 下还有 tools、fixtures 这类容易撞的名字）。
    所以按**文件位置**加载（naming.py 只依赖 re 与 pathlib，脱离包也能跑），
    加载一次就缓存：既不碰 sys.path，也不给导入顺序留坑。

    拿不到时只是少做一次"名字对不对得上"的比对 —— 反正是**读**名字，
    造名字仍然只有 naming 一个出处。
    """
    global _NAMING
    if _NAMING is not False:
        return _NAMING or None
    try:
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "core" / "naming.py"
        spec = importlib.util.spec_from_file_location("_classroom_core_naming", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _NAMING = module
    except Exception:          # 文件不在、加载器不给面子 —— 都只是"少一次比对"
        _NAMING = False
    return _NAMING or None


def _count_slides(pptx: Path) -> int | None:
    """数 pptx 有几页（打不开给 None）。懒导入：没有 python-pptx 也能列课程。"""
    try:
        from pptx import Presentation

        return len(Presentation(str(pptx)).slides)
    except Exception:
        return None
