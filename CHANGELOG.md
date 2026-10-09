# CHANGELOG.md — Revue d'ingénierie du 17-24 septembre 2026

Ce journal documente les corrections apportées au projet lors d'une revue de
code complète, avant la première campagne de test réel prévue le 24 septembre
2026. Chaque entrée indique le fichier concerné, le problème trouvé, et
comment il a été vérifié (pas seulement corrigé).

## 1. `control/pid_controller.py` — Inversion du signe de l'erreur PID

**Problème** : `error = t_target - t_oil` faisait que le régulateur suggérait
**moins** de refroidissement quand l'huile chauffait (ex. huile à 45°C →
consigne 0%). Les tests existants ne l'avaient pas détecté car ils
vérifiaient uniquement les bornes `[0, 100]`, pas la direction.

**Correction** : `error = t_oil - t_target` (positif quand l'huile est plus
chaude que la cible).

**Vérification** : 3 tests renforcés pour vérifier la direction physique en
plus des bornes, dont `test_setpoint_direction_matches_physical_expectation`
qui casse immédiatement si le signe est de nouveau inversé. Comportement
démontré en direct dans `simulate.py` (étape 5).

## 2. `control/pid_controller.py` — Anti-windup ajouté

**Problème** : le terme intégral n'avait aucune protection contre le
"windup" : après une longue saturation (ex. canicule prolongée), l'intégrale
accumulée met anormalement longtemps à se résorber, même une fois l'erreur
redevenue nulle.

**Correction** : anti-windup par clamping conditionnel — l'intégrale se gèle
quand la sortie est déjà saturée dans le même sens que l'erreur ; l'état PID
porte désormais `saturated_high` / `saturated_low` en plus de `integral` /
`last_error`.

**Vérification** : `test_anti_windup_allows_fast_recovery_after_saturation`
simule 500 cycles de saturation puis vérifie une récupération en un seul
cycle. Démontré en direct dans `simulate.py` (étape 5).

## 3. `ml/train.py` — Garde-fous contre un modèle trompeur

**Problème** : sur la collecte réelle du 9-10 septembre 2026, `freq_pompe`
et `ht01` sont restés strictement constants pendant toute la collecte (la
pompe n'était pas pilotée sur l'automate de test). Résultat : RandomForest
obtenait un RMSE de **0.0 exact** — un score parfait mais qui ne prouve rien,
puisque le modèle n'a jamais vu l'effet réel de la pompe sur la température.

**Correction** : deux garde-fous ajoutés.
- `insufficient_variance` : la cible elle-même n'a presque pas varié sur la
  période (écart-type < `MIN_TARGET_STD_CELSIUS`, 0.3°C par défaut).
- `no_actionable_signal` : une ou plusieurs variables d'entrée (typiquement
  `freq_pompe`) n'ont **jamais** varié (`std < FEATURE_DEGENERACY_STD_EPSILON`,
  quasi zéro — volontairement un seuil de dégénérescence réelle, pas un
  seuil "métier" à calibrer par capteur, pour ne jamais confondre un bruit de
  mesure normal avec une variable figée).

Dans les deux cas, le statut n'est jamais `"ok"` et **aucun fichier de
modèle n'est sauvegardé** — `ml/predictor.py` bascule alors automatiquement
en `FileNotFoundError`, et le système continue en mode dégradé (PID sans
anticipation ML) plutôt que d'utiliser un modèle trompeur.

**Vérification** : 4 tests dédiés dans `tests/test_train.py`, dont un qui
verrouille explicitement un faux positif détecté en cours de développement
(bruit de mesure réaliste sur `ht01` à ne jamais confondre avec une variable
figée). Démontré en direct dans `simulate.py` (étapes 3a/3b, qui rejouent
côte à côte le cas réel figé et un cas avec vraie variabilité).

## 4. `tests/test_main.py` — Hermétisme des tests

