#!/usr/bin/env python3
"""
demo.py : scénario de démonstration scripté (sans réseau, lecteur simulé).

    1. capture HORS LIGNE de la page « identification » → file d'attente
    2. retour de la connexion → traitement automatique
    3. révision d'un champ incertain (confirmation + correction)
    4. validation → liaison patiente (nouveau profil, code attribué)
    5. page suivante du même carnet (grossesse actuelle) → rattachée sans question
    6. deuxième carnet avec un code identique → correspondance proposée → décision
    7. renumérisation d'une page déjà présente → choix « compléter les vides »
    8. file de synchro vidée au retour du réseau

Le transcript est imprimé et écrit dans out/demo_transcript.md.

    python scripts/demo.py [--reader mock|tesseract|anthropic] [--images "../data/Paper Registry"]
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dayone.agent import Agent, Incoming  # noqa: E402
from dayone.reader.base import get_reader  # noqa: E402
from dayone.store import Store  # noqa: E402

LOG = []


def say(who, text):
    LOG.append((who, text))
    prefix = "👩‍⚕️ Sage-femme" if who == "user" else ("🤖 Agent" if who == "agent" else "⚙️  Système")
    print(f"\n{prefix}: {text}")


def send(agent, inc, label=None):
    say("user", label or inc.text or f"[bouton {inc.button_id}]" if not inc.image else f"[photo {inc.filename}]")
    outs = agent.handle(inc)
    for o in outs:
        extra = ("  " + "  ".join(f"[{t}]" for _, t in o.buttons)) if o.buttons else ""
        say("agent", o.text + ("\n   🖼️ (recadrage joint)" if o.image else "") + extra)
    return outs


def answer_doubts(agent, sender, keep=0):
    """Répond « oui » aux doutes restants (sauf les `keep` premiers, gardés pour le scénario)."""
    n = 0
    while agent.store.load_session(sender).get("mode") == "reviewing":
        pending = agent.store.load_session(sender).get("pending", [])
        if n < keep or not pending:
            break
        rec = agent.store.get(agent.store.load_session(sender)["record_id"])
        f = rec["payload"]["fields"].get(pending[0], {})
        if f.get("value") in (None, ""):
            send(agent, Incoming(sender, button_id="unknown"), label="[bouton 🤷 Je ne sais pas]")
        else:
            send(agent, Incoming(sender, button_id="yes"), label="[bouton ✅ Oui]")
        n += 1


def photo(agent, sender, path):
    data = Path(path).read_bytes()
    return send(agent, Incoming(sender, image=data, image_mime="image/png" if path.endswith(".png") else "image/jpeg", filename=Path(path).name))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reader", default="crnn")
    ap.add_argument("--images", default=str(ROOT / "data" / "samples"))
    ap.add_argument("--db", default=str(ROOT / "data" / "local" / "demo.db"))
    a = ap.parse_args()
    for p in (Path(a.db), Path(a.db).with_suffix(".key")):
        if p.exists():
            p.unlink()
    store = Store(a.db)
    kw = {"uncertain_keys": ["age", "gestation"]} if a.reader == "mock" else {}
    # le scénario attend des doutes sur « age » puis « gestation » ; avec un vrai lecteur
    # les doutes dépendent de la lecture : on répond alors « oui » à ce qui est demandé
    agent = Agent(store, get_reader(a.reader, **kw), online=False, simulate_network=True)
    SF = "+212600000001"
    imgs = Path(a.images)
    p_ident = str(imgs / "dossiers_specimen_10_patientes-02.png")
    p_gross = str(imgs / "dossiers_specimen_10_patientes-59.png")
    p_acc = str(imgs / "dossiers_specimen_10_patientes-04.png")

    say("sys", "=== 1. Capture hors ligne ===")
    send(agent, Incoming(SF, text="bonjour"))
    photo(agent, SF, p_ident)
    say("sys", f"File d'attente : {[(q['kind'], q['state']) for q in store.queue_items()]}")

    say("sys", "=== 2. Retour de la connexion → traitement automatique ===")
    outs = send(agent, Incoming(SF, button_id="toggle_net"), label="[bouton 🔌 Simuler coupure]")

    say("sys", "=== 3. Révision des champs incertains ===")
    if a.reader == "mock":
        # l'agent demande « age » (lu 31) puis « gestation » (lu 3)
        send(agent, Incoming(SF, button_id="yes"), label="[bouton ✅ Oui]")
        send(agent, Incoming(SF, button_id="edit"), label="[bouton ✏️ Corriger]")
        send(agent, Incoming(SF, text="2"))
    else:
        # vrai lecteur : on corrige le premier doute, on confirme les autres
        send(agent, Incoming(SF, button_id="edit"), label="[bouton ✏️ Corriger]")
        send(agent, Incoming(SF, text="Cycles réguliers"))
        answer_doubts(agent, SF)
    say("sys", "=== 4. Validation → liaison patiente (nouveau profil) ===")
    send(agent, Incoming(SF, button_id="confirm"), label="[bouton ✅ Valider la page]")
    send(agent, Incoming(SF, text="aucun"))
    rec_id = store.load_session(SF)["record_id"]
    code = store.get_patient(store.get(rec_id)["patient_id"])["code"]
    say("sys", f"Patiente créée avec le code {code} ; historique de la page : " +
        " → ".join(e["to_state"] for e in store.history(rec_id)))

    say("sys", "=== 5. Page suivante du même carnet ===")
    send(agent, Incoming(SF, button_id="next:yes"), label="[bouton ➕ Oui, page suivante]")
    photo(agent, SF, p_gross)
    answer_doubts(agent, SF)
    send(agent, Incoming(SF, button_id="confirm"), label="[bouton ✅ Confirmer]")
    send(agent, Incoming(SF, button_id="next:no"), label="[bouton ✅ Terminer ce carnet]")

    say("sys", "=== 6. Nouveau carnet, même code → correspondance proposée ===")
    photo(agent, SF, p_acc)
    answer_doubts(agent, SF)
    send(agent, Incoming(SF, button_id="confirm"), label="[bouton ✅ Confirmer]")
    send(agent, Incoming(SF, text=code))
    send(agent, Incoming(SF, button_id="link:1"), label="[bouton Patiente 1]")
    send(agent, Incoming(SF, button_id="next:no"), label="[bouton ✅ Terminer ce carnet]")

    say("sys", "=== 7. Renumérisation d'une page déjà présente ===")
    # même page « identification » rephotographiée (copie pour changer le hash)
    dup = ROOT / "out" / "redigit_ident.png"
    dup.parent.mkdir(exist_ok=True)
    shutil.copy(p_ident, dup)
    import cv2
    im = cv2.imread(str(dup))
    im[5:8, 5:8] = 0   # pixel modifié => autre empreinte
    cv2.imwrite(str(dup), im)
    photo(agent, SF, str(dup).replace("redigit_ident", "dossiers_specimen_10_patientes-02__redigit"))  if False else None
    # (le lecteur simulé reconnaît la page au nom : on renomme)
    dup2 = dup.with_name("dossiers_specimen_10_patientes-02__redigit.png")
    shutil.move(str(dup), str(dup2))
    photo(agent, SF, str(dup2))
    answer_doubts(agent, SF)
    send(agent, Incoming(SF, button_id="confirm"), label="[bouton ✅ Valider la page]")
    send(agent, Incoming(SF, text=code))
    send(agent, Incoming(SF, button_id="link:1"), label="[bouton Patiente 1]")
    send(agent, Incoming(SF, button_id="redig:fill"), label="[bouton Compléter les vides]")
    send(agent, Incoming(SF, button_id="next:no"), label="[bouton ✅ Terminer]")

    say("sys", "=== 8. Coupure réseau pendant l'enregistrement, puis synchro ===")
    agent.set_online(False)
    # vraie photo du défi si elle est présente (non publiée dans le dépôt), sinon une page du spécimen
    real = imgs / "1-2.jpg"
    photo(agent, SF, str(real if real.exists() else imgs / "dossiers_specimen_10_patientes-59.png"))   # traitée hors ligne => file
    say("sys", f"File : {[(q['kind'], q['state']) for q in store.queue_items()]}")
    notes = agent.set_online(True)
    for _, o in notes:
        say("agent", o.text + ("  " + "  ".join(f"[{t}]" for _, t in o.buttons) if o.buttons else ""))

    say("sys", "=== État final des dossiers ===")
    for r in store.list_records(SF):
        say("sys", f"{r['page_type'] or '?':28s} {r['state']:26s} patiente={str(r['patient_id'])[:8]} doc={r['document_id'][:8]}")
    out = ROOT / "out" / "demo_transcript.md"
    out.write_text("# Transcript de démonstration\n\n" + "\n\n".join(f"**{w}** : {t}" for w, t in LOG), encoding="utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
