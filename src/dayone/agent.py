"""
Agent conversationnel (indépendant du transport WhatsApp).

Entrée : un message entrant (texte / bouton / image) d'une sage-femme.
Sortie : une liste de messages sortants (texte + boutons + image éventuelle).

Il orchestre : qualité de la photo, capture chiffrée, file hors ligne,
traitement IA, présentation des données extraites, Confirmer / Corriger /
Reprendre la photo / Je ne sais pas, questions de suivi champ par champ,
saisie manuelle si l'IA est indisponible, sessions multipages, renumérisation,
liaison patiente, synchronisation.
"""
from __future__ import annotations

import base64
import datetime as dt
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import geometry as G
from . import lifecycle as L
from .extract import PageExtraction, extract_page
from .linking import find_candidates, merge_signals
from .normalize import normalize
from .pii import encode_jpeg, redact, sha256
from .reader.base import Reader
from .schema import PAGE_LABELS, PAGE_TYPES, field_def, groups_of, important_fields
from .store import Store

MAX_PROCESSING_ATTEMPTS = 3


@dataclass
class Incoming:
    sender: str
    text: str | None = None
    button_id: str | None = None
    image: bytes | None = None
    image_mime: str = "image/jpeg"
    filename: str | None = None      # nom du fichier photo (sert au lecteur simulé)
    sent_at: float | None = None     # heure d'envoi par la sage-femme (époque), donnée par WhatsApp


@dataclass
class Outgoing:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (id, libellé)
    image: bytes | None = None
    image_mime: str = "image/jpeg"

    def to_dict(self) -> dict:
        d = {"text": self.text, "buttons": [{"id": i, "title": t} for i, t in self.buttons]}
        if self.image:
            d["image"] = "data:%s;base64,%s" % (self.image_mime, base64.b64encode(self.image).decode())
        return d


def _iso(ts: float | None) -> str:
    import datetime as dt
    t = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else dt.datetime.now(dt.timezone.utc)
    return t.isoformat(timespec="seconds")


SECTION_LABELS = {
    "antecedents_familiaux": "Antécédents familiaux", "antecedents_femme": "Antécédents de la femme",
    "antecedents_obstetricaux": "Anomalies grossesses antérieures", "accouchements_anterieurs": "Accouchements antérieurs",
    "visites": "Visites",
}


def _fmt_value(v) -> str:
    return "vide" if v in (None, "") else str(v)


