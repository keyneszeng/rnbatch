"""改名引擎：模板渲染、冲突消解、两阶段改名、撤销。

三条铁律：
1. 永不静默改名 —— 必须先出计划（plan）让人看。
2. 永不不可逆 —— 每次 apply 写 undo 日志。
3. 永不互相覆盖 —— 两阶段改名（原名→临时名→终名）。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from .extract import type_of

STATE_DIR = Path(os.environ.get("RNBATCH_HOME", Path.home() / ".rnbatch"))
UNDO_DIR = STATE_DIR / "undo"

ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

# 单个文件名的字节上限。
# 理论上 APFS / ext4 是 255 字节，但备份工具、同步盘、加密层都会在这个名字上
# 再拼后缀，实测 240 字节就开始报 ENAMETOOLONG。取 200 留足余量，
# 200 字节 ≈ 66 个汉字，正常命名远远用不到。
MAX_NAME_BYTES = 200


class ConflictError(RuntimeError):
    """目标文件名已被目录里的其他文件占用。"""

    def __init__(self, conflicts):
        self.conflicts = conflicts
        detail = "、".join(b.name for _a, b in conflicts[:5])
        more = f" 等 {len(conflicts)} 个" if len(conflicts) > 5 else ""
        super().__init__(f"目标名已被占用：{detail}{more}")


# ------------------------------------------------------------------ 收集
def _norm_only(only) -> set[str] | None:
    """only 既接受 ['pdf','jpg'] 也接受 'pdf,jpg'，避免调用方传错就静默失效。

    传了完全无法解释的类型（数字、字典…）时当作「不过滤」而不是抛异常 ——
    过滤条件写错不该让整次改名失败。
    """
    if not only:
        return None
    if isinstance(only, str):
        items = only.split(",")
    elif isinstance(only, (list, tuple, set, frozenset)):
        items = list(only)
    else:
        return None
    out = {"." + str(e).strip().lower().lstrip(".") for e in items if str(e).strip()}
    return out or None


def collect(paths, recursive: bool = False, only=None,
            include_symlinks: bool = False) -> list[Path]:
    """展开文件夹 / 文件列表，去掉隐藏文件。

    默认跳过符号链接：改名链接本身几乎从不是想要的结果，而且一旦改名，
    链接指向的原文件也跟着改名，链接当场断掉（数据访问失败）。
    """
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_symlink() and not include_symlinks:
            continue
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            for c in it:
                if not c.is_file() or c.name.startswith("."):
                    continue
                if c.is_symlink() and not include_symlinks:
                    continue
                out.append(c)
    wanted = _norm_only(only)
    if wanted:
        out = [f for f in out if f.suffix.lower() in wanted]
    return sorted(set(out), key=lambda x: str(x))


def find_symlinks(paths, recursive: bool = False) -> list[Path]:
    """列出会被跳过的符号链接，用于提示用户。"""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_symlink():
            out.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            for c in it:
                if c.is_symlink():
                    out.append(c)
    return sorted(set(out), key=lambda x: str(x))


def relative_label(p: Path, roots) -> str:
    """递归改名时，光看文件名分不清 a.txt 到底在哪个子目录 —— 给个相对路径。"""
    for r in roots:
        r = Path(r).expanduser()
        base = r if r.is_dir() else r.parent
        try:
            return str(p.relative_to(base))
        except ValueError:
            continue
    return p.name


# ------------------------------------------------------------------ 模板
def sanitize(s: str) -> str:
    """去掉文件系统非法字符，统一 NFC（macOS 中文文件名常见 NFD）。"""
    s = unicodedata.normalize("NFC", s)
    s = ILLEGAL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


def build_name(template: str, kw: str, idx: int, width: int,
               orig: str, date_str: str, ext: str, label: str) -> str:
    """渲染模板。{n:03} 优先于 {n}（后者用自动宽度）。

    {ext} 特殊处理：自动补上点号，并吞掉它前面的分隔符。
    所以 {kw}-{n}.{ext} 和 {kw}-{n}-{ext} 都会得到 星海-01.jpg，
    而不是 星海-01-jpg（丢失扩展名，文件双击打不开）。
    """
    name = template
    for m in re.finditer(r"\{n:0?(\d+)d?\}", name):
        name = name.replace(m.group(0), str(idx).zfill(int(m.group(1))))
    name = name.replace("{n}", str(idx).zfill(width))
    name = name.replace("{kw}", kw)
    name = name.replace("{orig}", orig)
    name = name.replace("{date}", date_str)
    name = name.replace("{type}", label)

    bare = ext.lstrip(".")
    if "{ext}" in name:
        if bare:
            name = re.sub(r"[-_. ]?\{ext\}", "." + bare, name)
        else:
            name = re.sub(r"[-_. ]?\{ext\}", "", name)

    return sanitize(name)


def ensure_ext(name: str, ext: str) -> str:
    """兜底：无论模板怎么写，最终文件名必须带上原扩展名。"""
    if not ext:
        return name
    if name.lower().endswith(ext.lower()):
        return name
    return name + ext


def fit_length(name: str, ext: str = "") -> str:
    """按 UTF-8 字节截断，防止 'File name too long'（整批改名失败）。"""
    ext_b = len(ext.encode("utf-8"))
    budget = MAX_NAME_BYTES - ext_b
    if budget < 1:
        budget = MAX_NAME_BYTES
    while len(name.encode("utf-8")) > budget and name:
        name = name[:-1]
    name = name.rstrip(" .")
    return name or "renamed"


def date_of(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
    except Exception:
        return "00000000"


SORTS = ("name", "date", "mtime_desc", "size")


def _sorted(files: list[Path], how: str) -> list[Path]:
    if how == "date":
        return sorted(files, key=lambda p: (p.stat().st_mtime, str(p)))
    if how == "size":
        return sorted(files, key=lambda p: (p.stat().st_size, str(p)))
    if how == "mtime_desc":
        return sorted(files, key=lambda p: (-p.stat().st_mtime, str(p)))
    return sorted(files, key=lambda p: str(p).lower())


def plan(files, kw: str, template: str = "{kw}-{n}", start: int = 1,
         pad: int = 0, sort: str = "name", warnings: list | None = None
         ) -> list[tuple[Path, Path]]:
    """生成 (原路径, 新路径) 列表。纯计算，不碰文件。

    传一个 list 给 warnings，会把「名字太长被截断」这类提醒塞进去，
    免得用户莫名其妙发现文件名少了一截。
    """
    kw = sanitize(kw)
    if not kw:
        raise ValueError("关键词为空，拒绝执行。")
    files = _sorted(list(files), sort)
    width = pad or max(2, len(str(len(files) + start - 1)))

    taken: set[str] = set()
    pairs: list[tuple[Path, Path]] = []
    for i, p in enumerate(files):
        idx = start + i
        ext = p.suffix
        base = build_name(template, kw, idx, width, p.stem,
                          date_of(p), ext, type_of(ext))
        if not base:
            base = f"{kw}-{str(idx).zfill(width)}"
        name = ensure_ext(base, ext)

        # 拆成 主名 + 扩展名，方便截断和去重时插在正确位置
        root = name[: len(name) - len(ext)] if ext and name.lower().endswith(ext.lower()) else name
        fitted = fit_length(root, ext)
        if warnings is not None and fitted != root:
            warnings.append({
                "type": "truncated",
                "file": p.name,
                "kept_bytes": len(fitted.encode("utf-8")) + len(ext.encode("utf-8")),
                "limit": MAX_NAME_BYTES,
            })
        root = fitted
        candidate = root + ext

        if candidate.lower() in taken:          # 同批次内重名 → 追加 -2, -3…
            k = 2
            while True:
                alt = fit_length(f"{root}-{k}", ext) + ext
                if alt.lower() not in taken:
                    candidate = alt
                    break
                k += 1
        taken.add(candidate.lower())
        pairs.append((p, p.with_name(candidate)))
    return pairs


def check_conflicts(pairs) -> list[tuple[Path, Path]]:
    """找出「目标名已被目录里其他文件占用」的项。

    占用者如果在本次待改名单里就不算冲突 —— 两阶段改名会先把它们挪走。
    预览阶段就该提示，不能让用户点了执行才炸。
    """
    sources = set()
    for a, _b in pairs:
        try:
            sources.add(a.resolve())
        except OSError:
            sources.add(a.absolute())
    out: list[tuple[Path, Path]] = []
    for a, b in pairs:
        if a.name == b.name:
            continue
        try:
            if not b.exists():
                continue
            try:
                key = b.resolve()
            except OSError:
                key = b.absolute()
            if key not in sources:
                out.append((a, b))
        except OSError:
            continue
    return out


# ------------------------------------------------------------------ 执行
def _restore_staged(staged, completed) -> list[str]:
    """失败时逆转两阶段操作，避免 A→B / B→A 互换造成覆盖。

    staged / completed 的每条记录为 (original, destination, temporary)。
    先把已落到最终名的文件全部重新移回临时名，再恢复所有原名。
    无法安全恢复时保留文件和 .pending 日志，不强行覆盖任何路径。
    """
    errors: list[str] = []
    for _a, b, tmp in reversed(completed):
        if not b.exists() or tmp.exists():
            errors.append(f"无法回收目标文件：{b}")
            continue
        try:
            os.rename(b, tmp)
        except OSError as e:
            errors.append(f"无法回收 {b}: {e}")

    for a, _b, tmp in reversed(staged):
        if not tmp.exists():
            if not a.exists():
                errors.append(f"找不到原文件或临时文件：{a}")
            continue
        if a.exists():
            errors.append(f"原路径被占用，拒绝覆盖：{a}")
            continue
        try:
            os.rename(tmp, a)
        except OSError as e:
            errors.append(f"无法恢复 {a}: {e}")
    return errors


def apply_plan(pairs, keyword: str = "", template: str = "") -> Path | None:
    """先持久化恢复映射，再执行两阶段改名；成功后发布撤销日志。

    进程内异常会尽力完整回滚。SIGKILL/断电后，保留 .pending 日志
    和可能的 .rnbatch.tmp.* 文件供人工修复；暂不声称跨崩溃原子性。
    """
    todo = [(a, b) for a, b in pairs if a.name != b.name]
    if not todo:
        return None

    conflicts = check_conflicts(pairs)
    if conflicts:
        raise ConflictError(conflicts)

    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    ident = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    log_path = UNDO_DIR / f"{ident}.json"
    journal_path = UNDO_DIR / f"{ident}.pending"
    operations = [
        (a, b, a.with_name(f".rnbatch.tmp.{a.stem[:40]}.{uuid.uuid4().hex[:8]}"))
        for a, b in todo
    ]
    records = [
        {"from": str(a), "to": str(b), "tmp": str(tmp)}
        for a, b, tmp in operations
    ]

    # 所有映射先写到磁盘，再触碰任何用户文件。
    # .pending 不会出现在正常 undo 日志列表中；成功后原子改名为 .json。
    try:
        with journal_path.open("x", encoding="utf-8") as fh:
            json.dump({
                "time": datetime.now().isoformat(timespec="seconds"),
                "keyword": keyword,
                "template": template,
                "status": "pending",
                "records": records,
            }, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        journal_path.unlink(missing_ok=True)
        raise

    staged: list[tuple[Path, Path, Path]] = []
    completed: list[tuple[Path, Path, Path]] = []
    try:
        for a, b, tmp in operations:
            if tmp.exists():
                raise FileExistsError(f"临时文件名已存在：{tmp}")
            os.rename(a, tmp)
            staged.append((a, b, tmp))
        for a, b, tmp in staged:
            if b.exists():
                raise FileExistsError(f"目标已存在: {b}")
            os.rename(tmp, b)
            completed.append((a, b, tmp))

        os.replace(journal_path, log_path)
        return log_path
    except Exception as cause:
        failures = _restore_staged(staged, completed)
        if failures:
            raise RuntimeError(
                "批量改名失败，部分文件未能自动恢复；请保留现场，"
                f"按恢复日志 {journal_path} 处理。"
                + "；".join(failures)
            ) from cause
        journal_path.unlink(missing_ok=True)
        raise


def stale_temp_files(directory: Path) -> list[Path]:
    """找出上次改名中途被杀留下的临时文件。"""
    try:
        return sorted(directory.glob(".rnbatch.tmp.*"))
    except OSError:
        return []


def list_undo_logs() -> list[Path]:
    if not UNDO_DIR.exists():
        return []
    return sorted(UNDO_DIR.glob("*.json"))


def load_log(p: Path) -> dict | None:
    """读一份撤销日志；损坏或格式不对返回 None，不让调用方崩在 json 解析上。"""
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return None
    return data


def undo(log: str | Path | None = None) -> tuple[int, int]:
    """安全撤销：先全移到临时名，再恢复原名，支持互换和循环改名。

    对缺失的文件、被新文件占用的原路径采取跳过策略，不覆盖已有文件。
    """
    logs = list_undo_logs()
    if not logs:
        raise FileNotFoundError("没有可撤销的记录。")

    if log:
        target = Path(log)
        if not target.exists():
            target = UNDO_DIR / str(log)
        if not target.exists():
            raise FileNotFoundError(f"找不到日志: {log}")
        data = load_log(target)
        if data is None:
            raise ValueError(f"撤销日志已损坏，无法还原: {target.name}")
    else:
        target, data = None, None
        for p in reversed(logs):
            d = load_log(p)
            if d is not None:
                target, data = p, d
                break
        if data is None:
            raise ValueError("最近的撤销日志都已损坏，无法还原。")

    # 兼容旧版本不含 status 的日志，拒绝非完成状态的事务。
    if data.get("status", "completed") != "completed":
        raise ValueError(f"日志尚未完成，禁止直接撤销：{target.name}")

    entries = [(Path(rec["to"]), Path(rec["from"])) for rec in reversed(data["records"])]
    active = [(src, dst) for src, dst in entries if src.exists()]
    skipped = len(entries) - len(active)

    # 迭代排除被其他文件占用的原目标，并处理连锁依赖。
    # 如果 dst 同时是本批次另一个 src，暂时允许；两阶段会先腾出它。
    while True:
        sources = {src for src, _dst in active}
        safe = [(src, dst) for src, dst in active
                if not dst.exists() or dst in sources]
        if len(safe) == len(active):
            break
        skipped += len(active) - len(safe)
        active = safe

    staged: list[tuple[Path, Path, Path]] = []
    completed: list[tuple[Path, Path, Path]] = []
    try:
        for src, dst in active:
            tmp = src.with_name(f".rnbatch.tmp.undo.{uuid.uuid4().hex[:12]}")
            if tmp.exists():
                raise FileExistsError(f"临时文件名已存在：{tmp}")
            os.rename(src, tmp)
            staged.append((src, dst, tmp))
        for src, dst, tmp in staged:
            if dst.exists():
                raise FileExistsError(f"撤销目标已被占用：{dst}")
            os.rename(tmp, dst)
            completed.append((src, dst, tmp))
    except Exception as cause:
        failures = _restore_staged(staged, completed)
        if failures:
            raise RuntimeError(
                "撤销中途失败且未能完全恢复撤销前状态，请检查临时文件；"
                + "；".join(failures)
            ) from cause
        raise

    ok = len(active)
    if ok and not skipped:
        target.rename(target.with_suffix(".json.done"))
    return ok, skipped
