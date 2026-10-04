"""
Géométrie : qualité d'image, type de page, recalage sur le gabarit, recadrage.

Toutes les coordonnées de gabarit sont en pixels dans le repère 1654 × 2339
(celui des PNG fournis). Une photo est d'abord ramenée dans ce repère par une
homographie (contour de la page) puis affinée par les lignes du formulaire.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from .schema import PAGE_TYPES, load_templates

TPL_W, TPL_H = 1654, 2339


# --------------------------------------------------------------------------- #
# Qualité d'image (contrôle avant acceptation de la capture, bonus du défi)
# --------------------------------------------------------------------------- #
@dataclass
class Quality:
    ok: bool
    blur: float          # variance du laplacien (plus haut = plus net)
    brightness: float    # 0..255
    width: int
    height: int
    reasons: list[str] = field(default_factory=list)


def assess_quality(img: np.ndarray) -> Quality:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = g.shape[:2]
    scale = 1200 / max(h, w)
    gs = cv2.resize(g, None, fx=scale, fy=scale) if scale < 1 else g
    blur = float(cv2.Laplacian(gs, cv2.CV_64F).var())
    bright = float(gs.mean())
    reasons = []
    if min(h, w) < 600:
        reasons.append("résolution trop faible (min 600 px)")
    if blur < 25:
        reasons.append("image floue")
    if bright < 60:
        reasons.append("image trop sombre")
    if bright > 235:
        reasons.append("image surexposée")
    return Quality(not reasons, round(blur, 1), round(bright, 1), w, h, reasons)


# --------------------------------------------------------------------------- #
# Recalage
# --------------------------------------------------------------------------- #
@dataclass
class Registration:
    image: np.ndarray            # image recalée 1654 × 2339 (BGR)
    H: np.ndarray                # homographie original -> gabarit
    method: str
    score: float                 # 0..1 (accord des lignes avec le gabarit)

    def to_original(self, bbox_px):
        """bbox dans le repère gabarit -> bbox dans l'image d'origine."""
        x0, y0, x1, y1 = bbox_px
        pts = np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]).reshape(-1, 1, 2)
        Hinv = np.linalg.inv(self.H)
        q = cv2.perspectiveTransform(pts, Hinv).reshape(-1, 2)
        return [int(q[:, 0].min()), int(q[:, 1].min()), int(q[:, 0].max()), int(q[:, 1].max())]


def _page_mask(img: np.ndarray) -> np.ndarray:
    """Masque de la page (claire) sur fond sombre.

    Seuil = entre le niveau du fond (bande de bordure de l'image) et celui de la
    page (zone centrale), ce qui tient même quand la page est très sombre ; Otsu
    en secours quand la bordure n'est pas du fond.
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    v = cv2.GaussianBlur(hsv[..., 2], (0, 0), 3)
    h, w = v.shape
    b = max(4, int(0.03 * min(h, w)))
    border = np.concatenate([v[:b].ravel(), v[-b:].ravel(), v[:, :b].ravel(), v[:, -b:].ravel()])
    center = v[int(0.3 * h): int(0.7 * h), int(0.3 * w): int(0.7 * w)]
    bg_med, pg_med = float(np.median(border)), float(np.median(center))
    if pg_med > bg_med + 30:
        t = bg_med + 0.45 * (pg_med - bg_med)
        _, m = cv2.threshold(v, t, 255, cv2.THRESH_BINARY)
    else:
        _, m = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = max(9, int(min(h, w) * 0.02)) | 1
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    return m


def _page_quad(img: np.ndarray):
    """Quadrilatère de la page : 4 coins extrêmes de la plus grande composante claire."""
    mask = _page_mask(img)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 0.2 * img.shape[0] * img.shape[1]:
        return None
    hull = cv2.convexHull(c).reshape(-1, 2).astype(np.float32)
    s = hull.sum(1)
    d = hull[:, 0] - hull[:, 1]
    tl, br = hull[np.argmin(s)], hull[np.argmax(s)]
    tr, bl = hull[np.argmax(d)], hull[np.argmin(d)]
    quad = np.float32([tl, tr, br, bl])
    # cohérence : un vrai quadrilatère de page a des côtés opposés de longueurs voisines
    w1, w2 = np.linalg.norm(tr - tl), np.linalg.norm(br - bl)
    h1, h2 = np.linalg.norm(bl - tl), np.linalg.norm(br - tr)
    if min(w1, w2) / max(w1, w2) < 0.6 or min(h1, h2) / max(h1, h2) < 0.6:
        return None
    return quad, area / (img.shape[0] * img.shape[1])


def _line_profile(gray: np.ndarray, axis: int) -> np.ndarray:
    """Profil des lignes sombres longues (0 = horizontales, 1 = verticales)."""
    # lignes du formulaire = pixels sombres formant de longs segments ; on ignore
    # le fond sombre hors page (zones larges très sombres) en exigeant un contraste
    # local : pixel nettement plus sombre que son voisinage flou
    blur = cv2.GaussianBlur(gray, (0, 0), 15).astype(np.int16)
    thr = np.maximum(8, (0.10 * blur).astype(np.int16))         # contraste relatif (pages sombres / floues)
    dark = ((gray.astype(np.int16) < blur - thr) & (gray < 190)).astype(np.uint8) * 255
    k = (61, 1) if axis == 0 else (1, 61)          # (largeur, hauteur) pour OpenCV
    opened = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, k))
    prof = (opened > 0).sum(axis=1 - axis).astype(np.float64)
    prof -= prof.mean()
    return prof