**Problème** : la fixture partagée `app_with_fakes` simulait le collecteur,
MQTT et les modèles, mais pas les identifiants — 10 tests dépendaient
silencieusement du contenu réel de `.env` sur la machine d'exécution. Casse
démontrée après la rotation des identifiants du 18 septembre 2026 (vidage de
`.env`), sur une machine sans ces variables définies.

**Correction** : `monkeypatch.setattr(config, "VOLTANEUM_USER", ...)` et
`VOLTANEUM_PASSWORD` ajoutés à la fixture.

## 5. `generate_test_data.py` — Code mort trompeur

**Problème** : 4 lignes calculaient des tableaux (`pt01`, `pt03`, `pt04`,
`ht01`) jamais utilisés — la boucle d'insertion recalculait ces mêmes
colonnes différemment juste en dessous, avec d'autres formules. Repéré par
`ruff` (F841), mais le vrai risque était qu'une lecture rapide du haut du
fichier laisse croire à des valeurs différentes de celles réellement
insérées.

**Correction** : suppression du code mort.

## 6. `config.py` — Plage de validité de `freq_pompe` corrigée

**Problème** : `SENSOR_VALID_RANGES["freq_pompe"]` était fixée à `(0.0, 100.0)`,
alors que le variateur réel de la pompe sur l'automate de test est borné à
**0-15 Hz**. Une valeur aberrante entre 15 et 100 Hz aurait été acceptée
comme valide à tort.

**Correction** : `(0.0, 15.0)`, avec un commentaire expliquant pourquoi les
données actuelles affichent 0.0 en continu (automate de test ne pilotant pas
encore la pompe — ce n'est pas une valeur aberrante, voir point 3 ci-dessus).

## 7. `database/backup.py` — Exercice de sauvegarde/restauration réel

**Problème** : le mécanisme était codé et couvert par un test unitaire, mais
jamais exercé en conditions proches du réel (suppression effective du
fichier, écriture concurrente pendant la sauvegarde).

**Vérification effectuée** (pas une correction de code — le mécanisme
fonctionnait déjà) :
- Sauvegarde → suppression réelle de `voltaneum.db` → restauration →
  empreinte SHA-256 du contenu identique avant/après (1481 lignes).
- Sauvegarde déclenchée pendant une écriture concurrente active (mode WAL) :
  aucune erreur, sauvegarde valide.
- Fichier de sauvegarde corrompu : correctement détecté comme invalide par
  `verify_backup`.

Reproductible en abrégé via `simulate.py` (étape 7).

## 11. Audit complet et nettoyage (renommage Voltaneum)

**Renommage** : `Vaultanium`/`Vaultaneum` → `Voltaneum` partout (code, variables `VOLTANEUM_*`, topics
`voltaneum/*`, base `voltaneum.db`, services, documentation).

**Failles corrigées** (chacune verrouillée par un test) :
- **Purge de stockage** : la boucle mesurait la taille du *fichier*, qui ne rétrécit pas avant `VACUUM` ;
  elle supprimait donc *tout* ce qui était éligible au lieu de s'arrêter à la cible. Elle mesure désormais
  les données vivantes (pages utilisées).
- **SMTP** : `starttls()` sans contexte ne vérifiait pas le certificat ; contexte vérifié, en-têtes protégés
  contre l'injection.
- **Alertes** : une même prédiction ou un même écart était recompté à chaque message de télémétrie
  (confirmation « 3 cycles » illusoire) ; le chien de garde remettait les compteurs à zéro ; charges MQTT
  non validées (NaN, texte…) pouvaient faire planter les règles ; l'envoi d'e-mail bloquait l'abonné MQTT.
- **Restauration** : un ancien `-wal` pouvait être rejoué sur la base restaurée ; fichiers temporaires
  oubliés ; archives vides acceptées ; sauvegardes et modèles désormais en 0600/0700.
- **Connexions SQLite** jamais fermées (`with sqlite3.connect()` ne ferme pas) → gestionnaire `connect()`.
- **Supervision** : seuls 3 threads sur 6 étaient relancés ; broker MQTT absent au démarrage tuait
  définitivement certains threads ; la boucle de contrôle s'arrêtait sans capteur extérieur (bypass de
  sécurité compris) ; une valeur extérieure périmée restait utilisée indéfiniment.
