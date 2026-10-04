"""
Lecteur par modèle vision local via Ollama (http://localhost:11434).

Aucune clé, aucune donnée envoyée hors de la machine. Modèles testés dans la
documentation Ollama avec entrée image : qwen2.5vl (3b, 7b), llama3.2-vision,
minicpm-v, llava. Choix par OLLAMA_MODEL (défaut qwen2.5vl:7b).

    ollama pull qwen2.5vl:7b
    DAYONE_READER=ollama uvicorn dayone.server:app --app-dir src

Même découpage en zones que le lecteur Claude ; réponse demandée en JSON strict.
"""
from __future__ import annotations

import base64
import json
import os
import re

import cv2
import numpy as np

from ..geometry import crop
from ..schema import PAGE_LABELS, field_def
from .base import RawRead, Reader

SYSTEM = (
    "Tu lis des cellules d'un carnet de suivi de grossesse rempli à la main en français. "
    "Pour chaque clé demandée, recopie exactement le texte manuscrit de la zone correspondante (abréviations comprises : RAS, NF, nég, IG, 16SA+3j). "
    "Zone vide : raw null et empty true. Tiret ou zone barrée : raw \"—\". Encre présente mais illisible : legible false. "
    "Ne lis pas les libellés imprimés. N'invente rien. "
    "Réponds UNIQUEMENT par un objet JSON de la forme {\"fields\":[{\"key\":...,\"raw\":...,\"empty\":...,\"legible\":...,\"confidence\":0..1}]}."
)


def _b64(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode()


class OllamaReader(Reader):
    name = "ollama"
    supports_classification = True

    def __init__(self, model: str | None = None, host: str | None = None, rows_per_call: int = 6, timeout: int = 180):
        import httpx
        self.model = model or os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.rows_per_call = rows_per_call
        self.client = httpx.Client(timeout=timeout)
        self.calls = 0

    def _chat(self, img: np.ndarray, prompt: str, json_mode: bool = True) -> str:
        self.calls += 1
        body = {"model": self.model, "stream": False, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt, "images": [_b64(img)]}]}
        if json_mode:
            body["format"] = "json"
        r = self.client.post(f"{self.host}/api/chat", json=body)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")

    def _describe(self, page_type: str, keys: list[str]) -> str:
        lines = []
        for k in keys:
            fd = field_def(page_type, k)
            d = f"- {k} : {fd.get('label', k)} (type {fd.get('type', 'text')}"
            if fd.get("values"):
                d += ", valeurs attendues : " + " / ".join(fd["values"])
            lines.append(d + ")")
        return "\n".join(lines)

    def _parse(self, txt: str) -> dict:
        try:
            data = json.loads(txt)
        except Exception:
            m = re.search(r"\{.*\}", txt, re.S)
            if not m:
                return {}
            try:
                data = json.loads(m.group(0))
            except Exception:
                return {}
        items = data.get("fields", data if isinstance(data, list) else [])
        out = {}
        for f in items:
            if isinstance(f, dict) and "key" in f:
                out[f["key"]] = f
        return out

    def _zones(self, page_type: str, keys: list[str], template: dict):
        keyset = set(keys)
        zones = []
        rows = [z for z in template.get("zones", []) if z["kind"] == "table_row"]
        forms = [z for z in template.get("zones", []) if z["kind"] != "table_row"]
        for z in forms:
            ks = [k for k in z["fields"] if k in keyset]
            if ks:
                zones.append((z["bbox_px"], ks, z["id"]))
        for i in range(0, len(rows), self.rows_per_call):
            grp = rows[i: i + self.rows_per_call]
            ks = [k for z in grp for k in z["fields"] if k in keyset]
            if not ks:
                continue
            bb = [0, min(min(z["bbox_px"][1] for z in grp), 300), 1654, max(z["bbox_px"][3] for z in grp)]
            zones.append((bb, ks, "lignes " + ", ".join(z["id"].replace("ligne_", "") for z in grp)))
        return zones

    def read_fields(self, page, page_type, keys, template, context=None):
        out = {k: RawRead(None, 0.0) for k in keys}
        if context and context.get("template_free"):
            zones = [([0, 0, page.shape[1], page.shape[0]], keys[i: i + 40], "page entière") for i in range(0, len(keys), 40)]
        else:
            zones = self._zones(page_type, keys, template)
        for bbox, ks, hint in zones:
            img = crop(page, bbox, pad=8)
            prompt = (f"Page : {PAGE_LABELS.get(page_type, page_type)}. Zone : {hint}.\n"
                      f"Clés à rapporter (toutes, même vides) :\n{self._describe(page_type, ks)}")
            try:
                res = self._parse(self._chat(img, prompt))
            except Exception as e:  # Ollama absent : on laisse les champs illisibles plutôt qu'inventés
                raise RuntimeError(f"Ollama indisponible ({e})")
            for k in ks:
                f = res.get(k)
                if not f:
                    continue
                raw = None if f.get("empty") else (f.get("raw") or None)
                try:
                    conf = float(f.get("confidence", 0.6))
                except (TypeError, ValueError):
                    conf = 0.6
                out[k] = RawRead(raw if raw is None else str(raw), round(max(0.0, min(1.0, conf)), 2), bool(f.get("legible", True)))
        return out

    def read_choices(self, page, page_type, groups: dict) -> dict[str, list[str]]:
        desc = "\n".join(f"- {g} ({d['label']}, {d['choice']}) : {', '.join(d['options'])}" for g, d in groups.items())
        prompt = ("Pour chaque groupe, quelles options sont cochées (croix, hachures) ou encerclées ? Liste vide si aucune. "
                  "Réponds en JSON : {\"groups\":[{\"group\":...,\"checked\":[...]}]}\n" + desc)
        try:
            data = json.loads(self._chat(page, prompt))
        except Exception:
            return {g: [] for g in groups}
        out = {g: [] for g in groups}
        for g in data.get("groups", []):
            if isinstance(g, dict) and g.get("group") in out:
                out[g["group"]] = [o for o in g.get("checked", []) if o in groups[g["group"]]["options"]]
        return out

    def classify_page(self, image: np.ndarray) -> str | None:
        small = cv2.resize(image, None, fx=min(1.0, 1000 / image.shape[1]), fy=min(1.0, 1000 / image.shape[1]))
        labels = "\n".join(f"- {k} : {v}" for k, v in PAGE_LABELS.items())
        try:
            txt = self._chat(small, "Quelle page du carnet est-ce ? Réponds par la clé seule.\n" + labels, json_mode=False).upper()
        except Exception:
            return None
        for k in PAGE_LABELS:
            if k in txt:
                return k
        return None
