# Schéma de champs et modèle de statuts, Carnet maternel (DayOne)

Généré par `build_schema.py`. Source : `registry_schema.json` (même contenu, lisible par machine). Les clés sont celles de `ground_truth/ground_truth.json`.

## 1. Statuts de champ

| Statut | Sens |
|---|---|
| `CONNU` | Valeur lue avec une confiance >= seuil_connu (0.85 par défaut). Montrée à la sage-femme pour confirmation groupée. |
| `A_REVISER` | Valeur lue mais confiance entre seuil_revision (0.5) et seuil_connu, OU incohérence détectée par une règle (ex. parité > gestité), OU désaccord entre deux lectures. L'agent pose une question ciblée avec le recadrage de la cellule. |
| `ILLISIBLE` | De l'encre est présente dans la zone mais aucune lecture n'atteint seuil_revision. L'agent propose : reprendre la photo / saisir à la main. |
| `NON_FOURNI` | La zone est vide sur la page photographiée (la page a bien été capturée). Rien n'a été écrit par la sage-femme. |
| `NON_APPLICABLE` | Le champ ne s'applique pas : tiret/barré explicite sur le papier, ou règle de dépendance (ex. indication de césarienne si voie basse ; RAI si Rhésus positif ; Accouch. 3 si parité = 2). |
| `INCONNU` | On ne sait pas : la page/section n'a pas (encore) été photographiée dans cette session, l'IA est indisponible, ou la sage-femme a répondu « je ne sais pas ». |

Règles :

- Un champ a toujours exactement un statut ; `value` n'est renseigné que pour CONNU et A_REVISER (et pour ILLISIBLE si une lecture partielle existe, marquée `partial: true`).
- `confidence` est un réel [0,1] obligatoire pour CONNU / A_REVISER / ILLISIBLE ; il doit être calibré (voir SCHEMA.md §Confiance).
- NON_FOURNI ≠ NON_APPLICABLE ≠ INCONNU : un simple « N/A » est interdit (consigne du défi).
- Une valeur hors vocabulaire fermé (enum) passe automatiquement en A_REVISER avec la meilleure correspondance proposée.
- Toute réponse de la sage-femme (confirmation, correction, saisie manuelle) force CONNU avec confidence = 1.0 et `source = midwife`.
- Les champs PII (pii = true) sont détectés pour être masqués sur l'image puis supprimés : ils ne sont JAMAIS stockés (statut interne REDACTED, jamais exporté).

### Codes manuscrits qui portent un statut

| Écrit sur le papier | Statut | Note |
|---|---|---|
| `NF` | NON_FOURNI | « non fait » : l'examen n'a pas été réalisé |
| `—` | NON_APPLICABLE | tiret explicite |
| `/` | NON_APPLICABLE | cellule barrée |
| `IG` | CONNU | « primigeste » écrit en travers des antécédents obstétricaux → gestation = 1, parité = 0, accouchements antérieurs = NON_APPLICABLE |

## 2. Format d'un champ extrait

```json
{
  "value": "string | number | null, valeur normalisée selon `type`",
  "raw": "string | null, texte tel que lu avant normalisation",
  "status": "CONNU | A_REVISER | ILLISIBLE | NON_FOURNI | NON_APPLICABLE | INCONNU",
  "confidence": "float [0,1] | null",
  "source": "ai | midwife | rule, qui a fixé la valeur/le statut",
  "bbox_px": "[x0, y0, x1, y1] dans l'image d'origine | null, permet le recadrage pour la question de suivi",
  "page_id": "identifiant de la capture d'où vient la valeur",
  "notes": "string | null, ex. 'NF écrit sur le papier', 'IG = primigeste'"
}
```

### Confiance

- `confidence` doit être **calibrée** : parmi les champs annoncés à 0.9, ~90 % doivent être justes. Mesurer l'ECE sur le jeu de test.
- Recette simple et robuste : accord entre deux lectures indépendantes (deux prompts / deux modèles / deux recadrages) × score de correspondance au vocabulaire fermé × pénalité des règles de cohérence.
- Seuils par défaut : `CONNU` ≥ 0.85 ; `A_REVISER` ∈ [0.5, 0.85) ; `ILLISIBLE` < 0.5 avec encre détectée.

## 3. Types et normalisation

| Type | Détail |
|---|---|
| `date` | {"format": "JJ/MM/AAAA", "accepte": ["JJ/MM/AA", "JJ-MM-AAAA", "JJ.MM.AAAA"], "regex": "^\\d{2}/\\d{2}/\\d{4}$"} |
| `int` | {"regex": "^\\d+$"} |
| `float` | {"regex": "^\\d+([.,]\\d+)?$", "note": "virgule décimale acceptée, stockée avec un point"} |
| `text` | {} |
| `enum` | {"note": "vocabulaire fermé ; correspondance floue (accents/casse/abréviations) puis A_REVISER si distance > 2"} |
| `bool` | {"note": "case cochée / Oui-Non"} |
| `ta` | {"format": "SYS/DIA en mmHg", "regex": "^\\d{2,3}/\\d{2,3}$", "accepte": ["12/7 (cmHg → ×10)", "120/70"]} |
| `age_gestationnel` | {"format": "SA[+j]", "regex": "^\\d{1,2}\\s*SA(\\s*\\+\\s*\\d\\s*j)?$", "accepte": ["16SA+3j", "16 SA", "16 sem"]} |
| `poids_g` | {"unit": "g", "accepte": ["3,5 kg → 3500"]} |
| `poids_kg` | {"unit": "kg"} |
| `longueur_cm` | {"unit": "cm"} |
| `temperature` | {"unit": "°C", "regex": "^\\d{2}([.,]\\d)?$"} |

