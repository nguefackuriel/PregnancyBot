"""Tests unitaires : normalisation, règles, machine à états, store chiffré, liaison, géométrie."""
import json
import sqlite3
import sys
from pathlib import Path

import cv2
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone import geometry as G  # noqa: E402
from dayone import lifecycle as L  # noqa: E402
from dayone.linking import find_candidates, similarity  # noqa: E402
from dayone.normalize import normalize  # noqa: E402
from dayone.rules import apply_not_applicable, check_consistency  # noqa: E402
from dayone.schema import field_def, load_templates  # noqa: E402
from dayone.store import Store  # noqa: E402

SAMPLES = ROOT / "data" / "samples"


# --------------------------------------------------------------------------- normalisation
@pytest.mark.parametrize("raw,key,page,value,status", [
    ("P les", "visites.t1_v1.conjonctives", "GROSSESSE_ACTUELLE", "Pâles", "CONNU"),
    ("12/7", "visites.t1_v1.ta", "GROSSESSE_ACTUELLE", "120/70", "CONNU"),
    ("16SA+3j", "visites.t1_v1.age_gestationnel", "GROSSESSE_ACTUELLE", "16 SA+3j", "CONNU"),
    ("3,5 kg", "poids_naissance_g", "ACCOUCHEMENT", "3500", "CONNU"),
    ("22/12/25", "ddr", "GROSSESSE_ACTUELLE", "22/12/2025", "CONNU"),
    ("NF", "visites.t1_v1.hemoglobine", "GROSSESSE_ACTUELLE", None, "NON_FOURNI"),
    ("—", "visites.t1_v1.bcf", "GROSSESSE_ACTUELLE", None, "NON_APPLICABLE"),
    ("nég", "visites.t1_v1.vih", "GROSSESSE_ACTUELLE", "Neg", "CONNU"),
    ("Reçu", "visites.t1_v1.fer", "GROSSESSE_ACTUELLE", "Oui", "CONNU"),
    ("", "sexe", "ACCOUCHEMENT", None, "NON_FOURNI"),
])
def test_normalize(raw, key, page, value, status):
    n = normalize(raw, field_def(page, key))
    assert (n.value, n.status) == (value, status)


def test_normalize_out_of_vocab_is_low_score():
    n = normalize("Bizarre", field_def("GROSSESSE_ACTUELLE", "visites.t1_v1.conjonctives"))
    assert n.status == "CONNU" and n.score < 0.75


# --------------------------------------------------------------------------- règles
def _f(v, st="CONNU"):
    return {"value": v, "status": st}


def test_rules_parity_and_dates():
    fields = {"gestation": _f("1"), "parite": _f("2"), "enfants_vivants": _f("3"),
              "ddr": _f("01/01/2026"), "dpa": _f("01/02/2026")}
    flags = check_consistency("IDENTIFICATION_ANTECEDENTS", fields, {})
    ids = {f.rule for f in flags}
    assert {"parite_le_gestation", "vivants_le_parite", "dpa_vs_ddr"} <= ids


def test_not_applicable_rules():
    fields = {"parite": _f("1")}
    for i in range(1, 6):
        for c in ("date", "modalite", "indication_cesarienne"):
            fields[f"accouchements_anterieurs.{i}.{c}"] = _f(None, "NON_FOURNI")
    fields["accouchements_anterieurs.1.modalite"] = _f("Voie basse")
    fields["date_vaccin_rubeole"] = _f(None, "NON_FOURNI")
    changed = apply_not_applicable("IDENTIFICATION_ANTECEDENTS", fields, {"vaccin_rubeole": []})
    assert fields["accouchements_anterieurs.2.date"]["status"] == "NON_APPLICABLE"
    assert fields["accouchements_anterieurs.1.indication_cesarienne"]["status"] == "NON_APPLICABLE"
    assert fields["date_vaccin_rubeole"]["status"] == "NON_APPLICABLE"
    assert "accouchements_anterieurs.1.date" not in changed


