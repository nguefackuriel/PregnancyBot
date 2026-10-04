"""
Adaptateur WhatsApp Business Cloud API (Meta) : webhook -> Incoming -> Outgoing -> Graph API.

Variables d'environnement :
    WHATSAPP_TOKEN          jeton d'accès (temporaire 24 h depuis le tableau de bord, ou permanent)
    WHATSAPP_PHONE_ID       « Phone number ID » du numéro de test ou du numéro réel
    WHATSAPP_VERIFY_TOKEN   mot choisi par vous, recopié dans la configuration du webhook (défaut : dayone)
    WHATSAPP_APP_SECRET     facultatif, « App secret » de l'app Meta : vérifie la signature des webhooks
    WHATSAPP_GRAPH_VERSION  facultatif, défaut v21.0

Ce que fait l'adaptateur :
  - répond tout de suite 200 au webhook et traite le message en arrière-plan
    (Meta renvoie un webhook non acquitté en quelques secondes) ;
  - ignore un message déjà vu (même id), donc un webhook renvoyé ne relit pas la page ;
  - un message à la fois : l'agent est séquentiel, la lecture d'une page prend 2 à 4 s ;
  - marque le message comme lu (coches bleues) pendant la lecture ;
  - boutons : 3 au plus par message WhatsApp, au-delà une liste (10 lignes au plus),
    au-delà encore plusieurs listes ; un chiffre tapé au clavier vaut le bouton de ce rang ;
  - texte long découpé en morceaux de 4000 caractères ;
  - image sortante (recadrage de la cellule) envoyée via le Media API ;
  - photo entrante téléchargée avec le jeton ; une photo envoyée en « document » est acceptée.
  - vraie résilience, sans simulation : chaque message reçu est mis dans une boîte
    d'entrée chiffrée avant traitement, chaque réponse qui ne part pas (réseau coupé,
    Meta indisponible) va dans une boîte de sortie chiffrée ; `run_forever` mesure
    l'accès réel à Meta, met l'agent hors ligne / en ligne, et rejoue tout au retour ;
  - l'heure réelle d'envoi par la sage-femme (WhatsApp la transmet) devient la date
    de capture, et l'agent prévient quand une photo arrive en retard.
Sans jeton, l'adaptateur affiche les messages au lieu de les envoyer (mode à blanc).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import time
from collections import OrderedDict

from .agent import Agent, Incoming, Outgoing

log = logging.getLogger("dayone.whatsapp")

TEXT_MAX = 4000          # limite WhatsApp : 4096
BODY_MAX = 1024          # corps d'un message interactif
BUTTON_TITLE_MAX = 20
ROW_TITLE_MAX = 24
LIST_ROWS_MAX = 10


STATUS_HELP = {
    131026: "le destinataire n'a pas WhatsApp, ou le numéro est faux (indicatif pays compris), ou il a bloqué le numéro.",
    131047: "fenêtre de 24 h fermée : le destinataire doit écrire au numéro en premier.",
    131049: "Meta a choisi de ne pas livrer (limite de messages non sollicités) : le destinataire doit écrire en premier.",
    131053: "média refusé (type ou taille).",
    130472: "le destinataire est dans une expérience Meta qui bloque les messages d'entreprise.",
    131030: "numéro absent de la liste des destinataires autorisés du numéro de test.",
    470: "le destinataire n'a pas écrit depuis plus de 24 h.",
}


def graph_url() -> str:
    return f"https://graph.facebook.com/{os.environ.get('WHATSAPP_GRAPH_VERSION', 'v21.0')}"


class OfflineError(RuntimeError):
    """Meta injoignable (réseau coupé, DNS, délai dépassé, 5xx) : à rejouer plus tard."""


LATE_AFTER_S = 120          # au-delà, on dit à la sage-femme que sa photo est arrivée en retard


class WhatsApp:
    def __init__(self, agent: Agent, token: str | None = None, phone_id: str | None = None,
                 verify_token: str | None = None, app_secret: str | None = None):
        self.agent = agent
        self.store = getattr(agent, "store", None)
        self.token = token if token is not None else os.environ.get("WHATSAPP_TOKEN")
        self.phone_id = phone_id if phone_id is not None else os.environ.get("WHATSAPP_PHONE_ID")
        self.verify_token = verify_token or os.environ.get("WHATSAPP_VERIFY_TOKEN", "dayone")
        self.app_secret = app_secret if app_secret is not None else os.environ.get("WHATSAPP_APP_SECRET")
        self.lock = asyncio.Lock()
        self.seen: OrderedDict[str, float] = OrderedDict()      # ids de messages déjà traités
        self.last_buttons: dict[str, list[tuple[str, str]]] = {}  # par expéditeur : derniers boutons proposés
        self.sent: list[tuple[str, dict]] = []                    # mode à blanc : messages « envoyés »
        self.last_status: dict[str, dict] = {}                    # dernier statut de livraison par destinataire
        self.alias: dict[str, str] = {}                           # id WhatsApp -> forme du numéro acceptée par Meta
        # messages en cours de traitement : une photo prend quelques secondes ; pendant ce temps
        # la boucle de fond ou un renvoi de Meta ne doivent pas la traiter une deuxième fois
        self.inflight: set[str] = set()
        # dernier message à boutons envoyé à chaque personne (id WhatsApp) : un appui sur un
        # bouton d'un message plus ancien (double appui, retour en arrière) n'est pas rejoué
        self.last_interactive: dict[str, str] = {}

    @property
    def dry_run(self) -> bool:
        return not (self.token and self.phone_id)

    # ------------------------------------------------------------------ webhook
    def verify(self, params: dict) -> str | None:
        """GET /webhook : renvoie le challenge si le jeton de vérification est le bon."""
        if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == self.verify_token:
            return params.get("hub.challenge", "")
        return None

    def signature_ok(self, raw: bytes, header: str | None) -> bool:
        """X-Hub-Signature-256 = sha256=HMAC(app_secret, corps brut). Sans app secret : pas de contrôle."""
        if not self.app_secret:
            return True
        if not header or not header.startswith("sha256="):
            return False
        mac = hmac.new(self.app_secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, header[7:])

    def parse(self, body: dict) -> list[dict]:
        """Corps du webhook -> liste de messages {id, from, type, ...}.

        Les événements « statuses » (envoyé, livré, lu, échec) ne sont pas des
        messages : ils sont journalisés, et un échec est expliqué en clair.
        """
        out = []
        for entry in body.get("entry", []) or []:
            for ch in entry.get("changes", []) or []:
                value = ch.get("value", {}) or {}
                for st in value.get("statuses", []) or []:
                    self._log_status(st)
                for m in value.get("messages", []) or []:
                    out.append(m)
        return out

    def _log_status(self, st: dict):
        status, to = st.get("status"), st.get("recipient_id")
        if status == "failed":
            for e in st.get("errors", []) or []:
                code = e.get("code")
                detail = (e.get("error_data") or {}).get("details") or e.get("message") or e.get("title")
                log.warning("message vers %s NON LIVRÉ (code %s) : %s -> %s", to, code, detail,
                            STATUS_HELP.get(code, "voir la page des codes d'erreur Meta"))
            self.last_status[to] = {"status": "failed", "errors": st.get("errors", [])}
        else:
            log.info("statut vers %s : %s", to, status)
            self.last_status[to] = {"status": status}

    def _is_new(self, mid: str) -> bool:
        """True si le message n'a jamais été traité (mémoire + base, qui survit au redémarrage)."""
        if not mid:
            return True
        if mid in self.seen or mid in self.inflight:
            return False
        if self.store is not None and self.store.is_processed(mid):
            return False
        return True

    def _done(self, mid: str):
        if not mid:
            return
        self.seen[mid] = 1.0
        while len(self.seen) > 2000:
            self.seen.popitem(last=False)
        if self.store is not None:
            self.store.mark_processed(mid)

    def to_incoming(self, m: dict, media: bytes | None = None, mime: str = "image/jpeg") -> Incoming | None:
        sender = m.get("from", "")
        t = m.get("type")
        if t == "text":
            txt = (m.get("text") or {}).get("body", "")
            bid = self._number_to_button(sender, txt)
            return Incoming(sender, text=None if bid else txt, button_id=bid)
        if t == "interactive":
            i = m.get("interactive") or {}
            reply = i.get("button_reply") or i.get("list_reply") or {}
            return Incoming(sender, button_id=reply.get("id"), text=reply.get("title"))
        if t == "button":          # réponse à un modèle avec boutons
            return Incoming(sender, text=(m.get("button") or {}).get("text"))
        if t in ("image", "document") and media is not None:
            return Incoming(sender, image=media, image_mime=mime, filename=(m.get(t) or {}).get("filename") or m.get("id"),
                            sent_at=_ts(m))
        return None

    def _stale_tap(self, m: dict) -> bool:
        """True si c'est un appui sur un bouton d'un message plus ancien que le dernier message à boutons."""
        if m.get("type") != "interactive":
            return False
        ctx = (m.get("context") or {}).get("id")
        last = self.last_interactive.get(m.get("from", ""))
        return bool(ctx and last and ctx != last)

    def _number_to_button(self, sender: str, txt: str) -> str | None:
        """« 2 » tapé au clavier vaut le deuxième bouton proposé (utile sans boutons interactifs)."""
        s = txt.strip()
        if re.fullmatch(r"\d{1,2}", s):
            opts = self.last_buttons.get(sender) or []
            k = int(s)
            if 1 <= k <= len(opts):
                return opts[k - 1][0]
        return None

    async def process(self, body: dict) -> int:
        """Traite tous les messages d'un webhook. Renvoie le nombre de messages traités.

        Chaque message est d'abord mis dans la boîte d'entrée (persistante) ; il n'en
        sort qu'une fois traité. Si Meta est injoignable au milieu (média à télécharger,
        réponse à envoyer), le message y reste et `flush` le rejouera.
        """
        n = 0
        for m in self.parse(body):
            mid = m.get("id", "")
            if not self._is_new(mid):
                log.info("message déjà traité ou en cours, ignoré : %s", mid)
                continue
            if self.store is not None:
                self.store.inbox_put(mid, m.get("from", ""), m)
            if await self._handle_one(m):
                n += 1
        return n

    async def _handle_one(self, m: dict) -> bool:
        """Un message -> réponses. True si traité (ou abandonné), False s'il faut réessayer plus tard."""
        mid = m.get("id", "")
        if mid:
            if mid in self.inflight:
                return True                          # déjà en cours ailleurs (boucle de fond, renvoi de Meta)
            self.inflight.add(mid)
        try:
            return await self._handle_one_inner(m)
        finally:
            self.inflight.discard(mid)

    async def _handle_one_inner(self, m: dict) -> bool:
        mid, sender, t = m.get("id", ""), m.get("from", ""), m.get("type")
        log.info("message %s reçu de +%s (numéro tel que WhatsApp l'identifie)", t, sender)
        try:
            await self.mark_read(mid)
        except OfflineError:
            pass
        try:
            media, mime = None, "image/jpeg"
            if t in ("image", "document"):
                info = m.get(t) or {}
                mime = info.get("mime_type", "image/jpeg")
                if not mime.startswith("image/"):
                    await self.send(sender, Outgoing("Je ne lis que des photos. Envoyez la photo d'une page du carnet."))
                    self._done(mid)
                    return True
                media, mime = await self.download_media(info.get("id", ""), mime)
            if self._stale_tap(m):
                # bouton d'une question déjà passée : on ne l'applique pas à la question en cours
                log.info("appui sur un ancien bouton ignoré (%s)", mid)
                await self.send(sender, Outgoing("Ce bouton correspond à une question précédente. "
                                                 "Répondez à la dernière question, juste au-dessus."))
                self._done(mid)
                return True
            inc = self.to_incoming(m, media, mime)
            if inc is None:
                await self.send(sender, Outgoing("Envoyez la photo d'une page du carnet, ou tapez *menu*."))
                self._done(mid)
                return True
            late = _late_notice(m)
            if late:
                await self.send(sender, Outgoing(late))
            async with self.lock:
                outs = await asyncio.to_thread(self.agent.handle, inc)
            for o in outs:
                await self.send(sender, o)
            self._done(mid)
            return True
        except OfflineError as e:
            # réseau coupé côté serveur : on garde le message, on réessaie plus tard
            log.warning("Meta injoignable pendant le traitement de %s (%s) : message gardé en boîte d'entrée", mid, e)
            if self.store is not None:
                self.store.inbox_bump(mid, delay_s=30)
            if self.agent.online:
                self.agent.set_online(False)
            return False
        except Exception:                        # on ne laisse jamais la sage-femme sans réponse
            log.exception("échec du traitement du message %s", mid)
            self._done(mid)
            try:
                await self.send(sender, Outgoing("Désolé, une erreur est survenue de mon côté. Renvoyez la photo, ou tapez *menu*."))
            except Exception:
                pass
            return True

    # ------------------------------------------------------------------ résilience
    async def connectivity(self) -> bool:
        """Meta est-il joignable maintenant ? (une requête légère, 5 s au plus)."""
        if self.dry_run:
            return True
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{graph_url()}/{self.phone_id}", headers=self._headers())
        except httpx.HTTPError:
            return False
        if r.status_code == 200:
            return True
        try:
            err = (r.json() or {}).get("error") or {}
        except Exception:
            return False
        # Meta a répondu lui-même (jeton expiré, droit manquant) : le réseau marche, le problème est ailleurs
        if "fbtrace_id" in err:
            if err.get("code") == 190:
                log.error("jeton WhatsApp invalide ou expiré : les réponses ne partiront pas tant qu'il n'est pas renouvelé")
            return True
        return False

    async def flush(self, due_only: bool = True) -> dict:
        """Rejoue la boîte d'entrée, la boîte de sortie et la file IA. Renvoie des compteurs.

        due_only=False : tout de suite, sans attendre la pause entre deux essais
        (utilisé au retour du réseau).
        """
        done = {"inbox": 0, "outbox": 0, "queue": 0}
        if self.store is None:
            return done
        for item in self.store.inbox_items(due_only=due_only):
            if not self._is_new(item["message_id"]):
                self.store.inbox_drop(item["message_id"])
                continue
            if await self._handle_one(item["data"]):
                done["inbox"] += 1
            else:
                break                                # toujours coupé : inutile d'insister
        for item in self.store.outbox_items(due_only=due_only):
            o = _outgoing_from(item["data"])
            try:
                await self._send_now(item["recipient"], o)
                self.store.outbox_done(item["id"])
                done["outbox"] += 1
            except OfflineError:
                self.store.outbox_bump(item["id"], delay_s=30)
                break
        async with self.lock:
            notes = await asyncio.to_thread(self.agent.process_queue)
        for sender, o in notes:
            await self.send(sender, o)
            done["queue"] += 1
        return done

    async def run_forever(self, interval: float = 15.0):
        """Boucle de fond : mesure l'accès réel à Meta, bascule l'agent, rejoue tout au retour."""
        log.info("surveillance réseau démarrée (toutes les %.0f s)", interval)
        while True:
            try:
                online = await self.connectivity()
                just_back = online and not self.agent.online
                if online != self.agent.online:
                    log.warning("réseau %s", "rétabli" if online else "coupé")
                    async with self.lock:
                        notes = await asyncio.to_thread(self.agent.set_online, online)
                    for sender, o in notes:
                        await self.send(sender, o)
                if online:
                    done = await self.flush(due_only=not just_back)
                    if any(done.values()):
                        log.info("rattrapage : %s", done)
            except Exception:
                log.exception("boucle de fond")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ Graph API
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _post(self, path: str, **kw) -> dict:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{graph_url()}/{path}", headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise OfflineError(str(e) or e.__class__.__name__)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        if r.status_code >= 500 or r.status_code == 429:
            raise OfflineError(f"Meta {r.status_code}")
        if r.status_code >= 400:
            log.error("Graph API %s -> %s %s", path, r.status_code, data)
            code = (data.get("error") or {}).get("code")
            if code == 131030 and path.endswith("/messages"):
                to = (kw.get("json") or {}).get("to", "?")
                log.error("-> le numéro de test ne peut répondre qu'aux numéros de sa liste. WhatsApp identifie cette "
                          "personne comme +%s : ajoutez EXACTEMENT ce numéro dans la liste des destinataires (il peut "
                          "différer du numéro composé : ancien format sans le 6 au Cameroun, 9 en plus en Argentine, "
                          "1 en plus au Mexique).", to)
        return data

    async def mark_read(self, mid: str):
        if self.dry_run or not mid:
            return
        await self._post(f"{self.phone_id}/messages", json={"messaging_product": "whatsapp", "status": "read", "message_id": mid})

    async def download_media(self, media_id: str, mime: str = "image/jpeg") -> tuple[bytes, str]:
        """Photo entrante. OfflineError = réseau ou Meta indisponible (à rejouer) ;
        RuntimeError = Meta a refusé (média expiré, id inconnu : inutile de réessayer)."""
        if self.dry_run:
            raise RuntimeError("téléchargement impossible sans jeton")
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r0 = await c.get(f"{graph_url()}/{media_id}", headers=self._headers())
                if r0.status_code >= 500 or r0.status_code == 429:
                    raise OfflineError(f"Meta {r0.status_code}")
                try:
                    meta = r0.json()
                except Exception:
                    raise OfflineError(f"réponse inattendue ({r0.status_code}), pas de Meta")
                if "error" in meta:
                    raise RuntimeError(f"média refusé par Meta : {meta['error'].get('message')}")
                if "url" not in meta:
                    raise RuntimeError(f"média introuvable : {meta}")
                r = await c.get(meta["url"], headers=self._headers())
                if r.status_code >= 500 or r.status_code == 429:
                    raise OfflineError(f"Meta {r.status_code}")
                if r.status_code >= 400:
                    raise RuntimeError(f"média non téléchargeable ({r.status_code})")
                return r.content, meta.get("mime_type", mime)
        except httpx.HTTPError as e:
            raise OfflineError(str(e) or e.__class__.__name__)

    async def upload_media(self, data: bytes, mime: str = "image/jpeg") -> str | None:
        ext = "png" if "png" in mime else "jpg"
        res = await self._post(f"{self.phone_id}/media", data={"messaging_product": "whatsapp", "type": mime},
                               files={"file": (f"cellule.{ext}", data, mime)})
        return res.get("id")

    def payloads(self, to: str, o: Outgoing) -> list[dict]:
        """Un Outgoing -> un ou plusieurs messages Graph API, dans l'ordre d'envoi."""
        base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
        msgs: list[dict] = []
        text = (o.text or "").strip()
        if o.image:
            caption = text if (not o.buttons and len(text) <= BODY_MAX) else ""
            msgs.append({**base, "type": "image", "image": {"id": None, "caption": caption} if caption else {"id": None}})
            if caption:
                text = ""
        if not o.buttons:
            for chunk in _chunks(text, TEXT_MAX):
                msgs.append({**base, "type": "text", "text": {"preview_url": False, "body": chunk}})
            return msgs
        # texte au-dessus de la limite du corps interactif : on envoie le début à part
        body = text
        if len(body) > BODY_MAX:
            head, body = body[: -BODY_MAX].rstrip(), body[-BODY_MAX:].lstrip()
            for chunk in _chunks(head, TEXT_MAX):
                msgs.append({**base, "type": "text", "text": {"preview_url": False, "body": chunk}})
        body = body or "Choisissez :"
        if len(o.buttons) <= 3:
            msgs.append({**base, "type": "interactive", "interactive": {
                "type": "button", "body": {"text": body},
                "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": _short(t, BUTTON_TITLE_MAX)}}
                                       for i, t in o.buttons]}}})
        else:
            groups = [o.buttons[k: k + LIST_ROWS_MAX] for k in range(0, len(o.buttons), LIST_ROWS_MAX)]
            for g_no, group in enumerate(groups):
                msgs.append({**base, "type": "interactive", "interactive": {
                    "type": "list", "body": {"text": body if g_no == 0 else "Suite des choix :"},
                    "action": {"button": "Choisir", "sections": [{"title": "Options", "rows": [
                        {"id": i[:200], "title": _short(t, ROW_TITLE_MAX)} for i, t in group]}]}}})
        return msgs

    async def send(self, to: str, o: Outgoing):
        """Envoie ; si Meta est injoignable, garde la réponse en boîte de sortie (chiffrée)."""
        try:
            await self._send_now(to, o)
        except OfflineError as e:
            log.warning("réponse vers %s gardée en boîte de sortie (%s)", to, e)
            if self.store is not None:
                self.store.outbox_put(to, _outgoing_to(o))
            if self.agent is not None and self.agent.online:
                self.agent.set_online(False)

    async def _send_now(self, to: str, o: Outgoing):
        if o.buttons:
            self.last_buttons[to] = list(o.buttons)
        dest = self.alias.get(to, to)          # forme du numéro acceptée par Meta pour cette personne
        msgs = self.payloads(dest, o)
        if self.dry_run:
            for p in msgs:
                self.sent.append((to, p))
                log.info("[whatsapp à blanc] -> %s : %s", to, _describe(p))
            return
        for p in msgs:
            if p["type"] == "image":
                mid = await self.upload_media(o.image, o.image_mime)
                if not mid:
                    continue
                p["image"]["id"] = mid
            data = await self._post(f"{self.phone_id}/messages", json=p)
            if p["type"] == "interactive":
                sent_id = ((data.get("messages") or [{}])[0] or {}).get("id")
                if sent_id:
                    self.last_interactive[to] = sent_id
            if (data.get("error") or {}).get("code") == 131030 and dest == to:
                # numéro de test : la liste des destinataires contient la forme « composée » du numéro,
                # WhatsApp nous donne parfois une autre forme (Cameroun, Argentine, Mexique) : on essaie
                for alt in _alternates(to):
                    p2 = dict(p, to=alt)
                    d2 = await self._post(f"{self.phone_id}/messages", json=p2)
                    if "error" not in d2:
                        log.warning("réponse livrée à +%s sous la forme +%s : je garde cette forme pour la suite", to, alt)
                        self.alias[to] = alt
                        dest = alt
                        for q in msgs:
                            q["to"] = alt
                        break