Alias (forme canonique ← variantes vues sur le papier) :

- **RAS** ← RAS, R.A.S, Ras, rien à signaler, Aucun, Aucune, Néant, 0
- **Neg** ← Neg, Nég, Négatif, négative, (-), -
- **Pos** ← Pos, Positif, positive, (+), +
- **Oui** ← Oui, oui, O, ✓, ✗ (dans une case), Reçu
- **Non** ← Non, non, N, 0
- **Normales** ← Normales, Normal, Nles, colorées, bien colorées
- **Pâles** ← Pâles, Pales, P les, décolorées
- **Fermé** ← Fermé, Ferm, fermé, long fermé, LF
- **Céphalique** ← Céphalique, C phalique, céph, sommet
- **Voie basse** ← Voie basse, VB, AVB, accouchement voie basse
- **Césarienne** ← Césarienne, César, C/S, CS
- **Immune** ← Immune, immunisée, +
- **Non immune** ← Non immune, non immunisée, -

## 4. Pages et champs

### `COUVERTURE`, Fiche de surveillance de la grossesse et du post-partum (couverture)

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `numero_fiche` | N° de la fiche | text | peut servir de code de liaison si la sage-femme l'utilise ainsi |
| `region` | Région | enum | valeurs : Tanger-Tétouan-Al Hoceïma / Oriental / Fès-Meknès / Rabat-Salé-Kénitra / Béni Mellal-Khénifra / Casablanca-Settat / Marrakech-Safi / Drâa-Tafilalet / Souss-Massa / Guelmim-Oued Noun / Laâyoune-Sakia El Hamra / Dakhla-Oued Ed-Dahab |
| `province` | Province | text |  |
| `etablissement` | Nom de l'établissement sanitaire | text |  |
| `nom_prenom_parturiente` | Nom/Prénom de la parturiente | text | **PII, jamais stocké** |
| `risque_autres` | Autres à préciser (type de risque) | text |  |

Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `type_etablissement` | Type de l'établissement sanitaire | single | dr, csc, csu, csca, csua |  |
| `mode_couverture` | Mode de la couverture | single | fixe, mobile |  |
| `grossesse_a_risque` | Grossesse classée à risque | bool | oui |  |
| `type_risque` | Si grossesse à risque, type de risque | multi | anemie, hta, diabete, cardiopathie, metrorragie, infection, pre_eclampsie, eclampsie | dépend de `grossesse_a_risque == oui` |

