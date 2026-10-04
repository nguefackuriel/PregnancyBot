"""Cycle de vie d'un enregistrement : machine à états (voir SCHEMA.md §6)."""
from __future__ import annotations

STATES = ["CAPTURE", "EN_ATTENTE_IA", "TRAITE_IA", "A_REVISER", "VALIDE", "PATIENTE_LIEE", "ENREGISTRE", "SYNCHRONISE"]
FAILURES = ["ECHEC_TRAITEMENT", "ECHEC_SYNCHRO", "DOUBLON_SUSPECTE", "REVISION_MANUELLE_REQUISE"]
ALL = STATES + FAILURES

TRANSITIONS = {
    "CAPTURE": {"EN_ATTENTE_IA", "REVISION_MANUELLE_REQUISE"},
    "EN_ATTENTE_IA": {"TRAITE_IA", "ECHEC_TRAITEMENT", "REVISION_MANUELLE_REQUISE"},
    "ECHEC_TRAITEMENT": {"EN_ATTENTE_IA", "REVISION_MANUELLE_REQUISE"},
    "TRAITE_IA": {"A_REVISER", "VALIDE"},
    "A_REVISER": {"VALIDE", "CAPTURE", "A_REVISER"},
    "REVISION_MANUELLE_REQUISE": {"VALIDE", "EN_ATTENTE_IA"},
    "VALIDE": {"PATIENTE_LIEE", "DOUBLON_SUSPECTE"},
    "DOUBLON_SUSPECTE": {"PATIENTE_LIEE", "VALIDE"},
    "PATIENTE_LIEE": {"ENREGISTRE"},
    "ENREGISTRE": {"SYNCHRONISE", "ECHEC_SYNCHRO"},
    "ECHEC_SYNCHRO": {"SYNCHRONISE", "ENREGISTRE"},
    "SYNCHRONISE": set(),
}

LABELS_FR = {
    "CAPTURE": "Capturé", "EN_ATTENTE_IA": "En attente de traitement IA", "TRAITE_IA": "Traité par l'IA",
    "A_REVISER": "À réviser", "VALIDE": "Validé", "PATIENTE_LIEE": "Patiente liée", "ENREGISTRE": "Enregistré",
    "SYNCHRONISE": "Synchronisé", "ECHEC_TRAITEMENT": "Échec de traitement", "ECHEC_SYNCHRO": "Échec de synchronisation",
    "DOUBLON_SUSPECTE": "Doublon suspecté", "REVISION_MANUELLE_REQUISE": "Révision manuelle requise",
}


class IllegalTransition(Exception):
    pass


def can(frm: str, to: str) -> bool:
    return to in TRANSITIONS.get(frm, set())


def check(frm: str, to: str) -> None:
    if frm not in ALL or to not in ALL:
        raise IllegalTransition(f"état inconnu : {frm} -> {to}")
    if not can(frm, to):
        raise IllegalTransition(f"transition interdite : {frm} -> {to}")


def is_terminal(state: str) -> bool:
    return state == "SYNCHRONISE"


def needs_connectivity(state: str) -> bool:
    """États dont la sortie demande le réseau (traitement IA distant, synchro)."""
    return state in ("EN_ATTENTE_IA", "ECHEC_TRAITEMENT", "ENREGISTRE", "ECHEC_SYNCHRO")
