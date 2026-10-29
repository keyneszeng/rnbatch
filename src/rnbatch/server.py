"""本地 Web 应用：把 rnbatch 做成"界面是网页、文件不出本机"的独立应用。

为什么不是云端网页版：
  文件是用户的核心资产。真正的痛点不是"没有界面"，是"不敢把文件传上去"。
  所以后端跑在 127.0.0.1，浏览器只当画布用 —— 界面体验和网页版一样，
  但一个字节都不出机器。这一层也是将来包成 Tauri/Electron 桌面版的地基。

顺带：所有 /api/* 就是一套干净的 HTTP JSON 接口，不支持 MCP 的 agent 可以直接调。
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import keywords as kwmod
from .engine import (ConflictError, apply_plan, check_conflicts, collect,
                     find_symlinks, list_undo_logs, load_log, plan,
                     relative_label, sanitize, stale_temp_files, undo)
from .extract import extract_text, rich_texts, type_of
from .web_workspace import MAX_FILE, store as browser_store

WEB_DIR = Path(__file__).parent / "web"


# ------------------------------------------------------------------ 工具
def pick_folder(prompt: str = "选择要重命名的文件夹") -> str | None:
    """调用 macOS 原生文件夹选择器。非 macOS 返回 None。"""
    if sys.platform != "darwin":
        return None
    script = f'POSIX path of (choose folder with prompt "{prompt}")'
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:          # 用户点了取消
        return None
    return r.stdout.decode("utf-8", "replace").strip().rstrip("/") or None


def describe(p: Path) -> dict:
    st = p.stat()
    return {
        "name": p.name,
        "ext": p.suffix.lower(),
        "type": type_of(p.suffix),
        "size": st.st_size,
        "mtime": int(st.st_mtime),
    }


def resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{path}")
    return p


def want_path(payload: dict) -> Path:
    """取 path 参数。缺了给一句人话，而不是 KeyError: 'path'。"""
    raw = payload.get("path")
    if not raw or not isinstance(raw, str):
        raise ValueError("缺少 path 参数（要处理的文件或文件夹路径）")
    return resolve(raw)


def as_int(value, default: int, lo: int = 0, hi: int = 100_000) -> int:
    """把「看起来像数字」的东西转成数字，转不了就用默认值并夹到合理区间。

    start 写成 "abc"、pad 写成 -3 这类输入在 agent 调用里很常见，
    不该让整次改名报 invalid literal for int()。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# ------------------------------------------------------------------ 业务
def api_pick_folder(payload: dict) -> dict:
    path = pick_folder(payload.get("prompt") or "选择要重命名的文件夹")
    if not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": path}


def api_list(payload: dict) -> dict:
    p = want_path(payload)
    files = collect([p], bool(payload.get("recursive")), payload.get("only") or None)
    return {
        "ok": True,
        "dir": str(p if p.is_dir() else p.parent),
        "count": len(files),
        "files": [describe(f) for f in files],
    }


def api_extract(payload: dict) -> dict:
    p = want_path(payload)
    names = payload.get("files") or []
    if not isinstance(names, list):
        names = []
    limit = as_int(payload.get("limit"), 3000, lo=100, hi=200_000)
    if names:
        # 只允许抽 base 目录内的文件，防止 ../../ 读到别处
        base = p if p.is_dir() else p.parent
        targets = []
        for n in names:
            t = (base / str(n)).resolve()
            try:
                t.relative_to(base.resolve())
            except ValueError:
                continue                     # 越界，直接丢掉
            if t.is_file():
                targets.append(t)
        pairs = []
        for t in targets:
            try:
                text = extract_text(t, limit)
            except Exception:
                continue
            pairs.append((t, text))
    else:
        files = collect([p], False, None)
        targets_pool = [f for f in files if f.suffix.lower() in
                        {".docx", ".doc", ".pptx", ".xlsx", ".pdf", ".txt", ".md", ".csv"}
                        or type_of(f.suffix) == "照片"]
        # 解析失败 / 无文字层的文件不该占名额
        pairs = rich_texts(targets_pool, want=3, limit=limit)

    out = []
    all_candidates: list[dict] = []
    for t, text in pairs:
        cands = kwmod.suggest(text)
        out.append({"name": t.name, "type": type_of(t.suffix),
                    "text": text, "candidates": cands})
        for c in cands:
            c = dict(c)
            c["from"] = t.name
            all_candidates.append(c)
    merged = _merge_candidates(all_candidates)
    return {"ok": True, "sources": out, "candidates": merged}


