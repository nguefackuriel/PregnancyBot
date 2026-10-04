"""
Lecteur simulé : renvoie la vérité terrain (avec du bruit optionnel).

Sert aux tests, à la démo sans réseau et à valider la plomberie
(normalisation, statuts, confiance, flux conversationnel) indépendamment du
moteur de lecture. Il reconnaît la page par le nom de fichier d'origine
(context["source_name"]) ; sinon par le type de page + numéro de patiente.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from ..schema import DATA
from .base import RawRead, Reader


class MockReader(Reader):
    name = "mock"

    def __init__(self, gt_path: str | None = None, noise: float = 0.0, uncertain_keys: list[str] | None = None, seed: int = 0):
        p = Path(gt_path) if gt_path else DATA / "ground_truth" / "ground_truth.json"
        gt = json.loads(p.read_text(encoding="utf-8"))
        self.by_name = {pg["png_file"]: pg for pg in gt["pages"]}
        self.noise = noise
        self.uncertain = set(uncertain_keys or [])
        self.rng = random.Random(seed)

    def _page(self, context):
        name = (context or {}).get("source_name", "")
        base = Path(name).name
        # tolère les doublons renommés « -07__xxxx.png » et les variantes dégradées « -07__deg3.jpg »
        import re
        m = re.search(r"patientes-(\d\d)", base)
        if m:
            return self.by_name.get(f"dossiers_specimen_10_patientes-{m.group(1)}.png")
        return None

    def read_fields(self, page, page_type, keys, template, context=None):
        pg = self._page(context)
        out = {}
        for k in keys:
            f = pg["fields"].get(k) if pg else None
            if not f or f.get("status_on_image") not in ("CONNU", "NON_APPLICABLE"):
                out[k] = RawRead(None, 0.9)
                continue
            raw, conf = f["value"], 0.95
            if k in self.uncertain:
                conf = 0.55
            if self.noise and self.rng.random() < self.noise:
                raw = raw[:-1] + "?" if len(raw) > 1 else "?"
                conf = 0.4
            out[k] = RawRead(raw, conf)
        return out
