# Soumission du projet (Devpost et jury)

Équipe **Side Quest**. Défi DayOne, hackathon CodeML, défi 17.

## 1. Ce que le défi demande, et où c'est

| Livrable demandé | Où le trouver |
|---|---|
| Code source : pipeline d'extraction, flux conversationnel, file hors ligne, liaison patiente | `src/dayone/` (`extract.py`, `agent.py`, `store.py` + `whatsapp.py`, `linking.py`) |
| Prototype conversationnel type WhatsApp, simulé ou réel | simulateur web `web/simulator.html` et vrai WhatsApp (`WHATSAPP.md`), testé avec de vrais téléphones |
| README : installation, choix de conception, cycle de vie, limites connues | `README.md` sections 2, 6, 7, 12 |
| Démo : capture hors ligne, retour de la connexion, révision d'un champ incertain, décision de correspondance | vidéo (lien dans le README) et `python scripts/demo.py` qui rejoue le même scénario |

## 2. Le barème, et ce qu'on met en avant

| Critère | Points | Nos arguments |
|---|---|---|
| Qualité de l'extraction | 30 | 98.7 % des valeurs sur les 80 pages, 97.7 % sur photos dégradées légères, modèle local entraîné par nous ; vérité terrain automatique depuis le PDF ; harnais d'évaluation reproductible |
| Gestion de l'incertitude | 20 | 6 statuts, confiance calibrée (ECE 0.012), l'agent dit quand il doute, ne garde rien comme sûr si la mise en page est inconnue |
| Flux de vérification | 20 | résumé puis doutes un par un avec recadrage, Confirmer / Corriger / Reprendre / Je ne sais pas, saisie manuelle guidée, sessions multipages, renumérisation |
| Robustesse hors ligne | 15 | boîtes d'entrée et de sortie chiffrées, file IA, boucle de fond qui mesure le réseau réel, dédoublonnage persistant, date de capture réelle ; rien n'est perdu |
| Liaison et confidentialité | 10 | code sage-femme, candidats proposés, jamais de doublon automatique ; identifiants masqués avant lecture, jamais stockés ; image masquée à accès par rôle |
| Code et documentation | 5 | 34 tests, README pas à pas, guide WhatsApp, schéma lisible, scripts reproductibles |
| Bonus | | vrai WhatsApp branché (Meta Cloud API) ; en cours : qualité d'image sur l'appareil, tableau de bord anonymisé, bilingue, arabe |

## 3. Textes pour Devpost

### Nom du projet

PregnancyBot

### Phrase d'accroche (tagline)

FR : Une sage-femme envoie la photo d'une page du carnet de grossesse sur WhatsApp ; une IA locale en fait un dossier, et dit quand elle doute.

EN : A midwife sends a photo of a pregnancy booklet page on WhatsApp; a local AI turns it into a record, and says when it is unsure.

### Inspiration

FR : Au Maroc, le suivi de grossesse se fait sur un carnet papier rose. Les données y restent, difficiles à agréger, faciles à perdre. Les sages-femmes ont déjà un téléphone et WhatsApp. On a voulu un outil qui ne demande rien de plus : une photo, quelques réponses, et le dossier existe. Et comme ce sont des données de santé, rien ne doit partir vers un service d'IA externe.

EN : In Morocco, pregnancy follow-up lives in a pink paper booklet. The data stays there, hard to aggregate, easy to lose. Midwives already have a phone and WhatsApp. We wanted a tool that asks for nothing more: one photo, a few answers, and the record exists. And because this is health data, nothing should leave for an external AI service.

### Ce que ça fait (What it does)

FR : PregnancyBot lit la photo d'une page du carnet, reconnaît le type de page, masque les identifiants, lit chaque cellule avec un petit modèle local, et donne à chaque champ un statut (connu, à réviser, illisible, non fourni, non applicable, inconnu) et une confiance calibrée. Il résume la page, pose ses doutes un par un avec le recadrage de la cellule, accepte les corrections, propose la saisie à la main si la lecture échoue, relie la page à une patiente par un code, et propose des correspondances possibles sans jamais créer de doublon tout seul. Tout est chiffré, mis en file si le réseau manque, et synchronisé au retour. Ça marche dans le vrai WhatsApp.

