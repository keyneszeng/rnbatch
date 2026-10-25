"""MCP server（stdio 传输，零第三方依赖）。

为什么手写而不装官方 SDK：官方 `mcp` 包会连带拉起 httpx / starlette / uvicorn 一整套，
对"装个 CLI 就能被任何 agent 调用"这个目标来说是负担。MCP over stdio 本身就是
换行分隔的 JSON-RPC 2.0，需要的只有 initialize / tools/list / tools/call 三个方法。

任何支持 MCP 的客户端（WorkBuddy、Claude Code、Cursor、Cline、Zed…）都能直接用：

    {"mcpServers": {"rnbatch": {"command": "rnbatch", "args": ["mcp"]}}}
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from . import __version__
from . import keywords as kwmod
from .engine import (ConflictError, apply_plan, check_conflicts, collect,
                     list_undo_logs, load_log, plan, sanitize, undo)
from .extract import extract_text, rich_texts, type_of

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
LATEST = PROTOCOL_VERSIONS[0]


# ------------------------------------------------------------------ 工具实现
def _arg_path(args: dict) -> Path:
    """取 path 参数。缺了给一句人话，而不是 KeyError: 'path'。"""
    raw = args.get("path")
    if not raw or not isinstance(raw, str):
        raise ValueError("缺少 path 参数（要处理的文件或文件夹路径）")
    p = Path(raw).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{raw}")
    return p


def _int(value, default: int, lo: int = 0, hi: int = 100_000) -> int:
    """agent 传 "abc" / -3 这类值很常见，不该整次调用都失败。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _collect(path: str, recursive: bool = False, only=None):
    p = _arg_path({"path": path})
    return p, collect([p], recursive, only)


def tool_list_files(args: dict) -> dict:
    p = _arg_path(args)
    files = collect([p], bool(args.get("recursive")), args.get("only"))
    return {
        "directory": str(p),
        "count": len(files),
        "files": [{
            "name": f.name,
            "type": type_of(f.suffix),
            "size": f.stat().st_size,
        } for f in files],
    }


def tool_extract_text(args: dict) -> dict:
    p = _arg_path(args)
    limit = _int(args.get("limit"), 3000, lo=100, hi=200_000)
    names = args.get("files")
    if names and isinstance(names, list):
        # 只允许抽 path 所在目录内的文件，防止 ../.. 读到系统文件
        base = p if p.is_dir() else p.parent
        pairs = []
        for n in names:
            t = (base / str(n)).resolve()
            try:
                t.relative_to(base.resolve())
            except ValueError:
                continue                     # 越界，直接丢掉
            if t.is_file():
                pairs.append((t, extract_text(t, limit)))
    else:
        files = collect([p], False, None)
        # 解析失败 / 无文字层的文件不该占名额，多试几个兜底
        pairs = rich_texts(files, want=_int(args.get("max_files"), 2, lo=1, hi=20),
                           limit=limit)

    sources = []
    pool = []
    for t, text in pairs:
        cands = kwmod.suggest(text)
        sources.append({"name": t.name, "type": type_of(t.suffix),
                        "text": text, "candidates": cands})
        for c in cands:
            pool.append(dict(c, source=t.name))

    best: dict[str, dict] = {}
    for c in pool:
        if c["kw"] not in best or c["score"] > best[c["kw"]]["score"]:
            best[c["kw"]] = c
    return {
        "sources": sources,
        "candidates": sorted(best.values(), key=lambda c: -c["score"])[:10],
        "next_step": "挑一个 candidate 作为 keyword，再调 preview_rename 给用户看。",
    }


def _plan(args: dict):
    p = _arg_path(args)
    files = collect([p], bool(args.get("recursive")), args.get("only"))
    kw = sanitize(args.get("keyword") or args.get("kw") or "")
    if not kw:
        raise ValueError("keyword 不能为空")
    warns: list = []
    pairs = plan(files, kw,
                 template=args.get("template") or "{kw}-{n}",
                 start=_int(args.get("start"), 1, lo=0),
                 pad=_int(args.get("pad"), 0, lo=0, hi=12),
                 sort=args.get("sort") or "name",
                 warnings=warns)
    return pairs, kw, warns


