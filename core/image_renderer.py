"""Branded 4:5 (1080x1350) carousel cards rendered with Pillow. Output is JPEG (the only format
Instagram's publishing API accepts). Text is auto-fitted so it can never overflow a card, and only
original typography is used (no third-party news photos -> no copyright / hotlink failures)."""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from . import config
from .feeds import CATEGORY_STYLE

log = logging.getLogger("renderer")

W, H = 1080, 1350
MARGIN = 96


@dataclass(frozen=True)
class Layout:
    """Geometry of one canvas. REEL keeps text out of Instagram's top/bottom/right UI overlay zones."""
    w: int
    h: int
    chrome_y: int       # brand + page counter
    content_top: int
    content_bottom: int
    foot_y: int         # handle + date
    cta_y: int          # closing-card call to action
    hook_max_h: int
    text_w: int
    swipe_y: int | None  # "SWIPE" hint (feed carousels only)
    hook_size: int = 104
    heading_size: int = 76
    body_size: int = 64
    center_cover: bool = False  # vertically centre the cover block (used for tall Reels)


FEED = Layout(1080, 1350, chrome_y=70, content_top=250, content_bottom=1040, foot_y=1254, cta_y=920,
              hook_max_h=640, text_w=888, swipe_y=1160)
REEL = Layout(1080, 1920, chrome_y=250, content_top=400, content_bottom=1400, foot_y=1455, cta_y=1130,
              hook_max_h=820, text_w=834, swipe_y=None,
              hook_size=124, heading_size=88, body_size=74, center_cover=True)

_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
]
_FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
]
_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}


def _find_font(bold: bool) -> str | None:
    cands = ([config.FONT_PATH] if config.FONT_PATH else []) + (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG)
    for p in cands:
        if p and Path(p).exists():
            return p
    return None


