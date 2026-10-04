#!/usr/bin/env python3
"""
run_extraction.py : lance le pipeline sur un lot d'images et écrit predictions.json
(format de scripts/evaluate.py), puis affiche les scores si une vérité terrain est donnée.

Exemples :
    python scripts/run_extraction.py --images "../data/Paper Registry" --reader tesseract --out out/pred_tesseract.json
    python scripts/run_extraction.py --images "../data/Paper Registry" --reader anthropic --passes 2 --out out/pred_claude.json
    python scripts/run_extraction.py --images out/degraded --reader anthropic --gt out/degraded/ground_truth.json
    python scripts/run_extraction.py --images "../data/Paper Registry" --reader mock --limit 8
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dayone.extract import extract_page, predictions_entry  # noqa: E402
from dayone.reader.base import get_reader  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True, help="dossier ou motif glob")
    ap.add_argument("--reader", default="crnn", choices=["crnn", "ollama", "tesseract", "anthropic", "mock"])
    ap.add_argument("--passes", type=int, default=1, help="anthropic : 2 = auto-cohérence")
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="regex sur le nom de fichier")
    ap.add_argument("--dedupe", action="store_true", help="ignorer les doublons renommés « __xxxx »")
    ap.add_argument("--out", default="out/predictions.json")
    ap.add_argument("--gt", default=str(Path(__file__).resolve().parents[1] / "data" / "ground_truth" / "ground_truth.json"))
    ap.add_argument("--on-image", action="store_true")
    ap.add_argument("--known-type", action="store_true", help="utiliser le type de page de la vérité terrain (évalue la lecture seule)")
    a = ap.parse_args()

    kw = {}
    if a.reader == "anthropic":
        kw = {"passes": a.passes, "model": a.model}
    elif a.reader == "ollama" and a.model:
        kw = {"model": a.model}
    reader = get_reader(a.reader, **kw)

    p = Path(a.images)
    files = sorted(glob.glob(str(p / "*.png")) + glob.glob(str(p / "*.jpg")) + glob.glob(str(p / "*.jpeg"))) if p.is_dir() else sorted(glob.glob(a.images))
    if a.dedupe:
        # une seule image par page d'origine (les doublons renommés « __xxxx » ont le même contenu)
        seen, kept = set(), []
        for f in files:
            m = re.search(r"patientes-(\d\d)", Path(f).name)
            key = m.group(1) if m and "deg" not in Path(f).name else Path(f).name
            if key not in seen:
                seen.add(key)
                kept.append(f)
        files = kept
    if a.only:
        files = [f for f in files if re.search(a.only, Path(f).name)]
    if a.limit:
        files = files[: a.limit]
    gt = json.load(open(a.gt, encoding="utf-8")) if a.gt and os.path.exists(a.gt) else None
    gt_by_name = {pg["png_file"]: pg for pg in gt["pages"]} if gt else {}

    preds, t0 = [], time.time()
    for i, f in enumerate(files, 1):
        name = Path(f).name
        m = re.search(r"patientes-(\d\d)", name)
        canon = f"dossiers_specimen_10_patientes-{m.group(1)}.png" if m else name
        pt = gt_by_name.get(canon, {}).get("page_type") if a.known_type else None
        ext = extract_page(f, reader, page_type=pt, source_name=name, skip_quality=True)
        e = predictions_entry(ext, canon)
        e["source_file"] = name
        preds.append(e)
        n_known = sum(1 for x in ext.fields.values() if x["status"] == "CONNU")
        print(f"[{i}/{len(files)}] {name:50s} {ext.page_type or '?':26s} connu={n_known:3d} doutes={len(ext.doubts()):3d} "
              f"{ext.timing_s:5.1f}s {ext.error or ''}", flush=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"reader": a.reader, "passes": a.passes, "pages": preds}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n{len(preds)} pages en {time.time() - t0:.0f}s -> {a.out}" + (f" ({reader.calls} appels API)" if hasattr(reader, "calls") else ""))
    if gt:
        import subprocess
        cmd = [sys.executable, str(Path(__file__).parent / "evaluate.py"), a.gt, a.out] + (["--on-image"] if a.on_image else [])
        print(subprocess.run(cmd, capture_output=True, text=True).stdout)


if __name__ == "__main__":
    main()
