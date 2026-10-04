#!/usr/bin/env python3
"""
build_schema.py, génère registry_schema.json et SCHEMA.md

Schéma de champs + modèle de statuts du carnet « Fiche de surveillance de la
grossesse et du post-partum » (Ministère de la Santé, Maroc), tel qu'il
apparaît dans les données DayOne (8 pages par dossier).

Les clés sont EXACTEMENT celles produites par tools/auto_label_pdf.py, donc
la vérité terrain et le schéma parlent la même langue.

    python build_schema.py            # écrit registry_schema.json + SCHEMA.md
"""
import json
from pathlib import Path

# --------------------------------------------------------------------------- #
# 1. Statuts de champ (consigne du défi) et règles d'attribution
# --------------------------------------------------------------------------- #
STATUSES = {
    "CONNU": "Valeur lue avec une confiance >= seuil_connu (0.85 par défaut). Montrée à la sage-femme pour confirmation groupée.",
    "A_REVISER": "Valeur lue mais confiance entre seuil_revision (0.5) et seuil_connu, OU incohérence détectée par une règle (ex. parité > gestité), OU désaccord entre deux lectures. L'agent pose une question ciblée avec le recadrage de la cellule.",
    "ILLISIBLE": "De l'encre est présente dans la zone mais aucune lecture n'atteint seuil_revision. L'agent propose : reprendre la photo / saisir à la main.",
    "NON_FOURNI": "La zone est vide sur la page photographiée (la page a bien été capturée). Rien n'a été écrit par la sage-femme.",
    "NON_APPLICABLE": "Le champ ne s'applique pas : tiret/barré explicite sur le papier, ou règle de dépendance (ex. indication de césarienne si voie basse ; RAI si Rhésus positif ; Accouch. 3 si parité = 2).",
    "INCONNU": "On ne sait pas : la page/section n'a pas (encore) été photographiée dans cette session, l'IA est indisponible, ou la sage-femme a répondu « je ne sais pas ».",
}
STATUS_RULES = [
    "Un champ a toujours exactement un statut ; `value` n'est renseigné que pour CONNU et A_REVISER (et pour ILLISIBLE si une lecture partielle existe, marquée `partial: true`).",
    "`confidence` est un réel [0,1] obligatoire pour CONNU / A_REVISER / ILLISIBLE ; il doit être calibré (voir SCHEMA.md §Confiance).",
    "NON_FOURNI ≠ NON_APPLICABLE ≠ INCONNU : un simple « N/A » est interdit (consigne du défi).",
    "Une valeur hors vocabulaire fermé (enum) passe automatiquement en A_REVISER avec la meilleure correspondance proposée.",
    "Toute réponse de la sage-femme (confirmation, correction, saisie manuelle) force CONNU avec confidence = 1.0 et `source = midwife`.",
    "Les champs PII (pii = true) sont détectés pour être masqués sur l'image puis supprimés : ils ne sont JAMAIS stockés (statut interne REDACTED, jamais exporté).",
]

# Format d'un champ extrait (ce que produit le pipeline)
FIELD_RECORD = {
    "value": "string | number | null, valeur normalisée selon `type`",
    "raw": "string | null, texte tel que lu avant normalisation",
    "status": "CONNU | A_REVISER | ILLISIBLE | NON_FOURNI | NON_APPLICABLE | INCONNU",
    "confidence": "float [0,1] | null",
    "source": "ai | midwife | rule, qui a fixé la valeur/le statut",
    "bbox_px": "[x0, y0, x1, y1] dans l'image d'origine | null, permet le recadrage pour la question de suivi",
    "page_id": "identifiant de la capture d'où vient la valeur",
    "notes": "string | null, ex. 'NF écrit sur le papier', 'IG = primigeste'",
}

# --------------------------------------------------------------------------- #
# 2. Types de valeur et normalisation
# --------------------------------------------------------------------------- #
TYPES = {
    "date": {"format": "JJ/MM/AAAA", "accepte": ["JJ/MM/AA", "JJ-MM-AAAA", "JJ.MM.AAAA"], "regex": r"^\d{2}/\d{2}/\d{4}$"},
    "int": {"regex": r"^\d+$"},
    "float": {"regex": r"^\d+([.,]\d+)?$", "note": "virgule décimale acceptée, stockée avec un point"},
    "text": {},
    "enum": {"note": "vocabulaire fermé ; correspondance floue (accents/casse/abréviations) puis A_REVISER si distance > 2"},
    "bool": {"note": "case cochée / Oui-Non"},
    "ta": {"format": "SYS/DIA en mmHg", "regex": r"^\d{2,3}/\d{2,3}$", "accepte": ["12/7 (cmHg → ×10)", "120/70"]},
    "age_gestationnel": {"format": "SA[+j]", "regex": r"^\d{1,2}\s*SA(\s*\+\s*\d\s*j)?$", "accepte": ["16SA+3j", "16 SA", "16 sem"]},
    "poids_g": {"unit": "g", "accepte": ["3,5 kg → 3500"]},
    "poids_kg": {"unit": "kg"},
    "longueur_cm": {"unit": "cm"},
    "temperature": {"unit": "°C", "regex": r"^\d{2}([.,]\d)?$"},
}