### `IDENTIFICATION_ANTECEDENTS`, Identification et antécédents

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `age` | Age | int | plage [12, 55] |
| `cin` | CIN | text | **PII, jamais stocké** |
| `niveau_instruction` | Niveau d'instruction | enum | valeurs : Aucun / Primaire / Collège / Lycée / Supérieur |
| `profession` | Profession | text |  |
| `adresse` | Adresse | text | **PII, jamais stocké** |
| `telephone` | Téléphone | text | **PII, jamais stocké** |
| `nom_mari` | Nom du Mari | text | **PII, jamais stocké** |
| `profession_mari` | Profession (du mari) | text |  |
| `antecedents_familiaux.hta.famille_femme` | Antécédents hérédit. et familiaux, hta, famille_femme | text | alias RAS |
| `antecedents_familiaux.hta.famille_mari` | Antécédents hérédit. et familiaux, hta, famille_mari | text | alias RAS |
| `antecedents_familiaux.diabete.famille_femme` | Antécédents hérédit. et familiaux, diabete, famille_femme | text | alias RAS |
| `antecedents_familiaux.diabete.famille_mari` | Antécédents hérédit. et familiaux, diabete, famille_mari | text | alias RAS |
| `antecedents_familiaux.maladies_hereditaires.famille_femme` | Antécédents hérédit. et familiaux, maladies_hereditaires, famille_femme | text | alias RAS |
| `antecedents_familiaux.maladies_hereditaires.famille_mari` | Antécédents hérédit. et familiaux, maladies_hereditaires, famille_mari | text | alias RAS |
| `antecedents_familiaux.malformations.famille_femme` | Antécédents hérédit. et familiaux, malformations, famille_femme | text | alias RAS |
| `antecedents_familiaux.malformations.famille_mari` | Antécédents hérédit. et familiaux, malformations, famille_mari | text | alias RAS |
| `antecedents_familiaux.allergies.famille_femme` | Antécédents hérédit. et familiaux, allergies, famille_femme | text | alias RAS |
| `antecedents_familiaux.allergies.famille_mari` | Antécédents hérédit. et familiaux, allergies, famille_mari | text | alias RAS |
| `antecedents_familiaux.autres.famille_femme` | Antécédents hérédit. et familiaux, autres, famille_femme | text | alias RAS |
| `antecedents_familiaux.autres.famille_mari` | Antécédents hérédit. et familiaux, autres, famille_mari | text | alias RAS |
| `antecedents_femme.medicaux` | Antécédents de la femme, medicaux | text | alias RAS |
| `antecedents_femme.chirurgicaux` | Antécédents de la femme, chirurgicaux | text | alias RAS |
| `antecedents_femme.gynecologiques` | Antécédents de la femme, gynecologiques | text | alias RAS |
| `antecedents_obstetricaux.avortement.nombre` | Anomalies des grossesses antérieures, avortement, nombre | int |  |
| `antecedents_obstetricaux.avortement.date` | Anomalies des grossesses antérieures, avortement, date | date |  |
| `antecedents_obstetricaux.avortement.lieu` | Anomalies des grossesses antérieures, avortement, lieu | text |  |
| `antecedents_obstetricaux.avortement.age_gestationnel_sa` | Anomalies des grossesses antérieures, avortement, age_gestationnel_sa | age_gestationnel |  |
| `antecedents_obstetricaux.accouchement_premature.nombre` | Anomalies des grossesses antérieures, accouchement_premature, nombre | int |  |
| `antecedents_obstetricaux.accouchement_premature.date` | Anomalies des grossesses antérieures, accouchement_premature, date | date |  |
| `antecedents_obstetricaux.accouchement_premature.lieu` | Anomalies des grossesses antérieures, accouchement_premature, lieu | text |  |
| `antecedents_obstetricaux.accouchement_premature.age_gestationnel_sa` | Anomalies des grossesses antérieures, accouchement_premature, age_gestationnel_sa | age_gestationnel |  |
| `antecedents_obstetricaux.mort_foetale_in_utero.nombre` | Anomalies des grossesses antérieures, mort_foetale_in_utero, nombre | int |  |
| `antecedents_obstetricaux.mort_foetale_in_utero.date` | Anomalies des grossesses antérieures, mort_foetale_in_utero, date | date |  |
| `antecedents_obstetricaux.mort_foetale_in_utero.lieu` | Anomalies des grossesses antérieures, mort_foetale_in_utero, lieu | text |  |
| `antecedents_obstetricaux.mort_foetale_in_utero.age_gestationnel_sa` | Anomalies des grossesses antérieures, mort_foetale_in_utero, age_gestationnel_sa | age_gestationnel |  |
| `antecedents_obstetricaux.autres.nombre` | Anomalies des grossesses antérieures, autres, nombre | int |  |
| `antecedents_obstetricaux.autres.date` | Anomalies des grossesses antérieures, autres, date | date |  |
| `antecedents_obstetricaux.autres.lieu` | Anomalies des grossesses antérieures, autres, lieu | text |  |
| `antecedents_obstetricaux.autres.age_gestationnel_sa` | Anomalies des grossesses antérieures, autres, age_gestationnel_sa | age_gestationnel |  |
| `accouchements_anterieurs.1.date` | Accouchement antérieur 1, date | date | NON_APPLICABLE si `parite < 1` |
| `accouchements_anterieurs.1.modalite` | Accouchement antérieur 1, modalite | enum | valeurs : Voie basse / Césarienne / Forceps / Ventouse ; NON_APPLICABLE si `parite < 1` |
| `accouchements_anterieurs.1.indication_cesarienne` | Accouchement antérieur 1, indication_cesarienne | text | NON_APPLICABLE si `parite < 1 or accouchements_anterieurs.1.modalite != 'Césarienne'` |
| `accouchements_anterieurs.1.complication` | Accouchement antérieur 1, complication | text | NON_APPLICABLE si `parite < 1` |
| `accouchements_anterieurs.1.poids_nn_g` | Accouchement antérieur 1, poids_nn_g | poids_g | NON_APPLICABLE si `parite < 1` |
| `accouchements_anterieurs.1.complication_nn` | Accouchement antérieur 1, complication_nn | text | NON_APPLICABLE si `parite < 1` |
| `accouchements_anterieurs.2.date` | Accouchement antérieur 2, date | date | NON_APPLICABLE si `parite < 2` |
| `accouchements_anterieurs.2.modalite` | Accouchement antérieur 2, modalite | enum | valeurs : Voie basse / Césarienne / Forceps / Ventouse ; NON_APPLICABLE si `parite < 2` |
| `accouchements_anterieurs.2.indication_cesarienne` | Accouchement antérieur 2, indication_cesarienne | text | NON_APPLICABLE si `parite < 2 or accouchements_anterieurs.2.modalite != 'Césarienne'` |
| `accouchements_anterieurs.2.complication` | Accouchement antérieur 2, complication | text | NON_APPLICABLE si `parite < 2` |
| `accouchements_anterieurs.2.poids_nn_g` | Accouchement antérieur 2, poids_nn_g | poids_g | NON_APPLICABLE si `parite < 2` |
| `accouchements_anterieurs.2.complication_nn` | Accouchement antérieur 2, complication_nn | text | NON_APPLICABLE si `parite < 2` |
| `accouchements_anterieurs.3.date` | Accouchement antérieur 3, date | date | NON_APPLICABLE si `parite < 3` |
| `accouchements_anterieurs.3.modalite` | Accouchement antérieur 3, modalite | enum | valeurs : Voie basse / Césarienne / Forceps / Ventouse ; NON_APPLICABLE si `parite < 3` |
| `accouchements_anterieurs.3.indication_cesarienne` | Accouchement antérieur 3, indication_cesarienne | text | NON_APPLICABLE si `parite < 3 or accouchements_anterieurs.3.modalite != 'Césarienne'` |
| `accouchements_anterieurs.3.complication` | Accouchement antérieur 3, complication | text | NON_APPLICABLE si `parite < 3` |
| `accouchements_anterieurs.3.poids_nn_g` | Accouchement antérieur 3, poids_nn_g | poids_g | NON_APPLICABLE si `parite < 3` |
| `accouchements_anterieurs.3.complication_nn` | Accouchement antérieur 3, complication_nn | text | NON_APPLICABLE si `parite < 3` |
| `accouchements_anterieurs.4.date` | Accouchement antérieur 4, date | date | NON_APPLICABLE si `parite < 4` |
| `accouchements_anterieurs.4.modalite` | Accouchement antérieur 4, modalite | enum | valeurs : Voie basse / Césarienne / Forceps / Ventouse ; NON_APPLICABLE si `parite < 4` |
| `accouchements_anterieurs.4.indication_cesarienne` | Accouchement antérieur 4, indication_cesarienne | text | NON_APPLICABLE si `parite < 4 or accouchements_anterieurs.4.modalite != 'Césarienne'` |
| `accouchements_anterieurs.4.complication` | Accouchement antérieur 4, complication | text | NON_APPLICABLE si `parite < 4` |
| `accouchements_anterieurs.4.poids_nn_g` | Accouchement antérieur 4, poids_nn_g | poids_g | NON_APPLICABLE si `parite < 4` |
| `accouchements_anterieurs.4.complication_nn` | Accouchement antérieur 4, complication_nn | text | NON_APPLICABLE si `parite < 4` |
| `accouchements_anterieurs.5.date` | Accouchement antérieur 5, date | date | NON_APPLICABLE si `parite < 5` |
| `accouchements_anterieurs.5.modalite` | Accouchement antérieur 5, modalite | enum | valeurs : Voie basse / Césarienne / Forceps / Ventouse ; NON_APPLICABLE si `parite < 5` |
| `accouchements_anterieurs.5.indication_cesarienne` | Accouchement antérieur 5, indication_cesarienne | text | NON_APPLICABLE si `parite < 5 or accouchements_anterieurs.5.modalite != 'Césarienne'` |
| `accouchements_anterieurs.5.complication` | Accouchement antérieur 5, complication | text | NON_APPLICABLE si `parite < 5` |
| `accouchements_anterieurs.5.poids_nn_g` | Accouchement antérieur 5, poids_nn_g | poids_g | NON_APPLICABLE si `parite < 5` |
| `accouchements_anterieurs.5.complication_nn` | Accouchement antérieur 5, complication_nn | text | NON_APPLICABLE si `parite < 5` |
| `gestation` | Gestation (gestité) | int | plage [1, 15] ; « IG » = 1 |
| `parite` | Parité | int | plage [0, 15] ; contrôle : parite <= gestation |
| `enfants_vivants` | Nombre d'enfants vivants | int | plage [0, 15] ; contrôle : enfants_vivants <= parite |
| `date_vaccin_rubeole` | Vaccinée contre la rubéole, Le | date | NON_APPLICABLE si `vaccin_rubeole != oui` |
| `date_vaccin_hepatite_b` | Vaccinée contre l'hépatite B, Le | date | NON_APPLICABLE si `vaccin_hepatite_b != oui` |
| `frottis_iva` | Frottis cervical / IVA (moins de 3 ans) | enum | valeurs : Normal / Anormal / Non fait |

Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `consanguinite` | Consanguinité | bool | oui |  |
| `grossesse_desiree` | Grossesse désirée | bool | oui |  |
| `vat` | VAT (doses) | multi | 1, 2, 3, 4, 5 | sur le vrai carnet : cases cochées cumulatives |
| `vaccin_rubeole` | Vaccinée contre la rubéole | bool | oui |  |
| `vaccin_hepatite_b` | Vaccinée contre l'hépatite B | bool | oui |  |