def tool_preview_rename(args: dict) -> dict:
    pairs, kw, warns = _plan(args)
    conflicts = check_conflicts(pairs)
    cset = {str(a) for a, _ in conflicts}
    rows = [{"from": a.name, "to": b.name, "changed": a.name != b.name,
             "conflict": str(a) in cset,
             "from_path": str(a), "to_path": str(b)} for a, b in pairs]
    out = {
        "keyword": kw,
        "template": args.get("template") or "{kw}-{n}",
        "count": len(rows),
        "changed": sum(1 for r in rows if r["changed"]),
        "conflicts": len(conflicts),
        "warnings": warns,
        "rows": rows,
    }
    if conflicts:
        out["warning"] = (f"{len(conflicts)} 个目标名已被占用，apply_rename 会被拒绝："
                          + "、".join(b.name for _a, b in conflicts[:5]))
    else:
        out["next_step"] = "把 rows 给用户确认，同意后再调 apply_rename。"
    return out


def tool_apply_rename(args: dict) -> dict:
    pairs, kw, _warns = _plan(args)
    try:
        log = apply_plan(pairs, kw, args.get("template") or "{kw}-{n}")
    except ConflictError as e:
        return {"renamed": 0, "error": str(e),
                "hint": "先调 preview_rename 看哪些行标了 conflict，换模板或加 start 再试。"}
    if log is None:
        return {"renamed": 0, "message": "文件名已符合规则，无需改动"}
    n = sum(1 for a, b in pairs if a.name != b.name)
    return {
        "renamed": n,
        "undo_log": log.name,
        "message": f"已改名 {n} 个文件。如需还原，调 undo_rename(log='{log.name}')。",
    }


def tool_undo_rename(args: dict) -> dict:
    try:
        ok, skipped = undo(args.get("log"))
    except (FileNotFoundError, ValueError) as e:
        return {"restored": 0, "error": str(e)}
    return {"restored": ok, "skipped": skipped}


def tool_recent_renames(_args: dict) -> dict:
    out = []
    for p in list_undo_logs()[-20:]:
        d = load_log(p)
        if d is None:
            continue
        out.append({"log": p.name, "time": d.get("time"),
                    "keyword": d.get("keyword"), "count": len(d.get("records") or [])})
    return {"recent": list(reversed(out))}


# ------------------------------------------------------------------ 工具表
TOOLS = [
    {
        "name": "list_files",
        "description": "列出文件夹里的文件（名称、类型、大小）。先摸清目标目录用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件夹或文件的绝对路径"},
                "recursive": {"type": "boolean", "description": "是否递归子目录，默认 false"},
                "only": {"type": "array", "items": {"type": "string"},
                         "description": "只保留这些扩展名，如 ['pdf','jpg']"},
            },
            "required": ["path"],
        },
        "fn": tool_list_files,
    },
    {
        "name": "extract_text",
        "description": ("从文档或图片里抽取正文，并给出候选关键词。支持 docx/pptx/xlsx/pdf/"
                        "doc/rtf/txt/md/csv，图片走 OCR。**改名前先用这个找关键词。**"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "基准文件或文件夹路径"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "当 path 是文件夹时，指定抽哪几个文件"},
                "max_files": {"type": "integer", "description": "不指定 files 时最多抽几个，默认 2"},
                "limit": {"type": "integer", "description": "每个文件最多返回多少字，默认 3000"},
            },
            "required": ["path"],
        },
        "fn": tool_extract_text,
    },
    {
        "name": "preview_rename",
        "description": ("预览批量改名结果，**不改动任何文件**。返回每一行 原文件名 → 新文件名。"
                        "必须先给用户看过再执行。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件夹"},
                "keyword": {"type": "string", "description": "命名词：项目名 / 人名 / 公司简称"},
                "template": {"type": "string",
                             "description": ("命名模板，占位符 {kw} {n} {n:03} {orig} {date} "
                                             "{type} {ext}。默认 {kw}-{n}")},
                "sort": {"type": "string", "enum": ["name", "date", "mtime_desc", "size"],
                         "description": "序号排序依据，默认 name"},
                "start": {"type": "integer", "description": "序号起始，默认 1"},
                "pad": {"type": "integer", "description": "补零位数，默认自动"},
                "recursive": {"type": "boolean"},
                "only": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["path", "keyword"],
        },
        "fn": tool_preview_rename,
    },
    {
        "name": "apply_rename",
        "description": ("执行改名。**只在用户确认过 preview 结果后调用。**"
                        "每次执行都会写撤销日志，返回 undo_log 句柄。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "keyword": {"type": "string"},
                "template": {"type": "string"},
                "sort": {"type": "string", "enum": ["name", "date", "mtime_desc", "size"]},
                "start": {"type": "integer"},
                "pad": {"type": "integer"},
                "recursive": {"type": "boolean"},
                "only": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["path", "keyword"],
        },
        "fn": tool_apply_rename,
    },
    {
        "name": "undo_rename",
        "description": "撤销改名。不传 log 则撤销最近一次。",
        "inputSchema": {
            "type": "object",
            "properties": {"log": {"type": "string", "description": "撤销日志文件名，可省略"}},
        },
        "fn": tool_undo_rename,
    },
    {
        "name": "recent_renames",
        "description": "查看最近的改名记录（时间、关键词、文件数）。",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_recent_renames,
    },
]