def _best_affine_1d(prof: np.ndarray, positions: list[int], length: int):
    """(scale, offset) maximisant la somme du profil aux positions du gabarit (grossier -> fin)."""
    pos = np.array(positions, dtype=np.float64)

    def val(scale, off):
        idx = np.round(pos * scale + off).astype(int)
        idx = idx[(idx >= 0) & (idx < length)]
        return prof[idx].sum() if len(idx) else -1e18

    best = (1.0, 0, -1e18)
    for scale in np.linspace(0.90, 1.10, 41):
        for off in range(-120, 121, 4):
            v = val(scale, off)
            if v > best[2]:
                best = (float(scale), int(off), float(v))
    s0, o0, _ = best
    for scale in np.linspace(s0 - 0.006, s0 + 0.006, 13):
        for off in range(o0 - 5, o0 + 6):
            v = val(scale, off)
            if v > best[2]:
                best = (float(scale), int(off), float(v))
    return best


def warp_to_page(img: np.ndarray) -> tuple[np.ndarray, np.ndarray, str]:
    """Étape 1, indépendante du type de page : ramène la page dans le cadre 1654 × 2339."""
    h, w = img.shape[:2]
    quad = _page_quad(img)
    full_frame = abs(w / h - TPL_W / TPL_H) < 0.03
    if quad is not None and (quad[1] < 0.93 or not full_frame):
        src = quad[0]
        dst = np.float32([[0, 0], [TPL_W, 0], [TPL_W, TPL_H], [0, TPL_H]])
        H = cv2.getPerspectiveTransform(src, dst).astype(np.float64)
        method = "page_quad"
    else:
        H = np.array([[TPL_W / w, 0, 0], [0, TPL_H / h, 0], [0, 0, 1]], dtype=np.float64)
        method = "scale"
    warped = cv2.warpPerspective(img, H, (TPL_W, TPL_H), flags=cv2.INTER_LINEAR, borderValue=(245, 190, 205))
    return warped, H, method


def _profile_peaks(prof: np.ndarray, min_gap: int = 8) -> list[int]:
    """Positions des lignes détectées : maxima locaux au-dessus d'un seuil robuste."""
    thr = max(np.percentile(prof, 93), prof.mean() + 1.5 * prof.std())
    peaks, last = [], -10 ** 6
    for i in range(1, len(prof) - 1):
        if prof[i] >= thr and prof[i] >= prof[i - 1] and prof[i] >= prof[i + 1]:
            if i - last >= min_gap:
                peaks.append(i)
                last = i
    return peaks