### `GROSSESSE_ACTUELLE`, Grossesse actuelle (tableau longitudinal, 9 colonnes de visite)

> Sur le vrai carnet ce tableau est à cheval sur deux pages : les libellés de ligne ne sont que sur la page de gauche ; la page de droite (2e/3e trimestre) s'aligne par la géométrie du gabarit.

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `ddr` | DDR (date des dernières règles) | date |  |
| `taille_cm` | Taille | longueur_cm | plage [130, 200] |
| `dpa` | Date prévue d'accouchement | date | contrôle : dpa ≈ ddr + 280 j (±7) |
| `date_depassement_terme` | Date de dépassement de terme | date | contrôle : ≈ dpa + 7 j |

Tableau des visites : clé = `visites.<colonne>.<ligne>` avec colonnes `t1_v1`, `t1_v2`, `t1_v3`, `t2_v1`, `t2_v2`, `t2_v3`, `t3_m7`, `t3_m8`, `t3_m9` et lignes :

| Ligne | Libellé | Type | Cardinalité |
|---|---|---|---|
| `rendez_vous` | Rendez-vous | date | per_visit |
| `venue_le` | Venue le | date | per_visit |
| `visite_relance` | Visites de relance | enum:Oui|Non | per_visit |
| `age_gestationnel` | Age probable de la grossesse | age_gestationnel | per_visit |
| `poids_kg` | Poids (kg) | poids_kg | per_visit |
| `ta` | TA | ta | per_visit |
| `anomalies_squelette` | Anomalies du squelette | text | per_visit |
| `conjonctives` | État des conjonctives | enum:Normales|Pâles|Décolorées | per_visit |
| `seins` | Examen des seins | enum:Normaux|Anormaux | per_visit |
| `oedemes` | Œdèmes | enum:Oui|Non | per_visit |
| `mouvements_actifs` | Mouvements actifs | enum:Oui|Non | per_visit |
| `hu_cm` | HU (cm) | longueur_cm | per_visit |
| `bcf` | BCF | int | per_visit |
| `speculum` | Examen au spéculum | text | per_visit |
| `tv_col` | TV : état du col | enum:Fermé|Ouvert|Long fermé|Court|Effacé | per_visit |
| `tv_presentation` | TV : présentation | enum:Céphalique|Siège|Transverse | per_visit |
| `tv_bassin` | TV : bassin | enum:Normal|Limite|Rétréci | per_visit |
| `glucosurie` | Glucosurie | enum:Neg|Pos | per_visit |
| `albuminurie` | Albuminurie | enum:Neg|Pos | per_visit |
| `rubeole` | Rubéole | enum:Immune|Non immune | any_visit |
| `toxoplasmose` | Toxoplasmose | enum:Immune|Non immune | any_visit |
| `syphilis` | Syphilis (TPHA/VDRL) | enum:Neg|Pos | any_visit |
| `ag_hbs` | Ag HBs | enum:Neg|Pos | any_visit |
| `vih` | Sérologie VIH | enum:Neg|Pos | any_visit |
| `hemoglobine` | Hémoglobine (g/dL) | float | any_visit |
| `plaquettes` | Plaquettes | text | any_visit |
| `glycemie` | Bilan glycémique (g/L) | float | any_visit |
| `rai` | RAI (si Rh négatif) | enum:Neg|Pos | any_visit |
| `autres_bio` | Autres (bio), vrai carnet uniquement | text | any_visit |
| `fer` | Traitement, Fer | enum:Oui|Non|Reçu | per_visit |
| `traitement_autres` | Traitement, Autres à préciser (vrai carnet) | text | per_visit |
| `examinateur` | Examen fait par | text | per_visit |

