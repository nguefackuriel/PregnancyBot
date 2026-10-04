"""Briques de lecture locale : tiret ou bruit, découpe en lignes, vocabulaire, modèle ONNX."""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.geometry import has_ink, looks_like_dash  # noqa: E402
from dayone.normalize import lexicon_match, normalize  # noqa: E402
from dayone.reader.crnn_reader import text_lines  # noqa: E402

PINK = (204, 188, 247)


def _cell(w=120, h=48):
    return np.full((h, w, 3), PINK, np.uint8)


def test_dash_is_a_short_compact_stroke_and_border_residue_is_not():
    img = _cell()
    cv2.line(img, (20, 24), (29, 24), (40, 30, 160), 3)          # tiret : 10 x 3 px
    box = [0, 0, img.shape[1], img.shape[0]]
    assert has_ink(img, box)
    assert looks_like_dash(img, box)

    img2 = _cell()
    cv2.line(img2, (10, 20), (60, 20), (40, 40, 40), 1)           # reste de bordure : 50 x 1 px
    assert not looks_like_dash(img2, [0, 0, img2.shape[1], img2.shape[0]])


def test_tall_free_text_box_is_split_into_writing_lines():
    box = _cell(280, 300)
    cv2.rectangle(box, (0, 0), (279, 299), (30, 30, 30), 2)       # bordure de la boîte
    cv2.putText(box, "Cycles reguliers", (12, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 30, 160), 2)
    cv2.putText(box, "RAS", (12, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 30, 160), 2)
    lines = text_lines(box)
    assert len(lines) == 2
    assert all(l.shape[0] <= 60 for l in lines)
    small = _cell(120, 40)
    assert len(text_lines(small)) == 1                            # petite cellule : telle quelle


def test_lexicon_repairs_missing_accent_but_keeps_unknown_text():
    assert lexicon_match("Asthme l ger")[0] == "Asthme léger"
    assert lexicon_match("Cycles r guliers")[0] == "Cycles réguliers"
    assert lexicon_match("Kyste du rein")[0] is None
    n = normalize("N ant", {"type": "text"})
    assert n.value == "Néant" and n.status == "CONNU" and n.score < 1.0 and "rapproché" in (n.note or "")
    n2 = normalize("Kyste du rein", {"type": "text"})
    assert n2.value == "Kyste du rein" and n2.score == 1.0


def test_onnx_model_reads_a_rendered_cell():
    from dayone.ocr.crnn import OCR
    from dayone.ocr.synth import Renderer
    import random
    w = ROOT / "data" / "models" / "crnn.onnx"
    if not w.exists():
        return
    r = Renderer(ROOT / "data" / "fonts", random.Random(0))
    ocr = OCR(str(w))
    img, label = r.render("12/05/2026")
    (text, conf), = ocr.read([img])
    assert text.replace(" ", "") == label.replace(" ", "")
    assert conf > 0.5
    empty, _ = r.render("")
    (t2, _), = ocr.read([empty])
    assert t2.strip() == ""