def line_signature_scores(warped: np.ndarray) -> dict[str, float]:
    """Accord (F1) entre les lignes détectées et celles de chaque gabarit, H et V.

    Rappel = lignes du gabarit retrouvées ; précision = lignes détectées
    expliquées par le gabarit : un gabarit pauvre en lignes ne peut donc pas
    « gagner » sur une page qui en a beaucoup.
    """
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    peaks = {axis: np.array(_profile_peaks(_line_profile(gray, axis))) for axis in (0, 1)}
    scores = {}
    for pt, tpl in load_templates().items():
        tot, wsum = 0.0, 0.0
        for axis, key, wgt in ((0, "h", 1.0), (1, "v", 0.5)):
            pos = np.array(tpl["lines"][key])
            pk = peaks[axis]
            if len(pos) < 2 or len(pk) == 0:
                continue
            best = 0.0
            for off in range(-30, 31, 2):
                d = np.abs((pos + off)[:, None] - pk[None, :])
                recall = float((d.min(axis=1) <= 4).mean())
                precision = float((d.min(axis=0) <= 4).mean())
                f1 = 2 * precision * recall / (precision + recall + 1e-9)
                best = max(best, f1)
            tot += wgt * best
            wsum += wgt
        scores[pt] = tot / wsum if wsum else 0.0
    return scores


def _line_hit_axes(gray: np.ndarray, hs: list[int], vs: list[int]) -> tuple[float | None, float | None]:
    """Part des lignes du gabarit qui tombent sur une ligne détectée, par axe (h, v)."""
    out = []
    for axis, pos, length in ((0, hs, TPL_H), (1, vs, TPL_W)):
        if len(pos) < 2:
            out.append(None)
            continue
        prof = _line_profile(gray, axis)
        thr = np.percentile(prof, 88)
        idx = np.array([p for p in pos if 0 <= p < length])
        # tolérance ± 3 px
        hits = [(prof[max(0, i - 3): i + 4] > thr).any() for i in idx]
        out.append(float(np.mean(hits)) if hits else 0.0)
    return out[0], out[1]


def _line_hit(gray: np.ndarray, hs: list[int], vs: list[int]) -> float:
    """Part des lignes du gabarit qui tombent sur une ligne détectée (0..1), moyenne des deux axes."""
    vals = [v for v in _line_hit_axes(gray, hs, vs) if v is not None]
    return float(np.mean(vals)) if vals else 0.0


def register(img: np.ndarray, page_type: str, prewarped: tuple | None = None) -> Registration:
    """Ramène une photo dans le repère du gabarit du type de page donné.

    1. perspective par le contour de la page (indépendant du type),
    2. affinage échelle + décalage par les lignes du formulaire, conservé
       seulement s'il améliore l'accord avec le gabarit.
    """
    tpl = load_templates()[page_type]
    warped, H, method = prewarped if prewarped is not None else warp_to_page(img)
    hs, vs = tpl["lines"]["h"], tpl["lines"]["v"]
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    h0, v0 = _line_hit_axes(gray, hs, vs)
    score0 = float(np.mean([v for v in (h0, v0) if v is not None])) if (h0 is not None or v0 is not None) else 0.0
    best = Registration(warped, H, method, round(score0, 2))
    if len(hs) >= 4:
        prof_h = _line_profile(gray, 0)
        sy, oy, _ = _best_affine_1d(prof_h, hs, TPL_H)
        if len(vs) >= 6:
            prof_v = _line_profile(gray, 1)
            sx, ox, _ = _best_affine_1d(prof_v, vs, TPL_W)
        else:
            # seulement le bord de page (2 à 4 lignes) : l'axe x n'est pas contraint,
            # on garde la perspective du contour
            sx, ox = 1.0, 0
        # chaque axe est jugé sur ses propres lignes : un bon ajustement vertical ne
        # doit pas faire accepter un décalage horizontal faux (pages à soulignements)
        candidates = [((sx, ox), (sy, oy)), ((1.0, 0), (sy, oy)), ((sx, ox), (1.0, 0))]
        seen = set()
        for (cx, cox), (cy, coy) in candidates:
            if (cx, cox, cy, coy) in seen or (cx == 1.0 and cox == 0 and cy == 1.0 and coy == 0):
                continue
            seen.add((cx, cox, cy, coy))
            A = np.array([[cx, 0, cox], [0, cy, coy], [0, 0, 1]], dtype=np.float64)
            H2 = np.linalg.inv(A) @ H
            warped2 = cv2.warpPerspective(img, H2, (TPL_W, TPL_H), flags=cv2.INTER_LINEAR, borderValue=(245, 190, 205))
            h1, v1 = _line_hit_axes(cv2.cvtColor(warped2, cv2.COLOR_BGR2GRAY), hs, vs)
            score1 = float(np.mean([v for v in (h1, v1) if v is not None])) if (h1 is not None or v1 is not None) else 0.0
            # un axe modifié ne doit pas perdre ses propres lignes
            x_ok = (cx == 1.0 and cox == 0) or v1 is None or v0 is None or v1 >= v0 - 1e-6
            y_ok = (cy == 1.0 and coy == 0) or h1 is None or h0 is None or h1 >= h0 - 1e-6
            if x_ok and y_ok and score1 >= 0.3 and score1 > best.score + 0.05:
                best = Registration(warped2, H2, method + "+lines", round(score1, 2))
    return best


