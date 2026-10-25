# 给 agent 用

rnbatch 暴露两种 agent 接口，二选一：

| 接口 | 适合 | 启动方式 |
|---|---|---|
| **MCP server**（stdio） | 支持 MCP 的客户端 | `rnbatch mcp` |
| **HTTP JSON API** | 不支持 MCP、但能发 HTTP 的 agent | `rnbatch serve` |

两者是同一套引擎，行为一致。

---

## 一、MCP

### 客户端配置

任何支持 MCP 的客户端都是加一段 JSON：

**WorkBuddy / Claude Code / Cline / Cursor / Zed**

```json
{
  "mcpServers": {
    "rnbatch": {
      "command": "rnbatch",
      "args": ["mcp"]
    }
  }
}
```

如果 `rnbatch` 不在 PATH 里，把 `command` 换成绝对路径，例如
`"/Users/you/.venv/bin/rnbatch"`。

### 工具清单

| 工具 | 参数 | 返回 |
|---|---|---|
| `list_files` | `path`, `recursive?`, `only?` | 文件清单（名称/类型/大小） |
| `extract_text` | `path`, `files?`, `max_files?`, `limit?` | 正文 + 候选关键词 + 每条的判断依据 |
| `preview_rename` | `path`, `keyword`, `template?`, `sort?`, `start?`, `pad?`, `recursive?`, `only?` | 逐行 `from → to`，**不动文件**；另带 `conflicts` / `warnings` |
| `apply_rename` | 同上 | 改动数 + `undo_log` 句柄；冲突时返回 `error` 而非抛异常 |
| `undo_rename` | `log?` | 还原数 / 跳过数；日志不存在时返回 `{restored:0, error}` |
| `recent_renames` | — | 最近 20 次记录 |

`initialize` 响应里带 `instructions`，明确要求 agent：
**先 `extract_text` 找关键词 → `preview_rename` 给用户看 → 用户确认后才 `apply_rename`**。
安全边界写在协议层，不靠客户端自觉。

### 返回里必须看的两个字段

`preview_rename` 除了 `rows`，还返回两个**你必须在动手前处理掉**的字段：

| 字段 | 含义 | 该怎么处理 |
|---|---|---|
| `conflicts` | 目标名已被目录里别的文件占用的条数（`>0` 时 `rows` 里对应行 `conflict: true`） | **`apply_rename` 会被拒绝，一个文件都不碰。** 换模板、加 `start`，或让用户先挪走占位文件 |
| `warnings` | `[{type:"truncated", file, kept_bytes, limit}]`，名字超过 200 字节被截断 | 转述给用户，建议换短一点的关键词 |

`apply_rename` 遇到冲突**不抛异常**，返回 `{"renamed": 0, "error": "...", "hint": "..."}`。
所以判成功要看 `renamed`，不要只看有没有 `error` 键。

### 容错约定（可以少写一堆防御代码）

agent 传参不规范是常态，这些情况工具层已经兜住，不会让整次调用失败：

| 传入 | 行为 |
|---|---|
| `start: "abc"` / `pad: -3` | 用默认值，不报 `invalid literal for int()` |
| `only: 5`（非数组） | 当作「不过滤」 |
| `files` 里含 `../../etc/hosts` | 越界项直接丢弃，不会读到目录外 |
| 缺 `path` | 返回一句人话「缺少 path 参数…」，不是 `KeyError` |
| `keyword` 全是不合法字符 | 返回「keyword 不能为空」 |
| 客户端发来 JSON-RPC 批量数组 | 支持，逐个处理并批量回包 |

### 一次完整调用

