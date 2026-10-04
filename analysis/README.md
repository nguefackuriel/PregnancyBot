# Kit d'analyse DayOne : schéma, vérité terrain, évaluation

Dossier généré à côté des données du défi. **Aucun fichier d'origine n'est modifié** (`data/`, `manifest.json`, `consignes-fr-en.pdf` restent intacts).

## Installation

```bash
pip install pdfplumber        # seule dépendance (Python ≥ 3.9)
```

## 1. Régénérer la vérité terrain depuis le PDF

```bash
cd analysis
python tools/auto_label_pdf.py "../data/Paper Registry/dossiers_specimen_10_patientes.pdf" \
    --schema schema/registry_schema.json \
    --out ground_truth/ground_truth.json \
    --pages-dir ground_truth/pages
# -> 80 pages | 2110 champs | 1970 cases (478 cochées) | 0 valeurs non mappées
```

Chaque page produit :

```json
{
  "png_file": "dossiers_specimen_10_patientes-02.png",
  "patient_no": 1, "page_in_booklet": 2, "page_type": "IDENTIFICATION_ANTECEDENTS",
  "handwriting_font": ["Caveat-Regular"],
  "fields": {
    "age": {"value": "31", "status": "CONNU", "rendered": true, "status_on_image": "CONNU",
            "bbox_pdf": [77.0, 81.2, 86.4, 93.0], "bbox_px": [214, 226, 240, 258],
            "raw_label": "age", "raw_col": "", "pii": false},
    "cin": {"value": "CB609814", "status": "CONNU", "pii": true, ...},
    "visites.t1_v2.rendez_vous": {...},
    "accouchements_anterieurs.2.date": {"value": null, "status": "NON_APPLICABLE", "source": "schema", ...}
  },
  "choices": {"grossesse_desiree": ["oui"], "vat": ["1"], "consanguinite": [], ...},
  "checkboxes": [{"key": "vat.1", "group": "vat", "option": "1", "label": "1", "checked": true, "bbox_px": [...]}, ...],
  "tokens": [{"text": "31", "font": "Caveat-Regular", "bbox_px": [...]}, ...]
}
```

- `bbox_px` est dans le repère des PNG fournis (1654×2339).
- `pii: true` = identifiant direct (nom, CIN, adresse, téléphone, mari) : à **masquer**, jamais à stocker. `evaluate.py` ne les évalue pas.
- `rendered: false` = le générateur voulait un tiret mais la police n'a pas le glyphe : la cellule est vide sur l'image. `status` = NON_APPLICABLE (intention), `status_on_image` = NON_FOURNI (ce qu'on voit). Choisir la cible avec `--on-image` dans `evaluate.py`.

## 2. Le schéma

`schema/registry_schema.json` (machine) et `schema/SCHEMA.md` (lisible) : 8 types de page, 436 champs typés (date, int, float, enum, TA, âge gestationnel…), 69 groupes de cases (single / multi / bool), 6 statuts avec leurs règles d'attribution, les codes manuscrits (`NF`, `IG`, le tiret), 13 contrôles de cohérence, le cycle de vie des enregistrements et la règle de liaison patiente. Pour modifier : éditer `schema/build_schema.py` puis `python schema/build_schema.py`.

## 3. Évaluer un pipeline

Produire un `predictions.json` de la forme `{"pages": [{"png_file": ..., "fields": {clé: {"value", "status", "confidence"}}, "choices": {groupe: [options]}}]}` puis :

```bash
python tools/evaluate.py ground_truth/ground_truth.json predictions.json            # cible = intention du générateur
python tools/evaluate.py ground_truth/ground_truth.json predictions.json --on-image # cible = ce que montre le PNG
```

Sortie : exactitude valeur (champs CONNU), exactitude statut, groupes de cases exacts, ECE (calibration), ventilation par type de page et par police, matrice de confusion des statuts.

## 4. Dédupliquer les images (44 doublons renommés)

```bash
cd "data/Paper Registry" && shasum -a 256 *.png *.jpg | sort | awk '{print $1}' | uniq -d | wc -l   # 44
```

Garder un fichier par hash (par ex. le nom sans suffixe `__…`).
