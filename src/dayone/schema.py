"""Chargement du schéma de champs (registry_schema.json) et du gabarit géométrique (templates.json)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

STATUSES = ("CONNU", "A_REVISER", "ILLISIBLE", "NON_FOURNI", "NON_APPLICABLE", "INCONNU")
PAGE_TYPES = ("COUVERTURE", "IDENTIFICATION_ANTECEDENTS", "GROSSESSE_ACTUELLE", "ACCOUCHEMENT",
              "PP_PRECOCE_MERE", "PP_PRECOCE_NOUVEAU_NE", "PP_TARDIF_MERE", "PP_TARDIF_NOUVEAU_NE")
PAGE_LABELS = {
    "COUVERTURE": "Couverture", "IDENTIFICATION_ANTECEDENTS": "Identification et antécédents",
    "GROSSESSE_ACTUELLE": "Grossesse actuelle", "ACCOUCHEMENT": "Déroulement de l'accouchement",
    "PP_PRECOCE_MERE": "Post-partum précoce, mère", "PP_PRECOCE_NOUVEAU_NE": "Post-partum précoce, nouveau-né",
    "PP_TARDIF_MERE": "Post-partum tardif, mère", "PP_TARDIF_NOUVEAU_NE": "Post-partum tardif, nouveau-né",
}
# seuils de confiance (voir SCHEMA.md §Confiance)
T_CONNU = 0.85
T_REVISION = 0.50


@lru_cache(maxsize=1)
def load_schema(path: str | None = None) -> dict:
    p = Path(path) if path else DATA / "schema" / "registry_schema.json"
    return json.loads(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_templates(path: str | None = None) -> dict:
    p = Path(path) if path else DATA / "templates.json"
    return json.loads(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def fields_of(page_type: str) -> dict[str, dict]:
    """key -> définition de champ (type, values, pii, not_applicable_if, ...)."""
    return {f["key"]: f for f in load_schema()["pages"][page_type]["fields"]}


@lru_cache(maxsize=None)
def groups_of(page_type: str) -> dict[str, dict]:
    return {g["key"]: g for g in load_schema()["pages"][page_type].get("checkbox_groups", [])}


def field_def(page_type: str, key: str) -> dict:
    return fields_of(page_type).get(key, {"key": key, "label": key, "type": "text"})


def is_pii(page_type: str, key: str) -> bool:
    return bool(field_def(page_type, key).get("pii"))


def important_fields(page_type: str) -> list[str]:
    """Champs qu'on propose en saisie manuelle quand l'IA est indisponible (sous-ensemble)."""
    core = {
        "COUVERTURE": ["numero_fiche", "region", "province", "etablissement"],
        "IDENTIFICATION_ANTECEDENTS": ["age", "gestation", "parite", "enfants_vivants", "niveau_instruction"],
        "GROSSESSE_ACTUELLE": ["ddr", "dpa", "taille_cm"],
        "ACCOUCHEMENT": ["date_accouchement", "sexe", "poids_naissance_g", "perimetre_cranien_cm", "age_gestationnel_sa"],
        "PP_PRECOCE_MERE": ["date_consultation", "temperature", "ta", "pouls", "poids_kg", "prochain_rdv"],
        "PP_TARDIF_MERE": ["date_consultation", "temperature", "ta", "pouls", "poids_kg", "prochain_rdv"],
        "PP_PRECOCE_NOUVEAU_NE": ["date_consultation", "age_jours", "temperature", "poids_g", "taille_cm", "perimetre_cranien_cm", "prochain_rdv"],
        "PP_TARDIF_NOUVEAU_NE": ["date_consultation", "age_jours", "temperature", "poids_g", "taille_cm", "perimetre_cranien_cm", "prochain_rdv"],
    }
    return core.get(page_type, [])


def linking_signals(page_type: str) -> list[str]:
    """Champs non identifiants utilisés pour proposer des correspondances patiente."""
    return {
        "COUVERTURE": ["numero_fiche", "etablissement", "province"],
        "IDENTIFICATION_ANTECEDENTS": ["age", "gestation", "parite", "enfants_vivants"],
        "GROSSESSE_ACTUELLE": ["ddr", "dpa"],
        "ACCOUCHEMENT": ["date_accouchement"],
    }.get(page_type, [])