# Alias fréquents sur le papier (réels + synthétiques) → forme canonique
ALIASES = {
    "RAS": ["RAS", "R.A.S", "Ras", "rien à signaler", "Aucun", "Aucune", "Néant", "0"],
    "Neg": ["Neg", "Nég", "Négatif", "négative", "(-)", "-"],
    "Pos": ["Pos", "Positif", "positive", "(+)", "+"],
    "Oui": ["Oui", "oui", "O", "✓", "✗ (dans une case)", "Reçu"],
    "Non": ["Non", "non", "N", "0"],
    "Normales": ["Normales", "Normal", "Nles", "colorées", "bien colorées"],
    "Pâles": ["Pâles", "Pales", "P les", "décolorées"],
    "Fermé": ["Fermé", "Ferm", "fermé", "long fermé", "LF"],
    "Céphalique": ["Céphalique", "C phalique", "céph", "sommet"],
    "Voie basse": ["Voie basse", "VB", "AVB", "accouchement voie basse"],
    "Césarienne": ["Césarienne", "César", "C/S", "CS"],
    "Immune": ["Immune", "immunisée", "+"],
    "Non immune": ["Non immune", "non immunisée", "-"],
}
# Codes manuscrits qui portent un STATUT, pas une valeur
STATUS_CODES = {
    "NF": {"status": "NON_FOURNI", "note": "« non fait » : l'examen n'a pas été réalisé"},
    "—": {"status": "NON_APPLICABLE", "note": "tiret explicite"},
    "/": {"status": "NON_APPLICABLE", "note": "cellule barrée"},
    "IG": {"status": "CONNU", "note": "« primigeste » écrit en travers des antécédents obstétricaux → gestation = 1, parité = 0, accouchements antérieurs = NON_APPLICABLE"},
}

# --------------------------------------------------------------------------- #
# 3. Pages, champs, groupes de cases
# --------------------------------------------------------------------------- #
def f(key, label, type_, **kw):
    d = {"key": key, "label": label, "type": type_}
    d.update(kw)
    return d


def g(key, label, choice, options, **kw):
    d = {"key": key, "label": label, "choice": choice, "options": options}
    d.update(kw)
    return d


PII = {"pii": True, "store": False}

PAGES = {}

PAGES["COUVERTURE"] = {
    "title": "Fiche de surveillance de la grossesse et du post-partum (couverture)",
    "fields": [
        f("numero_fiche", "N° de la fiche", "text", note="peut servir de code de liaison si la sage-femme l'utilise ainsi"),
        f("region", "Région", "enum", values=["Tanger-Tétouan-Al Hoceïma", "Oriental", "Fès-Meknès", "Rabat-Salé-Kénitra", "Béni Mellal-Khénifra", "Casablanca-Settat", "Marrakech-Safi", "Drâa-Tafilalet", "Souss-Massa", "Guelmim-Oued Noun", "Laâyoune-Sakia El Hamra", "Dakhla-Oued Ed-Dahab"], fuzzy=True),
        f("province", "Province", "text"),
        f("etablissement", "Nom de l'établissement sanitaire", "text"),
        f("nom_prenom_parturiente", "Nom/Prénom de la parturiente", "text", **PII),
        f("risque_autres", "Autres à préciser (type de risque)", "text"),
    ],
    "checkbox_groups": [
        g("type_etablissement", "Type de l'établissement sanitaire", "single", ["dr", "csc", "csu", "csca", "csua"]),
        g("mode_couverture", "Mode de la couverture", "single", ["fixe", "mobile"]),
        g("grossesse_a_risque", "Grossesse classée à risque", "bool", ["oui"]),
        g("type_risque", "Si grossesse à risque, type de risque", "multi", ["anemie", "hta", "diabete", "cardiopathie", "metrorragie", "infection", "pre_eclampsie", "eclampsie"], depends_on="grossesse_a_risque == oui"),
    ],
}

