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

    def test_extract_blocks_path_traversal(self):
        # files 参数只允许指向 base 目录内部
        d = server.api_extract({"path": str(self.dir),
                                "files": ["../../../../etc/hosts"]})
        self.assertEqual(d["sources"], [])

    def test_extract_skips_directories(self):
        d = server.api_extract({"path": str(self.dir), "files": ["."]})
        self.assertEqual(d["sources"], [])

    def test_only_as_string(self):
        d = server.api_list({"path": str(self.dir), "only": "txt"})
        self.assertEqual(d["count"], 1)

    def test_preview_reports_conflict(self):
        # 只把 IMG_1.jpg 交给它，目标名 星海中心-01.jpg 已被另一个文件占用
        (self.dir / "星海中心-01.jpg").write_text("occupied", encoding="utf-8")
        d = server.api_preview({"path": str(self.dir / "IMG_1.jpg"),
                                "kw": "星海中心", "template": "{kw}-{n:02}"})
        self.assertEqual(d["conflicts"], 1)
        self.assertEqual(d["conflict_names"], ["星海中心-01.jpg"])

    def test_apply_refuses_on_conflict(self):
        (self.dir / "星海中心-01.jpg").write_text("occupied", encoding="utf-8")
        d = server.api_apply({"path": str(self.dir / "IMG_1.jpg"),
                              "kw": "星海中心", "template": "{kw}-{n:02}"})
        self.assertFalse(d["ok"])
        self.assertIn("占用", d["error"])
        self.assertTrue((self.dir / "IMG_1.jpg").exists())

    def test_preview_reports_truncation(self):
        d = server.api_preview({"path": str(self.dir), "kw": "项" * 200,
                                "only": ["txt"]})
        self.assertEqual(d["warnings"][0]["type"], "truncated")


class TestArgValidation(unittest.TestCase):
    """参数写错要给一句人话，不能抛 KeyError / invalid literal 这种原始报错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "a.txt").write_text("星海中心", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_path_message(self):
        with self.assertRaises(ValueError) as ctx:
            server.api_preview({"kw": "星海"})
        self.assertIn("path", str(ctx.exception))
        self.assertNotIn("KeyError", str(ctx.exception))

    def test_path_not_a_string(self):
        with self.assertRaises(ValueError):
            server.api_list({"path": 123})

    def test_bad_start_falls_back(self):
        d = server.api_preview({"path": str(self.dir), "kw": "星海", "start": "abc"})
        self.assertTrue(d["ok"])
        self.assertEqual(d["rows"][0]["to"], "星海-01.txt")

    def test_negative_pad_clamped(self):
        d = server.api_preview({"path": str(self.dir), "kw": "星海", "pad": -3})
        self.assertTrue(d["ok"])
        self.assertEqual(d["rows"][0]["to"], "星海-01.txt")

    def test_only_weird_type_does_not_crash(self):
        d = server.api_preview({"path": str(self.dir), "kw": "星海", "only": 5})
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 1)

    def test_extract_files_not_list(self):
        d = server.api_extract({"path": str(self.dir), "files": "a.txt"})
        self.assertTrue(d["ok"])          # 当成没指定，自动挑基准文件

    def test_err_text_strips_type_name(self):
        self.assertEqual(server.err_text(ValueError("关键词不能为空")), "关键词不能为空")
        self.assertIn("RuntimeError", server.err_text(RuntimeError("boom")))


if __name__ == "__main__":
    unittest.main()
