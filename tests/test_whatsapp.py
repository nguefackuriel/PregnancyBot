"""Adaptateurs WhatsApp (Meta Cloud API et Twilio) : webhooks réels rejoués contre le serveur,
sans réseau (lecteur simulé, envoi à blanc, média remplacé par une page du spécimen)."""
import hashlib
import hmac
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLE = ROOT / "data" / "samples" / "dossiers_specimen_10_patientes-02.png"
PHONE = "212600000042"


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYONE_ENV", str(tmp_path / "absent.env"))   # pas le .env du projet
    monkeypatch.setenv("DAYONE_READER", "mock")
    monkeypatch.setenv("DAYONE_DB", str(tmp_path / "wa.db"))
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "secret-verif")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_ID", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://exemple.ngrok.app")
    import dayone.server as server
    server = importlib.reload(server)
    from fastapi.testclient import TestClient

    async def fake_download(media_id, mime="image/jpeg"):
        return SAMPLE.read_bytes(), "image/png"

    async def fake_download_twilio(url):
        return SAMPLE.read_bytes()

    server.wa.download_media = fake_download
    server.twilio.download_media = fake_download_twilio
    return server, TestClient(server.app)


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _meta_message(m: dict) -> dict:
    """Enveloppe exacte d'un webhook « messages » de la Cloud API."""
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": "123"},
        "contacts": [{"profile": {"name": "Sage-femme"}, "wa_id": PHONE}], "messages": [m]}}]}]}