- **Monitoring ML** : comparaison en heure locale au lieu d'UTC ; `log_prediction` jamais appelée en
  production ; état des modèles jamais publié ; une ligne `pt02` NULL bloquait tout le rapprochement.
- **Flux passerelle muet** : détection (connexion à moitié morte), instantanés suspendus ; journaux de
  connexion ne recopient plus les corps de réponse.
- **Modèles (pickle)** : écriture atomique, droits 0600 ; rechargement automatique sans redémarrage.
- **Configuration** : lecture typée avec messages explicites, validation exhaustive, chemins relatifs
  résolus depuis la racine du projet.
- `train_from_local_db.py` dupliquait l'entraînement *sans* les garde-fous : réduit à un appel de `ml.train`.

**Code mort supprimé** : `purge_older_than`, `MultiHorizonPredictor`, `PLC_IP`/`PLC_PROTOCOL`,
`MQTT_TOPIC_ALERTS`, paramètre `control` des règles d'alerte, DDL dupliqué, anciens documents d'archive,
ancien PDF. Dépendances séparées en `requirements.txt` / `requirements-dev.txt`.

## 8. `simulate.py` — Couverture incomplète de la démonstration

**Problème** : la simulation bout-en-bout ne démontrait pas `ml/monitor.py`
(vérification en temps réel prédiction/réalité, RMSE glissant), et l'étape
stockage se contentait de constater "on est sous le seuil" sans jamais
déclencher réellement l'avertissement ni la purge — donc sans prouver que
ce chemin fonctionne.

**Correction** :
- Étape 4b ajoutée : journalise une prédiction, simule l'échéance, la
  rapproche de la valeur réelle via `complete_due_predictions`, calcule le
  RMSE glissant via `get_rolling_rmse`.
- Étape 2 enrichie : après le constat "sous le seuil", le seuil est
  artificiellement abaissé (et restauré ensuite, `try`/`finally`) pour
  déclencher réellement l'avertissement et une tentative de purge, avec un
  envoyeur d'alerte factice (aucun email réel).

**Vérification** : `simulate.py` passe de 13 à 15 vérifications, toutes au
vert.

## 9. Remplacement d'Open-Meteo par un capteur extérieur physique indépendant

**Décision** (30 septembre 2026, à la demande du responsable) : la température
extérieure ne vient plus d'une API météo régionale mais d'un capteur physique
dédié installé sur site, raccordé à un système/réseau **indépendant** de la
passerelle HinetGateway (pas un point de plus sur l'automate S7-200 SMART).

**Choix d'architecture** : le capteur publie lui-même sa valeur sur le broker
MQTT déjà utilisé par le projet (`OUTDOOR_SENSOR_MODE=mqtt`, topic
`voltaneum/sensors/outdoor_temperature`), plutôt que d'être interrogé
activement. Ce mode "push" évite le polling, n'ajoute aucune API/URL de plus
à sécuriser, et s'intègre nativement dans le bus MQTT déjà central au projet.
Un mode "http" (repli, `OUTDOOR_SENSOR_MODE=http`) reste disponible si le
matériel finalement choisi ne peut techniquement pas publier sur MQTT.

**Implémentation** :
- `collectors/outdoor_sensor_collector.py` (nouveau) : `OutdoorSensorListener`
  (mode mqtt, abonné sur le modèle de `notifications/email_notifier.py`) et
  `get_outdoor_temperature_http()` (mode http, repli).
- `config.py` : `OUTDOOR_SENSOR_MODE`, `OUTDOOR_SENSOR_MQTT_TOPIC`,
  `OUTDOOR_SENSOR_HTTP_URL`, `OUTDOOR_SENSOR_HTTP_TIMEOUT` ; validation au
  démarrage. `VOLTANEUM_LAT`/`LON` dépréciées (conservées pour
  `collectors/weather_collector.py`, qui n'est plus appelé en production).
- `main.py` : nouveau thread supervisé `outdoor-sensor-listener` en mode mqtt ;
  `_fetch_outdoor_temperature` bascule selon `OUTDOOR_SENSOR_MODE`, avec le
  même principe de repli qu'avant (dernière valeur connue, jamais de valeur
  inventée).

**Vérification** : 5 tests mis à jour ou ajoutés dans `tests/test_main.py`
(dont un faux `OutdoorSensorListener` pour ne pas tenter de vraie connexion
MQTT dans les tests qui ne concernent pas la météo, et un test couvrant
explicitement le mode mqtt par défaut). 97 tests passent, `ruff`/`mypy`
inchangés.

**Reste à faire dès que le matériel est choisi** : confirmer avec le
responsable/l'installateur le topic MQTT exact (ou l'URL HTTP) et le format
du payload — un `TODO` explicite est laissé dans `config.py` à cet effet.

