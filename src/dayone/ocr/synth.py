"""
Rendu synthétique de cellules manuscrites pour entraîner le CRNN.

Les 5 polices d'écriture sont celles du PDF spécimen (sous-ensembles extraits et
réparés, data/fonts/). Les textes sont tirés des types de champs du carnet
(dates, nombres, TA, âges gestationnels, vocabulaire fermé, noms) et des
valeurs réelles de la vérité terrain. Les images reçoivent les dégradations
d'une photo de téléphone : fond rose ou blanc, bordures de cellule, rotation,
flou, bruit, JPEG, encre bleue ou noire.
"""
from __future__ import annotations

import io
import json
import random
import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .crnn import CHAR2IDX, clean_label

ENUM_WORDS = ["Neg", "Pos", "Oui", "Non", "Normales", "Pâles", "Décolorées", "Normaux", "RAS", "Immune", "Non immune",
              "Fermé", "Ouvert", "Céphalique", "Siège", "Normal", "Anormal", "Voie basse", "Césarienne", "Forceps", "Ventouse",
              "Aucun", "Aucune", "Néant", "Lycée", "Collège", "Primaire", "Supérieur", "NF", "IG", "Reçu", "nég", "pos",
              "Non fait", "Propre", "Propre, sèche", "Cycles réguliers", "Père", "Mère", "Oncle", "Sage-femme", "Inf. Zahra",
              "Dr Benjelloun", "Dr Chakir", "Inf. Salima", "Inf. Hajar", "Étudiante", "Ouvrier", "Agricultrice", "Commerçante",
              "Femme au foyer", "Employée", "Couturière", "Fer 2 cp/j", "Vitamine D 400 UI/j", "Souffrance fœtale",
              "Utérus cicatriciel", "Pré-éclampsie sévère", "Appendicectomie", "Asthme léger", "Poursuivre l'allaitement exclusif",
              "Souhaite en discuter avec son mari", "Test rapide", "HCV nég", "Colorées", "Echo obst", "F", "M"]
PLACES = ["CSCA Al Wifaq", "CSC Hay Salam", "DR Tahannaout Sud", "CSCA Ait Mhamed", "CSC Al Amal", "DR Bni Ahmed", "CSU Ennahda",
          "CSCA Lahraouiyine", "CSC Ksar Jdid", "CSUA Hay Riad", "Kénitra", "Meknès", "Al Haouz", "Azilal", "Taroudant", "Chefchaouen",
          "Berkane", "Settat", "Errachidia", "Khémisset", "Rabat-Salé-Kénitra", "Fès-Meknès", "Marrakech-Safi", "Béni Mellal-Khénifra",
          "Souss-Massa", "Tanger-Tétouan-Al Hoceïma", "Oriental", "Casablanca-Settat", "Drâa-Tafilalet", "Rue Al Qods", "Rue Zerktouni",
          "Rue Mohammed V", "Rue Ibn Sina", "Rue Al Massira", "CIS Sidi Smail", "El Jadida", "Casa-Settat"]