ANT_ROWS = ["hta", "diabete", "maladies_hereditaires", "malformations", "allergies", "autres"]
PAGES["IDENTIFICATION_ANTECEDENTS"] = {
    "title": "Identification et antécédents",
    "fields": [
        f("age", "Age", "int", unit="ans", range=[12, 55]),
        f("cin", "CIN", "text", **PII),
        f("niveau_instruction", "Niveau d'instruction", "enum", values=["Aucun", "Primaire", "Collège", "Lycée", "Supérieur"], fuzzy=True),
        f("profession", "Profession", "text"),
        f("adresse", "Adresse", "text", **PII),
        f("telephone", "Téléphone", "text", **PII),
        f("nom_mari", "Nom du Mari", "text", **PII),
        f("profession_mari", "Profession (du mari)", "text"),
        *[f(f"antecedents_familiaux.{r}.{c}", f"Antécédents hérédit. et familiaux, {r}, {c}", "text", group="antecedents_familiaux", aliases="RAS")
          for r in ANT_ROWS for c in ["famille_femme", "famille_mari"]],
        *[f(f"antecedents_femme.{c}", f"Antécédents de la femme, {c}", "text", group="antecedents_femme", aliases="RAS")
          for c in ["medicaux", "chirurgicaux", "gynecologiques"]],
        *[f(f"antecedents_obstetricaux.{r}.{c}", f"Anomalies des grossesses antérieures, {r}, {c}",
            {"nombre": "int", "date": "date", "lieu": "text", "age_gestationnel_sa": "age_gestationnel"}[c], group="antecedents_obstetricaux")
          for r in ["avortement", "accouchement_premature", "mort_foetale_in_utero", "autres"] for c in ["nombre", "date", "lieu", "age_gestationnel_sa"]],
        *[f(f"accouchements_anterieurs.{i}.{c}", f"Accouchement antérieur {i}, {c}",
            {"date": "date", "modalite": "enum", "indication_cesarienne": "text", "complication": "text", "poids_nn_g": "poids_g", "complication_nn": "text"}[c],
            group="accouchements_anterieurs", **({"values": ["Voie basse", "Césarienne", "Forceps", "Ventouse"]} if c == "modalite" else {}),
            not_applicable_if=(f"parite < {i}" if c != "indication_cesarienne" else f"parite < {i} or accouchements_anterieurs.{i}.modalite != 'Césarienne'"))
          for i in range(1, 6) for c in ["date", "modalite", "indication_cesarienne", "complication", "poids_nn_g", "complication_nn"]],
        f("gestation", "Gestation (gestité)", "int", range=[1, 15], note="« IG » = 1"),
        f("parite", "Parité", "int", range=[0, 15], check="parite <= gestation"),
        f("enfants_vivants", "Nombre d'enfants vivants", "int", range=[0, 15], check="enfants_vivants <= parite"),
        f("date_vaccin_rubeole", "Vaccinée contre la rubéole, Le", "date", not_applicable_if="vaccin_rubeole != oui"),
        f("date_vaccin_hepatite_b", "Vaccinée contre l'hépatite B, Le", "date", not_applicable_if="vaccin_hepatite_b != oui"),
        f("frottis_iva", "Frottis cervical / IVA (moins de 3 ans)", "enum", values=["Normal", "Anormal", "Non fait"], fuzzy=True),
    ],
    "checkbox_groups": [
        g("consanguinite", "Consanguinité", "bool", ["oui"]),
        g("grossesse_desiree", "Grossesse désirée", "bool", ["oui"]),
        g("vat", "VAT (doses)", "multi", ["1", "2", "3", "4", "5"], note="sur le vrai carnet : cases cochées cumulatives"),
        g("vaccin_rubeole", "Vaccinée contre la rubéole", "bool", ["oui"]),
        g("vaccin_hepatite_b", "Vaccinée contre l'hépatite B", "bool", ["oui"]),
    ],
}

VISIT_COLS = {"t1_v1": "1er trimestre, Visite 1", "t1_v2": "1er trimestre, Visite 2", "t1_v3": "1er trimestre, Visite 3",
              "t2_v1": "2ème trimestre, Visite 1", "t2_v2": "2ème trimestre, Visite 2", "t2_v3": "2ème trimestre, Visite 3",
              "t3_m7": "3ème trimestre, 7ème mois", "t3_m8": "3ème trimestre, 8ème mois", "t3_m9": "3ème trimestre, 9ème mois"}
