"""命令行入口。"""
from __future__ import annotations

import argparse
import sys
import unicodedata

from .engine import (apply_plan, collect, list_undo_logs, plan, sanitize,
                     undo)
from .extract import extract_text, type_of


# ------------------------------------------------------------------ 显示宽度
def dwidth(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def dpad(s: str, width: int) -> str:
    return s + " " * max(0, width - dwidth(s))


def trunc(s: str, width: int) -> str:
    if dwidth(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


# ------------------------------------------------------------------ 子命令
def cmd_extract(args) -> int:
    files = collect(args.paths, recursive=False, only=args.only)
    if not files:
        print("没有可解析的文件", file=sys.stderr)
        return 2
    for p in files[: args.max_files]:
        body = extract_text(p, args.limit)
        print("=" * 70)
        print(f"文件: {p.name}")
        print(f"类型: {type_of(p.suffix)}   大小: {p.stat().st_size / 1024:.1f} KB")
        print("-" * 70)
        print(body if body else "（无可用文本）")
        print()
    return 0


def cmd_preview(args) -> int:
    files = collect(args.paths, args.recursive, args.only)
    if not files:
        print("没有匹配到文件。")
        return 2
    pairs = plan(files, args.kw, args.template, args.start, args.pad, args.sort)
    changed = sum(1 for a, b in pairs if a.name != b.name)
    w = min(46, max((dwidth(a.name) for a, _ in pairs), default=10))
    print(f"关键词: 「{args.kw}」   模板: {args.template}   "
          f"共 {len(pairs)} 个文件，将改动 {changed} 个\n")
    print(f"  {dpad('原文件名', w)}  →  新文件名")
    print("-" * (w + 6 + 46))
    for a, b in pairs:
        flag = " " if a.name == b.name else "*"
        print(f"{flag} {dpad(trunc(a.name, w), w)}  →  {b.name}")
    print()
    if changed:
        print("确认无误后，把 preview 换成 apply 执行（会写撤销日志）。")
    return 0


def cmd_apply(args) -> int:
    files = collect(args.paths, args.recursive, args.only)
    if not files:
        print("没有匹配到文件。")
        return 2
    pairs = plan(files, args.kw, args.template, args.start, args.pad, args.sort)
    try:
        log = apply_plan(pairs, args.kw, args.template)
    except Exception as e:
        print(f"❌ 改名中断：{e}", file=sys.stderr)
        print("已尽力回滚未完成的改动。", file=sys.stderr)
        return 1
    if log is None:
        print("所有文件名已符合规则，无需改动。")
        return 0
    n = sum(1 for a, b in pairs if a.name != b.name)
    print(f"✅ 已改名 {n} 个文件")
    print(f"   撤销日志: {log}")
    print(f"   回滚命令: rnbatch undo {log.name}")
    return 0


def cmd_undo(args) -> int:
    try:
        ok, skipped = undo(args.log)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    msg = f"↩️  已还原 {ok} 个文件"
    if skipped:
        msg += f"，跳过 {skipped} 个（原文件名已被占用或文件已不存在）"
    print(msg)
    return 0


def cmd_logs(args) -> int:
    logs = list_undo_logs()
    if not logs:
        print("暂无记录。")
        return 0
    for p in logs:
        print(f"{p.name}\t{p.stat().st_size} B")
    return 0


# ------------------------------------------------------------------ 参数
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rnbatch",
        description="批量重命名：从一份文件里取关键词，让一堆文件名字一致。",
    )
    ap.add_argument("-V", "--version", action="version", version="rnbatch 0.1.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("paths", nargs="+", help="文件夹或文件")
        p.add_argument("--kw", required=True, help="命名词（项目名/人名/公司简称）")
        p.add_argument("--template", default="{kw}-{n}", help="命名模板，默认 {kw}-{n}")
        p.add_argument("--start", type=int, default=1, help="序号起始，默认 1")
        p.add_argument("--pad", type=int, default=0, help="序号补零位数，默认自动")
        p.add_argument("--sort", default="name", choices=["name", "date", "mtime_desc", "size"])
        p.add_argument("--recursive", action="store_true", help="递归子目录")
        p.add_argument("--only", default="", help="只处理这些扩展名，逗号分隔，如 pdf,jpg")

    p_ex = sub.add_parser("extract", help="抽取基准文件正文")
    p_ex.add_argument("paths", nargs="+")
    p_ex.add_argument("--limit", type=int, default=4000)
    p_ex.add_argument("--max-files", type=int, default=1)
    p_ex.add_argument("--only", default="")
    p_ex.set_defaults(func=cmd_extract)

    p_pv = sub.add_parser("preview", help="预览改名结果（不动文件）")
    common(p_pv)
    p_pv.set_defaults(func=cmd_preview)

    p_ap = sub.add_parser("apply", help="执行改名")
    common(p_ap)
    p_ap.set_defaults(func=cmd_apply)

    p_un = sub.add_parser("undo", help="撤销改名")
    p_un.add_argument("log", nargs="?", default=None, help="日志文件名，省略则撤销最近一次")
    p_un.set_defaults(func=cmd_undo)

    p_lg = sub.add_parser("logs", help="列出撤销日志")
    p_lg.set_defaults(func=cmd_logs)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if getattr(args, "only", ""):
        args.only = {e.strip() for e in args.only.split(",") if e.strip()}
    else:
        args.only = None
    if getattr(args, "kw", None) is not None:
        if not sanitize(args.kw):
            print("关键词为空，拒绝执行。", file=sys.stderr)
            return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
