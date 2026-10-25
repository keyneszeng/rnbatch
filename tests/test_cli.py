"""命令行层测试：退出码 + 关键输出。

退出码是给脚本用的契约，改了就是破坏性变更，所以单独钉住。
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rnbatch import engine  # noqa: E402
from rnbatch.cli import main  # noqa: E402


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as e:          # argparse 的用法错误走这里
            code = e.code if isinstance(e.code, int) else 2
    return code, out.getvalue(), err.getvalue()


class TestExitCodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for n in ("IMG_1.jpg", "IMG_2.jpg"):
            (self.dir / n).write_text("x", encoding="utf-8")
        (self.dir / "note.txt").write_text(
            "星海计划项目\n甲方：深圳星海科技有限公司\n星海计划预计 2026 年上线。",
            encoding="utf-8")
        self.undo_bak = engine.UNDO_DIR
        engine.UNDO_DIR = self.dir / "_undo"

    def tearDown(self):
        engine.UNDO_DIR = self.undo_bak
        self.tmp.cleanup()

    def test_preview_ok_is_zero(self):
        code, out, _ = run(["preview", str(self.dir), "--kw", "星海计划"])
        self.assertEqual(code, 0)
        self.assertIn("星海计划-01.jpg", out)

    def test_preview_conflict_is_three(self):
        (self.dir / "星海计划-01.jpg").write_text("occupied", encoding="utf-8")
        code, out, _ = run(["preview", str(self.dir / "IMG_1.jpg"),
                            "--kw", "星海计划", "--template", "{kw}-{n:02}"])
        self.assertEqual(code, 3)
        self.assertIn("已被目录里的其他文件占用", out)

    def test_apply_conflict_is_one(self):
        (self.dir / "星海计划-01.jpg").write_text("occupied", encoding="utf-8")
        code, _, err = run(["apply", str(self.dir / "IMG_1.jpg"),
                            "--kw", "星海计划", "--template", "{kw}-{n:02}"])
        self.assertEqual(code, 1)
        self.assertIn("没有改动任何文件", err)
        self.assertTrue((self.dir / "IMG_1.jpg").exists())

    def test_missing_path_is_two(self):
        code, _, err = run(["preview", str(self.dir / "nope"), "--kw", "星海"])
        self.assertEqual(code, 2)
        self.assertIn("路径不存在", err)

    def test_empty_keyword_is_two(self):
        code, _, err = run(["preview", str(self.dir), "--kw", "///"])
        self.assertEqual(code, 2)
        self.assertIn("关键词为空", err)

    def test_apply_then_undo_roundtrip(self):
        code, out, _ = run(["apply", str(self.dir), "--kw", "星海计划",
                            "--template", "{kw}-{n:02}"])
        self.assertEqual(code, 0)
        self.assertIn("已改名", out)
        self.assertTrue((self.dir / "星海计划-01.jpg").exists())

        code, out, _ = run(["undo"])
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "IMG_1.jpg").exists())


class TestExtractOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "项目说明.txt").write_text(
            "星海计划项目\n甲方：深圳星海科技有限公司\n本项目建设内容如下。",
            encoding="utf-8")
        (self.dir / "a.txt").write_text("星海", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_prints_candidates(self):
        """extract 不提候选词的话，这个命令就只剩 cat 的作用了。"""
        code, out, _ = run(["extract", str(self.dir)])
        self.assertEqual(code, 0)
        self.assertIn("候选关键词", out)
        self.assertIn("星海计划", out)
        self.assertIn("--kw", out)          # 给出下一步命令

    def test_extract_picks_richest_file(self):
        """正文四行的文件要比正文两个字的文件优先被读。"""
        code, out, _ = run(["extract", str(self.dir)])
        self.assertIn("项目说明.txt", out)

    def test_extract_skips_broken_file(self):
        """损坏的 docx 不能占掉名额。"""
        (self.dir / "坏文件.docx").write_bytes(b"not a real docx")
        code, out, _ = run(["extract", str(self.dir), "--max-files", "1"])
        self.assertEqual(code, 0)
        self.assertIn("项目说明.txt", out)
        self.assertNotIn("坏文件.docx", out)


if __name__ == "__main__":
    unittest.main()