VISIT_ROWS = [
    ("rendez_vous", "Rendez-vous", "date", "per_visit"),
    ("venue_le", "Venue le", "date", "per_visit"),
    ("visite_relance", "Visites de relance", "enum:Oui|Non", "per_visit"),
    ("age_gestationnel", "Age probable de la grossesse", "age_gestationnel", "per_visit"),
    ("poids_kg", "Poids (kg)", "poids_kg", "per_visit"),
    ("ta", "TA", "ta", "per_visit"),
    ("anomalies_squelette", "Anomalies du squelette", "text", "per_visit"),
    ("conjonctives", "État des conjonctives", "enum:Normales|Pâles|Décolorées", "per_visit"),
    ("seins", "Examen des seins", "enum:Normaux|Anormaux", "per_visit"),
    ("oedemes", "Œdèmes", "enum:Oui|Non", "per_visit"),
    ("mouvements_actifs", "Mouvements actifs", "enum:Oui|Non", "per_visit"),
    ("hu_cm", "HU (cm)", "longueur_cm", "per_visit"),
    ("bcf", "BCF", "int", "per_visit"),
    ("speculum", "Examen au spéculum", "text", "per_visit"),
    ("tv_col", "TV : état du col", "enum:Fermé|Ouvert|Long fermé|Court|Effacé", "per_visit"),
    ("tv_presentation", "TV : présentation", "enum:Céphalique|Siège|Transverse", "per_visit"),
    ("tv_bassin", "TV : bassin", "enum:Normal|Limite|Rétréci", "per_visit"),
    ("glucosurie", "Glucosurie", "enum:Neg|Pos", "per_visit"),
    ("albuminurie", "Albuminurie", "enum:Neg|Pos", "per_visit"),
    ("rubeole", "Rubéole", "enum:Immune|Non immune", "any_visit"),
    ("toxoplasmose", "Toxoplasmose", "enum:Immune|Non immune", "any_visit"),
    ("syphilis", "Syphilis (TPHA/VDRL)", "enum:Neg|Pos", "any_visit"),
    ("ag_hbs", "Ag HBs", "enum:Neg|Pos", "any_visit"),
    ("vih", "Sérologie VIH", "enum:Neg|Pos", "any_visit"),
    ("hemoglobine", "Hémoglobine (g/dL)", "float", "any_visit"),
    ("plaquettes", "Plaquettes", "text", "any_visit"),
    ("glycemie", "Bilan glycémique (g/L)", "float", "any_visit"),
    ("rai", "RAI (si Rh négatif)", "enum:Neg|Pos", "any_visit"),
    ("autres_bio", "Autres (bio), vrai carnet uniquement", "text", "any_visit"),
    ("fer", "Traitement, Fer", "enum:Oui|Non|Reçu", "per_visit"),
    ("traitement_autres", "Traitement, Autres à préciser (vrai carnet)", "text", "per_visit"),
    ("examinateur", "Examen fait par", "text", "per_visit"),
]
visit_fields = []
for col, col_label in VISIT_COLS.items():
    for row, row_label, typ, card in VISIT_ROWS:
        d = {"key": f"visites.{col}.{row}", "label": f"{col_label}, {row_label}", "cardinality": card, "group": "visites"}
        if typ.startswith("enum:"):
            d["type"] = "enum"
            d["values"] = typ[5:].split("|")
            d["fuzzy"] = True
        else:
            d["type"] = typ
        if row == "rai":
            d["not_applicable_if"] = "rhesus == positif"
        if row in ("mouvements_actifs", "hu_cm", "bcf"):
            d["note"] = "généralement «, » au 1er trimestre"
        visit_fields.append(d)

PAGES["GROSSESSE_ACTUELLE"] = {
    "title": "Grossesse actuelle (tableau longitudinal, 9 colonnes de visite)",
    "note": "Sur le vrai carnet ce tableau est à cheval sur deux pages : les libellés de ligne ne sont que sur la page de gauche ; la page de droite (2e/3e trimestre) s'aligne par la géométrie du gabarit.",
    "fields": [
        f("ddr", "DDR (date des dernières règles)", "date"),
        f("taille_cm", "Taille", "longueur_cm", range=[130, 200]),
        f("dpa", "Date prévue d'accouchement", "date", check="dpa ≈ ddr + 280 j (±7)"),
        f("date_depassement_terme", "Date de dépassement de terme", "date", check="≈ dpa + 7 j"),
        *visit_fields,
    ],
    "checkbox_groups": [
        g("groupage", "Groupage", "single", ["A", "B", "O", "AB"], note="sur le vrai carnet : lettre ENCERCLÉE, pas de case"),
        g("rhesus", "Rhésus", "single", ["negatif", "positif"], note="sur le vrai carnet : mention encerclée"),
    ],
}

PAGES["ACCOUCHEMENT"] = {
    "title": "Déroulement de l'accouchement",
    "fields": [
        f("patiente_nom", "Patiente", "text", **PII),
        f("lieu_surveille_autres", "Lieu (milieu surveillé), Autres", "text"),
        f("lieu_domicile_autres", "Lieu (à domicile), Autres", "text"),
        f("date_accouchement", "Date de l'accouchement", "date"),
        f("indication_cesarienne", "Préciser l'indication (césarienne)", "text", not_applicable_if="mode not in (cesarienne_programmee, cesarienne_urgence)"),
        f("complication_autres", "Si autres à préciser (complications)", "text", not_applicable_if="'autres' not in complications_type"),
        f("sexe", "Sexe", "enum", values=["F", "M"], fuzzy=True),
        f("poids_naissance_g", "Poids à la naissance", "poids_g", range=[500, 6000]),
        f("perimetre_cranien_cm", "Périmètre crânien à la naissance", "longueur_cm", range=[25, 40]),
        f("anomalie", "Anomalie à préciser", "text", aliases="RAS"),
        f("age_gestationnel_sa", "Âge gestationnel", "age_gestationnel", range=[22, 44]),
    ],
    "checkbox_groups": [
        g("lieu", "Lieu", "single", ["milieu_surveille", "domicile"]),
        g("lieu_detail", "Lieu, détail", "single", ["maison_accouchement", "maternite", "clinique_privee", "domicile_assiste_qualifie"], depends_on="lieu"),
        g("mode", "Mode de l'accouchement", "single", ["voie_basse_non_instrumentale", "voie_basse_instrumentale", "cesarienne_programmee", "cesarienne_urgence"]),
        g("mode_instrument", "Voie basse instrumentale, détail", "multi", ["forceps", "ventouse", "episiotomie"], depends_on="mode == voie_basse_instrumentale"),
        g("complications", "Présence de complications", "bool", ["presence"]),
        g("complications_moment", "Moment des complications", "multi", ["accouchement", "suites_de_couches"], depends_on="complications == presence"),
        g("complications_type", "Type de complications", "multi", ["pre_eclampsie", "eclampsie", "hemorragie", "infection", "autres"], depends_on="complications == presence"),
        g("etat_nouveau_ne", "Etat du nouveau-né", "single", ["vivant", "mort_ne", "deces_moins_24h"]),
    ],
}

