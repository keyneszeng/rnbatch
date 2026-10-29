"""真实文件事务回归：双向循环、故障注入、先记录再变更。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rnbatch import engine  # noqa: E402


class TestTransactionalRename(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_undo_dir = engine.UNDO_DIR
        engine.UNDO_DIR = self.root / "undo"

    def tearDown(self):
        engine.UNDO_DIR = self.original_undo_dir
        self.tmp.cleanup()

    def make_files(self, names):
        for i, name in enumerate(names):
            (self.root / name).write_text(f"payload-{i}", encoding="utf-8")

    def snapshot(self):
        return {p.name: p.read_text(encoding="utf-8")
                for p in self.root.iterdir() if p.is_file()}

    def test_journal_is_durable_before_first_file_rename(self):
        self.make_files(["old1.txt", "old2.txt"])
        before = self.snapshot()
        pairs = [(self.root / "old1.txt", self.root / "new1.txt"),
                 (self.root / "old2.txt", self.root / "new2.txt")]
        original_rename = os.rename
        observed = []

        def checked_rename(src, dst):
            if not observed:
                journals = list(engine.UNDO_DIR.glob("*.pending"))
                self.assertEqual(len(journals), 1)
                data = json.loads(journals[0].read_text(encoding="utf-8"))
                self.assertEqual(len(data["records"]), 2)
                self.assertTrue(all("tmp" in r for r in data["records"]))
                observed.append(True)
            return original_rename(src, dst)

        with patch.object(engine.os, "rename", side_effect=checked_rename):
            log = engine.apply_plan(pairs, "name")
        self.assertTrue(observed)
        self.assertTrue(log.exists())
        self.assertFalse(list(engine.UNDO_DIR.glob("*.pending")))
        self.assertEqual(engine.undo(log), (2, 0))
        self.assertEqual(self.snapshot(), before)

    def test_failure_after_first_final_name_restores_every_original(self):
        self.make_files(["a.txt", "b.txt", "c.txt"])
        before = self.snapshot()
        pairs = [(self.root / name, self.root / f"new-{name}")
                 for name in ("a.txt", "b.txt", "c.txt")]
        original_rename = os.rename
        calls = 0

        def failing_rename(src, dst):
            nonlocal calls
            calls += 1
            if calls == 5:  # 3 staging moves + 1 completed move + next fails
                raise OSError("injected phase-2 failure")
            return original_rename(src, dst)

        with patch.object(engine.os, "rename", side_effect=failing_rename):
            with self.assertRaisesRegex(OSError, "injected"):
                engine.apply_plan(pairs)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(engine.UNDO_DIR.glob("*.pending")), [])
        self.assertEqual(list(engine.UNDO_DIR.glob("*.json")), [])
        self.assertFalse(list(self.root.glob(".rnbatch.tmp.*")))

    def test_log_publish_failure_does_not_leave_renamed_files(self):
        self.make_files(["a.txt", "b.txt"])
        before = self.snapshot()
        pairs = [(self.root / "a.txt", self.root / "new-a.txt"),
                 (self.root / "b.txt", self.root / "new-b.txt")]
        with patch.object(engine.os, "replace", side_effect=OSError("journal publish failed")):
            with self.assertRaisesRegex(OSError, "journal publish failed"):
                engine.apply_plan(pairs)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(engine.UNDO_DIR.glob("*.pending")))

    def test_undo_file_name_swap_preserves_both_contents(self):
        self.make_files(["a.txt", "b.txt"])
        before = self.snapshot()
        a, b = self.root / "a.txt", self.root / "b.txt"
        log = engine.apply_plan([(a, b), (b, a)])
        self.assertEqual(self.snapshot(), {"a.txt": before["b.txt"],
                                            "b.txt": before["a.txt"]})
        self.assertEqual(engine.undo(log), (2, 0))
        self.assertEqual(self.snapshot(), before)

    def test_undo_three_file_cycle_preserves_all_contents(self):
        self.make_files(["a.txt", "b.txt", "c.txt"])
        before = self.snapshot()
        a, b, c = (self.root / x for x in ("a.txt", "b.txt", "c.txt"))
        log = engine.apply_plan([(a, b), (b, c), (c, a)])
        self.assertEqual(engine.undo(log), (3, 0))
        self.assertEqual(self.snapshot(), before)

    def test_undo_failure_after_first_restore_preserves_renamed_state(self):
        self.make_files(["a.txt", "b.txt", "c.txt"])
        pairs = [(self.root / n, self.root / f"new-{n}")
                 for n in ("a.txt", "b.txt", "c.txt")]
        log = engine.apply_plan(pairs)
        renamed_state = self.snapshot()
        original_rename = os.rename
        calls = 0

        def failing_rename(src, dst):
            nonlocal calls
            calls += 1
            if calls == 5:
                raise OSError("injected undo failure")
            return original_rename(src, dst)

        with patch.object(engine.os, "rename", side_effect=failing_rename):
            with self.assertRaisesRegex(OSError, "injected undo failure"):
                engine.undo(log)
        self.assertEqual(self.snapshot(), renamed_state)
        self.assertTrue(log.exists())
        self.assertEqual(engine.undo(log), (3, 0))
        self.assertEqual(sorted(self.snapshot()), ["a.txt", "b.txt", "c.txt"])


if __name__ == "__main__":
    unittest.main()
