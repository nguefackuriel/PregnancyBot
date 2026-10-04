# Défi DayOne (CodeML) : analyse du dossier participant et stratégie

*Analyse du 3 octobre 2026, à partir de `consignes-fr-en.pdf`, `manifest.json` et `data/` (129 images, 1 PDF, 1 CSV). Mise à jour le même jour avec le prototype livré.*

## 1. Le défi en une phrase

Prototyper un agent WhatsApp **offline-first** qui photographie le carnet rose marocain (« Fiche de surveillance de la grossesse et du post-partum », Ministère de la Santé), en extrait des champs structurés avec **statut + confiance par champ**, fait vérifier la sage-femme (Confirmer / Corriger / Reprendre la photo / saisie manuelle) et relie les visites d'une même femme par un code. Hors périmètre : tout ce qui est clinique (risque, triage, diagnostic).

Grille (100 pts) : extraction 30 · incertitude 20 · flux de vérification 20 · offline 15 · liaison patiente & confidentialité 10 · code/doc 5. **50 % des points sont sur l'extraction et la gestion de l'incertitude.**

## 2. Ce que contient vraiment le dossier

### 2.1 Inventaire

| Élément | Réalité |
|---|---|
| « 129 images » | 85 fichiers uniques. 44 PNG sont des **doublons exacts** renommés avec un suffixe aléatoire (`-07__1O_M-H0E7z.png` ≡ `-07__1qaWGv8qLJ.png`, même SHA-256). Dédupliquer par hash dès le départ. |
| 80 PNG `dossiers_specimen_10_patientes-NN.png` | Rasterisation (1654×2339, ~200 dpi) des 80 pages du PDF. **Propres** : pas de flou, d'ombre ni d'inclinaison. 10 patientes fictives × 8 pages. |
| `dossiers_specimen_10_patientes.pdf` | Généré avec ReportLab. **Garde sa couche vectorielle** → vérité terrain extractible automatiquement (voir §3). |
| 5 JPG `1-1.jpg` … `1-5.jpg` (900×1600) | **Vraies photos de téléphone** d'un vrai carnet rempli à la main, nom masqué avec un papier. Le seul matériel « terrain ». |
| `maternal_registry_synthetic.csv` (200 lignes, 31 colonnes) | Jeu tabulaire de type cohorte (BMI, glycémie, prématurité, référence…). **Ne correspond pas aux 10 patientes du PDF** : ce n'est pas la vérité terrain des images. Utile pour alimenter un générateur en valeurs réalistes et pour le bonus « tableau de bord ». |
| `manifest.json` | Liste des fichiers + SHA-256. Rien d'autre. |

### 2.2 Les 8 pages d'un dossier

1. Couverture (n° de fiche, région, province, établissement, type, mode de couverture, nom, grossesse à risque)
2. Identification et antécédents (âge, CIN, instruction, adresse, téléphone, mari ; antécédents familiaux ; antécédents de la femme ; anomalies des grossesses antérieures ; 5 accouchements antérieurs ; gestité/parité/vivants ; VAT ; rubéole ; hépatite B ; frottis)
3. Grossesse actuelle (DDR, taille, groupage, DPA ; tableau 9 colonnes × ~30 lignes : visites, examen clinique, examen biologique, traitement, examinateur)
4. Déroulement de l'accouchement (lieu, date, mode, complications, état du nouveau-né, sexe, poids, PC, anomalie, âge gestationnel)
5. Post-partum précoce, mère. 6. Post-partum précoce, nouveau-né.
7. Post-partum tardif, mère. 8. Post-partum tardif, nouveau-né.

### 2.3 Le synthétique : comment il est fabriqué, et ses pièges

