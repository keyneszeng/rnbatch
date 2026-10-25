"""候选关键词抽取（纯启发式，不依赖任何模型）。

定位：给人和 agent 提供「从这段正文里，哪几串词最可能是项目名/公司名」的候选，
而不是替人做决定。真正的判断仍由使用者或 LLM 完成。

打分维度：
  1. 显式结构 —— 「」《》里的内容、XX项目、XX公司/集团/事务所、英文机构名
  2. 位置 —— 出现在开头几行（标题区）的权重更高
  3. 频次 —— 同一片段重复出现说明是主题词

三条清洗规则，全是被真实语料逼出来的：
  - 片段开头/结尾挂着虚词、动词 → 削掉再判断
    （「关于星海计划项目」→「星海计划项目」、「星海计划预计」→「星海计划」）
  - 「X项目」「X工程」收敛成「X」—— 项目/工程只是类别词，
    文件名里写「星海计划」比「星海计划项目」干净。
    但「X有限公司」不收敛，公司全称才是对的实体名。
  - 只由机构后缀拼成的片段（「集团有限公司」）直接丢掉。
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
    # 公文 / 合同里的套话
    "关于", "对于", "针对", "现将", "各部门", "各单位", "下属单位", "有关单位",
    "建设工程", "施工合同", "合同总价", "付款方式", "工程名称", "参会人员",
    "会议室", "有关事项", "如下", "特此", "印发", "项目名称", "报价单位",
    "联系人", "联系电话", "项目编号", "编制单位", "审核人", "批准人",
    # 动词 / 半句话的高频尾巴，单独成词没有意义
    "建设", "采购", "供应", "服务", "装修", "上线", "预计", "完成", "实施",
    "开展", "进行", "落实", "推进", "确保", "加强", "提升", "贯彻", "执行",
}

# 组织机构后缀
ORG_SUFFIX = ("有限公司", "股份公司", "集团", "公司", "事务所", "设计院", "研究院",
              "工作室", "中心", "大学", "医院", "银行", "局", "委", "办")

# 这些后缀只是「类别词」，去掉之后才是名字本体
CATEGORY_SUFFIX = ("项目", "工程")

# 以这些单字开头的片段，几乎一定是半句话（「年上线」「其内容…」）
BAD_HEAD_CHAR = set("年月日第其此那这我你他她它和与及或但而因所已将可需应未"
                    "不于对从向把被让使由据按依若如则即并且也都还又再更最很太较略些每各另余"
                    "的请现")

# 挂在片段头 / 尾的虚词、动词 —— 削掉再判断，而不是整条丢掉
BAD_HEAD_WORD = ("关于", "对于", "针对", "参照", "依据", "根据", "按照", "现将",
                 "如下", "以下", "以上", "其中", "预计", "目前", "同时", "另外",
                 "此外", "因此", "所以", "但是", "如果", "由于", "相关", "有关",
                 "上述", "本次", "本项目", "本公司", "本方案", "本报告", "本协议",
                 "本合同", "本文", "本表", "我们", "贵司", "我方", "贵方",
                 "关于印发", "各部门", "各")
BAD_TAIL_WORD = ("如下", "以上", "以下", "其中", "等等", "以及", "并且", "而且",
                 "但是", "因此", "所以", "由于", "如果", "根据", "按照", "相关",
                 "有关", "进行", "实施", "完成", "开展", "预计", "目前", "同时",
                 "另外", "此外", "的是", "可以", "需要", "应当", "必须", "要求",
                 "的通知", "的情况", "的材料", "的内容")

QUOTED = re.compile(r"[「『\"'“”‘’《【]([^」』\"'“”‘’》】\n]{2,30})[」』\"'“”‘’》】]")
# 贪婪匹配：先吃满再回退找后缀，才能拿到「中建三局集团有限公司」这样的全称
PROJECT = re.compile(rf"([{CJK}A-Za-z0-9]{{2,20}}(?:{'|'.join(CATEGORY_SUFFIX)}))")
ORG = re.compile(rf"([{CJK}A-Za-z0-9（）()]{{2,20}}(?:{'|'.join(ORG_SUFFIX)}))")
CJK_RUN = re.compile(rf"[{CJK}]{{2,}}")

EN_ORG_SUFFIX = {"inc", "corp", "corporation", "ltd", "limited", "llc", "gmbh",
                 "co", "group", "holdings", "technologies", "technology", "labs",
                 "studio", "partners", "company"}
# 只吃空格/制表符，不吃换行 —— 否则会跨行拼出「Acme Corp\nStatus」这种东西
EN_NAME = re.compile(r"\b([A-Z][A-Za-z0-9&.'-]*(?:[ \t]+[A-Z][A-Za-z0-9&.'-]*){0,3})\b")

# 长词优先：否则「关于印发」会先被「关于」吃掉，剩个「印发」
_BAD_HEAD_SORTED = tuple(sorted(BAD_HEAD_WORD, key=len, reverse=True))
_BAD_TAIL_SORTED = tuple(sorted(BAD_TAIL_WORD, key=len, reverse=True))


def _clean(s: str) -> str:
    return s.strip(" \t\r\n·•-—_|:：,，.。、()（）[]【】\"'")


def _trim(s: str) -> str:
    """削掉挂在头尾的虚词 / 动词，直到削不动为止。"""
    changed = True
    while changed and s:
        changed = False
        for w in _BAD_HEAD_SORTED:
            if s.startswith(w) and len(s) > len(w) + 1:
                s = _clean(s[len(w):])
                changed = True
                break
        if changed:
            continue
        for w in _BAD_TAIL_SORTED:
            if s.endswith(w) and len(s) > len(w) + 1:
                s = _clean(s[: -len(w)])
                changed = True
                break
    while s and s[0] in BAD_HEAD_CHAR and len(s) > 2:
        s = s[1:]
    return s


def _only_org_suffix(s: str) -> bool:
    """「集团有限公司」这种只由机构后缀拼成的片段，不是名字。"""
    rest = s
    changed = True
    while rest and changed:
        changed = False
        for suf in ORG_SUFFIX:
            if rest == suf:
                return True
            if rest.endswith(suf):
                rest = rest[: -len(suf)]
                changed = True
                break
    return not rest


def _valid(s: str) -> bool:
    if not (2 <= len(s) <= 20):
        return False
    if s in STOPWORDS:
        return False
    if re.fullmatch(r"[\d\s.\-]+", s):
        return False
    if re.fullmatch(r"[A-Za-z0-9&.'\- ]+", s) and not re.search(r"[A-Z]", s):
        return False
    return not _only_org_suffix(s)


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
        word = _trim(_clean(word))
        if not _valid(word):
            return
        scores[word] += weight
        reasons.setdefault(word, set()).add(why)

    # 1) 显式结构
    for m in QUOTED.finditer(text):
        bump(m.group(1), 5.0, "引号内")
    for m in PROJECT.finditer(text):
        word = m.group(1)
        bump(word, 4.0, "含「项目/工程」")
        # 在**第一个**类别词处切开：「星海计划项目一期建设工程」→「星海计划」。
        # 只切末尾不够 —— 贪婪匹配会把「项目一期建设」整段吃进来。
        cut = min((word.find(s) for s in CATEGORY_SUFFIX if s in word), default=-1)
        if cut >= 2:
            bump(word[:cut], 3.6, "去掉「项目/工程」后")
    for m in ORG.finditer(text):
        bump(m.group(1), 3.5, "机构名")

    # 1b) 英文名：两词以上，或带 Corp / Inc / Ltd 这类后缀
    for m in EN_NAME.finditer(text):
        word = m.group(1).strip()
        parts = word.split()
        tail = parts[-1].lower().strip(".,") if parts else ""
        if len(parts) >= 2:
            bump(word, 3.5 if tail in EN_ORG_SUFFIX else 2.8,
                 "英文机构名" if tail in EN_ORG_SUFFIX else "英文词组")

    # 2) 位置：开头几行里的中文片段。
    #    超过 12 字的连续中文不截断取片段 —— 截断只会产出半个词。
    for line in head.splitlines()[:4]:
        for m in CJK_RUN.finditer(line):
            frag = m.group(0)
            if not (2 <= len(frag) <= 12) or frag in STOPWORDS:
                continue
            bump(frag, 2.0, "出现在标题区")

    # 3) 频次：全文 2–8 字中文片段（同样要削头尾，否则「年上线」这种会混进来）
    for m in CJK_RUN.finditer(text):
        frag = m.group(0)
        if not (2 <= len(frag) <= 8):
            continue
        frag = _trim(frag)
        if frag in STOPWORDS or not _valid(frag):
            continue
        scores[frag] += 0.15

    # 4) 出现次数加成
    for word in list(scores):
        n = text.count(word)
        if n > 1:
            scores[word] += min(n, 6) * 0.6
            reasons.setdefault(word, set()).add(f"出现 {n} 次")

    # 5) 「X项目…」收敛成「X」：把分并到名字本体上，丢掉带类别词的版本。
    #    公司名不这么做 —— 「深圳星海科技有限公司」的全称才是对的。
    for word in list(scores):
        # 公司名里也可能含「工程」（广东启明照明工程有限公司），不能切
        if word.endswith(ORG_SUFFIX):
            continue
        cut = min((word.find(s) for s in CATEGORY_SUFFIX if s in word), default=-1)
        if cut < 2:
            continue
        stem = word[:cut]
        if _valid(stem):
            scores[stem] = max(scores.get(stem, 0.0), scores[word])
            reasons.setdefault(stem, set()).update(reasons.get(word, ()))
            del scores[word]
        elif stem in STOPWORDS:
            del scores[word]          # 「建设工程施工合同」这类文档名，不是名字

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
