"""Score honnête sur les vraies photos du carnet (5 pages, annotation partielle à l'œil).

    python scripts/score_real.py --images data/samples --gt data/ground_truth/real_photos_sample.json

Ce qu'on mesure, page par page :
- type de page et gabarit (variant du vrai carnet) reconnus ;
- cases à cocher : cochée / vide, comparé à l'annotation ;
- cellules annotées : la proposition du lecteur est-elle exacte, proche (ressemblance ≥ 0,7),
  ou fausse ; cellules vides reconnues vides ;
- sécurité : aucun champ ne doit sortir CONNU sans la sage-femme sur ce carnet.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.extract import extract_page  # noqa: E402
from dayone.reader.base import get_reader  # noqa: E402


def fold(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.lower().replace(",", ".").split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default=str(ROOT / "data" / "samples"))
    ap.add_argument("--gt", default=str(ROOT / "data" / "ground_truth" / "real_photos_sample.json"))
    ap.add_argument("--reader", default="crnn")
    ap.add_argument("--out", default=str(ROOT / "out" / "pred_real.json"))
    a = ap.parse_args()
    gt = json.loads(Path(a.gt).read_text(encoding="utf-8"))
    reader = get_reader(a.reader)
    tot = {"pages": 0, "type_ok": 0, "variant_ok": 0, "fit": 0, "boxes": 0, "boxes_ok": 0,
           "cells": 0, "exact": 0, "close": 0, "empty": 0, "empty_ok": 0, "connu_alone": 0, "proposed": 0}
    out = {}
    for name, g in gt.items():
        if name.startswith("_"):
            continue
        f = os.path.join(a.images, name)
        if not os.path.exists(f):
            print(f"{name}: absent de {a.images}")
            continue
        e = extract_page(f, reader, source_name=name)
        if e.error:
            print(f"{name}: erreur {e.error}")
            continue
        tot["pages"] += 1
        reg = e.registration or {}
        type_ok = e.page_type == g["page_type"]
        variant_ok = reg.get("variant") == g.get("variant")
        tot["type_ok"] += type_ok
        tot["variant_ok"] += variant_ok
        tot["fit"] += bool(reg.get("template_fit"))
        # cases
        b_ok = b_n = 0
        for grp, want in g.get("choices", {}).items():
            got = sorted(e.choices.get(grp, []))
            b_n += 1
            b_ok += got == sorted(want)
        tot["boxes"] += b_n
        tot["boxes_ok"] += b_ok
        # cellules
        rows = []
        for k, want in g.get("fields", {}).items():
            fld = e.fields.get(k)
            if fld is None:
                rows.append((k, want, "(absent du gabarit)", "absent"))
                continue
            if fld["status"] == "CONNU" and fld.get("source") == "ai":
                tot["connu_alone"] += 1
            raw = fld.get("raw") or ""
            if want == "":
                tot["empty"] += 1
                ok = fld["status"] in ("NON_FOURNI", "NON_APPLICABLE") or not raw.strip()
                tot["empty_ok"] += ok
                rows.append((k, "(vide)", raw or "(vide)", "ok" if ok else "faux"))
                continue
            tot["cells"] += 1
            if raw.strip():
                tot["proposed"] += 1
            w, r = fold(want), fold(fld.get("value") or raw)
            if w == r or (fld.get("value") is not None and fold(fld["value"]) == w):
                tot["exact"] += 1
                verdict = "exact"
            elif difflib.SequenceMatcher(None, w, fold(raw)).ratio() >= 0.7:
                tot["close"] += 1
                verdict = "proche"
            else:
                verdict = "faux"
            rows.append((k, want, raw or "(rien lu)", verdict))
        print(f"\n== {name} : type {'✓' if type_ok else '✗'} {e.page_type} · gabarit {'✓' if variant_ok else '✗'} {reg.get('template')} "
              f"(accord des traits {reg.get('line_score')}) · cases {b_ok}/{b_n}")
        for k, want, raw, verdict in rows:
            print(f"   {verdict:7s} {k:48s} attendu « {want} »  lu « {raw[:30]} »")
        out[name] = {"page_type": e.page_type, "registration": reg, "choices": e.choices,
                     "rows": [{"key": k, "attendu": w, "lu": r, "verdict": v} for k, w, r, v in rows]}
    n = tot["cells"]
    print("\n=== Bilan sur les vraies photos ===")
    print(f"pages : {tot['pages']} · type de page {tot['type_ok']}/{tot['pages']} · gabarit du carnet {tot['variant_ok']}/{tot['pages']} · "
          f"gabarit qui colle {tot['fit']}/{tot['pages']}")
    print(f"groupes de cases : {tot['boxes_ok']}/{tot['boxes']} corrects")
    print(f"cellules écrites annotées : {n} · proposition exacte {tot['exact']} ({100 * tot['exact'] / max(1, n):.0f} %) · "
          f"proche {tot['close']} ({100 * tot['close'] / max(1, n):.0f} %) · fausse {n - tot['exact'] - tot['close']}")
    print(f"cellules vides annotées : {tot['empty']} · reconnues vides {tot['empty_ok']} ({100 * tot['empty_ok'] / max(1, tot['empty']):.0f} %)")
    print(f"champs enregistrés CONNU sans la sage-femme : {tot['connu_alone']} (attendu : 0)")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"totaux": tot, "pages": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("->", a.out)


if __name__ == "__main__":
    main()
