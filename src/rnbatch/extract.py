"""正文抽取：docx / doc / pptx / xlsx / pdf / 纯文本 / 图片 OCR。

设计取舍：
- 文档解析用可选依赖，缺哪个降级哪个，不让整个工具挂掉。
- 图片 OCR 走 macOS 原生 Vision（pyobjc 桥），离线、支持中英混排；非 macOS 平台自动跳过。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp",
             ".tif", ".tiff", ".bmp", ".raw", ".cr2", ".nef", ".arw", ".dng"}
DOC_EXT = {".doc", ".docx", ".pages", ".rtf", ".txt", ".md"}
SHEET_EXT = {".xls", ".xlsx", ".csv", ".numbers"}
SLIDE_EXT = {".ppt", ".pptx", ".key"}
PDF_EXT = {".pdf"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
ARCHIVE_EXT = {".zip", ".rar", ".7z"}


def type_of(ext: str) -> str:
    """把扩展名映射成中文类型标签，供 {type} 占位符使用。"""
    ext = ext.lower()
    for label, group in (
        ("照片", PHOTO_EXT), ("视频", VIDEO_EXT), ("PDF", PDF_EXT),
        ("文档", DOC_EXT), ("表格", SHEET_EXT), ("演示", SLIDE_EXT),
        ("压缩包", ARCHIVE_EXT),
    ):
        if ext in group:
            return label
    return "其他"


# ------------------------------------------------------------------ 各格式解析
def _from_docx(p: Path) -> str:
    from docx import Document
    d = Document(str(p))
    parts = [para.text for para in d.paragraphs if para.text.strip()]
    for tbl in d.tables:
        for row in tbl.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _from_pptx(p: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(p))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        parts.append(f"[第{i}页]")
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    parts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _from_xlsx(p: Path, max_rows: int = 80) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(p), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[工作表 {ws.title}]")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                parts.append("…(截断)")
                break
            cells = [str(c) for c in row if c not in (None, "")]
            if cells:
                parts.append(" | ".join(cells))
    wb.close()
    return "\n".join(parts)


def _from_pdf(p: Path, max_pages: int = 4) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    parts = []
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            parts.append(f"…(仅取前{max_pages}页)")
            break
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n".join(parts)


def _from_plain(p: Path) -> str:
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return p.read_bytes().decode("utf-8", "replace")


def _from_textutil(p: Path) -> str:
    """老格式（.doc/.rtf/.odt/.html）交给 macOS 自带 textutil。"""
    if sys.platform != "darwin":
        return ""
    try:
        r = subprocess.run(["/usr/bin/textutil", "-convert", "txt", "-stdout", str(p)],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def ocr_image(p: Path) -> str:
    """macOS 原生 Vision OCR。离线、不联网、支持 zh-Hans + en-US。"""
    if sys.platform != "darwin":
        return ""
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
    except Exception:
        return ""
    try:
        url = NSURL.fileURLWithPath_(str(p))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            return ""
        img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if img is None:
            return ""
        collected: list[str] = []

        def handler(request, error):
            for obs in (request.results() or []):
                cands = obs.topCandidates_(1)
                if cands and len(cands):
                    collected.append(cands[0].string())

        req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
        req.setRecognitionLevel_(0)          # 0 = accurate
        req.setUsesLanguageCorrection_(True)
        try:
            req.setRecognitionLanguages_(["zh-Hans", "en-US"])
        except Exception:
            pass
        h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        h.performRequests_error_([req], None)
        return "\n".join(collected)
    except Exception as e:
        return f"（OCR 失败：{type(e).__name__}: {e}）"


def _from_image(p: Path) -> str:
    ocr = ocr_image(p).strip()
    meta = []
    try:
        from PIL import Image, ExifTags
        im = Image.open(str(p))
        meta.append(f"尺寸 {im.width}x{im.height}")
        try:
            for tag, val in im.getexif().items():
                name = ExifTags.TAGS.get(tag)
                if name in ("DateTimeOriginal", "DateTime", "Make", "Model"):
                    meta.append(f"{name}: {val}")
        except Exception:
            pass
    except Exception:
        pass
    tail = "，".join(meta)
    if not ocr or ocr.startswith("（OCR"):
        return f"（图片无可识别文字）{tail}"
    return f"[OCR 文字]\n{ocr}\n[图片信息] {tail}"


def extract_text(p: Path, limit: int = 4000) -> str:
    """抽取单个文件的正文，供人或 AI 判断关键词。"""
    p = Path(p)
    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            t = _from_docx(p)
        elif ext == ".pptx":
            t = _from_pptx(p)
        elif ext in (".xlsx", ".xlsm"):
            t = _from_xlsx(p)
        elif ext == ".pdf":
            t = _from_pdf(p)
        elif ext in PHOTO_EXT:
            t = _from_image(p)
        elif ext in (".doc", ".rtf", ".rtfd", ".odt", ".html", ".htm", ".webarchive"):
            t = _from_textutil(p) or _from_plain(p)
        elif ext in (".txt", ".md", ".csv", ".json", ".xml"):
            t = _from_plain(p)
        else:
            t = _from_plain(p) if p.stat().st_size < 3_000_000 else "（二进制文件，未解析）"
    except Exception as e:
        t = f"（解析失败：{type(e).__name__}: {e}）"
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit]