def pp_mere(title, fenetre):
    return {
        "title": title,
        "fields": [
            f("patiente_nom", "MÈRE, nom", "text", **PII),
            f("date_consultation", "Date de la consultation", "date"),
            f("temperature", "T°", "temperature", range=[35, 42]),
            f("ta", "TA", "ta"),
            f("pouls", "Pouls", "int", range=[40, 180]),
            f("poids_kg", "Poids", "poids_kg"),
            f("cicatrice_cesarienne", "Césarienne : état de la cicatrice", "text", not_applicable_if="cesarienne != oui"),
            f("medicaments", "Notion de prise de médicaments (détail)", "text"),
            f("traitement_autres", "Traitement prescrit, Autres à préciser", "text"),
            f("prochain_rdv", "Prochain rendez-vous le", "date"),
            f("pf_autre_methode", "Planification familiale, Autre à préciser", "text"),
            f("pf_referee", "Référée (détail)", "text"),
            f("pf_refus_raison", "Si la mère ne désire pas une méthode contraceptive : Pourquoi ?", "text", not_applicable_if="'desire_methode' in pf"),
        ],
        "checkbox_groups": [
            g("periode", "Moment de la consultation", "single", ["dans_fenetre", "apres_fenetre"], note=fenetre),
            g("conjonctives", "Etat des conjonctives", "single", ["normales", "decolorees"]),
            g("globe_uterin", "Présence du globe utérin", "bool", ["present"]),
            g("lochies_odeur", "Etat des lochies, odeur", "single", ["fade", "fetide"]),
            g("lochies_aspect", "Etat des lochies, aspect", "single", ["claires", "sanglantes", "jaunatres"]),
            g("perinee", "Etat du périnée", "multi", ["normal", "episiotomie", "episiotomie_reparee", "dechirure"]),
            g("sphincters", "Etat des sphincters (anal et urétral)", "single", ["normal", "anormal"]),
            g("cesarienne", "Césarienne", "bool", ["oui"]),
            g("seins", "Etat des seins", "single", ["normal", "lymphangite", "mastite_abces"]),
            g("mollets", "Etat des mollets", "multi", ["normal", "rouges", "chauds", "douloureux_dorsiflexion"]),
            g("complication", "Présence de complication", "bool", ["presence"]),
            g("complication_type", "Type de complication", "multi", ["hemorragie", "mammaires", "infection", "anemie", "eclampsie", "autres", "phlebite"], depends_on="complication == presence"),
            g("medicaments", "Notion de prise de médicaments", "bool", ["prise"]),
            g("traitement", "Traitement prescrit", "multi", ["fer", "vitamine_a"]),
            g("pf", "Planification familiale", "multi", ["desire_methode", "prescription_faite", "referee"]),
            g("pf_methode", "Si oui, laquelle", "single", ["pilule", "diu"], depends_on="'desire_methode' in pf"),
        ],
    }

def pp_nn(title):
    return {
        "title": title,
        "fields": [
            f("date_consultation", "Date de la consultation", "date"),
            f("age_jours", "Age (jours)", "int", range=[0, 90]),
            f("temperature", "Température", "temperature", range=[34, 42]),
            f("poids_g", "Poids", "poids_g", range=[500, 10000]),
            f("taille_cm", "Taille", "longueur_cm", range=[30, 70]),
            f("perimetre_cranien_cm", "Périmètre crânien", "longueur_cm", range=[25, 45]),
            f("signes_graves_autres", "Signes d'une affection grave, Autres à préciser", "text", aliases="RAS"),
            f("lesions_autres", "Contusions, lésions, malformations, Autres à préciser", "text", aliases="RAS"),
            f("vu_par", "Vu par", "text"),
            f("decision", "Décision prise", "text"),
            f("traitement", "Traitement prescrit", "text", aliases="RAS"),
            f("etablissement_reference", "Transfert, établissement de référence", "text", not_applicable_if="transfert != oui"),
            f("prochain_rdv", "Revenir pour une visite de suivi nécessaire le", "date"),
        ],
        "checkbox_groups": [
            g("etat", "Nouveau-né prématuré / hypotrophe", "multi", ["premature", "hypotrophe"]),
            g("allaitement", "Allaitement", "single", ["exclusif_sein", "artificiel", "mixte"]),
            g("signes_graves", "Signes d'une affection grave", "multi", ["convulsions", "refus_teter", "hematemeses", "melaenas", "diarrhee", "ictere", "tirage_sous_costal", "toux", "rythme_respiratoire_anormal", "fievre", "hypothermie"]),
            g("lesions", "Contusions, lésions traumatiques et malformations", "multi", ["bosse_serosanguine_cephalohematome", "luxation_hanche", "mobilite_membre_diminuee"]),
            g("evaluation_allaitement", "Evaluation de l'allaitement maternel", "single", ["normal", "a_problemes"]),
            g("vaccins", "Vaccins administrés ce jour", "multi", ["bcg", "hb"]),
            g("vitamine_d", "Supplémentation en vitamine D", "bool", ["oui"]),
            g("complications", "Présence de complications et de malformation", "multi", ["ictere", "infection", "conjonctivite", "traumatisme", "malformation", "autres"]),
            g("transfert", "Transfert", "bool", ["oui"]),
        ],
    }

