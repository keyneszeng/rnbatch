# rnbatch

**从一份文件里读出关键词，让一堆文件名字变一致。**

> 场景：一个项目文件夹里混着照片、PDF、Word、PPT、Excel，名字五花八门 ——
> `IMG_0421.JPG`、`微信图片_20261018.jpg`、`方案汇报 v3 final.pptx`。
> 而项目名「星海中心」只写在其中一份会议纪要里。
> 你要的是：所有文件都以「星海中心」为名、顺序编号、互不冲突、随时能反悔。

```bash
$ rnbatch extract 会议纪要.docx
会议主题：星海中心项目 一期交付启动会
甲方：恒远置业集团；乙方：筑元建筑设计有限公司

$ rnbatch preview ./项目文件夹 --kw "星海中心" --template "{kw}-{n:02}-{type}"
* 会议纪要-20261020.docx     →  星海中心-01-文档.docx
* 方案汇报 v3 final.pptx     →  星海中心-02-演示.pptx
* 材料清单(1).xlsx           →  星海中心-03-表格.xlsx
* IMG_0421.JPG               →  星海中心-04-照片.JPG
* 微信图片_20261018.jpg      →  星海中心-05-照片.jpg
* 扫描件_001.pdf             →  星海中心-06-PDF.pdf

$ rnbatch apply ./项目文件夹 --kw "星海中心" --template "{kw}-{n:02}-{type}"
✅ 已改名 6 个文件
   回滚命令: rnbatch undo 20261023-210533-a1b2c3.json
```

---

## 为什么不用现成工具

改名工具和"读正文"这两件事，市面上的产品是分开的。

| 方案 | 能不能读正文取词 | 结论 |
|---|---|---|
| Finder 右键重命名 | ❌ 只能替换 / 加前缀 / 编号 | 关键词得你自己想好 |
| A Better Finder Rename 12 | ❌ 只吃 EXIF / ID3 / Spotlight **元数据** | 项目名在正文里，取不到 |
| 纳寻 namesense.app | ✅ 有 AI 按内容命名 | 要求 macOS 13+；免费版每批 3 个文件；AI 需自备 API Key |
| 通用 CLI（`f2` / `rename` / `exiftool`） | ❌ 只做字符串变换 | 关键词仍需人肉提供 |

**真正的瓶颈不是"改名"，是"哪串词才是项目名"。** 正则猜不出来，只能靠理解。
所以 rnbatch 的分工是：

- **关键词的判断交给人或 AI** —— `extract` 把正文摊开，你（或你旁边的 LLM）一眼认出关键词。
- **批量改名交给机器** —— 模板、补零、冲突消解、两阶段改名、撤销日志，机械活一件不漏。

这也是它跟 GUI 改名器最本质的区别：**它不假设你知道关键词，它帮你去文件里找。**

---

## 安装

```bash
git clone https://github.com/keyneszeng/rnbatch.git
cd rnbatch
pip install -e ".[all]"
```

最小安装（不解析文档正文，只改纯文件名）：

```bash
pip install -e .
```

依赖按需降级：缺 `python-docx` 就只读不了 docx，不影响其他格式。

| extra | 作用 |
|---|---|
| `docs` | docx / pptx / xlsx / pdf 正文抽取 |
| `ocr` | 图片 OCR（**仅 macOS**，走系统 Vision 框架，离线、不联网） |

> 图片 OCR 用的是 macOS 自带 Vision（pyobjc 桥接），支持中英混排。白板照、名片、
> 合同扫描件都能读出字。非 macOS 平台自动跳过，不影响其他功能。

---

## 用法

### 1. 抽正文

```bash
rnbatch extract 会议纪要.docx              # 单个文件
rnbatch extract ./文件夹 --max-files 4     # 一次看几个候选
rnbatch extract 白板照片.png --limit 800   # 图片走 OCR
```

### 2. 预览（必做）

```bash
rnbatch preview ./文件夹 --kw "星海中心" --template "{kw}-{n:02}"
```

### 3. 执行

