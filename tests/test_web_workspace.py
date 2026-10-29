"""Web App workspaces: actual HTTP and file-content roundtrips, no desktop UI."""
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rnbatch import server
from rnbatch.web_workspace import WorkspaceStore, safe_relative_name, validate_options


class TestWebWorkspaces(unittest.TestCase):
    def setUp(self):
        self.store = WorkspaceStore()
        self.sid = self.store.create()["workspace"]

    def tearDown(self):
        self.store.close()

    def upload(self, name, content):
        return self.store.upload(self.sid, name, io.BytesIO(content), len(content))

    def test_upload_extract_preview_apply_zip_undo(self):
        original = "星海计划项目 一期交付启动会\n甲方：恒远置业集团".encode("utf-8")
        self.upload("项目/会议纪要.txt", original)
        self.upload("项目/IMG_1.jpg", b"fake image is never parsed during rename")
        self.assertEqual(self.store.listing(self.sid)["count"], 2)
        keywords = self.store.extract(self.sid)
        self.assertTrue(keywords["ok"])
        self.assertTrue(any("星海" in c["kw"] for c in keywords["candidates"]))
        options = {"kw": "星海中心", "template": "{kw}-{n:02}"}
        preview = self.store.preview(self.sid, options)
        self.assertEqual(preview["count"], 2)
        self.assertTrue(all(row["to"].startswith("项目/星海中心") for row in preview["rows"]))
        with self.assertRaisesRegex(ValueError, "有效预览"):
            self.store.apply(self.sid, "invalid-token")
        result = self.store.apply(self.sid, preview["plan_token"])
        self.assertEqual(result["renamed"], 2)
        self.assertTrue(result["download_ready"])
        with zipfile.ZipFile(self.store.archive(self.sid)) as zf:
            names = sorted(zf.namelist())
            self.assertEqual(len(names), 2)
            self.assertTrue(all("星海中心" in name for name in names))
            text_file = next(n for n in names if n.endswith(".txt"))
            self.assertEqual(zf.read(text_file), original)
        undo = self.store.revert(self.sid)
        self.assertEqual((undo["restored"], undo["skipped"]), (2, 0))
        with zipfile.ZipFile(self.store.archive(self.sid)) as zf:
            self.assertEqual(sorted(zf.namelist()),
                             ["项目/IMG_1.jpg", "项目/会议纪要.txt"])

    def test_reject_path_traversal_and_hidden_name(self):
        for name in ("../etc/passwd", "/tmp/evil", "a/../../evil",
                     ".hidden/file.txt", "a//b", "a\\b", "a/./b"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    safe_relative_name(name)
                self.assertEqual(self.store.listing(self.sid)["count"], 0)

    def test_reject_oversized_file_and_partial_upload(self):
        with self.assertRaisesRegex(ValueError, "25 MB"):
            self.store.upload(self.sid, "big.bin", io.BytesIO(b""), 26*1024*1024)
        with self.assertRaisesRegex(ValueError, "中断"):
            self.store.upload(self.sid, "partial.txt", io.BytesIO(b"a"), 6)
        self.assertEqual(self.store.listing(self.sid)["count"], 0)

    def test_reject_duplicate_without_overwrite(self):
        self.upload("a.txt", b"first")
        with self.assertRaisesRegex(ValueError, "同名"):
            self.upload("a.txt", b"second")
        self.assertEqual((self.store.get(self.sid).files / "a.txt").read_bytes(),
                         b"first")

    def test_upload_after_preview_denied(self):
        self.upload("a.txt", b"example")
        self.store.preview(self.sid, {"kw": "hello"})
        with self.assertRaisesRegex(ValueError, "预览"):
            self.upload("b.txt", b"data")

    def test_template_validation(self):
        for tpl in ("{kw}-{n:99999999}", "{kw}-{missing}", "x" * 181):
            with self.subTest(template=tpl):
                with self.assertRaises(ValueError):
                    validate_options({"kw": "abc", "template": tpl})
        self.assertEqual(validate_options({"kw": "abc", "template": "{kw}-{n:02}"})["kw"],
                         "abc")

    def test_workspaces_keep_files_and_logs_separate(self):
        self.upload("private.txt", b"workspace-one")
        sid2 = self.store.create()["workspace"]
        self.store.upload(sid2, "other.txt", io.BytesIO(b"workspace-two"), 13)
        self.assertEqual(self.store.listing(self.sid)["count"], 1)
        self.assertEqual(self.store.listing(sid2)["count"], 1)
        p1 = self.store.preview(self.sid, {"kw": "first"})
        self.store.apply(self.sid, p1["plan_token"])
        p2 = self.store.preview(sid2, {"kw": "second"})
        self.store.apply(sid2, p2["plan_token"])
        self.assertNotEqual(self.store.get(self.sid).logs,
                            self.store.get(sid2).logs)
        self.assertEqual(len(list(self.store.get(self.sid).logs.glob("*.json"))), 1)
        self.assertEqual(len(list(self.store.get(sid2).logs.glob("*.json"))), 1)
        self.assertTrue(self.store.get(sid2).files.joinpath("second-01.txt").exists())

    def test_plan_snapshot_changes_refuse_apply(self):
        self.upload("a.txt", b"before")
        preview = self.store.preview(self.sid, {"kw": "changed"})
        p = self.store.get(self.sid).files / "a.txt"
        p.write_bytes(b"modified content!")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.store.apply(self.sid, preview["plan_token"])

    def test_explicit_delete_removes_files(self):
        w = self.store.get(self.sid)
        base = w.base
        self.upload("x.txt", b"ok")
        self.assertTrue(base.exists())
        self.store.delete(self.sid)
        self.assertFalse(base.exists())
        with self.assertRaises(ValueError):
            self.store.get(self.sid)


class TestHttpWebWorkspaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_store = server.browser_store
        cls.store = WorkspaceStore()
        server.browser_store = cls.store
        server.Handler.token = "http-test-token"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()
        server.browser_store = cls.old_store

    def request(self, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=body, headers=headers or {})
        return urllib.request.urlopen(req, timeout=5)

    def json_request(self, path, data):
        url = path + "?t=http-test-token"
        with self.request(url, json.dumps(data).encode("utf-8"),
                          {"Content-Type": "application/json"}) as resp:
            return json.load(resp)

    def test_auth_upload_and_download(self):
        sid = self.json_request("/api/workspaces/create", {})["workspace"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/workspaces/upload?workspace="+sid+"&name=test.txt",
                         b"abc")
        self.assertEqual(ctx.exception.code, 403)
        path = ("/api/workspaces/upload?t=http-test-token&workspace=" + sid +
                "&name=" + urllib.parse.quote("目录/说明.txt"))
        with self.request(path, "星海计划项目".encode("utf-8"),
                          {"Content-Type": "application/octet-stream"}) as resp:
            self.assertTrue(json.load(resp)["ok"])
        preview = self.json_request("/api/workspaces/preview",
                                    {"workspace": sid, "kw": "星海中心"})
        applied = self.json_request("/api/workspaces/apply",
                                    {"workspace": sid, "plan_token": preview["plan_token"]})
        self.assertEqual(applied["renamed"], 1)
        with self.request("/api/workspaces/download?t=http-test-token&workspace="+sid) as response:
            archive = io.BytesIO(response.read())
            self.assertEqual(response.headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(archive) as zf:
            self.assertEqual(len(zf.namelist()), 1)
            self.assertTrue(zf.namelist()[0].startswith("目录/星海中心"))
        self.json_request("/api/workspaces/delete", {"workspace": sid})

    def test_workspace_create_disallows_get(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/workspaces/create?t=http-test-token")
        self.assertEqual(ctx.exception.code, 405)

    def test_http_rejects_invalid_json_and_excessive_content_length(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/workspaces/create?t=http-test-token",
                         b"{}",
                         {"Content-Type":"application/json",
                          "Content-Length":str(2*1024*1024)})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