def load_font(size: int, bold: bool = True):
    key = (size, bold)
    if key not in _font_cache:
        path = _find_font(bold)
        try:
            _font_cache[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        except Exception:
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


def clean_for_image(text: str) -> str:
    """Remove emoji / unrenderable symbols (fonts show them as empty boxes)."""
    out = []
    for ch in text:
        if ord(ch) > 0xFFFF or unicodedata.category(ch) in {"So", "Cs", "Cc", "Cf"}:
            continue
        out.append(ch)
    return " ".join("".join(out).split())


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        # hard-break absurdly long tokens
        while draw.textlength(word, font=font) > max_w and len(word) > 1:
            cut = len(word)
            while cut > 1 and draw.textlength(word[:cut], font=font) > max_w:
                cut -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:cut])
            word = word[cut:]
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text: str, max_w: int, max_h: int, start: int, min_size: int, bold: bool = True,
             spacing: float = 1.22):
    """Largest font size (start..min_size) whose wrapped text fits max_h. Truncates only as last resort."""
    text = clean_for_image(text)
    size = start
    while size >= min_size:
        font = load_font(size, bold)
        lines = wrap(draw, text, font, max_w)
        lh = int(size * spacing)
        if len(lines) * lh <= max_h:
            return font, lines, lh
        size -= 2
    font = load_font(min_size, bold)
    lh = int(min_size * spacing)
    lines = wrap(draw, text, font, max_w)
    keep = max(1, max_h // lh)
    if len(lines) > keep:
        lines = lines[:keep]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return font, lines, lh


def _draw_lines(draw, lines, font, x, y, lh, fill) -> int:
    for ln in lines:
        draw.text((x, y), ln, font=font, fill=fill)
        y += lh
    return y


def _background(category: str, L: Layout) -> Image.Image:
    top, bottom, accent = CATEGORY_STYLE.get(category, CATEGORY_STYLE["News"])
    mask = Image.linear_gradient("L").resize((L.w, L.h))
    img = Image.composite(Image.new("RGB", (L.w, L.h), hex_rgb(bottom)),
                          Image.new("RGB", (L.w, L.h), hex_rgb(top)), mask)
    overlay = Image.new("RGBA", (L.w, L.h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    ar = hex_rgb(accent)
    od.ellipse((L.w - 420, -260, L.w + 260, 420), fill=(*ar, 34))
    od.ellipse((-300, L.h - 420, 380, L.h + 260), fill=(255, 255, 255, 18))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _accent(cluster: dict):
    return hex_rgb(CATEGORY_STYLE.get(cluster["category"], CATEGORY_STYLE["News"])[2])


def _chrome(draw, idx: int, total: int, accent, now: datetime, L: Layout) -> None:
    f_small = load_font(30, True)
    y = L.chrome_y
    draw.text((MARGIN, y), clean_for_image(config.BRAND_NAME).upper(), font=f_small, fill=(255, 255, 255))
    right = f"{idx}/{total}"
    draw.text((L.w - MARGIN - draw.textlength(right, font=f_small), y), right, font=f_small, fill=accent)
    draw.rectangle((MARGIN, y + 54, MARGIN + 90, y + 60), fill=accent)
    f_foot = load_font(30, False)
    draw.text((MARGIN, L.foot_y), clean_for_image(config.ACCOUNT_HANDLE), font=f_foot, fill=(230, 235, 245))
    d = now.strftime("%d %b %Y").upper()
    draw.text((L.w - MARGIN - draw.textlength(d, font=f_foot), L.foot_y), d, font=f_foot, fill=(200, 208, 225))


def _pill(draw, xy, text, fill, fg, font) -> int:
    x, y = xy
    tw = draw.textlength(text, font=font)
    h = font.size + 26
    draw.rounded_rectangle((x, y, x + tw + 52, y + h), radius=h // 2, fill=fill)
    draw.text((x + 26, y + 11), text, font=font, fill=fg)
    return y + h


def render_cover(content: dict, cluster: dict, total: int, now: datetime, L: Layout = FEED) -> Image.Image:
    img = _background(cluster["category"], L)
    draw = ImageDraw.Draw(img)
    accent = _accent(cluster)
    _chrome(draw, 1, total, accent, now, L)
    f_pill = load_font(32, True)
    font, lines, lh = fit_text(draw, content["hook"], L.text_w, L.hook_max_h, start=L.hook_size, min_size=56)
    pill_h = f_pill.size + 26
    block_h = pill_h + 70 + len(lines) * lh
    top = L.content_top
    if L.center_cover:
        top = max(L.content_top, (L.content_top + L.content_bottom) // 2 - block_h // 2)
    y = _pill(draw, (MARGIN, top), cluster["category"].upper(), accent, (15, 15, 25), f_pill)
    if cluster.get("breaking"):
        _pill(draw, (MARGIN + 320, top), "BREAKING", (220, 38, 38), (255, 255, 255), f_pill)
    _draw_lines(draw, lines, font, MARGIN, y + 70, lh, (255, 255, 255))
    if L.swipe_y:
        f_sw = load_font(36, True)
        sw = "SWIPE  >"
        draw.text((L.w - MARGIN - draw.textlength(sw, font=f_sw), L.swipe_y), sw, font=f_sw, fill=accent)
    return img


def render_content(slide: dict, cluster: dict, idx: int, total: int, now: datetime, L: Layout = FEED) -> Image.Image:
    img = _background(cluster["category"], L)
    draw = ImageDraw.Draw(img)
    accent = _accent(cluster)
    _chrome(draw, idx, total, accent, now, L)
    hfont, hlines, hlh = fit_text(draw, slide["heading"], L.text_w, 240, start=L.heading_size, min_size=44)
    head_h = len(hlines) * hlh
    max_body_h = (L.content_bottom - L.content_top) - head_h - 70
    bfont, blines, blh = fit_text(draw, slide["body"], L.text_w, max_body_h, start=L.body_size, min_size=34, bold=False,
                                  spacing=1.35)
    block_h = head_h + 70 + len(blines) * blh
    center = (L.content_top + L.content_bottom) // 2
    y0 = max(L.content_top - 10, center - block_h // 2)  # vertically centred block
    y = _draw_lines(draw, hlines, hfont, MARGIN, y0, hlh, accent)
    draw.rectangle((MARGIN, y + 18, MARGIN + 140, y + 24), fill=(255, 255, 255))
    _draw_lines(draw, blines, bfont, MARGIN, y + 70, blh, (255, 255, 255))
    return img


def render_closing(content: dict, cluster: dict, idx: int, total: int, now: datetime, L: Layout = FEED) -> Image.Image:
    img = _background(cluster["category"], L)
    draw = ImageDraw.Draw(img)
    accent = _accent(cluster)
    _chrome(draw, idx, total, accent, now, L)
    y = L.content_top
    why = content.get("why_it_matters")
    if why:
        draw.text((MARGIN, y), "Why it matters", font=load_font(64, True), fill=accent)
        font, lines, lh = fit_text(draw, why, L.text_w, 420, start=50, min_size=34, bold=False, spacing=1.35)
        y = _draw_lines(draw, lines, font, MARGIN, y + 110, lh, (255, 255, 255)) + 40
    else:
        y = L.content_top + 80
    draw.rectangle((MARGIN, y, L.w - MARGIN, y + 3), fill=(255, 255, 255))
    src = "Source: " + ", ".join(cluster["sources"][:3])
    sfont, slines, slh = fit_text(draw, src, L.text_w, 130, start=36, min_size=26, bold=False)
    _draw_lines(draw, slines, sfont, MARGIN, y + 36, slh, (215, 222, 238))
    cta_font = load_font(58, True)
    draw.text((MARGIN, L.cta_y), "Follow for daily updates", font=cta_font, fill=(255, 255, 255))
    draw.text((MARGIN, L.cta_y + 80), clean_for_image(config.ACCOUNT_HANDLE), font=cta_font, fill=accent)
    draw.text((MARGIN, L.cta_y + 170), "Save   |   Share   |   Follow", font=load_font(34, False), fill=(230, 235, 245))
    return img


def render_slides(content: dict, cluster: dict, now: datetime | None = None, layout: Layout = FEED) -> list[Image.Image]:
    """Cover + content slides + closing card as in-memory images for the given layout."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    slides = content["slides"][:8]
    total = len(slides) + 2
    imgs = [render_cover(content, cluster, total, now, layout)]
    for i, sl in enumerate(slides, start=2):
        imgs.append(render_content(sl, cluster, i, total, now, layout))
    imgs.append(render_closing(content, cluster, total, total, now, layout))
    return imgs


def render_post(content: dict, cluster: dict, out_dir: Path, stem: str, now: datetime | None = None) -> list[Path]:
    """Feed carousel -> list of JPEG paths (2..10 files)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, im in enumerate(render_slides(content, cluster, now, FEED), start=1):
        p = out_dir / f"{stem}_{i:02d}.jpg"
        im.convert("RGB").save(p, "JPEG", quality=90, optimize=True, subsampling=0)
        paths.append(p)
    return paths
