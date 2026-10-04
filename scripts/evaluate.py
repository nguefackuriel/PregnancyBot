#!/usr/bin/env python3
"""
evaluate.py : harnais d'évaluation « exactitude par champ + statuts + calibration ».

Compare un fichier de prédictions au ground_truth.json produit par
auto_label_pdf.py. Les prédictions ont la même forme minimale :

{
  "pages": [
    {"png_file": "dossiers_specimen_10_patientes-02.png",
     "fields": {"age": {"value": "31", "status": "CONNU", "confidence": 0.97}, ...},
     "choices": {"grossesse_desiree": ["oui"], ...}}
  ]
}

Métriques :
  - exactitude valeur (champs CONNU dans la vérité terrain), après normalisation
  - exactitude statut (tous les champs du schéma), avec matrice de confusion
  - exactitude des groupes de cases (ensemble d'options cochées)
  - ECE (erreur de calibration) sur 10 intervalles
  - ventilation par type de page et par police d'écriture

Usage :
    python evaluate.py ground_truth.json predictions.json [--on-image]
    --on-image : utilise status_on_image (ce que montre réellement le PNG) au
                 lieu du statut voulu par le générateur (« — » invisible).
"""
import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict


def norm_value(v):
    if v is None:
        return ""
    s = unicodedata.normalize("NFKD", str(v))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace(",", ".")
    s = re.sub(r"\s*(g/dl|g/l|mm3|/mm3|cm|kg|g|sa|jours|°c|k)\b", "", s)   # unités
    s = re.sub(r"[^a-z0-9+/.-]+", " ", s).strip()
    s = re.sub(r"\.0$", "", s)
    return s


def ece(conf_correct, bins=10):
    tot = len(conf_correct)
    if not tot:
        return None
    e = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        xs = [(c, ok) for c, ok in conf_correct if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if xs:
            acc = sum(ok for _, ok in xs) / len(xs)
            conf = sum(c for c, _ in xs) / len(xs)
            e += len(xs) / tot * abs(acc - conf)
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ground_truth")
    ap.add_argument("predictions")
    ap.add_argument("--on-image", action="store_true")
    a = ap.parse_args()
    gt = json.load(open(a.ground_truth, encoding="utf-8"))
    pr = json.load(open(a.predictions, encoding="utf-8"))
    # clé = fichier image réel (jeu dégradé : plusieurs variantes par page d'origine),
    # avec repli sur la page d'origine (doublons renommés « __xxxx »)
    pr_pages = {}
    for p in pr["pages"]:
        pr_pages.setdefault(p.get("source_file", p["png_file"]), p)
        pr_pages.setdefault(p["png_file"], p)

    val_ok, val_n = Counter(), Counter()
    st_ok, st_n = Counter(), Counter()
    confusion = Counter()
    grp_ok, grp_n = 0, 0
    cal = []
    status_key = "status_on_image" if a.on_image else "status"

    n_missing = 0
    for g in gt["pages"]:
        p = pr_pages.get(g.get("image_file", g["png_file"]))
        if p is None:
            n_missing += 1
            continue
        fonts = ",".join(g.get("handwriting_font", [])) or "?"
        for k, gf in g["fields"].items():
            if gf.get("pii"):
                continue                      # jamais évalué : doit être masqué, pas lu
            pf = p.get("fields", {}).get(k, {"value": None, "status": "INCONNU"})
            gs = gf[status_key]
            ps = pf.get("status", "INCONNU")
            for dim in ("ALL", g["page_type"], "font:" + fonts):
                st_n[dim] += 1
                st_ok[dim] += gs == ps
            confusion[(gs, ps)] += 1
            if gs == "CONNU":
                ok = norm_value(gf["value"]) == norm_value(pf.get("value"))
                for dim in ("ALL", g["page_type"], "font:" + fonts):
                    val_n[dim] += 1
                    val_ok[dim] += ok
                if pf.get("confidence") is not None:
                    cal.append((float(pf["confidence"]), ok))
        for grp, opts in g.get("choices", {}).items():
            grp_n += 1
            grp_ok += sorted(opts) == sorted(p.get("choices", {}).get(grp, []))

    if n_missing:
        print(f"({n_missing} pages de la vérité terrain sans prédiction : ignorées)")
    type_ok = sum(1 for g in gt["pages"] for p in [pr_pages.get(g.get("image_file", g["png_file"]))] if p and p.get("page_type") == g["page_type"])
    type_n = sum(1 for g in gt["pages"] if pr_pages.get(g.get("image_file", g["png_file"])))
    print(f"Type de page reconnu             : {type_ok}/{type_n} = {type_ok/max(1,type_n):.3f}")
    print(f"Exactitude valeur (champs CONNU) : {val_ok['ALL']}/{val_n['ALL']} = {val_ok['ALL']/max(1,val_n['ALL']):.3f}")
    print(f"Exactitude statut (tous champs)  : {st_ok['ALL']}/{st_n['ALL']} = {st_ok['ALL']/max(1,st_n['ALL']):.3f}")
    print(f"Groupes de cases exacts          : {grp_ok}/{grp_n} = {grp_ok/max(1,grp_n):.3f}")
    e = ece(cal)
    print(f"ECE (calibration)                : {e:.3f}" if e is not None else "ECE : pas de confiance fournie")
    print("\nPar type de page / police :")
    for dim in sorted(d for d in val_n if d != "ALL"):
        print(f"  {dim:34s} valeur {val_ok[dim]/max(1,val_n[dim]):.3f} ({val_n[dim]})   statut {st_ok[dim]/max(1,st_n[dim]):.3f} ({st_n[dim]})")
    print("\nConfusion statuts (vérité -> prédit) :")
    for (gs, ps), n in sorted(confusion.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {gs:15s} -> {ps:15s} {n}")


if __name__ == "__main__":
    main()
