"""
Liaison patiente : rattacher une page à un profil existant.

1. Code sage-femme (écrit sur le carnet) : correspondance exacte => candidat n°1.
2. Sinon / en plus : similarité sur des signaux NON identifiants (établissement,
   âge, gestité/parité, DDR, DPA, date d'accouchement).
Jamais de création automatique quand une correspondance est plausible : l'agent
propose [Patiente 1] [Patiente 2] [Aucune, créer] [Je ne sais pas].
"""
from __future__ import annotations

from dataclasses import dataclass

from .normalize import parse_ddmmyyyy
from .schema import linking_signals

PLAUSIBLE = 0.45     # au-dessus : on propose la correspondance
STRONG = 0.9         # code exact


@dataclass
class Candidate:
    patient_id: str
    score: float
    reasons: list[str]
    label: str          # description non identifiante pour les boutons


def _val(fields: dict, key: str):
    f = fields.get(key) if fields else None
    if f and f.get("status") in ("CONNU", "A_REVISER") and f.get("value") not in (None, ""):
        return str(f["value"])
    return None


def signals_from_page(page_type: str, fields: dict) -> dict:
    """Extrait de la page les signaux de liaison (clés plates)."""
    out = {}
    for k in linking_signals(page_type):
        v = _val(fields, k)
        if v is not None:
            out[k] = v
    return out


def merge_signals(profile: dict, page_type: str, fields: dict) -> dict:
    sig = dict(profile.get("signals", {}))
    sig.update(signals_from_page(page_type, fields))
    return sig


def _date_close(a: str | None, b: str | None, days: int) -> bool | None:
    da, db = parse_ddmmyyyy(a), parse_ddmmyyyy(b)
    if not da or not db:
        return None
    return abs((da - db).days) <= days


def similarity(sig_a: dict, sig_b: dict) -> tuple[float, list[str]]:
    """Score 0..1 entre deux jeux de signaux non identifiants."""
    score, weight, reasons = 0.0, 0.0, []
    checks = [
        ("ddr", 0.35, lambda a, b: _date_close(a, b, 7)),
        ("dpa", 0.25, lambda a, b: _date_close(a, b, 7)),
        ("date_accouchement", 0.3, lambda a, b: _date_close(a, b, 1)),
        ("numero_fiche", 0.4, lambda a, b: a.replace(" ", "") == b.replace(" ", "")),
        ("etablissement", 0.1, lambda a, b: a.lower() == b.lower()),
        ("age", 0.1, lambda a, b: abs(int(float(a)) - int(float(b))) <= 1),
        ("gestation", 0.1, lambda a, b: a == b),
        ("parite", 0.1, lambda a, b: a == b),
        ("enfants_vivants", 0.05, lambda a, b: a == b),
    ]
    for key, w, fn in checks:
        a, b = sig_a.get(key), sig_b.get(key)
        if a is None or b is None:
            continue
        try:
            r = fn(a, b)
        except Exception:
            r = None
        if r is None:
            continue
        weight += w
        if r:
            score += w
            reasons.append(f"{key} identique")
        else:
            reasons.append(f"{key} différent")
    if weight == 0:
        return 0.0, ["aucun signal comparable"]
    return round(score / weight * min(1.0, weight / 0.5), 2), reasons


def describe(profile: dict) -> str:
    """Étiquette courte, non identifiante, pour un bouton."""
    s = profile.get("signals", {})
    bits = []
    if profile.get("code"):
        bits.append(f"code {profile['code']}")
    if s.get("age"):
        bits.append(f"{s['age']} ans")
    if s.get("gestation") and s.get("parite"):
        bits.append(f"G{s['gestation']}P{s['parite']}")
    if s.get("ddr"):
        bits.append(f"DDR {s['ddr']}")
    if s.get("etablissement"):
        bits.append(s["etablissement"])
    n = profile.get("n_pages", 0)
    bits.append(f"{n} page{'s' if n > 1 else ''}")
    return " · ".join(bits)


def find_candidates(patients: list[dict], code: str | None, page_type: str, fields: dict, max_n: int = 2) -> list[Candidate]:
    sig = signals_from_page(page_type, fields)
    code = (code or "").strip().upper() or None
    out = []
    for p in patients:
        prof = p.get("profile", {})
        reasons, score = [], 0.0
        if code and p.get("code") and p["code"] == code:
            score, reasons = STRONG, ["code sage-femme identique"]
            s2, r2 = similarity(sig, prof.get("signals", {}))
            if r2 and r2 != ["aucun signal comparable"]:
                score = round(min(1.0, STRONG + 0.1 * s2), 2)
                reasons += r2
        else:
            score, reasons = similarity(sig, prof.get("signals", {}))
            if code and p.get("code") and p["code"] != code:
                score = round(score * 0.5, 2)
                reasons.append("code différent")
        if score >= PLAUSIBLE:
            out.append(Candidate(p["id"], score, reasons, describe({**prof, "code": p.get("code")})))
    out.sort(key=lambda c: -c.score)
    return out[:max_n]