def test_webhook_verification_and_signature(srv):
    server, c = srv
    r = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "secret-verif", "hub.challenge": "4242"})
    assert r.status_code == 200 and r.text == "4242"
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "faux", "hub.challenge": "1"}).status_code == 403
    body = _meta_message({"from": PHONE, "id": "wamid.1", "timestamp": "1", "type": "text", "text": {"body": "bonjour"}})
    raw = json.dumps(body).encode()
    assert c.post("/webhook", content=raw, headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"}).status_code == 403
    assert c.get("/health").json()["whatsapp"]["signature_check"] is True


def test_text_then_photo_then_button_through_meta_webhook(srv):
    server, c = srv
    wa = server.wa
    # 1. « bonjour » -> menu avec boutons
    raw, h = _signed(_meta_message({"from": PHONE, "id": "wamid.a", "timestamp": "1", "type": "text", "text": {"body": "bonjour"}}))
    assert c.post("/webhook", content=raw, headers=h).json()["status"] == "ok"
    assert wa.sent and wa.sent[-1][0] == PHONE
    kinds = [p["type"] for _, p in wa.sent]
    assert "interactive" in kinds
    # 2. le même webhook renvoyé par Meta n'est pas retraité
    n = len(wa.sent)
    c.post("/webhook", content=raw, headers=h)
    assert len(wa.sent) == n
    # 3. photo d'une page -> résumé de la page (type de page reconnu)
    raw, h = _signed(_meta_message({"from": PHONE, "id": "wamid.b", "timestamp": "2", "type": "image",
                                    "image": {"id": "media-1", "mime_type": "image/jpeg", "sha256": "x"}}))
    c.post("/webhook", content=raw, headers=h)
    texts = " ".join(_text_of(p) for _, p in wa.sent[n:])
    assert "Identification" in texts
    # 4. réponse par bouton interactif (identifiant d'un bouton proposé)
    last_buttons = wa.last_buttons[PHONE]
    assert last_buttons
    bid, title = last_buttons[0]
    raw, h = _signed(_meta_message({"from": PHONE, "id": "wamid.c", "timestamp": "3", "type": "interactive",
                                    "interactive": {"type": "button_reply", "button_reply": {"id": bid, "title": title}}}))
    n = len(wa.sent)
    c.post("/webhook", content=raw, headers=h)
    assert len(wa.sent) > n
    # 5. un chiffre tapé vaut le bouton de ce rang
    n = len(wa.sent)
    raw, h = _signed(_meta_message({"from": PHONE, "id": "wamid.d", "timestamp": "4", "type": "text", "text": {"body": "1"}}))
    c.post("/webhook", content=raw, headers=h)
    assert len(wa.sent) > n
    # 6. un vocal -> on redemande une photo
    n = len(wa.sent)
    raw, h = _signed(_meta_message({"from": PHONE, "id": "wamid.e", "timestamp": "5", "type": "audio", "audio": {"id": "a"}}))
    c.post("/webhook", content=raw, headers=h)
    assert "photo" in _text_of(wa.sent[-1][1]).lower()


def test_payload_limits_buttons_lists_and_long_text(srv):
    server, _ = srv
    from dayone.agent import Outgoing
    wa = server.wa
    p = wa.payloads(PHONE, Outgoing("Choix", [("a", "Un libellé beaucoup trop long pour un bouton"), ("b", "B")]))
    assert p[0]["type"] == "interactive" and p[0]["interactive"]["type"] == "button"
    assert all(len(b["reply"]["title"]) <= 20 for b in p[0]["interactive"]["action"]["buttons"])
    p = wa.payloads(PHONE, Outgoing("Choix", [(f"k{i}", f"Option {i}") for i in range(12)]))
    lists = [m for m in p if m["interactive"]["type"] == "list"]
    assert len(lists) == 2 and all(len(m["interactive"]["action"]["sections"][0]["rows"]) <= 10 for m in lists)
    p = wa.payloads(PHONE, Outgoing("x" * 9000))
    assert [m["type"] for m in p] == ["text", "text", "text"] and all(len(m["text"]["body"]) <= 4000 for m in p)
    p = wa.payloads(PHONE, Outgoing("Cellule", image=b"jpg", image_mime="image/jpeg"))
    assert p[0]["type"] == "image" and p[0]["image"]["caption"] == "Cellule" and len(p) == 1


def test_twilio_sandbox_numbered_choices_and_media(srv):
    server, c = srv
    tw = server.twilio
    r = c.post("/twilio", data={"From": "whatsapp:+212600000042", "To": "whatsapp:+14155238886", "Body": "bonjour",
                                "NumMedia": "0", "MessageSid": "SM1"})
    assert r.status_code == 200 and "<Response>" in r.text
    assert tw.sent and "1." in tw.sent[-1][1]["Body"]          # boutons rendus en choix numérotés
    n = len(tw.sent)
    c.post("/twilio", data={"From": "whatsapp:+212600000042", "Body": "", "NumMedia": "1", "MessageSid": "SM2",
                            "MediaUrl0": "https://api.twilio.com/media/1", "MediaContentType0": "image/jpeg"})
    body = " ".join(p["Body"] for _, p in tw.sent[n:])
    assert "Identification" in body
    n = len(tw.sent)
    c.post("/twilio", data={"From": "whatsapp:+212600000042", "Body": "1", "NumMedia": "0", "MessageSid": "SM3"})
    assert len(tw.sent) > n
    # un recadrage sortant est servi sur une URL publique
    from dayone.agent import Outgoing
    p = tw.payloads("whatsapp:+212600000042", Outgoing("Cellule", image=b"\xff\xd8jpg", image_mime="image/jpeg"))
    url = p[0]["MediaUrl"]
    assert url.startswith("https://exemple.ngrok.app/media/")
    assert c.get(url.replace("https://exemple.ngrok.app", "")).content == b"\xff\xd8jpg"


def _text_of(p: dict) -> str:
    if p["type"] == "text":
        return p["text"]["body"]
    if p["type"] == "image":
        return p["image"].get("caption", "")
    return p["interactive"]["body"]["text"]


def _wa(tmp_path, name="r.db"):
    from dayone.agent import Agent
    from dayone.reader.base import get_reader
    from dayone.store import Store
    from dayone.whatsapp import WhatsApp
    agent = Agent(Store(tmp_path / name), get_reader("mock"), online=True)
    return WhatsApp(agent, token=None, phone_id=None), agent


def test_real_resilience_inbox_outbox_and_restart(tmp_path):
    """Pas de simulation : réseau coupé pendant le téléchargement, puis pendant la réponse, puis redémarrage."""
    import asyncio
    import time
    from dayone.whatsapp import OfflineError, WhatsApp
    wa, agent = _wa(tmp_path)
    photo = _meta_message({"from": PHONE, "id": "wamid.p1", "timestamp": str(int(time.time()) - 3 * 3600), "type": "image",
                           "image": {"id": "media-9", "mime_type": "image/jpeg"}})

    # 1. réseau coupé : le média ne se télécharge pas -> message gardé en boîte d'entrée, agent hors ligne
    async def down(media_id, mime="image/jpeg"):
        raise OfflineError("connexion refusée")
    wa.download_media = down
    assert asyncio.run(wa.process(photo)) == 0
    assert agent.store.transport_counts()["inbox"] == 1 and agent.online is False

    # 2. réseau revenu : flush rejoue le message ; la photo (envoyée il y a 3 h) est annoncée en retard,
    #    avec sa date de capture d'origine, puis lue normalement
    async def up(media_id, mime="image/jpeg"):
        return SAMPLE.read_bytes(), "image/png"
    wa.download_media = up
    agent.set_online(True)
    done = asyncio.run(wa.flush(due_only=False))          # retour du réseau : rattrapage immédiat
    assert done["inbox"] == 1 and agent.store.transport_counts()["inbox"] == 0
    texts = [_text_of(p) for _, p in wa.sent]
    assert any("retard" in t for t in texts) and any("Identification" in t for t in texts)
    rec = agent.store.list_records(PHONE)[-1]
    captured = agent.store.get(rec["id"])["payload"]["captured_at"]
    assert captured < time.strftime("%Y-%m-%dT%H:%M", time.gmtime(time.time() - 2 * 3600))

    # 3. Meta renvoie le même webhook (pas de 200 reçu de son côté) : ignoré, même après « redémarrage »
    wa2 = WhatsApp(agent, token=None, phone_id=None)
    wa2.download_media = up
    assert asyncio.run(wa2.process(photo)) == 0

    # 4. réseau coupé au moment de répondre : la réponse va en boîte de sortie, puis part au retour
    async def no_send(to, o):
        raise OfflineError("délai dépassé")
    real_send = wa2._send_now
    wa2._send_now = no_send
    txt = _meta_message({"from": PHONE, "id": "wamid.t9", "timestamp": str(int(time.time())), "type": "text", "text": {"body": "menu"}})
    asyncio.run(wa2.process(txt))
    assert agent.store.transport_counts()["outbox"] >= 1 and agent.store.transport_counts()["inbox"] == 0
    wa2._send_now = real_send
    agent.set_online(True)
    before = len(wa2.sent)
    done = asyncio.run(wa2.flush(due_only=False))
    assert done["outbox"] >= 1 and agent.store.transport_counts()["outbox"] == 0 and len(wa2.sent) > before


def test_no_simulation_button_in_production_menu(tmp_path):
    import asyncio
    wa, agent = _wa(tmp_path, "m.db")
    asyncio.run(wa.process(_meta_message({"from": PHONE, "id": "wamid.m1", "timestamp": "1", "type": "text", "text": {"body": "menu"}})))
    titles = [b["reply"]["title"] for _, p in wa.sent if p["type"] == "interactive" and p["interactive"]["type"] == "button"
              for b in p["interactive"]["action"]["buttons"]]
    assert not any("coupure" in t.lower() or "réseau" in t.lower() for t in titles)
    asyncio.run(wa.process(_meta_message({"from": PHONE, "id": "wamid.m2", "timestamp": "1", "type": "text", "text": {"body": "connexion"}})))
    assert "en ligne" in _text_of(wa.sent[-1][1]) and agent.online is True     # état réel, pas de bascule


def test_message_in_progress_is_not_processed_twice(srv):
    """Une photo prend quelques secondes : la boucle de fond (ou un renvoi de Meta) qui passe
    pendant ce temps ne doit pas la traiter une deuxième fois (questions en double)."""
    import asyncio
    server, c = srv
    wa = server.wa
    m = {"from": PHONE, "id": "wamid.lent", "timestamp": "1", "type": "image", "image": {"id": "media-1", "mime_type": "image/png"}}
    calls = {"n": 0}
    real_handle = wa.agent.handle

    def slow_handle(inc):
        calls["n"] += 1
        import time
        time.sleep(0.5)
        return real_handle(inc)

    wa.agent.handle = slow_handle

    async def scenario():
        first = asyncio.create_task(wa.process(_meta_message(m)))
        await asyncio.sleep(0.1)                      # en plein traitement
        again = await wa.process(_meta_message(m))    # renvoi de Meta
        flushed = await wa.flush(due_only=False)      # passage de la boucle de fond
        await first
        return again, flushed

    again, flushed = asyncio.run(scenario())
    assert calls["n"] == 1
    assert again == 0 and flushed["inbox"] == 0


def test_tap_on_an_old_button_is_not_replayed(srv):
    server, c = srv
    wa = server.wa
    wa.last_interactive[PHONE] = "wamid.question2"
    old_tap = {"from": PHONE, "id": "wamid.tap", "timestamp": "1", "type": "interactive",
               "context": {"from": "15550000000", "id": "wamid.question1"},
               "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "Oui"}}}
    called = {"n": 0}
    wa.agent.handle = lambda inc: called.__setitem__("n", called["n"] + 1) or []
    raw, h = _signed(_meta_message(old_tap))
    c.post("/webhook", content=raw, headers=h)
    assert called["n"] == 0
    assert "question précédente" in json.dumps(wa.sent[-1][1], ensure_ascii=False)
    # le bouton du dernier message, lui, passe
    new_tap = dict(old_tap, id="wamid.tap2", context={"from": "15550000000", "id": "wamid.question2"})
    raw, h = _signed(_meta_message(new_tap))
    c.post("/webhook", content=raw, headers=h)
    assert called["n"] == 1
