"""Browser-upload workspaces for the Web App.

Only temporary, isolated copies are renamed; never a user's original browser files.
This module is deliberately local-only for now. Public hosting requires separate
user authentication, CSRF/origin controls, rate limits, TLS and storage policy.
"""
from __future__ import annotations

import atexit
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import keywords as kwmod
from .engine import apply_plan, check_conflicts, collect, plan, undo
from .extract import rich_texts, type_of

MAX_FILE = 25 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_FILES = 200
EXPIRE_SECONDS = 3600
ALLOWED_FIELDS = ("kw", "template", "sort", "start", "pad")


@dataclass
class Workspace:
    identifier: str
    base: Path
    files: Path
    logs: Path
    names: set[str] = field(default_factory=set)
    bytes_used: int = 0
    touched: float = field(default_factory=time.monotonic)
    lock: threading.RLock = field(default_factory=threading.RLock)
    proposed: dict | None = None
    last_log: Path | None = None
    applied: bool = False


def safe_relative_name(raw: str) -> Path:
    """Reject traversal, absolute, hidden, Windows and control-character paths."""
    if not isinstance(raw, str) or not raw or len(raw) > 1200:
        raise ValueError("文件路径无效")
    if "\\" in raw or raw.startswith("/") or "\x00" in raw:
        raise ValueError("不允许上传绝对路径或反斜线文件路径")
    parts = raw.split("/")
    if len(parts) > 12 or any(
        p in ("", ".", "..") or p.startswith(".") or
        re.search(r"[\x00-\x1f]", p) or len(p.encode("utf-8")) > 200
        for p in parts
    ):
        raise ValueError("文件路径含非法目录、隐藏文件或过长文件名")
    return Path(*parts)


def validate_options(options: dict) -> dict:
    """Allow only safe-sized, known rename templates."""
    if not isinstance(options, dict):
        raise ValueError("参数必须是对象")
    kw = options.get("kw")
    if not isinstance(kw, str) or not kw.strip() or len(kw) > 100:
        raise ValueError("关键词不能为空或超过 100 字符")
    template = options.get("template") or "{kw}-{n:02}"
    if not isinstance(template, str) or len(template) > 180:
        raise ValueError("模板过长或格式不正确")
    permitted = re.sub(r"\{(?:kw|n|orig|date|type|ext|n:0?\d{1,2}d?)\}", "", template)
    if "{" in permitted or "}" in permitted:
        raise ValueError("模板中包含不支持的占位符")
    for digits in re.findall(r"\{n:0?(\d{1,2})d?\}", template):
        if int(digits) > 12:
            raise ValueError("编号补零宽度最大为 12")
    sort = options.get("sort") or "name"
    if sort not in ("name", "date", "mtime_desc", "size"):
        raise ValueError("排序方式无效")
    return {"kw": kw.strip(), "template": template, "sort": sort}


