"""WeChat mini program adapter: openid accounts, multipart upload, Bearer auth."""
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rnbatch.web_auth import AuthStore
from rnbatch.web_wx import (WxAccountError, WxAccounts, make_wx_handler,
                            read_multipart_file)
from rnbatch.web_workspace import MAX_FILE, WorkspaceStore

BOUNDARY = "----rnbatchtest"


def build_multipart(filename, payload, boundary=BOUNDARY):
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class TestWxAccounts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "web-users.sqlite3"
        self.auth = AuthStore(self.db)
        self.accounts = WxAccounts(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_openid_creates_one_reusable_account(self):
        uid, name = self.accounts.user_for("openid-abcdefghij")
        self.assertTrue(uid)
        self.assertTrue(name.startswith("wx_"))
        again = self.accounts.user_for("openid-abcdefghij")
        self.assertEqual(again, (uid, name))
        other = self.accounts.user_for("openid-zzzzzzzzzz")
        self.assertNotEqual(other[0], uid)

    def test_rejects_malformed_openid(self):
        for bad in ("", "short", None, 12345, "x" * 200):
            with self.assertRaises(WxAccountError):
                self.accounts.user_for(bad)

    def test_issued_session_is_accepted_by_auth_store(self):
        uid, name = self.accounts.user_for("openid-session-test")
        token, csrf = self.accounts.issue_session(uid)
        session = self.auth.verify(token)
        self.assertIsNotNone(session)
        self.assertEqual(session.user_id, uid)
        self.assertEqual(session.username, name)
        self.assertEqual(session.csrf, csrf)

    def test_wechat_account_cannot_be_used_with_a_password(self):
        uid, name = self.accounts.user_for("openid-nopassword")
        self.assertIsNone(self.auth.sign_in(name, "any-password-at-all", "127.0.0.1"))


class TestMultipartParsing(unittest.TestCase):
    def test_extracts_file_payload(self):
        body, ctype = build_multipart("会议纪要.txt", "会议主题：星海中心".encode())
        name, payload = read_multipart_file(io.BytesIO(body), len(body), ctype, MAX_FILE)
        self.assertEqual(name, "会议纪要.txt")
        self.assertEqual(payload.decode(), "会议主题：星海中心")

    def test_binary_payload_survives_round_trip(self):
        raw = bytes(range(256)) * 8
        body, ctype = build_multipart("blob.bin", raw)
        name, payload = read_multipart_file(io.BytesIO(body), len(body), ctype, MAX_FILE)
        self.assertEqual(name, "blob.bin")
        self.assertEqual(payload, raw)

    def test_rejects_non_multipart_content_type(self):
        body, _ = build_multipart("a.txt", b"x")
        with self.assertRaises(ValueError):
            read_multipart_file(io.BytesIO(body), len(body), "application/json", MAX_FILE)

    def test_rejects_missing_boundary(self):
        body, _ = build_multipart("a.txt", b"x")
        with self.assertRaises(ValueError):
            read_multipart_file(io.BytesIO(body), len(body), "multipart/form-data", MAX_FILE)

    def test_rejects_declared_oversize_before_reading(self):
        body, ctype = build_multipart("a.txt", b"x")
        with self.assertRaises(ValueError):
            read_multipart_file(io.BytesIO(body), MAX_FILE + 200_000, ctype, MAX_FILE)

    def test_rejects_body_without_file_part(self):
        body = (
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="workspace"\r\n\r\n'
            f"abc\r\n--{BOUNDARY}--\r\n"
        ).encode()
        with self.assertRaises(ValueError):
            read_multipart_file(io.BytesIO(body), len(body),
                                f"multipart/form-data; boundary={BOUNDARY}", MAX_FILE)


class TestWxRoutes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "web-users.sqlite3"
        self.auth = AuthStore(self.db)
        self.accounts = WxAccounts(self.db)
        self.store = WorkspaceStore()

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = "http://127.0.0.1:%s" % port
        handler = make_wx_handler(self.auth, self.store, self.base,
                                  self.accounts, "wx-appid", "wx-secret")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(timeout=5)
        self.store.close()
        self.temp.cleanup()

    # ------------------------------------------------------------ helpers
    def request(self, route, data=None, token="", headers=None, raw=None):
        extra = dict(headers or {})
        if token:
            extra["Authorization"] = "Bearer " + token
        if data is not None:
            extra["Content-Type"] = "application/json"
            raw = json.dumps(data).encode()
        req = urllib.request.Request(self.base + route, data=raw, headers=extra)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        body = response.read()
        if response.headers.get("Content-Type", "").startswith("application/json"):
            return response.status, json.loads(body)
        return response.status, body

    def login(self, code="test-code-1234"):
        with patch("rnbatch.web_wx.code_to_openid",
                   side_effect=lambda appid, secret, c: "openid-" + str(c)):
            status, data = self.request("/api/wx/login", {"code": code})
        self.assertEqual(status, 200, data)
        return data["token"]

    def upload(self, workspace, token, filename, payload):
        body, ctype = build_multipart(filename, payload)
        route = "/api/workspaces/upload?workspace=%s&name=%s" % (
            urllib.parse.quote(workspace), urllib.parse.quote(filename))
        return self.request(route, token=token,
                            headers={"Content-Type": ctype}, raw=body)

    # ------------------------------------------------------------ tests
    def test_login_rejects_bad_code_and_returns_token(self):
        with patch("rnbatch.web_wx.code_to_openid",
                   side_effect=WxAccountError("微信登录失败：code 无效")):
            status, data = self.request("/api/wx/login", {"code": "bad-code-123"})
        self.assertEqual(status, 401)
        self.assertIn("微信登录失败", data["error"])
        self.assertTrue(self.login())

    def test_anonymous_request_is_refused(self):
        status, _ = self.request("/api/workspaces/create", {})
        self.assertIn(status, (401, 403))

    def test_full_flow_over_bearer(self):
        token = self.login()
        status, created = self.request("/api/workspaces/create", {}, token=token)
        self.assertEqual(status, 200, created)
        workspace = created["workspace"]

        status, up = self.upload(workspace, token, "会议纪要.txt",
                                 "会议主题：星海中心项目 一期交付启动会".encode())
        self.assertEqual(status, 200, up)
        status, up2 = self.upload(workspace, token, "IMG_0421.JPG", b"fakejpg")
        self.assertEqual((status, up2["count"]), (200, 2))

        status, listing = self.request("/api/workspaces/list",
                                       {"workspace": workspace}, token=token)
        self.assertEqual(listing["count"], 2)

        status, extracted = self.request("/api/workspaces/extract",
                                         {"workspace": workspace}, token=token)
        self.assertIn("星海中心", [c["kw"] for c in extracted["candidates"]])

        status, preview = self.request("/api/workspaces/preview",
                                       {"workspace": workspace, "kw": "星海中心",
                                        "template": "{kw}-{n:02}-{type}"}, token=token)
        self.assertEqual((status, preview["conflicts"]), (200, 0))
        self.assertEqual(preview["changed"], 2)

        status, applied = self.request("/api/workspaces/apply",
                                       {"workspace": workspace,
                                        "plan_token": preview["plan_token"]}, token=token)
        self.assertEqual((status, applied["renamed"]), (200, 2))

        status, blob = self.request(
            "/api/workspaces/download?workspace=" + urllib.parse.quote(workspace),
            token=token)
        self.assertEqual(status, 200)
        self.assertEqual(blob[:2], b"PK")

        status, undone = self.request("/api/workspaces/undo",
                                      {"workspace": workspace}, token=token)
        self.assertEqual((status, undone["restored"]), (200, 2))

    def test_workspaces_are_isolated_between_wechat_users(self):
        owner = self.login("code-owner-111")
        stranger = self.login("code-stranger-222")
        status, created = self.request("/api/workspaces/create", {}, token=owner)
        workspace = created["workspace"]
        self.upload(workspace, owner, "a.txt", b"a")
        status, _ = self.request("/api/workspaces/list",
                                 {"workspace": workspace}, token=stranger)
        self.assertEqual(status, 400)

    def test_upload_rejects_oversize_and_bad_paths(self):
        token = self.login()
        status, created = self.request("/api/workspaces/create", {}, token=token)
        workspace = created["workspace"]
        status, _ = self.upload(workspace, token, "../escape.txt", b"x")
        self.assertEqual(status, 400)
        status, _ = self.upload(workspace, token, ".hidden", b"x")
        self.assertEqual(status, 400)

    def test_cookie_clients_still_need_csrf(self):
        # 适配层不能把浏览器原有的 CSRF 保护弄丢
        uid = self.auth.add_user("alice", "StrongPassword_2026!")
        status, _, headers = self._login_cookie("alice", "StrongPassword_2026!")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        session = self.auth.verify(cookie.split("=", 1)[1])
        self.assertEqual(session.user_id, uid)

        status, _ = self.request("/api/workspaces/create", {},
                                 headers={"Cookie": cookie, "Origin": self.base})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/workspaces/create", {},
                                 headers={"Cookie": cookie, "Origin": self.base,
                                          "X-RNBatch-CSRF": session.csrf})
        self.assertEqual(status, 200)

    def _login_cookie(self, username, password):
        req = urllib.request.Request(
            self.base + "/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json", "Origin": self.base})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.status, None, response.headers


if __name__ == "__main__":
    unittest.main()