def _merge_candidates(items: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for c in items:
        cur = best.get(c["kw"])
        if cur is None or c["score"] > cur["score"]:
            best[c["kw"]] = c
    return sorted(best.values(), key=lambda c: -c["score"])[:10]


def _plan_args(payload: dict):
    p = want_path(payload)
    files = collect([p], bool(payload.get("recursive")), payload.get("only") or None)
    kw = payload.get("kw") or ""
    if not sanitize(kw):
        raise ValueError("关键词不能为空")
    warns: list = []
    pairs = plan(files, kw,
                 template=payload.get("template") or "{kw}-{n}",
                 start=as_int(payload.get("start"), 1, lo=0),
                 pad=as_int(payload.get("pad"), 0, lo=0, hi=12),
                 sort=payload.get("sort") or "name",
                 warnings=warns)
    return p, pairs, kw, warns


def api_preview(payload: dict) -> dict:
    p, pairs, kw, warns = _plan_args(payload)
    conflicts = check_conflicts(pairs)
    cset = {str(a) for a, _ in conflicts}
    root = p
    rows = [{
        "from": a.name,
        "to": b.name,
        "from_label": relative_label(a, [root]),
        "to_label": relative_label(b, [root]),
        "from_path": str(a),
        "to_path": str(b),
        "changed": a.name != b.name,
        "conflict": str(a) in cset,
        "type": type_of(a.suffix),
    } for a, b in pairs]
    stale = []
    for a, _b in pairs:
        stale.extend(str(s) for s in stale_temp_files(a.parent))
    links = find_symlinks([root], bool(payload.get("recursive")))
    return {"ok": True, "rows": rows,
            "count": len(rows),
            "changed": sum(1 for r in rows if r["changed"]),
            "conflicts": len(conflicts),
            "conflict_names": [b.name for _a, b in conflicts[:5]],
            "warnings": warns,
            "skipped_symlinks": len(links),
            "stale_temp": sorted(set(stale))}


def api_apply(payload: dict) -> dict:
    _p, pairs, kw, _warns = _plan_args(payload)
    try:
        log = apply_plan(pairs, kw, payload.get("template") or "{kw}-{n}")
    except ConflictError as e:
        return {"ok": False, "error": str(e),
                "conflicts": [b.name for _a, b in e.conflicts]}
    if log is None:
        return {"ok": True, "renamed": 0, "message": "文件名已符合规则，无需改动"}
    n = sum(1 for a, b in pairs if a.name != b.name)
    return {"ok": True, "renamed": n, "log": log.name, "log_path": str(log)}


def api_undo(payload: dict) -> dict:
    try:
        ok, skipped = undo(payload.get("log"))
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "restored": ok, "skipped": skipped}


def api_logs(_payload: dict) -> dict:
    out = []
    for p in list_undo_logs():
        data = load_log(p)
        if data is None:
            continue
        out.append({
            "name": p.name,
            "time": data.get("time"),
            "keyword": data.get("keyword"),
            "count": len(data.get("records") or []),
        })
    return {"ok": True, "logs": list(reversed(out))[-20:]}


def api_workspace_create(_payload: dict) -> dict:
    return browser_store.create()


def api_workspace_list(payload: dict) -> dict:
    return browser_store.listing(payload.get("workspace"))


def api_workspace_extract(payload: dict) -> dict:
    return browser_store.extract(payload.get("workspace"))


def api_workspace_preview(payload: dict) -> dict:
    return browser_store.preview(payload.get("workspace"), payload)


def api_workspace_apply(payload: dict) -> dict:
    return browser_store.apply(payload.get("workspace"), payload.get("plan_token"))


def api_workspace_undo(payload: dict) -> dict:
    return browser_store.revert(payload.get("workspace"))


