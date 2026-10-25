"""本地 Web 应用的 API 层测试（不起 HTTP，直接调处理函数）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rnbatch import engine  # noqa: E402
from rnbatch import server  # noqa: E402


class TestApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for n in ["IMG_1.jpg", "IMG_2.jpg"]:
            (self.dir / n).write_text("x", encoding="utf-8")
        (self.dir / "note.txt").write_text("星海中心项目 一期交付", encoding="utf-8")
        self.undo_bak = engine.UNDO_DIR
        engine.UNDO_DIR = self.dir / "_undo"

    def tearDown(self):
        engine.UNDO_DIR = self.undo_bak
        self.tmp.cleanup()

    def test_list(self):
        d = server.api_list({"path": str(self.dir)})
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 3)

    def test_extract_prefers_documents_over_photos(self):
        d = server.api_extract({"path": str(self.dir), "limit": 500})
        self.assertTrue(d["ok"])
        self.assertEqual(d["sources"][0]["name"], "note.txt")
        self.assertIn("星海中心", [c["kw"] for c in d["candidates"]])

    def test_preview_does_not_touch_disk(self):
        d = server.api_preview({"path": str(self.dir), "kw": "星海中心",
                                "template": "{kw}-{n:02}"})
        self.assertEqual(d["changed"], 3)
        self.assertTrue((self.dir / "IMG_1.jpg").exists())

    def test_apply_then_undo(self):
        a = server.api_apply({"path": str(self.dir), "kw": "星海中心",
                              "template": "{kw}-{n:02}"})
        self.assertEqual(a["renamed"], 3)
        u = server.api_undo({"log": a["log"]})
        self.assertEqual(u["restored"], 3)
        self.assertTrue((self.dir / "IMG_1.jpg").exists())

    def test_empty_keyword_rejected(self):
        with self.assertRaises(ValueError):
            server.api_preview({"path": str(self.dir), "kw": "  "})

    def test_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            server.api_list({"path": "/definitely/not/here"})

    def test_logs_after_apply(self):
        server.api_apply({"path": str(self.dir), "kw": "星海中心"})
        d = server.api_logs({})
        self.assertEqual(len(d["logs"]), 1)
        self.assertEqual(d["logs"][0]["keyword"], "星海中心")


if __name__ == "__main__":
    unittest.main()
