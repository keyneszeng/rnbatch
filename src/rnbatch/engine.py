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


# ------------------------------------------------------------------ 收集
def collect(paths, recursive: bool = False, only=None) -> list[Path]:
    """展开文件夹 / 文件列表，去掉隐藏文件。"""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            for c in it:
                if c.is_file() and not c.name.startswith("."):
                    out.append(c)
    if only:
        wanted = {"." + str(e).lower().lstrip(".") for e in only}
        out = [f for f in out if f.suffix.lower() in wanted]
    return sorted(set(out), key=lambda x: str(x))


# ------------------------------------------------------------------ 模板
def sanitize(s: str) -> str:
    """去掉文件系统非法字符，统一 NFC（macOS 中文文件名常见 NFD）。"""
    s = unicodedata.normalize("NFC", s)
    s = ILLEGAL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


def build_name(template: str, kw: str, idx: int, width: int,
               orig: str, date_str: str, ext: str, label: str) -> str:
    """渲染模板。{n:03} 优先于 {n}（后者用自动宽度）。"""
    name = template
    for m in re.finditer(r"\{n:0?(\d+)d?\}", name):
        name = name.replace(m.group(0), str(idx).zfill(int(m.group(1))))
    name = name.replace("{n}", str(idx).zfill(width))
    name = name.replace("{kw}", kw)
    name = name.replace("{orig}", orig)
    name = name.replace("{date}", date_str)
    name = name.replace("{type}", label)
    name = name.replace("{ext}", ext.lstrip("."))
    return sanitize(name)


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
         pad: int = 0, sort: str = "name") -> list[tuple[Path, Path]]:
    """生成 (原路径, 新路径) 列表。纯计算，不碰文件。"""
    kw = sanitize(kw)
    if not kw:
        raise ValueError("关键词为空，拒绝执行。")
    files = _sorted(list(files), sort)
    width = pad or max(2, len(str(len(files) + start - 1)))
    has_ext_slot = "{ext}" in template

    taken: set[str] = set()
    pairs: list[tuple[Path, Path]] = []
    for i, p in enumerate(files):
        idx = start + i
        ext = p.suffix
        base = build_name(template, kw, idx, width, p.stem,
                          date_of(p), ext, type_of(ext))
        if not base:
            base = f"{kw}-{str(idx).zfill(width)}"
        candidate = base if has_ext_slot else base + ext
        if candidate.lower() in taken:          # 同批次内重名 → 追加 -2, -3…
            k = 2
            while True:
                alt = f"{base}-{k}" + ("" if has_ext_slot else ext)
                if alt.lower() not in taken:
                    candidate = alt
                    break
                k += 1
        taken.add(candidate.lower())
        pairs.append((p, p.with_name(candidate)))
    return pairs


# ------------------------------------------------------------------ 执行
def apply_plan(pairs, keyword: str = "", template: str = "") -> Path | None:
    """执行改名，返回 undo 日志路径；无改动返回 None。失败自动回滚。"""
    todo = [(a, b) for a, b in pairs if a.name != b.name]
    if not todo:
        return None

    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    log_path = UNDO_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.json"
    records: list[dict] = []
    staged: list[tuple[Path, Path, Path]] = []
    try:
        for a, b in todo:
            tmp = a.with_name(f".rnbatch.tmp.{uuid.uuid4().hex[:10]}")
            os.rename(a, tmp)
            staged.append((tmp, a, b))
        for tmp, a, b in staged:
            if b.exists():
                raise FileExistsError(f"目标已存在: {b}")
            os.rename(tmp, b)
            records.append({"from": str(a), "to": str(b)})
    except Exception:
        for tmp, a, _b in staged:                # 尽力回滚
            if tmp.exists():
                try:
                    os.rename(tmp, a)
                except Exception:
                    pass
        raise

    log_path.write_text(json.dumps({
        "time": datetime.now().isoformat(timespec="seconds"),
        "keyword": keyword,
        "template": template,
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return log_path


def list_undo_logs() -> list[Path]:
    if not UNDO_DIR.exists():
        return []
    return sorted(UNDO_DIR.glob("*.json"))


def undo(log: str | Path | None = None) -> tuple[int, int]:
    """回滚。返回 (成功数, 跳过数)。log 为空则撤销最近一次。"""
    logs = list_undo_logs()
    if not logs:
        raise FileNotFoundError("没有可撤销的记录。")
    target = Path(log) if log else logs[-1]
    if not target.is_absolute() and not target.exists():
        target = UNDO_DIR / target
    if not target.exists():
        raise FileNotFoundError(f"找不到日志: {log}")

    data = json.loads(target.read_text(encoding="utf-8"))
    ok = skipped = 0
    for rec in reversed(data["records"]):
        src, dst = Path(rec["to"]), Path(rec["from"])
        if not src.exists() or dst.exists():
            skipped += 1
            continue
        os.rename(src, dst)
        ok += 1
    if ok and not skipped:
        target.rename(target.with_suffix(".json.done"))
    return ok, skipped