def api_workspace_delete(payload: dict) -> dict:
    return browser_store.delete(payload.get("workspace"))


ROUTES = {
    "/api/workspaces/create": api_workspace_create,
    "/api/workspaces/list": api_workspace_list,
    "/api/workspaces/extract": api_workspace_extract,
    "/api/workspaces/preview": api_workspace_preview,
    "/api/workspaces/apply": api_workspace_apply,
    "/api/workspaces/undo": api_workspace_undo,
    "/api/workspaces/delete": api_workspace_delete,
    "/api/pick-folder": api_pick_folder,
    "/api/list": api_list,
    "/api/extract": api_extract,
    "/api/preview": api_preview,
    "/api/apply": api_apply,
    "/api/undo": api_undo,
    "/api/logs": api_logs,
}


# ------------------------------------------------------------------ HTTP
def err_text(e: Exception) -> str:
    """我们自己抛的异常消息本来就是人话，不用再挂个类型名。
    意外异常保留类型名，方便排查。"""
    if isinstance(e, (ValueError, FileNotFoundError, PermissionError,
                      IsADirectoryError, NotADirectoryError, ConflictError)):
        return str(e)
    return f"{type(e).__name__}: {e}"


class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = "rnbatch/0.2.1"

    def log_message(self, fmt, *args):        # 安静点，别刷屏
        pass

    # -- 输出
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _authorized(self, qs) -> bool:
        given = (qs.get("t") or [""])[0] or self.headers.get("X-RNBatch-Token", "")
        return bool(given) and hmac.compare_digest(given, self.token)

    # -- 路由
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            f = WEB_DIR / "index.html"
            if not f.exists():
                self._send(500, b"UI missing", "text/plain")
                return
            body = (f.read_text(encoding="utf-8")
                    .replace("__TOKEN__", self.token)
                    .replace("__VERSION__", __version__))
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/api/workspaces/download":
            if not self._authorized(qs):
                self._json(403, {"ok": False, "error": "token 无效"})
                return
            try:
                archive = browser_store.archive((qs.get("workspace") or [""])[0])
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="rnbatch-files.zip"')
                self.send_header("Content-Length", str(archive.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with archive.open("rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (ValueError, OSError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            return
        if u.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if u.path.startswith("/api/"):
            self._json(405, {"ok": False, "error": "此 API 必须使用 POST"})
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        fn = ROUTES.get(u.path)
        if u.path == "/api/workspaces/upload":
            if not self._authorized(qs):
                self._json(403, {"ok": False, "error": "token 无效"})
                return
            try:
                n = int(self.headers.get("Content-Length") or "-1")
                if n < 0 or n > MAX_FILE:
                    raise ValueError("上传文件必须包含 Content-Length 且不超过 25 MB")
                result = browser_store.upload(
                    (qs.get("workspace") or [""])[0],
                    (qs.get("name") or [""])[0], self.rfile, n)
                self._json(200, result)
            except Exception as e:
                self._json(400, {"ok": False, "error": err_text(e)})
            return
        if fn is None:
            self._json(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._authorized(qs):
            self._json(403, {"ok": False, "error": "token 无效"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1024 * 1024:
                raise ValueError("请求体超过 1 MB 限制")
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload 必须是对象")
        except Exception as e:
            self._json(400, {"ok": False, "error": f"请求体解析失败: {e}"})
            return
        try:
            self._json(200, fn(payload))
        except Exception as e:
            self._json(400, {"ok": False, "error": err_text(e)})


def serve(port: int = 8765, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(16)
    Handler.token = token
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        import errno as _errno
        if getattr(e, "errno", None) in (_errno.EADDRINUSE, _errno.EACCES):
            print(f"端口 {port} 已被占用。换一个： rnbatch serve --port {port + 1}",
                  file=sys.stderr)
            raise SystemExit(2)
        raise
    url = f"http://127.0.0.1:{port}/?t={token}"
    print("rnbatch 本地应用已启动")
    print(f"  {url}")
    print("  只监听 127.0.0.1，文件不会离开本机。Ctrl+C 退出。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