def _alternates(number: str) -> list[str]:
    """Autres formes du même numéro telles que WhatsApp ou l'annuaire peuvent les écrire.

    Cameroun : les comptes d'avant 2014 ont un id à 8 chiffres, le numéro composé en a 9 (6 en tête).
    Argentine : l'id porte un 9 après 54 que le numéro composé n'a pas. Mexique : idem avec un 1 après 52.
    """
    n = re.sub(r"\D", "", number or "")
    out = []
    if n.startswith("237") and len(n) == 11:          # 237 + 8 chiffres -> 237 6 + 8 chiffres
        out.append("2376" + n[3:])
    elif n.startswith("2376") and len(n) == 12:
        out.append("237" + n[4:])
    if n.startswith("549") and len(n) == 13:
        out.append("54" + n[3:])
    elif n.startswith("54") and len(n) == 12 and not n.startswith("549"):
        out.append("549" + n[2:])
    if n.startswith("521") and len(n) == 13:
        out.append("52" + n[3:])
    elif n.startswith("52") and len(n) == 12 and not n.startswith("521"):
        out.append("521" + n[2:])
    return [x for x in out if x != n]


def _ts(m: dict) -> float | None:
    try:
        return float(m.get("timestamp"))
    except (TypeError, ValueError):
        return None


