"""引擎测试。不依赖第三方库，直接 python -m unittest discover 即可。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rnbatch import engine  # noqa: E402
from rnbatch.engine import (apply_plan, build_name, collect, plan,  # noqa: E402
                            sanitize, undo)


class TestSanitize(unittest.TestCase):
    def test_strips_illegal_chars(self):
        self.assertEqual(sanitize('a/b\\c:d*e?f"g<h>i|j'), "abcdefghij")

    def test_collapses_whitespace(self):
        self.assertEqual(sanitize("  星海   中心  "), "星海 中心")

    def test_nfd_to_nfc(self):
        nfd = "星海中心".encode("utf-8")
        self.assertEqual(sanitize(nfd.decode("utf-8")), "星海中心")

    def test_empty(self):
        self.assertEqual(sanitize("///"), "")


class TestBuildName(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            build_name("{kw}-{n}", "星海中心", 3, 2, "IMG_0421", "20261018", ".jpg", "照片"),
            "星海中心-03",
        )

    def test_inline_padding(self):
        self.assertEqual(
            build_name("{kw}-{n:03}", "A", 7, 2, "x", "20261018", ".jpg", "照片"),
            "A-007",
        )

    def test_type_slot(self):
        self.assertEqual(
            build_name("{kw}-{n:02}-{type}", "星海", 1, 2, "x", "20261018", ".pdf", "PDF"),
            "星海-01-PDF",
        )

    def test_orig_and_date(self):
        self.assertEqual(
            build_name("{kw}-{date}-{orig}", "星海", 1, 2, "方案 v3", "20261018", ".pptx", "演示"),
            "星海-20261018-方案 v3",
        )


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name in ["IMG_2.JPG", "IMG_1.JPG", "a.docx"]:
            (self.dir / name).write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_width(self):
        pairs = plan(collect([self.dir]), "星海中心")
        names = [b.name for _, b in pairs]
        self.assertEqual(names, ["星海中心-01.docx", "星海中心-02.JPG", "星海中心-03.JPG"])

    def test_conflict_gets_suffix(self):
        # 模板里不含 {n}，三个文件会撞名 → 追加 -2、-3
        pairs = plan(collect([self.dir]), "同名", template="{kw}")
        names = [b.name for _, b in pairs]
        self.assertEqual(len(set(names)), 3)
        self.assertIn("同名-2.JPG", names)

    def test_empty_keyword_rejected(self):
        with self.assertRaises(ValueError):
            plan(collect([self.dir]), "   ")

    def test_only_filter(self):
        files = collect([self.dir], only={"jpg"})
        self.assertEqual(len(files), 2)


class TestApplyUndo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for i in range(3):
            (self.dir / f"IMG_{i}.jpg").write_text("x", encoding="utf-8")
        self.undo_bak = engine.UNDO_DIR
        engine.UNDO_DIR = self.dir / "_undo"

    def tearDown(self):
        engine.UNDO_DIR = self.undo_bak
        self.tmp.cleanup()

    def test_roundtrip(self):
        before = sorted(p.name for p in self.dir.iterdir() if p.is_file())
        pairs = plan(collect([self.dir]), "星海中心", template="{kw}-{n:02}")
        log = apply_plan(pairs, "星海中心", "{kw}-{n:02}")
        self.assertIsNotNone(log)
        after = sorted(p.name for p in self.dir.iterdir() if p.is_file())
        self.assertEqual(after, ["星海中心-01.jpg", "星海中心-02.jpg", "星海中心-03.jpg"])

        ok, skipped = undo(log)
        self.assertEqual((ok, skipped), (3, 0))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.is_file()), before)

    def test_noop_returns_none(self):
        pairs = plan(collect([self.dir]), "IMG")  # 不会产生实际变化的名字
        pairs = [(a, a) for a, _ in pairs]
        self.assertIsNone(apply_plan(pairs))

    def test_target_exists_rolls_back(self):
        # 目录里已有一个「目标同名」文件，且它不在待改名单里 → 必须报错并回滚
        (self.dir / "星海中心-01.jpg").write_text("occupied", encoding="utf-8")
        files = [self.dir / f"IMG_{i}.jpg" for i in range(3)]
        pairs = plan(files, "星海中心", template="{kw}-{n:02}")
        with self.assertRaises(Exception):
            apply_plan(pairs)
        # 原名必须还在
        for i in range(3):
            self.assertTrue((self.dir / f"IMG_{i}.jpg").exists())


if __name__ == "__main__":
    unittest.main()