class WorkspaceStore:
    def __init__(self):
        self._items: dict[str, Workspace] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweeper.start()
        atexit.register(self.close)

    def _sweep_loop(self):
        while not self._stop.wait(60):
            self._cleanup_expired()

    def close(self):
        self._stop.set()
        with self._lock:
            workspaces = list(self._items.values())
            self._items.clear()
        for w in workspaces:
            with w.lock:
                shutil.rmtree(w.base, ignore_errors=True)

    def _cleanup_expired(self):
        now = time.monotonic()
        with self._lock:
            for key, w in list(self._items.items()):
                if now - w.touched <= EXPIRE_SECONDS:
                    continue
                if not w.lock.acquire(blocking=False):
                    continue
                try:
                    if now - w.touched > EXPIRE_SECONDS:
                        self._items.pop(key, None)
                        shutil.rmtree(w.base, ignore_errors=True)
                finally:
                    w.lock.release()

    def create(self) -> dict:
        self._cleanup_expired()
        base = Path(tempfile.mkdtemp(prefix="rnbatch-web-"))
        files = base / "files"
        logs = base / "undo"
        files.mkdir()
        logs.mkdir()
        ident = secrets.token_urlsafe(24)
        w = Workspace(ident, base, files, logs)
        with self._lock:
            self._items[ident] = w
        return {"ok": True, "workspace": ident, "expires_in": EXPIRE_SECONDS,
                "limits": {"files": MAX_FILES, "total_bytes": MAX_TOTAL,
                           "file_bytes": MAX_FILE}}

    def get(self, ident: str) -> Workspace:
        if not isinstance(ident, str) or not ident:
            raise ValueError("缺少 workspace 标识")
        with self._lock:
            w = self._items.get(ident)
            if w is None or time.monotonic() - w.touched > EXPIRE_SECONDS:
                raise ValueError("工作区不存在或已过期，请重新上传")
            w.touched = time.monotonic()
            return w

    def upload(self, ident: str, raw_name: str, body, size: int) -> dict:
        w = self.get(ident)
        rel = safe_relative_name(raw_name)
        if size < 0 or size > MAX_FILE:
            raise ValueError("单个文件超过 25 MB 限制")
        with w.lock:
            if w.applied or w.proposed is not None:
                raise ValueError("已有预览或已执行改名，请创建新工作区")
            if len(w.names) >= MAX_FILES or w.bytes_used + size > MAX_TOTAL:
                raise ValueError("工作区超过文件数量或 100 MB 限制")
            name = rel.as_posix()
            if name in w.names:
                raise ValueError("同名文件已上传，请勿覆盖")
            dst = w.files / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # File names arrive as a plain HTTP path, not as an archive; no symlinks.
            dst.resolve().relative_to(w.files.resolve())
            if dst.exists():
                raise ValueError("目标文件已存在")
            tmp = dst.parent / (".upload-" + uuid.uuid4().hex)
            try:
                remaining = size
                with tmp.open("xb") as output:
                    while remaining:
                        chunk = body.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError("文件上传中断，请重试")
                        output.write(chunk)
                        remaining -= len(chunk)
                os.replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)
            w.names.add(name)
            w.bytes_used += size
            return {"ok": True, "file": name, "count": len(w.names),
                    "total_bytes": w.bytes_used}

    def listing(self, ident: str) -> dict:
        w = self.get(ident)
        with w.lock:
            files = collect([w.files], recursive=True)
            return {"ok": True, "count": len(files), "total_bytes": w.bytes_used,
                    "applied": w.applied, "files": [
                        {"name": str(p.relative_to(w.files)),
                         "type": type_of(p.suffix), "size": p.stat().st_size}
                        for p in files]}

    def extract(self, ident: str) -> dict:
        w = self.get(ident)
        with w.lock:
            files = collect([w.files], recursive=True)
            pool = [p for p in files if p.suffix.lower() in
                    {".docx", ".doc", ".pptx", ".xlsx", ".pdf",
                     ".txt", ".md", ".csv"} or type_of(p.suffix) == "照片"]
            pairs = rich_texts(pool, want=3, limit=2500)
            candidates: dict[str, dict] = {}
            sources = []
            for path, text in pairs:
                options = kwmod.suggest(text)
                sources.append({"name": str(path.relative_to(w.files)),
                                "text": text[:1200]})
                for item in options:
                    old = candidates.get(item["kw"])
                    if old is None or item["score"] > old["score"]:
                        candidates[item["kw"]] = item
            return {"ok": True, "sources": sources,
                    "candidates": sorted(candidates.values(),
                                         key=lambda x: -x["score"])[:10]}

    def _snapshot(self, w: Workspace):
        files = collect([w.files], recursive=True)
        return tuple((str(p.relative_to(w.files)), p.stat().st_size,
                      p.stat().st_mtime_ns) for p in files)

    def preview(self, ident: str, options: dict) -> dict:
        w = self.get(ident)
        opts = validate_options(options)
        with w.lock:
            if w.applied:
                raise ValueError("已执行改名，请先下载或撤销")
            files = collect([w.files], recursive=True)
            if not files:
                raise ValueError("请先上传文件")
            warnings = []
            pairs = plan(files, opts["kw"], opts["template"],
                         sort=opts["sort"], warnings=warnings)
            conflicts = check_conflicts(pairs)
            mark = {str(a) for a, _ in conflicts}
            token = secrets.token_urlsafe(24)
            w.proposed = {"token": token, "snapshot": self._snapshot(w),
                          "pairs": pairs, "options": opts}
            return {"ok": True, "plan_token": token,
                    "count": len(pairs),
                    "changed": sum(a != b for a, b in pairs),
                    "conflicts": len(conflicts), "warnings": warnings,
                    "rows": [{"from": str(a.relative_to(w.files)),
                              "to": str(b.relative_to(w.files)),
                              "changed": a != b, "conflict": str(a) in mark}
                             for a, b in pairs]}

    def apply(self, ident: str, plan_token: str) -> dict:
        w = self.get(ident)
        with w.lock:
            p = w.proposed
            if w.applied or not p or not isinstance(plan_token, str) or not secrets.compare_digest(
                p["token"], plan_token
            ):
                raise ValueError("缺少有效预览确认，请重新预览")
            if self._snapshot(w) != p["snapshot"]:
                w.proposed = None
                raise ValueError("文件已发生变化，请重新预览")
            if check_conflicts(p["pairs"]):
                w.proposed = None
                raise ValueError("目标文件已被占用，请重新预览")
            opts = p["options"]
            log = apply_plan(p["pairs"], opts["kw"], opts["template"],
                             undo_dir=w.logs)
            w.proposed = None
            w.last_log = log
            w.applied = True
            return {"ok": True, "renamed": sum(a != b for a, b in p["pairs"]),
                    "download_ready": True}

    def revert(self, ident: str) -> dict:
        w = self.get(ident)
        with w.lock:
            if not w.applied or w.last_log is None:
                raise ValueError("没有可以撤销的操作")
            restored, skipped = undo(w.last_log, undo_dir=w.logs)
            if skipped == 0:
                w.applied = False
                w.last_log = None
                w.proposed = None
            return {"ok": True, "restored": restored, "skipped": skipped}

    def archive(self, ident: str) -> Path:
        w = self.get(ident)
        with w.lock:
            result = w.base / "download.zip"
            temporary = w.base / ("archive-" + uuid.uuid4().hex + ".zip")
            try:
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in collect([w.files], recursive=True):
                        zf.write(p, arcname=p.relative_to(w.files).as_posix())
                os.replace(temporary, result)
            finally:
                temporary.unlink(missing_ok=True)
            return result

    def delete(self, ident: str) -> dict:
        w = self.get(ident)
        with self._lock:
            self._items.pop(ident, None)
        with w.lock:
            shutil.rmtree(w.base, ignore_errors=True)
        return {"ok": True}


store = WorkspaceStore()