def _late_notice(m: dict) -> str | None:
    """Photo envoyée bien avant d'arriver (téléphone sans réseau, serveur coupé) : on le dit."""
    ts = _ts(m)
    if ts is None or m.get("type") not in ("image", "document"):
        return None
    delay = time.time() - ts
    if delay < LATE_AFTER_S:
        return None
    sent = time.strftime("%H:%M", time.localtime(ts))
    if delay < 3600:
        d = f"{int(delay // 60)} min"
    elif delay < 86400:
        d = f"{delay / 3600:.0f} h"
    else:
        d = f"{delay / 86400:.0f} jour(s)"
    return f"📶 Votre photo de {sent} vient d'arriver ({d} de retard, réseau coupé entre-temps). Je la lis maintenant ; sa date de capture reste {sent}."


def _outgoing_to(o: Outgoing) -> dict:
    d = {"text": o.text, "buttons": list(o.buttons), "image_mime": o.image_mime}
    if o.image:
        d["image_b64"] = base64.b64encode(o.image).decode()
    return d


def _outgoing_from(d: dict) -> Outgoing:
    img = base64.b64decode(d["image_b64"]) if d.get("image_b64") else None
    return Outgoing(d.get("text", ""), [tuple(b) for b in d.get("buttons", [])], image=img, image_mime=d.get("image_mime", "image/jpeg"))


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