class Agent:
    def __init__(self, store: Store, reader: Reader, online: bool = True, crop_pad: int = 30,
                 simulate_network: bool = False):
        self.store = store
        self.reader = reader
        self.online = online
        self.crop_pad = crop_pad
        # True : le bouton « Simuler coupure » existe (simulateur web, démo).
        # False (production WhatsApp) : l'état réseau vient de la réalité (whatsapp.run_forever).
        self.simulate_network = simulate_network
        self.offline_since: float | None = None

    # ================================================================== utilitaires
    def _session(self, sender: str) -> dict:
        s = self.store.load_session(sender)
        s.setdefault("mode", "idle")
        return s

    def _save(self, sender: str, s: dict):
        self.store.save_session(sender, s)

    def set_online(self, online: bool) -> list[tuple[str, Outgoing]]:
        """Change l'état réseau (réel, mesuré par le transport, ou simulé) ; au retour, vide la file."""
        import time
        if online == self.online:
            return self.process_queue() if online else []
        self.online = online
        self.offline_since = None if online else time.time()
        if online:
            return self.process_queue()
        return []

    def network_line(self) -> str:
        import datetime as dt
        if self.online:
            return "🟢 en ligne"
        since = ""
        if self.offline_since:
            since = " depuis " + dt.datetime.fromtimestamp(self.offline_since).strftime("%H:%M")
        return f"🔴 coupé{since}" + (" (simulation)" if self.simulate_network else "")

    # ================================================================== point d'entrée
    def handle(self, m: Incoming) -> list[Outgoing]:
        s = self._session(m.sender)
        try:
            if m.image:
                out = self._on_image(m, s)
            else:
                out = self._on_text(m, s)
        finally:
            self._save(m.sender, s)
        return out

    # ================================================================== texte / boutons
    def _on_text(self, m: Incoming, s: dict) -> list[Outgoing]:
        bid = (m.button_id or "").strip()
        txt = (m.text or "").strip()
        low = txt.lower()
        if bid == "menu" or low in ("menu", "/start", "start", "bonjour", "salam", "salut", "hello", "aide", "help", "?"):
            s["mode"] = "idle"
            return [self._menu(m.sender)]
        if bid == "records" or low in ("dossiers", "mes dossiers", "liste"):
            return [self._records_list(m.sender)]
        if bid == "toggle_net" or low in ("connexion", "réseau", "reseau", "net"):
            n_wait = len([q for q in self.store.queue_items() if q["midwife_id"] == m.sender])
            if not self.simulate_network:
                # production : on ne bascule rien, on dit où on en est
                return [Outgoing(f"Réseau du serveur : {self.network_line()} · {n_wait} page(s) en attente de lecture.\n"
                                 "Si votre téléphone n'a pas de réseau, WhatsApp garde vos photos et les envoie dès qu'il revient : "
                                 "je les lis à leur arrivée, avec l'heure réelle de la prise de vue.")]
            self._save(m.sender, s)                  # la file peut modifier la session de cet expéditeur
            notes = self.set_online(not self.online)
            s.clear()
            s.update(self._session(m.sender))
            msgs = [Outgoing(("🟢 Réseau rétabli (fin de la simulation). Les pages en attente sont lues maintenant." if self.online
                              else "🔴 Coupure réseau simulée : les photos envoyées sont enregistrées chiffrées et mises en file, "
                                   "elles seront lues au retour du réseau. Appuyez sur *Rétablir réseau* pour terminer la simulation."),
                             [("toggle_net", self._net_button())])]
            msgs += [o for sender, o in notes if sender == m.sender]
            return msgs
        if bid == "manual" or low in ("saisie manuelle", "manuel"):
            return self._start_manual(m.sender, s)
        if bid == "send_anyway" and s.get("pending_image_id"):
            return self._accept_capture(m.sender, s, s.pop("pending_image_id"))
        if bid == "retake" or low in ("reprendre", "reprendre la photo"):
            s["mode"] = "awaiting_photo"
            rid = s.get("record_id")
            if rid and self.store.get(rid) and self.store.get(rid)["state"] == "A_REVISER":
                self.store.transition(rid, "CAPTURE", "reprise de la photo demandée")
            return [Outgoing("📷 D'accord, renvoyez une photo plus nette de la même page (bien à plat, sans ombre).")]

        mode = s.get("mode", "idle")
        if mode == "reviewing":
            return self._on_review_answer(m.sender, s, bid, txt)
        if mode == "correcting":
            return self._on_correction(m.sender, s, bid, txt)
        if mode == "confirming":
            return self._on_confirm(m.sender, s, bid, txt)
        if mode == "linking_code":
            return self._on_code(m.sender, s, bid, txt)
        if mode == "linking_choice":
            return self._on_link_choice(m.sender, s, bid, txt)
        if mode == "redigitize":
            return self._on_redigitize(m.sender, s, bid, txt)
        if mode == "next_page":
            return self._on_next_page(m.sender, s, bid, txt)
        if mode == "manual_type":
            return self._on_manual_type(m.sender, s, bid, txt)
        if mode == "manual_entry":
            return self._on_manual_value(m.sender, s, bid, txt)
        if mode in ("page_type_confirm", "page_type_pick"):
            return self._on_page_type_confirm(m.sender, s, bid, txt)
        return [Outgoing("Envoyez la photo d'une page du carnet pour commencer, ou tapez *menu*.",
                         [("records", "📋 Mes dossiers")] + ([("toggle_net", self._net_button())] if self.simulate_network else []))]

    def _net_button(self) -> str:
        """Libellé du bouton de simulation réseau (20 caractères au plus pour WhatsApp)."""
        return "🔌 Simuler coupure" if self.online else "🟢 Rétablir réseau"

    def _menu(self, sender: str) -> Outgoing:
        n_wait = len([q for q in self.store.queue_items() if q["midwife_id"] == sender])
        net = self.network_line()
        return Outgoing(
            "👋 Bonjour. Je transforme les pages du carnet de grossesse en dossier numérique.\n\n"
            "📷 Envoyez la *photo d'une page* (bien à plat, sans ombre). Je lis, je vous montre ce que j'ai compris, "
            "et je vous demande seulement ce dont je doute.\n\n"
            f"Réseau : {net} · {n_wait} page(s) en attente de traitement.\n"
            "Commandes : *dossiers*, *saisie manuelle*, *connexion*, *menu*."
            + (" Pour la démo, *connexion* simule une coupure réseau." if self.simulate_network else ""),
            [("records", "📋 Mes dossiers")] + ([("toggle_net", self._net_button())] if self.simulate_network else [])
            + [("manual", "✍️ Saisie manuelle")])

    def _records_list(self, sender: str) -> Outgoing:
        recs = self.store.list_records(sender)
        if not recs:
            return Outgoing("Aucun dossier pour l'instant.")
        lines = []
        for r in recs[-12:]:
            lines.append(f"• {PAGE_LABELS.get(r['page_type'], r['page_type'] or 'page ?')} : *{L.LABELS_FR[r['state']]}* ({r['created_at'][11:16]})")
        return Outgoing("📋 Vos dernières pages :\n" + "\n".join(lines))

    # ================================================================== image
    def _on_image(self, m: Incoming, s: dict) -> list[Outgoing]:
        arr = cv2.imdecode(np.frombuffer(m.image, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return [Outgoing("Je n'arrive pas à ouvrir cette image. Pouvez-vous la renvoyer ?")]
        sha = sha256(m.image)
        dup = self.store.find_by_sha(m.sender, sha)
        if dup and dup["state"] not in ("CAPTURE",):
            return [Outgoing(f"⚠️ Cette photo a déjà été envoyée (dossier *{L.LABELS_FR[dup['state']]}*). Je ne la traite pas deux fois.",
                             [("records", "📋 Mes dossiers")])]
        q = G.assess_quality(arr)
        # capture TOUJOURS enregistrée (chiffrée) : rien n'est perdu, même si on demande une reprise
        rid = self.store.create_record(m.sender, m.image, m.image_mime, sha,
                                       document_id=s.get("document_id") if s.get("mode") in ("awaiting_photo", "next_page_photo") else None,
                                       payload={"quality": {"ok": q.ok, "blur": q.blur, "brightness": q.brightness, "reasons": q.reasons},
                                                "source_name": m.filename or "",
                                                "captured_at": _iso(m.sent_at)})
        if not q.ok:
            s["pending_image_id"] = rid
            return [Outgoing("⚠️ Photo difficile à lire : " + ", ".join(q.reasons) + ".\nReprenez-la bien à plat, sans ombre, ou envoyez-la quand même.",
                             [("retake", "📷 Reprendre"), ("send_anyway", "Envoyer quand même")])]
        return self._accept_capture(m.sender, s, rid)

    def _accept_capture(self, sender: str, s: dict, rid: str) -> list[Outgoing]:
        rec = self.store.get(rid)
        s["record_id"] = rid
        s["document_id"] = rec["document_id"]
        s["mode"] = "processing"
        self.store.transition(rid, "EN_ATTENTE_IA", "capture acceptée, mise en file")
        self.store.enqueue(rid, "ai")
        if not self.online:
            return [Outgoing("📥 Page reçue et enregistrée chiffrée. État : *En attente de traitement IA* "
                             f"(réseau du serveur : {self.network_line()}).\n"
                             "Elle sera lue automatiquement dès que possible. Rien n'est perdu. "
                             "Vous pouvez envoyer les pages suivantes.", [("manual", "✍️ Saisir à la main maintenant")])]
        out = [Outgoing("🔎 Lecture de la page en cours…")]
        out += self._process_record(sender, s, rid)
        return out

    # ================================================================== file & traitement
    def process_queue(self) -> list[tuple[str, Outgoing]]:
        """À appeler quand le réseau revient (ou périodiquement). Retourne (destinataire, message)."""
        notes: list[tuple[str, Outgoing]] = []
        if not self.online:
            return notes
        for item in self.store.queue_items():
            rid, sender = item["record_id"], item["midwife_id"]
            s = self._session(sender)
            try:
                if item["kind"] == "ai":
                    if s.get("mode") in ("reviewing", "correcting", "confirming", "linking_code", "linking_choice") and s.get("record_id") != rid:
                        continue   # une révision est en cours sur une autre page : on attend
                    s["record_id"], s["document_id"] = rid, self.store.get(rid)["document_id"]
                    for o in self._process_record(sender, s, rid):
                        notes.append((sender, o))
                elif item["kind"] == "sync":
                    self._sync(rid)
                    notes.append((sender, Outgoing(f"☁️ Dossier synchronisé ({PAGE_LABELS.get(self.store.get(rid)['page_type'], 'page')}).")))
            finally:
                self._save(sender, s)
        return notes

    def _process_record(self, sender: str, s: dict, rid: str) -> list[Outgoing]:
        rec = self.store.get(rid)
        img_bytes, mime = self.store.get_image(rid)
        arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        try:
            ext = extract_page(arr, self.reader, source_name=rec["payload"].get("source_name", ""), skip_quality=True)
            if ext.error:
                raise RuntimeError(ext.error)
        except Exception as e:  # IA indisponible / erreur
            self.store.bump_attempt(rid)
            if rec["attempts"] + 1 >= MAX_PROCESSING_ATTEMPTS or "type de page" in str(e):
                self.store.transition(rid, "REVISION_MANUELLE_REQUISE", f"échec IA : {e}")
                self.store.dequeue(rid)
                s["mode"] = "manual_type"
                return [Outgoing(f"⚠️ Je n'ai pas pu lire cette page automatiquement ({e}). Passons en saisie manuelle.\n"
                                 "De quelle page s'agit-il ?", self._page_type_buttons())]
            if rec["state"] == "EN_ATTENTE_IA":
                self.store.transition(rid, "ECHEC_TRAITEMENT", str(e))
            self.store.transition(rid, "EN_ATTENTE_IA", "nouvelle tentative programmée")
            return [Outgoing(f"⚠️ Traitement IA indisponible pour le moment ({e}). Je réessaierai automatiquement ; vous pouvez aussi saisir à la main.",
                             [("manual", "✍️ Saisie manuelle")])]
        # image masquée remplace l'image brute (l'image brute n'a existé que chiffrée, le temps du traitement)
        masked = redact(arr, ext.pii_boxes)
        jpg = encode_jpeg(masked)
        self.store.replace_image(rid, jpg, "image/jpeg", sha256(jpg))
        payload = rec["payload"]
        payload.update(ext.to_dict())
        payload["processed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.store.update_payload(rid, payload, page_type=ext.page_type)
        self.store.transition(rid, "TRAITE_IA", f"{ext.reader}, {ext.timing_s}s")
        self.store.dequeue(rid)
        # type de page incertain (précoce / tardif) : on demande d'abord
        if ext.page_type_score < 0.6:
            s["mode"] = "page_type_confirm"
            s["record_id"] = rid
            return [Outgoing(f"J'ai lu cette page comme « {PAGE_LABELS[ext.page_type]} » mais je ne suis pas sûr. C'est bien ça ?",
                             [("pt_ok", "✅ Oui"), ("pt_other", "❌ Non, autre page")])]
        return self._present(sender, s, rid, ext.page_type, payload)

    # ================================================================== présentation & révision
    def _present(self, sender: str, s: dict, rid: str, page_type: str, payload: dict) -> list[Outgoing]:
        fields = payload["fields"]
        doubts = [k for k, f in fields.items() if f["status"] in ("A_REVISER", "ILLISIBLE")]
        doubts.sort(key=lambda k: (0 if "." not in k else 1, k))
        known = [k for k, f in fields.items() if f["status"] == "CONNU"]
        empty = [k for k, f in fields.items() if f["status"] == "NON_FOURNI"]
        na = [k for k, f in fields.items() if f["status"] == "NON_APPLICABLE"]
        summary = self._summary(page_type, fields, payload.get("choices", {}))
        head = (f"📄 *{PAGE_LABELS[page_type]}*\n{summary}\n\n"
                f"✅ {len(known)} champs lus · ⬜ {len(empty)} vides · ➖ {len(na)} non applicables · ❓ {len(doubts)} doute(s)")
        s["numbered"] = self._numbered(page_type, fields)
        reg = payload.get("registration") or {}
        if reg.get("template_fit") is False and not reg.get("template_free"):
            # vrai carnet ou autre version du formulaire : la lecture par cellules n'est pas fiable
            s["mode"], s["record_id"] = "confirming", rid
            self.store.transition(rid, "A_REVISER", "mise en page non reconnue")
            return [Outgoing("⚠️ Je ne reconnais pas la mise en page de cette page (format différent du modèle que je connais). "
                             "Ma lecture automatique n'est pas fiable ici : je ne garde rien sans vous.\n\n"
                             f"📄 Type de page : *{PAGE_LABELS[page_type]}*\n\n"
                             "Le plus sûr est la saisie à la main, guidée champ par champ.",
                             [("manual", "✍️ Saisir à la main"), ("retake", "📷 Reprendre la photo"), ("correct", "✏️ Voir ma lecture")])]
        if doubts:
            s["mode"], s["pending"], s["record_id"] = "reviewing", doubts, rid
            self.store.transition(rid, "A_REVISER", f"{len(doubts)} champ(s) à réviser")
            return [Outgoing(head + "\n\nJe vais vous demander les champs dont je doute, un par un.")] + self._ask_next(sender, s)
        s["mode"], s["record_id"] = "confirming", rid
        return [Outgoing(head + "\n\nTout est correct ?",
                         [("confirm", "✅ Confirmer"), ("correct", "✏️ Corriger un champ"), ("retake", "📷 Reprendre la photo")])]

    def _numbered(self, page_type: str, fields: dict) -> list[str]:
        keys = [k for k, f in fields.items() if f["status"] in ("CONNU", "A_REVISER", "ILLISIBLE", "NON_FOURNI") and not f.get("pii")]
        return keys

    def _summary(self, page_type: str, fields: dict, choices: dict) -> str:
        lines = []
        n = 0
        groups = groups_of(page_type)
        if page_type == "GROSSESSE_ACTUELLE":
            for k in ("ddr", "taille_cm", "dpa", "date_depassement_terme"):
                f = fields.get(k, {})
                if f.get("status") in ("CONNU", "A_REVISER"):
                    n += 1
                    lines.append(f"{n}. {field_def(page_type, k)['label']} : *{f['value']}*" + (" ❓" if f["status"] != "CONNU" else ""))
            for g in ("groupage", "rhesus"):
                if choices.get(g):
                    lines.append(f"☑ {groups[g]['label']} : {', '.join(choices[g])}")
            cols = ["t1_v1", "t1_v2", "t1_v3", "t2_v1", "t2_v2", "t2_v3", "t3_m7", "t3_m8", "t3_m9"]
            for c in cols:
                d = fields.get(f"visites.{c}.venue_le", {})
                if d.get("status") in ("CONNU", "A_REVISER"):
                    bits = [f"{d['value']}"]
                    for r, lab in (("age_gestationnel", ""), ("poids_kg", "kg"), ("ta", "TA"), ("hu_cm", "HU"), ("bcf", "BCF")):
                        f = fields.get(f"visites.{c}.{r}", {})
                        if f.get("status") in ("CONNU", "A_REVISER"):
                            bits.append((lab + " " if lab else "") + str(f["value"]) + ("❓" if f["status"] != "CONNU" else ""))
                    lines.append(f"• {c.replace('_', ' ').upper()} : " + ", ".join(bits))
            return "\n".join(lines)
        # autres pages : champs connus numérotés + cases cochées par groupe
        for k, f in fields.items():
            if f.get("pii") or f["status"] not in ("CONNU", "A_REVISER", "ILLISIBLE"):
                continue
            n += 1
            lab = field_def(page_type, k)["label"]
            mark = {"CONNU": "", "A_REVISER": " ❓", "ILLISIBLE": " ❓(illisible)"}[f["status"]]
            lines.append(f"{n}. {lab} : *{_fmt_value(f.get('value'))}*{mark}")
        for g, opts in choices.items():
            if opts and g in groups:
                lines.append(f"☑ {groups[g]['label']} : {', '.join(opts)}")
        if len(lines) > 40:
            lines = lines[:40] + [f"… (+{len(lines) - 40})"]
        return "\n".join(lines)

    def _crop(self, rid: str, bbox) -> bytes | None:
        if not bbox:
            return None
        img_bytes, _ = self.store.get_image(rid)
        arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        x0, y0, x1, y1 = bbox
        pad = self.crop_pad
        h, w = arr.shape[:2]
        z = arr[max(0, y0 - pad): min(h, y1 + pad), max(0, x0 - 3 * pad): min(w, x1 + 3 * pad)]
        if z.size == 0:
            return None
        if z.shape[1] < 600:
            f = 600 / z.shape[1]
            z = cv2.resize(z, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        return encode_jpeg(z, 80)

    def _ask_next(self, sender: str, s: dict) -> list[Outgoing]:
        rid = s["record_id"]
        rec = self.store.get(rid)
        fields = rec["payload"]["fields"]
        while s.get("pending"):
            k = s["pending"][0]
            f = fields.get(k)
            if not f:
                s["pending"].pop(0)
                continue
            s["current_key"] = k
            lab = field_def(rec["page_type"], k)["label"]
            why = f.get("notes") or ("écriture difficile" if f["status"] == "ILLISIBLE" else "lecture incertaine")
            if f["status"] == "ILLISIBLE" or f.get("value") in (None, ""):
                txt = f"❓ *{lab}* : je n'arrive pas à lire ({why}). Que lisez-vous sur le carnet ? Tapez la valeur, ou :"
                btns = [("empty", "⬜ C'est vide"), ("retake", "📷 Reprendre la photo"), ("unknown", "🤷 Je ne sais pas")]
            else:
                txt = f"❓ *{lab}* : j'ai lu *{f['value']}* (confiance {int((f.get('confidence') or 0) * 100)} %, {why}). C'est bien ça ?"
                btns = [("yes", f"✅ Oui, {str(f['value'])[:18]}"), ("edit", "✏️ Corriger"), ("unknown", "🤷 Je ne sais pas")]
            return [Outgoing(txt, btns, image=self._crop(rid, f.get("bbox_px")))]
        # plus de doutes
        s["mode"] = "confirming"
        return [Outgoing("Merci. Tout est correct maintenant ?",
                         [("confirm", "✅ Valider la page"), ("correct", "✏️ Corriger un champ"), ("retake", "📷 Reprendre la photo")])]

    def _set_field(self, rid: str, key: str, value: str | None, status: str, note: str = "réponse de la sage-femme") -> tuple[dict, str | None]:
        rec = self.store.get(rid)
        payload = rec["payload"]
        f = payload["fields"][key]
        warn = None
        if status == "CONNU":
            n = normalize(value, field_def(rec["page_type"], key))
            if n.status == "CONNU" and n.value is not None:
                if n.score < 0.6:
                    warn = f"⚠️ « {value} » ne ressemble pas au format attendu ({field_def(rec['page_type'], key).get('type')}) ; j'enregistre tel quel."
                f.update({"value": n.value, "raw": value, "status": "CONNU", "confidence": 1.0, "source": "midwife", "notes": note})
            else:
                f.update({"value": None, "raw": value, "status": n.status, "confidence": 1.0, "source": "midwife", "notes": n.note or note})
        else:
            f.update({"value": None, "status": status, "confidence": 1.0 if status != "INCONNU" else None, "source": "midwife", "notes": note})
        self.store.update_payload(rid, payload)
        return payload, warn

    def _on_review_answer(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid, k = s["record_id"], s.get("current_key")
        if not k:
            s["mode"] = "confirming"
            return self._ask_next(sender, s)
        rec = self.store.get(rid)
        cur = rec["payload"]["fields"][k]
        out = []
        if bid == "yes":
            self._set_field(rid, k, cur.get("value"), "CONNU", "confirmé par la sage-femme")
        elif bid == "empty":
            self._set_field(rid, k, None, "NON_FOURNI", "vide, confirmé par la sage-femme")
        elif bid == "unknown":
            self._set_field(rid, k, None, "INCONNU", "la sage-femme ne sait pas")
        elif bid == "edit":
            s["mode"] = "correcting"
            s["correcting_key"] = k
            return [Outgoing(f"✏️ Tapez la valeur de *{field_def(rec['page_type'], k)['label']}* telle qu'écrite sur le carnet (ou *vide*, *illisible*, *passer*).")]
        elif txt:
            if txt.lower() in ("vide", "rien", "aucun"):
                self._set_field(rid, k, None, "NON_FOURNI", "vide, confirmé par la sage-femme")
            elif txt.lower() in ("illisible",):
                self._set_field(rid, k, None, "ILLISIBLE", "illisible, confirmé par la sage-femme")
            elif txt.lower() in ("passer", "skip", "je ne sais pas"):
                self._set_field(rid, k, None, "INCONNU", "la sage-femme ne sait pas")
            else:
                _, warn = self._set_field(rid, k, txt, "CONNU")
                if warn:
                    out.append(Outgoing(warn))
        else:
            return [Outgoing("Répondez avec un bouton ou tapez la valeur.")]
        s["pending"].pop(0)
        s["current_key"] = None
        return out + self._ask_next(sender, s)

    def _on_correction(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid = s["record_id"]
        rec = self.store.get(rid)
        k = s.get("correcting_key")
        if not k:
            # « numéro = valeur »
            m = re.match(r"^\s*(\d+)\s*[=:]\s*(.+)$", txt)
            if not m:
                return [Outgoing("Format : *numéro = valeur* (ex. 3 = 12/05/2025). Tapez *menu* pour annuler.")]
            idx = int(m.group(1)) - 1
            keys = s.get("numbered", [])
            if not 0 <= idx < len(keys):
                return [Outgoing(f"Numéro inconnu (1 à {len(keys)}).")]
            k, txt = keys[idx], m.group(2).strip()
        out = []
        if txt.lower() in ("vide", "rien"):
            self._set_field(rid, k, None, "NON_FOURNI", "vide, corrigé par la sage-femme")
        elif txt.lower() == "illisible":
            self._set_field(rid, k, None, "ILLISIBLE", "illisible, confirmé par la sage-femme")
        elif txt.lower() in ("passer", "skip"):
            self._set_field(rid, k, None, "INCONNU", "la sage-femme ne sait pas")
        else:
            _, warn = self._set_field(rid, k, txt, "CONNU", "corrigé par la sage-femme")
            if warn:
                out.append(Outgoing(warn))
        s["correcting_key"] = None
        lab = field_def(rec["page_type"], k)["label"]
        out.append(Outgoing(f"✔️ {lab} → *{self.store.get(rid)['payload']['fields'][k].get('value') or 'vide'}*"))
        if s.get("pending") and k in s["pending"]:
            s["pending"].remove(k)
        if s.get("pending"):
            s["mode"] = "reviewing"
            return out + self._ask_next(sender, s)
        s["mode"] = "confirming"
        return out + [Outgoing("Autre correction ? Sinon validez.",
                               [("confirm", "✅ Valider la page"), ("correct", "✏️ Corriger un autre champ")])]

    def _on_confirm(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid = s["record_id"]
        if bid == "correct" or txt.lower().startswith("corr"):
            s["mode"] = "correcting"
            s["correcting_key"] = None
            keys = s.get("numbered", [])
            rec = self.store.get(rid)
            listing = "\n".join(f"{i + 1}. {field_def(rec['page_type'], k)['label']} = {_fmt_value(rec['payload']['fields'][k].get('value'))}"
                                for i, k in enumerate(keys[:40]))
            return [Outgoing("Quel champ ? Répondez *numéro = valeur* (ex. 3 = 12/05/2025).\n" + listing)]
        if bid == "confirm" or txt.lower() in ("ok", "oui", "valider", "confirmer"):
            rec = self.store.get(rid)
            if rec["state"] in ("TRAITE_IA", "A_REVISER", "REVISION_MANUELLE_REQUISE"):
                self.store.transition(rid, "VALIDE", {"TRAITE_IA": "confirmé par la sage-femme", "A_REVISER": "révision terminée",
                                                      "REVISION_MANUELLE_REQUISE": "saisie manuelle validée"}[rec["state"]])
            return self._start_linking(sender, s, rid)
        return [Outgoing("Validez, corrigez ou reprenez la photo.",
                         [("confirm", "✅ Valider la page"), ("correct", "✏️ Corriger un champ"), ("retake", "📷 Reprendre la photo")])]

    # ================================================================== type de page incertain
    def _page_type_buttons(self):
        return [(f"pt:{pt}", PAGE_LABELS[pt][:20]) for pt in PAGE_TYPES]

    def _on_page_type_confirm(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid = s["record_id"]
        rec = self.store.get(rid)
        if bid == "pt_ok":
            return self._present(sender, s, rid, rec["page_type"], rec["payload"])
        if bid == "pt_other":
            s["mode"] = "page_type_pick"
            return [Outgoing("Quelle page est-ce ?", self._page_type_buttons())]
        if bid.startswith("pt:"):
            pt = bid[3:]
            # relire avec le bon gabarit
            img_bytes, _ = self.store.get_image(rid)
            arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
            ext = extract_page(arr, self.reader, page_type=pt, source_name=rec["payload"].get("source_name", ""), skip_quality=True)
            payload = rec["payload"]
            payload.update(ext.to_dict())
            self.store.update_payload(rid, payload, page_type=pt)
            return self._present(sender, s, rid, pt, payload)
        return [Outgoing("Choisissez avec un bouton.", [("pt_ok", "✅ Oui"), ("pt_other", "❌ Autre page")])]

    # ================================================================== liaison patiente
    def _start_linking(self, sender: str, s: dict, rid: str) -> list[Outgoing]:
        rec = self.store.get(rid)
        # page suivante d'un même carnet déjà lié : on rattache directement
        siblings = [r for r in self.store.list_records(sender, document_id=rec["document_id"]) if r["id"] != rid and r.get("patient_id")]
        if siblings:
            return self._link_to(sender, s, rid, siblings[0]["patient_id"], "même carnet (session multipage)")
        s["mode"] = "linking_code"
        return [Outgoing("🔗 Quel *code patiente* avez-vous écrit sur ce carnet ? (ex. AB12). Tapez *aucun* s'il n'y en a pas encore.")]

    def _on_code(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid = s["record_id"]
        rec = self.store.get(rid)
        code = None if txt.lower() in ("aucun", "non", "pas de code", "") else txt.strip().upper()
        s["code"] = code
        cands = find_candidates(self.store.list_patients(sender), code, rec["page_type"], rec["payload"]["fields"])
        s["candidates"] = [{"patient_id": c.patient_id, "score": c.score, "label": c.label, "reasons": c.reasons} for c in cands]
        if not cands:
            if code is None:
                code = self._new_code(sender)
                s["code"] = code
                note = f"Aucune patiente connue ne correspond. Je crée un nouveau profil avec le code *{code}* : écrivez-le sur le carnet."
            else:
                note = f"Aucune patiente connue avec le code *{code}* ni de visite ressemblante. Je crée un nouveau profil."
            return self._link_to(sender, s, rid, None, note)
        s["mode"] = "linking_choice"
        lines = [f"J'ai trouvé {len(cands)} correspondance(s) possible(s) :"]
        btns = []
        for i, c in enumerate(cands, 1):
            lines.append(f"*Patiente {i}* ({int(c.score * 100)} %) : {c.label}\n   ↳ {', '.join(c.reasons[:3])}")
            btns.append((f"link:{i}", f"Patiente {i}"))
        btns += [("link:new", "Aucune, créer"), ("link:unsure", "Je ne sais pas")]
        return [Outgoing("\n".join(lines) + "\n\nÀ quelle patiente rattacher cette page ?", btns)]

    def _on_link_choice(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid = s["record_id"]
        if bid == "link:new":
            return self._link_to(sender, s, rid, None, "nouveau profil créé à la demande de la sage-femme")
        if bid == "link:unsure" or txt.lower().startswith("je ne sais"):
            self.store.transition(rid, "DOUBLON_SUSPECTE", "correspondance non tranchée")
            s["mode"] = "idle"
            return [Outgoing("D'accord. La page reste *en attente de décision* (doublon suspecté) : vous pourrez trancher plus tard depuis « Mes dossiers ». Rien n'est perdu.",
                             [("records", "📋 Mes dossiers")])]
        m = re.match(r"link:(\d+)", bid) or re.match(r"^\s*(\d+)\s*$", txt)
        if m:
            i = int(m.group(1)) - 1
            cands = s.get("candidates", [])
            if 0 <= i < len(cands):
                return self._link_to(sender, s, rid, cands[i]["patient_id"], f"choix de la sage-femme : patiente {i + 1}")
        return [Outgoing("Choisissez avec un bouton.")]

    def _new_code(self, sender: str) -> str:
        import random
        import string
        existing = {p["code"] for p in self.store.list_patients(sender)}
        while True:
            c = "".join(random.choices(string.ascii_uppercase.replace("O", "").replace("I", ""), k=2)) + "".join(random.choices(string.digits, k=2))
            if c not in existing:
                return c

    def _link_to(self, sender: str, s: dict, rid: str, patient_id: str | None, note: str) -> list[Outgoing]:
        rec = self.store.get(rid)
        pt, fields = rec["page_type"], rec["payload"]["fields"]
        out = []
        if patient_id is None:
            code = s.get("code") or self._new_code(sender)
            profile = {"signals": merge_signals({}, pt, fields), "pages": {}, "n_pages": 0, "code": code}
            patient_id = self.store.create_patient(sender, code, profile)
            out.append(Outgoing(f"🆕 {note}"))
        pat = self.store.get_patient(patient_id)
        profile = pat["profile"]
        # renumérisation : une page du même type existe déjà pour cette patiente
        existing = profile.get("pages", {}).get(pt)
        if existing and existing != rid and self.store.get(existing):
            s["mode"] = "redigitize"
            s["link_patient_id"] = patient_id
            old = self.store.get(existing)
            n_old = sum(1 for f in old["payload"]["fields"].values() if f["status"] == "CONNU")
            n_new = sum(1 for f in fields.values() if f["status"] == "CONNU")
            return out + [Outgoing(f"♻️ Cette patiente a déjà une page « {PAGE_LABELS[pt]} » numérisée le {old['created_at'][:10]} "
                                   f"({n_old} champs). La nouvelle photo en contient {n_new}. Que faire ?",
                                   [("redig:replace", "Remplacer"), ("redig:fill", "Compléter les vides"), ("redig:keep", "Garder l'ancienne")])]
        return out + self._finish_link(sender, s, rid, patient_id, note)

    def _on_redigitize(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid, pid = s["record_id"], s["link_patient_id"]
        pat = self.store.get_patient(pid)
        rec = self.store.get(rid)
        pt = rec["page_type"]
        old_id = pat["profile"]["pages"].get(pt)
        old = self.store.get(old_id)
        if bid == "redig:keep":
            self.store.set_patient(rid, pid)
            self.store.transition(rid, "DOUBLON_SUSPECTE", "renumérisation : ancienne page conservée")
            s["mode"] = "next_page"
            return [Outgoing("Ancienne page conservée ; la nouvelle photo est gardée comme doublon (non fusionnée).",
                             [("next:yes", "➕ Page suivante"), ("next:no", "✅ Terminer")])]
        if bid == "redig:fill":
            payload = rec["payload"]
            merged = old["payload"]["fields"]
            n = 0
            for k, f in payload["fields"].items():
                if merged.get(k, {}).get("status") in ("NON_FOURNI", "INCONNU", "ILLISIBLE") and f["status"] == "CONNU":
                    merged[k] = f
                    n += 1
            old["payload"]["fields"] = merged
            self.store.update_payload(old_id, old["payload"])
            self.store.set_patient(rid, pid)
            self.store.transition(rid, "DOUBLON_SUSPECTE", f"renumérisation : {n} champs fusionnés dans {old_id}")
            s["mode"] = "next_page"
            return [Outgoing(f"✔️ {n} champ(s) vide(s) complété(s) dans la page existante.",
                             [("next:yes", "➕ Page suivante"), ("next:no", "✅ Terminer")])]
        if bid == "redig:replace":
            return self._finish_link(sender, s, rid, pid, "renumérisation : remplace l'ancienne page")
        return [Outgoing("Choisissez avec un bouton.", [("redig:replace", "Remplacer"), ("redig:fill", "Compléter les vides"), ("redig:keep", "Garder l'ancienne")])]

    def _finish_link(self, sender: str, s: dict, rid: str, patient_id: str, note: str) -> list[Outgoing]:
        rec = self.store.get(rid)
        pt, fields = rec["page_type"], rec["payload"]["fields"]
        pat = self.store.get_patient(patient_id)
        profile = pat["profile"]
        profile["signals"] = merge_signals(profile, pt, fields)
        profile.setdefault("pages", {})[pt] = rid
        profile["n_pages"] = len(profile["pages"])
        self.store.update_patient(patient_id, profile)
        self.store.set_patient(rid, patient_id)
        if rec["state"] == "DOUBLON_SUSPECTE":
            self.store.transition(rid, "PATIENTE_LIEE", note)
        else:
            self.store.transition(rid, "PATIENTE_LIEE", note)
        self.store.transition(rid, "ENREGISTRE", "fusionné dans le profil")
        out = [Outgoing(f"🔗 Page rattachée à la patiente *{profile.get('code') or pat.get('code') or '?'}* ({profile['n_pages']} page(s) dans son dossier).")]
        if self.online:
            self._sync(rid)
            out.append(Outgoing("☁️ Synchronisé avec le serveur."))
        else:
            self.store.enqueue(rid, "sync")
            out.append(Outgoing("📤 Enregistré sur le téléphone ; synchronisation au retour du réseau."))
        s["mode"] = "next_page"
        out.append(Outgoing("Une autre page du *même carnet* ?", [("next:yes", "➕ Oui, page suivante"), ("next:no", "✅ Terminer ce carnet")]))
        return out

    def _sync(self, rid: str):
        rec = self.store.get(rid)
        if rec["state"] in ("ENREGISTRE", "ECHEC_SYNCHRO"):
            self.store.transition(rid, "SYNCHRONISE", "envoyé au serveur (simulé)")
        self.store.dequeue(rid)

    def _on_next_page(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        if bid == "next:yes" or txt.lower() in ("oui", "suivante"):
            s["mode"] = "awaiting_photo"
            return [Outgoing("📷 Envoyez la photo de la page suivante (elle sera rattachée au même carnet et à la même patiente).")]
        s["mode"] = "idle"
        s["document_id"] = None
        return [Outgoing("✅ Carnet terminé. Envoyez une photo pour un autre carnet, ou tapez *menu*.")]

    # ================================================================== saisie manuelle
    def _start_manual(self, sender: str, s: dict) -> list[Outgoing]:
        rid = s.get("record_id")
        rec = self.store.get(rid) if rid else None
        if not rec or rec["state"] in ("SYNCHRONISE", "ENREGISTRE", "PATIENTE_LIEE"):
            # saisie sans photo : on crée un enregistrement « manuel »
            blank = np.full((40, 40, 3), 255, np.uint8)
            jpg = encode_jpeg(blank)
            rid = self.store.create_record(sender, jpg, "image/jpeg", sha256(jpg + str(dt.datetime.now()).encode()),
                                           document_id=s.get("document_id"), payload={"manual": True})
            self.store.transition(rid, "REVISION_MANUELLE_REQUISE", "saisie manuelle sans IA")
            s["record_id"], s["document_id"] = rid, self.store.get(rid)["document_id"]
        elif rec["state"] in ("EN_ATTENTE_IA", "ECHEC_TRAITEMENT", "CAPTURE"):
            if rec["state"] == "CAPTURE":
                self.store.transition(rid, "REVISION_MANUELLE_REQUISE", "saisie manuelle")
            else:
                self.store.transition(rid, "REVISION_MANUELLE_REQUISE", "saisie manuelle demandée (IA indisponible)")
            self.store.dequeue(rid)
        s["mode"] = "manual_type"
        return [Outgoing("✍️ Saisie manuelle. De quelle page s'agit-il ?", self._page_type_buttons())]

    def _on_manual_type(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        pt = bid[3:] if bid.startswith("pt:") else None
        if pt is None:
            for k, v in PAGE_LABELS.items():
                if txt.lower() and txt.lower() in v.lower():
                    pt = k
        if pt is None:
            return [Outgoing("Choisissez la page avec un bouton.", self._page_type_buttons())]
        rid = s["record_id"]
        rec = self.store.get(rid)
        from .schema import fields_of
        payload = rec["payload"]
        payload["page_type"] = pt
        payload["fields"] = {k: {"value": None, "raw": None, "status": "INCONNU", "confidence": None, "source": "midwife",
                                 "bbox_px": None, "notes": None, "pii": bool(fd.get("pii"))} for k, fd in fields_of(pt).items()}
        payload["choices"] = {}
        self.store.update_payload(rid, payload, page_type=pt)
        s["manual_keys"] = list(important_fields(pt))
        s["mode"] = "manual_entry"
        return self._ask_manual(sender, s)

    def _ask_manual(self, sender: str, s: dict) -> list[Outgoing]:
        rid = s["record_id"]
        rec = self.store.get(rid)
        if not s.get("manual_keys"):
            s["numbered"] = self._numbered(rec["page_type"], rec["payload"]["fields"])
            s["mode"] = "confirming"
            return [Outgoing("Saisie terminée.\n" + self._summary(rec["page_type"], rec["payload"]["fields"], {}) + "\n\nValider ?",
                             [("confirm", "✅ Valider"), ("correct", "✏️ Corriger un champ")])]
        k = s["manual_keys"][0]
        s["current_key"] = k
        fd = field_def(rec["page_type"], k)
        hint = f" ({' / '.join(fd['values'])})" if fd.get("values") else (f" (format {fd['type']})" if fd.get("type") not in ("text",) else "")
        return [Outgoing(f"✍️ *{fd['label']}*{hint} ? Tapez la valeur, *vide* ou *passer*.")]

    def _on_manual_value(self, sender: str, s: dict, bid: str, txt: str) -> list[Outgoing]:
        rid, k = s["record_id"], s.get("current_key")
        out = []
        if txt.lower() in ("vide", "rien"):
            self._set_field(rid, k, None, "NON_FOURNI", "vide (saisie manuelle)")
        elif txt.lower() in ("passer", "skip", "je ne sais pas"):
            self._set_field(rid, k, None, "INCONNU", "non saisi")
        elif txt:
            _, warn = self._set_field(rid, k, txt, "CONNU", "saisie manuelle")
            if warn:
                out.append(Outgoing(warn))
        else:
            return [Outgoing("Tapez la valeur, *vide* ou *passer*.")]
        s["manual_keys"].pop(0)
        return out + self._ask_manual(sender, s)
