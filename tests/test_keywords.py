"""候选关键词抽取测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rnbatch.keywords import suggest  # noqa: E402

SAMPLE = """会议纪要
会议主题：星海中心项目 一期交付启动会
甲方：恒远置业集团；乙方：筑元建筑设计有限公司
会议结论：一期施工图于 11 月 15 日前完成交付，现场配合由张明远负责。
"""


class TestSuggest(unittest.TestCase):
    def test_picks_project_name_first(self):
        got = [c["kw"] for c in suggest(SAMPLE)]
        self.assertEqual(got[0], "星海中心")

    def test_returns_reason(self):
        for c in suggest(SAMPLE):
            self.assertTrue(c["why"], f"{c['kw']} 缺少 why")

    def test_placeholder_text_yields_nothing(self):
        self.assertEqual(suggest("（图片无可识别文字）尺寸 800x600"), [])

    def test_meta_markers_stripped(self):
        text = "[OCR 文字]\n星海中心项目协调会\n[图片信息] 尺寸 100x100"
        got = [c["kw"] for c in suggest(text)]
        self.assertTrue(any("星海" in g for g in got))
        self.assertFalse(any("图片信息" in g or "OCR" in g for g in got))

    def test_empty_input(self):
        self.assertEqual(suggest(""), [])

    def test_respects_limit(self):
        self.assertLessEqual(len(suggest(SAMPLE, limit=3)), 3)

    def test_scores_descending(self):
        scores = [c["score"] for c in suggest(SAMPLE)]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
