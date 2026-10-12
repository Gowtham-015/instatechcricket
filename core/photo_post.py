"""Photo-first editorial posts. The real photograph is the visual focus; text sits in a compact panel.

  render_photo_post      one 4:5 JPEG: photo + headline + one line of context + credit
  render_photo_carousel  cover photo post, then photo slides (each with its own caption) and fact slides, closing card
  render_fact_card       typographic card for stories with no usable media, labelled as an editorial graphic

Layout follows the picture: landscape photos fill the top of the canvas with a panel below; portrait and square
photos run full-bleed with a gradient behind a short text block. Output is JPEG (the only format the Instagram API
accepts for images). A photo that is only a file photo (right subject, not shown to be this event) always carries a
"FILE PHOTO" badge. Photos are never stretched, and nothing is invented: text comes from the validated copy.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageOps

from . import config
from .image_renderer import (FEED, H, MARGIN, W, _accent, _background, _chrome, _draw_lines, _pill, clean_for_image,
                             fit_text, load_font, render_closing, render_content)
from .provenance import MediaAsset

log = logging.getLogger("photo_post")
LANDSCAPE_AR = 1.2           # width/height at or above this -> photo on top, panel below
PANEL_RATIO = 0.40           # share of the canvas the text panel may use at most


def load_photo(path: str | Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def cover_crop(img: Image.Image, w: int, h: int, focus_y: float = 0.35) -> Image.Image:
    """Fill w x h without distortion. Vertical crops are biased towards the upper part (faces, scoreboards, banners)."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = max(w, round(iw * scale)), max(h, round(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = int((nh - h) * focus_y)
    return img.crop((left, top, left + w, top + h))


def _gradient(w: int, h: int, strength: int = 235) -> Image.Image:
    """Transparent -> dark, top to bottom (keeps the photo visible above the text)."""
    mask = Image.linear_gradient("L").resize((w, h)).point(lambda v: int(v * strength / 255))
    layer = Image.new("RGBA", (w, h), (8, 12, 22, 0))
    layer.putalpha(mask)
    return layer


def credit_line(asset: MediaAsset, cluster: dict) -> str:
    who = asset.attribution or asset.creator or asset.source
    bits = [f"Photo: {who}"] if who else []
    if cluster.get("sources"):
        bits.append("Report: " + cluster["sources"][0])
    return clean_for_image("  |  ".join(bits))


def _badges(draw, accent, cluster: dict, asset: MediaAsset, x: int, y: int) -> None:
    f = load_font(30, True)
    for text, fill, fg in ((cluster["category"].upper(), accent, (15, 15, 25)),
                           ("BREAKING" if cluster.get("breaking") else "", (220, 38, 38), (255, 255, 255)),
                           ("FILE PHOTO" if asset.role != "event" else "", (245, 245, 245), (20, 20, 30))):
        if text:
            _pill(draw, (x, y), text, fill, fg, f)
            x += int(draw.textlength(text, font=f)) + 52 + 16


def render_photo_slide(asset: MediaAsset, headline: str, context: str, cluster: dict, now: datetime,
                       idx: int = 1, total: int = 1) -> Image.Image:
    photo = load_photo(asset.path)
    ar = photo.width / photo.height
    accent = _accent(cluster)
    canvas = Image.new("RGB", (W, H), (10, 14, 26))
    probe = ImageDraw.Draw(canvas)
    f_credit = load_font(26, False)
    credit = credit_line(asset, cluster)

    if ar >= LANDSCAPE_AR:
        photo_h = int(H * (1 - 0.34))                   # photo gets ~66% of the card
        canvas.paste(cover_crop(photo, W, photo_h), (0, 0))
        panel_top = photo_h
        draw = ImageDraw.Draw(canvas)
        for i in range(24):                              # soft seam between photo and panel
            draw.line((0, panel_top - i, W, panel_top - i), fill=(10, 14, 26))
        canvas.paste(_background(cluster["category"], FEED).crop((0, 0, W, H - panel_top)), (0, panel_top))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, panel_top, W, panel_top + 6), fill=accent)
        y = panel_top + 34
        text_bottom = H - 110
    else:
        canvas.paste(cover_crop(photo, W, H, focus_y=0.3), (0, 0))
        grad_h = int(H * PANEL_RATIO)
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(_gradient(W, grad_h), (0, H - grad_h))
        canvas = canvas.convert("RGB")
        draw = ImageDraw.Draw(canvas)
        y = H - grad_h + 90
        text_bottom = H - 110

    draw = ImageDraw.Draw(canvas)
    # headline: short, large; context: small, at most 3 lines
    max_head_h = int((text_bottom - y) * 0.62)
    hfont, hlines, hlh = fit_text(draw, headline, W - 2 * MARGIN, max_head_h, start=68, min_size=44)
    y = _draw_lines(draw, hlines, hfont, MARGIN, y, hlh, (255, 255, 255))
    if context:
        cfont, clines, clh = fit_text(draw, context, W - 2 * MARGIN, max(60, text_bottom - y - 20), start=34, min_size=26,
                                      bold=False, spacing=1.3)
        _draw_lines(draw, clines, cfont, MARGIN, y + 14, clh, (225, 231, 242))
    # chrome on the photo: badges top-left, brand + counter top-right
    f_small = load_font(30, True)
    _badges(draw, accent, cluster, asset, MARGIN, 56)
    if total > 1:
        right = f"{idx}/{total}"
        draw.text((W - MARGIN - draw.textlength(right, font=f_small), 66), right, font=f_small, fill=(255, 255, 255),
                  stroke_width=2, stroke_fill=(0, 0, 0))
    # footer: credit line + handle
    draw.text((MARGIN, H - 92), credit, font=f_credit, fill=(205, 212, 226))
    f_foot = load_font(28, True)
    draw.text((MARGIN, H - 54), clean_for_image(config.BRAND_NAME).upper() + "  " + clean_for_image(config.ACCOUNT_HANDLE),
              font=f_foot, fill=accent)
    d = now.strftime("%d %b %Y").upper()
    draw.text((W - MARGIN - draw.textlength(d, font=f_foot), H - 54), d, font=f_foot, fill=(200, 208, 225))
    return canvas