`per_visit` = une valeur attendue par visite ; `any_visit` = examen fait une (ou deux) fois, les autres colonnes sont NON_FOURNI, pas ILLISIBLE.


Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `groupage` | Groupage | single | A, B, O, AB | sur le vrai carnet : lettre ENCERCLÉE, pas de case |
| `rhesus` | Rhésus | single | negatif, positif | sur le vrai carnet : mention encerclée |

### `ACCOUCHEMENT`, Déroulement de l'accouchement

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `patiente_nom` | Patiente | text | **PII, jamais stocké** |
| `lieu_surveille_autres` | Lieu (milieu surveillé), Autres | text |  |
| `lieu_domicile_autres` | Lieu (à domicile), Autres | text |  |
| `date_accouchement` | Date de l'accouchement | date |  |
| `indication_cesarienne` | Préciser l'indication (césarienne) | text | NON_APPLICABLE si `mode not in (cesarienne_programmee, cesarienne_urgence)` |
| `complication_autres` | Si autres à préciser (complications) | text | NON_APPLICABLE si `'autres' not in complications_type` |
| `sexe` | Sexe | enum | valeurs : F / M |
| `poids_naissance_g` | Poids à la naissance | poids_g | plage [500, 6000] |
| `perimetre_cranien_cm` | Périmètre crânien à la naissance | longueur_cm | plage [25, 40] |
| `anomalie` | Anomalie à préciser | text | alias RAS |
| `age_gestationnel_sa` | Âge gestationnel | age_gestationnel | plage [22, 44] |

Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `lieu` | Lieu | single | milieu_surveille, domicile |  |
| `lieu_detail` | Lieu, détail | single | maison_accouchement, maternite, clinique_privee, domicile_assiste_qualifie | dépend de `lieu` |
| `mode` | Mode de l'accouchement | single | voie_basse_non_instrumentale, voie_basse_instrumentale, cesarienne_programmee, cesarienne_urgence |  |
| `mode_instrument` | Voie basse instrumentale, détail | multi | forceps, ventouse, episiotomie | dépend de `mode == voie_basse_instrumentale` |
| `complications` | Présence de complications | bool | presence |  |
| `complications_moment` | Moment des complications | multi | accouchement, suites_de_couches | dépend de `complications == presence` |
| `complications_type` | Type de complications | multi | pre_eclampsie, eclampsie, hemorragie, infection, autres | dépend de `complications == presence` |
| `etat_nouveau_ne` | Etat du nouveau-né | single | vivant, mort_ne, deces_moins_24h |  |