# --------------------------------------------------------------------------- #
# Type de page
# --------------------------------------------------------------------------- #
_TITLE_PATTERNS = {
    "COUVERTURE": ["FICHE", "SURVEILLANCE", "GROSSESSE"],
    "IDENTIFICATION_ANTECEDENTS": ["IDENTIFICATION", "ANTECEDENTS"],
    "GROSSESSE_ACTUELLE": ["GROSSESSE", "ACTUELLE", "DDR"],
    "ACCOUCHEMENT": ["DEROULEMENT", "ACCOUCHEMENT", "LIEU"],
    "PP_PRECOCE_MERE": ["POST-PARTUM", "PRECOCE", "MERE"],
    "PP_PRECOCE_NOUVEAU_NE": ["POST-PARTUM", "PRECOCE", "NOUVEAU-NE"],
    "PP_TARDIF_MERE": ["POST-PARTUM", "TARDIF", "MERE"],
    "PP_TARDIF_NOUVEAU_NE": ["POST-PARTUM", "TARDIF", "NOUVEAU-NE"],
}
# mots qui départagent des pages jumelles : s'ils sont lus, ils doivent l'emporter
_EXCLUSIVE = {("PRECOCE", "TARDIF"), ("MERE", "NOUVEAU-NE"), ("GROSSESSE", "ACCOUCHEMENT")}
_PAIRS = {("PP_PRECOCE_MERE", "PP_TARDIF_MERE"), ("PP_PRECOCE_NOUVEAU_NE", "PP_TARDIF_NOUVEAU_NE")}


def _fold(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s.upper())


def tesseract_config(extra: str = "") -> tuple[str, str]:
    """(lang, config) : modèle français embarqué dans data/tessdata si tesseract ne l'a pas."""
    from .schema import DATA
    local = DATA / "tessdata"
    cfg = extra
    lang = "fra"
    try:
        import pytesseract
        langs = pytesseract.get_languages(config="")
    except Exception:
        langs = []
    if "fra" not in langs:
        if (local / "fra.traineddata").exists():
            cfg = f'--tessdata-dir "{local}" ' + extra
        else:
            lang = "eng"
    return lang, cfg.strip()


def _ocr_title(warped: np.ndarray) -> str:
    try:
        import pytesseract
    except Exception:
        return ""
    top = warped[: int(TPL_H * 0.26)]
    g = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    g = cv2.resize(g, None, fx=1.3, fy=1.3, interpolation=cv2.INTER_CUBIC)
    lang, cfg = tesseract_config("--psm 6")
    try:
        return pytesseract.image_to_string(g, lang=lang, config=cfg)
    except Exception:
        try:
            return pytesseract.image_to_string(g, lang="eng", config="--psm 6")
        except Exception:
            return ""


def _title_scores(ocr_text: str) -> dict[str, float]:
    """Score de chaque type de page d'après les mots du titre lus par OCR (tolérant aux fautes)."""
    t = _fold(ocr_text)
    tokens = set(re.findall(r"[A-Z][A-Z\-]{2,}", t))
    all_words = {w for pats in _TITLE_PATTERNS.values() for w in pats}
    found = {}
    for w in all_words:
        if w in t:
            found[w] = 1.0
        else:
            m = difflib.get_close_matches(w, tokens, n=1, cutoff=0.75)
            found[w] = 0.8 if m else 0.0
    # un mot exclusif lu nettement fait perdre son jumeau (TARDIF lu => PRECOCE = 0)
    for a, b in _EXCLUSIVE:
        if found.get(a, 0) >= 0.8 and found.get(b, 0) >= 0.8:
            continue
        if found.get(a, 0) >= 0.8:
            found[b] = 0.0
        elif found.get(b, 0) >= 0.8:
            found[a] = 0.0
    scores = {}
    for pt, pats in _TITLE_PATTERNS.items():
        scores[pt] = sum(found.get(w, 0.0) for w in pats) / len(pats)
    return scores


