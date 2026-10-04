#!/usr/bin/env python3
"""
degrade.py : fabrique un jeu de test « terrain » à partir des pages synthétiques
propres : perspective (photo de biais), fond sombre autour de la page, ombre
portée, flou, faible lumière / bruit, compression JPEG. La vérité terrain est
transformée avec la même homographie (bbox des champs et des cases).

    python scripts/degrade.py --images "../data/Paper Registry" --gt data/ground_truth/ground_truth.json \
        --out out/degraded --variants 3 --seed 0

Produit out/degraded/<page>__deg<k>.jpg + out/degraded/ground_truth.json
(même format que la vérité terrain d'origine, bbox_px transformées, champ
"degradation" décrivant la variante). Les images d'origine ne sont pas modifiées.
"""
import argparse
import glob
import json
import random
import re
from pathlib import Path

import cv2
import numpy as np

LEVELS = {
    "legere": dict(persp=0.03, blur=(0, 1.2), shadow=0.25, gain=(0.85, 1.05), noise=4, jpeg=(70, 90), tilt=3),
    "moyenne": dict(persp=0.07, blur=(0.8, 2.0), shadow=0.45, gain=(0.6, 0.95), noise=8, jpeg=(45, 70), tilt=6),
    "forte": dict(persp=0.12, blur=(1.5, 3.2), shadow=0.6, gain=(0.45, 0.8), noise=14, jpeg=(30, 50), tilt=10),
}


def degrade(img: np.ndarray, level: str, rng: random.Random):
    P = LEVELS[level]
    h, w = img.shape[:2]
    # 1) fond sombre plus grand que la page, page posée dedans avec perspective
    margin = int(0.12 * w)
    W, H = w + 2 * margin, h + 2 * margin
    bg_col = np.array([rng.randint(20, 60)] * 3) + np.array([rng.randint(0, 15), rng.randint(0, 15), rng.randint(0, 15)])
    canvas = np.full((H, W, 3), bg_col, np.uint8)
    canvas = cv2.GaussianBlur(canvas + rng.randint(0, 10), (0, 0), 3)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    jit = lambda s: rng.uniform(-s, s)
    px, py = P["persp"] * w, P["persp"] * h
    dst = np.float32([[margin + jit(px), margin + jit(py)], [margin + w + jit(px), margin + jit(py)],
                      [margin + w + jit(px), margin + h + jit(py)], [margin + jit(px), margin + h + jit(py)]])
    # inclinaison globale
    ang = np.deg2rad(jit(P["tilt"]))
    c, s = np.cos(ang), np.sin(ang)
    ctr = np.array([W / 2, H / 2])
    dst = ((dst - ctr) @ np.array([[c, -s], [s, c]], dtype=np.float32).T + ctr).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, M, (W, H), borderMode=cv2.BORDER_TRANSPARENT, dst=canvas.copy())
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (W, H))
    out = np.where(mask[..., None] > 0, warped, canvas)
    # 2) éclairage : gradient + ombre portée douce
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    gx, gy = rng.uniform(-1, 1), rng.uniform(-1, 1)
    grad = (gx * (xx / W - 0.5) + gy * (yy / H - 0.5)) * rng.uniform(0.1, 0.35)
    light = rng.uniform(*P["gain"]) + grad
    if rng.random() < 0.8:
        sx, sy = rng.uniform(0, W), rng.uniform(0, H)
        r = rng.uniform(0.25, 0.6) * max(W, H)
        d = np.sqrt((xx - sx) ** 2 + (yy - sy) ** 2)
        shadow = 1 - P["shadow"] * np.clip(1 - d / r, 0, 1)
        light = light * shadow
    out = np.clip(out.astype(np.float32) * light[..., None], 0, 255)
    # 3) flou (mouvement ou gaussien)
    b = rng.uniform(*P["blur"])
    if b > 0.3:
        if rng.random() < 0.4:
            k = max(3, int(b * 3) | 1)
            kern = np.zeros((k, k), np.float32)
            kern[k // 2, :] = 1.0 / k
            ang = rng.uniform(0, 180)
            R = cv2.getRotationMatrix2D((k / 2 - 0.5, k / 2 - 0.5), ang, 1)
            kern = cv2.warpAffine(kern, R, (k, k))
            kern /= kern.sum() + 1e-9
            out = cv2.filter2D(out, -1, kern)
        else:
            out = cv2.GaussianBlur(out, (0, 0), b)
    # 4) bruit de capteur
    out = out + np.random.default_rng(rng.randint(0, 10**9)).normal(0, P["noise"], out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    # 5) résolution façon téléphone (largeur ~ 900-1600) + JPEG
    scale = rng.uniform(900, 1600) / W
    out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    q = rng.randint(*P["jpeg"])
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])
    out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    S = np.array([[scale, 0, 0], [0, scale, 0], [0, 0, 1]], dtype=np.float64)
    return out, S @ M, {"level": level, "jpeg_q": q, "blur": round(b, 2), "scale": round(scale, 3)}


def tx_bbox(M, b):
    if not b:
        return None
    x0, y0, x1, y1 = b
    pts = np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]).reshape(-1, 1, 2)
    q = cv2.perspectiveTransform(pts, M.astype(np.float64)).reshape(-1, 2)
    return [int(q[:, 0].min()), int(q[:, 1].min()), int(q[:, 0].max()), int(q[:, 1].max())]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--out", default="out/degraded")
    ap.add_argument("--variants", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--levels", default="", help="niveaux à produire, séparés par des virgules (défaut : tous)")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    gt = json.load(open(a.gt, encoding="utf-8"))
    by_name = {pg["png_file"]: pg for pg in gt["pages"]}
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(str(Path(a.images) / "dossiers_specimen_10_patientes-*.png")))
    files = [f for f in files if "__" not in Path(f).name]           # pas les doublons
    if a.only:
        files = [f for f in files if re.search(a.only, Path(f).name)]
    pages = []
    levels = [l for l in a.levels.split(",") if l] or list(LEVELS)
    for f in files:
        name = Path(f).name
        g = by_name.get(name)
        if not g:
            continue
        img = cv2.imread(f)
        for k in range(a.variants):
            level = levels[k % len(levels)] if a.variants >= len(levels) else rng.choice(levels)
            out, M, info = degrade(img, level, rng)
            stem = name.replace(".png", f"__deg{k}.jpg")
            cv2.imwrite(str(outdir / stem), out)
            pg = json.loads(json.dumps(g))
            pg["png_file"] = name                 # clé d'évaluation (page d'origine)
            pg["image_file"] = stem
            pg["degradation"] = info
            pg["homography_from_png"] = M.tolist()
            for fld in pg["fields"].values():
                fld["bbox_px"] = tx_bbox(M, fld.get("bbox_px"))
                fld["cell_px"] = tx_bbox(M, fld.get("cell_px"))
            for cb in pg["checkboxes"]:
                cb["bbox_px"] = tx_bbox(M, cb["bbox_px"])
            for t in pg.get("tokens", []):
                t["bbox_px"] = tx_bbox(M, t["bbox_px"])
            pages.append(pg)
            print(f"{stem}  {info}")
    out_gt = {k: v for k, v in gt.items() if k != "pages"}
    out_gt["pages"] = pages
    out_gt["note"] = "variantes dégradées ; png_file = page d'origine (clé d'évaluation), image_file = fichier dégradé"
    (outdir / "ground_truth.json").write_text(json.dumps(out_gt, ensure_ascii=False), encoding="utf-8")
    print(f"{len(pages)} images dégradées -> {outdir}")


if __name__ == "__main__":
    main()
