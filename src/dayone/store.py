"""
Stockage local chiffré + file hors ligne.

- SQLite sur l'appareil ; les charges utiles (JSON du dossier, image) sont
  chiffrées avec Fernet (AES-128-CBC + HMAC) avant écriture. La clé vient de
  DAYONE_STORE_KEY ou d'un fichier de clé à côté de la base (simule le
  keystore du téléphone). Les métadonnées en clair sont minimales : identifiants
  internes (UUID), états, horodatages, identifiant de la sage-femme.
- Aucun identifiant direct de patiente n'est écrit : les images sont masquées
  (pii.py) AVANT d'entrer dans le store.
- La file (`queue`) contient les enregistrements qui attendent le réseau ;
  rien n'est jamais supprimé par le système.
- Chaque transition d'état est journalisée (`events`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from . import lifecycle


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = "data/local/dayone.db", key: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = key or os.environ.get("DAYONE_STORE_KEY")
        if not key:
            kf = self.path.with_suffix(".key")
            if kf.exists():
                key = kf.read_text().strip()
            else:
                key = Fernet.generate_key().decode()
                kf.write_text(key)
                try:
                    os.chmod(kf, 0o600)
                except OSError:
                    pass
        self.fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        c = self.db
        c.executescript("""
        CREATE TABLE IF NOT EXISTS records(
            id TEXT PRIMARY KEY, midwife_id TEXT NOT NULL, state TEXT NOT NULL,
            page_type TEXT, document_id TEXT, patient_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            image_sha256 TEXT, capture_sha256 TEXT, payload BLOB, image BLOB, image_mime TEXT, attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT, from_state TEXT, to_state TEXT, at TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS patients(
            id TEXT PRIMARY KEY, code TEXT, midwife_id TEXT, created_at TEXT, profile BLOB);
        CREATE TABLE IF NOT EXISTS queue(
            record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, enqueued_at TEXT, next_try TEXT, attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(
            midwife_id TEXT PRIMARY KEY, data BLOB, updated_at TEXT);
        -- messages WhatsApp reçus mais pas encore traités (média à télécharger, IA en panne, réseau coupé)
        CREATE TABLE IF NOT EXISTS inbox(
            message_id TEXT PRIMARY KEY, sender TEXT, data BLOB, received_at TEXT, attempts INTEGER DEFAULT 0, next_try TEXT);
        -- réponses qui n'ont pas pu partir (réseau coupé, Meta indisponible)
        CREATE TABLE IF NOT EXISTS outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT, data BLOB, created_at TEXT, attempts INTEGER DEFAULT 0, next_try TEXT);
        -- identifiants des messages déjà traités (Meta renvoie un webhook tant qu'il n'a pas de 200)
        CREATE TABLE IF NOT EXISTS processed(
            message_id TEXT PRIMARY KEY, at TEXT);
        """)
        c.commit()

    # ------------------------------------------------------------------ chiffrement
    def enc(self, obj) -> bytes:
        data = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, ensure_ascii=False).encode()
        return self.fernet.encrypt(bytes(data))

    def dec_json(self, blob):
        return json.loads(self.fernet.decrypt(blob).decode()) if blob else None

    def dec_bytes(self, blob) -> bytes | None:
        return self.fernet.decrypt(blob) if blob else None

    # ------------------------------------------------------------------ enregistrements
    def create_record(self, midwife_id: str, image_bytes: bytes, image_mime: str, image_sha256: str,
                      document_id: str | None = None, payload: dict | None = None) -> str:
        rid = str(uuid.uuid4())
        now = _now()
        self.db.execute(
            "INSERT INTO records(id, midwife_id, state, document_id, created_at, updated_at, image_sha256, capture_sha256, payload, image, image_mime) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (rid, midwife_id, "CAPTURE", document_id or str(uuid.uuid4()), now, now, image_sha256, image_sha256,
             self.enc(payload or {}), self.enc(image_bytes), image_mime))
        self.db.execute("INSERT INTO events(record_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
                        (rid, None, "CAPTURE", now, "capture chiffrée"))
        self.db.commit()
        return rid

    def get(self, rid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = self.dec_json(r["payload"]) or {}
        d.pop("image", None)
        return d

    def get_image(self, rid: str) -> tuple[bytes | None, str | None]:
        r = self.db.execute("SELECT image, image_mime FROM records WHERE id=?", (rid,)).fetchone()
        return (self.dec_bytes(r["image"]), r["image_mime"]) if r else (None, None)

    def replace_image(self, rid: str, image_bytes: bytes, image_mime: str, image_sha256: str):
        self.db.execute("UPDATE records SET image=?, image_mime=?, image_sha256=?, updated_at=? WHERE id=?",
                        (self.enc(image_bytes), image_mime, image_sha256, _now(), rid))
        self.db.commit()

    def update_payload(self, rid: str, payload: dict, page_type: str | None = None):
        self.db.execute("UPDATE records SET payload=?, page_type=COALESCE(?, page_type), updated_at=? WHERE id=?",
                        (self.enc(payload), page_type, _now(), rid))
        self.db.commit()

    def set_patient(self, rid: str, patient_id: str | None):
        self.db.execute("UPDATE records SET patient_id=?, updated_at=? WHERE id=?", (patient_id, _now(), rid))
        self.db.commit()

    def transition(self, rid: str, to: str, note: str = "") -> str:
        r = self.db.execute("SELECT state FROM records WHERE id=?", (rid,)).fetchone()
        if not r:
            raise KeyError(rid)
        frm = r["state"]
        lifecycle.check(frm, to)
        now = _now()
        self.db.execute("UPDATE records SET state=?, updated_at=? WHERE id=?", (to, now, rid))
        self.db.execute("INSERT INTO events(record_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)", (rid, frm, to, now, note))
        self.db.commit()
        return to

    def history(self, rid: str) -> list[dict]:
        return [dict(x) for x in self.db.execute("SELECT * FROM events WHERE record_id=? ORDER BY id", (rid,))]

    def list_records(self, midwife_id: str | None = None, document_id: str | None = None) -> list[dict]:
        q, args = "SELECT id, midwife_id, state, page_type, document_id, patient_id, created_at, updated_at, image_sha256, attempts FROM records", []
        conds = []
        if midwife_id:
            conds.append("midwife_id=?")
            args.append(midwife_id)
        if document_id:
            conds.append("document_id=?")
            args.append(document_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at"
        return [dict(x) for x in self.db.execute(q, args)]

    def find_by_sha(self, midwife_id: str, sha: str) -> dict | None:
        r = self.db.execute("SELECT id FROM records WHERE midwife_id=? AND capture_sha256=? ORDER BY created_at DESC", (midwife_id, sha)).fetchone()
        return self.get(r["id"]) if r else None

    # ------------------------------------------------------------------ file hors ligne
    def enqueue(self, rid: str, kind: str):
        self.db.execute("INSERT OR REPLACE INTO queue(record_id, kind, enqueued_at, next_try, attempts) VALUES(?,?,?,?,0)",
                        (rid, kind, _now(), _now()))
        self.db.commit()

    def dequeue(self, rid: str):
        self.db.execute("DELETE FROM queue WHERE record_id=?", (rid,))
        self.db.commit()

    def queue_items(self, kind: str | None = None) -> list[dict]:
        q = "SELECT q.*, r.midwife_id, r.state FROM queue q JOIN records r ON r.id=q.record_id"
        if kind:
            return [dict(x) for x in self.db.execute(q + " WHERE kind=? ORDER BY enqueued_at", (kind,))]
        return [dict(x) for x in self.db.execute(q + " ORDER BY enqueued_at")]

    def bump_attempt(self, rid: str, delay_s: int = 30):
        nxt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay_s)).isoformat(timespec="seconds")
        self.db.execute("UPDATE queue SET attempts=attempts+1, next_try=? WHERE record_id=?", (nxt, rid))
        self.db.execute("UPDATE records SET attempts=attempts+1 WHERE id=?", (rid,))
        self.db.commit()

    # ------------------------------------------------------------------ boîtes d'entrée / de sortie (transport)
    def _due(self, delay_s: int) -> str:
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay_s)).isoformat(timespec="seconds")

    def is_processed(self, message_id: str) -> bool:
        return bool(message_id) and self.db.execute("SELECT 1 FROM processed WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_processed(self, message_id: str):
        if message_id:
            self.db.execute("INSERT OR IGNORE INTO processed(message_id, at) VALUES(?,?)", (message_id, _now()))
            self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
            self.db.commit()

    def inbox_put(self, message_id: str, sender: str, data: dict):
        self.db.execute("INSERT OR IGNORE INTO inbox(message_id, sender, data, received_at, attempts, next_try) VALUES(?,?,?,?,0,?)",
                        (message_id, sender, self.enc(data), _now(), _now()))
        self.db.commit()

    def inbox_items(self, due_only: bool = True) -> list[dict]:
        rows = self.db.execute("SELECT * FROM inbox ORDER BY received_at").fetchall()
        now = _now()
        out = []
        for r in rows:
            if due_only and r["next_try"] and r["next_try"] > now:
                continue
            d = dict(r)
            d["data"] = self.dec_json(r["data"])
            out.append(d)
        return out

    def inbox_bump(self, message_id: str, delay_s: int = 30):
        self.db.execute("UPDATE inbox SET attempts=attempts+1, next_try=? WHERE message_id=?", (self._due(delay_s), message_id))
        self.db.commit()

    def inbox_drop(self, message_id: str):
        self.db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
        self.db.commit()

    def outbox_put(self, recipient: str, data: dict) -> int:
        cur = self.db.execute("INSERT INTO outbox(recipient, data, created_at, attempts, next_try) VALUES(?,?,?,0,?)",
                              (recipient, self.enc(data), _now(), _now()))
        self.db.commit()
        return int(cur.lastrowid)

    def outbox_items(self, due_only: bool = True) -> list[dict]:
        rows = self.db.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        now = _now()
        out = []
        for r in rows:
            if due_only and r["next_try"] and r["next_try"] > now:
                continue
            d = dict(r)
            d["data"] = self.dec_json(r["data"])
            out.append(d)
        return out

    def outbox_bump(self, oid: int, delay_s: int = 30):
        self.db.execute("UPDATE outbox SET attempts=attempts+1, next_try=? WHERE id=?", (self._due(delay_s), oid))
        self.db.commit()

    def outbox_done(self, oid: int):
        self.db.execute("DELETE FROM outbox WHERE id=?", (oid,))
        self.db.commit()

    def transport_counts(self) -> dict:
        n_in = self.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        n_out = self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        return {"inbox": int(n_in), "outbox": int(n_out)}

    # ------------------------------------------------------------------ patientes
    def create_patient(self, midwife_id: str, code: str | None, profile: dict) -> str:
        pid = str(uuid.uuid4())
        self.db.execute("INSERT INTO patients(id, code, midwife_id, created_at, profile) VALUES(?,?,?,?,?)",
                        (pid, (code or "").strip().upper() or None, midwife_id, _now(), self.enc(profile)))
        self.db.commit()
        return pid

    def get_patient(self, pid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["profile"] = self.dec_json(r["profile"]) or {}
        return d

    def update_patient(self, pid: str, profile: dict, code: str | None = None):
        if code:
            self.db.execute("UPDATE patients SET profile=?, code=? WHERE id=?", (self.enc(profile), code.strip().upper(), pid))
        else:
            self.db.execute("UPDATE patients SET profile=? WHERE id=?", (self.enc(profile), pid))
        self.db.commit()

    def list_patients(self, midwife_id: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT * FROM patients" + (" WHERE midwife_id=?" if midwife_id else ""),
                               (midwife_id,) if midwife_id else ()).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["profile"] = self.dec_json(r["profile"]) or {}
            out.append(d)
        return out

    # ------------------------------------------------------------------ sessions de conversation
    def load_session(self, midwife_id: str) -> dict:
        r = self.db.execute("SELECT data FROM sessions WHERE midwife_id=?", (midwife_id,)).fetchone()
        return self.dec_json(r["data"]) if r else {}

    def save_session(self, midwife_id: str, data: dict):
        self.db.execute("INSERT OR REPLACE INTO sessions(midwife_id, data, updated_at) VALUES(?,?,?)", (midwife_id, self.enc(data), _now()))
        self.db.commit()
