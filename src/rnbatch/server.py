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

from . import keywords as kwmod
from .engine import apply_plan, collect, list_undo_logs, plan, sanitize, undo
from .extract import extract_text, preferred_sources, type_of

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


# ------------------------------------------------------------------ 业务
def api_pick_folder(payload: dict) -> dict:
    path = pick_folder(payload.get("prompt") or "选择要重命名的文件夹")
    if not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": path}


def api_list(payload: dict) -> dict:
    p = resolve(payload["path"])
    files = collect([p], bool(payload.get("recursive")), None)
    only = payload.get("only")
    if only:
        wanted = {"." + e.lower().lstrip(".") for e in only}
        files = [f for f in files if f.suffix.lower() in wanted]
    return {
        "ok": True,
        "dir": str(p if p.is_dir() else p.parent),
        "count": len(files),
        "files": [describe(f) for f in files],
    }


def api_extract(payload: dict) -> dict:
    p = resolve(payload["path"])
    names = payload.get("files") or []
    if names:
        targets = [p / n if p.is_dir() else p for n in names]
        targets = [t for t in targets if t.exists()]
    else:
        files = collect([p], False, None)
        targets = preferred_sources(
            [f for f in files if f.suffix.lower() in
             {".docx", ".doc", ".pptx", ".xlsx", ".pdf", ".txt", ".md", ".csv"}
             or type_of(f.suffix) == "照片"], 3)
    limit = int(payload.get("limit") or 3000)
    out = []
    all_candidates: list[dict] = []
    for t in targets:
        text = extract_text(t, limit)
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
    p = resolve(payload["path"])
    files = collect([p], bool(payload.get("recursive")), payload.get("only") or None)
    kw = payload.get("kw") or ""
    if not sanitize(kw):
        raise ValueError("关键词不能为空")
    pairs = plan(files, kw,
                 template=payload.get("template") or "{kw}-{n}",
                 start=int(payload.get("start") or 1),
                 pad=int(payload.get("pad") or 0),
                 sort=payload.get("sort") or "name")
    return pairs, kw


def api_preview(payload: dict) -> dict:
    pairs, kw = _plan_args(payload)
    rows = [{
        "from": a.name,
        "to": b.name,
        "from_path": str(a),
        "to_path": str(b),
        "changed": a.name != b.name,
        "type": type_of(a.suffix),
    } for a, b in pairs]
    return {"ok": True, "rows": rows,
            "count": len(rows),
            "changed": sum(1 for r in rows if r["changed"])}


def api_apply(payload: dict) -> dict:
    pairs, kw = _plan_args(payload)
    log = apply_plan(pairs, kw, payload.get("template") or "{kw}-{n}")
    if log is None:
        return {"ok": True, "renamed": 0, "message": "文件名已符合规则，无需改动"}
    n = sum(1 for a, b in pairs if a.name != b.name)
    return {"ok": True, "renamed": n, "log": log.name, "log_path": str(log)}


def api_undo(payload: dict) -> dict:
    ok, skipped = undo(payload.get("log"))
    return {"ok": True, "restored": ok, "skipped": skipped}


def api_logs(_payload: dict) -> dict:
    out = []
    for p in list_undo_logs():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "name": p.name,
            "time": data.get("time"),
            "keyword": data.get("keyword"),
            "count": len(data.get("records") or []),
        })
    return {"ok": True, "logs": list(reversed(out))[-20:]}


ROUTES = {
    "/api/pick-folder": api_pick_folder,
    "/api/list": api_list,
    "/api/extract": api_extract,
    "/api/preview": api_preview,
    "/api/apply": api_apply,
    "/api/undo": api_undo,
    "/api/logs": api_logs,
}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = "rnbatch/0.2"

    def log_message(self, fmt, *args):        # 安静点，别刷屏
        pass

    # -- 输出
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
            body = f.read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if u.path.startswith("/api/"):
            fn = ROUTES.get(u.path)
            if fn is None:
                self._json(404, {"ok": False, "error": "unknown endpoint"})
                return
            if not self._authorized(qs):
                self._json(403, {"ok": False, "error": "token 无效"})
                return
            try:
                self._json(200, fn({}))
            except Exception as e:
                self._json(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        fn = ROUTES.get(u.path)
        if fn is None:
            self._json(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._authorized(qs):
            self._json(403, {"ok": False, "error": "token 无效"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload 必须是对象")
        except Exception as e:
            self._json(400, {"ok": False, "error": f"请求体解析失败: {e}"})
            return
        try:
            self._json(200, fn(payload))
        except Exception as e:
            self._json(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def serve(port: int = 8765, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(16)
    Handler.token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
