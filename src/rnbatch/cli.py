"""命令行入口。"""
from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

from . import keywords as kwmod
from .engine import (MAX_NAME_BYTES, ConflictError, apply_plan,
                     check_conflicts, collect, find_symlinks, list_undo_logs,
                     plan, relative_label, sanitize, stale_temp_files, undo)
from .extract import rich_texts, type_of


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

    # 传目录时按「正文最可能有料」挑，而不是按文件名排头几个 ——
    # 否则会先挑到 café.txt 这种只有三个字的文件，把项目说明挤掉。
    # rich_texts 会跳过解析失败 / 没有文字层的，避免名额被废文件占掉。
    picked = rich_texts(files, want=args.max_files, limit=args.limit)
    if not picked:
        print("这几个文件都没抽出可用正文（可能都是扫描件或已损坏）。", file=sys.stderr)
        return 2

    pool: list[dict] = []
    for p, body in picked:
        cands = kwmod.suggest(body)
        print("=" * 70)
        print(f"文件: {p.name}")
        print(f"类型: {type_of(p.suffix)}   大小: {p.stat().st_size / 1024:.1f} KB")
        print("-" * 70)
        print(body)
        print()
        for c in cands:
            c = dict(c)
            c["from"] = p.name
            pool.append(c)

    best: dict[str, dict] = {}
    for c in pool:
        cur = best.get(c["kw"])
        if cur is None or c["score"] > cur["score"]:
            best[c["kw"]] = c
    merged = sorted(best.values(), key=lambda c: -c["score"])[:10]

    if merged:
        print("=" * 70)
        print("候选关键词（挑一个给 --kw 用）")
        print("-" * 70)
        for i, c in enumerate(merged, 1):
            print(f"  {i}. {dpad(c['kw'], 24)} {c['score']:>5.1f}  {c['why']}")
        print()
        print(f"下一步： rnbatch preview <目录> --kw \"{merged[0]['kw']}\"")
    else:
        print("没有提出候选关键词，请手动指定 --kw。", file=sys.stderr)
    return 0


def _resolve_files(args):
    """收集文件，并对「路径根本不存在」给出明确错误，而不是含糊的"没有匹配到文件"。"""
    missing = [p for p in args.paths if not Path(p).expanduser().exists()]
    if len(missing) == len(args.paths):
        raise FileNotFoundError(f"路径不存在：{missing[0]}")
    for m in missing:
        print(f"⚠️  跳过（不存在）: {m}", file=sys.stderr)
    return collect(args.paths, args.recursive, args.only)


def cmd_preview(args) -> int:
    try:
        files = _resolve_files(args)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not files:
        print("目录里没有可改名的文件（隐藏文件已跳过）。")
        return 2
    warns: list = []
    pairs = plan(files, args.kw, args.template, args.start, args.pad, args.sort,
                 warnings=warns)
    changed = sum(1 for a, b in pairs if a.name != b.name)
    conflicts = check_conflicts(pairs)
    cset = {a for a, _ in conflicts}
    links = find_symlinks(args.paths, args.recursive)

    # 递归时用相对路径，否则两个 a.txt 在表里长得一模一样
    def label(p):
        return relative_label(p, args.paths) if args.recursive else p.name

    w = min(46, max((dwidth(label(a)) for a, _ in pairs), default=10))
    print(f"关键词: 「{args.kw}」   模板: {args.template}   "
          f"共 {len(pairs)} 个文件，将改动 {changed} 个\n")
    if links:
        print(f"ⓘ 跳过 {len(links)} 个符号链接（改名会让链接断掉）："
              f"{links[0].name}" + ("…" if len(links) > 1 else ""))
        print()
    print(f"  {dpad('原文件名', w)}  →  新文件名")
    print("-" * (w + 6 + 46))
    for a, b in pairs:
        if a.name == b.name:
            flag = " "
        elif a in cset:
            flag = "!"
        else:
            flag = "*"
        print(f"{flag} {dpad(trunc(label(a), w), w)}  →  {label(b)}")
    print()

    if warns:
        print(f"⚠️  {len(warns)} 个名字超过 {MAX_NAME_BYTES} 字节上限，已自动截断"
              f"（首个：{warns[0]['file']}）")
        print()

    if conflicts:
        print(f"⚠️  {len(conflicts)} 个目标名已被目录里的其他文件占用，apply 会被拒绝：")
        for a, b in conflicts[:5]:
            print(f"     {a.name}  →  {b.name}  （已存在）")
        if len(conflicts) > 5:
            print(f"     …另有 {len(conflicts) - 5} 个")
        print("   处理办法：换模板、加 --start，或先把占位文件挪走。")
        return 0

    if changed:
        print("确认无误后，把 preview 换成 apply 执行（会写撤销日志）。")
    return 0


def cmd_apply(args) -> int:
    try:
        files = _resolve_files(args)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not files:
        print("目录里没有可改名的文件（隐藏文件已跳过）。")
        return 2

    stale = set()
    for f in files:
        stale.update(stale_temp_files(f.parent))
    if stale:
        print(f"⚠️  目录里有 {len(stale)} 个上次改名残留的临时文件（.rnbatch.tmp.*），"
              f"已跳过。确认后可手动还原或删除。", file=sys.stderr)

    pairs = plan(files, args.kw, args.template, args.start, args.pad, args.sort)
    try:
        log = apply_plan(pairs, args.kw, args.template)
    except ConflictError as e:
        print(f"❌ {e}", file=sys.stderr)
        print("   没有改动任何文件。先跑 preview 看哪几行标了 !。", file=sys.stderr)
        return 1
    except Exception as e:
        msg = str(e)
        if "too long" in msg.lower() or "ENAMETOOLONG" in msg:
            print("❌ 文件名过长：源文件名本身已经贴着系统上限，改名时连临时名都放不下。",
                  file=sys.stderr)
            print("   先把过长的源文件改短，再重跑。没有文件被改动。", file=sys.stderr)
        elif "EACCES" in msg or "Permission denied" in msg:
            print("❌ 没有写权限：这个目录或文件不允许改名。", file=sys.stderr)
            print("   检查目录权限，或换到有权限的位置。", file=sys.stderr)
        else:
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
    except (FileNotFoundError, ValueError) as e:
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


def cmd_serve(args) -> int:
    from .server import serve
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run_stdio
    return run_stdio()


# ------------------------------------------------------------------ 参数
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rnbatch",
        description="批量重命名：从一份文件里取关键词，让一堆文件名字一致。",
    )
    ap.add_argument("-V", "--version", action="version", version="rnbatch 0.2.1")
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

    p_sv = sub.add_parser("serve", help="启动本地 Web 应用（文件不出本机）")
    p_sv.add_argument("--port", type=int, default=8765)
    p_sv.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_sv.set_defaults(func=cmd_serve)

    p_mc = sub.add_parser("mcp", help="以 MCP server 运行（stdio），供任意 agent 调用")
    p_mc.set_defaults(func=cmd_mcp)

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
