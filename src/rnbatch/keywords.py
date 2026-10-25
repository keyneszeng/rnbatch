"""候选关键词抽取（纯启发式，不依赖任何模型）。

定位：给人和 agent 提供「从这段正文里，哪几串词最可能是项目名/公司名」的候选，
而不是替人做决定。真正的判断仍由使用者或 LLM 完成。

打分维度：
  1. 显式结构 —— 「」《》里的内容、XX项目、XX公司/集团/事务所
  2. 位置 —— 出现在开头几行（标题区）的权重更高
  3. 频次 —— 同一片段重复出现说明是主题词
"""
from __future__ import annotations

import re
from collections import Counter

CJK = r"\u4e00-\u9fff"

# 通用词：出现在标题里但没有区分度，命中要降权
STOPWORDS = {
    "会议纪要", "会议", "纪要", "方案", "汇报", "报告", "清单", "合同", "协议",
    "通知", "说明", "计划", "总结", "记录", "附件", "目录", "封面", "首页",
    "第一", "第二", "第三", "一期", "二期", "三期", "本次", "以上", "以下",
    "甲方", "乙方", "内容", "事项", "时间", "地点", "人员", "负责", "负责人",
    "设计", "施工", "材料", "数量", "序号", "名称", "备注", "合计", "总价",
}

# 组织机构后缀
ORG_SUFFIX = ("有限公司", "股份公司", "集团", "公司", "事务所", "设计院", "研究院",
              "工作室", "中心", "大学", "医院", "银行", "局", "委", "办")

QUOTED = re.compile(r"[「『\"'“”‘’《【]([^」』\"'“”‘’》】\n]{2,30})[」』\"'“”‘’》】]")
PROJECT = re.compile(rf"([{CJK}A-Za-z0-9]{{2,20}}?(?:项目|工程|中心|大厦|园区|基地))")
ORG = re.compile(rf"([{CJK}A-Za-z0-9（）()]{{2,20}}?(?:{'|'.join(ORG_SUFFIX)}))")
CJK_RUN = re.compile(rf"[{CJK}]{{2,12}}")


def _clean(s: str) -> str:
    s = s.strip(" \t\r\n·•-—_|:：,，.。、()（）[]【】")
    return s


def _valid(s: str) -> bool:
    if not (2 <= len(s) <= 20):
        return False
    if s in STOPWORDS:
        return False
    if re.fullmatch(r"[\d\s.\-]+", s):
        return False
    return True


def _strip_meta(text: str) -> str:
    """去掉抽取层自己加的标记行（[OCR 文字] / [图片信息] / （图片无可识别文字）…），
    否则这些占位文字会被当成关键词候选。"""
    keep = []
    for line in text.splitlines():
        if re.match(r"^\s*[\[（(]", line):
            continue
        keep.append(line)
    return "\n".join(keep)


def suggest(text: str, limit: int = 8) -> list[dict]:
    """返回 [{"kw": 词, "score": 分, "why": 依据}]，按分降序。"""
    if not text:
        return []
    text = _strip_meta(text)
    if len(text.strip()) < 2:
        return []

    head = text[:300]
    scores: Counter = Counter()
    reasons: dict[str, set[str]] = {}

    def bump(word: str, weight: float, why: str):
        word = _clean(word)
        if not _valid(word):
            return
        scores[word] += weight
        reasons.setdefault(word, set()).add(why)

    # 1) 显式结构
    for m in QUOTED.finditer(text):
        bump(m.group(1), 5.0, "引号内")
    for m in PROJECT.finditer(text):
        bump(m.group(1), 4.0, "含「项目/工程」")
    for m in ORG.finditer(text):
        bump(m.group(1), 3.5, "机构名")

    # 2) 位置：开头几行里的中文片段
    for line in head.splitlines()[:4]:
        for m in CJK_RUN.finditer(line):
            frag = m.group(0)
            if frag in STOPWORDS:
                continue
            bump(frag, 2.0, "出现在标题区")

    # 3) 频次：全文 2–8 字中文片段
    for m in re.finditer(rf"[{CJK}]{{2,8}}", text):
        frag = m.group(0)
        if frag in STOPWORDS or not _valid(frag):
            continue
        scores[frag] += 0.15

    # 4) 出现次数加成
    for word in list(scores):
        n = text.count(word)
        if n > 1:
            scores[word] += min(n, 6) * 0.6
            reasons.setdefault(word, set()).add(f"出现 {n} 次")

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], -len(kv[0])))

    # 去掉被更长候选完全包含的短词（"星海" vs "星海中心"）
    out: list[dict] = []
    for word, sc in ranked:
        if any(word != o["kw"] and word in o["kw"] for o in out):
            continue
        out.append({"kw": word, "score": round(sc, 1),
                    "why": "、".join(sorted(reasons.get(word, [])))})
        if len(out) >= limit:
            break
    return out