BY_NAME = {t["name"]: t for t in TOOLS}


# ------------------------------------------------------------------ JSON-RPC
def _result(mid, payload):
    return {"jsonrpc": "2.0", "id": mid, "result": payload}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg) -> dict | None:
    # 客户端可能发来数组（JSON-RPC 批量）、字符串、null —— 一律不能把进程搞崩
    if not isinstance(msg, dict):
        return _error(None, -32600, "请求必须是 JSON 对象")

    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}

    # 通知（无 id）不需要回应
    if mid is None and method:
        return None

    if not method:
        return _error(mid, -32600, "缺少 method 字段")

    if method == "initialize":
        asked = params.get("protocolVersion")
        ver = asked if asked in PROTOCOL_VERSIONS else LATEST
        return _result(mid, {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "rnbatch", "version": __version__},
            "instructions": (
                "批量重命名工具。标准流程：extract_text 读正文找关键词 → "
                "preview_rename 给用户看 → 用户确认后 apply_rename → 需要时 undo_rename。"
                "不要在用户确认前直接 apply。"
            ),
        })

    if method == "ping":
        return _result(mid, {})

    if method == "tools/list":
        return _result(mid, {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["inputSchema"]} for t in TOOLS
        ]})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = BY_NAME.get(name)
        if tool is None:
            return _error(mid, -32602, f"未知工具：{name}")
        try:
            data = tool["fn"](args)
            return _result(mid, {
                "content": [{"type": "text",
                             "text": json.dumps(data, ensure_ascii=False, indent=2)}],
                "isError": False,
            })
        except Exception as e:
            # 参数类错误的消息本来就是人话，别再挂类型名让 agent 犯迷糊
            if isinstance(e, (ValueError, FileNotFoundError, PermissionError)):
                msg = str(e)
            else:
                msg = f"{type(e).__name__}: {e}"
            return _result(mid, {
                "content": [{"type": "text", "text": msg}],
                "isError": True,
            })

    return _error(mid, -32601, f"未实现的方法：{method}")


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_stdio() -> int:
    """逐行读 stdin，逐行写 stdout。日志一律走 stderr，绝不污染协议流。"""
    # 终端不是 UTF-8 时，中文 JSON 会直接抛 UnicodeEncodeError 把服务搞死
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _emit(_error(None, -32700, "JSON 解析失败"))
            continue

        try:
            if isinstance(msg, list):                 # JSON-RPC 批量
                if not msg:
                    _emit(_error(None, -32600, "空批次"))
                    continue
                replies = []
                for m in msg:
                    try:
                        r = handle(m)
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                        r = _error(m.get("id") if isinstance(m, dict) else None,
                                   -32603, "内部错误")
                    if r is not None:
                        replies.append(r)
                if replies:
                    _emit(replies)
                continue

            reply = handle(msg)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            reply = _error(msg.get("id") if isinstance(msg, dict) else None,
                           -32603, "内部错误")

        if reply is not None:
            _emit(reply)
    return 0
