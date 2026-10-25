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


class TestNameQuality(unittest.TestCase):
    """回归：真实语料里挖出来的几类噪音，不能再回来。

    每一条都对应一次实测踩坑，别随手删。
    """

    def _top(self, text):
        got = suggest(text, 3)
        return got[0]["kw"] if got else ""

    def test_category_suffix_stripped(self):
        """「星海计划项目」→「星海计划」，项目/工程只是类别词。"""
        self.assertEqual(self._top("星海计划项目\n甲方：深圳星海科技有限公司\n"
                                   "本项目建设内容如下\n星海计划预计 2026 年上线。"),
                         "星海计划")

    def test_company_full_name_kept(self):
        """公司全称才是实体名，不能砍成「深圳星海科技」。"""
        got = [c["kw"] for c in suggest(
            "甲方：深圳星海科技有限公司\n乙方：中建三局集团有限公司")]
        self.assertIn("深圳星海科技有限公司", got)
        self.assertIn("中建三局集团有限公司", got)

    def test_company_with_gongcheng_not_cut(self):
        """公司名里含「工程」时不能当类别词切掉。"""
        got = [c["kw"] for c in suggest(
            "报价单位：广东启明照明工程有限公司\n项目名称：滨海新区智慧路灯改造工程")]
        self.assertIn("广东启明照明工程有限公司", got)
        self.assertIn("滨海新区智慧路灯改造", got)

    def test_long_title_cuts_at_first_category_word(self):
        """长标题在第一个类别词处切：「关于星海计划项目一期建设工程…」→「星海计划」。"""
        self.assertEqual(self._top("关于星海计划项目一期建设工程竣工验收情况的汇报材料"),
                         "星海计划")

    def test_half_sentence_fragments_filtered(self):
        """「年上线」「星海计划预计」这类半句话不能进候选。"""
        got = [c["kw"] for c in suggest(
            "星海计划项目\n星海计划预计 2026 年上线。\n本项目建设内容如下")]
        self.assertNotIn("年上线", got)
        self.assertNotIn("星海计划预计", got)

    def test_suffix_only_org_rejected(self):
        """「集团有限公司」这种纯后缀片段不是名字。"""
        got = [c["kw"] for c in suggest("乙方：中建三局集团有限公司")]
        self.assertNotIn("集团有限公司", got)

    def test_english_names(self):
        got = [c["kw"] for c in suggest(
            "Project StarOcean\nClient: Acme Corp\nStatus: in progress")]
        self.assertIn("Acme Corp", got)
        self.assertIn("Project StarOcean", got)
        self.assertNotIn("Status", got)

    def test_english_does_not_span_newlines(self):
        got = [c["kw"] for c in suggest("Acme Corp\nStatus: in progress")]
        self.assertFalse(any("\n" in g for g in got))


if __name__ == "__main__":
    unittest.main()
