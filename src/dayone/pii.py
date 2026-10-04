"""Masquage des identifiants directs sur l'image avant tout stockage."""
from __future__ import annotations

import hashlib

import cv2
import numpy as np


def redact(img: np.ndarray, boxes: list[list[int]], pad: int = 6) -> np.ndarray:
    """Noircit les zones PII (nom, CIN, adresse, téléphone, nom du mari)."""
    out = img.copy()
    h, w = out.shape[:2]
    for b in boxes or []:
        if not b:
            continue
        x0, y0, x1, y1 = b
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
        if x1 > x0 and y1 > y0:
            cv2.rectangle(out, (x0, y0), (x1, y1), (20, 20, 20), thickness=-1)
            cv2.putText(out, "PII", (x0 + 4, min(y1 - 4, y0 + 22)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    return out


def encode_jpeg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