class TextSampler:
    def __init__(self, rng: random.Random, extra_values: list[str] | None = None):
        self.rng = rng
        self.extra = [v for v in (extra_values or []) if v and len(v) <= 40]

    def date(self):
        r = self.rng
        d, m, y = r.randint(1, 28), r.randint(1, 12), r.randint(2015, 2027)
        f = r.random()
        if f < 0.7:
            return f"{d:02d}/{m:02d}/{y}"
        if f < 0.85:
            return f"{d:02d}/{m:02d}/{y % 100:02d}"
        return f"{d}/{m}/{y}"

    def number(self):
        r = self.rng
        k = r.random()
        if k < 0.2:
            return str(r.randint(0, 99))
        if k < 0.35:
            return f"{r.uniform(45, 95):.1f}"
        if k < 0.45:
            return f"{r.uniform(8, 15):.1f} g/dL"
        if k < 0.52:
            return f"{r.uniform(0.6, 1.3):.2f} g/L"
        if k < 0.6:
            return f"{r.randint(150, 400)}k"
        if k < 0.7:
            return f"{r.randint(2300, 4800)} g"
        if k < 0.8:
            return f"{r.randint(28, 60)} cm"
        if k < 0.88:
            return f"{r.uniform(36, 39):.1f}" + (" °C" if r.random() < 0.3 else "")
        if k < 0.94:
            return f"{r.randint(140, 180)} cm"
        return f"{r.randint(100, 165)}"

    def ta(self):
        r = self.rng
        s, d = r.randint(90, 160), r.randint(50, 100)
        return f"{s}/{d}" if r.random() < 0.85 else f"{s // 10}/{d // 10}"

    def ag(self):
        r = self.rng
        w = r.randint(5, 41)
        k = r.random()
        if k < 0.5:
            return f"{w} SA"
        if k < 0.8:
            return f"{w}SA+{r.randint(0, 6)}j"
        return f"{w} SA + {r.randint(0, 6)} j"

    def sample(self) -> str:
        r = self.rng
        k = r.random()
        if k < 0.07:
            return ""                      # cellule vide : le modèle doit répondre « rien »
        if k < 0.24:
            return self.date()
        if k < 0.44:
            return self.number()
        if k < 0.52:
            return self.ta()
        if k < 0.60:
            return self.ag()
        if k < 0.80:
            return r.choice(ENUM_WORDS)
        if k < 0.88:
            return r.choice(PLACES)
        if k < 0.92:
            return "—" if r.random() < 0.7 else r.choice(["-", "/", "RAS", "NF"])
        if self.extra and k < 0.98:
            return r.choice(self.extra)
        # mot au hasard (noms, mots isolés)
        return r.choice(["Tazi Meryem", "Haddad Nadia", "Idrissi Salma", "Benali", "Alaoui", "Bennani", "El Idrissi", "Lahlou",
                         "Ouazzani", "Saidi", "Mohamed Tazi", "Khadija", "Loubna", "Zineb", "Hajar"])


def load_fonts(fonts_dir: Path) -> list[tuple[str, set[str]]]:
    from fontTools.ttLib import TTFont
    out = []
    for p in sorted(fonts_dir.glob("*.ttf")):
        tt = TTFont(str(p))
        chars = {chr(c) for t in tt["cmap"].tables if t.platformID == 3 for c in t.cmap}
        out.append((str(p), chars))
    if not out:
        raise FileNotFoundError(f"aucune police dans {fonts_dir}")
    return out


def rendered_label(text: str, chars: set[str]) -> str:
    """Ce que montre vraiment l'image : un caractère absent de la police laisse un blanc."""
    out = "".join(c if c in chars else " " for c in text)
    out = re.sub(r"\s+", " ", out).strip()
    return clean_label(out)