### `PP_PRECOCE_MERE`, Consultation du post-partum précoce, Mère

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `patiente_nom` | MÈRE, nom | text | **PII, jamais stocké** |
| `date_consultation` | Date de la consultation | date |  |
| `temperature` | T° | temperature | plage [35, 42] |
| `ta` | TA | ta |  |
| `pouls` | Pouls | int | plage [40, 180] |
| `poids_kg` | Poids | poids_kg |  |
| `cicatrice_cesarienne` | Césarienne : état de la cicatrice | text | NON_APPLICABLE si `cesarienne != oui` |
| `medicaments` | Notion de prise de médicaments (détail) | text |  |
| `traitement_autres` | Traitement prescrit, Autres à préciser | text |  |
| `prochain_rdv` | Prochain rendez-vous le | date |  |
| `pf_autre_methode` | Planification familiale, Autre à préciser | text |  |
| `pf_referee` | Référée (détail) | text |  |
| `pf_refus_raison` | Si la mère ne désire pas une méthode contraceptive : Pourquoi ? | text | NON_APPLICABLE si `'desire_methode' in pf` |

Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `periode` | Moment de la consultation | single | dans_fenetre, apres_fenetre | dans_fenetre = entre le 7ème et 8ème jour ; apres_fenetre = après le 8ème jour |
| `conjonctives` | Etat des conjonctives | single | normales, decolorees |  |
| `globe_uterin` | Présence du globe utérin | bool | present |  |
| `lochies_odeur` | Etat des lochies, odeur | single | fade, fetide |  |
| `lochies_aspect` | Etat des lochies, aspect | single | claires, sanglantes, jaunatres |  |
| `perinee` | Etat du périnée | multi | normal, episiotomie, episiotomie_reparee, dechirure |  |
| `sphincters` | Etat des sphincters (anal et urétral) | single | normal, anormal |  |
| `cesarienne` | Césarienne | bool | oui |  |
| `seins` | Etat des seins | single | normal, lymphangite, mastite_abces |  |
| `mollets` | Etat des mollets | multi | normal, rouges, chauds, douloureux_dorsiflexion |  |
| `complication` | Présence de complication | bool | presence |  |
| `complication_type` | Type de complication | multi | hemorragie, mammaires, infection, anemie, eclampsie, autres, phlebite | dépend de `complication == presence` |
| `medicaments` | Notion de prise de médicaments | bool | prise |  |
| `traitement` | Traitement prescrit | multi | fer, vitamine_a |  |
| `pf` | Planification familiale | multi | desire_methode, prescription_faite, referee |  |
| `pf_methode` | Si oui, laquelle | single | pilule, diu | dépend de `'desire_methode' in pf` |

### `PP_PRECOCE_NOUVEAU_NE`, Consultation du post-partum précoce, Nouveau-né

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `date_consultation` | Date de la consultation | date |  |
| `age_jours` | Age (jours) | int | plage [0, 90] |
| `temperature` | Température | temperature | plage [34, 42] |
| `poids_g` | Poids | poids_g | plage [500, 10000] |
| `taille_cm` | Taille | longueur_cm | plage [30, 70] |
| `perimetre_cranien_cm` | Périmètre crânien | longueur_cm | plage [25, 45] |
| `signes_graves_autres` | Signes d'une affection grave, Autres à préciser | text | alias RAS |
| `lesions_autres` | Contusions, lésions, malformations, Autres à préciser | text | alias RAS |
| `vu_par` | Vu par | text |  |
| `decision` | Décision prise | text |  |
| `traitement` | Traitement prescrit | text | alias RAS |
| `etablissement_reference` | Transfert, établissement de référence | text | NON_APPLICABLE si `transfert != oui` |
| `prochain_rdv` | Revenir pour une visite de suivi nécessaire le | date |  |

Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `etat` | Nouveau-né prématuré / hypotrophe | multi | premature, hypotrophe |  |
| `allaitement` | Allaitement | single | exclusif_sein, artificiel, mixte |  |
| `signes_graves` | Signes d'une affection grave | multi | convulsions, refus_teter, hematemeses, melaenas, diarrhee, ictere, tirage_sous_costal, toux, rythme_respiratoire_anormal, fievre, hypothermie |  |
| `lesions` | Contusions, lésions traumatiques et malformations | multi | bosse_serosanguine_cephalohematome, luxation_hanche, mobilite_membre_diminuee |  |
| `evaluation_allaitement` | Evaluation de l'allaitement maternel | single | normal, a_problemes |  |
| `vaccins` | Vaccins administrés ce jour | multi | bcg, hb |  |
| `vitamine_d` | Supplémentation en vitamine D | bool | oui |  |
| `complications` | Présence de complications et de malformation | multi | ictere, infection, conjonctivite, traumatisme, malformation, autres |  |
| `transfert` | Transfert | bool | oui |  |