PAGES["PP_PRECOCE_MERE"] = pp_mere("Consultation du post-partum précoce, Mère", "dans_fenetre = entre le 7ème et 8ème jour ; apres_fenetre = après le 8ème jour")
PAGES["PP_PRECOCE_NOUVEAU_NE"] = pp_nn("Consultation du post-partum précoce, Nouveau-né")
PAGES["PP_TARDIF_MERE"] = pp_mere("Consultation du post-partum tardif, Mère", "dans_fenetre = entre le 40ème et 50ème jour ; apres_fenetre = après le 50ème jour")
PAGES["PP_TARDIF_NOUVEAU_NE"] = pp_nn("Consultation du post-partum tardif, Nouveau-né")

# --------------------------------------------------------------------------- #
# 4. Cohérence inter-champs (alimente A_REVISER et la confiance)
# --------------------------------------------------------------------------- #
CONSISTENCY_CHECKS = [
    {"id": "parite_le_gestation", "expr": "parite <= gestation", "on_fail": "A_REVISER sur les deux champs"},
    {"id": "vivants_le_parite", "expr": "enfants_vivants <= parite", "on_fail": "A_REVISER"},
    {"id": "nb_accouchements", "expr": "nombre d'Accouch. renseignés == parite", "on_fail": "A_REVISER (ou NON_APPLICABLE pour les colonnes > parité)"},
    {"id": "dpa_vs_ddr", "expr": "|dpa - (ddr + 280 j)| <= 7 j", "on_fail": "A_REVISER sur dpa et ddr"},
    {"id": "depassement", "expr": "|date_depassement_terme - (dpa + 7 j)| <= 2 j", "on_fail": "A_REVISER"},
    {"id": "ag_vs_date", "expr": "|age_gestationnel(visite) - (venue_le - ddr)/7| <= 1 SA", "on_fail": "A_REVISER sur age_gestationnel ou venue_le"},
    {"id": "visites_chrono", "expr": "venue_le croissant de t1_v1 à t3_m9", "on_fail": "A_REVISER"},
    {"id": "ta_plausible", "expr": "60 <= sys <= 250 and 30 <= dia <= 150 and sys > dia", "on_fail": "A_REVISER ; si valeurs à 1 chiffre (12/7) → ×10"},
    {"id": "poids_nn", "expr": "500 <= poids_naissance_g <= 6000", "on_fail": "A_REVISER (kg ↔ g ?)"},
    {"id": "rai_rhesus", "expr": "rhesus == positif → rai NON_APPLICABLE", "on_fail": "statut forcé"},
    {"id": "cesarienne_indication", "expr": "mode cesarienne → indication attendue ; sinon NON_APPLICABLE", "on_fail": "statut forcé"},
    {"id": "age_jours_pp", "expr": "précoce : 5 <= age_jours <= 15 ; tardif : 35 <= age_jours <= 60", "on_fail": "A_REVISER"},
    {"id": "date_consult_vs_accouchement", "expr": "date_consultation - date_accouchement ≈ age_jours", "on_fail": "A_REVISER"},
]

