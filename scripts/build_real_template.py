"""
build_real_template.py : gabarit d'une page du VRAI carnet (petit format) à partir d'une photo.

    python scripts/build_real_template.py --photo "../data/Paper Registry/1-4.jpg" \
        --variant GROSSESSE_ACTUELLE@carnet_gauche --out data/templates_carnet.json --debug out/tpl_1-4.jpg

Principe : la photo est redressée, tesseract lit les libellés imprimés, et une
« recette » (l'ordre connu des rangées et des colonnes de cette page) permet
d'aligner les cellules sur les lignes détectées. Le gabarit produit a la même
forme que data/templates.json (champs -> bbox_px dans le cadre 1654 x 2339),
plus une clé "base" : le type de page du schéma qu'il représente. Une page
réelle peut ne porter qu'une partie des champs du type (le tableau de grossesse
est sur deux pages) : les champs absents sont marqués INCONNU à la lecture.

Les recettes sont écrites à la main, une fois par page du carnet. Elles ne
contiennent aucune coordonnée : seulement des libellés et des clés.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.geometry import TPL_H, TPL_W, _line_profile, _profile_peaks, tesseract_config, warp_to_page  # noqa: E402

VISIT_ROWS = [
    ("Rendez vous", "rendez_vous"), ("Venue le", "venue_le"), ("Visites de relance", "visite_relance"),
    ("Age probable de la grossesse", "age_gestationnel"), ("EXAMEN CLINIQUE", None),
    ("Poids", "poids_kg"), ("TA", "ta"), ("Anomalies du squelette", "anomalies_squelette"),
    ("Etat des conjonctives", "conjonctives"), ("Examen des seins", "seins"), ("Oedèmes", "oedemes"),
    ("Mouvements actifs", "mouvements_actifs"), ("HU", "hu_cm"), ("BCF", "bcf"),
    ("Examen au speculum", "speculum"), ("Etat du col", "tv_col"), ("Présentation", "tv_presentation"),
    ("Bassin", "tv_bassin"), ("EXAMEN BIOLOGIQUE", None),
    ("Glucosurie", "glucosurie"), ("Albuminurie", "albuminurie"), ("Rubéole", "rubeole"),
    ("Toxoplasmose", "toxoplasmose"), ("Syphilis", "syphilis"), ("Ag HBs", "ag_hbs"), ("Sérologie VIH", "vih"),
    ("Hémoglobine", "hemoglobine"), ("Plaquettes", "plaquettes"), ("Bilan glycémique", "glycemie"),
    ("RAI", "rai"), ("Autres", "autres_bio"), ("TRAITEMENT", None),
    ("Fer", "fer"), ("Autres à préciser", "traitement_autres"), ("EXAMEN FAIT PAR", "examinateur"),
]

RECIPES = {
    "GROSSESSE_ACTUELLE@carnet_gauche": {
        "base": "GROSSESSE_ACTUELLE",
        "title_words": ["GROSSESSE", "ACTUELLE", "DDR"],
        "inline": [("DDR", "ddr", 360), ("Taille", "taille_cm", 220)],
        "table": {"columns": [("Visite 1", "t1_v1"), ("Visite 2", "t1_v2"), ("Visite 3", "t1_v3")],
                  "rows": VISIT_ROWS, "first_row_label": "Rendez vous"},
    },
    "GROSSESSE_ACTUELLE@carnet_droite": {
        "base": "GROSSESSE_ACTUELLE",
        "title_words": ["DATE", "PREVUE", "ACCOUCHEMENT", "DEPASSEMENT"],
        "inline": [("DATE PREVUE D'ACCOUCHEMENT", "dpa", 420), ("DATE DE DEPASSEMENT DE TERME", "date_depassement_terme", 420)],
        "table": {"columns": [("Visite 1", "t2_v1"), ("Visite 2", "t2_v2"), ("Visite 3", "t2_v3"),
                              ("7ème mois", "t3_m7"), ("8ème mois", "t3_m8"), ("9ème mois", "t3_m9")],
                  "rows": VISIT_ROWS, "first_row_label": None,
                  # pas de colonne de libellés sur cette page : rangées = traits, dans l'ordre de la page gauche
                  "rows_by_lines": True, "header_words": ["Visite", "mois", "trimestre"]},
    },
    "COUVERTURE@carnet_couv": {
        "base": "COUVERTURE",
        "title_words": ["FICHE", "SURVEILLANCE", "GROSSESSE", "POST", "PARTUM"],
        "builder": "cover",
        "inline": [("N° de la fiche", "numero_fiche", 520), ("Région", "region", 540), ("Province", "province", 520),
                   ("Nom de l'établissement sanitaire", "etablissement", 760),
                   ("Nom/Prénom de la parturiente", "nom_prenom_parturiente", 900), ("Autres à préciser", "risque_autres", 560)],
        "checkboxes": [("DR", "type_etablissement.dr"), ("CSC", "type_etablissement.csc"), ("CSU", "type_etablissement.csu"),
                       ("CSCA", "type_etablissement.csca"), ("CSUA", "type_etablissement.csua"),
                       ("Fixe", "mode_couverture.fixe"), ("Mobile", "mode_couverture.mobile"),
                       ("Grossesse classée à risque", "grossesse_a_risque.oui"),
                       ("Anémie", "type_risque.anemie"), ("HTA", "type_risque.hta"), ("Diabète", "type_risque.diabete"),
                       ("Cardiopathie", "type_risque.cardiopathie"), ("Métrorragie", "type_risque.metrorragie"),
                       ("Infection", "type_risque.infection"), ("Pré-éclampsie", "type_risque.pre_eclampsie"),
                       ("Eclampsie", "type_risque.eclampsie")],
    },
    "IDENTIFICATION_ANTECEDENTS@carnet_p2": {
        "base": "IDENTIFICATION_ANTECEDENTS",
        "title_words": ["ANTECEDENTS", "OBSTETRICAUX", "DEROULEMENT", "ACCOUCHEMENTS"],
        "builder": "identification_p2",
    },
    "IDENTIFICATION_ANTECEDENTS@carnet_p1": {
        "base": "IDENTIFICATION_ANTECEDENTS",
        "title_words": ["IDENTIFICATION", "ANTECEDENTS", "HEREDITAIRES"],
        "builder": "identification_p1",
        "inline": [("Age", "age", 300), ("CIN", "cin", 420), ("Niveau d'instruction", "niveau_instruction", 300),
                   ("Profession", "profession", 380), ("Adresse", "adresse", 320), ("Téléphone", "telephone", 360),
                   ("Nom du Mari", "nom_mari", 300), ("Profession", "profession_mari", 380)],
        "checkboxes": [("Consanguinité", "consanguinite.oui"), ("Grossesse désirée", "grossesse_desiree.oui")],
        "tables": [
            {"anchor": "ANTECEDENTS", "columns": [("Famille de la femme", "famille_femme"), ("Mari et famille du mari", "famille_mari")],
             "rows": [("HTA", "hta"), ("Diabète", "diabete"), ("Maladies héréditaires", "maladies_hereditaires"),
                      ("Malformations", "malformations"), ("Allergie(s)", "allergies"), ("Autres à préciser", "autres")],
             "key": "antecedents_familiaux.{row}.{col}"},
            {"anchor": "ANTECEDENTS DE LA FEMME", "columns": [("Médicaux", "medicaux"), ("Chirurgicaux", "chirurgicaux"), ("Gynécologiques", "gynecologiques")],
             "rows": [("", "")], "key": "antecedents_femme.{col}", "single_row": True},
        ],
    },
}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def sim(label: str, text: str) -> float:
    """Ressemblance d'un libellé attendu avec un texte lu (0..1). Le libellé doit être
    contenu, ou presque, dans le texte ; un fragment lu trop court ne compte pas."""
    a, b = fold(label), fold(text)
    if not a or not b:
        return 0.0
    if a in b:
        return 0.9 + 0.1 * len(a) / len(b)
    if b in a and len(b) >= 0.7 * len(a):
        return 0.85
    return difflib.SequenceMatcher(None, a, b).ratio()


def cluster(vals, tol=12):
    out = []
    for x in sorted(vals):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [int(np.mean(c)) for c in out]


def ocr_lines(gray: np.ndarray, scale: float = 2.0, scales=(2.0, 2.5)) -> list[dict]:
    """Lignes de texte imprimé : {text, x0, y0, x1, y1} dans le repère de gray.
    Union de deux segmentations (psm 3 et 11) et de deux agrandissements : l'OCR des
    petits libellés est capricieux, on garde toutes les lectures et on choisit après."""
    import pytesseract
    lines: dict = {}
    for sc in (scales or (scale,)):
        up = cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
        for psm in (3, 11):
            lang, cfg = tesseract_config(f"--psm {psm}")
            d = pytesseract.image_to_data(up, lang=lang, config=cfg, output_type=pytesseract.Output.DICT)
            _collect(d, lines, sc, (psm, sc))
    out = []
    for L in lines.values():
        L["words"].sort()
        out.append({"text": " ".join(t for _, _, t in L["words"]), "x0": int(L["x0"]), "y0": int(L["y0"]), "x1": int(L["x1"]), "y1": int(L["y1"]),
                    "words": [(int(x0), int(x1), t) for x0, x1, t in L["words"]]})
    return sorted(out, key=lambda L: (L["y0"], L["x0"]))


def _collect(d, lines, scale, psm):
    for i, txt in enumerate(d["text"]):
        txt = (txt or "").strip()
        if not txt or int(d["conf"][i]) < 20:
            continue
        key = (psm, d["block_num"][i], d["par_num"][i], d["line_num"][i])
        x, y, w, h = d["left"][i] / scale, d["top"][i] / scale, d["width"][i] / scale, d["height"][i] / scale
        L = lines.setdefault(key, {"words": [], "x0": 1e9, "y0": 1e9, "x1": 0, "y1": 0})
        L["words"].append((x, x + w, txt))
        L["x0"], L["y0"], L["x1"], L["y1"] = min(L["x0"], x), min(L["y0"], y), max(L["x1"], x + w), max(L["y1"], y + h)


def find_label(lines, label, x_max=None, y_min=0, min_sim=0.6):
    best, best_s = None, 0.0
    for L in lines:
        if L["y0"] < y_min or (x_max is not None and L["x0"] > x_max):
            continue
        # le libellé peut être une partie de la ligne OCR (ex. « Taille : /___/ Groupage ... »)
        s = max(sim(label, L["text"]), max((sim(label, " ".join(t for _, _, t in L["words"][i:j]))
                                            for i in range(len(L["words"])) for j in range(i + 1, min(len(L["words"]), i + 5) + 1)), default=0))
        if s > best_s:
            best, best_s = L, s
    return (best, best_s) if best_s >= min_sim else (None, best_s)


def word_right_edge(L, label):
    """x à droite du dernier mot du libellé dans la ligne OCR (pour un champ « libellé : valeur »)."""
    words = L["words"]                      # (x0, x1, texte)
    n = max(1, len(fold(label).split()))
    best, best_s = None, 0.0
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + n + 1) + 1):
            s = sim(label, " ".join(t for _, _, t in words[i:j]))
            if s > best_s:
                best_s, best = s, j - 1
    if best is None:
        return L["x1"], 0.0
    x1 = words[best][1]
    # un « : » collé au libellé
    if best + 1 < len(words) and words[best + 1][2] in (":", ";", "."):
        x1 = words[best + 1][1]
    return int(x1), best_s


def label_x0(L, label):
    """x du premier mot du libellé dans la ligne OCR (la ligne peut contenir d'autres mots)."""
    words = L["words"]
    n = max(1, len(fold(label).split()))
    best, best_s = L["x0"], 0.0
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + n + 1) + 1):
            sc = sim(label, " ".join(t for _, _, t in words[i:j]))
            if sc > best_s:
                best_s, best = sc, words[i][0]
    return int(best)


