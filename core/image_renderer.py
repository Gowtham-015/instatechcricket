"""Branded 4:5 (1080x1350) carousel cards rendered with Pillow. Output is JPEG (the only format
Instagram's publishing API accepts). Text is auto-fitted so it can never overflow a card, and only
original typography is used (no third-party news photos -> no copyright / hotlink failures)."""
from __future__ import annotations

import logging
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from . import config
from .feeds import CATEGORY_STYLE

log = logging.getLogger("renderer")

W, H = 1080, 1350
MARGIN = 96
TEXT_W = W - 2 * MARGIN

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


def _background(category: str) -> Image.Image:
    top, bottom, accent = CATEGORY_STYLE.get(category, CATEGORY_STYLE["News"])
    mask = Image.linear_gradient("L").resize((W, H))
    img = Image.composite(Image.new("RGB", (W, H), hex_rgb(bottom)), Image.new("RGB", (W, H), hex_rgb(top)), mask)
    # soft decorative circles
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    ar = hex_rgb(accent)
    od.ellipse((W - 420, -260, W + 260, 420), fill=(*ar, 34))
    od.ellipse((-300, H - 420, 380, H + 260), fill=(255, 255, 255, 18))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _chrome(draw, idx: int, total: int, accent, now: datetime) -> None:
    f_small = load_font(30, True)
    draw.text((MARGIN, 70), clean_for_image(config.BRAND_NAME).upper(), font=f_small, fill=(255, 255, 255))
    right = f"{idx}/{total}"
    draw.text((W - MARGIN - draw.textlength(right, font=f_small), 70), right, font=f_small, fill=accent)
    draw.rectangle((MARGIN, 124, MARGIN + 90, 130), fill=accent)
    f_foot = load_font(30, False)
    draw.text((MARGIN, H - 96), clean_for_image(config.ACCOUNT_HANDLE), font=f_foot, fill=(230, 235, 245))
    d = now.strftime("%d %b %Y").upper()
    draw.text((W - MARGIN - draw.textlength(d, font=f_foot), H - 96), d, font=f_foot, fill=(200, 208, 225))


def _pill(draw, xy, text, fill, fg, font) -> int:
    x, y = xy
    tw = draw.textlength(text, font=font)
    h = font.size + 26
    draw.rounded_rectangle((x, y, x + tw + 52, y + h), radius=h // 2, fill=fill)
    draw.text((x + 26, y + 11), text, font=font, fill=fg)
    return y + h


def render_cover(content: dict, cluster: dict, total: int, now: datetime) -> Image.Image:
    img = _background(cluster["category"])
    draw = ImageDraw.Draw(img)
    accent = hex_rgb(CATEGORY_STYLE.get(cluster["category"], CATEGORY_STYLE["News"])[2])
    _chrome(draw, 1, total, accent, now)
    f_pill = load_font(32, True)
    y = _pill(draw, (MARGIN, 250), cluster["category"].upper(), accent, (15, 15, 25), f_pill)
    if cluster.get("breaking"):
        _pill(draw, (MARGIN + 320, 250), "BREAKING", (220, 38, 38), (255, 255, 255), f_pill)
    font, lines, lh = fit_text(draw, content["hook"], TEXT_W, 640, start=104, min_size=56)
    _draw_lines(draw, lines, font, MARGIN, y + 70, lh, (255, 255, 255))
    f_sw = load_font(36, True)
    sw = "SWIPE  >"
    draw.text((W - MARGIN - draw.textlength(sw, font=f_sw), H - 190), sw, font=f_sw, fill=accent)
    return img


def render_content(slide: dict, cluster: dict, idx: int, total: int, now: datetime) -> Image.Image:
    img = _background(cluster["category"])
    draw = ImageDraw.Draw(img)
    accent = hex_rgb(CATEGORY_STYLE.get(cluster["category"], CATEGORY_STYLE["News"])[2])
    _chrome(draw, idx, total, accent, now)
    hfont, hlines, hlh = fit_text(draw, slide["heading"], TEXT_W, 220, start=76, min_size=44)
    head_h = len(hlines) * hlh
    max_body_h = H - 330 - 250 - head_h - 70
    bfont, blines, blh = fit_text(draw, slide["body"], TEXT_W, max_body_h, start=64, min_size=34, bold=False,
                                  spacing=1.35)
    block_h = head_h + 70 + len(blines) * blh
    y0 = max(240, (H - block_h) // 2 - 20)  # vertically centred block
    y = _draw_lines(draw, hlines, hfont, MARGIN, y0, hlh, accent)
    draw.rectangle((MARGIN, y + 18, MARGIN + 140, y + 24), fill=(255, 255, 255))
    _draw_lines(draw, blines, bfont, MARGIN, y + 70, blh, (255, 255, 255))
    return img


def render_closing(content: dict, cluster: dict, idx: int, total: int, now: datetime) -> Image.Image:
    img = _background(cluster["category"])
    draw = ImageDraw.Draw(img)
    accent = hex_rgb(CATEGORY_STYLE.get(cluster["category"], CATEGORY_STYLE["News"])[2])
    _chrome(draw, idx, total, accent, now)
    y = 250
    why = content.get("why_it_matters")
    if why:
        hfont = load_font(64, True)
        draw.text((MARGIN, y), "Why it matters", font=hfont, fill=accent)
        font, lines, lh = fit_text(draw, why, TEXT_W, 420, start=50, min_size=34, bold=False, spacing=1.35)
        y = _draw_lines(draw, lines, font, MARGIN, y + 110, lh, (255, 255, 255))
        y += 40
    else:
        y = 330
    draw.rectangle((MARGIN, y, W - MARGIN, y + 3), fill=(255, 255, 255))
    src = "Source: " + ", ".join(cluster["sources"][:3])
    sfont, slines, slh = fit_text(draw, src, TEXT_W, 130, start=36, min_size=26, bold=False)
    y = _draw_lines(draw, slines, sfont, MARGIN, y + 36, slh, (215, 222, 238))
    cta_font = load_font(58, True)
    draw.text((MARGIN, H - 430), "Follow for daily updates", font=cta_font, fill=(255, 255, 255))
    draw.text((MARGIN, H - 350), clean_for_image(config.ACCOUNT_HANDLE), font=cta_font, fill=accent)
    f = load_font(34, False)
    draw.text((MARGIN, H - 260), "Save   |   Share   |   Follow", font=f, fill=(230, 235, 245))
    return img


def render_post(content: dict, cluster: dict, out_dir: Path, stem: str, now: datetime | None = None) -> list[Path]:
    """Render cover + content slides + closing card -> list of JPEG paths (2..10 files)."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slides = content["slides"][:8]
    total = len(slides) + 2
    imgs = [render_cover(content, cluster, total, now)]
    for i, s in enumerate(slides, start=2):
        imgs.append(render_content(s, cluster, i, total, now))
    imgs.append(render_closing(content, cluster, total, total, now))
    paths = []
    for i, im in enumerate(imgs, start=1):
        p = out_dir / f"{stem}_{i:02d}.jpg"
        im.convert("RGB").save(p, "JPEG", quality=90, optimize=True, subsampling=0)
        paths.append(p)
    return paths