- Valeurs « manuscrites » = vrai texte dans **5 polices Google** (Caveat, Shadows Into Light, Nanum Pen, Gaegu, Reenie Beanie, 2 patientes chacune) ; libellés en Helvetica.
- Cases à cocher = carrés de 8 pt ; cochée = deux diagonales (⊠) **ou** trois hachures selon la patiente.
- Pages légèrement inclinées (jusqu'à ~0,3°).
- **Glyphes accentués absents** de plusieurs polices : l'image montre « P les » pour *Pâles*, « c phalique », « Coll ge », « Ferm » (*Fermé*), et le tiret (qui veut dire non applicable) est **invisible** dans 3 polices sur 5 → la cellule paraît vide.
- Jitter d'espacement : « 1 55 c m », « 202 6-823-001 ».
- **Aucun arabe** dans le PDF (l'arabe n'est que sur l'en-tête imprimé du vrai carnet). L'arabe est un bonus, pas une priorité.
- Les tiers de choix (groupage, rhésus) sont des cases dans le synthétique mais des **mentions encerclées** sur le vrai carnet.

### 2.4 Les vraies photos : ce que le terrain ajoute

- Abréviations métier : `RAS`, `NF` (non fait), `IG` (primigeste), `16SA+3j`, `Reçu`, `nég`, `Colorées`.
- `RAS` écrit **en diagonale sur plusieurs cellules** ; traits barrant des colonnes entières.
- Le tableau « grossesse actuelle » est **à cheval sur deux pages** : la photo de droite (`1-5.jpg`) n'a aucun libellé de ligne, il faut aligner par la géométrie du gabarit.
- Notes en marge (« test rapide » pour VIH, « HCV : nég » dans la ligne Autres).
- Qualité : flou, ombre, inclinaison, papier qui gondole, doigt dans le cadre.
- PII visibles à masquer : CIN, adresse, téléphone, nom du mari (le nom est déjà caché).

## 3. La découverte clé : la vérité terrain est dans le PDF

Avec `pdfplumber` on lit, page par page : les mots manuscrits (police ≠ Helvetica) avec leur boîte, les lignes de tableau, les cases (courbes 8×8 pt) et les marques dedans. `tools/auto_label_pdf.py` en tire pour les 80 pages :

- **2 110 valeurs** rattachées à une clé canonique (0 non mappée), **1 970 cases** (478 cochées), toutes avec bbox en points PDF et en pixels PNG ;
- les cellules vides complétées par le schéma : `NON_FOURNI` ou `NON_APPLICABLE` (règles de dépendance : parité, césarienne, rhésus…) ;
- un drapeau `rendered: false` + `status_on_image` quand le générateur voulait un tiret mais que l'image ne montre rien ;
- la réparation des glyphes manquants (« Ferm » → « Fermé ») pour que la vérité terrain porte la valeur voulue.

Conséquences :
1. **Zéro annotation manuelle** : un jeu d'évaluation complet, par champ, par statut, par police, tout de suite.
2. Comme on connaît le générateur (ReportLab + polices Google + gabarit), on peut le **reconstruire** et produire des milliers de pages avec d'autres valeurs (tirées du CSV), d'autres polices manuscrites, et des **dégradations** (flou, ombre, inclinaison, faible lumière, JPEG). Le jeu de test du jury vient très probablement du même générateur.
3. Les bbox permettent d'entraîner/évaluer un détecteur de cellules et de recadrer la cellule douteuse dans la question de suivi WhatsApp.

## 4. Ce qui fait gagner

1. **Partir du schéma** (`schema/registry_schema.json`, 436 champs + 69 groupes de cases, 6 statuts, règles N/A, 13 contrôles de cohérence). C'est le conseil n°1 des organisateurs et c'est ce qui rapporte les points « statuts corrects ».
2. **Pipeline** : type de page → recalage sur le gabarit (homographie via les lignes) → découpe par cellule → lecture par VLM avec sortie structurée → normalisation par vocabulaire fermé (corrige « P les ») → règles de cohérence → statut + confiance. Les bbox = « reprendre la photo » ciblé et crop montré à la sage-femme.
3. **Confiance calibrée, pas inventée** : accord entre deux lectures × score vocabulaire × règles ; mesurer l'ECE avec `tools/evaluate.py`. Un harnais d'évaluation dans le README vaut autant que le modèle.
4. **Tester sur les deux mondes** : PNG auto-dégradés (le plus probable au jury) et les 5 vraies photos (la crédibilité terrain). Prévoir les codes de statut manuscrits (`NF`, `IG`, tiret, barré).
5. **Contrainte « aucune donnée réelle vers un tiers »** : données synthétiques → API cloud possible ; prévoir un fallback local (petit VLM open-source) et le mode « IA indisponible → saisie manuelle ».
6. **Flux et offline** : machine à états explicite (8 états + 4 échecs, dans `SCHEMA.md` §6), file locale chiffrée, démo en coupant le réseau en plein milieu. Simulateur WhatsApp web suffit ; le sandbox Meta/Twilio est un bonus.
7. **Liaison patiente** : code sage-femme (le n° de fiche peut jouer ce rôle) + rapprochement flou sur champs non identifiants (établissement, âge, G/P, DDR, DPA) → candidats → la sage-femme tranche. Masquage PII sur l'image **avant** stockage.

## 5. Risques

- Jeu de test du jury inconnu : synthétique propre, dégradé, ou vraies photos. La stratégie « générateur reconstruit + dégradation + 5 vraies photos » couvre les trois.
- Le vrai carnet a quelques lignes de plus que le synthétique (« Autres » en bio, « Autres à préciser » en traitement, « Autres à préciser » dans les antécédents familiaux) : elles sont dans le schéma, pas dans la vérité terrain.
- Sessions multipages (2 photos = 1 tableau) : à intégrer au flux, pas seulement au schéma.

## 6. Contenu du kit (`analysis/`)

```
ANALYSE.md                     ce document
README.md                      comment lancer
schema/registry_schema.json    schéma de champs + statuts + cycle de vie (machine)
schema/SCHEMA.md               le même, lisible
schema/build_schema.py         générateur du schéma (modifier ici, pas le JSON)
tools/auto_label_pdf.py        vérité terrain automatique depuis le PDF
tools/evaluate.py              harnais d'évaluation (valeur, statut, cases, ECE)
ground_truth/ground_truth.json 80 pages labellisées (2,7 Mo)
ground_truth/pages/*.json      une page par fichier, même nom que le PNG
```

## 7. Prototype livré (`dayone-participants/dayone-agent/`), 3 octobre 2026

Le détail est dans `claude/dayone-agent-readme.md`. En bref :

- **Lecture 100 % locale, sans clé.** Le lecteur par défaut est un petit modèle de reconnaissance d'écriture (CRNN + CTC, 2 millions de paramètres, 8 Mo en ONNX) entraîné sur les cellules du carnet : cellules synthétiques rendues avec les 5 polices extraites du PDF, cellules réelles des 80 pages, et les mêmes cellules découpées sur des photos dégradées après recalage. Il lit une page en 2 à 4 secondes sur CPU. Rien ne sort de l'appareil. Un lecteur Ollama (modèle vision local) est aussi branché pour les vraies écritures plus variées.
- **Scores du modèle livré** (patientes 9 et 10 jamais vues à l'entraînement). 80 pages propres : type de page 100 %, valeurs 98.7 % avec lexique (96.0 % sans), statuts 98.7 %, cases 99.1 %, calibration ECE 0.012. Photos façon téléphone : niveau léger 97.7 % des valeurs, niveau moyen 81.9 % (statuts 93.8 %, et quand le modèle dit CONNU il a raison 9 fois sur 10), niveau fort refusé par le contrôle qualité.
- **Ce qui a fait la différence**, dans l'ordre : entraîner sur les 80 pages (74.9 % vers 92.6 %), recaler axe par axe (93.8 %), lire ligne par ligne les grandes zones de texte (95.2 %), rapprocher du vocabulaire du carnet (98.4 %), entraîner sur photos dégradées (niveau moyen 74 % vers 82 %). Et une règle simple : un tiret est un trait court et compact, le reste est du bruit.
- **Couche géométrique** (lecteur simulé qui renvoie la vérité terrain) : 80 pages propres, type de page 100 %, valeurs 99.7 %, statuts 99.2 %, cases 99.1 %.
- **Couche agent** : stockage chiffré + file hors ligne + machine à états (8 états, 4 échecs), conversation Confirmer / Corriger / Reprendre / Je ne sais pas avec recadrage de la cellule, saisie manuelle si l'IA échoue, sessions multipages, renumérisation, liaison patiente par code et signaux non identifiants, identifiants masqués avant lecture et jamais stockés.
- **Livrables** : simulateur WhatsApp web, adaptateur WhatsApp Cloud API, démo scriptée, 28 tests, générateur de jeu de test dégradé, harnais d'évaluation, scripts d'entraînement et d'export ONNX, lexique du carnet.
- **Reste à faire** : de vraies photos annotées pour réentraîner (le modèle n'a vu que l'écriture synthétique) ; tester le sandbox WhatsApp ; gabarit du vrai carnet au petit format ; bonus tableau de bord d'agrégats à partir du CSV.
