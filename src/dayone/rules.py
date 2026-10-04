"""
Règles de dépendance (NON_APPLICABLE) et contrôles de cohérence inter-champs.

Entrée : fields = {key: {"value", "status", ...}}, choices = {groupe: [options]}
Sortie : liste de Flag(keys, rule_id, message) ; les champs cités passent en
A_REVISER (si CONNU) avec une pénalité de confiance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import parse_ddmmyyyy


@dataclass
class Flag:
    keys: list[str]
    rule: str
    message: str


def _num(fields, key):
    f = fields.get(key)
    if not f or f.get("status") not in ("CONNU", "A_REVISER") or f.get("value") in (None, ""):
        return None
    try:
        return float(str(f["value"]).replace(",", "."))
    except ValueError:
        return None


def _date(fields, key):
    f = fields.get(key)
    if not f or f.get("status") not in ("CONNU", "A_REVISER"):
        return None
    return parse_ddmmyyyy(f.get("value"))


def apply_not_applicable(page_type: str, fields: dict, choices: dict) -> list[str]:
    """Force NON_APPLICABLE selon les dépendances du schéma. Retourne les clés modifiées."""
    changed = []

    def na(key, why):
        f = fields.get(key)
        if f and f.get("status") in ("NON_FOURNI", "INCONNU", "ILLISIBLE"):
            f["status"] = "NON_APPLICABLE"
            f["value"] = None
            f["source"] = "rule"
            f["notes"] = why
            changed.append(key)

    parite = _num(fields, "parite")
    if page_type == "IDENTIFICATION_ANTECEDENTS":
        for i in range(1, 6):
            if parite is not None and parite < i:
                for c in ("date", "modalite", "indication_cesarienne", "complication", "poids_nn_g", "complication_nn"):
                    na(f"accouchements_anterieurs.{i}.{c}", f"parité = {int(parite)}")
            modal = fields.get(f"accouchements_anterieurs.{i}.modalite", {}).get("value") or ""
            if modal and "sarienne" not in modal.lower():
                na(f"accouchements_anterieurs.{i}.indication_cesarienne", "accouchement par voie basse")
        if not choices.get("vaccin_rubeole"):
            na("date_vaccin_rubeole", "case non cochée")
        if not choices.get("vaccin_hepatite_b"):
            na("date_vaccin_hepatite_b", "case non cochée")
    if page_type == "GROSSESSE_ACTUELLE" and choices.get("rhesus") == ["positif"]:
        for k in list(fields):
            if k.endswith(".rai"):
                na(k, "Rhésus positif")
    if page_type == "ACCOUCHEMENT":
        mode = choices.get("mode", [])
        if mode and not any("cesarienne" in m for m in mode):
            na("indication_cesarienne", "accouchement par voie basse")
        if "autres" not in choices.get("complications_type", []):
            na("complication_autres", "« Autres » non coché")
    if page_type.endswith("_MERE"):
        if not choices.get("cesarienne"):
            na("cicatrice_cesarienne", "pas de césarienne")
        if "desire_methode" in choices.get("pf", []):
            na("pf_refus_raison", "méthode désirée")
    if page_type.endswith("_NOUVEAU_NE") and not choices.get("transfert"):
        na("etablissement_reference", "pas de transfert")
    if page_type == "COUVERTURE" and not choices.get("grossesse_a_risque"):
        na("risque_autres", "grossesse non classée à risque")
    return changed


def check_consistency(page_type: str, fields: dict, choices: dict) -> list[Flag]:
    flags: list[Flag] = []
    g, p, v = _num(fields, "gestation"), _num(fields, "parite"), _num(fields, "enfants_vivants")
    if g is not None and p is not None and p > g:
        flags.append(Flag(["gestation", "parite"], "parite_le_gestation", f"parité {p:g} > gestité {g:g}"))
    if p is not None and v is not None and v > p:
        flags.append(Flag(["parite", "enfants_vivants"], "vivants_le_parite", f"enfants vivants {v:g} > parité {p:g}"))
    ddr, dpa, dep = _date(fields, "ddr"), _date(fields, "dpa"), _date(fields, "date_depassement_terme")
    if ddr and dpa and abs((dpa - ddr).days - 280) > 7:
        flags.append(Flag(["ddr", "dpa"], "dpa_vs_ddr", f"DPA − DDR = {(dpa - ddr).days} j (attendu ≈ 280)"))
    if dpa and dep and abs((dep - dpa).days - 7) > 2:
        flags.append(Flag(["dpa", "date_depassement_terme"], "depassement", f"dépassement − DPA = {(dep - dpa).days} j (attendu 7)"))
    if page_type == "GROSSESSE_ACTUELLE" and ddr:
        prev = None
        for col in ("t1_v1", "t1_v2", "t1_v3", "t2_v1", "t2_v2", "t2_v3", "t3_m7", "t3_m8", "t3_m9"):
            d = _date(fields, f"visites.{col}.venue_le")
            ag = fields.get(f"visites.{col}.age_gestationnel", {})
            if d:
                if prev and d < prev:
                    flags.append(Flag([f"visites.{col}.venue_le"], "visites_chrono", "date de visite antérieure à la précédente"))
                prev = d
                m = re.match(r"(\d+) SA", str(ag.get("value") or ""))
                if m and ag.get("status") in ("CONNU", "A_REVISER"):
                    expected = (d - ddr).days / 7
                    if abs(int(m.group(1)) - expected) > 1.5:
                        flags.append(Flag([f"visites.{col}.age_gestationnel", f"visites.{col}.venue_le"], "ag_vs_date",
                                          f"{m.group(1)} SA vs {expected:.1f} SA calculé depuis la DDR"))
    for k, f in fields.items():
        if (k == "ta" or k.endswith(".ta")) and f.get("status") in ("CONNU", "A_REVISER") and f.get("value"):
            m = re.match(r"(\d+)/(\d+)$", str(f["value"]))
            if m:
                s, d = int(m.group(1)), int(m.group(2))
                if not (60 <= s <= 250 and 30 <= d <= 150 and s > d):
                    flags.append(Flag([k], "ta_plausible", f"TA {s}/{d} invraisemblable"))
    w = _num(fields, "poids_naissance_g")
    if w is not None and not 500 <= w <= 6000:
        flags.append(Flag(["poids_naissance_g"], "poids_nn", f"poids {w:g} g hors plage"))
    aj = _num(fields, "age_jours")
    if aj is not None:
        lo, hi = (5, 15) if "PRECOCE" in page_type else (35, 60)
        if not lo <= aj <= hi:
            flags.append(Flag(["age_jours"], "age_jours_pp", f"{aj:g} jours hors fenêtre {lo}-{hi}"))
    return flags
