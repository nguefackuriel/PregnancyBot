"""
Lecteur local de secours : tesseract cellule par cellule.

Faible sur l'écriture manuscrite (c'est attendu) mais il tourne hors ligne,
sans aucun service tiers, et lit correctement les dates / nombres imprimés ou
très lisibles. Il sert de solution de repli quand le lecteur VLM est
indisponible et de référence basse dans l'évaluation.
"""
from __future__ import annotations

import re

import cv2
import numpy as np

from ..geometry import crop, tesseract_config
from ..schema import field_def
from .base import RawRead, Reader

NUM_TYPES = {"date", "int", "float", "ta", "poids_g", "poids_kg", "longueur_cm", "temperature", "age_gestationnel"}


class TesseractReader(Reader):
    name = "tesseract"

    def __init__(self, lang: str = "fra", scale: float = 2.5):
        import pytesseract  # noqa: F401
        self.lang = lang
        self.scale = scale

    def _prep(self, z: np.ndarray) -> np.ndarray:
        g = cv2.cvtColor(z, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_CUBIC)
        g = cv2.GaussianBlur(g, (3, 3), 0)
        _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return cv2.copyMakeBorder(b, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)

    def read_fields(self, page, page_type, keys, template, context=None):
        import pytesseract
        out = {}
        for k in keys:
            tb = template["fields"].get(k)
            if not tb:
                out[k] = RawRead(None, 0.0)
                continue
            z = crop(page, tb["bbox_px"], pad=2)
            if z.size == 0:
                out[k] = RawRead(None, 0.0)
                continue
            t = field_def(page_type, k).get("type", "text")
            cfg = "--psm 7"
            if t in NUM_TYPES:
                cfg += " -c tessedit_char_whitelist=0123456789/.,+-:SAjkgcmdLl°"
            lang, cfg = tesseract_config(cfg)
            img = self._prep(z)
            data = pytesseract.image_to_data(img, lang=lang, config=cfg, output_type=pytesseract.Output.DICT)
            words, confs = [], []
            for w, c in zip(data["text"], data["conf"]):
                if w.strip():
                    words.append(w.strip())
                    try:
                        confs.append(float(c))
                    except ValueError:
                        pass
            raw = " ".join(words)
            raw = re.sub(r"^[|\[\]_~'`\"]+|[|\[\]_~'`\"]+$", "", raw).strip()
            conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
            out[k] = RawRead(raw or None, round(min(conf, 0.75), 2))   # jamais > 0.75 : lecteur faible
        return out
