"""引擎测试。不依赖第三方库，直接 python -m unittest discover 即可。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rnbatch import engine  # noqa: E402
from rnbatch.engine import (MAX_NAME_BYTES, ConflictError, apply_plan,  # noqa: E402
                            build_name, check_conflicts, collect,
                            ensure_ext, find_symlinks, plan, sanitize, undo)


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


class TestExtSlot(unittest.TestCase):
    """{ext} 必须产出带点的真实扩展名，否则文件双击打不开。"""

    def test_ext_after_dash(self):
        self.assertEqual(
            build_name("{kw}-{n}-{ext}", "星海", 1, 2, "a", "20261018", ".txt", "文档"),
            "星海-01.txt",
        )

    def test_ext_after_dot(self):
        self.assertEqual(
            build_name("{kw}-{n}.{ext}", "星海", 1, 2, "a", "20261018", ".txt", "文档"),
            "星海-01.txt",
        )

    def test_ext_no_separator(self):
        self.assertEqual(
            build_name("{kw}{ext}", "星海", 1, 2, "a", "20261018", ".jpg", "照片"),
            "星海.jpg",
        )

    def test_ext_slot_without_extension(self):
        self.assertEqual(
            build_name("{kw}-{n}-{ext}", "星海", 1, 2, "noext", "20261018", "", "其他"),
            "星海-01",
        )

    def test_ensure_ext_appends(self):
        self.assertEqual(ensure_ext("星海-01-jpg", ".jpg"), "星海-01-jpg.jpg")
        self.assertEqual(ensure_ext("星海-01.jpg", ".jpg"), "星海-01.jpg")


class TestLength(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "a.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_keyword_truncated(self):
        warns: list = []
        pairs = plan(collect([self.dir]), "项目名" * 40, warnings=warns)
        name = pairs[0][1].name
        self.assertLessEqual(len(name.encode("utf-8")), MAX_NAME_BYTES)
        self.assertTrue(name.endswith(".txt"), "截断后仍必须保留扩展名")
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["type"], "truncated")

    def test_short_name_not_truncated(self):
        warns: list = []
        plan(collect([self.dir]), "星海中心", warnings=warns)
        self.assertEqual(warns, [])


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

    def test_conflict_detected_before_touching_disk(self):
        (self.dir / "星海中心-01.jpg").write_text("occupied", encoding="utf-8")
        files = [self.dir / f"IMG_{i}.jpg" for i in range(3)]
        pairs = plan(files, "星海中心", template="{kw}-{n:02}")
        conflicts = check_conflicts(pairs)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][1].name, "星海中心-01.jpg")

        with self.assertRaises(ConflictError):
            apply_plan(pairs)
        self.assertTrue((self.dir / "IMG_0.jpg").exists())
        self.assertEqual((self.dir / "星海中心-01.jpg").read_text(encoding="utf-8"),
                         "occupied")

    def test_no_conflict_when_occupier_is_also_a_source(self):
        # 占位文件本身也在待改名单里 → 两阶段改名会先挪走它，不算冲突
        (self.dir / "星海中心-01.jpg").write_text("x", encoding="utf-8")
        pairs = plan(collect([self.dir]), "星海中心", template="{kw}-{n:02}")
        self.assertEqual(check_conflicts(pairs), [])


class TestCollectOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for n in ["a.txt", "b.jpg", "c.pdf"]:
            (self.dir / n).write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_as_list(self):
        self.assertEqual(len(collect([self.dir], only=["txt", "jpg"])), 2)

    def test_only_as_comma_string(self):
        # 传字符串时不能按字符迭代（曾经导致静默返回 0 个文件）
        self.assertEqual(len(collect([self.dir], only="txt,jpg")), 2)

    def test_only_with_dots(self):
        self.assertEqual(len(collect([self.dir], only=[".TXT"])), 1)


class TestSymlinks(unittest.TestCase):
    """改名符号链接会让它指向的原文件一起改名，链接当场断掉。默认必须跳过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "real.txt").write_text("x", encoding="utf-8")
        try:
            (self.dir / "link.txt").symlink_to(self.dir / "real.txt")
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不支持符号链接")

    def tearDown(self):
        self.tmp.cleanup()

    def test_symlink_skipped_by_default(self):
        names = [p.name for p in collect([self.dir])]
        self.assertIn("real.txt", names)
        self.assertNotIn("link.txt", names)

    def test_find_symlinks_reports_them(self):
        self.assertEqual([p.name for p in find_symlinks([self.dir])], ["link.txt"])

    def test_opt_in_includes_symlinks(self):
        names = [p.name for p in collect([self.dir], include_symlinks=True)]
        self.assertIn("link.txt", names)


class TestUndoRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "a.txt").write_text("x", encoding="utf-8")
        self.undo_bak = engine.UNDO_DIR
        engine.UNDO_DIR = self.dir / "_undo"

    def tearDown(self):
        engine.UNDO_DIR = self.undo_bak
        self.tmp.cleanup()

    def test_corrupt_latest_log_is_skipped(self):
        pairs = plan(collect([self.dir]), "星海中心")
        log = apply_plan(pairs)
        good = Path(log).read_text(encoding="utf-8")
        Path(log).write_text("{broken", encoding="utf-8")

        # 再跑一次，产生一份完好的新日志
        pairs2 = plan(collect([self.dir]), "星海中心", start=5)
        apply_plan(pairs2)

        ok, skipped = undo()          # 应跳过损坏的，用最近一份完好的
        self.assertGreaterEqual(ok + skipped, 1)
        self.assertTrue(good)

    def test_all_logs_corrupt_raises_cleanly(self):
        pairs = plan(collect([self.dir]), "星海中心")
        log = apply_plan(pairs)
        Path(log).write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            undo()


class TestOnlyFilter(unittest.TestCase):
    """only 的入参形态千奇百怪，写错不该让整次改名失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for n in ("a.txt", "b.jpg", "c.pdf"):
            (self.dir / n).write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_string(self):
        got = {p.name for p in collect([self.dir], only="txt,jpg")}
        self.assertEqual(got, {"a.txt", "b.jpg"})

    def test_only_list(self):
        got = {p.name for p in collect([self.dir], only=["pdf"])}
        self.assertEqual(got, {"c.pdf"})

    def test_only_int_is_ignored(self):
        self.assertEqual(len(collect([self.dir], only=5)), 3)

    def test_only_dict_is_ignored(self):
        self.assertEqual(len(collect([self.dir], only={"a": 1})), 3)


class TestPreferredSources(unittest.TestCase):
    """挑基准文件时，正文最丰富的那个必须被选进来。

    早先只按文件名排序，「项目说明.txt」（正文四行）被「café.txt」（正文三个字）
    挤出候选集，候选关键词于是全是噪音。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bigger_text_beats_alphabetical(self):
        from rnbatch.extract import preferred_sources
        tiny = self.dir / "café-星海.txt"
        tiny.write_text("星海计划", encoding="utf-8")
        rich = self.dir / "项目说明.txt"
        rich.write_text("星海计划项目\n甲方：深圳星海科技有限公司\n" * 20, encoding="utf-8")
        got = preferred_sources([tiny, rich], 1)
        self.assertEqual(got, [rich])

    def test_office_doc_beats_plain_text(self):
        from rnbatch.extract import preferred_sources
        txt = self.dir / "log.txt"
        txt.write_text("x" * 5000, encoding="utf-8")
        docx = self.dir / "方案.docx"
        docx.write_bytes(b"PK\x03\x04fake")
        got = preferred_sources([txt, docx], 1)
        self.assertEqual(got, [docx])

    def test_photo_ranks_last(self):
        from rnbatch.extract import preferred_sources
        photo = self.dir / "IMG_0001.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100_000)
        txt = self.dir / "a.txt"
        txt.write_text("星海", encoding="utf-8")
        got = preferred_sources([photo, txt], 1)
        self.assertEqual(got, [txt])


if __name__ == "__main__":
    unittest.main()
