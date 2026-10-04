"""
Adaptateur Twilio WhatsApp (bac à sable) : plus simple à ouvrir que l'API Meta,
mais sans boutons interactifs. Les choix sont présentés numérotés et la sage-femme
répond par un chiffre.

Variables d'environnement :
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN     identifiants du compte Twilio
    TWILIO_WHATSAPP_FROM                      whatsapp:+14155238886 (numéro du bac à sable)
    PUBLIC_BASE_URL                           URL publique du serveur (ngrok), pour servir les
                                              recadrages de cellules en image sortante

Webhook à configurer dans la console Twilio (bac à sable WhatsApp, « When a message
comes in ») : POST https://<votre tunnel>/twilio
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time

from .agent import Agent, Incoming, Outgoing

log = logging.getLogger("dayone.twilio")
TEXT_MAX = 1500          # limite Twilio WhatsApp : 1600


class TwilioWhatsApp:
    def __init__(self, agent: Agent, sid: str | None = None, token: str | None = None, sender: str | None = None,
                 public_base: str | None = None):
        self.agent = agent
        self.sid = sid if sid is not None else os.environ.get("TWILIO_ACCOUNT_SID")
        self.token = token if token is not None else os.environ.get("TWILIO_AUTH_TOKEN")
        self.sender = sender or os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        self.public_base = (public_base or os.environ.get("PUBLIC_BASE_URL", "")).rstrip("/")
        self.lock = asyncio.Lock()
        self.last_buttons: dict[str, list[tuple[str, str]]] = {}
        self.media: dict[str, tuple[bytes, str, float]] = {}      # jeton -> (image, mime, date)
        self.sent: list[tuple[str, dict]] = []
        self.seen: dict[str, float] = {}

    @property
    def dry_run(self) -> bool:
        return not (self.sid and self.token)

    # ------------------------------------------------------------------ entrant
    def to_incoming(self, form: dict, media: bytes | None = None, mime: str = "image/jpeg") -> Incoming | None:
        sender = form.get("From", "")
        body = (form.get("Body") or "").strip()
        if media is not None:
            return Incoming(sender, image=media, image_mime=mime, filename=form.get("MessageSid"))
        if not body:
            return None
        bid = self._number_to_button(sender, body)
        return Incoming(sender, text=None if bid else body, button_id=bid)

    def _number_to_button(self, sender: str, txt: str) -> str | None:
        if re.fullmatch(r"\d{1,2}", txt.strip()):
            opts = self.last_buttons.get(sender) or []
            k = int(txt.strip())
            if 1 <= k <= len(opts):
                return opts[k - 1][0]
        return None

    async def process(self, form: dict) -> int:
        sid = form.get("MessageSid", "")
        if sid and sid in self.seen:
            return 0
        if sid:
            self.seen[sid] = time.time()
            if len(self.seen) > 2000:
                for k in list(self.seen)[:1000]:
                    self.seen.pop(k, None)
        sender = form.get("From", "")
        try:
            media, mime = None, "image/jpeg"
            n_media = int(form.get("NumMedia", "0") or 0)
            if n_media:
                mime = form.get("MediaContentType0", "image/jpeg")
                if not mime.startswith("image/"):
                    await self.send(sender, Outgoing("Je ne lis que des photos. Envoyez la photo d'une page du carnet."))
                    return 0
                media = await self.download_media(form.get("MediaUrl0", ""))
            inc = self.to_incoming(form, media, mime)
            if inc is None:
                await self.send(sender, Outgoing("Envoyez la photo d'une page du carnet, ou tapez *menu*."))
                return 0
            async with self.lock:
                outs = await asyncio.to_thread(self.agent.handle, inc)
            for o in outs:
                await self.send(sender, o)
            return 1
        except Exception:
            log.exception("échec du traitement du message %s", sid)
            try:
                await self.send(sender, Outgoing("Désolé, une erreur est survenue de mon côté. Renvoyez la photo, ou tapez *menu*."))
            except Exception:
                pass
            return 0

    async def download_media(self, url: str) -> bytes:
        if self.dry_run:
            raise RuntimeError("téléchargement impossible sans identifiants Twilio")
        import httpx
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(url, auth=(self.sid, self.token))
            r.raise_for_status()
            return r.content

    # ------------------------------------------------------------------ sortant
    def render(self, o: Outgoing) -> str:
        """Texte + choix numérotés (pas de boutons dans le bac à sable)."""
        text = (o.text or "").strip()
        if o.buttons:
            lines = [f"{k}. {t}" for k, (_, t) in enumerate(o.buttons, 1)]
            text = (text + "\n\n" if text else "") + "\n".join(lines) + "\n_(répondez par le numéro)_"
        return text

    def stash_media(self, data: bytes, mime: str) -> str | None:
        if not self.public_base:
            return None
        now = time.time()
        for k, (_, _, t) in list(self.media.items()):
            if now - t > 3600:
                self.media.pop(k, None)
        tok = secrets.token_urlsafe(12)
        self.media[tok] = (data, mime, now)
        return f"{self.public_base}/media/{tok}"

    def get_media(self, tok: str) -> tuple[bytes, str] | None:
        item = self.media.get(tok)
        return (item[0], item[1]) if item else None

    def payloads(self, to: str, o: Outgoing) -> list[dict]:
        text = self.render(o)
        msgs: list[dict] = []
        chunks = _chunks(text, TEXT_MAX) or [""]
        for k, chunk in enumerate(chunks):
            p = {"From": self.sender, "To": to, "Body": chunk}
            if k == 0 and o.image:
                url = self.stash_media(o.image, o.image_mime)
                if url:
                    p["MediaUrl"] = url
            if p["Body"] or "MediaUrl" in p:
                msgs.append(p)
        return msgs

    async def send(self, to: str, o: Outgoing):
        if o.buttons:
            self.last_buttons[to] = list(o.buttons)
        msgs = self.payloads(to, o)
        if self.dry_run:
            for p in msgs:
                self.sent.append((to, p))
                log.info("[twilio à blanc] -> %s : %s", to, p["Body"][:80])
            return
        import httpx
        async with httpx.AsyncClient(timeout=60) as c:
            for p in msgs:
                r = await c.post(f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                                 auth=(self.sid, self.token), data=p)
                if r.status_code >= 400:
                    log.error("Twilio -> %s %s", r.status_code, r.text[:300])


def _chunks(text: str, n: int) -> list[str]:
    text = text or ""
    if not text:
        return []
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out
