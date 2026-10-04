"""
Lecteur principal : modèle vision Claude (API Anthropic), zone par zone,
avec sortie structurée (tool use) et, en option, une seconde passe pour
l'auto-cohérence (accord entre deux lectures => confiance).

Variables d'environnement : ANTHROPIC_API_KEY (obligatoire), DAYONE_MODEL
(défaut : claude-sonnet-5-5).

Rappel de la consigne du défi : seules des données SYNTHÉTIQUES peuvent être
envoyées à un service tiers. Pour de vraies patientes, utiliser un modèle
hébergé localement derrière la même interface Reader.
"""
from __future__ import annotations

import base64
import json
import os

import cv2
import numpy as np

from ..geometry import crop
from ..schema import PAGE_LABELS, field_def
from .base import RawRead, Reader

SYSTEM = """Tu es un assistant de saisie pour un carnet de suivi de grossesse marocain (« Fiche de surveillance de la grossesse et du post-partum »), rempli à la main en français par des sages-femmes.
On te montre un recadrage d'une page et la liste des champs qu'il contient. Pour CHAQUE clé demandée, tu rapportes exactement ce qui est écrit à la main dans la zone correspondante.
Règles :
- Recopie le texte manuscrit tel quel (abréviations comprises : RAS, NF, nég, IG, 16SA+3j, Reçu...). Ne corrige pas, ne complète pas, n'invente jamais.
- Une zone vide => empty=true, raw=null. Un tiret « — », un trait ou une zone barrée => raw="—".
- Si l'encre est présente mais que tu n'arrives pas à lire => legible=false et raw = ta meilleure hypothèse (ou null).
- Les lettres accentuées peuvent manquer dans l'écriture (ex. « P les » pour « Pâles ») : recopie ce que tu vois ; la normalisation est faite ensuite.
- confidence ∈ [0,1] : 1 = parfaitement lisible et sans ambiguïté ; 0.5 = ambigu (ex. 1/7, 3/8, 0/6) ; < 0.3 = quasi illisible.
- Ne lis PAS les libellés imprimés ni les en-têtes : seulement la valeur manuscrite de chaque champ.
Dans les tableaux, les colonnes sont, de gauche à droite : 1er trimestre V1, V2, V3 ; 2ème trimestre V1, V2, V3 ; 3ème trimestre 7ème, 8ème, 9ème mois. Une colonne vide est vide : ne décale jamais les valeurs d'une colonne à l'autre."""

TOOL = {
    "name": "rapporter_champs",
    "description": "Rapporte la lecture brute de chaque champ demandé.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "raw": {"type": ["string", "null"]},
                        "empty": {"type": "boolean"},
                        "legible": {"type": "boolean"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["key", "raw", "empty", "legible", "confidence"],
                },
            }
        },
        "required": ["fields"],
    },
}