# --------------------------------------------------------------------------- #
# 5. Cycle de vie d'un enregistrement (machine à états)
# --------------------------------------------------------------------------- #
LIFECYCLE = {
    "states": ["CAPTURE", "EN_ATTENTE_IA", "TRAITE_IA", "A_REVISER", "VALIDE", "PATIENTE_LIEE", "ENREGISTRE", "SYNCHRONISE"],
    "failure_states": ["ECHEC_TRAITEMENT", "ECHEC_SYNCHRO", "DOUBLON_SUSPECTE", "REVISION_MANUELLE_REQUISE"],
    "transitions": [
        ["CAPTURE", "EN_ATTENTE_IA", "photo acceptée (qualité OK), chiffrée et mise en file locale"],
        ["EN_ATTENTE_IA", "TRAITE_IA", "connexion disponible, extraction réussie"],
        ["EN_ATTENTE_IA", "ECHEC_TRAITEMENT", "erreur IA / timeout (retry avec backoff, max N) → REVISION_MANUELLE_REQUISE si IA indisponible durablement"],
        ["TRAITE_IA", "A_REVISER", "au moins un champ A_REVISER / ILLISIBLE, ou doute de l'agent"],
        ["TRAITE_IA", "VALIDE", "tous les champs CONNU / NON_FOURNI / NON_APPLICABLE et sage-femme confirme"],
        ["A_REVISER", "VALIDE", "sage-femme a confirmé / corrigé / saisi chaque champ douteux"],
        ["A_REVISER", "CAPTURE", "sage-femme choisit « Reprendre la photo »"],
        ["REVISION_MANUELLE_REQUISE", "VALIDE", "saisie manuelle complète par questions"],
        ["VALIDE", "PATIENTE_LIEE", "code sage-femme reconnu OU choix explicite [Patiente 1] [Patiente 2] [Aucune, créer]"],
        ["VALIDE", "DOUBLON_SUSPECTE", "correspondance plausible mais non tranchée ([Je ne sais pas]) ou registre déjà numérisé"],
        ["DOUBLON_SUSPECTE", "PATIENTE_LIEE", "sage-femme tranche ; si registre rephotographié : choisit les champs à mettre à jour"],
        ["PATIENTE_LIEE", "ENREGISTRE", "fusion dans le profil longitudinal, image masquée attachée"],
        ["ENREGISTRE", "SYNCHRONISE", "envoi au serveur accusé"],
        ["ENREGISTRE", "ECHEC_SYNCHRO", "erreur réseau (reste en file, retry), jamais de perte"],
        ["ECHEC_SYNCHRO", "SYNCHRONISE", "retry réussi"],
    ],
    "invariants": [
        "Un enregistrement n'est jamais supprimé par le système ; toute capture a un état.",
        "Le stockage local est chiffré ; la file survit à la fermeture de l'app.",
        "Les identifiants internes (record_id, patient_id) sont des UUID v4, jamais dérivés d'informations personnelles.",
        "L'image d'origine est stockée masquée (PII floutée) avec record_id, date de capture, midwife_id, statut ; accès par rôle.",
    ],
}

PATIENT_LINKING = {
    "code_sage_femme": "code aléatoire court (ex. 4 lettres + 2 chiffres) écrit par la sage-femme sur le carnet (N° de fiche sur le carnet marocain) ; clé primaire de liaison",
    "signaux_secondaires_non_identifiants": ["etablissement", "age", "gestation", "parite", "ddr", "dpa", "date_accouchement", "groupage/rhesus"],
    "regle": "code exact → proposer la correspondance ; sinon score de similarité sur les signaux secondaires → si score >= seuil, proposer [Patiente 1] [Patiente 2] [Aucune, créer] [Je ne sais pas] ; jamais de création automatique quand une correspondance est plausible",
}

SCHEMA = {
    "name": "dayone_maternal_registry",
    "version": "0.1.0",
    "source_form": "Fiche de surveillance de la grossesse et du post-partum, Ministère de la Santé, Royaume du Maroc (carnet rose)",
    "pages_per_booklet": list(PAGES.keys()),
    "statuses": STATUSES,
    "status_rules": STATUS_RULES,
    "field_record": FIELD_RECORD,
    "types": TYPES,
    "aliases": ALIASES,
    "status_codes": STATUS_CODES,
    "pages": PAGES,
    "consistency_checks": CONSISTENCY_CHECKS,
    "record_lifecycle": LIFECYCLE,
    "patient_linking": PATIENT_LINKING,
}


