# Tester avec le vrai WhatsApp

Deux chemins. Le premier est l'officiel, gratuit, avec les vrais boutons. Le second
est plus rapide à ouvrir mais sans boutons (on répond par un chiffre).

Dans les deux cas le serveur tourne sur votre ordinateur et WhatsApp doit pouvoir le
joindre : il faut un tunnel HTTPS (ngrok ou cloudflared). Rien d'autre ne change :
le modèle de lecture reste local, les photos sont lues sur votre machine.

## Chemin 1 : WhatsApp Business Cloud API (Meta), numéro de test

Durée : 20 à 30 minutes. Il faut un compte Facebook.

### A. Créer l'app Meta et obtenir le numéro de test

1. Allez sur https://developers.facebook.com/apps et cliquez **Créer une app**.
   Cas d'usage : **Autre**, type : **Business**. Donnez un nom (ex. `DayOne carnet`).
2. Dans le tableau de bord de l'app, ajoutez le produit **WhatsApp** (bouton
   « Configurer »). Meta crée ou vous fait choisir un portefeuille Business.
3. Ouvrez **WhatsApp > Démarrage de l'API** (API Setup). Vous y trouvez :
   - un **numéro de test** gratuit (il commence souvent par +1 555),
   - le **Phone number ID** (une suite de chiffres, ce n'est pas le numéro),
   - un **jeton d'accès temporaire** (valable 24 h, bouton « Générer »).
4. Toujours sur cette page, dans « To » (destinataire), cliquez **Gérer la liste des
   numéros** et ajoutez votre téléphone (et ceux des testeurs, 5 au plus). Chaque
   numéro reçoit un code à recopier.
5. Cliquez le bouton **Envoyer un message** de la page : votre téléphone doit recevoir
   le message « Hello World ». Si oui, le numéro de test marche.

### B. Lancer le serveur et le tunnel

```bash
cd dayone-agent
cp .env.example .env            # puis remplissez WHATSAPP_TOKEN et WHATSAPP_PHONE_ID
set -a; source .env; set +a
python scripts/wa_check.py +2126XXXXXXXX     # vérifie le jeton et envoie un message de test

uvicorn dayone.server:app --app-dir src --port 8000
```

Dans un second terminal :

```bash
ngrok http 8000                  # ou : cloudflared tunnel --url http://localhost:8000
```

Notez l'adresse `https://xxxx.ngrok-free.app` affichée. Vérifiez qu'elle répond :
`https://xxxx.ngrok-free.app/health` doit afficher `"configured": true`.

Compte ngrok gratuit : https://dashboard.ngrok.com (un jeton à coller une fois avec
`ngrok config add-authtoken ...`). L'adresse change à chaque lancement de ngrok,
il faut alors refaire l'étape C.

### C. Brancher le webhook

1. Dans l'app Meta : **WhatsApp > Configuration** (Configuration du webhook), bouton
   **Modifier**.
2. URL de rappel : `https://xxxx.ngrok-free.app/webhook`
3. Jeton de vérification : la valeur de `WHATSAPP_VERIFY_TOKEN` (par défaut `dayone`).
4. **Vérifier et enregistrer**. Le serveur affiche une ligne `GET /webhook ... 200`.
5. Sous « Champs du webhook », cliquez **Gérer** et abonnez-vous à **messages**.

### D. Tester

Depuis un numéro autorisé, écrivez `bonjour` au numéro de test. L'agent répond avec
le menu et ses boutons. Envoyez ensuite la **photo d'une page** du carnet (une page du
spécimen affichée à l'écran et prise en photo fait l'affaire, ou le PNG envoyé
comme photo). L'agent lit la page, résume, pose ses doutes un par un.

Commandes utiles en cours de test : `menu`, `dossiers`, `connexion` (état réel du
réseau du serveur et pages en attente), `saisie manuelle`.

Pour voir le hors ligne pour de vrai : coupez le wifi du téléphone, prenez deux photos
dans WhatsApp (elles restent « en attente » dans la discussion), remettez le wifi : elles
partent, et l'agent annonce « votre photo de 14h02 vient d'arriver, je la lis maintenant ».
Côté serveur : coupez internet sur l'ordinateur, envoyez une photo depuis le téléphone,
remettez internet : Meta relivre le webhook, le serveur rattrape (boîte d'entrée,
boîte de sortie, file IA), `/health` montre `network` et `transport`.

### E. Pour durer plus de 24 h

