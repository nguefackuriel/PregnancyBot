"""
Vérifie la configuration WhatsApp Cloud API (Meta) et envoie un message de test.

    export WHATSAPP_TOKEN=...  WHATSAPP_PHONE_ID=...  WHATSAPP_WABA_ID=...
    python scripts/wa_check.py                      # vérifie le jeton, le numéro, l'abonnement du compte à l'app
    python scripts/wa_check.py +212612345678        # envoie aussi un message de test à ce numéro

WHATSAPP_WABA_ID (facultatif) : « WhatsApp Business Account ID », affiché sous le Phone
number ID sur la page « Faites un essai ». S'il est donné, le script abonne le compte à
l'app si ce n'est pas déjà fait : sans cet abonnement, aucun webhook n'arrive.

Les erreurs fréquentes sont expliquées en clair.
"""
from __future__ import annotations

import os
import sys

import httpx

GRAPH = f"https://graph.facebook.com/{os.environ.get('WHATSAPP_GRAPH_VERSION', 'v21.0')}"

EXPLAIN = {
    190: "jeton expiré ou invalide : régénérez le jeton temporaire (24 h) dans WhatsApp > Démarrage de l'API, ou créez un jeton permanent (utilisateur système).",
    100: "paramètre invalide : vérifiez WHATSAPP_PHONE_ID (c'est le « Phone number ID », pas le numéro de téléphone).",
    131030: "le destinataire n'est pas dans la liste des numéros autorisés du numéro de test : ajoutez-le dans WhatsApp > Démarrage de l'API > « To ».",
    131047: "fenêtre de 24 h fermée : le destinataire doit d'abord vous écrire (ou utilisez un modèle de message).",
    131026: "le destinataire n'a pas WhatsApp ou le numéro est mal formé (indicatif pays sans +, ex. 212612345678).",
    10: "permission manquante : le jeton n'a pas whatsapp_business_messaging.",
    200: "permission manquante sur l'app : ajoutez whatsapp_business_messaging et whatsapp_business_management.",
}


def main():
    tok, pid = os.environ.get("WHATSAPP_TOKEN"), os.environ.get("WHATSAPP_PHONE_ID")
    if not tok or not pid:
        print("Il manque WHATSAPP_TOKEN ou WHATSAPP_PHONE_ID dans l'environnement.")
        sys.exit(1)
    h = {"Authorization": f"Bearer {tok}"}
    r = httpx.get(f"{GRAPH}/{pid}", headers=h, params={"fields": "display_phone_number,verified_name,quality_rating"}, timeout=30)
    d = r.json()
    if "error" in d:
        e = d["error"]
        print(f"Échec : {e.get('message')} (code {e.get('code')})")
        print("  ->", EXPLAIN.get(e.get("code"), "voir https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes"))
        sys.exit(2)
    print(f"Jeton et numéro OK : {d.get('verified_name')} {d.get('display_phone_number')} (qualité {d.get('quality_rating')})")
    waba = os.environ.get("WHATSAPP_WABA_ID")
    if not waba:
        # le jeton dit lui-même sur quels comptes WhatsApp Business il a des droits
        try:
            dbg = httpx.get(f"{GRAPH}/debug_token", params={"input_token": tok, "access_token": tok}, timeout=30).json()
            ids = []
            for sc in (dbg.get("data", {}).get("granular_scopes") or []):
                if sc.get("scope") in ("whatsapp_business_management", "whatsapp_business_messaging"):
                    ids += [str(i) for i in sc.get("target_ids", [])]
            ids = list(dict.fromkeys(ids))
            if len(ids) == 1:
                waba = ids[0]
                print("Compte WhatsApp Business trouvé via le jeton :", waba)
            elif len(ids) > 1:
                print("Plusieurs comptes WhatsApp Business pour ce jeton :", ", ".join(ids), "-> mettez le bon dans WHATSAPP_WABA_ID")
        except Exception as e:  # pas bloquant
            print("(recherche du compte via le jeton impossible :", e, ")")
    if waba:
        # quelle est MON app ? (celle à qui appartient le jeton)
        me = httpx.get(f"{GRAPH}/app", headers=h, timeout=30).json()
        my_id, my_name = str(me.get("id", "")), me.get("name", "?")
        print(f"App du jeton : {my_name} (id {my_id})")
        # abonnement du compte à mon app (sans effet s'il existe déjà)
        r2 = httpx.post(f"{GRAPH}/{waba}/subscribed_apps", headers=h, timeout=30).json()
        if not r2.get("success"):
            print("Abonnement du compte à l'app refusé :", r2)
        subs = httpx.get(f"{GRAPH}/{waba}/subscribed_apps", headers=h, timeout=30).json()
        apps = [(str(a.get("whatsapp_business_api_data", {}).get("id", "")), a.get("whatsapp_business_api_data", {}).get("name", "?"))
                for a in subs.get("data", [])]
        print("Apps abonnées au compte WhatsApp Business :", ", ".join(f"{n} ({i})" for i, n in apps) or "aucune")
        if any(i == my_id for i, _ in apps):
            print("-> votre app est abonnée : les webhooks des messages vont lui arriver.")
        else:
            print("-> votre app n'est PAS dans la liste : les webhooks partent ailleurs. Vérifiez que le jeton vient bien de cette app.")
    else:
        print("Compte WhatsApp Business inconnu : mettez WHATSAPP_WABA_ID (page « Faites un essai », sous le Phone number ID)\n"
              "  dans .env, puis relancez. Sans abonnement du compte à l'app, aucun webhook n'arrive.")
    if len(sys.argv) < 2:
        print("Pour envoyer un message de test : python scripts/wa_check.py +2126XXXXXXXX")
        return
    to = sys.argv[1].lstrip("+").replace(" ", "")
    body = {"messaging_product": "whatsapp", "to": to, "type": "text",
            "text": {"body": "DayOne : le serveur est bien relié à WhatsApp. Envoyez la photo d'une page du carnet pour commencer."}}
    r = httpx.post(f"{GRAPH}/{pid}/messages", headers=h, json=body, timeout=30)
    d = r.json()
    if "error" in d:
        e = d["error"]
        print(f"Envoi refusé : {e.get('message')} (code {e.get('code')})")
        print("  ->", EXPLAIN.get(e.get("code"), "voir la page des codes d'erreur"))
        sys.exit(3)
    print("Message envoyé, id :", d.get("messages", [{}])[0].get("id"))
    print("Si rien n'arrive sur le téléphone : le numéro doit avoir écrit en premier au numéro de test (fenêtre de 24 h).")


if __name__ == "__main__":
    main()
