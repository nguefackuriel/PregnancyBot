#!/usr/bin/env python3
"""
auto_label_pdf.py : vérité terrain automatique à partir du PDF spécimen DayOne.

Le PDF `dossiers_specimen_10_patientes.pdf` (ReportLab) garde sa couche
vectorielle : les valeurs « manuscrites » sont du vrai texte dans des polices
d'écriture (Caveat, Shadows Into Light, Nanum Pen, Gaegu, Reenie Beanie), les
libellés imprimés sont en Helvetica, les cases à cocher sont des carrés de
~8 pt et une case cochée contient 2 diagonales (ou 3 hachures).

Ce script en tire, pour chacune des 80 pages (10 patientes × 8 pages) :
  - page_type, patient_no, page_in_booklet
  - fields  : {clé canonique -> {value, status, bbox, raw_label, raw_col}}
  - checkboxes : [{label, checked, bbox, group}]
  - tokens  : tous les mots manuscrits bruts avec bbox (pour entraîner un
              détecteur ou évaluer un OCR mot à mot)
  - unmapped : valeurs manuscrites dont le libellé n'a pas été reconnu par la
               table de correspondance (à ajouter dans FIELD_MAP si besoin)

Les bbox sont données dans deux repères :
  - "pdf"  : points PDF (origine en haut à gauche, 595.3 × 841.9)
  - "px"   : pixels dans les PNG fournis (1654 × 2339, soit ×2.7778)

Usage :
    pip install pdfplumber
    python auto_label_pdf.py <pdf> [--out ground_truth.json] [--pages-dir DIR]

Les images d'origine ne sont jamais modifiées.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    sys.exit("pdfplumber manquant : pip install pdfplumber")

PNG_SCALE = 1654 / 595.276  # ≈ 2.7778 (rendu 200 dpi)

# --------------------------------------------------------------------------- #
# 1. Types de page (détectés sur le titre imprimé en haut à gauche)
# --------------------------------------------------------------------------- #
PAGE_TYPES = [
    ("FICHE DE SURVEILLANCE", "COUVERTURE"),
    ("IDENTIFICATION ET ANT", "IDENTIFICATION_ANTECEDENTS"),
    ("GROSSESSE ACTUELLE", "GROSSESSE_ACTUELLE"),
    ("DÉROULEMENT DE L'ACCOUCHEMENT", "ACCOUCHEMENT"),
    ("CONSULTATION DU POST-PARTUM PRÉCOCE|MÈRE", "PP_PRECOCE_MERE"),
    ("CONSULTATION DU POST-PARTUM PRÉCOCE|NOUVEAU-NÉ", "PP_PRECOCE_NOUVEAU_NE"),
    ("CONSULTATION DU POST-PARTUM TARDIF|MÈRE", "PP_TARDIF_MERE"),
    ("CONSULTATION DU POST-PARTUM TARDIF|NOUVEAU-NÉ", "PP_TARDIF_NOUVEAU_NE"),
]

# Champs qui sont des identifiants directs : on les détecte pour pouvoir les
# masquer, on NE DOIT PAS les stocker dans un vrai système (consigne du défi).
PII_FIELDS = {
    "nom_prenom_parturiente", "cin", "adresse", "telephone", "nom_mari",
    "patiente_nom",
}

# --------------------------------------------------------------------------- #
# 2. Table de correspondance libellé -> clé canonique
#    clé = (page_type, libellé normalisé, en-tête de colonne normalisé ou "")
#    Les libellés sont normalisés : minuscules, sans accents, sans ponctuation.
# --------------------------------------------------------------------------- #
def norm(s: str) -> str:
    s = s.replace("Œ", "OE").replace("œ", "oe").replace("\x00", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9+/<>]+", " ", s)
    return s.strip()


# Colonnes de la table « grossesse actuelle » (ordre gauche -> droite)
VISIT_COLS = {
    ("1er trimestre", "visite 1"): "t1_v1",
    ("1er trimestre", "visite 2"): "t1_v2",
    ("1er trimestre", "visite 3"): "t1_v3",
    ("2eme trimestre", "visite 1"): "t2_v1",
    ("2eme trimestre", "visite 2"): "t2_v2",
    ("2eme trimestre", "visite 3"): "t2_v3",
    ("3eme trimestre", "7eme mois"): "t3_m7",
    ("3eme trimestre", "8eme mois"): "t3_m8",
    ("3eme trimestre", "9eme mois"): "t3_m9",
}
VISIT_ROWS = {
    "rendez vous": "rendez_vous",
    "venue le": "venue_le",
    "visites de relance": "visite_relance",
    "age probable": "age_gestationnel",
    "poids kg": "poids_kg",
    "ta": "ta",
    "anomalies squelette": "anomalies_squelette",
    "etat des conjonctives": "conjonctives",
    "examen des seins": "seins",
    "oedemes": "oedemes",
    "mouvements actifs": "mouvements_actifs",
    "hu cm": "hu_cm",
    "bcf": "bcf",
    "examen au speculum": "speculum",
    "tv etat du col": "tv_col",
    "tv presentation": "tv_presentation",
    "tv bassin": "tv_bassin",
    "glucosurie": "glucosurie",
    "albuminurie": "albuminurie",
    "rubeole": "rubeole",
    "toxoplasmose": "toxoplasmose",
    "syphilis tpha/vdrl": "syphilis",
    "ag hbs": "ag_hbs",
    "serologie vih": "vih",
    "hemoglobine": "hemoglobine",
    "plaquettes": "plaquettes",
    "bilan glycemique": "glycemie",
    "rai si rh negatif": "rai",
    "fer": "fer",
    "examen fait par": "examinateur",
}
ACC_COLS = {"accouch 1": 1, "accouch 2": 2, "accouch 3": 3, "accouch 4": 4, "accouch 5": 5}
ACC_ROWS = {
    "date": "date",
    "modalite d'extraction": "modalite",
    "si cesarienne indication": "indication_cesarienne",
    "complication type": "complication",
    "poids nouveau ne s": "poids_nn_g",
    "compl nouveau ne type": "complication_nn",
}
ANT_FAM_ROWS = {"hta": "hta", "diabete": "diabete", "maladies hereditaires": "maladies_hereditaires",
                "malformations": "malformations", "allergie s": "allergies"}
ANT_FAM_COLS = {"famille de la femme": "famille_femme", "mari/famille": "famille_mari"}
ANT_FEMME_COLS = {"medicaux": "medicaux", "chirurgicaux": "chirurgicaux", "gynecologiques": "gynecologiques"}
ANT_OBS_ROWS = {"avortement": "avortement", "accouchement premature": "accouchement_premature",
                "mort foetale in utero": "mort_foetale_in_utero", "autres a preciser": "autres"}
ANT_OBS_COLS = {"nombre": "nombre", "date": "date", "lieu": "lieu", "age gestationnel sa": "age_gestationnel_sa"}

# Champs « formulaire » (libellé : valeur) par type de page.
# clé = libellé normalisé (fin de ligne à gauche de la valeur) -> clé canonique
FORM_FIELDS = {
    "COUVERTURE": {
        "n de la fiche": "numero_fiche",
        "region": "region",
        "province": "province",
        "nom de l'etablissement sanitaire": "etablissement",
        "nom/prenom de la parturiente": "nom_prenom_parturiente",
        "autres a preciser": "risque_autres",
    },
    "IDENTIFICATION_ANTECEDENTS": {
        "age": "age",
        "cin": "cin",
        "niveau d'instruction": "niveau_instruction",
        "profession": "profession",            # 1re occurrence = femme, 2e = mari (géré par l'ordre y)
        "adresse": "adresse",
        "telephone": "telephone",
        "nom du mari": "nom_mari",
        "gestation": "gestation",
        "parite": "parite",
        "nombre d'enfants vivants": "enfants_vivants",
        "le": "date_vaccination",               # 1re = rubéole, 2e = hépatite B (ordre y)
        "frottis cervical / iva moins de 3 ans": "frottis_iva",
    },
    "GROSSESSE_ACTUELLE": {
        "ddr": "ddr",
        "taille": "taille_cm",
        "date prevue d'accouchement": "dpa",
        "date de depassement de terme": "date_depassement_terme",
    },
    "ACCOUCHEMENT": {
        "patiente": "patiente_nom",
        "date de l'accouchement": "date_accouchement",
        "preciser l'indication": "indication_cesarienne",
        "si autres a preciser": "complication_autres",
        "autres": "lieu_autres",
        "sexe": "sexe",
        "poids a la naissance": "poids_naissance_g",
        "perimetre cranien a la naissance": "perimetre_cranien_cm",
        "anomalie a preciser": "anomalie",
        "age gestationnel": "age_gestationnel_sa",
    },
    "PP_MERE": {
        "mere": "patiente_nom",
        "date de la consultation": "date_consultation",
        "t": "temperature",
        "ta": "ta",
        "pouls": "pouls",
        "poids": "poids_kg",
        "etat de la cicatrice": "cicatrice_cesarienne",
        "notion de prise de medicaments": "medicaments",
        "autres a preciser": "traitement_autres",
        "prochain rendez vous le": "prochain_rdv",
        "autre a preciser": "pf_autre_methode",
        "referee": "pf_referee",
        "pourquoi": "pf_refus_raison",
    },
    "PP_NOUVEAU_NE": {
        "date de la consultation": "date_consultation",
        "age": "age_jours",
        "temperature": "temperature",
        "poids": "poids_g",
        "taille": "taille_cm",
        "perimetre cranien": "perimetre_cranien_cm",
        "autres a preciser": "autres",            # 1re = signes graves, 2e = lésions (ordre y)
        "vu par": "vu_par",
        "decision prise": "decision",
        "traitement prescrit": "traitement",
        "preciser l'etablissement de reference": "etablissement_reference",
        "revenir pour une visite de suivi necessaire le": "prochain_rdv",
    },
}
# libellés apparaissant deux fois sur une page : on suffixe par ordre vertical
DUPLICATE_SUFFIX = {
    ("IDENTIFICATION_ANTECEDENTS", "profession"): ["profession", "profession_mari"],
    ("IDENTIFICATION_ANTECEDENTS", "date_vaccination"): ["date_vaccin_rubeole", "date_vaccin_hepatite_b"],
    ("PP_NOUVEAU_NE", "autres"): ["signes_graves_autres", "lesions_autres"],
    ("ACCOUCHEMENT", "lieu_autres"): ["lieu_surveille_autres", "lieu_domicile_autres"],
}

# Cases à cocher : (libellé imprimé normalisé, groupe, option) dans l'ordre de
# lecture de la page (haut -> bas, gauche -> droite). Un même libellé peut
# revenir plusieurs fois (« Normal ») : l'ordre d'apparition lève l'ambiguïté.
# Les groupes marqués * dans SCHEMA.md sont à choix unique (radio).
CHECKBOX_MAP = {
    "COUVERTURE": [
        ("", "type_etablissement", "dr"), ("csc", "type_etablissement", "csc"), ("csu", "type_etablissement", "csu"),
        ("csca", "type_etablissement", "csca"), ("csua", "type_etablissement", "csua"),
        ("fixe", "mode_couverture", "fixe"), ("mobile", "mode_couverture", "mobile"),
        ("grossesse classee a risque", "grossesse_a_risque", "oui"),
        ("anemie", "type_risque", "anemie"), ("metrorragie", "type_risque", "metrorragie"),
        ("h t a", "type_risque", "hta"), ("infection", "type_risque", "infection"),
        ("diabete", "type_risque", "diabete"), ("pre eclampsie", "type_risque", "pre_eclampsie"),
        ("cardiopathie", "type_risque", "cardiopathie"), ("eclampsie", "type_risque", "eclampsie"),
    ],
    "IDENTIFICATION_ANTECEDENTS": [
        ("consanguinite", "consanguinite", "oui"), ("grossesse desiree", "grossesse_desiree", "oui"),
        ("1", "vat", "1"), ("2", "vat", "2"), ("3", "vat", "3"), ("4", "vat", "4"), ("5", "vat", "5"),
        ("vaccinee contre la rubeole", "vaccin_rubeole", "oui"), ("vaccinee contre l'hepatite b", "vaccin_hepatite_b", "oui"),
    ],
    "GROSSESSE_ACTUELLE": [
        ("a", "groupage", "A"), ("b", "groupage", "B"), ("o", "groupage", "O"), ("ab", "groupage", "AB"),
        ("rh", "rhesus", "negatif"), ("rh+", "rhesus", "positif"),
    ],
    "ACCOUCHEMENT": [
        ("maison d'accouchement", "lieu_detail", "maison_accouchement"), ("en milieu surveille", "lieu", "milieu_surveille"),
        ("maternite", "lieu_detail", "maternite"), ("clinique privee", "lieu_detail", "clinique_privee"),
        ("a domicile", "lieu", "domicile"), ("assiste par un personnel qualifie", "lieu_detail", "domicile_assiste_qualifie"),
        ("voie basse non instrumentale", "mode", "voie_basse_non_instrumentale"),
        ("voie basse instrumentale", "mode", "voie_basse_instrumentale"),
        ("forceps", "mode_instrument", "forceps"), ("ventouse", "mode_instrument", "ventouse"),
        ("avec episiotomie", "mode_instrument", "episiotomie"),
        ("cesarienne", "mode", "cesarienne_programmee"), ("urgence", "mode", "cesarienne_urgence"),
        ("", "complications", "presence"), ("au moment de l'accouchement", "complications_moment", "accouchement"),
        ("suites de couches", "complications_moment", "suites_de_couches"),
        ("pre eclampsie", "complications_type", "pre_eclampsie"), ("eclampsie", "complications_type", "eclampsie"),
        ("hemorragie", "complications_type", "hemorragie"), ("infection", "complications_type", "infection"),
        ("autres", "complications_type", "autres"),
        ("vivant", "etat_nouveau_ne", "vivant"), ("mort ne", "etat_nouveau_ne", "mort_ne"),
        ("deces < 24 heures", "etat_nouveau_ne", "deces_moins_24h"),
    ],
    "PP_MERE": [
        ("", "periode", "dans_fenetre"), ("", "periode", "apres_fenetre"),
        ("normales", "conjonctives", "normales"), ("decolorees", "conjonctives", "decolorees"),
        ("presence du globe uterin", "globe_uterin", "present"),
        ("fade", "lochies_odeur", "fade"), ("fetide", "lochies_odeur", "fetide"), ("claires", "lochies_aspect", "claires"),
        ("sanglantes", "lochies_aspect", "sanglantes"), ("jaunatres", "lochies_aspect", "jaunatres"),
        ("normal", "perinee", "normal"), ("episiotomie", "perinee", "episiotomie"), ("reparee", "perinee", "episiotomie_reparee"),
        ("dechirure", "perinee", "dechirure"),
        ("normal", "sphincters", "normal"), ("anormal", "sphincters", "anormal"),
        ("cesarienne", "cesarienne", "oui"),
        ("normal", "seins", "normal"), ("lymphangite", "seins", "lymphangite"), ("mastite et abces", "seins", "mastite_abces"),
        ("normal", "mollets", "normal"), ("rouges", "mollets", "rouges"), ("chauds", "mollets", "chauds"),
        ("douloureux a la dorsiflexion", "mollets", "douloureux_dorsiflexion"),
        ("", "complication", "presence"),
        ("hemorragie", "complication_type", "hemorragie"), ("complications mammaires", "complication_type", "mammaires"),
        ("infection", "complication_type", "infection"), ("anemie", "complication_type", "anemie"),
        ("eclampsie", "complication_type", "eclampsie"), ("autres", "complication_type", "autres"),
        ("phlebite", "complication_type", "phlebite"),
        ("", "medicaments", "prise"),
        ("fer", "traitement", "fer"), ("vitamine a", "traitement", "vitamine_a"),
        ("desire utiliser une methode", "pf", "desire_methode"), ("pilule", "pf_methode", "pilule"), ("diu", "pf_methode", "diu"),
        ("prescription faite", "pf", "prescription_faite"), ("referee", "pf", "referee"),
    ],
    "PP_NOUVEAU_NE": [
        ("nouveau ne premature", "etat", "premature"), ("nouveau ne hypotrophe", "etat", "hypotrophe"),
        ("exclusivement au sein", "allaitement", "exclusif_sein"), ("artificiel", "allaitement", "artificiel"),
        ("mixte", "allaitement", "mixte"),
        ("convulsions", "signes_graves", "convulsions"), ("refus de teter", "signes_graves", "refus_teter"),
        ("hematemeses", "signes_graves", "hematemeses"), ("melaenas", "signes_graves", "melaenas"),
        ("diarrhee", "signes_graves", "diarrhee"), ("ictere", "signes_graves", "ictere"),
        ("tirage sous costal", "signes_graves", "tirage_sous_costal"), ("toux", "signes_graves", "toux"),
        ("rythme respiratoire anormal", "signes_graves", "rythme_respiratoire_anormal"),
        ("fievre", "signes_graves", "fievre"), ("hypothermie", "signes_graves", "hypothermie"),
        ("bosse serosanguine ou cephalohematome", "lesions", "bosse_serosanguine_cephalohematome"),
        ("luxation congenitale de la hanche", "lesions", "luxation_hanche"),
        ("diminution ou absence de la mobilite d'un membre", "lesions", "mobilite_membre_diminuee"),
        ("normal", "evaluation_allaitement", "normal"), ("a problemes", "evaluation_allaitement", "a_problemes"),
        ("bcg", "vaccins", "bcg"), ("hb", "vaccins", "hb"),
        ("supplementation en vitamine d", "vitamine_d", "oui"),
        ("ictere", "complications", "ictere"), ("infection", "complications", "infection"),
        ("conjonctivite", "complications", "conjonctivite"), ("traumatisme", "complications", "traumatisme"),
        ("malformation", "complications", "malformation"), ("autres", "complications", "autres"),
        ("transfert", "transfert", "oui"),
    ],
}
CHECKBOX_MAP["PP_PRECOCE_MERE"] = CHECKBOX_MAP["PP_TARDIF_MERE"] = CHECKBOX_MAP["PP_MERE"]
CHECKBOX_MAP["PP_PRECOCE_NOUVEAU_NE"] = CHECKBOX_MAP["PP_TARDIF_NOUVEAU_NE"] = CHECKBOX_MAP["PP_NOUVEAU_NE"]

# Les clés des tables ci-dessus sont normalisées une fois pour toutes
FORM_FIELDS = {pt: {norm(k): v for k, v in m.items()} for pt, m in FORM_FIELDS.items()}
CHECKBOX_MAP = {pt: [(norm(l), g, o) for l, g, o in lst] for pt, lst in CHECKBOX_MAP.items()}
VISIT_COLS = {(norm(a), norm(b)): v for (a, b), v in VISIT_COLS.items()}
for _d in (VISIT_ROWS, ACC_COLS, ACC_ROWS, ANT_FAM_ROWS, ANT_FAM_COLS, ANT_FEMME_COLS, ANT_OBS_ROWS, ANT_OBS_COLS):
    for _k in list(_d):
        _d[norm(_k)] = _d.pop(_k)

# ---- Réparation des glyphes manquants -------------------------------------
# Certaines polices d'écriture n'ont pas les glyphes accentués : la couche
# texte contient alors un caractère NUL (\x00) et l'image montre un blanc
# (« P les » pour « Pâles », « Ferm » pour « Fermé », rien pour « — »).
# La vérité terrain doit porter la valeur voulue. On répare chaque mot en le
# comparant au vocabulaire des mots propres (manuscrits et imprimés) du PDF,
# \x00 jouant le rôle de joker d'un caractère, puis par un petit dictionnaire.
MANUAL_GLYPH_FIXES = {
    "s\x00v\x00re": "sévère", "\x00": "—", "\x00C": "°C", "Pr\x00-\x00clampsie": "Pré-éclampsie",
    "Ut\x00rus": "Utérus", "r\x00guliers": "réguliers", "s\x00che": "sèche", "P\x00les": "Pâles",
    "Ferm\x00": "Fermé", "C\x00phalique": "Céphalique", "N\x00ant": "Néant", "D\x00color\x00es": "Décolorées",
    "Coll\x00ge": "Collège", "Couturi\x00re": "Couturière", "Commer\x00ante": "Commerçante",
    "Employ\x00e": "Employée", "B\x00ni": "Béni", "Mellal-Kh\x00nifra": "Mellal-Khénifra",
    "Dr\x00a-Tafilalet": "Drâa-Tafilalet", "K\x00nitra": "Kénitra", "Mekn\x00s": "Meknès",
    "Rabat-Sal\x00-K\x00nitra": "Rabat-Salé-Kénitra", "F\x00s-Mekn\x00s": "Fès-Meknès",
    "l\x00ger": "léger", "Hoce\x00ma": "Hoceïma", "Kh\x00misset": "Khémisset", "C\x00sarienne": "Césarienne",
    "pr\x00matur\x00": "prématuré", "f\x00tale": "fœtale",
}
_VOCAB: set = set()


def build_vocab(pdf):
    """Mots sans glyphe manquant, toutes pages (manuscrits + imprimés)."""
    for p in pdf.pages:
        for w in p.extract_words(extra_attrs=["fontname"], x_tolerance=1.5):
            if "\x00" not in w["text"]:
                _VOCAB.add(w["text"])


def repair_word(t: str) -> str:
    if "\x00" not in t:
        return t
    if t in MANUAL_GLYPH_FIXES:
        return MANUAL_GLYPH_FIXES[t]
    pat = re.compile("^" + "".join("." if ch == "\x00" else re.escape(ch) for ch in t) + "$")
    cands = sorted({v for v in _VOCAB if len(v) == len(t) and pat.match(v)})
    if len(cands) == 1:
        return cands[0]
    if cands:   # plusieurs candidats : préférer un mot avec accent
        acc = [c for c in cands if norm(c) != c.lower()]
        return (acc or cands)[0]
    return t.replace("\x00", "?")


def fix_value(v: str) -> str:
    v = " ".join(repair_word(t) for t in v.split())
    return re.sub(r"\s+", " ", v).strip()


# --------------------------------------------------------------------------- #
# 3. Géométrie
# --------------------------------------------------------------------------- #
def center(o):
    return ((o["x0"] + o["x1"]) / 2, (o["top"] + o["bottom"]) / 2)


def bbox(o):
    return [round(o["x0"], 1), round(o["top"], 1), round(o["x1"], 1), round(o["bottom"], 1)]


def bbox_px(b):
    return [int(round(v * PNG_SCALE)) for v in b]


def page_structure(p):
    """Segments de grille (lignes + bords des cadres), cases, marques."""
    V, H = [], []
    for l in p.lines:
        w, h = l["x1"] - l["x0"], l["bottom"] - l["top"]
        # les pages sont légèrement inclinées (jusqu'à ~0.3°) : on classe par
        # ratio et on prend la position médiane du segment
        if h > 15 and w < max(4, 0.06 * h):
            V.append(((l["x0"] + l["x1"]) / 2, l["top"], l["bottom"]))
        elif w > 15 and h < max(4, 0.06 * w):
            H.append(((l["top"] + l["bottom"]) / 2, l["x0"], l["x1"]))
    boxes, marks, bands = [], [], []
    for c in p.curves:
        w, h = c["width"], c["height"]
        if 7 < w < 9.6 and 7 < h < 9.6 and len(c["pts"]) == 5:
            boxes.append(c)
        elif w > 30 and h > 30:                      # cadre rectangulaire
            V += [(c["x0"], c["top"], c["bottom"]), (c["x1"], c["top"], c["bottom"])]
            H += [(c["top"], c["x0"], c["x1"]), (c["bottom"], c["x0"], c["x1"])]
        elif w > 100 and 10 < h < 30:                # bandeau de section (plein)
            bands.append(c)
            H += [(c["top"], c["x0"], c["x1"]), (c["bottom"], c["x0"], c["x1"])]
        elif w < 20 and h < 20:                      # coche dessinée en courbe
            marks.append(c)
    for l in p.lines:                                # diagonales / hachures
        if (l["x1"] - l["x0"]) <= 16 and (l["bottom"] - l["top"]) <= 16:
            marks.append(l)
    return V, H, boxes, marks, bands


def is_checked(box, marks):
    bx0, by0, bx1, by1 = box["x0"] - 3, box["top"] - 3, box["x1"] + 3, box["bottom"] + 3
    for m in marks:
        if m is box:
            continue
        cx, cy = center(m)
        if bx0 <= cx <= bx1 and by0 <= cy <= by1:
            return True
    return False


def find_cell(cx, cy, V, H):
    """Bornes de la cellule de grille contenant (cx, cy), ou None."""
    left = max((x for x, t, b in V if x < cx and t - 2 <= cy <= b + 2), default=None)
    right = min((x for x, t, b in V if x > cx and t - 2 <= cy <= b + 2), default=None)
    top = max((y for y, a, b in H if y < cy and a - 2 <= cx <= b + 2), default=None)
    bottom = min((y for y, a, b in H if y > cy and a - 2 <= cx <= b + 2), default=None)
    if None in (left, right, top, bottom):
        return None
    if right - left > 320 or bottom - top > 130:
        return None
    return left, top, right, bottom


def words_in(words, x0, y0, x1, y1):
    out = [w for w in words if x0 - 1 <= center(w)[0] <= x1 + 1 and y0 - 1 <= center(w)[1] <= y1 + 1]
    if not out:
        return out
    if max(w["top"] for w in out) - min(w["top"] for w in out) < 7:   # une seule ligne (page inclinée)
        return sorted(out, key=lambda w: w["x0"])
    out.sort(key=lambda w: w["top"])
    lines, cur = [], [out[0]]
    for w in out[1:]:                                                   # plusieurs lignes
        if w["top"] - cur[-1]["top"] < 7:
            cur.append(w)
        else:
            lines.append(cur)
            cur = [w]
    lines.append(cur)
    return [w for ln in lines for w in sorted(ln, key=lambda w: w["x0"])]


def join(words):
    return " ".join(w["text"] for w in words)


# --------------------------------------------------------------------------- #
# 4. Extraction d'une page
# --------------------------------------------------------------------------- #
def detect_page_type(printed):
    head = " ".join(w["text"] for w in printed if w["top"] < 60 and w["x0"] < 390)
    body = " ".join(w["text"] for w in printed if w["top"] < 110)
    head_u = head.upper()
    for key, pt in PAGE_TYPES:
        parts = key.split("|")
        if all(k in head_u or k in body.upper() for k in parts):
            if "POST-PARTUM" in parts[0]:
                is_nn = "NOUVEAU-NÉ" in body.upper()
                if ("NOUVEAU-NÉ" in parts[1]) != is_nn:
                    continue
            return pt
    return "INCONNU"


def extract_page(p, page_index):
    words = p.extract_words(extra_attrs=["fontname"], keep_blank_chars=False, x_tolerance=1.5)
    printed = [w for w in words if "Helvetica" in w["fontname"]]
    hw = [w for w in words if "Helvetica" not in w["fontname"]]
    V, H, boxes, marks, bands = page_structure(p)
    page_type = detect_page_type(printed)
    m = re.search(r"n°(\d+)/10", " ".join(w["text"] for w in printed if w["top"] < 60))
    patient_no = int(m.group(1)) if m else None

    fields: dict = {}
    unmapped = []
    form_group = {"PP_PRECOCE_MERE": "PP_MERE", "PP_TARDIF_MERE": "PP_MERE",
                  "PP_PRECOCE_NOUVEAU_NE": "PP_NOUVEAU_NE", "PP_TARDIF_NOUVEAU_NE": "PP_NOUVEAU_NE"}.get(page_type, page_type)
    form_map = FORM_FIELDS.get(form_group, {})
    seen_counts: dict = defaultdict(int)

    # -- 4a. regrouper les mots manuscrits par cellule de grille ou par ligne --
    cells: dict = defaultdict(list)       # (left,top,right,bottom) -> words
    loose: list = []                       # mots hors grille (champs formulaire)
    for w in hw:
        if w["top"] < 60 and w["x0"] > 390:   # cartouche « spécimen »
            continue
        cx, cy = center(w)
        cell = find_cell(cx, cy, V, H)
        if cell:
            cells[cell].append(w)
        else:
            loose.append(w)

    def add_field(key, ws, raw_label, raw_col="", cell=None):
        ws = sorted(ws, key=lambda w: w["x0"])
        b = [min(w["x0"] for w in ws), min(w["top"] for w in ws), max(w["x1"] for w in ws), max(w["bottom"] for w in ws)]
        b = [round(v, 1) for v in b]
        if key in fields:                  # même clé deux fois -> concaténer
            fields[key]["value"] += " " + fix_value(join(ws))
            fields[key]["bbox_pdf"] = [min(fields[key]["bbox_pdf"][0], b[0]), min(fields[key]["bbox_pdf"][1], b[1]),
                                       max(fields[key]["bbox_pdf"][2], b[2]), max(fields[key]["bbox_pdf"][3], b[3])]
            fields[key]["bbox_px"] = bbox_px(fields[key]["bbox_pdf"])
            return
        raw = join(ws)
        value = fix_value(raw)
        status = "NON_APPLICABLE" if value.strip() in {"—", "-", "–"} else "CONNU"
        # « — » dessiné avec une police sans ce glyphe => la cellule est VIDE sur
        # l'image : rendered=False. Un modèle qui lit l'image doit alors répondre
        # NON_FOURNI ; la valeur voulue par le générateur reste « — ».
        rendered = raw.strip("\x00 ") != ""
        fields[key] = {"value": value, "status": status, "rendered": rendered,
                       "status_on_image": status if rendered else "NON_FOURNI",
                       "bbox_pdf": b, "bbox_px": bbox_px(b),
                       "cell_pdf": [round(v, 1) for v in cell] if cell else None,
                       "cell_px": bbox_px(cell) if cell else None,
                       "raw_label": raw_label, "raw_col": raw_col, "pii": key in PII_FIELDS}

    # -- 4b. cellules de tableau : libellé de ligne + en-tête(s) de colonne --
    # Un texte trop large pour sa cellule déborde dans la cellule voisine
    # (« Voie | basse ») : on rattache à la cellule de gauche les mots qui
    # commencent au ras du bord quand la cellule de gauche déborde déjà.
    for key_cell in sorted(cells, key=lambda c: (c[1], c[0])):
        left, top, right, bottom = key_cell
        ws = cells.get(key_cell)
        if not ws:
            continue
        prev = next((c for c in cells if abs(c[2] - left) < 1 and abs(c[1] - top) < 1 and c is not key_cell), None)
        if prev and cells[prev] and max(w["x1"] for w in cells[prev]) > left - 1 and min(w["x0"] for w in ws) < left + 6:
            cells[prev].extend(ws)
            cells[key_cell] = []
    for (left, top, right, bottom), ws in cells.items():
        if not ws:
            continue
        cy = (top + bottom) / 2
        # libellé de ligne = texte imprimé dans la 1re colonne du MÊME tableau
        # (une ligne verticale plus à gauche n'appartient au tableau que si une
        # ligne horizontale de cette bande la rejoint)
        lefts = [x for x, t, b in V if x < left and t - 2 <= cy <= b + 2
                 and any(abs(y - top) < 3 and a - 6 <= x <= b + 6 and a - 6 <= left <= b + 6 for y, a, b in H)]
        table_left = min(lefts) if lefts else left
        row_words = words_in(printed, table_left, top, left, bottom) if table_left < left else []
        if not row_words and table_left < left:  # ligne à cheval (libellé décalé de 1-2 px)
            row_words = [w for w in printed if w["x1"] <= left and top - 4 <= center(w)[1] <= bottom + 4 and w["x0"] >= table_left - 2]
        row_label = norm(join(row_words))
        # en-têtes de colonne : remonter les bandes au-dessus dans la même colonne
        col_labels = []
        y = top
        for _ in range(80):                      # remonter bande par bande jusqu'au haut du tableau
            above = [yy for yy, a, b in H if yy < y - 1 and a - 2 <= (left + right) / 2 <= b + 2]
            if not above:
                break
            y2 = max(above)
            cell_above = find_cell((left + right) / 2, (y2 + y) / 2, V, H)
            if cell_above is None:
                break
            hdr = words_in(printed, cell_above[0], cell_above[1], cell_above[2], cell_above[3])
            if hdr:
                col_labels.append(norm(join(hdr)))
                if len(col_labels) == 2:
                    break
            y = y2
        col_label = " | ".join(col_labels)
        key = None
        if page_type == "GROSSESSE_ACTUELLE":
            r = VISIT_ROWS.get(row_label)
            c = None
            if len(col_labels) >= 2:
                c = VISIT_COLS.get((col_labels[1], col_labels[0]))
            if r and c:
                key = f"visites.{c}.{r}"
        elif page_type == "IDENTIFICATION_ANTECEDENTS":
            if row_label in ANT_FAM_ROWS and col_labels and col_labels[0] in ANT_FAM_COLS:
                key = f"antecedents_familiaux.{ANT_FAM_ROWS[row_label]}.{ANT_FAM_COLS[col_labels[0]]}"
            elif col_labels and col_labels[0] in ANT_FEMME_COLS and not row_label:
                key = f"antecedents_femme.{ANT_FEMME_COLS[col_labels[0]]}"
            elif row_label in ANT_OBS_ROWS and col_labels and col_labels[0] in ANT_OBS_COLS:
                key = f"antecedents_obstetricaux.{ANT_OBS_ROWS[row_label]}.{ANT_OBS_COLS[col_labels[0]]}"
            elif row_label in ACC_ROWS and col_labels and col_labels[0] in ACC_COLS:
                key = f"accouchements_anterieurs.{ACC_COLS[col_labels[0]]}.{ACC_ROWS[row_label]}"
        if key:
            add_field(key, ws, row_label, col_label, cell=(left, top, right, bottom))
        else:
            # cadre simple (pas un tableau) : traiter comme champ formulaire
            loose.extend(ws)

    # -- 4c. champs formulaire : libellé imprimé à gauche sur la même ligne --
    # 1) regrouper les mots manuscrits libres en « runs » : même ligne, trou < 12 pt
    loose.sort(key=lambda w: center(w)[1])
    lines_: list = []
    for w in loose:                       # regroupement séquentiel par ligne (tolérance 5 pt)
        if lines_ and abs(center(w)[1] - lines_[-1][-1]) < 5:
            lines_[-1][0].append(w)
        else:
            lines_.append([[w], center(w)[1]])
    loose = [w for ws, _ in lines_ for w in sorted(ws, key=lambda w: w["x0"])]
    runs: list = []
    is_dots = lambda pw: set(pw["text"]) <= set(". ")
    for w in loose:
        cx, cy = center(w)
        if runs:
            gap = w["x0"] - runs[-1]["x1"]
            printed_between = any(abs(center(pw)[1] - cy) < 5 and runs[-1]["x1"] - 1 <= pw["x0"] and pw["x1"] <= w["x0"] + 1
                                  and not is_dots(pw) for pw in printed)
            if abs(runs[-1]["cy"] - cy) < 5 and 0 <= gap < 22 and not printed_between:
                runs[-1]["words"].append(w)
                runs[-1]["x1"] = w["x1"]
                continue
        runs.append({"cy": cy, "x1": w["x1"], "words": [w]})

    def match_key(label):
        for cand, k in sorted(form_map.items(), key=lambda kv: -len(kv[0])):
            if label == cand or label.endswith(" " + cand):
                return k
        return None

    # 2) libellé = run contigu de mots imprimés juste à gauche du 1er mot
    groups: list = []
    for r in runs:
        w = r["words"][0]
        cy = r["cy"]
        same_line = sorted([pw for pw in printed if abs(center(pw)[1] - cy) < 5 and pw["x1"] <= w["x0"] + 1
                            and not is_dots(pw)], key=lambda pw: pw["x0"])
        dots_line = [pw for pw in printed if abs(center(pw)[1] - cy) < 5 and is_dots(pw)]
        run = []
        last_x = w["x0"]
        for pw in reversed(same_line):
            gap = last_x - pw["x1"]
            inter_hw = any(h2 not in r["words"] and abs(center(h2)[1] - cy) < 5 and pw["x1"] <= h2["x0"] < last_x for h2 in hw)
            dots_between = any(pw["x1"] - 2 <= d["x0"] and d["x0"] <= last_x for d in dots_line)
            box_between = any(abs(center(b)[1] - cy) < 6 and pw["x1"] <= b["x0"] <= last_x for b in boxes)
            if inter_hw or (box_between and run) or (gap > 40 and not dots_between and run):
                break
            run.insert(0, pw)
            last_x = pw["x0"]
        label = norm(join(run)).rstrip(" :")
        label = re.sub(r"[ .]+$", "", label)
        key = match_key(label)
        if key is None and run:               # cas « T° 37.2 TA 110/71 » : libellé = dernier mot
            key = form_map.get(norm(run[-1]["text"]).strip())
        if key is None and not run:
            # valeur écrite SOUS son libellé (« Pourquoi ? », « Préciser l'indication : »)
            above = [pw for pw in printed if 0 < cy - center(pw)[1] < 24 and abs(pw["x0"] - w["x0"]) < 260]
            if above:
                yref = max(center(pw)[1] for pw in above)
                line_above = sorted([pw for pw in printed if abs(center(pw)[1] - yref) < 5], key=lambda pw: pw["x0"])
                lab2 = re.sub(r"[ .]+$", "", norm(join(line_above)).rstrip(" :?"))
                if line_above and line_above[-1]["text"].rstrip()[-1:] in ":?":
                    key = match_key(lab2)
                    if key:
                        label = lab2
        groups.append({"key": key, "label": label, "cy": cy, "words": r["words"]})
    # 3) une valeur sur deux lignes : la 2e ligne n'a pas de libellé et commence
    #    sous la 1re -> on la rattache au groupe précédent
    merged: list = []
    for g in groups:
        if merged and g["key"] is None and g["label"] == "" and merged[-1]["key"] \
                and 0 < g["cy"] - merged[-1]["cy"] < 16 and abs(g["words"][0]["x0"] - merged[-1]["words"][0]["x0"]) < 120:
            merged[-1]["words"].extend(g["words"])
        else:
            merged.append(g)
    for g in merged:
        key = g["key"]
        if key is None:
            unmapped.append({"value": fix_value(join(g["words"])), "left_text": g["label"], "bbox_pdf": bbox(g["words"][0])})
            continue
        dup = DUPLICATE_SUFFIX.get((form_group, key))
        if dup:
            i = min(seen_counts[key], len(dup) - 1)
            seen_counts[key] += 1
            key = dup[i]
        add_field(key, g["words"], g["label"])

    # -- 4d. cases à cocher --
    # ordre de lecture robuste à l'inclinaison : regroupement par ligne (6 pt) puis x
    boxes_sorted = sorted(boxes, key=lambda b: center(b)[1])
    box_lines: list = []
    for b in boxes_sorted:
        if box_lines and abs(center(b)[1] - box_lines[-1][-1]) < 6:
            box_lines[-1][0].append(b)
        else:
            box_lines.append([[b], center(b)[1]])
    ordered = [b for bs, _ in box_lines for b in sorted(bs, key=lambda b: b["x0"])]
    cb_map = list(CHECKBOX_MAP.get(page_type, []))
    consumed = [False] * len(cb_map)
    checkboxes = []
    choices: dict = defaultdict(list)
    for b in ordered:
        cy = center(b)[1]
        right = [w for w in printed if abs(center(w)[1] - cy) < 5 and 0 <= w["x0"] - b["x1"] < 14]
        label_words = []
        if right:
            # étendre vers la droite jusqu'à un trou, une autre case ou un ':'
            line = sorted([w for w in printed if abs(center(w)[1] - cy) < 5 and w["x0"] >= b["x1"]], key=lambda w: w["x0"])
            last = b["x1"]
            for w in line:
                if w["x0"] - last > 14:
                    break
                if any(abs(center(ob)[1] - cy) < 5 and last <= ob["x0"] < w["x0"] for ob in boxes if ob is not b):
                    break
                label_words.append(w)
                last = w["x1"]
                if w["text"].endswith(":"):
                    break
            side = "right"
        else:
            line = sorted([w for w in printed if abs(center(w)[1] - cy) < 5 and w["x1"] <= b["x0"] + 1], key=lambda w: w["x0"])
            last = b["x0"]
            for w in reversed(line):
                if last - w["x1"] > 14:
                    break
                label_words.insert(0, w)
                last = w["x0"]
            side = "left"
        label = join(label_words).strip(" :")
        lab_n = norm(label).rstrip(" :")
        key = group = option = None
        for i, (ln, g, o) in enumerate(cb_map):
            if not consumed[i] and ln == lab_n:
                consumed[i] = True
                group, option = g, o
                key = f"{g}.{o}"
                break
        checked = is_checked(b, marks)
        if group and checked:
            choices[group].append(option)
        if group and group not in choices:
            choices[group] = []
        checkboxes.append({
            "key": key, "group": group, "option": option,
            "label": label, "label_side": side, "checked": checked,
            "bbox_pdf": bbox(b), "bbox_px": bbox_px(bbox(b)),
        })

    tokens = [{"text": w["text"], "font": w["fontname"].split("+")[-1], "bbox_pdf": bbox(w), "bbox_px": bbox_px(bbox(w))}
              for w in hw if not (w["top"] < 60 and w["x0"] > 390)]

    return {
        "page_index": page_index + 1,
        "png_file": f"dossiers_specimen_10_patientes-{page_index + 1:02d}.png",
        "patient_no": patient_no,
        "page_in_booklet": page_index % 8 + 1,
        "page_type": page_type,
        "handwriting_font": sorted({t["font"] for t in tokens}),
        "fields": fields,
        "choices": dict(choices),
        "checkboxes": checkboxes,
        "tokens": tokens,
        "unmapped": unmapped,
    }


# --------------------------------------------------------------------------- #
# 5. Complétion par le schéma : champs vides -> NON_FOURNI ou NON_APPLICABLE
# --------------------------------------------------------------------------- #
def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def complete_with_schema(page: dict, schema: dict):
    """Ajoute les champs du schéma absents de la page avec le bon statut."""
    pt = page["page_type"]
    if pt not in schema["pages"]:
        return
    fields, choices = page["fields"], page["choices"]
    parite = _to_int(fields.get("parite", {}).get("value"))
    for fd in schema["pages"][pt]["fields"]:
        k = fd["key"]
        if k in fields:
            continue
        status = "NON_FOURNI"
        m = re.match(r"accouchements_anterieurs\.(\d)\.(\w+)", k)
        if m and parite is not None and parite < int(m.group(1)):
            status = "NON_APPLICABLE"
        elif m and m.group(2) == "indication_cesarienne":
            modal = fields.get(f"accouchements_anterieurs.{m.group(1)}.modalite", {}).get("value", "")
            if modal and "sarienne" not in modal:
                status = "NON_APPLICABLE"
        elif k.endswith(".rai") and choices.get("rhesus") == ["positif"]:
            status = "NON_APPLICABLE"
        elif k == "indication_cesarienne" and pt == "ACCOUCHEMENT" and choices.get("mode") and not any("cesarienne" in o for o in choices["mode"]):
            status = "NON_APPLICABLE"
        elif k == "cicatrice_cesarienne" and not choices.get("cesarienne"):
            status = "NON_APPLICABLE"
        elif k == "etablissement_reference" and not choices.get("transfert"):
            status = "NON_APPLICABLE"
        elif k == "date_vaccin_rubeole" and not choices.get("vaccin_rubeole"):
            status = "NON_APPLICABLE"
        elif k == "date_vaccin_hepatite_b" and not choices.get("vaccin_hepatite_b"):
            status = "NON_APPLICABLE"
        elif k == "pf_refus_raison" and "desire_methode" in choices.get("pf", []):
            status = "NON_APPLICABLE"
        elif k == "complication_autres" and "autres" not in choices.get("complications_type", []):
            status = "NON_APPLICABLE"
        elif k == "risque_autres" and not choices.get("grossesse_a_risque"):
            status = "NON_APPLICABLE"
        fields[k] = {"value": None, "status": status, "rendered": True, "status_on_image": status,
                     "bbox_pdf": None, "bbox_px": None, "cell_pdf": None, "cell_px": None,
                     "raw_label": "", "raw_col": "", "pii": k in PII_FIELDS, "source": "schema"}
    for grp in schema["pages"][pt].get("checkbox_groups", []):
        choices.setdefault(grp["key"], [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="ground_truth.json")
    ap.add_argument("--pages-dir", default=None, help="écrit aussi un JSON par page dans ce dossier")
    ap.add_argument("--schema", default=None, help="registry_schema.json : complète les champs vides (NON_FOURNI / NON_APPLICABLE)")
    args = ap.parse_args()

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8")) if args.schema else None
    pages = []
    with pdfplumber.open(args.pdf) as pdf:
        build_vocab(pdf)
        for i, p in enumerate(pdf.pages):
            pg = extract_page(p, i)
            if schema:
                complete_with_schema(pg, schema)
            pages.append(pg)

    out = {
        "source_pdf": Path(args.pdf).name,
        "png_scale": PNG_SCALE,
        "coordinate_note": "bbox_pdf en points (origine haut-gauche) ; bbox_px dans les PNG 1654x2339",
        "statuses": ["CONNU", "INCONNU", "NON_FOURNI", "ILLISIBLE", "NON_APPLICABLE", "A_REVISER"],
        "pages": pages,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.pages_dir:
        d = Path(args.pages_dir)
        d.mkdir(parents=True, exist_ok=True)
        for pg in pages:
            (d / (Path(pg["png_file"]).stem + ".json")).write_text(json.dumps(pg, ensure_ascii=False, indent=1), encoding="utf-8")

    n_fields = sum(1 for pg in pages for v in pg["fields"].values() if v.get("source") != "schema")
    n_boxes = sum(len(pg["checkboxes"]) for pg in pages)
    n_checked = sum(1 for pg in pages for b in pg["checkboxes"] if b["checked"])
    n_unmapped = sum(len(pg["unmapped"]) for pg in pages)
    print(f"{len(pages)} pages | {n_fields} champs | {n_boxes} cases ({n_checked} cochées) | {n_unmapped} valeurs non mappées")
    for pg in pages:
        if pg["unmapped"]:
            print(f"  p{pg['page_index']:02d} {pg['page_type']}: non mappé -> {[(u['left_text'], u['value']) for u in pg['unmapped']]}")


if __name__ == "__main__":
    main()
