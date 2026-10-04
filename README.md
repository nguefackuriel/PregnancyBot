# PregnancyBot

**Une sage-femme, un téléphone, un carnet de grossesse. Et une IA qui tourne en local.**

PregnancyBot est un agent WhatsApp qui lit la photo d'une page du carnet de grossesse
(la « Fiche de surveillance de la grossesse et du post-partum » du Ministère de la
Santé, Maroc) et en fait un dossier numérique structuré. Chaque champ reçoit un
statut et une confiance. La sage-femme confirme, corrige, ou reprend la photo. Tout
fonctionne sans réseau, rien ne part vers un service d'IA externe : le modèle de
lecture tourne sur l'ordinateur du centre de santé, sans clé API.

Projet de l'équipe **Side Quest** pour le défi DayOne (hackathon CodeML, défi 17).

🎬 Vidéo de démonstration (5 min) : [docs/DayOne.mp4](docs/DayOne.mp4) — [YouTube](https://youtu.be/_lOit6A2P5A)

```
photo WhatsApp > qualité > type de page > recalage sur le gabarit > masquage des identifiants
   > encre par cellule > lecture locale (CRNN) > normalisation > règles > statut + confiance
   > conversation (doutes un par un) > validation > liaison patiente > synchronisation
```

## Sommaire

1. [Ce que fait l'agent](#1-ce-que-fait-lagent)
2. [Installation pas à pas](#2-installation-pas-à-pas)
3. [Lancer le simulateur, la démo, les tests](#3-lancer-le-simulateur-la-démo-les-tests)
4. [Brancher le vrai WhatsApp](#4-brancher-le-vrai-whatsapp)
5. [Résultats mesurés](#5-résultats-mesurés)
6. [Choix de conception](#6-choix-de-conception)
7. [Cycle de vie d'un enregistrement](#7-cycle-de-vie-dun-enregistrement)
8. [Hors ligne, pour de vrai](#8-hors-ligne-pour-de-vrai)
9. [Liaison patiente et confidentialité](#9-liaison-patiente-et-confidentialité)
10. [Réentraîner le modèle](#10-réentraîner-le-modèle)
11. [Organisation du dépôt](#11-organisation-du-dépôt)
12. [Limites connues et suite](#12-limites-connues-et-suite)
13. [In English, briefly](#13-in-english-briefly)

## 1. Ce que fait l'agent

La sage-femme envoie la photo d'une page. L'agent :

1. vérifie la qualité de la photo (flou, lumière) et demande une reprise si besoin ;
2. reconnaît le type de page (couverture, identification, grossesse actuelle,
   accouchement, post-partum mère ou nouveau-né, précoce ou tardif) et demande
   confirmation quand il hésite ;
3. recale la photo sur le gabarit de la page et masque les identifiants directs (nom,
   CIN, adresse, téléphone, nom du mari) avant toute lecture ;
4. lit chaque cellule avec un petit modèle local, normalise les valeurs (dates, tension,
   âge gestationnel, poids, vocabulaire fermé) et applique les règles de cohérence ;
5. donne à chaque champ un statut (`CONNU`, `A_REVISER`, `ILLISIBLE`, `NON_FOURNI`,
   `NON_APPLICABLE`, `INCONNU`) et une confiance calibrée ;
6. résume la page, puis pose ses doutes un par un, avec le recadrage de la cellule :
   Confirmer, Corriger, Reprendre la photo, Je ne sais pas ;
7. propose la saisie à la main si la lecture échoue trois fois ou si la mise en page
   n'est pas reconnue ;
8. relie la page à une patiente par le code écrit sur le carnet, ou propose des
   correspondances possibles sans jamais créer un doublon tout seul ;
9. garde tout chiffré, en file si le réseau manque, et synchronise au retour.

## 2. Installation pas à pas

Il faut Python 3.10 ou plus. Aucune carte graphique, aucune clé API.

```bash
# 1. récupérer le code
git clone https://github.com/nguefackuriel/PregnancyBot.git
cd PregnancyBot

# 2. un environnement Python isolé
python3 -m venv venv
source venv/bin/activate            # Windows : venv\Scripts\activate

# 3. les dépendances (pas de PyTorch nécessaire pour lire)
pip install -r requirements.txt

# 4. facultatif : tesseract, lecteur de secours
#    macOS : brew install tesseract      Debian/Ubuntu : sudo apt install tesseract-ocr

# 5. vérifier que tout marche (34 tests, aucun réseau)
pytest -q
```

Les données du défi (les 80 pages du spécimen, le PDF, les vraies photos) ne sont pas
dans le dépôt. Pour les scripts qui en ont besoin (évaluation, réentraînement), placez
le dossier `Paper Registry` du défi à côté du dépôt, dans `../data/Paper Registry`, ou
donnez son chemin avec `--images`.

## 3. Lancer le simulateur, la démo, les tests

**Le simulateur WhatsApp dans le navigateur** (aucun compte nécessaire) :

```bash
python -m uvicorn dayone.server:app --app-dir src --port 8000
# puis ouvrir http://localhost:8000
```

On y envoie les pages d'exemple, on répond aux doutes avec les boutons, on coupe le
réseau avec l'interrupteur pour voir la file, on le remet pour voir la synchronisation.

**La démo scriptée** joue tout le scénario du défi dans le terminal et écrit la
conversation dans `out/demo_transcript.md` : capture hors ligne, retour du réseau,
révision d'un champ incertain, validation, liaison d'une patiente (nouveau code),
page suivante du même carnet, deuxième carnet avec correspondance à trancher,
renumérisation, coupure pendant l'enregistrement puis synchronisation.

```bash
python scripts/demo.py --reader crnn
```

**Les tests** : `pytest -q`. Ils couvrent la normalisation, les règles, le cycle de
vie, le flux conversationnel complet, les briques de lecture locale, et les
adaptateurs WhatsApp (webhooks réels rejoués sans réseau).

## 4. Brancher le vrai WhatsApp

Le guide complet est dans [`WHATSAPP.md`](WHATSAPP.md). En résumé : une app Meta avec
le produit WhatsApp (numéro de test gratuit), le serveur sur votre ordinateur, un tunnel
HTTPS (`cloudflared` ou `ngrok`), le webhook branché sur `/webhook`. Comptez 20 minutes.

```bash
cp .env.example .env                 # remplir WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_WABA_ID
python scripts/wa_check.py +2126XXXXXXXX     # vérifie tout et envoie un message de test
python -m uvicorn dayone.server:app --app-dir src --port 8001
cloudflared tunnel --url http://localhost:8001
```

Le serveur a été testé avec de vrais téléphones sur le numéro de test Meta. Un
bac à sable Twilio est aussi pris en charge (sans boutons, choix numérotés).

## 5. Résultats mesurés

Mesurés avec `scripts/evaluate.py` (exactitude par champ, statuts, cases, calibration),
cible `--on-image` (ce que montre vraiment l'image). Lecteur local `crnn`, modèle
livré dans `data/models/crnn.onnx` : entraîné sur les patientes 1 à 8 (pages propres
et versions dégradées) plus du synthétique ; les patientes 9 et 10 n'ont jamais servi à
l'entraînement.

Sur les 80 pages propres du spécimen :

| Lexique | Type de page | Valeurs exactes | Statuts | Cases | ECE |
|---|---|---|---|---|---|
| avec (défaut) | 100 % | 98.7 % | 98.7 % | 99.1 % | 0.012 |
| sans (`DAYONE_LEXICON=0`) | 100 % | 96.0 % | 98.8 % | 99.1 % | 0.022 |

Par police d'écriture (avec lexique) : Caveat 99.5 %, NanumPen 99.0 %, ReenieBeanie
99.2 %, ShadowsIntoLight 98.8 %, Gaegu 97.1 % des valeurs. Les 1.3 % de statuts faux
sont pour moitié des tirets trop fins pour être vus et pour moitié des valeurs justes
passées en `A_REVISER` par prudence.

Sur des photos façon téléphone (`scripts/degrade.py` : perspective, ombre, flou,
bruit, JPEG) :

| Niveau | Type de page | Valeurs exactes | Statuts | Cases | ECE |
|---|---|---|---|---|---|
| léger | 100 % | 97.7 % | 98.5 % | 100 % | 0.017 |
| moyen | 100 % | 81.9 % | 93.8 % | 100 % | 0.112 |
| fort (refusé par le contrôle qualité, reprise demandée) | 86 % | 11 % | 58 % | 66 % | |

Au niveau moyen, quand le modèle dit `CONNU` il a raison 9 fois sur 10, et il passe
le reste en `A_REVISER` : la sage-femme est sollicitée, pas trompée.

Sur les cellules seules (validation, patientes 9 et 10) : 95.5 % des cellules propres
et 90.0 % des cellules de photos dégradées sont lues sans aucune faute de caractère ;
100 % des cellules vides sont lues vides.

Couche géométrique seule (lecteur simulé qui renvoie la vérité terrain) : 80 pages,
type de page 100 %, valeurs 99.7 %, statuts 99.2 %, cases 99.1 %. Tesseract seul :
21 % des valeurs, c'est pour cela qu'il n'est qu'un secours.

Sur les 5 vraies photos du défi (vrai carnet, petit format, deux pages par vue), on a
construit un gabarit pour chacune des 5 pages physiques (`data/templates_carnet.json`,
fait par `scripts/build_real_template.py` à partir des traits du formulaire), puis noté
à l'œil 108 cellules lisibles (`data/ground_truth/real_photos_sample.json`).
`python scripts/score_real.py` donne, sans arrondir la réalité :

| Mesure | Résultat |
|---|---|
| Type de page reconnu | 5 / 5 |
| Gabarit du vrai carnet reconnu, traits alignés | 5 / 5 |
| Groupes de cases à cocher justes | 9 / 9 |
| Cellules vides reconnues vides | 25 / 27 (93 %) |
| Cellules écrites lues exactement | 1 / 81 |
| Champ enregistré comme sûr sans la sage-femme | 0 |

La géométrie suit : chaque cellule est découpée au bon endroit. La lecture de l'écriture
ne suit pas : le modèle n'a jamais vu d'écriture cursive réelle. L'agent le sait, il ne
garde rien comme `CONNU` sur ce carnet et propose trois choix à la sage-femme : réviser
un par un (avec la photo de chaque cellule), saisir l'essentiel, ou garder la page à
réviser plus tard. La prochaine étape est claire : annoter des photos du vrai carnet et
réentraîner, avec le même outillage que pour le spécimen.

Ce qui a fait monter le score, dans l'ordre : entraîner sur les 80 pages (74.9 % vers
92.6 %), recaler axe par axe (93.8 %), lire ligne par ligne les grandes zones de texte
(95.2 %), rapprocher du vocabulaire du carnet (98.4 %), entraîner sur photos dégradées
(niveau moyen de 74 % à 82 %).

Pour tout refaire :

```bash
# vérité terrain des 80 pages, extraite du PDF (déjà dans data/ground_truth)
python scripts/auto_label_pdf.py "../data/Paper Registry/dossiers_specimen_10_patientes.pdf" \
    --schema data/schema/registry_schema.json --out data/ground_truth/ground_truth.json

# lecture d'un lot + scores
python scripts/run_extraction.py --images "../data/Paper Registry" --reader crnn --dedupe --on-image

# jeu de test façon photo de téléphone, vérité terrain transformée
python scripts/degrade.py --images "../data/Paper Registry" --gt data/ground_truth/ground_truth.json --out out/degraded --variants 3
python scripts/run_extraction.py --images out/degraded --gt out/degraded/ground_truth.json --reader crnn --on-image
```

## 6. Choix de conception

**Un modèle local, sans clé.** La consigne interdit d'envoyer des données réelles à un
service tiers. Le lecteur par défaut est un petit modèle de reconnaissance d'écriture
(CRNN + CTC, 2 millions de paramètres, 8 Mo en ONNX) entraîné par nous sur les cellules
du carnet. Il lit une page en 2 à 4 secondes sur un processeur ordinaire. Deux autres
lecteurs existent, sans clé non plus : `ollama` (modèle vision local, pour une écriture
très variable) et `tesseract` (secours). Un lecteur `anthropic` reste dans le code pour
comparer sur des données synthétiques seulement, jamais par défaut.

**Le schéma d'abord.** `data/schema/registry_schema.json` décrit 436 champs typés, 69
groupes de cases, les 6 statuts, les codes écrits à la main (`NF`, `IG`, le tiret), 13
contrôles de cohérence, le cycle de vie et la règle de liaison. La vérité terrain, le
pipeline, l'agent et l'évaluation parlent tous cette langue. Le document lisible est
`analysis/schema/SCHEMA.md`.

**La vérité terrain vient du PDF.** Le spécimen est un PDF vectoriel : chaque valeur
manuscrite est un texte avec sa police et sa position. `scripts/auto_label_pdf.py` en
tire 80 pages annotées (2 110 champs, 1 970 cases) sans aucune annotation manuelle. Les
5 polices d'écriture, extraites du PDF, servent à fabriquer des cellules synthétiques
en quantité illimitée pour entraîner le modèle.

**Des statuts, pas de « N/A ».** `NON_FOURNI` (vide sur la page), `NON_APPLICABLE`
(tiret, zone barrée, ou règle : indication de césarienne si voie basse, RAI si Rh+,
accouchement n si parité plus petite), `ILLISIBLE` (encre présente, lecture impossible),
`A_REVISER` (lu mais peu sûr, hors vocabulaire ou incohérent), `INCONNU` (page non
capturée, IA en panne, « je ne sais pas »), `CONNU`. Une réponse de la sage-femme passe
le champ en `CONNU`, confiance 1.0, source `midwife`.

**La confiance.** `0.55 x confiance du lecteur + 0.35 x score de normalisation + 0.10`,
divisée par deux si le lecteur dit « illisible », réduite de 30 % si une règle est
violée. Seuils : `CONNU` à partir de 0.85, `A_REVISER` à partir de 0.5. L'ECE mesure
si ces chiffres sont honnêtes (0.012 sur les 80 pages).

**La géométrie.** Le gabarit de chaque type de page (`data/templates.json`, tiré du PDF)
donne la position de chaque cellule et de chaque case. Une photo est redressée par le
contour de la page, puis affinée par les lignes du formulaire, axe par axe. Le type de
page vient de l'OCR du titre, puis en secours de la signature des lignes. La détection
d'encre évite d'appeler le lecteur sur une case vide, donc le lecteur ne peut pas y
inventer une valeur. Un tiret n'est accepté que si l'encre forme un trait court et
compact. Les grandes zones de texte libre sont lues ligne par ligne. Si la mise en
page ne correspond à aucun gabarit, l'agent le dit et passe en saisie manuelle.

**Le vocabulaire.** `data/lexicon.txt` liste les mots du carnet (vocabulaire fermé,
lieux, professions, phrases usuelles). Une lecture proche d'un mot connu est rapprochée
de ce mot, avec la trace de l'écart dans la note et dans le score. Le fichier se
complète à la main, `DAYONE_LEXICON=0` le coupe, et les scores sont donnés avec et sans.

**La conversation.** `agent.py` ne dépend pas du transport : simulateur web, adaptateur
WhatsApp Cloud API de Meta, bac à sable Twilio. Résumé de la page, doutes un par un avec
le recadrage de la cellule, corrections « numéro = valeur », saisie manuelle guidée,
sessions multipages, renumérisation (Remplacer, Compléter, Garder), question sur le
type de page quand précoce et tardif ne se distinguent pas. WhatsApp impose 3 boutons
par message, au-delà une liste, et un chiffre tapé vaut le bouton de ce rang.

## 7. Cycle de vie d'un enregistrement

Chaque capture est un enregistrement avec un état, et aucun enregistrement n'est
jamais supprimé par le système. Les transitions sont vérifiées par `lifecycle.py` et
chacune est journalisée (qui, quand, pourquoi).

```
CAPTURE > EN_ATTENTE_IA > TRAITE_IA > A_REVISER > VALIDE > PATIENTE_LIEE > ENREGISTRE > SYNCHRONISE
```

| De | Vers | Quand |
|---|---|---|
| `CAPTURE` | `EN_ATTENTE_IA` | photo acceptée (qualité suffisante), chiffrée, mise en file |
| `EN_ATTENTE_IA` | `TRAITE_IA` | lecture réussie |
| `EN_ATTENTE_IA` | `ECHEC_TRAITEMENT` | erreur ou délai de l'IA, trois essais, puis `REVISION_MANUELLE_REQUISE` |
| `TRAITE_IA` | `A_REVISER` | au moins un champ `A_REVISER` ou `ILLISIBLE`, ou mise en page non reconnue |
| `TRAITE_IA` ou `A_REVISER` | `VALIDE` | la sage-femme a confirmé, corrigé ou saisi chaque champ douteux |
| `A_REVISER` | `CAPTURE` | la sage-femme reprend la photo |
| `REVISION_MANUELLE_REQUISE` | `VALIDE` | saisie manuelle complète, par questions |
| `VALIDE` | `PATIENTE_LIEE` | code reconnu, ou choix explicite Patiente 1 / Patiente 2 / Aucune (créer) |
| `VALIDE` | `DOUBLON_SUSPECTE` | correspondance plausible mais non tranchée (« Je ne sais pas ») |
| `DOUBLON_SUSPECTE` | `PATIENTE_LIEE` | la sage-femme tranche ; renumérisation : Remplacer, Compléter, Garder |
| `PATIENTE_LIEE` | `ENREGISTRE` | fusion dans le profil de la patiente, image masquée attachée |
| `ENREGISTRE` | `SYNCHRONISE` | envoi au serveur accusé |
| `ENREGISTRE` | `ECHEC_SYNCHRO` | erreur réseau : reste en file, réessai, jamais de perte |

## 8. Hors ligne, pour de vrai

Trois pannes réelles, trois réponses, aucune simulation en production.

- **Le téléphone n'a pas de réseau.** WhatsApp garde les photos et les envoie au retour.
  Le serveur reçoit l'heure réelle d'envoi, la garde comme date de capture, et prévient :
  « Votre photo de 14h02 vient d'arriver (2 h de retard, réseau coupé entre-temps). Je la
  lis maintenant ; sa date de capture reste 14h02. »
- **Le serveur perd internet.** Chaque message reçu est mis dans une boîte d'entrée
  chiffrée avant traitement ; chaque réponse qui ne part pas va dans une boîte de sortie
  chiffrée. Une boucle de fond mesure l'accès réel à Meta toutes les 15 secondes, met
  l'agent hors ligne, et rejoue tout au retour, sans attendre. Meta renvoie aussi les
  webhooks sans réponse pendant des jours. Les messages déjà traités sont mémorisés en
  base : un webhook rejoué après un redémarrage ne relit pas la page.
- **L'IA ne répond pas.** La page reste `EN_ATTENTE_IA`, chiffrée ; trois essais, puis
  saisie manuelle guidée. La même boucle relance la file.

`store.py` : SQLite, charges utiles et images chiffrées (Fernet) avec une clé hors base,
boîtes et file qui survivent au redémarrage. `/health` montre l'état réseau, les tailles
des boîtes et la file IA. Le simulateur web garde un interrupteur « Simuler coupure »
pour montrer le mécanisme sans débrancher quoi que ce soit.

## 9. Liaison patiente et confidentialité

**Liaison.** Le code écrit par la sage-femme sur le carnet (quatre lettres et deux
chiffres, généré par l'agent à la création) est la clé. Sans code, l'agent compare des
signaux non identifiants (DDR, DPA, date d'accouchement, établissement, âge, G/P) et
propose : Patiente 1, Patiente 2, Aucune (créer), Je ne sais pas (le dossier attend en
`DOUBLON_SUSPECTE`). Jamais de création automatique quand une correspondance est
plausible. Une page déjà numérisée pour la même patiente déclenche la renumérisation :
Remplacer, Compléter les vides, Garder l'ancienne.

**Confidentialité.** Nom, CIN, adresse, téléphone, nom du mari : leurs zones sont
masquées sur la page recalée avant toute lecture ; ces champs sont `REDACTED`, jamais
lus, jamais stockés. L'image conservée est la version masquée, chiffrée, et son accès
passe par un rôle (`X-Role: midwife | supervisor`). Les identifiants internes sont des
UUID. Le numéro WhatsApp sert de clé de session, pas de donnée patiente. Avec l'API
officielle de WhatsApp, la photo transite par Meta : c'est le canal imposé par le
défi ; la lecture et le stockage, eux, restent chez nous.

## 10. Réentraîner le modèle

PyTorch est nécessaire pour entraîner, pas pour lire.

```bash
pip install -r requirements-train.txt
# photos façon téléphone des 80 pages, niveaux léger et moyen
python scripts/degrade.py --images "../data/Paper Registry" --gt data/ground_truth/ground_truth.json \
    --out out/degraded80 --variants 2 --levels legere,moyenne
# entraînement : synthétique + cellules réelles + cellules des photos dégradées recalées
python scripts/train_crnn.py --images "../data/Paper Registry" --degraded out/degraded80 --steps 4000
python scripts/export_onnx.py                               # crnn.pt -> crnn.onnx
```

Comptez une heure sur un portable. Les patientes 9 et 10 restent en validation. Le
script affiche à chaque étape l'exactitude sur les cellules propres, les cellules
dégradées et les cellules vides.

## 11. Organisation du dépôt

```
src/dayone/
  schema.py        schéma + gabarits, seuils
  geometry.py      qualité, contour de page, recalage, type de page, encre, cases
  normalize.py     vocabulaire fermé, types (date, TA, SA+j, poids), codes NF / tiret / IG, lexique
  rules.py         NON_APPLICABLE par dépendance, cohérence entre champs
  extract.py       pipeline photo -> champs + statuts + confiance
  ocr/             crnn.py (modèle + décodage), synth.py (rendu), dataset.py
  reader/          crnn (défaut), ollama, tesseract, anthropic (synthétique seulement), mock
  pii.py           masquage des identifiants
  lifecycle.py     machine à états
  store.py         SQLite chiffré : dossiers, patientes, file IA, boîtes d'entrée et de sortie
  linking.py       liaison patiente
  agent.py         la conversation (indépendante du transport)
  server.py        FastAPI : simulateur web, API, webhooks WhatsApp et Twilio, /health
  whatsapp.py      WhatsApp Cloud API (Meta), résilience réelle
  twilio_wa.py     bac à sable Twilio
scripts/           auto_label_pdf, build_templates, degrade, run_extraction, evaluate,
                   train_crnn, export_onnx, demo, wa_check
tests/             34 tests
web/simulator.html simulateur WhatsApp
data/              schema, templates.json, ground_truth, fonts, lexicon.txt, models/crnn.onnx, samples
analysis/          analyse du défi, schéma lisible (SCHEMA.md), outils d'annotation et d'évaluation
docs/              soumission Devpost, vidéo de démonstration
WHATSAPP.md        brancher le vrai WhatsApp, pas à pas
```

## 12. Limites connues et suite

- Le modèle n'a vu que l'écriture synthétique du spécimen (5 polices). Les gabarits du
  vrai carnet existent (5 pages physiques) et découpent les bonnes cellules, mais la
  lecture de l'écriture réelle reste à apprendre : il faut des photos annotées. D'ici là,
  l'agent ne garde rien comme sûr sur ce carnet et s'appuie sur la sage-femme.
- Le lexique contient le vocabulaire du spécimen, y compris les noms du personnel qui y
  figurent ; les scores sont donnés avec et sans.
- L'arabe n'est pas traité : il est absent des données fournies.
- La formule de confiance est un point de départ ; elle se règle avec l'ECE.
- Le numéro de test Meta limite à 5 destinataires ; un vrai numéro demande la
  vérification de l'entreprise.

Suite prévue, dans l'ordre : annotations du vrai carnet et réentraînement ; contrôle qualité
sur l'appareil avant envoi ; interface bilingue français/anglais ; tableau de bord
d'agrégats anonymisés (tension, température, VIH, syphilis, hépatite) ; pages
multilingues et écriture arabe.

## 13. In English, briefly

PregnancyBot is a WhatsApp agent that turns a photo of a page from the Moroccan
pregnancy booklet into a structured record. Each field gets a status (`KNOWN`,
`TO_REVIEW`, `ILLEGIBLE`, `NOT_PROVIDED`, `NOT_APPLICABLE`, `UNKNOWN`) and a calibrated
confidence. The midwife confirms, edits or retakes the photo; the agent asks only about
what it doubts, with a crop of the cell. Reading is done by a small local handwriting
model (CRNN + CTC, 8 MB, ONNX, no GPU, no API key), trained on cells rendered with the
booklet's own fonts and on the real specimen pages. Identifiers are masked before any
reading and never stored. Everything is encrypted locally; incoming messages, outgoing
replies and AI jobs sit in persistent queues, so nothing is lost when the phone, the
server or the model is offline. Patient linking uses a midwife code first, then
non-identifying signals, and never creates a duplicate on its own. On the 80 specimen
pages: 98.7 % exact values, 98.7 % correct statuses, 99.1 % checkbox groups, ECE 0.012.
Install with `pip install -r requirements.txt`, run `pytest -q`, then
`python -m uvicorn dayone.server:app --app-dir src --port 8000` for the web simulator,
or follow `WHATSAPP.md` to connect a real WhatsApp number.