EN : PregnancyBot reads a photo of a booklet page, recognizes the page type, masks identifiers, reads each cell with a small local model, and gives each field a status (known, to review, illegible, not provided, not applicable, unknown) with a calibrated confidence. It summarizes the page, asks about its doubts one by one with a crop of the cell, accepts corrections, offers manual entry when reading fails, links the page to a patient by a code, and proposes possible matches without ever creating a duplicate on its own. Everything is encrypted, queued when the network is down, and synced when it returns. It works in real WhatsApp.

### Comment on l'a construit (How we built it)

FR : Le schéma d'abord : 436 champs, 69 groupes de cases, 6 statuts, 13 règles de cohérence, un cycle de vie à 8 états. Ensuite une découverte : le spécimen est un PDF vectoriel, donc chaque valeur manuscrite y est un texte avec sa police et sa position. On en a tiré 80 pages de vérité terrain sans annotation manuelle, et les 5 polices d'écriture pour fabriquer des cellules synthétiques à volonté. Avec ça, on a entraîné un petit modèle de lecture (CRNN + CTC, 2 millions de paramètres, 8 Mo en ONNX) qui tourne sur CPU. Autour : recalage géométrique sur le gabarit, détection d'encre, normalisation par vocabulaire, règles, confiance calibrée mesurée par l'ECE. L'agent conversationnel est indépendant du transport : simulateur web, WhatsApp Cloud API de Meta, Twilio. Python, OpenCV, PyTorch pour entraîner, onnxruntime pour lire, FastAPI, SQLite chiffré.

EN : Schema first: 436 fields, 69 checkbox groups, 6 statuses, 13 consistency rules, an 8-state lifecycle. Then a discovery: the specimen is a vector PDF, so every handwritten value is a text with its font and position. We extracted 80 ground-truth pages with no manual annotation, and the 5 handwriting fonts to render unlimited synthetic cells. With that we trained a small handwriting model (CRNN + CTC, 2M parameters, 8 MB ONNX) that runs on CPU. Around it: geometric registration on the template, ink detection, vocabulary normalization, rules, calibrated confidence measured by ECE. The conversational agent is transport-independent: web simulator, Meta WhatsApp Cloud API, Twilio. Python, OpenCV, PyTorch to train, onnxruntime to read, FastAPI, encrypted SQLite.

### Difficultés (Challenges)

FR : Faire tenir une bonne lecture sans aucun service externe. Apprendre au modèle à lire « rien » sur une cellule vide bruitée plutôt qu'un tiret. Un décalage de 16 pixels du recalage qui coupait les cellules des pages nouveau-né. Les polices du spécimen sans lettres accentuées. Et côté WhatsApp : les webhooks qui n'arrivent que si le compte est abonné à l'app, la fenêtre de 24 heures, les numéros que WhatsApp écrit autrement que l'annuaire.

EN : Getting good reading with no external service at all. Teaching the model to read "nothing" on a noisy empty cell instead of a dash. A 16-pixel registration shift that cut the cells of newborn pages. Specimen fonts with no accented letters. And on the WhatsApp side: webhooks that only arrive once the account is subscribed to the app, the 24-hour window, numbers WhatsApp writes differently from the phone book.

### Ce dont on est fiers (Accomplishments)

FR : 98.7 % des valeurs et une calibration ECE de 0.012 sur les 80 pages, avec un modèle de 8 Mo entraîné par nous. Un agent qui ne cache jamais ses doutes et refuse de garder un champ comme sûr quand la mise en page lui est inconnue. Un hors ligne réel, pas simulé. Et de vrais téléphones qui parlent à l'agent dans WhatsApp.

EN : 98.7 % exact values and ECE 0.012 on the 80 pages, with an 8 MB model we trained ourselves. An agent that never hides its doubts and refuses to keep a field as certain when the layout is unknown. Real offline behaviour, not simulated. And real phones talking to the agent in WhatsApp.

### Ce qu'on a appris (What we learned)

FR : Partir du schéma rapporte plus que n'importe quel modèle. Une vérité terrain gratuite change tout. Et la confiance d'un modèle ne vaut que si on la mesure.

EN : Starting from the schema pays more than any model. Free ground truth changes everything. And a model's confidence is only worth something if you measure it.

### La suite (What's next)

FR : Un gabarit du vrai carnet au petit format et des photos annotées sur le terrain ; le contrôle qualité sur le téléphone avant l'envoi ; une interface bilingue ; un tableau de bord d'agrégats anonymisés pour l'usage épidémiologique ; l'écriture arabe.

