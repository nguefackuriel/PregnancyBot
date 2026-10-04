"""
Lecteur local par défaut : CRNN entraîné sur les cellules du carnet (data/models/crnn.pt).

Tourne sur CPU, sans réseau, sans clé. Une cellule se lit en quelques millisecondes,
une page complète en une à deux secondes. Les données ne quittent jamais l'appareil.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..geometry import crop
from ..schema import DATA
from .base import RawRead, Reader


class CRNNReader(Reader):
    name = "crnn"

    def __init__(self, weights: str | None = None, pad: int = 2):
        from ..ocr.crnn import OCR
        if weights:
            w = Path(weights)
        else:
            w = DATA / "models" / "crnn.onnx"
            if not w.exists():
                w = DATA / "models" / "crnn.pt"
        if not w.exists():
            raise FileNotFoundError(f"modèle absent : {w} (lancer scripts/train_crnn.py puis scripts/export_onnx.py)")
        self.ocr = OCR(str(w))
        self.pad = pad

    def read_fields(self, page, page_type, keys, template, context=None):
        crops, order = [], []          # (clé, n° de ligne) par recadrage
        for k in keys:
            tb = template["fields"].get(k)
            if not tb:
                continue
            z = crop(page, tb["bbox_px"], pad=self.pad)
            if z.size == 0 or z.shape[0] < 6 or z.shape[1] < 6:
                continue
            for i, line in enumerate(text_lines(z)):
                crops.append(line)
                order.append((k, i))
        out = {k: RawRead(None, 0.0) for k in keys}
        if not crops:
            return out
        parts: dict[str, list[tuple[str, float]]] = {}
        for (k, _), (text, conf) in zip(order, self.ocr.read(crops)):
            parts.setdefault(k, []).append((text.strip(), conf))
        for k, ps in parts.items():
            texts = [t for t, _ in ps if t]
            text = " ".join(texts)
            conf = min(c for _, c in ps) if len(ps) > 1 else ps[0][1]
            out[k] = RawRead(text or None, conf, legible=conf >= 0.3)
        return out


def text_lines(z, max_h: int = 56, gap: int = 6, min_band: int = 8) -> list:
    """Une grande zone de texte libre (hauteur > max_h) est découpée en lignes
    d'écriture avant lecture : le modèle lit des lignes de 32 px de haut, et une
    boîte de 300 px écrasée à 32 px n'est plus lisible.

    Les bandes d'encre sont trouvées par le profil des lignes (fond = médiane
    locale). Sans encre, la zone entière est rendue telle quelle.
    """
    h = z.shape[0]
    if h <= max_h:
        return [z]
    g = cv2.cvtColor(z, cv2.COLOR_BGR2GRAY) if z.ndim == 3 else z
    bg = float(np.median(g))
    ink = (g < bg - max(25.0, 0.15 * bg)).astype(np.uint8)
    # on retire les traits longs (bordures de la boîte), qui ne sont pas de l'écriture
    long_h = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, z.shape[1] // 3), 1)))
    long_v = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, h // 3))))
    ink = ink & (1 - long_h) & (1 - long_v)
    ink[:, :4] = 0
    ink[:, -4:] = 0
    rows = ink.sum(axis=1) >= 2
    bands, start = [], None
    for y, on in enumerate(rows.tolist() + [False]):
        if on and start is None:
            start = y
        elif not on and start is not None:
            if bands and start - bands[-1][1] <= gap:
                bands[-1] = (bands[-1][0], y)
            else:
                bands.append((start, y))
            start = None
    bands = [(a, b) for a, b in bands if b - a >= min_band]
    if not bands:
        return [z]
    out = []
    for a, b in bands:
        y0, y1 = max(0, a - 5), min(h, b + 5)
        line = z[y0:y1]
        if line.shape[0] >= 6:
            out.append(line)
    return out or [z]
