"""Gabarits du vrai carnet (petit format) : chargés à côté du spécimen, sans le perturber."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.schema import PAGE_TYPES, fields_of, load_variants, templates_for  # noqa: E402


def test_variants_are_loaded_and_point_to_a_known_page():
    v = load_variants()
    assert len(v) >= 5
    for name, tpl in v.items():
        assert tpl["base"] in PAGE_TYPES
        known = set(fields_of(tpl["base"]))
        # chaque cellule du variant est un vrai champ du schéma
        assert set(tpl["fields"]) <= known, name
        for b in list(tpl["fields"].values()):
            x0, y0, x1, y1 = b["bbox_px"]
            assert x1 > x0 and y1 > y0
        for b in tpl["checkboxes"].values():
            assert len(b) == 4


def test_specimen_template_comes_first():
    for pt in PAGE_TYPES:
        names = [n for n, _ in templates_for(pt)]
        assert names[0] == pt