## 10. Vérification de cohérence capteur extérieur / météo (suite discussion avec Taher)

**Décision** (30 septembre 2026) : plutôt que de choisir entre le capteur
physique indépendant et l'API météo, les deux sont conservés — mais avec des
rôles strictement asymétriques. Le capteur reste la SEULE valeur injectée
dans `last_known`/SQLite/le PID. L'API météo ne sert plus qu'à calculer un
écart de référence, publié sur MQTT, qui déclenche une alerte `ATTENTION`
(pour vérification humaine) en cas de divergence importante et persistante
— sans jamais remplacer ni rejeter la valeur du capteur.

**Pourquoi un seuil large (8°C par défaut)** : le capteur local et une
station météo régionale peuvent légitimement diverger de plusieurs degrés
(micro-climat au pied de l'installation, air soufflé par le dry cooler,
ensoleillement direct) sans qu'aucune des deux mesures ne soit fausse. Un
seuil trop strict rejetterait (en alerte, pas en donnée) exactement les
écarts les plus informatifs.

**Implémentation** :
- `collectors/outdoor_consistency_checker.py` (nouveau) : compare
  périodiquement (15 min par défaut) capteur et météo, publie l'écart sur
  `voltaneum/sensors/outdoor_consistency`. Ne décide jamais d'alerte
  lui-même (même séparation des responsabilités que `ml/monitor.py`).
- `notifications/email_notifier.py` : nouvelle règle
  `outdoor_sensor_weather_mismatch` (ATTENTION, confirmation multi-cycles,
  cooldown, compteur remis à zéro si l'écart redevient normal — même
  discipline que les autres règles).
- `config.py` : `OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS`,
  `OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS`.
- `main.py` : nouveau thread supervisé `outdoor-consistency-checker`,
  démarré quel que soit le mode du capteur (mqtt ou http).

**Vérification** : 9 tests dédiés (`tests/test_outdoor_consistency.py`),
couvrant notamment le point le plus sensible — le message d'alerte ne doit
jamais laisser penser que la valeur du capteur a été modifiée. 106 tests
passent au total, `ruff`/`mypy` inchangés, `simulate.py` toujours 15/15.

**Écart avec le développement précédent** : ce changement rétablit un usage
actif de `collectors/weather_collector.py` (précédemment retiré du chemin de
production, voir point 9) — mais uniquement comme référence de comparaison,
jamais comme source de la valeur injectée. La note de dépréciation en tête
de ce fichier reste donc légèrement obsolète et pourrait être clarifiée.

## Points déjà conformes (vérifiés, non modifiés)

Les décisions d'architecture des addenda A et B (purge déclenchée uniquement
par la taille et non par le calendrier, cooldown dédié pour les alertes de
stockage, purge couvrant les trois tables avec distinction "inefficace" vs
"bloquée par le plancher", TLS MQTT, `MAX_GAP_SECONDS` lu depuis
l'environnement) étaient déjà correctement implémentées dans
`database/storage_manager.py` et `config.py` au moment de cette revue.