class Renderer:
    def __init__(self, fonts_dir: Path, rng: random.Random):
        self.fonts = load_fonts(fonts_dir)
        self.rng = rng
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def font(self, path: str, size: int):
        k = (path, size)
        if k not in self._cache:
            self._cache[k] = ImageFont.truetype(path, size)
        return self._cache[k]

    def render(self, text: str) -> tuple[np.ndarray, str]:
        r = self.rng
        path, chars = r.choice(self.fonts)
        label = rendered_label(text, chars)
        size = r.randint(26, 46)
        font = self.font(path, size)
        draw_text = "".join(c if c in chars else " " for c in text)
        tmp = ImageDraw.Draw(Image.new("L", (10, 10)))
        try:
            x0, y0, x1, y1 = tmp.textbbox((0, 0), draw_text, font=font)
        except Exception:
            x0, y0, x1, y1 = 0, 0, 10 * len(draw_text), size
        tw, th = max(8, x1 - x0), max(8, y1 - y0)
        if not text.strip():
            tw, th = r.randint(30, 260), r.randint(14, 40)      # taille d'une cellule du carnet
        padx, pady = r.randint(6, 40), r.randint(4, 18)
        W, H = tw + 2 * padx, th + 2 * pady
        # fond : rose carnet, blanc, ou teinte aléatoire claire
        k = r.random()
        if k < 0.6:
            bg = (247 + r.randint(-20, 8), 188 + r.randint(-25, 20), 204 + r.randint(-25, 20))
        elif k < 0.85:
            bg = (r.randint(225, 255),) * 3
        else:
            bg = (r.randint(200, 250), r.randint(200, 250), r.randint(200, 250))
        ink = (r.randint(10, 60), r.randint(10, 70), r.randint(120, 200)) if r.random() < 0.75 else (r.randint(0, 50),) * 3
        im = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(im)
        d.text((padx - x0 + r.randint(-3, 3), pady - y0 + r.randint(-3, 3)), draw_text, font=font, fill=ink)
        # bordures de cellule / soulignement (ce que voit le recadrage réel)
        line_col = (r.randint(20, 80),) * 3
        if r.random() < 0.5:
            d.line([(r.randint(0, 4), 0), (r.randint(0, 4), H)], fill=line_col, width=r.randint(1, 3))
        if r.random() < 0.5:
            d.line([(W - r.randint(1, 5), 0), (W - r.randint(1, 5), H)], fill=line_col, width=r.randint(1, 3))
        if r.random() < 0.4:
            d.line([(0, r.randint(0, 3)), (W, r.randint(0, 3))], fill=line_col, width=r.randint(1, 3))
        if r.random() < 0.5:
            yy = H - r.randint(1, 6)
            d.line([(0, yy), (W, yy)], fill=line_col, width=r.randint(1, 3))
        if r.random() < 0.2:   # soulignement de champ formulaire, sous le texte
            yy = pady - y0 + th + r.randint(0, 6)
            d.line([(r.randint(0, 10), yy), (W, yy)], fill=line_col, width=1)
        if not text.strip():
            # cellule vide d'une photo : restes de bordure après recalage, petites
            # taches. Étiquette vide : c'est ce que le modèle doit apprendre à dire.
            # Attention : un trait horizontal court et un peu épais EST un tiret.
            # Les restes de bordure sont longs et fins (1 px), ou verticaux.
            for _ in range(r.randint(0, 3)):
                if r.random() < 0.6:
                    x, y, L = r.randint(0, max(1, W - 20)), r.randint(1, H - 2), r.randint(18, 70)
                    d.line([(x, y), (min(W - 1, x + L), y)], fill=line_col, width=1)
                else:
                    x, y, L = r.randint(2, W - 3), r.randint(0, max(1, H - 6)), r.randint(4, 24)
                    d.line([(x, y), (x + r.randint(-1, 1), min(H - 1, y + L))], fill=line_col, width=r.randint(1, 2))
            for _ in range(r.randint(0, 6)):
                x, y = r.randint(0, W - 3), r.randint(0, H - 3)
                d.rectangle([x, y, x + r.randint(0, 2), y + r.randint(0, 2)], fill=line_col)
        arr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
        arr = self.degrade(arr)
        return arr, label

    def degrade(self, arr: np.ndarray) -> np.ndarray:
        r = self.rng
        h, w = arr.shape[:2]
        # rotation légère + cisaillement
        if r.random() < 0.8:
            ang = r.uniform(-3, 3)
            M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, r.uniform(0.9, 1.1))
            M[0, 1] += r.uniform(-0.08, 0.08)
            bgc = tuple(int(v) for v in np.median(arr.reshape(-1, 3), axis=0))
            arr = cv2.warpAffine(arr, M, (w, h), borderValue=bgc)
        # éclairage inégal
        if r.random() < 0.5:
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            g = 1 + r.uniform(-0.25, 0.25) * (xx / w - 0.5) + r.uniform(-0.25, 0.25) * (yy / h - 0.5)
            arr = np.clip(arr.astype(np.float32) * g[..., None] * r.uniform(0.55, 1.1), 0, 255).astype(np.uint8)
        # flou
        if r.random() < 0.7:
            s = r.uniform(0.3, 1.6)
            arr = cv2.GaussianBlur(arr, (0, 0), s)
        # bruit
        if r.random() < 0.7:
            arr = np.clip(arr.astype(np.float32) + np.random.default_rng(r.randint(0, 10 ** 9)).normal(0, r.uniform(2, 10), arr.shape), 0, 255).astype(np.uint8)
        # rééchantillonnage (téléphone) + JPEG
        if r.random() < 0.6:
            f = r.uniform(0.5, 1.0)
            arr = cv2.resize(arr, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        if r.random() < 0.7:
            ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, r.randint(35, 90)])
            arr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return arr


def gt_values(gt_path: Path) -> list[str]:
    """Valeurs réelles de la vérité terrain (pour enrichir les textes synthétiques)."""
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    vals = set()
    for pg in gt["pages"]:
        for k, f in pg["fields"].items():
            if f.get("pii") or f.get("status_on_image") != "CONNU" or not f.get("value"):
                continue
            vals.add(str(f["value"]))
    return sorted(vals)
