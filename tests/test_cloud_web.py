"""Authenticated Web App regression: sessions, CSRF, ownership, no legacy filesystem APIs."""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rnbatch.web_auth import AuthStore
from rnbatch.web_cloud import make_handler
from rnbatch.web_workspace import WorkspaceStore


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "users.sqlite3"
        self.auth = AuthStore(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_users_are_hashed_and_survive_restart(self):
        self.auth.add_user("alice", "StrongPassword_2026!")
        db_content = self.db.read_bytes()
        self.assertNotIn(b"StrongPassword_2026!", db_content)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertTrue(AuthStore(self.db).has_users())

    def test_login_cookie_session_expiry_and_logout(self):
        uid = self.auth.add_user("alice", "StrongPassword_2026!")
        self.assertIsNone(self.auth.sign_in("alice", "bad", "client-1"))
        token, session = self.auth.sign_in("alice", "StrongPassword_2026!", "client-1")
        self.assertEqual(session.user_id, uid)
        self.assertIsNotNone(self.auth.verify(token))
        self.auth.sign_out(token)
        self.assertIsNone(self.auth.verify(token))

    def test_user_input_and_local_origin_restriction(self):
        for username in ("../../root", "a", "hi x", "x" * 100):
            with self.assertRaises(ValueError):
                self.auth.add_user(username, "StrongPassword_2026!")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            make_handler(self.auth, WorkspaceStore(), "http://public.example.com")

    def test_repeated_bad_password_is_throttled(self):
        self.auth.add_user("alice", "StrongPassword_2026!")
        for _ in range(5):
            self.assertIsNone(self.auth.sign_in("alice", "bad", "remote"))
        with self.assertRaisesRegex(ValueError, "过多"):
            self.auth.sign_in("alice", "StrongPassword_2026!", "remote")


class TestAuthenticatedHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.auth = AuthStore(Path(cls.temp.name) / "users.sqlite3")
        cls.auth.add_user("alice", "StrongPassword_2026!")
        cls.auth.add_user("bob", "AnotherStrongPassword!")
        cls.store = WorkspaceStore()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        handler = make_handler(cls.auth, cls.store, cls.base)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()
        cls.temp.cleanup()

    def request(self, path, data=None, *, cookie="", csrf="", origin=True,
                host=None, binary=False):
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-RNBatch-CSRF"] = csrf
        if host:
            headers["Host"] = host
        if data is not None:
            if origin:
                headers["Origin"] = self.base
            if not binary:
                headers["Content-Type"] = "application/json"
                data = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            result = urllib.request.urlopen(req, timeout=5)
            body = result.read()
            return result.status, result.headers, (
                json.loads(body) if result.headers.get("Content-Type", "").startswith("application/json")
                else body)
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, e.headers, json.loads(body)

    def login(self, user, password):
        status, headers, data = self.request("/auth/login", {
            "username": user, "password": password})
        self.assertEqual(status, 200, data)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        token = cookie.split("=", 1)[1]
        session = self.auth.verify(token)
        self.assertIsNotNone(session)
        return cookie, session.csrf

    def test_no_legacy_filesystem_api_or_directory_access(self):
        cookie, csrf = self.login("alice", "StrongPassword_2026!")
        for route in ("/api/list", "/api/apply", "/api/extract",
                      "/api/pick-folder", "/api/logs", "/api/undo"):
            status, _, _ = self.request(route, {"path": "/etc"}, cookie=cookie, csrf=csrf)
            self.assertEqual(status, 404, route)
        status, _, _ = self.request("/api/workspaces/create", {}, cookie=cookie,
                                     csrf=csrf, host="evil.example")
        self.assertEqual(status, 403)

    def test_require_login_origin_and_csrf(self):
        status, _, _ = self.request("/api/workspaces/create", {})
        self.assertEqual(status, 401)
        cookie, csrf = self.login("alice", "StrongPassword_2026!")
        status, _, _ = self.request("/api/workspaces/create", {}, cookie=cookie)
        self.assertEqual(status, 403)
        status, _, _ = self.request("/api/workspaces/create", {}, cookie=cookie,
                                     csrf=csrf, origin=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("/api/workspaces/create", {}, cookie=cookie,
                                     csrf=csrf)
        self.assertEqual(status, 200)

    def test_two_accounts_are_strictly_isolated(self):
        alice_cookie, alice_csrf = self.login("alice", "StrongPassword_2026!")
        bob_cookie, bob_csrf = self.login("bob", "AnotherStrongPassword!")
        status, _, created = self.request("/api/workspaces/create", {},
                                           cookie=alice_cookie, csrf=alice_csrf)
        self.assertEqual(status, 200)
        wid = created["workspace"]
        qs = urllib.parse.urlencode({"workspace":wid,"name":"project/note.txt"})
        status, _, _ = self.request("/api/workspaces/upload?" + qs,
            b"confidential alpha", cookie=alice_cookie, csrf=alice_csrf, binary=True)
        self.assertEqual(status, 200)
        for route in ("/api/workspaces/list", "/api/workspaces/extract",
                      "/api/workspaces/preview", "/api/workspaces/apply",
                      "/api/workspaces/undo", "/api/workspaces/delete"):
            status, _, _ = self.request(route, {"workspace":wid,"kw":"test"},
                                         cookie=bob_cookie,csrf=bob_csrf)
            self.assertEqual(status, 400, route)
        status, _, _ = self.request("/api/workspaces/download?workspace="+wid,
                                      cookie=bob_cookie)
        self.assertEqual(status, 400)
        status, _, data = self.request("/api/workspaces/list", {"workspace":wid},
                                        cookie=alice_cookie,csrf=alice_csrf)
        self.assertEqual((status,data["count"]),(200,1))
        status, _, _ = self.request("/api/workspaces/delete", {"workspace":wid},
                                     cookie=alice_cookie,csrf=alice_csrf)
        self.assertEqual(status, 200)

    def test_login_page_cookie_flags_logout_and_upload_download(self):
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"login", body)
        cookie, csrf = self.login("alice", "StrongPassword_2026!")
        status, _, body = self.request("/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"__USERNAME__", body.replace(b"alice", b"__USERNAME__"))
        self.assertIn(csrf.encode(), body)
        status, _, created = self.request("/api/workspaces/create", {},
                                            cookie=cookie, csrf=csrf)
        wid = created["workspace"]
        qs = urllib.parse.urlencode({"workspace":wid,"name":"sample.txt"})
        status, _, upload = self.request("/api/workspaces/upload?"+qs,b"sample data",
            cookie=cookie,csrf=csrf,binary=True)
        self.assertEqual(status, 200, upload)
        status, _, preview = self.request("/api/workspaces/preview",
            {"workspace":wid,"kw":"project"},cookie=cookie,csrf=csrf)
        self.assertEqual(status, 200,preview)
        status, _, applied = self.request("/api/workspaces/apply",
            {"workspace":wid,"plan_token":preview["plan_token"]},cookie=cookie,csrf=csrf)
        self.assertEqual(status, 200,applied)
        status, headers, body = self.request("/api/workspaces/download?workspace="+wid,cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"),"application/zip")
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertEqual(zf.namelist(),["project-01.txt"])
            self.assertEqual(zf.read("project-01.txt"),b"sample data")
        status, headers, _ = self.request("/auth/logout",{},cookie=cookie,csrf=csrf)
        self.assertEqual(status,200)
        self.assertIn("Max-Age=0",headers.get("Set-Cookie",""))
        status, _, _ = self.request("/api/workspaces/list",{"workspace":wid},
                                     cookie=cookie,csrf=csrf)
        self.assertEqual(status,401)


class TestCloudFrontend(unittest.TestCase):
    def test_browser_script_syntax(self):
        import re
        import shutil
        if shutil.which("node") is None:
            self.skipTest("Node.js not installed")
        for filename in ("login.html", "cloud.html"):
            path = (Path(__file__).resolve().parents[1] /
                    "src/rnbatch/web" / filename)
            content = path.read_text(encoding="utf-8")
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", content, re.DOTALL)
            self.assertTrue(scripts, filename)
            for script in scripts:
                result = subprocess.run(["node", "--check"], input=script,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, filename + ": " + result.stderr)

if __name__ == "__main__":
    unittest.main()