def band_texts(gray, hs, x0, x1):
    """Texte imprimé de chaque bande entre deux traits horizontaux, dans la colonne [x0, x1]."""
    import pytesseract
    lang, cfg = tesseract_config("--psm 6")
    out = []
    for a, b in zip(hs[:-1], hs[1:]):
        if b - a < 18:
            continue
        cell = gray[a + 2: b - 2, x0 + 2: x1 - 2]
        if cell.size == 0:
            out.append((a, b, "")); continue
        cell = cv2.resize(cell, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        t = pytesseract.image_to_string(cell, lang=lang, config=cfg).strip().replace("\n", " ")
        out.append((a, b, t))
    return out


def text_rows(gray, a, b, x0, x1):
    """Lignes de texte imprimé dans une bande [a, b] : [(y0, y1, texte)] dans le repère de gray."""
    import pytesseract
    lang, cfg = tesseract_config("--psm 6")
    cell = gray[a + 2: b - 2, x0 + 2: x1 - 2]
    if cell.size == 0:
        return []
    up = cv2.resize(cell, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    d = pytesseract.image_to_data(up, lang=lang, config=cfg, output_type=pytesseract.Output.DICT)
    lines: dict = {}
    for i, txt in enumerate(d["text"]):
        txt = (txt or "").strip()
        if not txt or int(d["conf"][i]) < 20 or not re.search(r"[A-Za-zÀ-ÿ]{2,}", txt):
            continue
        k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        L = lines.setdefault(k, {"t": [], "y0": 1e9, "y1": 0})
        L["t"].append((d["left"][i], txt))
        L["y0"] = min(L["y0"], a + 2 + d["top"][i] / 2); L["y1"] = max(L["y1"], a + 2 + (d["top"][i] + d["height"][i]) / 2)
    out = sorted(({"y0": L["y0"], "y1": L["y1"], "t": " ".join(t for _, t in sorted(L["t"]))} for L in lines.values()), key=lambda L: L["y0"])
    # deux lignes très proches = même rangée
    merged = []
    for L in out:
        if merged and L["y0"] - merged[-1][1] < 10:
            merged[-1] = (merged[-1][0], max(merged[-1][1], L["y1"]), merged[-1][2] + " " + L["t"])
        else:
            merged.append((L["y0"], L["y1"], L["t"]))
    return merged


def align(rows, bands):
    """Alignement ordonné (programmation dynamique) des rangées attendues sur les bandes lues.

    rows : [(libellé, clé)] ; bands : [(y0, y1, texte)]. Renvoie {index rangée: index bande}.
    Une rangée peut rester sans bande (saut), une bande sans rangée.
    """
    R, B = len(rows), len(bands)
    gap = 0.25
    S = [[sim(rows[i][0], bands[j][2]) for j in range(B)] for i in range(R)]
    NEG = -1e9
    dp = [[NEG] * (B + 1) for _ in range(R + 1)]
    back = [[None] * (B + 1) for _ in range(R + 1)]
    dp[0][0] = 0.0
    for i in range(R + 1):
        for j in range(B + 1):
            if i == 0 and j == 0:
                continue
            best, arg = NEG, None
            if i > 0 and j > 0:
                v = dp[i - 1][j - 1] + max(S[i - 1][j - 1], 0.12)     # 0.12 : « par ordre » quand rien n'est lu
                if v > best:
                    best, arg = v, "m"
            if i > 0:
                v = dp[i - 1][j] - gap
                if v > best:
                    best, arg = v, "r"
            if j > 0:
                v = dp[i][j - 1] - gap
                if v > best:
                    best, arg = v, "b"
            dp[i][j], back[i][j] = best, arg
    out = {}
    i, j = R, B
    while i > 0 or j > 0:
        a = back[i][j]
        if a == "m":
            out[i - 1] = j - 1; i, j = i - 1, j - 1
        elif a == "r":
            i -= 1
        else:
            j -= 1
    return out


def build_table_by_lines(gray, lines, hs, vs, recipe_table):
    """Page sans colonne de libellés (page droite du tableau) : les rangées sont les bandes
    entre traits sous l'en-tête, dans l'ordre de la recette ; les colonnes sont tous les traits verticaux."""
    rows = recipe_table["rows"]
    heads = [L for L in lines if any(sim(w, L["text"]) >= 0.8 or fold(w) in fold(L["text"]) for w in recipe_table["header_words"])]
    y_head = max((L["y1"] for L in heads), default=0)
    hs_t = [h for h in hs if h > y_head - 4]
    bands = [(a, b) for a, b in zip(hs_t[:-1], hs_t[1:]) if b - a >= 18]
    heights = sorted(b - a for a, b in bands)
    med = heights[len(heights) // 2] if heights else 44
    # trop peu de bandes : on coupe les plus hautes ; trop : on garde les premières
    while len(bands) < len(rows):
        i = max(range(len(bands)), key=lambda k: bands[k][1] - bands[k][0])
        a, b = bands[i]
        if b - a < 1.5 * med:
            break
        bands[i: i + 1] = [(a, (a + b) / 2), ((a + b) / 2, b)]
    bands = bands[: len(rows)]
    cols = recipe_table["columns"]
    bounds = cluster(vs, tol=60)[: len(cols) + 1]
    while len(bounds) < len(cols) + 1:
        bounds.append(min(TPL_W - 2, bounds[-1] + int(np.mean(np.diff(bounds))) if len(bounds) >= 2 else TPL_W - 2))
    fields = {}
    for (lab, key), (y0, y1) in zip(rows, bands):
        if key is None:
            continue
        for c, (_, ckey) in enumerate(cols):
            x0, x1 = int(bounds[c]), int(bounds[c + 1])
            fields[f"visites.{ckey}.{key}"] = {"bbox_px": [x0 + 3, int(y0) + 3, x1 - 3, int(y1) - 3]}
    report = [(lab, f"bande {int(y0)}-{int(y1)}", 1.0) for (lab, _), (y0, y1) in zip(rows, bands)]
    return fields, report, bounds, len(bands)


def build_table(gray, lines, hs, vs, recipe_table, y_start):
    """Rangées = bandes entre traits horizontaux, alignées sur les libellés attendus ;
    colonnes = traits verticaux après la colonne des libellés."""
    if recipe_table.get("rows_by_lines"):
        return build_table_by_lines(gray, lines, hs, vs, recipe_table)
    rows = recipe_table["rows"]
    label_x1 = vs[1] if len(vs) > 1 else int(TPL_W * 0.45)
    label_x0 = vs[0] if vs else 0
    # le trait juste au-dessus de la première rangée en fait partie (le libellé est sous lui)
    above = [h for h in hs if h < y_start - 5]
    hs_t = ([above[-1]] if above and y_start - above[-1] < 60 else []) + [h for h in hs if h >= y_start - 5]
    bands = band_texts(gray, hs_t, label_x0, min(label_x1, label_x0 + 460))
    # bandes trop hautes (traits manqués) : découpées d'après la position de chaque ligne de texte lue
    heights = sorted(b - a for a, b, _ in bands)
    med = heights[len(heights) // 2] if heights else 44
    vb = []
    for a, b, t in bands:
        if (b - a) < 1.6 * med:
            vb.append((a, b, t)); continue
        sub = text_rows(gray, a, b, label_x0, min(label_x1, label_x0 + 460))
        k = max(1, int(np.ceil((b - a) / med - 0.2)))      # rangées un peu plus basses que la médiane
        if len(sub) < max(2, k):
            # moins de lignes de texte lues que de rangées attendues : parts égales
            for m in range(k):
                vb.append((a + (b - a) * m / k, a + (b - a) * (m + 1) / k, t))
            continue
        # bornes = milieux entre lignes de texte consécutives
        ys = [(y0 + y1) / 2 for y0, y1, _ in sub]
        cuts = [a] + [(ys[i] + ys[i + 1]) / 2 for i in range(len(ys) - 1)] + [b]
        for i, (y0, y1, txt) in enumerate(sub):
            vb.append((cuts[i], cuts[i + 1], txt))
    assign = align(rows, vb)
    fields = {}
    cols = recipe_table["columns"]
    vcols = cluster([v for v in vs if v > label_x1 - 5], tol=60)
    bounds = vcols[: len(cols) + 1]
    if len(bounds) < len(cols) + 1:
        # page coupée à droite : la dernière colonne visible va jusqu'au bord, les suivantes sont estimées
        if len(bounds) == len(cols):
            bounds.append(TPL_W - 2)
        else:
            w = int(np.mean(np.diff(bounds))) if len(bounds) >= 2 else int((TPL_W - label_x1) / len(cols))
            if not bounds:
                bounds = [int(label_x1)]
            while len(bounds) < len(cols) + 1:
                bounds.append(min(TPL_W - 2, bounds[-1] + w))
    matched = 0
    for i, (lab, key) in enumerate(rows):
        if i not in assign:
            continue
        y0, y1, t = vb[assign[i]]
        if sim(lab, t) >= 0.5:
            matched += 1
        if key is None:
            continue
        for c, (_, ckey) in enumerate(cols):
            x0, x1 = int(bounds[c]), int(bounds[c + 1])
            fields[f"visites.{ckey}.{key}"] = {"bbox_px": [x0 + 3, int(y0) + 3, x1 - 3, int(y1) - 3]}
    report = [(lab, vb[assign[i]][2][:28], round(sim(lab, vb[assign[i]][2]), 2)) for i, (lab, _) in enumerate(rows) if i in assign]
    return fields, report, bounds, matched


def build_identification_p1(gray, lines, hs, vs, recipe):
    """Page « Identification » du vrai carnet, d'après ses traits :
    5 lignes de formulaire sur 2 colonnes, puis un tableau de 6 rangées x 2 colonnes,
    puis un tableau à 3 colonnes d'une seule grande rangée. L'OCR sert à placer la
    fin de chaque libellé (début de la zone d'écriture)."""
    fields, checkboxes = {}, {}
    # bande d'ancrage du tableau des antécédents familiaux : celle qui contient « ANTÉCÉDENTS »
    A, _ = find_label(lines, "ANTECEDENTS", min_sim=0.7)
    if A is None:
        raise SystemExit("libellé ANTECEDENTS introuvable")
    band_idx = [i for i in range(len(hs) - 1) if hs[i] <= (A["y0"] + A["y1"]) / 2 < hs[i + 1]]
    if not band_idx:
        raise SystemExit("bande du tableau introuvable")
    ia = band_idx[0]
    # colonnes
    mid = min(vs, key=lambda v: abs(v - TPL_W / 2))             # séparation gauche / droite du formulaire
    left, right = min(vs), max(vs)
    # 1. formulaire : les bandes au-dessus de l'ancre (on prend les 5 dernières, de bas en haut)
    form_rows = [("age", "cin"), ("niveau_instruction", "profession"), ("adresse", "telephone"),
                 ("nom_mari", "profession_mari"), ("consanguinite.oui", "grossesse_desiree.oui")]
    labels = {"age": "Age", "cin": "CIN", "niveau_instruction": "Niveau d'instruction", "profession": "Profession",
              "adresse": "Adresse", "telephone": "Téléphone", "nom_mari": "Nom du Mari", "profession_mari": "Profession",
              "consanguinite.oui": "Consanguinité", "grossesse_desiree.oui": "Grossesse désirée"}
    default_label_w = {"age": 110, "cin": 110, "niveau_instruction": 330, "profession": 210, "adresse": 160, "telephone": 220,
                       "nom_mari": 240, "profession_mari": 210, "consanguinite.oui": 240, "grossesse_desiree.oui": 300}
    bands_above = [(hs[i], hs[i + 1]) for i in range(ia) if hs[i + 1] - hs[i] >= 70]   # les bandes fines sont des espaces
    bands_form = bands_above[-5:]
    if len(bands_form) < 5:
        raise SystemExit("moins de 5 lignes de formulaire trouvées")
    used = set()
    for (lk, rk), (y0, y1) in zip(form_rows, bands_form):
        for key, x0c, x1c in ((lk, left, mid), (rk, mid, right)):
            lab = labels[key]
            cands = [L for L in lines if y0 - 5 <= (L["y0"] + L["y1"]) / 2 <= y1 + 5 and x0c - 10 <= L["x0"] <= x1c and id(L) not in used]
            L, sc = find_label(cands, lab, min_sim=0.7)
            if L is not None:
                used.add(id(L))
                x, _ = word_right_edge(L, lab)
            else:
                x = x0c + default_label_w[key]
            if key.endswith(".oui"):
                # la case est un petit carré à droite du libellé, après les deux points
                checkboxes[key] = {"bbox_px": [int(x) + 36, int(y0) + 14, int(x) + 100, int(y1) - 14]}
            else:
                fields[key] = {"bbox_px": [int(x) + 6, int(y0) + 6, int(x1c) - 8, int(y1) - 6]}
    # 2. tableau des antécédents familiaux : 6 bandes après l'ancre, colonnes = traits après la colonne des libellés
    # Les traits verticaux sont cherchés dans la hauteur du tableau seulement : le trait qui sépare
    # les deux colonnes du formulaire (au-dessus) ne doit pas être pris pour une colonne du tableau.
    rows_t1 = ["hta", "diabete", "maladies_hereditaires", "malformations", "allergies", "autres"]
    bands_t1 = [(hs[i], hs[i + 1]) for i in range(ia + 1, min(ia + 1 + len(rows_t1), len(hs) - 1))]
    yt0, yt1 = int(hs[ia]), int(bands_t1[-1][1]) if bands_t1 else int(hs[min(ia + 7, len(hs) - 1)])
    vt = cluster(_profile_peaks(_line_profile(gray[yt0:yt1], 1)), tol=40)
    c_bounds = []
    for head in ("Famille de la femme", "Mari et famille du mari"):
        Hh, _ = find_label(lines, head, min_sim=0.7)
        if Hh is not None:
            hx = label_x0(Hh, head)
            cand = [v for v in vt if v < hx + 30]                 # le trait est au bord gauche de l'en-tête
            if cand:
                c_bounds.append(max(cand))
    if len(c_bounds) != 2 or c_bounds[1] - c_bounds[0] < 150:
        c_bounds = [left + (right - left) * 0.37, left + (right - left) * 0.68]
    c_bounds = sorted(set(int(c) for c in c_bounds)) + [int(right)]
    for rk, (y0, y1) in zip(rows_t1, bands_t1):
        for ck, (x0, x1) in zip(("famille_femme", "famille_mari"), zip(c_bounds[:-1], c_bounds[1:])):
            fields[f"antecedents_familiaux.{rk}.{ck}"] = {"bbox_px": [int(x0) + 4, int(y0) + 4, int(x1) - 4, int(y1) - 4]}
    # 3. antécédents de la femme : après le tableau 1, une bande de titre, une bande d'en-tête, puis la grande zone
    i2 = ia + 1 + len(rows_t1)
    y_head_bottom = hs[i2 + 3] if i2 + 3 < len(hs) else hs[-1]
    # plus sûr : le premier trait sous l'en-tête « Médicaux / Chirurgicaux / Gynécologiques »
    Hm, _ = find_label(lines, "Chirurgicaux", min_sim=0.7)
    if Hm is not None:
        below = [h for h in hs if h > Hm["y1"] - 4]
        if below:
            y_head_bottom = below[0]
    y_bottom = hs[-1]
    v2 = cluster(_profile_peaks(_line_profile(gray[int(y_head_bottom):int(y_bottom)], 1)), tol=40)
    c3 = sorted(set([int(left), int(right)] + [int(v) for v in v2 if left + 100 < v < right - 100]))
    # 3 colonnes : traits les plus proches des tiers
    thirds = [left + (right - left) * k / 3 for k in range(4)]
    c3b = [min(c3, key=lambda v: abs(v - t)) for t in thirds]
    if len(set(c3b)) < 4:
        c3b = [int(t) for t in thirds]
    for ck, (x0, x1) in zip(("medicaux", "chirurgicaux", "gynecologiques"), zip(c3b[:-1], c3b[1:])):
        fields[f"antecedents_femme.{ck}"] = {"bbox_px": [int(x0) + 6, int(y_head_bottom) + 6, int(x1) - 6, int(y_bottom) - 6]}
    print(f"  formulaire : 5 lignes ; antécédents familiaux : {len(bands_t1)} rangées, colonnes {[int(c) for c in c_bounds]} ; "
          f"antécédents de la femme : colonnes {[int(c) for c in c3b]}, y {int(y_head_bottom)}-{int(y_bottom)}")
    return fields, checkboxes


def h_extent(gray, y, pad=4):
    """Étendue (x0, x1) du trait horizontal passant à la hauteur y (bords d'un tableau)."""
    blur = cv2.GaussianBlur(gray, (0, 0), 15).astype(np.int16)
    thr = np.maximum(8, (0.10 * blur).astype(np.int16))
    dark = ((gray.astype(np.int16) < blur - thr) & (gray < 190)).astype(np.uint8) * 255
    opened = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (61, 1)))
    band = opened[max(0, int(y) - pad): int(y) + pad + 1]
    cols = np.where(band.any(axis=0))[0]
    if len(cols) == 0:
        return None
    return int(cols.min()), int(cols.max())


def table_extent(gray, hs, y0, y1):
    """Bords gauche et droit d'un tableau : médiane des étendues de ses traits horizontaux."""
    exts = [h_extent(gray, h) for h in hs if y0 - 3 <= h <= y1 + 3]
    exts = [e for e in exts if e is not None and e[1] - e[0] > 200]
    if not exts:
        return None
    return int(np.median([e[0] for e in exts])), int(np.median([e[1] for e in exts]))


def table_columns(gray, y0, y1, heads, lines, hs, left=None, right=None):
    """Bornes de colonnes d'un tableau : traits verticaux dans sa hauteur, un par en-tête lu
    (le trait juste à gauche du mot), puis le bord droit. `heads` = libellés des colonnes."""
    vt = cluster(_profile_peaks(_line_profile(gray[int(y0):int(y1)], 1)), tol=30)
    ext = table_extent(gray, hs, y0, y1)
    if left is None:
        left = ext[0] if ext else (min(vt) if vt else 0)
    if right is None:
        right = ext[1] if ext else (max(vt) if vt else TPL_W)
    bounds = [int(left)]
    for head in heads[1:]:
        found, _ = find_word(lines, head, min_sim=0.7)
        if found is None:
            bounds.append(None)
            continue
        L, wx0, _ = found
        if not (y0 - 60 <= L["y0"] <= y1):
            bounds.append(None)
            continue
        cand = [v for v in vt if wx0 - 120 <= v < wx0 + 20]
        bounds.append(max(cand) if cand else int(wx0 - 20))
    bounds.append(int(right))
    # colonnes sans en-tête lu : interpolation entre les voisines connues
    known = [(i, b) for i, b in enumerate(bounds) if b is not None]
    for i, b in enumerate(bounds):
        if b is None:
            prev = max((k, v) for k, v in known if k < i)
            nxt = min((k, v) for k, v in known if k > i)
            bounds[i] = int(prev[1] + (nxt[1] - prev[1]) * (i - prev[0]) / (nxt[0] - prev[0]))
    return bounds


def build_identification_p2(gray, lines, hs, vs, recipe):
    """Deuxième page d'identification du vrai carnet : tableau des antécédents obstétricaux
    (4 rangées x 4 colonnes de valeurs), tableau des accouchements antérieurs (6 rangées x 5),
    puis gestation / parité / enfants vivants, VAT (5 cases), vaccins et frottis."""
    fields, checkboxes = {}, {}
    # ---- tableau A : antécédents obstétricaux ----
    Ht, _ = find_label(lines, "Nombre", min_sim=0.8)
    if Ht is None:
        raise SystemExit("en-tête « Nombre » introuvable")
    ia = [i for i in range(len(hs) - 1) if hs[i] <= (Ht["y0"] + Ht["y1"]) / 2 < hs[i + 1]]
    if not ia:
        raise SystemExit("bande d'en-tête du tableau A introuvable")
    ia = ia[0]
    rows_a = ["avortement", "accouchement_premature", "mort_foetale_in_utero", "autres"]
    bands_a = [(hs[i], hs[i + 1]) for i in range(ia + 1, min(ia + 1 + len(rows_a), len(hs) - 1))]
    cols_a = table_columns(gray, hs[ia], bands_a[-1][1], ["Nature", "Nombre", "Date", "Lieu", "Age gestationnel"], lines, hs)
    for rk, (y0, y1) in zip(rows_a, bands_a):
        for ck, (x0, x1) in zip(("nombre", "date", "lieu", "age_gestationnel_sa"), zip(cols_a[1:-1], cols_a[2:])):
            fields[f"antecedents_obstetricaux.{rk}.{ck}"] = {"bbox_px": [int(x0) + 4, int(y0) + 4, int(x1) - 4, int(y1) - 4]}
    # ---- tableau B : accouchements antérieurs ----
    Tb, _ = find_label(lines, "DEROULEMENT DES ACCOUCHEMENTS", min_sim=0.7)
    if Tb is None:
        raise SystemExit("titre « Déroulement des accouchements antérieurs » introuvable")
    ib = [i for i in range(len(hs) - 1) if hs[i] <= (Tb["y0"] + Tb["y1"]) / 2 < hs[i + 1]]
    ib = ib[0] + 1 if ib else None                        # bande d'en-tête = celle qui suit le titre
    if ib is None:
        raise SystemExit("bande d'en-tête du tableau B introuvable")
    rows_b = ["date", "modalite", "indication_cesarienne", "complication", "poids_nn_g", "complication_nn"]
    bands_b = [(hs[i], hs[i + 1]) for i in range(ib + 1, min(ib + 1 + len(rows_b), len(hs) - 1))]
    if len(bands_b) < len(rows_b):
        raise SystemExit(f"tableau B : {len(bands_b)} rangées trouvées sur {len(rows_b)}")
    yb0, yb1 = hs[ib], bands_b[-1][1]
    vt = cluster(_profile_peaks(_line_profile(gray[int(yb0):int(yb1)], 1)), tol=30)
    ext = table_extent(gray, hs, yb0, yb1)
    left_b = ext[0] if ext else min(vt)
    right_b = ext[1] if ext else max(vt)
    print(f"  tableau B : traits verticaux {vt}, bords {left_b}-{right_b}")
    inner = [v for v in vt if left_b + 60 < v < right_b - 60]
    if len(inner) >= 5:
        # 5 séparateurs : colonne des libellés puis 5 accouchements ; on prend les plus réguliers
        cols_b = [int(left_b)] + [int(v) for v in inner[:5]] + [int(right_b)]
    else:
        # il en manque : la première (libellés) vient des traits, les autres à pas régulier
        first = inner[0] if inner else int(left_b + (right_b - left_b) * 0.33)
        step = (right_b - first) / 5
        cols_b = [int(left_b), int(first)] + [int(first + step * k) for k in range(1, 5)] + [int(right_b)]
    for rk, (y0, y1) in zip(rows_b, bands_b):
        for n, (x0, x1) in enumerate(zip(cols_b[1:-1], cols_b[2:]), start=1):
            fields[f"accouchements_anterieurs.{n}.{rk}"] = {"bbox_px": [int(x0) + 4, int(y0) + 4, int(x1) - 4, int(y1) - 4]}
    # ---- gestation / parité / enfants vivants : « Gestation: /_/ Parité /_/ Nombre d'enfants vivants /_/ » ----
    G_, _ = find_word(lines, "Gestation", min_sim=0.8)
    Pa, _ = find_word(lines, "Parité", min_sim=0.8)
    Nv, _ = find_word(lines, "vivants", min_sim=0.8)
    if G_ is not None:
        L, gx0, gx1 = G_
        yy0, yy1 = int(L["y0"]) - 34, int(L["y1"]) + 14
        px0 = Pa[1] if Pa else gx1 + 180
        fields["gestation"] = {"bbox_px": [gx1 + 10, yy0, int(px0) - 8, yy1]}
        if Pa is not None:
            nx0 = find_word(lines, "Nombre", min_sim=0.8)[0]
            nx0 = nx0[1] if nx0 and abs(nx0[0]["y0"] - L["y0"]) < 60 else Pa[2] + 200
            fields["parite"] = {"bbox_px": [Pa[2] + 10, yy0, int(nx0) - 8, yy1]}
        if Nv is not None:
            fields["enfants_vivants"] = {"bbox_px": [Nv[2] + 6, yy0, min(TPL_W - 40, Nv[2] + 180), yy1]}
    # ---- VAT : 5 cases sur la ligne « VAT : 1 [] 2 [] ... » ----
    V_, _ = find_word(lines, "VAT", min_sim=0.7)
    if V_ is not None:
        L, vx0, vx1 = V_
        squares = []
        x = vx0 + 90                      # après « VAT : 1 » (l'OCR englobe parfois toute la ligne)
        while len(squares) < 5:
            sq = find_square(gray, L["y0"] - 16, L["y1"] + 16, x + 2, min(TPL_W, x + 400))
            if sq is None:
                break
            squares.append(sq)
            x = sq[2]
        for n, sq in enumerate(squares, start=1):
            checkboxes[f"vat.{n}"] = {"bbox_px": sq}
        if len(squares) < 5:
            print(f"  VAT : {len(squares)} cases vues sur 5")
    # ---- vaccins : case à droite du libellé, puis date après « Le » ----
    for lab, ck, dk in (("Vaccinée contre la rubéole", "vaccin_rubeole.oui", "date_vaccin_rubeole"),
                        ("Vaccinée contre l'hépatite B", "vaccin_hepatite_b.oui", "date_vaccin_hepatite_b")):
        Lb, _ = find_label(lines, lab, min_sim=0.8)
        if Lb is None:
            print("  libellé non trouvé :", lab)
            continue
        x, _ = word_right_edge(Lb, lab)
        sq = find_square(gray, Lb["y0"] - 16, Lb["y1"] + 16, x + 2, x + 400)
        if sq is not None:
            checkboxes[ck] = {"bbox_px": sq}
            xd = sq[2]
        else:
            print(f"  case « {lab} » non vue")
            xd = x + 80
        Le = [l for l in lines if abs(l["y0"] - Lb["y0"]) < 40 and l["x0"] > xd]
        le_x = None
        for l in Le:
            for wx0, wx1, t in l["words"]:
                if fold(t) == "le" and wx0 > xd:
                    le_x = wx1
                    break
            if le_x:
                break
        xs = (le_x + 6) if le_x else xd + 150
        fields[dk] = {"bbox_px": [int(xs), int(Lb["y0"]) - 14, min(TPL_W - 40, int(xs) + 560), int(Lb["y1"]) + 14]}
    # ---- frottis cervical ----
    Fr, _ = find_label(lines, "Frottis cervical", min_sim=0.7)
    if Fr is not None:
        x = max(wx1 for wx0, wx1, t in Fr["words"] if ")" in t or ":" in t) if any(")" in t or ":" in t for _, _, t in Fr["words"]) else Fr["x1"]
        fields["frottis_iva"] = {"bbox_px": [int(x) + 8, int(Fr["y0"]) - 14, int(x) + 200, int(Fr["y1"]) + 14]}
    print(f"  tableau A : {len(bands_a)} rangées, colonnes {cols_a} ; tableau B : {len(bands_b)} rangées, colonnes {cols_b} ; "
          f"{len(fields)} champs, {len(checkboxes)} cases")
    return fields, checkboxes


def find_word(lines, label, min_sim=0.75):
    """Ligne OCR et bornes (x0, x1) du mot (ou groupe de mots) le plus proche du libellé.
    Un mot exact l'emporte sur une ressemblance : « CSC » ne doit pas prendre « CSCA »."""
    target = fold(label)
    n = max(1, len(target.split()))
    best, best_s = None, 0.0
    for L in lines:
        words = L["words"]
        for i in range(len(words)):
            for j in range(i + 1, min(len(words), i + n + 1) + 1):
                t = fold(" ".join(w for _, _, w in words[i:j]))
                s = 1.0 if t == target else difflib.SequenceMatcher(None, target, t).ratio()
                if s > best_s:
                    best_s, best = s, (L, int(words[i][0]), int(words[j - 1][1]))
    return (best, best_s) if best_s >= min_sim else (None, best_s)


def find_square(gray, y0, y1, x_from, x_to, smin=16, smax=70):
    """Premier petit carré (case à cocher) à droite de x_from dans la bande [y0, y1]."""
    H_, W_ = gray.shape[:2]
    y0, y1, x_from, x_to = max(0, int(y0)), min(H_, int(y1)), max(0, int(x_from)), min(W_, int(x_to))
    strip = gray[y0:y1, x_from:x_to]
    if strip.size == 0 or strip.shape[0] < smin or strip.shape[1] < smin:
        return None
    bw = cv2.adaptiveThreshold(strip, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cands = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if smin <= w <= smax and smin <= h <= smax and 0.7 <= w / h <= 1.4:
            # un carré : son contour fait à peu près le tour de son rectangle englobant
            if cv2.arcLength(c, True) >= 0.7 * 2 * (w + h):
                cands.append((x, y, w, h))
    if not cands:
        return None
    x, y, w, h = min(cands, key=lambda b: b[0])
    return [x_from + x, y0 + y, x_from + x + w, y0 + y + h]


def build_cover(gray, lines, hs, vs, recipe):
    """Couverture du vrai carnet : champs « libellé : valeur » sur une ligne, et cases à cocher
    placées à DROITE de leur libellé (le spécimen les a à gauche). Chaque case est trouvée
    comme un petit carré dans la bande du libellé ; sinon, position par défaut."""
    fields, checkboxes = {}, {}
    used = set()
    for lab, key, width in recipe.get("inline", []):
        L, s = find_label([l for l in lines if id(l) not in used], lab, min_sim=0.8)
        if L is None and key == "risque_autres":
            # pointillés souvent non lus : la ligne juste sous « Eclampsie », même colonne
            found, _ = find_word(lines, "Eclampsie")
            if found is not None:
                E, ex0, _ = found
                fields[key] = {"bbox_px": [ex0 + 270, int(E["y1"]) + 4, min(TPL_W - 40, ex0 + 270 + width), int(E["y1"]) + 48]}
                print(f"  {key:24s} <- sous « Eclampsie » (libellé non lu)")
                continue
        if L is None:
            print(f"  libellé non trouvé : {lab}")
            continue
        used.add(id(L))
        x, _ = word_right_edge(L, lab)
        fields[key] = {"bbox_px": [x + 6, int(L["y0"]) - 22, min(TPL_W - 40, x + width), int(L["y1"]) + 14]}
        print(f"  {key:24s} <- « {L['text'][:40]} » (sim {s:.2f})")
    missing = []
    for lab, key in recipe.get("checkboxes", []):
        found, s = find_word(lines, lab)
        if found is None:
            missing.append(lab)
            continue
        L, wx0, wx1 = found
        sq = find_square(gray, L["y0"] - 16, L["y1"] + 16, wx1 + 4, wx1 + 330)
        if sq is None:
            sq = [wx1 + 40, int((L["y0"] + L["y1"]) / 2) - 16, wx1 + 72, int((L["y0"] + L["y1"]) / 2) + 16]
            print(f"  case « {lab} » : carré non vu, position par défaut")
        checkboxes[key] = {"bbox_px": [int(v) for v in sq]}
    if missing:
        print("  cases sans libellé lu :", ", ".join(missing))
    print(f"  couverture : {len(fields)} champs, {len(checkboxes)} cases")
    return fields, checkboxes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", required=True)
    ap.add_argument("--variant", required=True, choices=sorted(RECIPES))
    ap.add_argument("--out", default=str(ROOT / "data" / "templates_carnet.json"))
    ap.add_argument("--debug", default="")
    a = ap.parse_args()
    recipe = RECIPES[a.variant]
    img = cv2.imread(a.photo)
    warped, H, method = warp_to_page(img, level=True)
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    hs = cluster(_profile_peaks(_line_profile(gray, 0)))
    vs = cluster(_profile_peaks(_line_profile(gray, 1)))
    lines = ocr_lines(gray)
    print(f"{len(lines)} lignes de texte lues, {len(hs)} traits horizontaux, {len(vs)} verticaux ({method})")
    fields, pii_zones = {}, []
    checkboxes = {}
    if recipe.get("builder") == "identification_p1":
        fields, checkboxes = build_identification_p1(gray, lines, hs, vs, recipe)
    elif recipe.get("builder") == "cover":
        fields, checkboxes = build_cover(gray, lines, hs, vs, recipe)
    elif recipe.get("builder") == "identification_p2":
        fields, checkboxes = build_identification_p2(gray, lines, hs, vs, recipe)
    # champs en ligne « libellé : valeur »
    used = set()
    for lab, key, width in (recipe.get("inline", []) if not recipe.get("builder") else []):
        # « Taille » est court et ressemble à « ACTUELLE » : on exige une lecture plus nette
        L, s = find_label([l for l in lines if id(l) not in used], lab, min_sim=0.75 if key == "taille_cm" else 0.6)
        if L is None and key == "taille_cm" and "ddr" in fields:
            # « Taille : /___/ » est la ligne sous « DDR », à la marge gauche ; l'OCR la rate souvent
            T, _ = find_label(lines, "GROSSESSE ACTUELLE", min_sim=0.7)
            x0 = int(T["x0"]) + 80 if T else 150
            y1 = fields["ddr"]["bbox_px"][3]
            fields[key] = {"bbox_px": [x0, y1 + 50, x0 + 160, y1 + 100]}
            print(f"  {key:22s} <- sous « DDR » (libellé non lu)")
            continue
        if L is None:
            print(f"  libellé non trouvé : {lab}")
            continue
        used.add(id(L))
        x, _ = word_right_edge(L, lab)
        y0, y1 = L["y0"] - 10, L["y1"] + 14
        fields[key] = {"bbox_px": [x + 4, int(y0), min(TPL_W - 4, x + width), int(y1)]}
        print(f"  {key:22s} <- « {L['text'][:40]} » (sim {s:.2f})")
    # tableau des visites
    if "table" in recipe:
        y_start = 0
        if recipe["table"].get("first_row_label"):
            L, _ = find_label(lines, recipe["table"]["first_row_label"], min_sim=0.6)
            if L is not None:
                y_start = L["y0"] - 5
            else:
                # première rangée non lue : juste sous l'en-tête de la première colonne (« Visite 1 »)
                Hd, _ = find_label(lines, recipe["table"]["columns"][0][0], min_sim=0.6)
                y_start = Hd["y1"] + 2 if Hd else 0
                print("  première rangée non lue, départ sous l'en-tête :", y_start)
        tf, report, bounds, matched = build_table(gray, lines, hs, vs, recipe["table"], y_start)
        fields.update(tf)
        print(f"  tableau : {matched} libellés reconnus sur {len(recipe['table']['rows'])} rangées, colonnes {bounds}, {len(tf)} cellules")
        for lab, t, sc in report:
            print(f"      {lab:30s} <- « {t} » {sc}")
    # tableaux de l'identification (petits)
    for tb in (recipe.get("tables", []) if not recipe.get("builder") else []):
        L, _ = find_label(lines, tb["anchor"], min_sim=0.6)
        if L is None:
            print("  tableau non trouvé :", tb["anchor"])
            continue
        y_anchor = L["y1"]
        # en-têtes de colonnes
        xs = []
        for lab, _ in tb["columns"]:
            C, s = find_label(lines, lab, y_min=y_anchor - 60, min_sim=0.6)
            xs.append(((C["x0"] + C["x1"]) / 2, C["y1"]) if C else (None, None))
        if any(x is None for x, _ in xs):
            print("  en-têtes incomplets pour", tb["anchor"], xs)
            continue
        # bornes de colonnes : traits verticaux entre les centres d'en-têtes, sinon milieux
        centers_x = [x for x, _ in xs]
        bounds = []
        for i in range(len(centers_x) + 1):
            if i == 0:
                cand = [v for v in vs if v < centers_x[0]]
                bounds.append(max(cand) if cand else int(centers_x[0] - 150))
            elif i == len(centers_x):
                cand = [v for v in vs if v > centers_x[-1]]
                bounds.append(min(cand) if cand else TPL_W - 40)
            else:
                mid = (centers_x[i - 1] + centers_x[i]) / 2
                cand = [v for v in vs if centers_x[i - 1] < v < centers_x[i]]
                bounds.append(min(cand, key=lambda v: abs(v - mid)) if cand else int(mid))
        y_head = max(y for _, y in xs)
        if tb.get("single_row"):
            below = [h for h in hs if h > y_head + 40]
            y0 = min([h for h in hs if h > y_head] or [y_head + 10])
            y1 = below[1] if len(below) > 1 else min(TPL_H - 40, y0 + 420)
            # la zone s'étend jusqu'au prochain grand trait ou 420 px
            for c, (_, ckey) in enumerate(tb["columns"]):
                fields[tb["key"].format(col=ckey)] = {"bbox_px": [int(bounds[c]) + 4, int(y0) + 4, int(bounds[c + 1]) - 4, int(y1) - 4]}
            continue
        # rangées par libellé
        y_min = y_head
        row_y = []
        for lab, rkey in tb["rows"]:
            R, s = find_label(lines, lab, x_max=bounds[0] + 20, y_min=y_min, min_sim=0.7)
            if R is None:
                row_y.append((rkey, None)); continue
            row_y.append((rkey, (R["y0"] + R["y1"]) / 2)); y_min = R["y1"]
        ys = [y for _, y in row_y if y is not None]
        for i, (rkey, yc) in enumerate(row_y):
            if yc is None:
                continue
            prev_y = max([y for _, y in row_y[:i] if y is not None] or [y_head])
            next_y = min([y for _, y in row_y[i + 1:] if y is not None] or [yc + (yc - prev_y)])
            top = min([h for h in hs if prev_y < h < yc] or [(prev_y + yc) / 2], key=lambda h: abs(h - (prev_y + yc) / 2))
            bot = min([h for h in hs if yc < h < next_y] or [(yc + next_y) / 2], key=lambda h: abs(h - (yc + next_y) / 2))
            for c, (_, ckey) in enumerate(tb["columns"]):
                fields[tb["key"].format(row=rkey, col=ckey)] = {"bbox_px": [int(bounds[c]) + 4, int(top) + 3, int(bounds[c + 1]) - 4, int(bot) - 3]}
        print(f"  tableau « {tb['anchor']} » : {len(ys)} rangées, colonnes {[int(b) for b in bounds]}")
    # cases à cocher : petit carré à droite du libellé
    for lab, ckey in (recipe.get("checkboxes", []) if not recipe.get("builder") else []):
        L, s = find_label(lines, lab, min_sim=0.6)
        if L is None:
            continue
        x, _ = word_right_edge(L, lab)
        checkboxes[ckey] = {"bbox_px": [x + 10, L["y0"] - 6, x + 60, L["y1"] + 6]}
    entry = {"base": recipe["base"], "variant": a.variant, "size_px": [TPL_W, TPL_H], "title_words": recipe["title_words"],
             "lines": {"h": [int(x) for x in hs], "v": [int(x) for x in vs]}, "fields": fields,
             # même forme que templates.json : case -> [x0, y0, x1, y1]
             "checkboxes": {k: (v["bbox_px"] if isinstance(v, dict) else v) for k, v in checkboxes.items()},
             "source_photo": Path(a.photo).name}
    out = Path(a.out)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    data[a.variant] = entry
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(fields)} champs, {len(checkboxes)} cases -> {out} [{a.variant}]")
    if a.debug:
        vis = warped.copy()
        for k, f in fields.items():
            x0, y0, x1, y1 = f["bbox_px"]
            cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 0, 255), 2)
        for k, f in checkboxes.items():
            x0, y0, x1, y1 = f["bbox_px"]
            cv2.rectangle(vis, (x0, y0), (x1, y1), (255, 0, 0), 2)
        Path(a.debug).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(a.debug, vis)


if __name__ == "__main__":
    main()
