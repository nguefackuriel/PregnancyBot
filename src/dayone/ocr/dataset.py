"""Jeux de données pour le CRNN : recadrages réels (vérité terrain) + rendu synthétique à la volée."""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

import cv2
import numpy as np
import torch
from torch.utils.data import Dataset

from .crnn import MAX_W, preprocess
from .synth import Renderer, TextSampler, gt_values, load_fonts, rendered_label

FONT_FILES = {"Caveat-Regular": "caveat.ttf", "ShadowsIntoLight": "shadowsintolight.ttf", "NanumPen-Regular": "nanumpen.ttf",
              "Gaegu-Regular": "gaegu.ttf", "ReenieBeanie": "reeniebeanie.ttf"}


def real_crops(gt_path: Path, images_dir: Path, fonts_dir: Path, pad: int = 6,
               templates_path: Path | None = None, empties_per_page: int = 20, seed: int = 0) -> list[tuple[np.ndarray, str, int]]:
    """(image, label tel que rendu, n° de patiente) pour chaque valeur CONNU de la vérité terrain.

    Ajoute aussi, par page, quelques cellules vides (boîte du gabarit, étiquette
    vide) : une fois dégradées à l'entraînement, elles ressemblent au bruit d'une
    vraie photo, et le modèle apprend à n'y lire rien plutôt qu'un tiret.
    """
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    templates = json.loads(templates_path.read_text(encoding="utf-8")) if templates_path and templates_path.exists() else {}
    rng = random.Random(seed)
    chars_by_font = {}
    for name, f in FONT_FILES.items():
        p = fonts_dir / f
        if p.exists():
            chars_by_font[name] = load_fonts(p.parent)[[Path(x[0]).name for x in load_fonts(p.parent)].index(f)][1]
    # index des images disponibles (doublons renommés compris)
    files = {}
    for p in list(images_dir.glob("*.png")) + list(images_dir.glob("*.jpg")):
        m = re.search(r"patientes-(\d\d)", p.name)
        if m:
            files.setdefault(m.group(1), p)
    out = []
    for pg in gt["pages"]:
        m = re.search(r"patientes-(\d\d)", pg["png_file"])
        if not m or m.group(1) not in files:
            continue
        img = cv2.imread(str(files[m.group(1)]))
        if img is None:
            continue
        font = (pg.get("handwriting_font") or [""])[0]
        chars = chars_by_font.get(font)
        for k, f in pg["fields"].items():
            if f.get("pii") or not f.get("value"):
                continue
            # valeurs lues (CONNU) et tirets visibles (NON_APPLICABLE dessiné) : le
            # modèle doit distinguer un vrai tiret d'un reste de bordure
            visible_dash = f.get("status_on_image") == "NON_APPLICABLE" and str(f["value"]) == "—" and f.get("rendered", True)
            if f.get("status_on_image") != "CONNU" and not visible_dash:
                continue
            box = f.get("cell_px") or f.get("bbox_px")
            if not box:
                continue
            x0, y0, x1, y1 = box
            if f.get("cell_px"):
                p = 2
            else:
                p = pad
            crop = img[max(0, y0 - p): y1 + p, max(0, x0 - p): x1 + p]
            if crop.size == 0 or crop.shape[0] < 8 or crop.shape[1] < 8:
                continue
            label = rendered_label(str(f["value"]), chars) if chars else str(f["value"])
            if label:
                out.append((crop, label, pg["patient_no"]))
        # cellules vides de la page (boîtes du gabarit), étiquette vide
        tfields = (templates.get(pg["page_type"]) or {}).get("fields") or {}
        empty_keys = [k for k, f in pg["fields"].items()
                      if not f.get("pii") and f.get("status_on_image") == "NON_FOURNI" and k in tfields]
        rng.shuffle(empty_keys)
        for k in empty_keys[:empties_per_page]:
            x0, y0, x1, y1 = tfields[k]["bbox_px"]
            crop = img[max(0, y0 - 2): y1 + 2, max(0, x0 - 2): x1 + 2]
            if crop.size and crop.shape[0] >= 8 and crop.shape[1] >= 8:
                out.append((crop, "", pg["patient_no"]))
    return out


class RealDataset(Dataset):
    def __init__(self, items, renderer: Renderer | None = None, augment: bool = True):
        self.items, self.renderer, self.augment = items, renderer, augment

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        img, label, _ = self.items[i]
        if self.augment and self.renderer is not None and self.renderer.rng.random() < 0.7:
            img = self.renderer.degrade(img)
        return preprocess(img), label