def detect_page_type(img: np.ndarray, warped: np.ndarray | None = None, ocr_text: str | None = None) -> tuple[str | None, float]:
    """Type de page = OCR du titre (page redressée) puis signature des lignes du formulaire.

    Retourne (type, score) ; score < 0.6 signifie « à confirmer par la sage-femme »
    (notamment précoce / tardif, dont la mise en page est identique).
    """
    if warped is None:
        warped, _, _ = warp_to_page(img)
    if ocr_text is None:
        ocr_text = _ocr_title(warped)
    ts = _title_scores(ocr_text)
    best_ocr = max(ts, key=ts.get)
    if ts[best_ocr] >= 0.6:
        return best_ocr, round(ts[best_ocr], 2)
    ls = line_signature_scores(warped)
    ranked = sorted(ls.items(), key=lambda kv: -kv[1])
    (p1, s1), (p2, s2) = ranked[0], ranked[1]
    t = _fold(ocr_text)
    if (p1, p2) in _PAIRS or (p2, p1) in _PAIRS:
        # même mise en page : départager par un mot du titre, sinon demander
        if "TARDIF" in t or "40" in t or "50" in t:
            pick = p1 if "TARDIF" in p1 else p2
            return pick, 0.7
        if "PRECOCE" in t or "7EME" in t or "8EME" in t:
            pick = p1 if "PRECOCE" in p1 else p2
            return pick, 0.7
        return p1, 0.5
    if s1 > 0.45 and (s1 - s2) > 0.12:
        return p1, round(min(0.9, 0.6 + (s1 - s2)), 2)
    combined = {pt: ts[pt] + 0.8 * max(0.0, ls[pt]) for pt in ts}
    best = max(combined, key=combined.get)
    if combined[best] >= 0.45:
        return best, round(min(0.6, combined[best]), 2)
    return None, round(combined[best], 2)


# --------------------------------------------------------------------------- #
# Recadrage et encre
# --------------------------------------------------------------------------- #
def crop(img: np.ndarray, bbox, pad: int = 6) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    h, w = img.shape[:2]
    return img[max(0, y0 - pad): min(h, y1 + pad), max(0, x0 - pad): min(w, x1 + pad)]


def page_background(img: np.ndarray) -> np.ndarray:
    """Couleur de fond de la page (médiane d'un sous-échantillon)."""
    return np.median(img[::16, ::16].reshape(-1, 3), axis=0)


def ink_mask(img: np.ndarray, bbox, shrink: int = 4, bg=None, remove_lines: bool = True) -> np.ndarray | None:
    """Masque des pixels d'encre dans la zone, lignes du formulaire retirées."""
    x0, y0, x1, y1 = bbox
    x0, y0, x1, y1 = x0 + shrink, y0 + shrink, x1 - shrink, y1 - shrink
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    z = img[max(0, y0):y1, max(0, x0):x1]
    if z.size == 0:
        return None
    # encre = pixels éloignés de la couleur de fond locale (médiane de la zone) :
    # fonctionne pour l'encre bleue sur papier rose comme pour le noir sur blanc
    if bg is None:
        bg = np.median(z.reshape(-1, 3), axis=0)
    dist = np.abs(z.astype(np.int16) - np.asarray(bg).astype(np.int16)).sum(axis=2)
    thr = int(np.clip(0.14 * float(np.sum(bg)), 30, 90))        # contraste relatif à la clarté du papier
    m = (dist > thr).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))   # bruit isolé (JPEG, capteur)
    if not remove_lines:
        return m.astype(bool)
    # retirer les segments rectilignes longs (bordures de cellules, soulignements)
    h, w = m.shape
    kh = max(9, min(31, int(0.6 * w)))
    kv = max(9, min(31, int(0.6 * h)))
    lines = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (kh, 1)))
    lines |= cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, kv)))
    lines = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    return (m & (1 - lines)).astype(bool)