EN : A template for the real small-format booklet and annotated field photos; quality check on the phone before sending; a bilingual interface; an anonymized aggregate dashboard for epidemiological use; Arabic handwriting.

### Technologies (Built with)

python, opencv, pytorch, onnxruntime, fastapi, sqlite, cryptography, whatsapp-cloud-api, twilio, pdfplumber, fonttools, tesseract

### Liens

- Dépôt : https://github.com/nguefackuriel/PregnancyBot
- Vidéo : (à compléter)

## 4. Liste de contrôle avant d'envoyer

- [ ] Le dépôt est public, le README s'affiche bien, les liens internes marchent.
- [ ] `pip install -r requirements.txt` puis `pytest -q` passent sur une machine propre.
- [ ] Le lien de la vidéo est dans le README et sur Devpost.
- [ ] La vidéo montre les 4 moments demandés : capture hors ligne, retour de la connexion, révision d'un champ incertain, décision de correspondance.
- [ ] Les textes Devpost sont collés (FR, et EN si le formulaire le demande).
- [ ] Les captures d'écran : conversation WhatsApp avec les boutons, résumé de page avec doutes, question avec recadrage, choix de correspondance, `/health` avec le réseau coupé puis rétabli.
- [ ] Le `.env` n'est pas dans le dépôt (vérifier `git log --all -- .env` vide).
- [ ] Les vraies photos du défi ne sont pas dans le dépôt.
- [ ] Les membres de l'équipe sont ajoutés sur Devpost.

## 5. Script de la vidéo (2 min 30)

Format : écran partagé, le téléphone à gauche (enregistrement d'écran ou caméra), le
terminal du serveur à droite. Voix calme, phrases courtes. Pas de musique forte.

**0:00 à 0:20, le problème.** Une main pose le carnet rose sur une table. Voix : « Au
Maroc, le suivi de grossesse se fait sur ce carnet. Les sages-femmes ont déjà
WhatsApp. On a construit PregnancyBot : une photo, et le dossier existe. L'IA tourne
en local, rien ne sort du centre de santé. »

**0:20 à 0:50, capture hors ligne et retour du réseau.** Mode avion sur le téléphone.
On écrit « bonjour », on envoie la photo d'une page : WhatsApp la laisse en attente.
Voix : « Pas de réseau. La photo reste sur le téléphone. » On coupe le mode avion : la
photo part, le terminal montre le webhook, et l'agent répond : « Votre photo de 10h02
vient d'arriver, je la lis maintenant ; sa date de capture reste 10h02. » Puis le
résumé de la page : champs lus, vides, non applicables, doutes.

**0:50 à 1:30, révision d'un champ incertain.** L'agent pose son premier doute avec
le recadrage de la cellule : « Est-ce bien 12/05/2026 ? » Oui. Deuxième doute : la
sage-femme corrige en tapant « 3 = 64 kg ». Troisième : « Je ne sais pas » : le champ
passe en inconnu, pas inventé. Voix : « L'agent dit ce dont il doute. Il ne cache rien,
il n'invente rien. » Validation de la page.

**1:30 à 2:00, décision de correspondance.** L'agent demande le code patiente ; on
répond « aucun » : il crée un profil et donne un code à écrire sur le carnet. Deuxième
carnet, autre page, même type de signaux : l'agent montre « Patiente 1, Patiente 2,
Aucune, Je ne sais pas ». On choisit Patiente 1. Voix : « Il ne crée jamais un doublon
tout seul. »

**2:00 à 2:20, le serveur coupé.** On coupe le wifi de l'ordinateur ; un testeur envoie
une photo ; le terminal affiche « réseau coupé », `/health` montre la boîte d'entrée à
1. On remet le wifi : « réseau rétabli », « rattrapage », la réponse part. Voix :
« Rien n'est perdu, jamais. »

**2:20 à 2:30, fin.** Le README avec les chiffres : 98.7 % des valeurs, ECE 0.012,
modèle de 8 Mo, 34 tests. « PregnancyBot, par Side Quest. »

Conseils : préparer les deux carnets avant, écrire le code patiente au crayon pour
pouvoir refaire la prise, afficher le terminal en gros caractères, couper les
notifications du téléphone.
