"""Test de bout en bout du flux conversationnel (lecteur simulé, sans réseau)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.agent import Agent, Incoming  # noqa: E402
from dayone.reader.base import get_reader  # noqa: E402
from dayone.store import Store  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
SF = "+212600000009"


def _photo(name):
    p = SAMPLES / name
    return Incoming(SF, image=p.read_bytes(), image_mime="image/png", filename=name)


def _agent(tmp_path, online, uncertain=()):
    return Agent(Store(tmp_path / "a.db"), get_reader("mock", uncertain_keys=list(uncertain)), online=online)


def test_offline_capture_then_online_processing(tmp_path):
    ag = _agent(tmp_path, online=False, uncertain=["age"])
    out = ag.handle(_photo("dossiers_specimen_10_patientes-02.png"))
    assert "En attente de traitement IA" in out[0].text and "coupé" in out[0].text
    rid = ag.store.load_session(SF)["record_id"]
    assert ag.store.get(rid)["state"] == "EN_ATTENTE_IA" and len(ag.store.queue_items()) == 1
    notes = ag.set_online(True)                       # retour du réseau => traitement
    assert ag.store.get(rid)["state"] == "A_REVISER"
    assert any("Age" in o.text for _, o in notes)     # question sur le champ incertain
    # confirmation, validation, nouveau profil
    ag.handle(Incoming(SF, button_id="yes"))
    ag.handle(Incoming(SF, button_id="confirm"))
    out = ag.handle(Incoming(SF, text="aucun"))
    assert ag.store.get(rid)["state"] == "SYNCHRONISE"
    pid = ag.store.get(rid)["patient_id"]
    assert ag.store.get_patient(pid)["code"]
    states = [e["to_state"] for e in ag.store.history(rid)]
    assert states == ["CAPTURE", "EN_ATTENTE_IA", "TRAITE_IA", "A_REVISER", "VALIDE", "PATIENTE_LIEE", "ENREGISTRE", "SYNCHRONISE"]


def test_pii_never_stored(tmp_path):
    ag = _agent(tmp_path, online=True)
    ag.handle(_photo("dossiers_specimen_10_patientes-02.png"))
    rid = ag.store.load_session(SF)["record_id"]
    rec = ag.store.get(rid)
    for k in ("cin", "adresse", "telephone", "nom_mari"):
        assert rec["payload"]["fields"][k]["status"] == "REDACTED"
        assert rec["payload"]["fields"][k]["value"] is None
    assert rec["payload"]["pii_boxes"]                      # zones masquées sur l'image stockée


def test_correction_and_linking_choice(tmp_path):
    ag = _agent(tmp_path, online=True)
    ag.handle(_photo("dossiers_specimen_10_patientes-02.png"))
    ag.handle(Incoming(SF, button_id="correct"))
    out = ag.handle(Incoming(SF, text="1 = 29"))              # champ n°1 = Age
    rid = ag.store.load_session(SF)["record_id"]
    f = ag.store.get(rid)["payload"]["fields"]["age"]
    assert f["value"] == "29" and f["source"] == "midwife" and f["confidence"] == 1.0
    ag.handle(Incoming(SF, button_id="confirm"))
    ag.handle(Incoming(SF, text="AB12"))
    ag.handle(Incoming(SF, button_id="next:no"))
    # second carnet, même code : correspondance proposée, jamais de création automatique
    ag.handle(_photo("dossiers_specimen_10_patientes-04.png"))
    ag.handle(Incoming(SF, button_id="confirm"))
    out = ag.handle(Incoming(SF, text="AB12"))
    assert any("Patiente 1" in t for _, t in out[0].buttons) and any("Aucune" in t for _, t in out[0].buttons)
    ag.handle(Incoming(SF, button_id="link:unsure"))
    rid2 = ag.store.load_session(SF)["record_id"]
    assert ag.store.get(rid2)["state"] == "DOUBLON_SUSPECTE"


def test_manual_entry_when_ai_unavailable(tmp_path):
    class Broken:
        name = "broken"
        supports_classification = False

        def read_fields(self, *a, **k):
            raise RuntimeError("service IA indisponible")

    ag = Agent(Store(tmp_path / "m.db"), Broken(), online=True)
    out = ag.handle(_photo("dossiers_specimen_10_patientes-04.png"))
    assert any("indisponible" in o.text for o in out)
    out = ag.handle(Incoming(SF, button_id="manual"))
    assert any(b[0].startswith("pt:") for b in out[0].buttons)
    ag.handle(Incoming(SF, button_id="pt:ACCOUCHEMENT"))
    ag.handle(Incoming(SF, text="03/02/2026"))         # date_accouchement
    ag.handle(Incoming(SF, text="F"))                  # sexe
    ag.handle(Incoming(SF, text="3,2 kg"))             # poids -> 3200
    ag.handle(Incoming(SF, text="passer"))
    out = ag.handle(Incoming(SF, text="40 SA"))
    rid = ag.store.load_session(SF)["record_id"]
    f = ag.store.get(rid)["payload"]["fields"]
    assert f["poids_naissance_g"]["value"] == "3200" and f["sexe"]["value"] == "F"
    assert f["perimetre_cranien_cm"]["status"] == "INCONNU"
    assert ag.store.get(rid)["state"] == "REVISION_MANUELLE_REQUISE"
    ag.handle(Incoming(SF, button_id="confirm"))
    assert ag.store.get(rid)["state"] in ("VALIDE",) or ag.store.load_session(SF)["mode"] == "linking_code"


def test_duplicate_photo_rejected(tmp_path):
    ag = _agent(tmp_path, online=True)
    ag.handle(_photo("dossiers_specimen_10_patientes-04.png"))
    out = ag.handle(_photo("dossiers_specimen_10_patientes-04.png"))
    assert "déjà été envoyée" in out[0].text