```jsonc
// 1. 找关键词
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
  "name":"extract_text","arguments":{"path":"/Users/me/项目/会议纪要.docx"}}}
// → candidates: [{kw:"星海中心", score:7.5, why:"含「项目/工程」、机构名"}, ...]

// 2. 预览
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
  "name":"preview_rename","arguments":{
    "path":"/Users/me/项目","keyword":"星海中心","template":"{kw}-{n:02}-{type}"}}}
// → changed: 9, rows: [{from:"IMG_0421.JPG", to:"星海中心-04-照片.JPG"}, ...]

// 3. 用户确认后执行
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
  "name":"apply_rename","arguments":{
    "path":"/Users/me/项目","keyword":"星海中心","template":"{kw}-{n:02}-{type}"}}}
// → renamed: 9, undo_log: "20261025-144722-a1b2c3.json"

// 4. 需要时撤销
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
  "name":"undo_rename","arguments":{"log":"20261025-144722-a1b2c3.json"}}}
```

### 实现说明

没用官方 `mcp` SDK —— 它会连带拉起 httpx / starlette / uvicorn 一整套，
对一个"装个 CLI 就能用"的工具来说是负担。MCP over stdio 本身只是
换行分隔的 JSON-RPC 2.0，实现 `initialize` / `tools/list` / `tools/call`
三个方法即可（见 `src/rnbatch/mcp_server.py`，约 330 行）。

支持的协议版本：`2025-06-18`、`2025-03-26`、`2024-11-05`。
客户端请求哪个就回哪个，不认识的回落到最新版。

---

## 二、HTTP JSON API

```bash
rnbatch serve --port 8765 --no-browser
```

启动时会打印带 token 的地址：`http://127.0.0.1:8765/?t=<token>`。
**所有 `/api/*` 请求都要带 `?t=<token>`**，防止本机其他网页跨站调用。

| 端点 | 方法 | 请求体 |
|---|---|---|
| `/api/list` | POST | `{path, recursive?, only?}` |
| `/api/extract` | POST | `{path, files?, limit?}` |
| `/api/preview` | POST | `{path, kw, template?, sort?, start?, pad?, recursive?, only?}` |
| `/api/apply` | POST | 同上 |
| `/api/undo` | POST | `{log?}` |
| `/api/logs` | POST | `{}` |
| `/api/pick-folder` | POST | `{prompt?}` —— 弹 macOS 原生选择器，返回真实路径 |

`/api/preview` 的返回比 MCP 版本多几个便于渲染的字段：

```jsonc
{
  "ok": true,
  "count": 7, "changed": 7,
  "conflicts": 0, "conflict_names": [],          // 目标名被占用的条数 + 前 5 个名字
  "warnings": [],                                 // [{type:"truncated", file, kept_bytes, limit}]
  "skipped_symlinks": 1,                          // 被跳过的符号链接数
  "stale_temp": [],                               // 上次中途被杀留下的 .rnbatch.tmp.*
  "rows": [{
    "from": "IMG_1.jpg", "to": "星海-01.jpg",
    "from_label": "sub/IMG_1.jpg",                // 递归时的相对路径，避免两个 a.txt 分不清
    "to_label": "sub/星海-01.jpg",
    "from_path": "/abs/...", "to_path": "/abs/...",
    "changed": true, "conflict": false, "type": "照片"
  }]
}
```

```bash
curl -s -X POST "http://127.0.0.1:8765/api/preview?t=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"path":"/Users/me/项目","kw":"星海中心","template":"{kw}-{n:02}"}'
```

失败一律返回 `{"ok": false, "error": "..."}`，HTTP 400。
`error` 已经是给人看的中文句子（不再带 `ValueError:` / `KeyError:` 这类类型前缀），
可以直接转述给用户。`/api/apply` 遇到冲突返回 `{"ok": false, "error": "...", "conflicts": [...]}`。

---

## 三、给 agent 的建议

1. **别跳过 `extract_text`。** 关键词选错，一百个文件全错，而且用户会怪你。
2. **把 `preview_rename` 的 `rows` 原样贴给用户看**，别只报数字。
3. **`apply_rename` 之后一定把 `undo_log` 告诉用户**，这是他们的后悔药。
4. 候选关键词有 `why` 字段（"含「项目/工程」"、"机构名"、"出现 3 次"），
   转述给用户能显著提高他们的判断速度。
5. 混合格式文件夹用 `{kw}-{n:02}-{type}`，纯照片用 `{kw}-{n:02}`。