def ink_ratio(img: np.ndarray, bbox, shrink: int = 4) -> float:
    m = ink_mask(img, bbox, shrink)
    return float(m.mean()) if m is not None else 0.0


def ink_count(img: np.ndarray, bbox, shrink: int = 4) -> int:
    m = ink_mask(img, bbox, shrink)
    return int(m.sum()) if m is not None else 0


def has_ink(img: np.ndarray, bbox, thresh: float = 0.003, min_pixels: int = 10, min_blob: int = 8) -> bool:
    """De l'encre « réelle » : assez de pixels ET au moins une composante compacte (pas du bruit épars).

    Les seuils sont bas pour ne pas rater un tiret fin (8 x 2 px dans certaines
    écritures). Sur une photo bruitée, quelques cellules vides passent alors au
    lecteur ; `looks_like_dash` et la lecture vide les ramènent à NON_FOURNI.
    """
    m = ink_mask(img, bbox)
    if m is None or m.sum() < min_pixels:
        return False
    if not (m.mean() > thresh or m.sum() >= 120):
        return False
    n, _, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
    biggest = stats[1:, cv2.CC_STAT_AREA].max() if n > 1 else 0
    return bool(biggest >= min_blob)


def looks_like_dash(img: np.ndarray, bbox) -> bool:
    """Un tiret manuscrit : un seul trait court (4 à 16 px de large, 2 à 7 px de haut)
    qui porte au moins la moitié de l'encre de la cellule.

    Un reste de bordure de cellule après recalage est long et fin (1 à 2 px de
    haut, souvent plus de 16 px de long) ; du bruit JPEG est éparpillé en
    plusieurs taches. Mesuré sur les 80 pages et sur les photos dégradées :
    122 tirets sur 126 acceptés, 65 restes de bordure sur 80 refusés.
    """
    m = ink_mask(img, bbox)
    if m is None or m.sum() == 0:
        return False
    n, _, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
    if n <= 1:
        return False
    areas = stats[1:, cv2.CC_STAT_AREA]
    i = 1 + int(areas.argmax())
    w, h = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
    frac = float(areas.max()) / float(m.sum())
    return frac >= 0.5 and 4 <= w <= 16 and 2 <= h <= 7 and w <= 6 * h


def checkbox_checked(img: np.ndarray, bbox, margin: int = 8, bg=None) -> tuple[bool, float]:
    """Une case cochée a de l'encre à l'intérieur de sa bordure.

    La bordure est localisée dans l'image (pics des sommes de lignes/colonnes
    autour de la position du gabarit) pour tolérer un recalage à ± margin px.
    """
    x0, y0, x1, y1 = bbox
    big = [x0 - margin, y0 - margin, x1 + margin, y1 + margin]
    # fond = médiane LOCALE du recadrage élargi (robuste aux ombres) ; la case
    # et sa coche occupent moins de la moitié de la zone, la médiane reste le papier
    m = ink_mask(img, big, shrink=0, bg=None, remove_lines=False)
    if m is None or m.size == 0:
        return False, 0.0
    h, w = m.shape
    side = max(6, min(x1 - x0, y1 - y0))
    cols, rows = m.sum(axis=0), m.sum(axis=1)
    # bordure gauche/droite : meilleure paire de colonnes distantes de ~side
    def best_pair(prof, length):
        best, bs = (margin, margin + side), -1
        for a in range(0, length - side + 1):
            for d in (side - 2, side - 1, side, side + 1, side + 2):
                b = a + d
                if b >= length:
                    continue
                s = prof[a] + prof[b]
                if s > bs:
                    best, bs = (a, b), s
        return best
    cx0, cx1 = best_pair(cols, w)
    cy0, cy1 = best_pair(rows, h)
    inner = m[cy0 + 4: cy1 - 3, cx0 + 4: cx1 - 3]
    r = float(inner.mean()) if inner.size else 0.0
    # vide ≈ 0 ; cochée (croix / hachures) ≥ 0.2
    checked = r > 0.16
    conf = min(1.0, 0.5 + abs(r - 0.16) / 0.16 * 0.5)
    return checked, round(conf, 2)