class SynthDataset(Dataset):
    """Lot synthétique pré-rendu (uint8 en mémoire) : évite de régénérer à chaque pas sur CPU."""

    def __init__(self, fonts_dir: Path, gt_path: Path | None, n: int = 30000, seed: int = 0, verbose: bool = True):
        rng = random.Random(seed)
        R = Renderer(fonts_dir, rng)
        S = TextSampler(rng, gt_values(gt_path) if gt_path and gt_path.exists() else None)
        self.items: list[tuple[np.ndarray, str]] = []
        t0 = __import__("time").time()
        while len(self.items) < n:
            text = S.sample()
            img, label = R.render(text)
            if not label and text.strip():
                continue            # texte sans glyphe dans cette police : rien à apprendre
            # text vide -> label vide, gardé : le modèle apprend à ne rien lire sur une cellule vide
            x = preprocess(img)
            if x.shape[-1] >= MAX_W:
                continue
            self.items.append((((x[0] + 1) * 127.5).astype(np.uint8), label))
            if verbose and len(self.items) % 5000 == 0:
                print(f"  rendu synthétique : {len(self.items)}/{n} ({__import__('time').time() - t0:.0f}s)", flush=True)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        u8, label = self.items[i]
        x = (u8.astype(np.float32) / 127.5 - 1)[None]
        return x, label


def degraded_crops(gt_path: Path, degraded_dir: Path, fonts_dir: Path, templates_path: Path,
                   empties_per_page: int = 12, seed: int = 0, verbose: bool = True) -> list[tuple[np.ndarray, str, int]]:
    """Recadrages pris sur des photos dégradées (scripts/degrade.py) APRÈS recalage,
    avec les boîtes du gabarit : exactement ce que le lecteur voit en production.

    Étiquettes : valeurs CONNU telles que rendues, tirets visibles, cellules vides.
    """
    from ..geometry import register
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    templates = json.loads(templates_path.read_text(encoding="utf-8"))
    chars_by_font = {}
    fonts = load_fonts(fonts_dir)
    names = [Path(x[0]).name for x in fonts]
    for name, f in FONT_FILES.items():
        if f in names:
            chars_by_font[name] = fonts[names.index(f)][1]
    by_no = {}
    for pg in gt["pages"]:
        m = re.search(r"patientes-(\d\d)", pg["png_file"])
        if m:
            by_no[m.group(1)] = pg
    rng = random.Random(seed)
    out = []
    files = sorted(list(degraded_dir.glob("*.jpg")) + list(degraded_dir.glob("*.png")))
    t0 = __import__("time").time()
    for i, p in enumerate(files):
        m = re.search(r"patientes-(\d\d)", p.name)
        if not m or m.group(1) not in by_no:
            continue
        pg = by_no[m.group(1)]
        img = cv2.imread(str(p))
        if img is None:
            continue
        reg = register(img, pg["page_type"])
        if reg.score < 0.5:
            continue                      # page non recalée : les boîtes ne veulent rien dire
        page = reg.image
        tfields = templates[pg["page_type"]]["fields"]
        chars = chars_by_font.get((pg.get("handwriting_font") or [""])[0])
        empties = []
        for k, f in pg["fields"].items():
            if f.get("pii") or k not in tfields:
                continue
            st = f.get("status_on_image")
            if st == "NON_FOURNI":
                empties.append(k)
                continue
            visible_dash = st == "NON_APPLICABLE" and str(f.get("value")) == "—" and f.get("rendered", True)
            if st != "CONNU" and not visible_dash:
                continue
            x0, y0, x1, y1 = tfields[k]["bbox_px"]
            crop = page[max(0, y0 - 2): y1 + 2, max(0, x0 - 2): x1 + 2]
            if crop.size == 0 or crop.shape[0] < 8 or crop.shape[1] < 8:
                continue
            label = rendered_label(str(f["value"]), chars) if chars else str(f["value"])
            if label:
                out.append((crop, label, pg["patient_no"]))
        rng.shuffle(empties)
        for k in empties[:empties_per_page]:
            x0, y0, x1, y1 = tfields[k]["bbox_px"]
            crop = page[max(0, y0 - 2): y1 + 2, max(0, x0 - 2): x1 + 2]
            if crop.size and crop.shape[0] >= 8 and crop.shape[1] >= 8:
                out.append((crop, "", pg["patient_no"]))
        if verbose and (i + 1) % 40 == 0:
            print(f"  photos dégradées : {i + 1}/{len(files)} ({__import__('time').time() - t0:.0f}s)", flush=True)
    return out