def _context_line(content: dict) -> str:
    why = (content.get("why_it_matters") or "").strip()
    if why:
        return why
    first = (content.get("caption") or "").strip().split("\n")[0]
    return first[:170]


def _save(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "JPEG", quality=92, optimize=True, subsampling=0)
    return path


def render_photo_post(content: dict, cluster: dict, asset: MediaAsset, out_dir: Path, stem: str,
                      now: datetime | None = None) -> list[Path]:
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    img = render_photo_slide(asset, content["hook"], _context_line(content), cluster, now)
    return [_save(img, Path(out_dir) / f"{stem}_photo.jpg")]


def render_photo_carousel(content: dict, cluster: dict, assets: list[MediaAsset], out_dir: Path, stem: str,
                          now: datetime | None = None) -> list[Path]:
    """Cover photo, then each fact slide: a photo slide when a further photo exists, else a clean fact slide."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    slides = content["slides"][:6]
    extra = list(assets[1:])
    total = 1 + len(slides) + 1
    imgs = [render_photo_slide(assets[0], content["hook"], _context_line(content), cluster, now, 1, total)]
    for i, sl in enumerate(slides, start=2):
        if extra:
            imgs.append(render_photo_slide(extra.pop(0), sl["heading"], sl["body"], cluster, now, i, total))
        else:
            imgs.append(render_content(sl, cluster, i, total, now, FEED))
    imgs.append(render_closing(content, cluster, total, total, now, FEED))
    return [_save(im, Path(out_dir) / f"{stem}_{n:02d}.jpg") for n, im in enumerate(imgs[:10], start=1)]


def render_fact_card(content: dict, cluster: dict, out_dir: Path, stem: str, now: datetime | None = None) -> list[Path]:
    """Original editorial card from the verified copy. Says plainly that it is a graphic, not a photograph."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    img = _background(cluster["category"], FEED)
    draw = ImageDraw.Draw(img)
    accent = _accent(cluster)
    _chrome(draw, 1, 1, accent, now, FEED)
    f_pill = load_font(32, True)
    y = _pill(draw, (MARGIN, 230), cluster["category"].upper(), accent, (15, 15, 25), f_pill)
    if cluster.get("breaking"):
        _pill(draw, (MARGIN + 320, 230), "BREAKING", (220, 38, 38), (255, 255, 255), f_pill)
    hfont, hlines, hlh = fit_text(draw, content["hook"], W - 2 * MARGIN, 420, start=84, min_size=54)
    y = _draw_lines(draw, hlines, hfont, MARGIN, y + 50, hlh, (255, 255, 255))
    draw.rectangle((MARGIN, y + 18, MARGIN + 140, y + 24), fill=accent)
    y += 60
    facts = [s["body"] for s in content.get("slides", [])[:3]] or [_context_line(content)]
    for fact in facts:
        if y > 1030:
            break
        bfont, blines, blh = fit_text(draw, fact, W - 2 * MARGIN - 40, min(230, 1060 - y), start=40, min_size=28,
                                      bold=False, spacing=1.3)
        draw.ellipse((MARGIN, y + 14, MARGIN + 16, y + 30), fill=accent)
        y = _draw_lines(draw, blines, bfont, MARGIN + 40, y, blh, (236, 240, 248)) + 22
    src = "Source: " + ", ".join(cluster["sources"][:3])
    sfont, slines, slh = fit_text(draw, src, W - 2 * MARGIN, 90, start=32, min_size=24, bold=False)
    _draw_lines(draw, slines, sfont, MARGIN, 1130, slh, (215, 222, 238))
    draw.text((MARGIN, 1196), "EDITORIAL GRAPHIC  |  no photograph used", font=load_font(26, True), fill=accent)
    return [_save(img, Path(out_dir) / f"{stem}_card.jpg")]