def write_md(path: Path):
    L = []
    L.append("# Schéma de champs et modèle de statuts, Carnet maternel (DayOne)\n")
    L.append("Généré par `build_schema.py`. Source : `registry_schema.json` (même contenu, lisible par machine). Les clés sont celles de `ground_truth/ground_truth.json`.\n")
    L.append("## 1. Statuts de champ\n")
    L.append("| Statut | Sens |\n|---|---|")
    for k, v in STATUSES.items():
        L.append(f"| `{k}` | {v} |")
    L.append("\nRègles :\n")
    for r in STATUS_RULES:
        L.append(f"- {r}")
    L.append("\n### Codes manuscrits qui portent un statut\n")
    L.append("| Écrit sur le papier | Statut | Note |\n|---|---|---|")
    for k, v in STATUS_CODES.items():
        L.append(f"| `{k}` | {v['status']} | {v['note']} |")
    L.append("\n## 2. Format d'un champ extrait\n")
    L.append("```json\n" + json.dumps(FIELD_RECORD, ensure_ascii=False, indent=2) + "\n```\n")
    L.append("### Confiance\n")
    L.append("- `confidence` doit être **calibrée** : parmi les champs annoncés à 0.9, ~90 % doivent être justes. Mesurer l'ECE sur le jeu de test.")
    L.append("- Recette simple et robuste : accord entre deux lectures indépendantes (deux prompts / deux modèles / deux recadrages) × score de correspondance au vocabulaire fermé × pénalité des règles de cohérence.")
    L.append("- Seuils par défaut : `CONNU` ≥ 0.85 ; `A_REVISER` ∈ [0.5, 0.85) ; `ILLISIBLE` < 0.5 avec encre détectée.\n")
    L.append("## 3. Types et normalisation\n")
    L.append("| Type | Détail |\n|---|---|")
    for k, v in TYPES.items():
        L.append(f"| `{k}` | {json.dumps(v, ensure_ascii=False)} |")
    L.append("\nAlias (forme canonique ← variantes vues sur le papier) :\n")
    for k, v in ALIASES.items():
        L.append(f"- **{k}** ← {', '.join(v)}")
    L.append("\n## 4. Pages et champs\n")
    for pt, pg in PAGES.items():
        L.append(f"### `{pt}`, {pg['title']}\n")
        if pg.get("note"):
            L.append(f"> {pg['note']}\n")
        L.append("Champs (texte/valeur) :\n")
        L.append("| Clé | Libellé | Type | Détails |\n|---|---|---|---|")
        fields = pg["fields"]
        if pt == "GROSSESSE_ACTUELLE":
            fields = [x for x in fields if not x["key"].startswith("visites.")]
        for x in fields:
            det = []
            if x.get("pii"):
                det.append("**PII, jamais stocké**")
            if x.get("values"):
                det.append("valeurs : " + " / ".join(x["values"]))
            if x.get("range"):
                det.append(f"plage {x['range']}")
            if x.get("not_applicable_if"):
                det.append(f"NON_APPLICABLE si `{x['not_applicable_if']}`")
            if x.get("check"):
                det.append(f"contrôle : {x['check']}")
            if x.get("note"):
                det.append(x["note"])
            if x.get("aliases"):
                det.append(f"alias {x['aliases']}")
            L.append(f"| `{x['key']}` | {x['label']} | {x['type']} | {' ; '.join(det)} |")
        if pt == "GROSSESSE_ACTUELLE":
            L.append("\nTableau des visites : clé = `visites.<colonne>.<ligne>` avec colonnes " + ", ".join(f"`{c}`" for c in VISIT_COLS) + " et lignes :\n")
            L.append("| Ligne | Libellé | Type | Cardinalité |\n|---|---|---|---|")
            for row, lab, typ, card in VISIT_ROWS:
                L.append(f"| `{row}` | {lab} | {typ} | {card} |")
            L.append("\n`per_visit` = une valeur attendue par visite ; `any_visit` = examen fait une (ou deux) fois, les autres colonnes sont NON_FOURNI, pas ILLISIBLE.\n")
        if pg.get("checkbox_groups"):
            L.append("\nCases à cocher (groupes) :\n")
            L.append("| Groupe | Libellé | Choix | Options | Détails |\n|---|---|---|---|---|")
            for x in pg["checkbox_groups"]:
                det = []
                if x.get("depends_on"):
                    det.append(f"dépend de `{x['depends_on']}`")
                if x.get("note"):
                    det.append(x["note"])
                L.append(f"| `{x['key']}` | {x['label']} | {x['choice']} | {', '.join(x['options'])} | {' ; '.join(det)} |")
        L.append("")
    L.append("## 5. Contrôles de cohérence inter-champs\n")
    L.append("| Id | Règle | Si faux |\n|---|---|---|")
    for c in CONSISTENCY_CHECKS:
        L.append(f"| `{c['id']}` | {c['expr']} | {c['on_fail']} |")
    L.append("\n## 6. Cycle de vie d'un enregistrement\n")
    L.append("États : " + " → ".join(f"`{s}`" for s in LIFECYCLE["states"]))
    L.append("\nÉtats d'échec : " + ", ".join(f"`{s}`" for s in LIFECYCLE["failure_states"]) + "\n")
    L.append("| De | Vers | Condition |\n|---|---|---|")
    for a, b, c in LIFECYCLE["transitions"]:
        L.append(f"| `{a}` | `{b}` | {c} |")
    L.append("\nInvariants :\n")
    for i in LIFECYCLE["invariants"]:
        L.append(f"- {i}")
    L.append("\n## 7. Liaison patiente\n")
    for k, v in PATIENT_LINKING.items():
        L.append(f"- **{k}** : {v if isinstance(v, str) else ', '.join(v)}")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    here = Path(__file__).parent
    (here / "registry_schema.json").write_text(json.dumps(SCHEMA, ensure_ascii=False, indent=1), encoding="utf-8")
    write_md(here / "SCHEMA.md")
    n_fields = sum(len(p["fields"]) for p in PAGES.values())
    n_groups = sum(len(p.get("checkbox_groups", [])) for p in PAGES.values())
    print(f"registry_schema.json : {len(PAGES)} pages, {n_fields} champs, {n_groups} groupes de cases")
