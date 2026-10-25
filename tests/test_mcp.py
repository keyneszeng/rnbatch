"""MCP 协议测试：不需要任何 MCP 客户端，直接喂 JSON-RPC 消息。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rnbatch import engine  # noqa: E402
from rnbatch.mcp_server import handle  # noqa: E402


class TestHandshake(unittest.TestCase):
    def test_initialize(self):
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        self.assertEqual(r["id"], 1)
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(r["result"]["serverInfo"]["name"], "rnbatch")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_unknown_protocol_falls_back(self):
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01"}})
        self.assertIn("protocolVersion", r["result"])

    def test_notification_returns_none(self):
        self.assertIsNone(handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping(self):
        self.assertEqual(handle({"jsonrpc": "2.0", "id": 9, "method": "ping"})["result"], {})

    def test_unknown_method(self):
        r = handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})
        self.assertEqual(r["error"]["code"], -32601)


class TestTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for n in ["IMG_1.jpg", "IMG_2.jpg", "note.txt"]:
            (self.dir / n).write_text("星海中心项目", encoding="utf-8")
        self.undo_bak = engine.UNDO_DIR
        engine.UNDO_DIR = self.dir / "_undo"

    def tearDown(self):
        engine.UNDO_DIR = self.undo_bak
        self.tmp.cleanup()

    def _call(self, name, args):
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args}})
        self.assertFalse(r["result"]["isError"], r["result"]["content"][0]["text"])
        return json.loads(r["result"]["content"][0]["text"])

    def test_tools_list(self):
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(names, ["list_files", "extract_text", "preview_rename",
                                 "apply_rename", "undo_rename", "recent_renames"])
        for t in r["result"]["tools"]:
            self.assertIn("inputSchema", t)

    def test_list_files(self):
        d = self._call("list_files", {"path": str(self.dir)})
        self.assertEqual(d["count"], 3)

    def test_extract_text(self):
        d = self._call("extract_text", {"path": str(self.dir / "note.txt")})
        self.assertIn("星海中心", [c["kw"] for c in d["candidates"]])

    def test_preview_then_apply_then_undo(self):
        p = self._call("preview_rename",
                       {"path": str(self.dir), "keyword": "星海中心",
                        "template": "{kw}-{n:02}"})
        self.assertEqual(p["changed"], 3)

        a = self._call("apply_rename",
                       {"path": str(self.dir), "keyword": "星海中心",
                        "template": "{kw}-{n:02}"})
        self.assertEqual(a["renamed"], 3)
        self.assertIn("undo_log", a)

        u = self._call("undo_rename", {"log": a["undo_log"]})
        self.assertEqual(u["restored"], 3)
        self.assertTrue((self.dir / "IMG_1.jpg").exists())

    def test_unknown_tool(self):
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_bad_path_is_tool_error_not_crash(self):
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list_files",
                               "arguments": {"path": "/definitely/not/here"}}})
        self.assertTrue(r["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