def _short(title: str, n: int) -> str:
    t = re.sub(r"\s+", " ", title).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _describe(p: dict) -> str:
    if p["type"] == "text":
        return p["text"]["body"][:80]
    if p["type"] == "image":
        return "[image] " + (p["image"].get("caption") or "")[:60]
    i = p["interactive"]
    if i["type"] == "button":
        return i["body"]["text"][:60] + " " + str([b["reply"]["title"] for b in i["action"]["buttons"]])
    return i["body"]["text"][:60] + " [liste] " + str([r["title"] for s in i["action"]["sections"] for r in s["rows"]])


# ---------------------------------------------------------------------- compatibilité
_default: WhatsApp | None = None


def adapter(agent: Agent) -> WhatsApp:
    global _default
    if _default is None or _default.agent is not agent:
        _default = WhatsApp(agent)
    return _default


def verify(params: dict):
    """Ancienne interface (GET /webhook)."""
    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse
    ch = WhatsApp(agent=None).verify(params)   # type: ignore[arg-type]
    if ch is None:
        raise HTTPException(403)
    return PlainTextResponse(ch)


async def handle_webhook(agent: Agent, body: dict) -> dict:
    """Ancienne interface : traitement synchrone d'un webhook (tests, scripts)."""
    n = await adapter(agent).process(body)
    return {"status": "ok", "processed": n}
