"""生成 rnbatch（正名）的品牌图形：图标 + 横版组合。

设计概念：一枚朱红印章，章面是一个「名」字。
印章在中文语境里就是「盖章确认」，与产品「正名」同义；
朱红 + 米白的配色取自真实印章的印泥与宣纸。

超采样 4 倍绘制再降采样，边缘比直接画更干净。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent
S = 4
SIZE = 1024

INK_TOP = (27, 43, 74, 255)      # #1B2B4A
INK_BOTTOM = (12, 20, 34, 255)   # #0C1422
SEAL = (200, 69, 47, 255)        # #C8452F
PAPER = (245, 239, 227, 255)     # #F5EFE3

PINGFANG = "/System/Library/Fonts/PingFang.ttc"
MONO = "/System/Library/Fonts/SFNSMono.ttf"

# 印章方块：位置与圆角（基于 1024 画布）
SEAL_BOX = (212, 212, 812, 812)
SEAL_RADIUS = 72
GLYPH_SIZE = 368


def gradient(n: int) -> Image.Image:
    strip = Image.new("RGBA", (1, n))
    for y in range(n):
        t = y / max(1, n - 1)
        strip.putpixel((0, y), tuple(
            round(INK_TOP[i] + (INK_BOTTOM[i] - INK_TOP[i]) * t) for i in range(4)
        ))
    return strip.resize((n, n), Image.BILINEAR)


def icon(corner_ratio: float = 1.0) -> Image.Image:
    n = SIZE * S
    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, n - 1, n - 1], radius=int(230 * S * corner_ratio), fill=255)
    canvas.paste(gradient(n), (0, 0), mask)

    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([v * S for v in SEAL_BOX],
                        radius=SEAL_RADIUS * S, fill=SEAL)

    font = ImageFont.truetype(PINGFANG, GLYPH_SIZE * S, index=4)
    box = d.textbbox((0, 0), "名", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    x = (n - w) / 2 - box[0]
    y = (n - h) / 2 - box[1]
    d.text((x, y), "名", font=font, fill=PAPER)

    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def tracked(d, xy, text, font, fill, tracking):
    """按固定字距逐字绘制，PIL 的 text 不支持 letter-spacing。"""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking


CN_TEXT = "文件改名"
CN_SIZE = 104
EN_SIZE = 38


def lockup() -> Image.Image:
    pad, mark_size, gap = 80, 320, 64
    cn = ImageFont.truetype(PINGFANG, CN_SIZE, index=4)
    en = ImageFont.truetype(MONO, EN_SIZE)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    # 汉字按墨迹边界摆放：CJK 字形左右留白不对称，
    # 直接用字体步进会排出比预期大得多的假字距。
    inks = [probe.textbbox((0, 0), ch, font=cn) for ch in CN_TEXT]
    widths = [b[2] - b[0] for b in inks]
    tracking = 8
    pair_w = sum(widths) + tracking * (len(CN_TEXT) - 1)
    cn_h = max(b[3] for b in inks) - min(b[1] for b in inks)

    en_w = sum(probe.textlength(c, font=en) for c in "rnbatch") + 6 * 6
    h = 480
    block_h = cn_h + 24 + EN_SIZE
    top = (h - block_h) // 2

    w = pad * 2 + mark_size + gap + int(max(pair_w, en_w))
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(icon(1.0).resize((mark_size, mark_size), Image.LANCZOS),
                           (pad, (h - mark_size) // 2))

    d = ImageDraw.Draw(canvas)
    tx = pad + mark_size + gap
    y = top - inks[0][1]
    x = tx - inks[0][0]
    for ch, ink, width in zip(CN_TEXT, inks, widths):
        d.text((x, y), ch, font=cn, fill=(20, 32, 58, 255))
        x += width + tracking
    tracked(d, (tx + 2, top + cn_h + 24), "rnbatch", en, (122, 140, 168, 255), 6)
    return canvas


if __name__ == "__main__":
    print("渲染中：")
    for name, img in (
        ("logo-rounded-1024", icon(1.0)),
        ("logo-square-1024", icon(0.0)),
        ("logo-lockup-1600", lockup()),
    ):
        path = OUT / f"{name}.png"
        img.save(path, optimize=True)
        print(f"  {path.name}  {path.stat().st_size // 1024} KB")
