"""Authenticated browser-only Web App API.

Exposes ONLY temporary upload workspaces; intentionally never imports the
legacy path-based HTTP Handler. Bind to 127.0.0.1 behind an HTTPS proxy.
"""
from __future__ import annotations

import html
import hmac
import json
import sys
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .web_auth import AuthStore, SESSION_SECONDS
from .web_workspace import MAX_FILE, WorkspaceStore

WEB_DIR = Path(__file__).parent / "web"
JSON_LIMIT = 64 * 1024


def make_handler(auth: AuthStore, store: WorkspaceStore, origin: str):
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("无效的 Web App origin")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("非本机的 Web App 必须使用 HTTPS")
    expected_host = parsed.netloc.lower()
    cookie_secure = parsed.scheme == "https"

    class CloudHandler(BaseHTTPRequestHandler):
        server_version = "rnbatch-web/0.3"

        def log_message(self, fmt, *args):
            # Avoid logging query strings, account names or session tokens.
            pass

        def _send(self, status, body, mime="application/json; charset=utf-8", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; object-src 'none'; "
                             "base-uri 'none'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, status, data, headers=None):
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       headers=headers)

        def _host_ok(self):
            return self.headers.get("Host", "").lower() == expected_host

        def _origin_ok(self):
            # Require an explicit same-origin browser POST. No wildcard CORS.
            return self.headers.get("Origin", "") == origin.rstrip("/")

        def _token(self):
            raw = self.headers.get("Cookie", "")
            if len(raw) > 4096:
                return ""
            jar = cookies.SimpleCookie()
            try:
                jar.load(raw)
            except cookies.CookieError:
                return ""
            item = jar.get("rnbatch_session")
            return item.value if item else ""

        def _session(self):
            return auth.verify(self._token())

        def _auth(self, require_csrf=False):
            sess = self._session()
            if not sess:
                self._json(401, {"ok": False, "error": "请先登录"})
                return None
            if require_csrf and not hmac.compare_digest(
                self.headers.get("X-RNBatch-CSRF", ""), sess.csrf
            ):
                self._json(403, {"ok": False, "error": "会话确认失败，请刷新网页"})
                return None
            return sess

        def _owned(self, sess, workspace):
            # Owner identity comes ONLY from the signed-in cookie, never payload.
            return store.get(workspace, owner=sess.user_id)

        def _cookie(self, token, max_age=SESSION_SECONDS):
            value = f"rnbatch_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            if cookie_secure:
                value += "; Secure"
            return value

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise ValueError("Content-Length 无效")
            if length < 0 or length > JSON_LIMIT:
                raise ValueError("请求体长度无效或超过 64 KB")
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("请求必须使用 application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求体未完整接收")
            try:
                data = json.loads(raw)
            except (UnicodeError, ValueError) as e:
                raise ValueError("请求体不是合法 JSON") from e
            if not isinstance(data, dict):
                raise ValueError("请求体必须是对象")
            return data

        def do_GET(self):
            if not self._host_ok():
                self._json(400, {"ok": False, "error": "无效 Host"})
                return
            route = urlparse(self.path)
            if route.path in ("/", "/index.html"):
                sess = self._session()
                filename = "cloud.html" if sess else "login.html"
                body = (WEB_DIR / filename).read_text(encoding="utf-8")
                if sess:
                    body = body.replace("__CSRF__", sess.csrf)
                    body = body.replace("__USERNAME__", html.escape(sess.username, quote=True))
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if route.path == "/api/workspaces/download":
                sess = self._auth()
                if not sess:
                    return
                sent = False
                try:
                    wid = (parse_qs(route.query).get("workspace") or [""])[0]
                    self._owned(sess, wid)
                    archive = store.archive(wid)
                    with archive.open("rb") as source:
                        length = archive.stat().st_size
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Disposition",
                                         'attachment; filename="rnbatch-files.zip"')
                        self.send_header("Content-Length", str(length))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.end_headers()
                        sent = True
                        while True:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (OSError, ValueError) as e:
                    if not sent:
                        self._json(400, {"ok": False, "error": str(e)})
                    else:
                        # Headers/body already started; never send a second response.
                        self.close_connection = True
                return
            self._json(404, {"ok": False, "error": "未知页面或 API"})

        def do_POST(self):
            if not self._host_ok() or not self._origin_ok():
                self._json(403, {"ok": False, "error": "请求来源不受信任"})
                return
            route = urlparse(self.path).path
            if route == "/auth/login":
                try:
                    data = self._read_json()
                    verified = auth.sign_in(data.get("username"), data.get("password"),
                                            self.client_address[0])
                    if not verified:
                        self._json(401, {"ok": False, "error": "用户名或密码不正确"})
                        return
                    token, sess = verified
                    self._json(200, {"ok": True, "username": sess.username},
                               {"Set-Cookie": self._cookie(token)})
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
                return

            sess = self._auth(require_csrf=True)
            if not sess:
                return
            if route == "/auth/logout":
                auth.sign_out(self._token())
                self._json(200, {"ok": True},
                           {"Set-Cookie": self._cookie("", 0)})
                return
            if route == "/api/workspaces/upload":
                qs = parse_qs(urlparse(self.path).query)
                wid = (qs.get("workspace") or [""])[0]
                name = (qs.get("name") or [""])[0]
                try:
                    self._owned(sess, wid)
                    length = int(self.headers.get("Content-Length", "-1"))
                    if not (0 <= length <= MAX_FILE):
                        raise ValueError("文件大小或 Content-Length 不正确")
                    self._json(200, store.upload(wid, name, self.rfile, length))
                except (ValueError, OSError) as e:
                    self._json(400, {"ok": False, "error": str(e)})
                return

            calls = {
                "/api/workspaces/list": store.listing,
                "/api/workspaces/extract": store.extract,
                "/api/workspaces/preview": store.preview,
                "/api/workspaces/apply": store.apply,
                "/api/workspaces/undo": store.revert,
                "/api/workspaces/delete": store.delete,
            }
            if route != "/api/workspaces/create" and route not in calls:
                self._json(404, {"ok": False, "error": "未知 API"})
                return
            try:
                data = self._read_json()
                if route == "/api/workspaces/create":
                    result = store.create(owner=sess.user_id)
                else:
                    wid = data.get("workspace")
                    self._owned(sess, wid)
                    if route == "/api/workspaces/preview":
                        result = store.preview(wid, data)
                    elif route == "/api/workspaces/apply":
                        result = store.apply(wid, data.get("plan_token"))
                    else:
                        result = calls[route](wid)
                self._json(200, result)
            except (ValueError, OSError) as e:
                self._json(400, {"ok": False, "error": str(e)})

    return CloudHandler


def serve_cloud(port: int = 8766, db_path: str = "~/.rnbatch/web-users.sqlite3",
                public_origin: str | None = None):
    """Local-only socket; put a hardened HTTPS reverse proxy in front if needed."""
    origin = public_origin or f"http://127.0.0.1:{port}"
    auth = AuthStore(db_path)
    if not auth.has_users():
        raise RuntimeError("尚无账号。请先运行 rnbatch user-add <用户名>")
    store = WorkspaceStore()
    handler = make_handler(auth, store, origin)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Authenticated Web App listening on 127.0.0.1:{port}")
    print(f"Browser origin: {origin}")
    print("仅供本机访问。对外上线需 HTTPS 反向代理、边缘限流和上传安全策略。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止 Web App")
    finally:
        httpd.server_close()
        store.close()
