"""Your own photo library (PHOTO_DIR): pictures you took, bought, or have written permission to use.

Name files after what they show; add the credit after a double underscore:
    narendra_modi_eknath_shinde_meeting__PIB.jpg      -> credit "PIB" (shown on screen + in the caption)
    eden_gardens_stadium_crowd__Your Name.jpg
    election_commission_building.jpg                  -> no credit part: nothing is shown on screen
Folder names also count as search words. Matching needs most of the story's names to appear in the file name.
No network, no licence lookup: YOU are responsible for having the right to use these photos.
"""
from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

from . import config
from .commons import PhotoSource
from .stock import overlap, query_terms

EXT = {".jpg", ".jpeg", ".png"}


class LocalPhotos(PhotoSource):
    name = "local"

    def __init__(self, directory: str | Path | None = None):
        d = directory if directory is not None else config.PHOTO_DIR
        self.dir = Path(d) if d else None
        self.enabled = bool(self.dir and self.dir.is_dir())

    def candidates(self, query: str, context=None) -> list[dict]:
        if not self.enabled:
            return []
        terms = query_terms(query)
        need = max(1, math.ceil(0.6 * len(terms)))
        out = []
        for p in sorted(self.dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in EXT:
                continue
            left, _, right = p.stem.partition("__")
            folders = " ".join(p.relative_to(self.dir).parts[:-1])
            text = re.sub(r"[_\\-]+", " ", f"{folders} {left}")
            score = overlap(terms, text)
            if score < need:
                continue
            credit = re.sub(r"[_]+", " ", right).strip()
            out.append({"title": left, "url": str(p), "score": score, "width": 9999, "height": 9999,
                        "credit": credit or "Own photo", "via": "", "needs_credit": bool(credit),
                        "license": "own library", "creator": credit, "text": text, "page_url": "", "date": ""})
        return out

    def download(self, url: str, dest: Path) -> Path:
        shutil.copyfile(url, dest)
        return dest