### `PP_TARDIF_MERE`, Consultation du post-partum tardif, Mère

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `patiente_nom` | MÈRE, nom | text | **PII, jamais stocké** |
| `date_consultation` | Date de la consultation | date |  |
| `temperature` | T° | temperature | plage [35, 42] |
| `ta` | TA | ta |  |
| `pouls` | Pouls | int | plage [40, 180] |
| `poids_kg` | Poids | poids_kg |  |
| `cicatrice_cesarienne` | Césarienne : état de la cicatrice | text | NON_APPLICABLE si `cesarienne != oui` |
| `medicaments` | Notion de prise de médicaments (détail) | text |  |
| `traitement_autres` | Traitement prescrit, Autres à préciser | text |  |
| `prochain_rdv` | Prochain rendez-vous le | date |  |
| `pf_autre_methode` | Planification familiale, Autre à préciser | text |  |
| `pf_referee` | Référée (détail) | text |  |
| `pf_refus_raison` | Si la mère ne désire pas une méthode contraceptive : Pourquoi ? | text | NON_APPLICABLE si `'desire_methode' in pf` |

Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `periode` | Moment de la consultation | single | dans_fenetre, apres_fenetre | dans_fenetre = entre le 40ème et 50ème jour ; apres_fenetre = après le 50ème jour |
| `conjonctives` | Etat des conjonctives | single | normales, decolorees |  |
| `globe_uterin` | Présence du globe utérin | bool | present |  |
| `lochies_odeur` | Etat des lochies, odeur | single | fade, fetide |  |
| `lochies_aspect` | Etat des lochies, aspect | single | claires, sanglantes, jaunatres |  |
| `perinee` | Etat du périnée | multi | normal, episiotomie, episiotomie_reparee, dechirure |  |
| `sphincters` | Etat des sphincters (anal et urétral) | single | normal, anormal |  |
| `cesarienne` | Césarienne | bool | oui |  |
| `seins` | Etat des seins | single | normal, lymphangite, mastite_abces |  |
| `mollets` | Etat des mollets | multi | normal, rouges, chauds, douloureux_dorsiflexion |  |
| `complication` | Présence de complication | bool | presence |  |
| `complication_type` | Type de complication | multi | hemorragie, mammaires, infection, anemie, eclampsie, autres, phlebite | dépend de `complication == presence` |
| `medicaments` | Notion de prise de médicaments | bool | prise |  |
| `traitement` | Traitement prescrit | multi | fer, vitamine_a |  |
| `pf` | Planification familiale | multi | desire_methode, prescription_faite, referee |  |
| `pf_methode` | Si oui, laquelle | single | pilule, diu | dépend de `'desire_methode' in pf` |

### `PP_TARDIF_NOUVEAU_NE`, Consultation du post-partum tardif, Nouveau-né

Champs (texte/valeur) :

| Clé | Libellé | Type | Détails |
|---|---|---|---|
| `date_consultation` | Date de la consultation | date |  |
| `age_jours` | Age (jours) | int | plage [0, 90] |
| `temperature` | Température | temperature | plage [34, 42] |
| `poids_g` | Poids | poids_g | plage [500, 10000] |
| `taille_cm` | Taille | longueur_cm | plage [30, 70] |
| `perimetre_cranien_cm` | Périmètre crânien | longueur_cm | plage [25, 45] |
| `signes_graves_autres` | Signes d'une affection grave, Autres à préciser | text | alias RAS |
| `lesions_autres` | Contusions, lésions, malformations, Autres à préciser | text | alias RAS |
| `vu_par` | Vu par | text |  |
| `decision` | Décision prise | text |  |
| `traitement` | Traitement prescrit | text | alias RAS |
| `etablissement_reference` | Transfert, établissement de référence | text | NON_APPLICABLE si `transfert != oui` |
| `prochain_rdv` | Revenir pour une visite de suivi nécessaire le | date |  |

Cases à cocher (groupes) :

| Groupe | Libellé | Choix | Options | Détails |
|---|---|---|---|---|
| `etat` | Nouveau-né prématuré / hypotrophe | multi | premature, hypotrophe |  |
| `allaitement` | Allaitement | single | exclusif_sein, artificiel, mixte |  |
| `signes_graves` | Signes d'une affection grave | multi | convulsions, refus_teter, hematemeses, melaenas, diarrhee, ictere, tirage_sous_costal, toux, rythme_respiratoire_anormal, fievre, hypothermie |  |
| `lesions` | Contusions, lésions traumatiques et malformations | multi | bosse_serosanguine_cephalohematome, luxation_hanche, mobilite_membre_diminuee |  |
| `evaluation_allaitement` | Evaluation de l'allaitement maternel | single | normal, a_problemes |  |
| `vaccins` | Vaccins administrés ce jour | multi | bcg, hb |  |
| `vitamine_d` | Supplémentation en vitamine D | bool | oui |  |
| `complications` | Présence de complications et de malformation | multi | ictere, infection, conjonctivite, traumatisme, malformation, autres |  |
| `transfert` | Transfert | bool | oui |  |

## 5. Contrôles de cohérence inter-champs

