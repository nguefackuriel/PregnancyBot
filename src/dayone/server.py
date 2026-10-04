"""
Serveur HTTP : simulateur WhatsApp (page web) + API + adaptateur WhatsApp Cloud (bonus).

    DAYONE_READER=mock|tesseract|anthropic uvicorn dayone.server:app --reload --port 8000
    puis http://localhost:8000

API :
    POST /api/message        {sender, text?, button_id?, image? (data URL), filename?} -> {messages:[...]}
    POST /api/connectivity   {online: bool} -> notifications éventuelles
    GET  /api/state          état réseau, file, dossiers
    GET  /api/records/{id}   dossier (champs + statuts), image masquée via /api/records/{id}/image
                             (accès par rôle : en-tête X-Role = midwife | supervisor ; refusé sinon)
    GET  /webhook, POST /webhook : WhatsApp Business Cloud API, Meta (voir whatsapp.py)
    POST /twilio                 : bac à sable WhatsApp de Twilio (voir twilio_wa.py)
    GET  /media/{token}          : recadrage de cellule servi à Twilio (URL publique)
    GET  /health                 : lecteur, base, configuration WhatsApp (sans secrets)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from .agent import Agent, Incoming
from .reader.base import get_reader
from .schema import ROOT
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)       # la sonde réseau ne doit pas remplir le terminal


def _load_dotenv() -> int:
    """Lit le fichier .env du projet (ou DAYONE_ENV) et met ses valeurs dans l'environnement.

    Le fichier a priorité sur une variable déjà exportée : c'est lui la référence,
    comme ça relancer le serveur suffit après avoir changé un jeton.
    """
    path = Path(os.environ.get("DAYONE_ENV", ROOT / ".env"))
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k.startswith("export "):
            k = k[7:].strip()
        if v == "":
            continue
        os.environ[k] = v
        n += 1
    logging.getLogger("dayone.server").info("%s : %d variable(s) chargée(s)", path, n)
    return n


_load_dotenv()
READER = os.environ.get("DAYONE_READER", "crnn")
_kw = {}
if READER == "mock":
    _kw = {"uncertain_keys": os.environ.get("DAYONE_MOCK_UNCERTAIN", "age,gestation").split(",")}
store = Store(os.environ.get("DAYONE_DB", str(ROOT / "data" / "local" / "server.db")))
# simulation réseau (bouton « Simuler coupure ») : seulement pour le simulateur web / la démo.
# En production WhatsApp, l'état réseau est mesuré par la boucle de fond (whatsapp.run_forever).
SIMULATE = os.environ.get("DAYONE_SIMULATE_NETWORK", "0") == "1" or not os.environ.get("WHATSAPP_TOKEN")
agent = Agent(store, get_reader(READER, **_kw), online=os.environ.get("DAYONE_ONLINE", "1") == "1", simulate_network=SIMULATE)
app = FastAPI(title="DayOne, agent carnet maternel")
WEB = ROOT / "web"


class Message(BaseModel):
    sender: str
    text: str | None = None
    button_id: str | None = None
    image: str | None = None      # data URL
    filename: str | None = None


class Connectivity(BaseModel):
    online: bool


def _decode_image(data_url: str) -> tuple[bytes, str]:
    head, b64 = data_url.split(",", 1)
    mime = head.split(":")[1].split(";")[0] if ":" in head else "image/jpeg"
    return base64.b64decode(b64), mime


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "simulator.html").read_text(encoding="utf-8")


@app.get("/samples/{name}")
def sample(name: str):
    p = ROOT / "data" / "samples" / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return Response(content=p.read_bytes(), media_type="image/png" if p.suffix == ".png" else "image/jpeg")


@app.post("/api/message")
def api_message(m: Message):
    img, mime = (None, "image/jpeg")
    if m.image:
        img, mime = _decode_image(m.image)
    outs = agent.handle(Incoming(m.sender, text=m.text, button_id=m.button_id, image=img, image_mime=mime, filename=m.filename))
    return {"messages": [o.to_dict() for o in outs], "online": agent.online}


@app.post("/api/connectivity")
def api_connectivity(c: Connectivity):
    notes = agent.set_online(c.online)
    return {"online": agent.online, "notifications": [{"to": s, **o.to_dict()} for s, o in notes]}


@app.get("/api/state")
def api_state(sender: str | None = None):
    recs = store.list_records(sender)
    return {"online": agent.online, "reader": agent.reader.name,
            "queue": store.queue_items(),
            "records": recs,
            "patients": [{"id": p["id"], "code": p["code"], "n_pages": p["profile"].get("n_pages", 0),
                          "signals": p["profile"].get("signals", {})} for p in store.list_patients(sender)]}


@app.get("/api/records/{rid}")
def api_record(rid: str, x_role: str = Header(default="midwife")):
    if x_role not in ("midwife", "supervisor"):
        raise HTTPException(403, "rôle non autorisé")
    r = store.get(rid)
    if not r:
        raise HTTPException(404)
    r["history"] = store.history(rid)
    return r


@app.get("/api/records/{rid}/image")
def api_record_image(rid: str, x_role: str = Header(default="")):
    # image d'origine (masquée) : accès restreint aux rôles autorisés
    if x_role not in ("midwife", "supervisor"):
        raise HTTPException(403, "l'image d'origine est à accès restreint (X-Role: midwife|supervisor)")
    data, mime = store.get_image(rid)
    if not data:
        raise HTTPException(404)
    return Response(content=data, media_type=mime)


# ---------------------------------------------------------------- WhatsApp (vrai)
from .twilio_wa import TwilioWhatsApp  # noqa: E402
from .whatsapp import WhatsApp, graph_url  # noqa: E402

wa = WhatsApp(agent)
twilio = TwilioWhatsApp(agent)


@app.on_event("startup")
async def _start_background():
    # vraie résilience : surveillance de l'accès à Meta + rattrapage des boîtes et de la file
    if not wa.dry_run and os.environ.get("DAYONE_NO_BACKGROUND", "0") != "1":
        asyncio.create_task(wa.run_forever(float(os.environ.get("DAYONE_NET_CHECK_S", "15"))))


@app.get("/health")
def health():
    return {"ok": True, "reader": agent.reader.name, "online": agent.online, "network": agent.network_line(),
            "simulate_network": agent.simulate_network, "transport": store.transport_counts(),
            "ai_queue": len(store.queue_items()),
            "db": str(store.path) if hasattr(store, "path") else None,
            "whatsapp": {"configured": not wa.dry_run, "phone_id": wa.phone_id or None,
                         "token_tail": (wa.token[-4:] if wa.token else None),
                         "last_delivery_status": wa.last_status,
                         "signature_check": bool(wa.app_secret), "graph": graph_url()},
            "twilio": {"configured": not twilio.dry_run, "from": twilio.sender, "public_base": twilio.public_base or None}}


@app.get("/webhook")
def wa_verify(request: Request):
    ch = wa.verify(dict(request.query_params))
    if ch is None:
        raise HTTPException(403, "jeton de vérification incorrect")
    return PlainTextResponse(ch)


@app.post("/webhook")
async def wa_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not wa.signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "signature invalide")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "corps JSON attendu")
    # on répond tout de suite ; la lecture de la page se fait en arrière-plan
    background.add_task(wa.process, body)
    return JSONResponse({"status": "ok"})


@app.post("/twilio")
async def twilio_webhook(request: Request, background: BackgroundTasks):
    form = dict(await request.form())
    background.add_task(twilio.process, form)
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>", media_type="application/xml")


@app.get("/media/{token}")
def media(token: str):
    item = twilio.get_media(token)
    if not item:
        raise HTTPException(404)
    return Response(content=item[0], media_type=item[1])