# --------------------------------------------------------------------------- cycle de vie
def test_lifecycle_transitions():
    assert L.can("CAPTURE", "EN_ATTENTE_IA")
    assert L.can("EN_ATTENTE_IA", "ECHEC_TRAITEMENT")
    assert L.can("ENREGISTRE", "ECHEC_SYNCHRO") and L.can("ECHEC_SYNCHRO", "SYNCHRONISE")
    assert not L.can("CAPTURE", "SYNCHRONISE")
    with pytest.raises(L.IllegalTransition):
        L.check("VALIDE", "CAPTURE")


# --------------------------------------------------------------------------- store chiffré + file
def test_store_encrypts_and_queues(tmp_path):
    st = Store(tmp_path / "t.db")
    rid = st.create_record("sf1", b"IMAGEBYTES", "image/jpeg", "sha", payload={"secret": "valeur"})
    raw = sqlite3.connect(tmp_path / "t.db").execute("SELECT payload, image FROM records").fetchone()
    assert b"valeur" not in raw[0] and b"IMAGEBYTES" not in raw[1]          # chiffré au repos
    assert st.get(rid)["payload"]["secret"] == "valeur"
    assert st.get_image(rid)[0] == b"IMAGEBYTES"
    st.transition(rid, "EN_ATTENTE_IA")
    st.enqueue(rid, "ai")
    assert [q["record_id"] for q in st.queue_items()] == [rid]
    with pytest.raises(L.IllegalTransition):
        st.transition(rid, "SYNCHRONISE")
    st.bump_attempt(rid)
    assert st.get(rid)["attempts"] == 1
    st.dequeue(rid)
    assert st.queue_items() == []
    assert [e["to_state"] for e in st.history(rid)] == ["CAPTURE", "EN_ATTENTE_IA"]
    # réouverture avec la même clé (fichier .key) : les données restent lisibles
    st2 = Store(tmp_path / "t.db")
    assert st2.get(rid)["payload"]["secret"] == "valeur"


def test_store_survives_restart_queue(tmp_path):
    st = Store(tmp_path / "q.db")
    rid = st.create_record("sf", b"x", "image/jpeg", "s1")
    st.transition(rid, "EN_ATTENTE_IA")
    st.enqueue(rid, "ai")
    del st
    st = Store(tmp_path / "q.db")
    assert len(st.queue_items()) == 1          # rien n'est perdu


# --------------------------------------------------------------------------- liaison
def test_linking_candidates():
    patients = [
        {"id": "p1", "code": "AB12", "profile": {"signals": {"age": "31", "gestation": "3", "parite": "1", "ddr": "26/04/2025"}, "n_pages": 2}},
        {"id": "p2", "code": "ZZ99", "profile": {"signals": {"age": "22", "gestation": "1", "parite": "0"}, "n_pages": 1}},
    ]
    fields = {"age": _f("31"), "gestation": _f("3"), "parite": _f("1")}
    c = find_candidates(patients, "AB12", "IDENTIFICATION_ANTECEDENTS", fields)
    assert c and c[0].patient_id == "p1" and c[0].score >= 0.9
    c = find_candidates(patients, None, "IDENTIFICATION_ANTECEDENTS", fields)
    assert c and c[0].patient_id == "p1"
    c = find_candidates(patients, "QQ00", "IDENTIFICATION_ANTECEDENTS", {"age": _f("50")})
    assert c == []                                       # rien de plausible => création proposée
    s, _ = similarity({"ddr": "01/01/2026"}, {"ddr": "05/01/2026"})
    assert s > 0.5


# --------------------------------------------------------------------------- géométrie
def test_page_type_and_checkboxes_on_sample():
    img = cv2.imread(str(SAMPLES / "dossiers_specimen_10_patientes-04.png"))
    pt, score = G.detect_page_type(img)
    assert pt == "ACCOUCHEMENT"
    reg = G.register(img, pt)
    tpl = load_templates()[pt]
    checked = {k for k, b in tpl["checkboxes"].items() if G.checkbox_checked(reg.image, b)[0]}
    assert checked == {"lieu.milieu_surveille", "lieu_detail.maternite", "mode.voie_basse_non_instrumentale", "etat_nouveau_ne.vivant"}


def test_quality_gate():
    img = cv2.imread(str(SAMPLES / "dossiers_specimen_10_patientes-04.png"))
    assert G.assess_quality(img).ok
    dark = (img * 0.1).astype("uint8")
    assert not G.assess_quality(dark).ok
