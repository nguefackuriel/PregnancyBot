"""
Normalisation d'une lecture brute vers (valeur canonique, statut, score de correspondance, note).

Le score ∈ [0,1] mesure à quel point la lecture brute « ressemble » à une
valeur valide pour le type du champ ; il entre dans la confiance finale.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

from .schema import load_schema


@dataclass
class Norm:
    value: str | None
    status: str          # CONNU | NON_FOURNI | NON_APPLICABLE | ILLISIBLE
    score: float         # 0..1
    note: str | None = None


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s.lower().strip())


def _aliases() -> dict[str, str]:
    """variante repliée -> forme canonique (table ALIASES du schéma)."""
    out = {}
    for canon, variants in load_schema()["aliases"].items():
        for v in variants:
            out[fold(v)] = canon
        out[fold(canon)] = canon
    return out


ALIASES = None
BLANK_MARKS = {"", "-", "—", "–", "/", "//", "—-", "x", "∅"}
NA_MARKS = {"—", "–", "-", "/", "//", "n/a", "na", "sans objet", "so"}
NF_MARKS = {"nf", "non fait", "n.f", "n.f.", "nonfait", "pas fait", "non realise", "non réalisé"}
ILLEGIBLE_MARKS = {"?", "??", "???", "illisible"}


def _enum_match(raw: str, values: list[str]) -> tuple[str | None, float]:
    global ALIASES
    if ALIASES is None:
        ALIASES = _aliases()
    f = fold(raw)
    if f in ALIASES and ALIASES[f] in values:
        return ALIASES[f], 1.0
    # correspondance sur les valeurs elles-mêmes (sans accents, préfixes)
    folded = {fold(v): v for v in values}
    if f in folded:
        return folded[f], 1.0
    for fv, v in folded.items():
        if len(f) >= 3 and (fv.startswith(f) or f.startswith(fv)):
            return v, 0.9
    best, score = None, 0.0
    for fv, v in folded.items():
        r = difflib.SequenceMatcher(None, f, fv).ratio()
        if r > score:
            best, score = v, r
    # « P les » (glyphe manquant) ~ « pales » : ratio ~0.9 ; on accepte >= 0.75
    if score >= 0.75:
        return best, round(score, 2)
    return None, round(score, 2)


DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{2,4})\s*$")


def _date(raw: str) -> tuple[str | None, float]:
    m = DATE_RE.match(raw.replace(" ", "").replace("l", "1").replace("O", "0") if len(raw) <= 12 else raw)
    if not m:
        return None, 0.2 if re.search(r"\d", raw) else 0.0
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    if len(y) == 2:
        y = "20" + y
    if not (1 <= d <= 31 and 1 <= mo <= 12 and 1990 <= int(y) <= 2040):
        return None, 0.3
    return f"{d:02d}/{mo:02d}/{y}", 1.0 if m.group(0).strip() == raw.strip() else 0.9


def _number(raw: str, as_int: bool) -> tuple[str | None, float]:
    s = raw.replace(",", ".").replace("O", "0").replace("o", "0")
    m = re.search(r"-?\d+(\.\d+)?", s)
    if not m:
        return None, 0.0
    v = m.group(0)
    clean = re.sub(r"[^\d.]", "", s)
    score = 1.0 if clean == v else 0.7
    if as_int:
        if "." in v:
            return str(int(round(float(v)))), 0.6
        return str(int(v)), score
    return str(float(v)).rstrip("0").rstrip(".") if "." in v else v, score


def _ta(raw: str) -> tuple[str | None, float]:
    m = re.search(r"(\d{1,3})\s*[/|\\]\s*(\d{1,3})", raw)
    if not m:
        return None, 0.1 if re.search(r"\d", raw) else 0.0
    s, d = int(m.group(1)), int(m.group(2))
    note_scale = False
    if s < 30:                      # 12/7 en cmHg
        s, d = s * 10, d * 10
        note_scale = True
    if not (60 <= s <= 260 and 30 <= d <= 160 and s > d):
        return f"{s}/{d}", 0.4
    return f"{s}/{d}", 0.9 if note_scale else 1.0


def _ag(raw: str) -> tuple[str | None, float]:
    f = fold(raw).replace("semaines", "sa").replace("sem", "sa").replace("s.a", "sa")
    m = re.search(r"(\d{1,2})\s*sa?\s*(?:\+\s*(\d)\s*j?)?", f)
    if not m:
        m2 = re.search(r"^\s*(\d{1,2})\s*$", f)
        if m2 and 4 <= int(m2.group(1)) <= 44:
            return f"{int(m2.group(1))} SA", 0.8
        return None, 0.0
    w = int(m.group(1))
    if not 4 <= w <= 44:
        return None, 0.3
    if m.group(2):
        return f"{w} SA+{int(m.group(2))}j", 1.0
    return f"{w} SA", 1.0


def _weight_g(raw: str) -> tuple[str | None, float]:
    f = fold(raw)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g)?", f)
    if not m:
        return None, 0.0
    v = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    if unit == "kg" or (unit is None and v < 20):
        v = v * 1000
    return str(int(round(v))), 1.0 if unit else 0.9


def _unit_number(raw: str, unit_words: tuple[str, ...], as_int=False) -> tuple[str | None, float]:
    f = fold(raw)
    for u in unit_words:
        f = f.replace(u, "")
    return _number(f, as_int)


_LEXICON: list[tuple[str, str]] | None = None


def lexicon() -> list[tuple[str, str]]:
    """(forme repliée, forme canonique) pour chaque entrée de data/lexicon.txt."""
    global _LEXICON
    if _LEXICON is None:
        import os
        from .schema import DATA
        path = DATA / "lexicon.txt"
        entries = []
        if os.environ.get("DAYONE_LEXICON", "1") not in ("0", "off", "non"):
            pass
        else:
            path = DATA / "absent"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append((fold(line), line))
        _LEXICON = entries
    return _LEXICON


def lexicon_match(text: str, min_ratio: float = 0.8) -> tuple[str | None, float]:
    """Mot du vocabulaire le plus proche de la lecture (accents ignorés).

    Exact (accents mis à part) -> ratio 1.0. Sinon le meilleur rapprochement
    par difflib s'il atteint min_ratio : « Asthme l ger » -> « Asthme léger »,
    « Eommer ante » -> « Commerçante ». En dessous, (None, 0).
    """
    f = fold(text)
    if not f or len(f) < 3:
        return None, 0.0
    best, best_r = None, 0.0
    for ff, canon in lexicon():
        if ff == f:
            return canon, 1.0
        if abs(len(ff) - len(f)) > max(3, len(f) // 3):
            continue
        r = difflib.SequenceMatcher(None, ff, f).ratio()
        if r > best_r:
            best, best_r = canon, r
    if best is not None and best_r >= min_ratio:
        return best, round(best_r, 3)
    return None, 0.0


def normalize(raw: str | None, fdef: dict, ink: bool = True) -> Norm:
    """Lecture brute -> valeur canonique + statut.

    ink=False signale que la zone est vide sur l'image (aucune encre) : on force
    NON_FOURNI quoi qu'ait pu halluciner le lecteur.
    """
    if not ink:
        return Norm(None, "NON_FOURNI", 1.0)
    raw = (raw or "").strip()
    f = fold(raw)
    if f in BLANK_MARKS and f == "":
        return Norm(None, "NON_FOURNI", 0.9, "zone lue vide")
    if f in NA_MARKS:
        return Norm(None, "NON_APPLICABLE", 1.0, "tiret / barré")
    if f in NF_MARKS:
        return Norm(None, "NON_FOURNI", 1.0, "« NF » (non fait) écrit sur le papier")
    if f in ILLEGIBLE_MARKS:
        return Norm(None, "ILLISIBLE", 1.0)
    t = fdef.get("type", "text")
    if t == "date":
        v, s = _date(raw)
    elif t == "int":
        v, s = _number(raw, True)
    elif t == "float":
        v, s = _unit_number(raw, ("g/dl", "g/l", "k", "mm3"), False)
    elif t == "ta":
        v, s = _ta(raw)
    elif t == "age_gestationnel":
        v, s = _ag(raw)
    elif t == "poids_g":
        v, s = _weight_g(raw)
    elif t == "poids_kg":
        v, s = _unit_number(raw, ("kg",), False)
    elif t == "longueur_cm":
        v, s = _unit_number(raw, ("cm",), False)
    elif t == "temperature":
        v, s = _unit_number(raw, ("°c", "c", "°"), False)
        if v is not None and not 33 <= float(v) <= 43:
            s = min(s, 0.4)
    elif t == "enum":
        v, s = _enum_match(raw, fdef.get("values", []))
        if v is None:
            # hors vocabulaire : on garde la lecture brute mais le score bas => A_REVISER
            return Norm(raw, "CONNU", s, "hors vocabulaire fermé")
    elif t == "bool":
        v, s = _enum_match(raw, ["Oui", "Non"])
    else:  # text : on garde ce qui est écrit ; « RAS » et le vocabulaire du carnet sont canonisés
        v = "RAS" if f in {"ras", "r.a.s", "r.a.s.", "rien a signaler", "rien à signaler"} else raw
        v = re.sub(r"\s+", " ", v).strip()
        s = 1.0 if v else 0.0
        snap, ratio = lexicon_match(v)
        if snap is not None and snap != v:
            # lecture proche d'un mot connu (accent absent de la police, lettre
            # manquante) : on prend le mot connu, le score garde la trace de l'écart
            note = f"rapproché de « {snap} » (lu « {v} »)"
            return Norm(snap, "CONNU", ratio, note)
    if v is None:
        return Norm(raw, "CONNU", s, "format inattendu")
    # plage de valeurs
    rng = fdef.get("range")
    if rng and t in ("int", "float", "poids_g", "poids_kg", "longueur_cm", "temperature"):
        try:
            x = float(v)
            if not rng[0] <= x <= rng[1]:
                s = min(s, 0.4)
                return Norm(v, "CONNU", s, f"hors plage {rng}")
        except ValueError:
            pass
    return Norm(v, "CONNU", s)


def parse_ddmmyyyy(v: str | None):
    """-> datetime.date ou None."""
    import datetime as dt
    if not v:
        return None
    try:
        d, m, y = v.split("/")
        return dt.date(int(y), int(m), int(d))
    except Exception:
        return None
