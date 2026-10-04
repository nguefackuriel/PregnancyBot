#!/usr/bin/env python3
"""
build_templates.py : gabarit géométrique de chaque type de page.

Pour chaque type de page (8), on dérive du PDF spécimen :
  - size_px            : taille des PNG (1654 × 2339)
  - lines              : positions médianes des lignes horizontales / verticales
                         (en px) -> servent au recalage d'une photo sur le gabarit
  - fields[key]        : rectangle (px) où la valeur est écrite :
                           * cellule de tableau (grille) pour les tableaux,
                           * zone « libellé → fin du soulignement » pour les champs
                             formulaire (ou bbox médiane des valeurs vues).
  - checkboxes[key]    : rectangle (px) de la case
  - zones              : découpage en zones de lecture pour un lecteur VLM
                         (une zone = une image recadrée + la liste des champs
                          qu'elle contient)
  - title_words        : mots imprimés du titre (classification du type de page)

Usage :
    python scripts/build_templates.py <pdf> --gt data/ground_truth/ground_truth.json \
        --out data/templates.json
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pdfplumber  # noqa: E402

import auto_label_pdf as AL  # noqa: E402

S = AL.PNG_SCALE


def px(b):
    return [int(round(v * S)) for v in b]


def median_box(boxes):
    return [statistics.median(b[i] for b in boxes) for i in range(4)]


def table_cells(p, V, H, printed, page_type):
    """Toutes les cellules (clé -> rect pdf) des tableaux de la page, remplies ou non."""
    out = {}
    if page_type == "GROSSESSE_ACTUELLE":
        raw = sorted(x for x, t, b in V if b - t > 500 and x > 100)      # colonnes du grand tableau
        xs = []
        for x in raw:                                                      # dédoublonner (bord du cadre + ligne)
            if not xs or x - xs[-1] > 2:
                xs.append(round(x, 1))
        # libellés de ligne : mots imprimés dans la 1re colonne
        rows = defaultdict(list)
        for w in printed:
            if 40 < w["x0"] < xs[0] and 130 < w["top"] < 745:
                rows[round(AL.center(w)[1] / 4)].append(w)
        for _, ws in rows.items():
            ws.sort(key=lambda w: w["x0"])
            lab = AL.norm(AL.join(ws))
            r = AL.VISIT_ROWS.get(lab)
            if not r:
                continue
            cy = AL.center(ws[0])[1]
            top = max((y for y, a, b in H if y < cy and a - 6 <= 100 <= b + 6), default=None)
            bottom = min((y for y, a, b in H if y > cy and a - 6 <= 100 <= b + 6), default=None)
            if top is None or bottom is None:
                continue
            cols = ["t1_v1", "t1_v2", "t1_v3", "t2_v1", "t2_v2", "t2_v3", "t3_m7", "t3_m8", "t3_m9"]
            for i, c in enumerate(cols):
                out[f"visites.{c}.{r}"] = [xs[i], top, xs[i + 1], bottom]
    elif page_type == "IDENTIFICATION_ANTECEDENTS":
        def grid(x_range, y_range, row_map, col_map, prefix, rows_from_words=True):
            vx = sorted({round(x, 1) for x, t, b in V if x_range[0] - 3 <= x <= x_range[1] + 3 and t < y_range[1] and b > y_range[0]})
            hy = sorted({round(y, 1) for y, a, b in H if y_range[0] - 3 <= y <= y_range[1] + 3 and a < x_range[1] and b > x_range[0]})
            # en-têtes de colonnes = mots imprimés dans la 1re bande
            hdr_words = [w for w in printed if hy[0] - 2 < AL.center(w)[1] < hy[1] + 2 and x_range[0] < w["x0"] < x_range[1]]
            cols = {}
            for i in range(len(vx) - 1):
                ws = [w for w in hdr_words if vx[i] - 1 <= AL.center(w)[0] <= vx[i + 1] + 1]
                lab = AL.norm(AL.join(sorted(ws, key=lambda w: w["x0"])))
                if lab in col_map:
                    cols[i] = col_map[lab]
            # libellés de ligne = mots imprimés dans la 1re colonne, sous l'en-tête
            for j in range(1, len(hy) - 1):
                ws = [w for w in printed if hy[j] - 2 < AL.center(w)[1] < hy[j + 1] + 2 and vx[0] - 1 <= AL.center(w)[0] <= vx[1] + 1]
                lab = AL.norm(AL.join(sorted(ws, key=lambda w: w["x0"])))
                r = row_map.get(lab)
                if r is None and not rows_from_words:
                    r = ""
                if r is None:
                    continue
                for i, c in cols.items():
                    key = f"{prefix}.{r}.{c}" if r else f"{prefix}.{c}"
                    out[key] = [vx[i], hy[j], vx[i + 1], hy[j + 1]]
        grid((40, 292), (212, 334), AL.ANT_FAM_ROWS, AL.ANT_FAM_COLS, "antecedents_familiaux")
        # antécédents de la femme : 3 colonnes, pas de ligne → une cellule haute par colonne
        vx = sorted({round(x, 1) for x, t, b in V if 309 <= x <= 558 and 210 < t < 220})
        hy = sorted({round(y, 1) for y, a, b in H if 212 <= y <= 338 and a > 300})
        hdr = [w for w in printed if hy[0] < AL.center(w)[1] < hy[1] and w["x0"] > 305]
        for i in range(len(vx) - 1):
            ws = [w for w in hdr if vx[i] <= AL.center(w)[0] <= vx[i + 1]]
            lab = AL.norm(AL.join(ws))
            if lab in AL.ANT_FEMME_COLS:
                out[f"antecedents_femme.{AL.ANT_FEMME_COLS[lab]}"] = [vx[i], hy[1], vx[i + 1], hy[-1]]
        grid((38, 557), (358, 462), AL.ANT_OBS_ROWS, AL.ANT_OBS_COLS, "antecedents_obstetricaux")
        acc_rows = {k: v for k, v in AL.ACC_ROWS.items()}
        acc_cols = {k: str(v) for k, v in AL.ACC_COLS.items()}
        g2 = {}
        # grille accouchements : la 1re ligne d'en-tête est haute (2 bandes) -> on prend les H de 484 à 703
        vx = sorted({round(x, 1) for x, t, b in V if 36 <= x <= 558 and t < 500 and b > 690})
        hy = sorted({round(y, 1) for y, a, b in H if 482 <= y <= 705 and a < 100})
        hdr_words = [w for w in printed if hy[0] < AL.center(w)[1] < hy[1] and w["x0"] > 140]
        cols = {}
        for i in range(len(vx) - 1):
            ws = [w for w in hdr_words if vx[i] <= AL.center(w)[0] <= vx[i + 1]]
            lab = AL.norm(AL.join(sorted(ws, key=lambda w: w["x0"])))
            if lab in acc_cols:
                cols[i] = acc_cols[lab]
        for j in range(1, len(hy) - 1):
            ws = [w for w in printed if hy[j] < AL.center(w)[1] < hy[j + 1] and AL.center(w)[0] < vx[1]]
            lab = AL.norm(AL.join(sorted(ws, key=lambda w: w["x0"])))
            r = acc_rows.get(lab)
            if not r:
                continue
            for i, c in cols.items():
                g2[f"accouchements_anterieurs.{c}.{r}"] = [vx[i], hy[j], vx[i + 1], hy[j + 1]]
        out.update(g2)
    return out


def underline_zone(p, printed, label_words_text, after_x, cy):
    """Zone de valeur = du bout du libellé jusqu'à la fin du soulignement."""
    lines = [l for l in p.lines if abs(l["bottom"] - l["top"]) < 4 and (l["x1"] - l["x0"]) > 15
             and abs((l["top"] + l["bottom"]) / 2 - (cy + 6)) < 10 and l["x0"] >= after_x - 20]
    if not lines:
        return None
    l = min(lines, key=lambda l: l["x0"])
    return [max(after_x, l["x0"] - 2), cy - 9, l["x1"], cy + 7]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--gt", required=True)
    ap.add_argument("--out", default="data/templates.json")
    a = ap.parse_args()
    gt = json.load(open(a.gt, encoding="utf-8"))
    gt_pages = {pg["page_index"]: pg for pg in gt["pages"]}

    acc = defaultdict(lambda: {"fields": defaultdict(list), "cells": defaultdict(list), "checkboxes": defaultdict(list),
                               "hlines": [], "vlines": [], "titles": [], "labels": defaultdict(list)})
    with pdfplumber.open(a.pdf) as pdf:
        AL.build_vocab(pdf)
        for i, p in enumerate(pdf.pages):
            g = gt_pages[i + 1]
            pt = g["page_type"]
            words = p.extract_words(extra_attrs=["fontname"], x_tolerance=1.5)
            printed = [w for w in words if "Helvetica" in w["fontname"]]
            V, H, boxes, marks, bands = AL.page_structure(p)
            A = acc[pt]
            A["hlines"].append(sorted(round(y, 1) for y, _, _ in H))
            A["vlines"].append(sorted(round(x, 1) for x, _, _ in V))
            A["titles"].append(" ".join(w["text"] for w in printed if w["top"] < 60 and w["x0"] < 390))
            for w in printed:
                if w["top"] < 60:
                    continue
                A["labels"][w["text"]].append([w["x0"], w["top"], w["x1"], w["bottom"]])
            for k, rect in table_cells(p, V, H, printed, pt).items():
                A["cells"][k].append(rect)
            for k, f in g["fields"].items():
                if f.get("bbox_pdf"):
                    A["fields"][k].append(f["bbox_pdf"])
            for cb in g["checkboxes"]:
                if cb["key"]:
                    A["checkboxes"][cb["key"]].append(cb["bbox_pdf"])
            # zones de valeur des champs formulaire : libellé -> soulignement
            for k, f in g["fields"].items():
                if f.get("bbox_pdf") and not f.get("cell_pdf") and f.get("raw_label"):
                    b = f["bbox_pdf"]
                    cy = (b[1] + b[3]) / 2
                    z = underline_zone(p, printed, f["raw_label"], b[0] - 4, cy)
                    if z:
                        A["fields"][k + "@zone"].append(z)

    templates = {}
    for pt, A in acc.items():
        T = {"size_px": [1654, 2339], "title": max(set(A["titles"]), key=A["titles"].count)}
        # lignes : regrouper les positions à 3 pt près et garder celles présentes sur >= 70 % des pages
        def consensus(lists):
            allv = sorted(v for L in lists for v in L)
            groups, cur = [], []
            for v in allv:
                if cur and v - cur[-1] > 3:
                    groups.append(cur)
                    cur = []
                cur.append(v)
            if cur:
                groups.append(cur)
            n = len(lists)
            return [round(statistics.median(gp) * S) for gp in groups if len(gp) >= 0.7 * n]
        T["lines"] = {"h": consensus(A["hlines"]), "v": consensus(A["vlines"])}
        fields = {}
        for k, boxes in A["cells"].items():
            fields[k] = {"bbox_px": px(median_box(boxes)), "kind": "cell"}
        for k, boxes in A["fields"].items():
            if k.endswith("@zone"):
                continue
            if k in fields:
                continue
            zone = A["fields"].get(k + "@zone")
            if zone:
                fields[k] = {"bbox_px": px(median_box(zone)), "kind": "underline"}
            else:
                b = median_box(boxes)
                fields[k] = {"bbox_px": px([b[0] - 3, b[1] - 3, max(b[2] + 40, b[0] + 120), b[3] + 3]), "kind": "value_box"}
        T["fields"] = fields
        T["checkboxes"] = {k: px(median_box(v)) for k, v in A["checkboxes"].items()}
        # ancres textuelles : libellés imprimés stables (présents sur toutes les pages)
        n = len(A["titles"])
        T["anchors"] = {t: px(median_box(v)) for t, v in A["labels"].items() if len(v) == n and len(t) >= 4}
        templates[pt] = T

    # zones de lecture
    for pt, T in templates.items():
        zones = []
        if pt == "GROSSESSE_ACTUELLE":
            # en-tête (DDR, taille, DPA...) puis une zone par ligne du tableau (9 cellules)
            head = [k for k in T["fields"] if not k.startswith("visites.")]
            zones.append({"id": "entete", "bbox_px": [0, 140, 1654, 290], "fields": head, "kind": "form"})
            rows = defaultdict(list)
            for k, f in T["fields"].items():
                if k.startswith("visites."):
                    rows[k.split(".")[2]].append(k)
            for r, keys in rows.items():
                bb = [T["fields"][k]["bbox_px"] for k in keys]
                y0 = min(b[1] for b in bb) - 4
                y1 = max(b[3] for b in bb) + 4
                zones.append({"id": f"ligne_{r}", "bbox_px": [100, y0, 1654, y1], "fields": sorted(keys), "kind": "table_row",
                              "row_label_bbox_px": [100, y0, 420, y1]})
        else:
            zones.append({"id": "page", "bbox_px": [0, 0, 1654, 2339], "fields": sorted(T["fields"]), "kind": "form"})
        T["zones"] = zones

    Path(a.out).write_text(json.dumps(templates, ensure_ascii=False, indent=1), encoding="utf-8")
    for pt, T in templates.items():
        print(f"{pt:28s} champs {len(T['fields']):4d}  cases {len(T['checkboxes']):3d}  zones {len(T['zones']):3d}  lignes h/v {len(T['lines']['h'])}/{len(T['lines']['v'])}  ancres {len(T['anchors'])}")


if __name__ == "__main__":
    main()