```bash
rnbatch apply ./文件夹 --kw "星海中心" --template "{kw}-{n:02}"
```

### 4. 后悔

```bash
rnbatch undo              # 撤销最近一次
rnbatch undo 20261023-….json   # 撤销指定批次
rnbatch logs              # 看看历史记录
```

---

## 模板

| 占位符 | 含义 | 例 |
|---|---|---|
| `{kw}` | 关键词 | 星海中心 |
| `{n}` | 序号，宽度自动 | 01 |
| `{n:03}` | 强制补零 3 位 | 001 |
| `{orig}` | 原文件名（不含扩展名） | 方案汇报 v3 final |
| `{date}` | 文件修改日期 | 20261018 |
| `{type}` | 中文类型标签 | 照片 / PDF / 文档 / 表格 / 演示 |
| `{ext}` | 扩展名（不写也会自动保留） | jpg |

```bash
--template "{kw}-{n:02}"          # 星海中心-01.jpg
--template "{kw}-{n:02}-{type}"   # 星海中心-01-照片.jpg   ← 混合格式最好用
--template "{kw}-{orig}"          # 星海中心-方案汇报 v3 final.pptx
--template "{kw}-{date}-{n:02}"   # 星海中心-20261018-01.jpg
```

其他参数：

| 参数 | 说明 |
|---|---|
| `--sort name\|date\|mtime_desc\|size` | 序号排序依据，默认 `name`；照片按时间排用 `date` |
| `--start 5` | 序号从 5 起（分批接续） |
| `--pad 3` | 固定补零位数 |
| `--recursive` | 递归子目录 |
| `--only pdf,jpg` | 只处理指定扩展名 |

---

## 三条设计原则

**1. 永不静默改名。** `preview` 和 `apply` 是两个命令，中间隔着人的一次确认。

**2. 永不不可逆。** 每次 `apply` 都往 `~/.rnbatch/undo/` 写一份 JSON 日志，
`undo` 一条命令全还原。日志是可读的，出问题能手改。

**3. 永不互相覆盖。** 改名走两阶段（原名 → 临时名 → 终名），
`A→B, B→A` 这种互换不会丢文件；目标名已被占用时**直接报错并回滚**，不猜你的意图。

---

## 架构

```
src/rnbatch/
├── extract.py   正文抽取（docx/pptx/xlsx/pdf/纯文本/图片OCR）
├── engine.py    模板渲染 · 冲突消解 · 两阶段改名 · 撤销
└── cli.py       命令行
```

引擎是纯函数式的：`plan()` 只算不碰文件，`apply_plan()` 才落盘。
所以测试可以在临时目录里跑完整流程，不需要 mock 文件系统。

```bash
python -m unittest discover -s tests
```

---

## 集成

- `integrations/workbuddy/SKILL.md` —— 给 [WorkBuddy](https://www.workbuddy.cn) 用的技能定义：
  让 AI 读正文、判断关键词、复述确认，再调 CLI 执行。
- `integrations/macos/` —— 拖放式 App 的 AppleScript 源码（拖文件上去 → 输关键词 → 选模板 → 终端确认）。

---

## 状态

`v0.1.0` —— 能用，跑在真实项目文件夹上验证过（改名 + 回滚）。**还不是产品。**

已知限制：

- 扫描版 PDF（无文字层）读不出正文 —— 需要 OCR 层，目前只对图片做 OCR。
- 关键词判断靠人或 AI，没有内置模型；这是设计选择，不是缺陷（见 [docs/COMPETITIVE.md](docs/COMPETITIVE.md)）。
- 只支持本地文件系统，不做跨卷/网络盘的原子改名。
- 无 GUI（macOS 拖放 App 是薄壳）。

后续方向见 [docs/ROADMAP.md](docs/ROADMAP.md)，商业化思考见 [docs/COMPETITIVE.md](docs/COMPETITIVE.md)。

---

## License

MIT —— 见 [LICENSE](LICENSE)。
如需改为 AGPL / 双许可 / 专有，是 LICENSE 一行的事，先谈。