| Id | Règle | Si faux |
|---|---|---|
| `parite_le_gestation` | parite <= gestation | A_REVISER sur les deux champs |
| `vivants_le_parite` | enfants_vivants <= parite | A_REVISER |
| `nb_accouchements` | nombre d'Accouch. renseignés == parite | A_REVISER (ou NON_APPLICABLE pour les colonnes > parité) |
| `dpa_vs_ddr` | |dpa - (ddr + 280 j)| <= 7 j | A_REVISER sur dpa et ddr |
| `depassement` | |date_depassement_terme - (dpa + 7 j)| <= 2 j | A_REVISER |
| `ag_vs_date` | |age_gestationnel(visite) - (venue_le - ddr)/7| <= 1 SA | A_REVISER sur age_gestationnel ou venue_le |
| `visites_chrono` | venue_le croissant de t1_v1 à t3_m9 | A_REVISER |
| `ta_plausible` | 60 <= sys <= 250 and 30 <= dia <= 150 and sys > dia | A_REVISER ; si valeurs à 1 chiffre (12/7) → ×10 |
| `poids_nn` | 500 <= poids_naissance_g <= 6000 | A_REVISER (kg ↔ g ?) |
| `rai_rhesus` | rhesus == positif → rai NON_APPLICABLE | statut forcé |
| `cesarienne_indication` | mode cesarienne → indication attendue ; sinon NON_APPLICABLE | statut forcé |
| `age_jours_pp` | précoce : 5 <= age_jours <= 15 ; tardif : 35 <= age_jours <= 60 | A_REVISER |
| `date_consult_vs_accouchement` | date_consultation - date_accouchement ≈ age_jours | A_REVISER |

## 6. Cycle de vie d'un enregistrement

États : `CAPTURE` → `EN_ATTENTE_IA` → `TRAITE_IA` → `A_REVISER` → `VALIDE` → `PATIENTE_LIEE` → `ENREGISTRE` → `SYNCHRONISE`

États d'échec : `ECHEC_TRAITEMENT`, `ECHEC_SYNCHRO`, `DOUBLON_SUSPECTE`, `REVISION_MANUELLE_REQUISE`

| De | Vers | Condition |
|---|---|---|
| `CAPTURE` | `EN_ATTENTE_IA` | photo acceptée (qualité OK), chiffrée et mise en file locale |
| `EN_ATTENTE_IA` | `TRAITE_IA` | connexion disponible, extraction réussie |
| `EN_ATTENTE_IA` | `ECHEC_TRAITEMENT` | erreur IA / timeout (retry avec backoff, max N) → REVISION_MANUELLE_REQUISE si IA indisponible durablement |
| `TRAITE_IA` | `A_REVISER` | au moins un champ A_REVISER / ILLISIBLE, ou doute de l'agent |
| `TRAITE_IA` | `VALIDE` | tous les champs CONNU / NON_FOURNI / NON_APPLICABLE et sage-femme confirme |
| `A_REVISER` | `VALIDE` | sage-femme a confirmé / corrigé / saisi chaque champ douteux |
| `A_REVISER` | `CAPTURE` | sage-femme choisit « Reprendre la photo » |
| `REVISION_MANUELLE_REQUISE` | `VALIDE` | saisie manuelle complète par questions |
| `VALIDE` | `PATIENTE_LIEE` | code sage-femme reconnu OU choix explicite [Patiente 1] [Patiente 2] [Aucune, créer] |
| `VALIDE` | `DOUBLON_SUSPECTE` | correspondance plausible mais non tranchée ([Je ne sais pas]) ou registre déjà numérisé |
| `DOUBLON_SUSPECTE` | `PATIENTE_LIEE` | sage-femme tranche ; si registre rephotographié : choisit les champs à mettre à jour |
| `PATIENTE_LIEE` | `ENREGISTRE` | fusion dans le profil longitudinal, image masquée attachée |
| `ENREGISTRE` | `SYNCHRONISE` | envoi au serveur accusé |
| `ENREGISTRE` | `ECHEC_SYNCHRO` | erreur réseau (reste en file, retry), jamais de perte |
| `ECHEC_SYNCHRO` | `SYNCHRONISE` | retry réussi |

Invariants :

- Un enregistrement n'est jamais supprimé par le système ; toute capture a un état.
- Le stockage local est chiffré ; la file survit à la fermeture de l'app.
- Les identifiants internes (record_id, patient_id) sont des UUID v4, jamais dérivés d'informations personnelles.
- L'image d'origine est stockée masquée (PII floutée) avec record_id, date de capture, midwife_id, statut ; accès par rôle.

## 7. Liaison patiente

- **code_sage_femme** : code aléatoire court (ex. 4 lettres + 2 chiffres) écrit par la sage-femme sur le carnet (N° de fiche sur le carnet marocain) ; clé primaire de liaison
- **signaux_secondaires_non_identifiants** : etablissement, age, gestation, parite, ddr, dpa, date_accouchement, groupage/rhesus
- **regle** : code exact → proposer la correspondance ; sinon score de similarité sur les signaux secondaires → si score >= seuil, proposer [Patiente 1] [Patiente 2] [Aucune, créer] [Je ne sais pas] ; jamais de création automatique quand une correspondance est plausible
