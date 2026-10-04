"""
Pipeline d'extraction : photo -> objet structuré avec statut + confiance par champ.

    photo ──► qualité ──► type de page ──► recalage sur le gabarit
          ──► encre par zone (vide => NON_FOURNI sans appeler le lecteur)
          ──► lecteur (Claude / tesseract / mock) sur les zones encrées
          ──► normalisation (vocabulaire, types, codes NF / tiret / IG)
          ──► cases à cocher (géométrie) ──► règles N/A ──► cohérence
          ──► confiance calibrée ──► statut
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import geometry as G
from .normalize import normalize
from .reader.base import Reader
from .rules import apply_not_applicable, check_consistency
from .schema import T_CONNU, T_REVISION, field_def, fields_of, groups_of, is_pii, load_templates

IG_KEYS = ("gestation",)


@dataclass
class PageExtraction:
    page_type: str | None
    page_type_score: float
    quality: dict
    registration: dict
    fields: dict = field(default_factory=dict)     # key -> enregistrement de champ (voir SCHEMA.md §2)
    choices: dict = field(default_factory=dict)    # groupe -> options cochées
    checkboxes: dict = field(default_factory=dict) # key -> {checked, confidence, bbox_px}
    flags: list = field(default_factory=list)      # incohérences
    pii_boxes: list = field(default_factory=list)  # bbox (image d'origine) à masquer
    timing_s: float = 0.0
    reader: str = ""
    error: str | None = None
    registered_image: np.ndarray | None = None   # page recalée et masquée (non sérialisée)

    # ---- résumés utiles au flux conversationnel ----
    def by_status(self, status: str) -> list[str]:
        return [k for k, f in self.fields.items() if f["status"] == status]

    def doubts(self) -> list[str]:
        """Champs sur lesquels l'agent doit poser une question (ordre d'importance)."""
        keys = self.by_status("A_REVISER") + self.by_status("ILLISIBLE")
        return sorted(keys, key=lambda k: (0 if "." not in k else 1, k))

    def to_dict(self) -> dict:
        return {
            "page_type": self.page_type, "page_type_score": self.page_type_score, "quality": self.quality,
            "registration": self.registration, "fields": self.fields, "choices": self.choices,
            "checkboxes": self.checkboxes, "flags": self.flags, "pii_boxes": self.pii_boxes,
            "timing_s": self.timing_s, "reader": self.reader, "error": self.error,
        }


def _confidence(reader_conf: float, norm_score: float, legible: bool, flagged: bool) -> float:
    """Combinaison simple et monotone ; à recalibrer sur le jeu de test (ECE dans evaluate.py)."""
    c = 0.55 * reader_conf + 0.35 * norm_score + 0.10
    if not legible:
        c *= 0.5
    if flagged:
        c *= 0.7
    return round(max(0.0, min(1.0, c)), 3)


def _status_from_conf(conf: float, ink: bool, has_value: bool) -> str:
    if not ink:
        return "NON_FOURNI"
    if not has_value:
        return "ILLISIBLE"
    if conf >= T_CONNU:
        return "CONNU"
    if conf >= T_REVISION:
        return "A_REVISER"
    return "ILLISIBLE"


def extract_page(image: np.ndarray | str, reader: Reader, page_type: str | None = None,
                 source_name: str = "", skip_quality: bool = False) -> PageExtraction:
    t0 = time.time()
    img = cv2.imread(image) if isinstance(image, str) else image
    if img is None:
        return PageExtraction(None, 0.0, {}, {}, error="image illisible", reader=reader.name)
    q = G.assess_quality(img)
    quality = {"ok": q.ok, "blur": q.blur, "brightness": q.brightness, "reasons": q.reasons}
    if not q.ok and not skip_quality:
        return PageExtraction(None, 0.0, quality, {}, error="qualité insuffisante : " + "; ".join(q.reasons), reader=reader.name)

    # ---- redressement de la page (indépendant du type) puis type de page ----
    prewarped = G.warp_to_page(img)
    score = 1.0
    if page_type is None:
        page_type, score = G.detect_page_type(img, warped=prewarped[0])
        if (page_type is None or score < 0.6) and reader.supports_classification:
            guess = reader.classify_page(prewarped[0])
            if guess:
                page_type, score = guess, 0.8
    if page_type is None:
        return PageExtraction(None, score, quality, {}, error="type de page non reconnu", reader=reader.name)
    tpl = load_templates()[page_type]

    # ---- recalage fin sur le gabarit ----
    reg = G.register(img, page_type, prewarped=prewarped)
    page = reg.image
    registration = {"method": reg.method, "line_score": reg.score}
    bg = G.page_background(page)

    # ---- PII : zones d'identifiants directs masquées AVANT toute lecture ----
    # (positions connues du gabarit : nom, CIN, adresse, téléphone, nom du mari)
    pii_boxes = []
    for k, fd in fields_of(page_type).items():
        if fd.get("pii") and k in tpl["fields"]:
            b = tpl["fields"][k]["bbox_px"]
            page[b[1]:b[3], b[0]:b[2]] = bg.astype(np.uint8)
            pii_boxes.append(reg.to_original(b))

    # ---- mode « sans gabarit » : la page ne se recale pas (vrai carnet, autre
    #      mise en page) et le lecteur sait lire une page entière ----
    # gabarit qui ne colle pas (vrai carnet au petit format, autre version du
    # formulaire) : les cellules découpées ne tombent pas au bon endroit
    template_fit = reg.score >= 0.5
    registration["template_fit"] = template_fit
    template_free = not template_fit and reader.supports_classification and reg.method != "scale"
    registration["template_free"] = template_free
    if reg.score < 0.25 and score < 1.0:
        # la page ne colle pas au gabarit : on demandera confirmation du type de page
        score = min(score, 0.5)

    # ---- encre par champ ----
    fdefs = fields_of(page_type)
    fields: dict = {}
    inked = []
    for k, fd in fdefs.items():
        tb = tpl["fields"].get(k)
        if template_free:
            fields[k] = {"value": None, "raw": None, "status": "NON_FOURNI", "confidence": None, "source": "ai",
                         "bbox_px": None, "bbox_tpl": None, "notes": None, "pii": is_pii(page_type, k)}
            if not fd.get("pii"):
                inked.append(k)
            continue
        if tb is None:
            fields[k] = {"value": None, "raw": None, "status": "NON_FOURNI", "confidence": None, "source": "rule",
                         "bbox_px": None, "notes": "ligne absente de cette version du formulaire", "pii": is_pii(page_type, k)}
            continue
        ink = G.has_ink(page, tb["bbox_px"]) and not fd.get("pii")
        bbox_orig = reg.to_original(tb["bbox_px"])
        fields[k] = {"value": None, "raw": None, "status": "NON_FOURNI", "confidence": None, "source": "ai",
                     "bbox_px": bbox_orig, "bbox_tpl": tb["bbox_px"], "notes": None, "pii": is_pii(page_type, k)}
        if ink:
            inked.append(k)

    # ---- lecture ----
    reads = reader.read_fields(page if not template_free else img, page_type, inked, tpl,
                               context={"source_name": source_name, "template_free": template_free}) if inked else {}
    for k in inked:
        r = reads.get(k)
        fd = fdefs[k]
        f = fields[k]
        if r is None:
            f["status"], f["notes"] = "ILLISIBLE", "aucune lecture"
            f["confidence"] = 0.0
            continue
        n = normalize(r.raw, fd, ink=True)
        f["raw"] = r.raw
        if n.status == "NON_FOURNI" and not (r.raw or "").strip():
            if r.conf >= 0.8:
                # encre détectée (bruit, trait de bordure) mais lecteur confiant : zone vide
                f["status"], f["confidence"], f["notes"] = "NON_FOURNI", round(r.conf, 3), "encre faible, lue vide"
            else:
                # de l'encre est présente mais le lecteur n'a rien lu => illisible, pas vide
                f["status"], f["confidence"], f["notes"] = "ILLISIBLE", round(r.conf * 0.5, 3), "encre présente, lecture vide"
            continue
        if n.status == "NON_APPLICABLE" and (r.raw or "").strip() in ("—", "–", "-") and not template_free:
            # un tiret est un trait court et compact ; du bruit ou un reste de bordure
            # de cellule n'en est pas un, même si le lecteur y voit un tiret
            if not G.looks_like_dash(page, f["bbox_tpl"]):
                f["status"], f["confidence"], f["notes"] = "NON_FOURNI", round(min(r.conf, 0.8), 3), "encre faible, pas un tiret"
                continue
        if n.status in ("NON_APPLICABLE", "NON_FOURNI"):
            f["status"], f["value"], f["notes"], f["confidence"] = n.status, None, n.note, round(r.conf, 3)
            continue
        if n.status == "ILLISIBLE":
            f["status"], f["confidence"], f["notes"] = "ILLISIBLE", round(r.conf * 0.5, 3), n.note
            continue
        conf = _confidence(r.conf, n.score, r.legible, False)
        if not template_fit and not template_free:
            # gabarit incertain : jamais CONNU sans la sage-femme
            conf = min(conf, T_CONNU - 0.01)
            f["notes"] = "mise en page non reconnue, à confirmer" + (f" ; {n.note}" if n.note else "")
        else:
            f["notes"] = n.note
        f["value"] = n.value
        f["confidence"] = conf
        f["status"] = _status_from_conf(conf, True, n.value is not None)
        # « IG » (primigeste) écrit en travers : gestation = 1, parité = 0
        if k in IG_KEYS and (r.raw or "").strip().upper() in ("IG", "1G", "I G"):
            f["value"], f["status"], f["notes"] = "1", "CONNU", "« IG » = primigeste"
            if "parite" in fields and fields["parite"]["status"] == "NON_FOURNI":
                fields["parite"].update({"value": "0", "status": "CONNU", "confidence": conf, "source": "rule", "notes": "déduit de « IG »"})

    # ---- PII : jamais lues ni stockées ----
    for k, f in fields.items():
        if f.get("pii"):
            f["value"], f["raw"], f["confidence"] = None, None, None
            f["status"] = "REDACTED"
            f["notes"] = "identifiant direct : masqué sur l'image, jamais lu ni stocké"

    # ---- cases à cocher ----
    checkboxes, choices = {}, {}
    for gkey, gdef in groups_of(page_type).items():
        choices[gkey] = []
    if template_free and hasattr(reader, "read_choices"):
        choices.update(reader.read_choices(img, page_type, groups_of(page_type)))
    else:
        for key, bbox in tpl["checkboxes"].items():
            checked, conf = G.checkbox_checked(page, bbox, bg=bg)
            checkboxes[key] = {"checked": checked, "confidence": conf, "bbox_px": reg.to_original(bbox)}
            grp, opt = key.split(".", 1)
            if checked:
                choices.setdefault(grp, []).append(opt)
    # un groupe à choix unique avec 2 options cochées => doute
    choice_doubts = []
    for gkey, gdef in groups_of(page_type).items():
        if gdef["choice"] == "single" and len(choices.get(gkey, [])) > 1:
            choice_doubts.append(gkey)

    # ---- règles ----
    apply_not_applicable(page_type, fields, choices)
    flags = check_consistency(page_type, fields, choices)
    for fl in flags:
        for k in fl.keys:
            f = fields.get(k)
            if f and f["status"] == "CONNU":
                f["status"] = "A_REVISER"
                f["confidence"] = round((f["confidence"] or 0.5) * 0.7, 3)
                f["notes"] = fl.message
    out = PageExtraction(page_type, score, quality, registration, fields, choices, checkboxes,
                         [{"keys": f.keys, "rule": f.rule, "message": f.message} for f in flags]
                         + [{"keys": [g], "rule": "choix_multiple", "message": f"plusieurs cases cochées pour « {groups_of(page_type)[g]['label']} »"} for g in choice_doubts],
                         pii_boxes, round(time.time() - t0, 2), reader.name)
    out.registered_image = page
    return out


def predictions_entry(ext: PageExtraction, png_file: str) -> dict:
    """Format attendu par scripts/evaluate.py."""
    return {
        "png_file": png_file,
        "page_type": ext.page_type,
        "fields": {k: {"value": f["value"], "status": f["status"], "confidence": f["confidence"]} for k, f in ext.fields.items()},
        "choices": ext.choices,
    }
