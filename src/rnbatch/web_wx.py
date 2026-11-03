"""微信小程序接入层（rnbatch Web App 的适配器）。

放到 `src/rnbatch/web_wx.py` 后即可用：

    python -m rnbatch.web_wx --appid wx... --secret ... --origin https://files.example.com

它不重写业务，只在 `web_cloud.make_handler()` 之上补三件事——这三件事都是
微信小程序和浏览器的差异，不是产品逻辑：

1. 小程序没有 Cookie，认证改成 `Authorization: Bearer <token>`。
2. `wx.uploadFile` 发的是 multipart/form-data，原接口只认裸 body。
3. 登录走 `wx.login` 的 code，服务端换 openid，不需要密码。

安全边界不变：CSRF 校验只对 Cookie 认证生效（Bearer 不会被浏览器自动带上，
天然免疫 CSRF）；Host 校验保留；Origin 校验对 Bearer 请求跳过（小程序不发 Origin）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .web_auth import SESSION_SECONDS, AuthStore
from .web_cloud import make_handler
from .web_workspace import MAX_FILE, WorkspaceStore

WX_SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
MAX_MULTIPART_HEADER = 64 * 1024


class WxAccountError(ValueError):
    """小程序账号侧的错误，消息本身就是给用户看的。"""


# ---------------------------------------------------------------- openid 账号
class WxAccounts:
    """openid 与 users 表的映射。密码列填随机值，因此这些账号无法用密码登录。"""

    def __init__(self, database: str | Path):
        self.path = Path(database).expanduser()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wx_accounts (
                    openid TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        # sqlite3 的 `with conn` 只管事务，不关连接；这里必须显式 close，
        # 否则每个请求泄漏一个句柄。
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def user_for(self, openid: str) -> tuple[str, str]:
        """返回 (user_id, username)，没有就建一个。"""
        if not isinstance(openid, str) or not (10 <= len(openid) <= 128):
            raise WxAccountError("openid 格式不正确")
        with self._connect() as db:
            row = db.execute(
                "SELECT user_id FROM wx_accounts WHERE openid=?", (openid,)
            ).fetchone()
            if row:
                uid = row[0]
                name = db.execute(
                    "SELECT username FROM users WHERE id=?", (uid,)
                ).fetchone()
                return uid, (name[0] if name else "")

            uid = uuid.uuid4().hex
            # 用户名只用于内部标识和额度归属，用户在微信里看不到它
            username = "wx_" + hashlib.sha256(openid.encode("utf-8")).hexdigest()[:12]
            db.execute(
                "INSERT INTO users VALUES (?,?,?,?,?)",
                (uid, username, os.urandom(16), os.urandom(32), int(time.time())),
            )
            db.execute(
                "INSERT INTO wx_accounts VALUES (?,?,?)",
                (openid, uid, int(time.time())),
            )
            return uid, username

    def issue_session(self, user_id: str) -> tuple[str, str]:
        """直接签发会话 token（复用 AuthStore 的 sessions 表和哈希方式）。"""
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expiry = int(time.time()) + SESSION_SECONDS
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (AuthStore._token_hash(token), user_id, csrf, expiry),
            )
        return token, csrf


def code_to_openid(appid: str, secret: str, code: str, timeout: int = 8) -> str:
    if not isinstance(code, str) or not (8 <= len(code) <= 128):
        raise WxAccountError("登录 code 无效")
    query = urllib.parse.urlencode({
        "appid": appid,
        "secret": secret,
        "js_code": code,
        "grant_type": "authorization_code",
    })
    try:
        with urllib.request.urlopen(WX_SESSION_URL + "?" + query, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 网络问题不该把 500 抛给小程序
        raise WxAccountError("微信登录服务暂时不可用") from exc

    if payload.get("errcode"):
        raise WxAccountError(
            "微信登录失败：%s" % payload.get("errmsg", "未知错误")
        )
    openid = payload.get("openid")
    if not openid:
        raise WxAccountError("微信未返回 openid")
    return openid


# ---------------------------------------------------------------- multipart
def read_multipart_file(rfile, content_length: int, content_type: str, limit: int):
    """从 multipart/form-data 里取出第一个文件部分。

    返回 (filename, payload)。只处理 wx.uploadFile 这一种形态：一个文件字段，
    外加少量文本字段。总大小超过 limit 直接拒绝，避免内存被撑爆。
    """
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("上传必须使用 multipart/form-data")
    if content_length <= 0:
        raise ValueError("缺少 Content-Length")
    if content_length > limit + MAX_MULTIPART_HEADER:
        raise ValueError("文件超过单文件大小限制")

    boundary = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece[len("boundary="):].strip('"')
    if not boundary:
        raise ValueError("multipart 缺少 boundary")

    body = bytearray()
    remaining = content_length
    while remaining > 0:
        chunk = rfile.read(min(65536, remaining))
        if not chunk:
            break
        body.extend(chunk)
        remaining -= len(chunk)
    if remaining:
        raise ValueError("上传数据不完整，请重试")

    delimiter = b"--" + boundary.encode("ascii", "ignore")
    segments = bytes(body).split(delimiter)
    for segment in segments:
        if not segment or segment in (b"--\r\n", b"--"):
            continue
        part = segment.lstrip(b"\r\n")
        split_at = part.find(b"\r\n\r\n")
        if split_at < 0:
            continue
        headers = part[:split_at].decode("utf-8", "replace")
        if 'filename="' not in headers and "filename=" not in headers:
            continue
        payload = part[split_at + 4:]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        if len(payload) > limit:
            raise ValueError("文件超过单文件大小限制")
        return _filename_from(headers), payload

    raise ValueError("multipart 里没有找到文件字段")


def _filename_from(headers: str) -> str:
    for line in headers.splitlines():
        if line.lower().startswith("content-disposition:"):
            for piece in line.split(";"):
                piece = piece.strip()
                if piece.lower().startswith("filename="):
                    return piece[len("filename="):].strip('"')
    return ""


# ---------------------------------------------------------------- handler
def make_wx_handler(auth: AuthStore, store: WorkspaceStore, origin: str,
                    accounts: WxAccounts, appid: str, secret: str):
    Base = make_handler(auth, store, origin)

    class WxHandler(Base):
        server_version = "rnbatch-wx/0.1"

        # --- 认证：Bearer 优先，退回 Cookie ---
        def _bearer(self) -> str:
            raw = self.headers.get("Authorization", "")
            if raw.startswith("Bearer "):
                return raw[7:].strip()
            return ""

        def _token(self):
            return self._bearer() or super()._token()

        def _auth(self, require_csrf=False):
            # Bearer 不会被浏览器自动附带，不存在 CSRF 面，因此跳过 token 校验
            if self._bearer():
                require_csrf = False
            return super()._auth(require_csrf=require_csrf)

        def _origin_ok(self):
            # 小程序不发 Origin，靠 Bearer + Host 校验兜底
            if self._bearer():
                return True
            return super()._origin_ok()

        # --- 路由 ---
        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/api/wx/login":
                self._handle_login()
                return
            if route == "/api/workspaces/upload":
                ctype = self.headers.get("Content-Type", "")
                if ctype.lower().startswith("multipart/form-data"):
                    self._handle_multipart_upload()
                    return
            super().do_POST()

        def _handle_login(self):
            if not self._host_ok():
                self._json(400, {"ok": False, "error": "无效 Host"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not (0 <= length <= 4096):
                    raise WxAccountError("请求体长度无效")
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise WxAccountError("请求体必须是对象")
                openid = code_to_openid(appid, secret, data.get("code"))
                user_id, username = accounts.user_for(openid)
                token, _csrf = accounts.issue_session(user_id)
            except (WxAccountError, ValueError) as exc:
                self._json(401, {"ok": False, "error": str(exc)})
                return
            except Exception:
                self._json(500, {"ok": False, "error": "登录处理失败"})
                return
            self._json(200, {"ok": True, "token": token, "username": username})

        def _handle_multipart_upload(self):
            if not self._host_ok() or not self._origin_ok():
                self._json(403, {"ok": False, "error": "请求来源不受信任"})
                return
            sess = self._auth(require_csrf=True)
            if not sess:
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length <= 0:
                    raise ValueError("缺少 Content-Length")
                filename, payload = read_multipart_file(
                    self.rfile, length, self.headers.get("Content-Type", ""), MAX_FILE)
                query = parse_qs(urlparse(self.path).query)
                name = (query.get("name") or [""])[0] or filename
                wid = (query.get("workspace") or [""])[0]
                self._owned(sess, wid)
                result = store.upload(wid, name, io.BytesIO(payload), len(payload))
                self._json(200, result)
            except (ValueError, OSError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})

    return WxHandler


def serve_wx(port: int = 8767, db_path: str = "~/.rnbatch/web-users.sqlite3",
             public_origin: str | None = None, appid: str = "", secret: str = ""):
    if not appid or not secret:
        raise ValueError("必须提供小程序 AppID 和 AppSecret")
    origin = public_origin or f"http://127.0.0.1:{port}"
    auth = AuthStore(db_path)
    accounts = WxAccounts(auth.path)
    store = WorkspaceStore()
    handler = make_wx_handler(auth, store, origin, accounts, appid, secret)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"rnbatch 小程序后端监听 127.0.0.1:{port}")
    print(f"对外 origin: {origin}")
    print("小程序侧需在后台登记该域名（HTTPS），并把 utils/config.js 的 API_BASE 改成它。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rnbatch.web_wx")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--db", default="~/.rnbatch/web-users.sqlite3")
    parser.add_argument("--origin", default=None, help="小程序访问的 HTTPS origin")
    parser.add_argument("--appid", required=True)
    parser.add_argument("--secret", required=True)
    args = parser.parse_args(argv)
    try:
        serve_wx(args.port, args.db, args.origin, args.appid, args.secret)
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