Le jeton temporaire expire. Pour un jeton permanent : https://business.facebook.com
> Paramètres > **Utilisateurs système** > Ajouter (rôle Admin) > **Générer un jeton** pour
votre app, avec les permissions `whatsapp_business_messaging` et
`whatsapp_business_management`. Mettez-le dans `WHATSAPP_TOKEN`.

Pour vérifier la signature des webhooks (recommandé dès que l'URL est publique
longtemps) : **Paramètres de l'app > Général > Clé secrète**, à mettre dans
`WHATSAPP_APP_SECRET`. Sans elle, le serveur accepte tout webhook au bon format.

### Limites du numéro de test

- 5 destinataires au plus, à ajouter à la main.
- Le serveur ne fait que répondre, dans les 24 h qui suivent le dernier message de la
  sage-femme : c'est exactement notre usage, aucun modèle de message n'est nécessaire.
- Pour un vrai numéro et des destinataires illimités, il faut un numéro de téléphone
  dédié et la vérification de l'entreprise par Meta (quelques jours).

## Chemin 2 : bac à sable WhatsApp de Twilio

Durée : 10 minutes. Pas de boutons : les choix sont numérotés, on répond `1`, `2`...

1. Créez un compte sur https://www.twilio.com/try-twilio (essai gratuit).
2. Console : **Messaging > Try it out > Send a WhatsApp message**. Twilio affiche un
   numéro (+1 415 523 8886) et un mot de passe du type `join xxxx-yyyy`. Envoyez ce
   texte depuis votre WhatsApp au numéro. Chaque testeur doit le faire.
3. Dans **Sandbox settings**, « When a message comes in » :
   `https://xxxx.ngrok-free.app/twilio`, méthode POST. Enregistrez.
4. Récupérez **Account SID** et **Auth Token** sur la page d'accueil de la console.

```bash
export TWILIO_ACCOUNT_SID=AC...  TWILIO_AUTH_TOKEN=...  TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
export PUBLIC_BASE_URL=https://xxxx.ngrok-free.app      # pour envoyer les recadrages de cellules
uvicorn dayone.server:app --app-dir src --port 8000
```

Écrivez `bonjour` au numéro Twilio, puis envoyez une photo. Les recadrages de
cellules que l'agent montre pour ses doutes sont servis depuis `/media/...` sur
votre tunnel, c'est pour cela que `PUBLIC_BASE_URL` est nécessaire.

## Ce que fait le serveur (pour comprendre les traces)

- `POST /webhook` répond `200` tout de suite et lit la page en arrière-plan : Meta
  renverrait sinon le même webhook plusieurs fois.
- Un message déjà vu (même identifiant) est ignoré, même après un redémarrage.
- Chaque message passe par une boîte d'entrée chiffrée ; chaque réponse qui ne part pas
  par une boîte de sortie chiffrée ; une boucle de fond (toutes les 15 s, `DAYONE_NET_CHECK_S`)
  mesure l'accès réel à Meta et rejoue tout au retour. `/health` donne `network`,
  `transport` (tailles des boîtes) et `ai_queue`.
- Les messages sont traités un par un, dans l'ordre.
- Le message reçu est marqué « lu » pendant la lecture.
- Plus de 3 choix : WhatsApp impose une liste déroulante (10 lignes au plus).
  Un chiffre tapé au clavier vaut aussi le bouton de ce rang.
- Un vocal, une vidéo ou un document qui n'est pas une image : l'agent redemande
  une photo.

## Problèmes courants

| Symptôme | Cause probable | Quoi faire |
|---|---|---|
| « Vérifier et enregistrer » échoue | tunnel éteint, mauvais jeton de vérification, ou `/webhook` manquant dans l'URL | ouvrez `https://.../health` dans le navigateur, comparez `WHATSAPP_VERIFY_TOKEN` |
| le message arrive dans les traces mais rien ne revient | jeton expiré (code 190) ou numéro non autorisé (131030) | `python scripts/wa_check.py +...` explique le code |
| rien n'arrive dans les traces | webhook pas abonné à `messages`, ou adresse ngrok changée | revoir l'étape C |
| la photo est refusée « floue / trop sombre » | contrôle qualité | reprendre à plat, sans ombre ; envoyer la photo en « document » garde la pleine résolution |
| `signature invalide` | `WHATSAPP_APP_SECRET` ne correspond pas à l'app | retirez la variable ou mettez la bonne clé |