def _b64(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode()


class AnthropicReader(Reader):
    name = "anthropic"
    supports_classification = True

    def __init__(self, model: str | None = None, passes: int = 1, rows_per_call: int = 8, max_tokens: int = 4000):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("DAYONE_MODEL", "claude-sonnet-5-5")
        self.passes = passes
        self.rows_per_call = rows_per_call
        self.max_tokens = max_tokens
        self.calls = 0

    # ------------------------------------------------------------------ #
    def _describe(self, page_type: str, keys: list[str]) -> str:
        lines = []
        for k in keys:
            fd = field_def(page_type, k)
            desc = f"- {k} : {fd.get('label', k)} (type {fd.get('type', 'text')}"
            if fd.get("values"):
                desc += f", valeurs attendues : {' / '.join(fd['values'])}"
            if fd.get("unit"):
                desc += f", unité {fd['unit']}"
            desc += ")"
            lines.append(desc)
        return "\n".join(lines)

    def _call(self, img: np.ndarray, page_type: str, keys: list[str], zone_hint: str, temperature: float = 0.0) -> dict:
        h, w = img.shape[:2]
        if w < 1200 and h < 400:                       # agrandir les petites bandes
            f = min(2.0, 1500 / w)
            img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        prompt = (f"Page : {PAGE_LABELS.get(page_type, page_type)}. Zone : {zone_hint}.\n"
                  f"Champs à rapporter (utilise exactement ces clés) :\n{self._describe(page_type, keys)}\n"
                  "Rapporte tous les champs listés, y compris les vides.")
        self.calls += 1
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, temperature=temperature, system=SYSTEM,
            tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(img)}},
                {"type": "text", "text": prompt},
            ]}],
        )
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                return {f["key"]: f for f in block.input.get("fields", []) if "key" in f}
        # repli : JSON dans le texte
        txt = "".join(getattr(b, "text", "") for b in resp.content)
        try:
            data = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
            return {f["key"]: f for f in data.get("fields", [])}
        except Exception:
            return {}

    def _zones(self, page_type: str, keys: list[str], template: dict):
        """Regroupe les clés par zone de lecture (bande de lignes pour le tableau)."""
        keyset = set(keys)
        zones = []
        tzones = template.get("zones", [])
        rows = [z for z in tzones if z["kind"] == "table_row"]
        forms = [z for z in tzones if z["kind"] != "table_row"]
        for z in forms:
            ks = [k for k in z["fields"] if k in keyset]
            if ks:
                zones.append((z["bbox_px"], ks, z["id"]))
        for i in range(0, len(rows), self.rows_per_call):
            grp = rows[i: i + self.rows_per_call]
            ks = [k for z in grp for k in z["fields"] if k in keyset]
            if not ks:
                continue
            bb = [min(z["bbox_px"][0] for z in grp), min(z["bbox_px"][1] for z in grp),
                  max(z["bbox_px"][2] for z in grp), max(z["bbox_px"][3] for z in grp)]
            # inclure l'en-tête de colonnes pour que le modèle voie les 9 colonnes
            hdr = template["zones"][1]["bbox_px"] if len(template["zones"]) > 1 else bb
            bb = [0, min(bb[1], 300), 1654, bb[3]] if page_type == "GROSSESSE_ACTUELLE" else bb
            zones.append((bb, ks, "lignes " + ", ".join(z["id"].replace("ligne_", "") for z in grp)))
        return zones

    def read_fields(self, page, page_type, keys, template, context=None):
        out: dict[str, RawRead] = {}
        if context and context.get("template_free"):
            # recalage impossible (vrai carnet, mise en page différente) : page entière,
            # tous les champs, par paquets de 60 clés
            for i in range(0, len(keys), 60):
                ks = keys[i: i + 60]
                r = self._call(page, page_type, ks, "page entière (sans gabarit)")
                for k in ks:
                    x = r.get(k)
                    if x is None:
                        out[k] = RawRead(None, 0.0)
                    else:
                        out[k] = RawRead(None if x.get("empty") else (x.get("raw") or None),
                                         round(max(0.0, min(1.0, float(x.get("confidence", 0.5)))), 2), bool(x.get("legible", True)))
            return out
        for bbox, ks, hint in self._zones(page_type, keys, template):
            img = crop(page, bbox, pad=8)
            reads = []
            for p in range(self.passes):
                reads.append(self._call(img if p == 0 else crop(page, bbox, pad=20), page_type, ks, hint,
                                        temperature=0.0 if p == 0 else 0.4))
            for k in ks:
                r0 = reads[0].get(k)
                if r0 is None:
                    out[k] = RawRead(None, 0.0)
                    continue
                raw = None if r0.get("empty") else (r0.get("raw") or None)
                conf = float(r0.get("confidence", 0.5))
                legible = bool(r0.get("legible", True))
                if self.passes > 1 and reads[1].get(k) is not None:
                    r1 = reads[1][k]
                    raw1 = None if r1.get("empty") else (r1.get("raw") or None)
                    agree = (raw or "").strip().lower() == (raw1 or "").strip().lower()
                    conf = min(1.0, conf * 0.6 + 0.4) if agree else conf * 0.5
                out[k] = RawRead(raw, round(max(0.0, min(1.0, conf)), 2), legible)
        return out

    # ------------------------------------------------------------------ #
    def read_choices(self, page: np.ndarray, page_type: str, groups: dict) -> dict[str, list[str]]:
        """Cases cochées / mentions encerclées lues par le modèle (mode sans gabarit)."""
        desc = "\n".join(f"- {g} ({d['label']}, {d['choice']}) : options {', '.join(d['options'])}" for g, d in groups.items())
        tool = {"name": "rapporter_cases", "description": "Options cochées ou encerclées par groupe.",
                "input_schema": {"type": "object", "properties": {"groups": {"type": "array", "items": {"type": "object", "properties": {
                    "group": {"type": "string"}, "checked": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"}}, "required": ["group", "checked", "confidence"]}}}, "required": ["groups"]}}
        self.calls += 1
        resp = self.client.messages.create(
            model=self.model, max_tokens=2000, temperature=0.0, system=SYSTEM,
            tools=[tool], tool_choice={"type": "tool", "name": "rapporter_cases"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(page)}},
                {"type": "text", "text": f"Page : {PAGE_LABELS.get(page_type, page_type)}. Pour chaque groupe, quelles options sont cochées "
                                         f"(croix, hachure) ou ENCERCLÉES ? Liste vide si aucune.\n{desc}"},
            ]}],
        )
        out = {g: [] for g in groups}
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                for g in block.input.get("groups", []):
                    if g.get("group") in out:
                        out[g["group"]] = [o for o in g.get("checked", []) if o in groups[g["group"]]["options"]]
        return out

    def classify_page(self, image: np.ndarray) -> str | None:
        small = cv2.resize(image, None, fx=min(1.0, 1000 / image.shape[1]), fy=min(1.0, 1000 / image.shape[1]))
        labels = "\n".join(f"- {k} : {v}" for k, v in PAGE_LABELS.items())
        self.calls += 1
        resp = self.client.messages.create(
            model=self.model, max_tokens=50, temperature=0.0,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(small)}},
                {"type": "text", "text": "Quelle page du carnet est-ce ? Réponds uniquement par la clé.\n" + labels},
            ]}],
        )
        txt = "".join(getattr(b, "text", "") for b in resp.content).strip().upper()
        for k in PAGE_LABELS:
            if k in txt:
                return k
        return None
