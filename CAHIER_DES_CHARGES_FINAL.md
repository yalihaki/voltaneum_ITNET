# VOLTANEUM / ITNET
## Cahier des charges final unifié

**Version :** 2.0  
**Date :** 15 octobre 2026 (1.1 : 10 octobre 2026 ; 1.0 : 17 septembre 2026)  
**Statut :** logiciel prêt pour essais terrain sur la passerelle réelle ; validation industrielle et calibration avec l'automaticien à réaliser  
**Référence principale :** ce document, complété par `README.md` pour l'exploitation (commande unique `./voltaneum`).

---

## 1. Objet du projet

Voltaneum / ITNET est un système de télémétrie et d'aide à la régulation pour un échangeur thermique industriel reliant un bain d'huile à un dry cooler.

Le système doit :

1. collecter les mesures de la passerelle industrielle ;
2. valider et historiser les mesures dans SQLite ;
3. publier la télémétrie sur MQTT ;
4. prédire l'évolution de la température `PT02` ;
5. comparer les modèles ML à une baseline de persistance ;
6. mesurer la qualité des prédictions après observation de la valeur réelle ;
7. publier la température cible et les conditions de refroidissement à suivre (seuils de marche et d'arrêt du ventilateur selon la température extérieure) ;
8. calculer une suggestion de consigne PID bornée, publiée à titre informatif ;
9. ne jamais commander directement l'automate : il lit les messages et décide seul ;
10. envoyer des alertes email contrôlées (dont « pompe en marche sans débit ») ;
11. protéger automatiquement l'espace disque et conserver les données au moins un an ;
12. continuer en mode dégradé lorsqu'un service externe est indisponible ;
13. s'installer et s'exploiter par une seule commande, sans droits administrateur, sur le contrôleur Linux.

Le projet ne remplace ni l'automate, ni les sécurités électriques, ni la supervision industrielle officielle.

---

## 2. Périmètre matériel et réseau

### 2.1 Équipement

- Équipement : échangeur `VTHEC01` ;
- passerelle : HinetGateway L100-WRS485 ;
- automate : Siemens S7-200 SMART ;
- accès process : lecture seule ;
- aucun accès direct logiciel en écriture au variateur ou à l'automate.

### 2.2 Points de mesure

| Point | Rôle | Colonne SQLite |
|---|---|---|
| `PT01` | **liquide diélectrique** (cuve où baignent les équipements) | `pt01` |
| `PT02` | **liquide diélectrique**, grandeur régulée (et prédite) | `pt02` |
| `PT03` | **liquide de refroidissement** (boucle du dry cooler) | `pt03` |
| `PT04` | **liquide de refroidissement** | `pt04` |
| `HT01` | débit circuit bain d'huile | `ht01` |
| fréquence | fréquence pompe/ventilateur | `freq_pompe` |
| capteur extérieur physique | température extérieure (source prioritaire ; repli sur l'API météo si le capteur n'a pas de valeur, voir §6.5) | `temp_outdoor` (+ `temp_outdoor_source`) |

Plages de validité (`config.SENSOR_VALID_RANGES`) :

| Colonne | Plage valide |
|---|---|
| `pt01` à `pt04` | −20 … 150 °C |
| `ht01` | 0 … 15 m³/h |
| `freq_pompe` | 0 … 50 Hz |
| `temp_outdoor` | −20 … 55 °C |

La borne haute des sondes de température est **volontairement supérieure au seuil critique** (55 °C) : une borne à 55 °C rejetterait toute mesure supérieure comme « hors plage », et l'alerte critique comme le bypass du PID ne verraient jamais une surchauffe réelle. `validate_config()` refuse de démarrer si la borne haute de `pt02` ne dépasse pas `CRITICAL_TEMP`.

Aucun capteur de température de pièce ou de baie ne doit être inventé ou ajouté implicitement.

Confirmé par l'automaticien (16/10/2026) : deux circuits séparés par un échangeur, le **liquide diélectrique** de la cuve (PT01, PT02 — PT02 régulée) et le **liquide de refroidissement** de la boucle du dry cooler (PT03, PT04). Restent à préciser : l'aller et le retour de chaque paire, et le circuit de la pompe (`PUMP`) et du débitmètre HT01. La puissance estimée (PT03 − PT04 par défaut) n'est valable que si HT01 mesure le débit de la boucle de refroidissement (sinon `THERMAL_POWER_HOT=pt02`, `THERMAL_POWER_COLD=pt01` et les propriétés du diélectrique). Les noms affichés sont définis une seule fois (`config.SENSOR_LABELS`, réglables dans `.env` : `PT01_LABEL`…`PT04_SHORT`) et identiques sur tous les écrans.

Grandeurs dérivées, enregistrées à chaque instantané :

- `humidity_outdoor` : humidité relative extérieure (%), fournie par l'API météo avec la température ; enregistrée mais ni affichée ni exportée pour l'instant ;
- `thermal_power_kw` : puissance dissipée **estimée** = débit HT01 × masse volumique × capacité thermique × (PT03 − PT04). Fluide (`THERMAL_FLUID_DENSITY_KG_M3`, `THERMAL_FLUID_HEAT_CAPACITY_J_KG_K`, valeurs d'une huile minérale typique par défaut) et sondes (`THERMAL_POWER_HOT` / `_COLD`) à confirmer ; tant que `THERMAL_POWER_CONFIRMED=false`, l'interface la présente comme une estimation. Elle n'est pas encore une entrée des modèles.

### 2.3 Adresses par défaut

Les adresses sont configurables dans `.env` et ne doivent pas être considérées comme accessibles depuis Internet :

```env
GATEWAY_HOST=10.0.1.33
MANAGE_API_URL=http://10.0.1.33/ManageApi/Base.Auth.php
IOT_API_URL=http://10.0.1.33:8001/iot/query_all_points
WS_URL=ws://10.0.1.33:91
```

La passerelle est joignable uniquement depuis le réseau industriel ou le VPN officiellement autorisé. Aucun contournement réseau, exposition publique ou désactivation de pare-feu n'est prévu.

---

## 3. Principes non négociables

### 3.1 Sécurité

- aucun secret dans le code, les logs ou les fichiers versionnés ;
- secrets uniquement dans `.env` ou le gestionnaire de secrets de la machine ;
- `.env` et les bases SQLite avec permissions restrictives ;
- utilisateur passerelle limité à la lecture ;
- données réseau validées avant utilisation ;
- valeurs physiques hors plage, `NaN`, infinis et JSON invalides rejetés ;
- TLS obligatoire et vérifié pour MQTT lorsqu'il est activé ;
- `MQTT_TLS_INSECURE=true` interdit ;
- TLS obligatoire pour SMTP ;
- aucun appel de commande directe à l'automate ;
- dépendances épinglées et vérifiées avec `pip-audit`.

### 3.2 Fiabilité

- timeouts sur les appels réseau ;
- reconnexion avec backoff plafonné ;
- séparation des composants par threads et responsabilités ;
- verrouillage de l'état partagé du collecteur ;
- redémarrage supervisé des threads métier arrêtés ;
- panne météo, MQTT, SMTP ou ML traitée en mode dégradé ;
- arrêt propre sur `Ctrl+C`, `SIGINT` ou `SIGTERM` lorsque le système le permet ;
- aucune métrique ML présentée comme fiable si les données sont insuffisantes ou quasi constantes.

### 3.3 Sécurité fonctionnelle

Le logiciel publie uniquement des conditions et une suggestion :

```text
Voltaneum -> MQTT control/cooling_conditions (conditions à suivre)
          -> MQTT control/setpoint (consigne PID indicative)
          -> automate ou son pont -> décision finale
```

Voltaneum ne doit jamais écrire dans un registre de commande, un variateur ou un automate. Il ne dit jamais « allume » ou « éteins » : il publie des seuils, l'automate compare lui-même la température d'huile à ces seuils.

---

## 4. Architecture logicielle

Un seul processus (`voltaneum.py run`, lancé et surveillé par `./voltaneum`), dont aucun composant ne dépend du broker MQTT pour fonctionner :

```text
Passerelle (HTTP + WebSocket)
        |
collectors/gateway_collector.py --- instantané 15 s ---> SQLite : telemetry
        |                                   |
        |                                   +--> notifications/alerts.py (règles, e-mail, alerts_log)
        v
service/app.py : boucle de contrôle (60 s)
        ml/predictor.py -> control/pid_controller.py + control/cooling_advice.py
        |                                   |
        +--> SQLite : predictions_log, control_log
        +--> MQTT (si joignable) : conditions + consigne informative -> pont -> automate

Dans le même processus : stockage (sauvegarde/purge), monitoring ML, interface web
(dashboard/, lecture seule), ré-entraînement nocturne (sous-processus basse priorité).
```

### 4.1 Modules

| Module | Responsabilité |
|---|---|
| `voltaneum` | commande unique : environnement Python, `.env`, démarrage, relance automatique (crontab), état, arrêt |
| `voltaneum.py` | point d'entrée Python : `run`, `retrain`, `model`, `data`, `diagnose`, `demo` |
| `config.py` | configuration et validation de démarrage |
| `service/app.py` | démarrage, supervision et arrêt de tous les composants, boucle de contrôle |
| `service/diagnose.py` | diagnostic réseau / services / base / modèle, sans envoyer d'identifiant |
| `service/watch.py` | supervision en direct dans le terminal (lecture seule) |
| `collectors/gateway_collector.py` | login, mapping, WebSocket, validation, instantanés |
| `collectors/outdoor_*.py`, `weather_collector.py` | température extérieure (capteur prioritaire, météo en repli) et contrôle de cohérence |
| `database/sqlite_client.py` | schéma, écritures, lectures |
| `database/storage_manager.py`, `backup.py` | limite disque effective, avertissement, sauvegarde vérifiée, purge |
| `database/summary.py` | résumé lisible des mesures |
| `ml/train.py`, `predictor.py`, `monitor.py` | entraînement contre la persistance, prédiction, erreur en production |
| `ml/auto_retrain.py` | ré-entraînement nocturne multi-horizons, proposition, validation humaine, retour arrière |
| `control/pid_controller.py`, `cooling_advice.py` | PID détaillé terme par terme, conditions de refroidissement (calcul pur) |
| `notifications/alerts.py` | règles d'alerte, moteur dans le processus, e-mail, journal unique |
| `messaging/mqtt.py` | clients MQTT (authentification, TLS vérifié) |
| `dashboard/` | interface web en lecture seule (supervision, historique, alertes, PID, modèles, réglages) |
| `dashboard/exports.py` | exports CSV, un par élément (mesures, consignes, alertes, prédictions, ré-entraînements), interface et `./voltaneum export` |

---

## 5. Cycle de vie principal

### 5.1 Démarrage

1. charger `.env` ;
2. valider les paramètres ;
3. initialiser SQLite en mode WAL ;
4. configurer MQTT avec identifiants et TLS éventuels ;
5. démarrer le collecteur ;
6. charger les modèles disponibles ;
7. démarrer la boucle de contrôle, le moteur d'alertes, le superviseur de stockage, le monitoring ML, l'interface et la planification du ré-entraînement ;
8. démarrer le superviseur des threads.

Un verrou (`logs/voltaneum.lock`) garantit qu'un seul processus tourne : deux collectes écriraient chaque mesure en double.

Une absence de modèles ne bloque pas la collecte : le contrôle passe en mode PID sans feedforward ML.

Si la passerelle est muette depuis plus de `CONTROL_MAX_DATA_AGE_SECONDS` (120 s), la boucle de contrôle ne publie rien (mesures périmées), avec un avertissement par panne et une reprise automatique. En plus, chaque capteur est suivi individuellement : une valeur sans mise à jour depuis plus de `SENSOR_MAX_AGE_SECONDS` (300 s), ou rejetée comme invalide, est considérée comme absente — jamais enregistrée, publiée ni utilisée comme actuelle ; sans `PT02` valide, rien n'est publié.

### 5.2 Fonctionnement continu

- le collecteur reçoit et valide les points ;
- un snapshot est enregistré selon `SNAPSHOT_INTERVAL_SECONDS` ;
- la télémétrie est publiée sur MQTT si le broker est disponible ;
- la boucle de contrôle calcule la consigne selon son intervalle ;
- les erreurs sont journalisées sans arrêter les autres composants ;
- un thread métier mort est relancé par le superviseur.

### 5.3 Arrêt

1. signaler l'arrêt ;
2. fermer le WebSocket ;
3. arrêter les boucles internes ;
4. joindre les threads ;
5. arrêter la boucle MQTT ;
6. déconnecter MQTT ;
7. quitter sans thread zombie attendu.

---

## 6. Interfaces réseau

### 6.1 Authentification passerelle

`POST MANAGE_API_URL` avec identifiants d'environnement. Le token peut être à la racine JSON ou sous `data.token`. Un token absent est une erreur explicite. Le mot de passe ne doit jamais apparaître dans une exception ou un log.

### 6.2 Catalogue

`POST IOT_API_URL` avec le token. Seuls les points du `SENSOR_DRIVER_NODE` configuré sont retenus. Le mapping `KeyNode -> colonne` est validé avant usage. Deux catalogues réels ont été relevés le 9 octobre 2026 : `KEYNAME_TO_COLUMN` accepte les noms des deux passerelles (`PT01-temperature`, `Pump frequency` sur 10.0.1.33 ; `PT01`, `PUMP` sur 10.0.1.34 ; `HT01` commun). Seuls des points en lecture seule sont mappés : les points modifiables du catalogue (`ON/OFF`, `Temperature setpoint`, bypass de sécurité…) n'y figurent jamais. Deux points alimentant la même colonne sont signalés par un avertissement. **À confirmer avec l'automaticien** : `PUMP` est supposé être la fréquence pompe / ventilateur.

### 6.3 WebSocket

Le collecteur ne traite que :

```json
{
  "Code": "IotData",
  "Data": {
    "Node": "idospV1_1",
    "Point": [{"K": "K0", "V": 27.8}]
  }
}
```

Tout autre message est ignoré. Les valeurs sont contrôlées par `SENSOR_VALID_RANGES`.

### 6.4 MQTT

Configuration :

```env
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_USERNAME=
MQTT_PASSWORD=
MQTT_TLS_ENABLED=false
MQTT_TLS_CA_CERT=
MQTT_TLS_CERTFILE=
MQTT_TLS_KEYFILE=
MQTT_TLS_INSECURE=false
MQTT_KEEPALIVE_SECONDS=60
```

Le client applique l'authentification si elle est configurée. Pour un broker distant, TLS doit être activé avec un certificat CA valide.

Topics :

| Topic | Producteur | Rôle |
|---|---|---|
| `voltaneum/telemetry` | collecteur | télémétrie validée |
| `voltaneum/prediction` | boucle de contrôle | prédictions comparées |
| `voltaneum/control/cooling_conditions` | boucle de contrôle | température cible et conditions à suivre (contrat principal) |
| `voltaneum/control/setpoint` | PID | consigne en % indicative (`PUBLISH_PID_SETPOINT`) |
| `voltaneum/model_health` | monitoring ML | RMSE glissant |

Le système accepte l'absence temporaire du broker et réessaie avec backoff. L'absence de MQTT ne doit pas empêcher l'écriture SQLite.

#### Contrat `voltaneum/control/cooling_conditions`

Publié à chaque cycle de contrôle (60 s), sans drapeau « retained » :

| Champ | Sens |
|---|---|
| `target_temp` | température cible de l'huile (°C) |
| `critical_temp` | seuil critique (°C) |
| `outdoor_band` | `froid` (< 10 °C), `tempere` (10 à 25 °C), `chaud` (≥ 25 °C) ou `inconnue` |
| `fan_on_above_temp` | seuil de marche du ventilateur = cible + décalage de la bande |
| `fan_off_below_temp` | seuil d'arrêt = seuil de marche − `FAN_HYSTERESIS_CELSIUS` |
| `fan_min_freq_hz` | fréquence minimale conseillée (`null` si bande inconnue) |
| `t_oil`, `t_outdoor` | mesures utilisées, pour la traçabilité (`t_outdoor` = `null` si inconnue) |
| `t_outdoor_source` | origine de `t_outdoor` : `sensor` (capteur, prioritaire), `weather` (repli API météo) ou `null` |
| `valid_for_seconds` | durée de validité (3 × intervalle de contrôle) |
| `ts` | horodatage de publication |

Règles : rien n'est publié si la température d'huile est invalide ou si les mesures sont périmées. Air froid : le dry cooler refroidit mieux, le ventilateur démarre plus tard (décalage +2 °C) ; air chaud : il démarre plus tôt (−2 °C). Les bandes, décalages, hystérésis et fréquences (`OUTDOOR_BANDS`, `FAN_HYSTERESIS_CELSIUS`) sont des **valeurs indicatives non calibrées**, à valider avec l'automaticien ; `validate_config()` refuse une table incohérente (bandes non ordonnées, seuil de marche ≥ seuil critique, fréquence hors de la plage du variateur).

Exigences côté automate (ou pont) : ignorer un message plus vieux que `valid_for_seconds` et retomber sur sa propre régulation ; rester dans ses bornes de variateur ; respecter son mode manuel / automatique, ses arrêts d'urgence et ses temporisations ; limiter la vitesse de variation ; ne jamais être piloté en écriture par ce projet. À ma connaissance, le S7-200 SMART n'a pas de client MQTT natif : un pont est probablement nécessaire (à confirmer).

### 6.5 Capteur extérieur, météo et SMTP

La température extérieure vient en priorité d'un capteur physique indépendant (mode `mqtt` recommandé, ou `http` en repli), pas de l'automate. Si le capteur n'a pas de valeur actuelle (non installé, muet, défaillant), la température de l'API météo est utilisée à sa place, interrogée au plus toutes les `OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS` (600 s : l'API ne change qu'aux 15 min) et reportée sur chaque instantané ; la source (`sensor` / `weather`) est enregistrée dans `temp_outdoor_source`. Une mesure plus ancienne que `OUTDOOR_SENSOR_MAX_AGE_SECONDS` (30 min), du capteur comme de la météo, est ignorée : jamais de valeur périmée. Sans aucune des deux, le PID et son bypass de sécurité continuent ; les conditions sont publiées avec la bande « inconnue ». Quand le capteur fournit une valeur, l'API météo sert aussi à calculer un écart de vérification, transmis au moteur d'alertes ; un écart important et persistant déclenche une alerte, jamais un remplacement de la valeur du capteur.

SMTP utilise STARTTLS, un timeout et une gestion d'erreur qui n'arrête jamais la collecte.

---

## 7. Données et stockage

### 7.1 Schéma SQLite

#### `telemetry`

`id`, `timestamp`, `pt01`, `pt02`, `pt03`, `pt04`, `ht01`, `freq_pompe`, `temp_outdoor`, `temp_outdoor_source` (`sensor` / `weather`), `humidity_outdoor`, `thermal_power_kw` (colonnes ajoutées automatiquement aux bases existantes, sans perte).

#### `predictions_log`

`made_at`, `target_ts`, `horizon_seconds`, `model_name`, `predicted_value`, `actual_value`, `error`.

#### `alerts_log`

`timestamp`, `level`, `rule_name`, `message`, `email_sent`.

#### `control_log`

Un enregistrement par cycle de contrôle (60 s) : `timestamp`, `t_oil`, `t_target`, `t_predicted` (prédiction combinée réellement utilisée, `NULL` sans modèle), `t_outdoor`, `t_outdoor_source`, `setpoint`, `mode`, `error`, `p_term`, `i_term`, `d_term`, `feedforward_prediction`, `feedforward_outdoor`, `raw_output` (somme avant bornage), `outdoor_band`, `fan_on_above`, `fan_off_below`, `fan_min_freq_hz`, `mqtt_setpoint` et `mqtt_conditions` (messages JSON exacts transmis au broker, `NULL` si non transmis), `mqtt_sent` (1 transmis, 0 échec, `NULL` publication désactivée). Rien n'est enregistré quand rien n'est publié (mesures périmées). Purge : même plancher que `predictions_log`.

SQLite utilise WAL et un index sur les timestamps.

### 7.2 Trous de données

Les trous sont conservés dans l'historique et signalés dans l'analyse. L'entraînement ne doit pas relier artificiellement deux segments séparés par plus de `MAX_GAP_SECONDS`.

La cible ML est construite par timestamp réel avec une tolérance configurée, jamais par décalage aveugle de lignes.

### 7.3 Campagne de collecte recommandée

La collecte démarre avec `./voltaneum`. La boucle de contrôle tourne dès le départ, mais ne fait que publier des messages informatifs : tant qu'aucun pont vers l'automate n'est validé (§14.3), rien n'agit sur l'installation.

Durées recommandées :

- **minimum technique :** 72 heures, pour vérifier le pipeline et la continuité ;
- **minimum recommandé pour un premier modèle :** 14 jours, avec au moins 20 à 30 cycles actifs complets ;
- **campagne recommandée avant validation industrielle :** 30 jours, couvrant démarrages, arrêts, charge faible, charge nominale, variations météo et incidents de communication.

Pour l'horizon de production de 600 secondes, chaque cycle actif doit durer au moins 20 à 30 minutes et présenter une variation mesurable de `PT02`. Une base volumineuse mais avec pompe à zéro ou cible constante n'est pas suffisante.

La campagne est acceptable seulement si `freq_pompe` présente plusieurs phases, si `pt02` varie réellement, si les autres variables ne sont pas figées, et si les trous restent compatibles avec `MAX_GAP_SECONDS`.

### 7.4 Rétention temporelle

Il n'existe aucune purge calendaire : seule la taille déclenche une purge. La purge
automatique est déclenchée exclusivement par le dépassement de taille
(`STORAGE_WARNING_RATIO`) : les rétentions sont des planchers de conservation,
jamais un calendrier de suppression. Sous pression, l'ordre est
`telemetry`, `predictions_log` complétées, puis `alerts_log`; si le plancher
empêche d'atteindre la cible, l'alerte critique est
`storage_purge_ineffective`.

### 7.5 Sauvegardes et restauration

Une sauvegarde **quotidienne**, cohérente (API de sauvegarde SQLite), compressée et vérifiée par restauration d'essai, est faite dans `backups/daily` ; les `STORAGE_DAILY_BACKUP_COUNT` (7) plus récentes sont gardées. Elle est indépendante des archives faites avant une purge (conservées au moins un an). Une sauvegarde impossible (espace, droits) déclenche l'alerte `storage_daily_backup_failed`.

Restauration : `./voltaneum stop`, `./voltaneum restore` (liste), `./voltaneum restore <nom>` (refusée tant que Voltaneum tourne ; la base remplacée est d'abord sauvegardée dans `backups/avant-restauration`), puis `./voltaneum`.

### 7.6 Protection de capacité disque

Le `StorageManager` surveille `.db`, `.db-wal` et `.db-shm`.

Paramètres par défaut :

```env
STORAGE_LIMIT_BYTES=1099511627776
STORAGE_WARNING_RATIO=0.90
STORAGE_PURGE_TARGET_RATIO=0.75
STORAGE_GRACE_PERIOD_SECONDS=3600
STORAGE_CHECK_INTERVAL_SECONDS=300
STORAGE_PURGE_BATCH_SIZE=1000
```

Séquence obligatoire :

1. email d'avertissement à 90 % ;
2. journalisation dans `alerts_log` ;
3. délai de grâce ;
4. absence de réaction ;
5. suppression des lignes les plus anciennes par lots ;
6. checkpoint et compactage SQLite ;
7. email de confirmation ;
8. reprise normale de la surveillance.

Cette purge protège la disponibilité mais ne remplace pas une sauvegarde. Une sauvegarde et un test de restauration doivent précéder tout déploiement terrain.

Avant une purge, `database/backup.py` crée une copie cohérente via l'API SQLite, la compresse en gzip, vérifie son intégrité. En cas d'échec, la purge est annulée et une alerte critique est journalisée.

**Conservation d'un an (exigence).** Les données doivent rester récupérables au moins un an :

- base vivante : `RETENTION_DAYS=365` interdit de supprimer une ligne de moins d'un an ;
- archives compressées : une archive n'est supprimée que si elle est **à la fois** au-delà des `STORAGE_BACKUP_RETENTION_COUNT` plus récentes **et** plus vieille que `STORAGE_BACKUP_MIN_AGE_DAYS` (365 jours). Une archive de moins d'un an n'est jamais supprimée, même en grand nombre. L'âge est lu dans le nom de l'archive (horodatage UTC de création) ; un nom illisible est conservé.

**Espace libre.** La sauvegarde crée une copie temporaire non compressée puis son archive : avant toute purge, le volume `STORAGE_BACKUP_DIR` doit offrir la taille de la base × `STORAGE_BACKUP_FREE_SPACE_FACTOR` (1,1) d'espace libre, sinon la purge est annulée, rien n'est supprimé et une alerte CRITIQUE est envoyée. Le volume de sauvegarde doit être distinct du volume de la base ; les archives doivent aussi être copiées sur un autre support (une archive sur le même disque ne protège pas d'une panne de disque).

**Volume attendu.** À 15 s par mesure : environ 2,1 millions de lignes de télémétrie par an, soit quelques centaines de Mo. `STORAGE_LIMIT_BYTES` (1 Tio par défaut) doit être réglé sur la part de disque réellement disponible.

---

## 8. Machine Learning

### 8.1 Entrées et cible

Features : `pt02` (valeur actuelle), `pt01`, `pt03`, `pt04`, `ht01`, `freq_pompe`, `temp_outdoor`.  
Cible : `pt02` à l'horizon. La valeur actuelle de `pt02` fait partie des entrées : sans elle, le modèle devait la deviner alors que la persistance, à laquelle il est comparé, la connaît. Chaque modèle enregistre sa liste de variables ; un modèle entraîné avec une autre liste est refusé au chargement.  
Horizon de production actuel : `600` secondes.

### 8.2 Entraînement

Les modèles sont :

- `RandomForestRegressor` ;
- `GradientBoostingRegressor`.

Le split est chronologique 80/20. La baseline est la persistance : valeur actuelle conservée pour l'avenir.

Un rapport valide doit contenir :

- `status: ok` ;
- métriques RF et Gradient Boosting ;
- métriques de persistance ;
- `best_model` ;
- `beats_persistence` ;
- nombre d'échantillons ;
- horizon ;
- colonnes utilisées.

### 8.3 Règles de validité

Un modèle ne doit pas être déclaré industriellement valide si :

- le segment continu est trop court ;
- le nombre d'échantillons est insuffisant ;
- la cible est quasi constante ;
- la pompe n'a pas de cycles actifs ;
- le modèle ne bat pas la persistance ;
- les données d'essai ne représentent pas les conditions terrain.

Les modèles synthétiques servent à tester le pipeline, jamais à justifier une décision industrielle.

### 8.3 bis Ré-entraînement automatique

`ml/auto_retrain.py` (lancé chaque nuit, et 5 min après le démarrage tant qu'aucun modèle n'est installé et qu'aucun ré-entraînement n'a eu lieu depuis 24 h ; un seul à la fois grâce à un verrou) entraîne les horizons de `AUTO_RETRAIN_HORIZONS` sur les `AUTO_RETRAIN_MAX_DAYS` derniers jours et **propose** l'horizon le plus long jugé fiable (garde-fous de §8.3, erreur au moins `AUTO_RETRAIN_MIN_GAIN` sous la persistance, sous `AUTO_RETRAIN_MAX_RMSE_CELSIUS`, période de test d'au moins 3 fois l'horizon). Le remplacement du modèle de production exige une validation humaine (`./voltaneum model accept`), avec sauvegarde de l'ancien modèle et retour arrière (`./voltaneum model rollback`). Le monitoring (§8.4) suit tous les horizons présents dans `predictions_log`.

### 8.4 Monitoring

Les prédictions arrivées à échéance sont complétées par une mesure réelle, puis l'erreur et le RMSE glissant sont calculés. L'état est publié sur `voltaneum/model_health` lorsqu'un client MQTT est disponible.

### 8.5 Validation multi-horizon

Le ré-entraînement nocturne (§8.3 bis) entraîne chaque horizon séparément et rend un verdict par horizon (onglet Modèles). Un horizon dont la cible est trop stable, ou que la persistance bat déjà, n'est jamais proposé. Un modèle qui ne bat pas la persistance n'est jamais sauvegardé (statut `no_skill`).

---

## 9. Contrôle et sécurité process

Le PID reçoit la température actuelle, la cible, la prédiction, la météo, `dt` et son état précédent.

Sortie :

```text
(setpoint_percent, new_pid_state, mode)
```

`setpoint_percent` est borné entre 0 et 100.

Modes :

- `pid` ;
- `feedforward` ;
- `safety_bypass`.

Si `t_oil > CRITICAL_TEMP`, la sortie est immédiatement 100 %. Les entrées non finies ne doivent jamais contaminer l'état PID.

**Anti-windup.** L'intégrale est gelée tant que la sortie est saturée **et** bornée dans `[0, SETPOINT_MAX / Ki]`. Le correctif « huile froide » évite qu'une période froide crée une dette négative inutile : avant correction, huile à 10 °C puis 40 °C donnait 9 minutes à 0 % ; après, la consigne réagit dès le premier cycle chaud.

**Rôle du PID.** Le contrat principal est la publication de la température cible et des conditions (§6.4) ; la consigne PID en % est publiée sur `voltaneum/control/setpoint` à titre informatif (suivi et comparaison avec la régulation de l'automate), désactivable par `PUBLISH_PID_SETPOINT=false`. Gains par défaut : Kp = 10 %/°C, Ki = 0,02 (temps d'intégration 500 s), Kd = 0 — point de départ prudent pour un procédé thermique lent échantillonné à 60 s, à régler sur site. Les anciennes valeurs (5 / 0,1 / 1) donnaient un temps d'intégration de 50 s, plus court qu'un cycle de calcul : consigne vite saturée.

**Reprise après coupure.** Quand les mesures redeviennent disponibles après une période périmée (passerelle muette ou PT02 absente), l'état du PID est réinitialisé : l'intégrale accumulée avant la panne ne correspond plus au procédé.

**Désaccord des modèles.** Si les deux modèles divergent de plus de `DISAGREEMENT_THRESHOLD_CELSIUS`, la prédiction n'est pas utilisée par le PID pendant ce cycle (pas d'anticipation sur une prédiction douteuse), et l'alerte `model_disagreement` est évaluée.

Chaque cycle est journalisé dans `control_log` (termes, prédiction utilisée, message MQTT exact et résultat de l'envoi) et visible dans l'onglet PID.

Aucun module ne dispose d'une primitive d'écriture directe sur le PLC ou le variateur ; les points modifiables du catalogue de la passerelle ne sont jamais mappés.

---

## 10. Alertes

Le moteur d'alertes tourne **dans le processus principal** : il est alimenté directement par le collecteur, la boucle de contrôle et le contrôle de cohérence, et ne dépend d'aucun broker MQTT (sur le contrôleur, sans broker, aucune alerte n'aurait jamais été émise auparavant).

Règles :

- PT02 critique (`critical_temp`, CRITIQUE, immédiate) ;
- **liquide diélectrique critique** (`dielectric_critical_temp`, CRITIQUE, immédiate) : PT01 au-delà de `DIELECTRIC_CRITICAL_TEMP` (PT02, aussi diélectrique, relève du seuil critique de la grandeur régulée) (55 °C par défaut) — c'est la température qui protège les équipements immergés ;
- **pompe / ventilateur en marche sans débit** (`pump_without_flow`, CRITIQUE) : fréquence > `PUMP_RUNNING_MIN_HZ` et débit < `FLOW_MIN_M3H` sur `ALERT_CONFIRMATION_CYCLES` échantillons distincts ; une valeur absente ne déclenche jamais la règle ;
- perte de télémétrie (`telemetry_lost`, CRITIQUE) ;
- **PT02 absente** alors que la passerelle émet (`oil_sensor_missing`, ATTENTION, après `OIL_SENSOR_MISSING_ALERT_SECONDS`) : aucune consigne n'est alors publiée ;
- **PT02 figée** (`oil_sensor_frozen`, ATTENTION) : valeur strictement constante pendant `SENSOR_FROZEN_ALERT_MINUTES` (cas réel : 34,0 °C sur 652 mesures, point de passerelle douteux) ;
- désaccord entre modèles ;
- écart entre le capteur extérieur et la météo ;
- dérive d'efficacité : formule non validée, **désactivée par défaut** (`EFFICIENCY_ALERT_ENABLED`) ;
- stockage : seuil d'avertissement, purge exécutée ou bloquée, sauvegarde (avant purge ou quotidienne) impossible.

Les e-mails sont envoyés par le thread du moteur d'alertes, jamais par la collecte : un serveur SMTP lent ne retarde aucune mesure. Un défaut durable (`oil_sensor_frozen`) n'est rappelé qu'une fois par jour ; les autres règles respectent `ALERT_COOLDOWN_MINUTES`.

Chaque alerte est journalisée **une seule fois**, sous son vrai nom de règle, dans `alerts_log` (`email_sent` indique si l'e-mail est parti). Les alertes `ATTENTION` exigent plusieurs échantillons distincts, toutes ont un délai entre deux e-mails ; un e-mail en échec est retenté sous 60 s. Sans SMTP, les alertes restent visibles dans l'interface.

Une panne SMTP ne doit jamais stopper la collecte, le PID ou le stockage.

---

## 11. Plateformes d'exploitation

Cible : le contrôleur Linux, avec Python 3.11 ou plus récent, **sans droits root**. Un poste macOS sert au développement et à la démonstration (`./voltaneum demo`).

`./voltaneum` crée l'environnement Python, installe les dépendances épinglées (seulement si `requirements.txt` change), vérifie `.env`, installe la relance automatique dans la crontab de l'utilisateur (vérification chaque minute : redémarrage après plantage ou redémarrage du serveur) et démarre le processus. Les anciennes installations du projet (collecte, interface et entraînement lancés séparément) sont arrêtées et leurs lignes de crontab retirées ; les autres tâches de la crontab sont conservées.

---

## 12. Déploiement terrain

### 12.1 Installation et mise à jour

```bash
git clone … voltaneum_ITNET && cd voltaneum_ITNET
./voltaneum                 # crée .env au premier lancement : le renseigner, puis relancer
git pull && ./voltaneum restart
```

Ne jamais transférer un secret par dépôt public ou messagerie non sécurisée ; `.env` est en `0600`.

### 12.2 Diagnostics

`./voltaneum diagnose` vérifie la configuration, l'accès TCP à la passerelle (aucun identifiant envoyé, aucune commande), le broker, l'API météo, l'e-mail, la base, le modèle et le port de l'interface.

### 12.3 Mise en service progressive

1. `./voltaneum` : collecte, alertes et interface ; aucun pont vers l'automate ;
2. vérifier les mesures, les trous et l'alerte « PT02 figée » (`./voltaneum data`, interface) ;
3. configurer l'e-mail des alertes ;
4. attendre une collecte avec de vrais cycles de pompe ; le ré-entraînement nocturne proposera un modèle ;
5. valider le modèle (`./voltaneum model accept`) après lecture du rapport ;
6. régler les gains PID et les seuils avec l'exploitant ;
7. définir et tester le pont MQTT vers l'automate ;
8. laisser l'automate décider de toute application physique.

### 12.4 Exploitation

`./voltaneum watch` (supervision en direct dans le terminal, pour un contrôleur sans environnement graphique), `./voltaneum status` (état, dernières mesures, alertes, modèle proposé), `./voltaneum logs`, `./voltaneum stop`, `./voltaneum restart`, `./voltaneum restore`. Une passerelle injoignable est journalisée à la première tentative puis toutes les 20 tentatives (~10 min), pas à chaque essai.

---

## 13. Essais hors réseau industriel

`./voltaneum demo` lance l'interface sur une base fictive séparée (7 jours de mesures, PID réel, prédictions, alertes), alimentée en continu. Elle valide l'interface et le pipeline, pas l'accès à la passerelle ni la performance industrielle.

---

## 14. Critères de recette

### 14.1 Tests automatisés

`./voltaneum test` : tests (pytest), style (ruff), types (mypy), sécurité (bandit) — tous verts.

### 14.2 Tests de résilience

Les scénarios suivants doivent être exécutés :

- broker MQTT absent, arrêté puis relancé (collecte et alertes continuent) ;
- réseau passerelle coupé puis restauré ;
- API météo indisponible ;
- SMTP indisponible ;
- modèle absent, défaillant ou en désaccord ;
- valeur sensorielle hors plage, `NaN` ou infinie ;
- PT02 absente ou figée ;
- thread métier arrêté ;
- base proche de la limite effective de stockage ;
- volume de sauvegarde sans espace libre (purge annulée, rien supprimé) ;
- deuxième lancement (refusé par le verrou) ;
- arrêt puis redémarrage propre, redémarrage du serveur (relance par la crontab).

### 14.3 Validation terrain

La recette industrielle n'est prononcée qu'après :

- accès réel à la passerelle depuis le réseau autorisé ;
- collecte continue sans corruption ;
- cycles réels de pompe/ventilateur ;
- variation mesurable de `PT02` (le point actuel, constant à 34,0 °C, est à vérifier) ;
- modèle meilleur que la persistance sur une période représentative, validé par une personne ;
- validation des seuils et des gains PID par l'exploitant ;
- confirmation de `PUMP` (ou `DIAG Actual frequency VW12`) comme fréquence pompe / ventilateur ;
- validation des bandes de température extérieure, décalages, hystérésis et fréquences conseillées ;
- validation des seuils de la règle pompe sans débit ;
- définition et test du pont MQTT vers l'automate (âge du message, bornes du variateur, mode manuel / automatique) ;
- confirmation qu'aucune écriture directe PLC n'existe ;
- plan de sauvegarde et restauration testé.

---

## 15. État actuel et limites connues

Le logiciel, ses tests, sa gestion des erreurs, sa supervision, sa sécurité MQTT, sa gestion de stockage et la publication des conditions de refroidissement sont implémentés.

Le 9 octobre 2026, la passerelle 10.0.1.34 a été jointe depuis le réseau industriel : login réussi, catalogue et WebSocket opérationnels, six colonnes remplies (pompe à 30 Hz, débit ≈ 10,4 m³/h, `PT01` à `PT04` entre 31 et 34 °C).

Les premières données réelles ne contiennent encore aucune variation de fréquence de pompe : `freq_pompe` à 30 Hz constant ne permet pas d'entraîner un modèle (garde-fou `no_actionable_signal`). Une collecte avec des fréquences variées est nécessaire. La base de test précédente a montré des périodes avec pompe à zéro, `HT01` constant, faible variation de `PT02` et des trous importants. Elle permet de tester le pipeline, mais ne suffit pas seule à valider la performance industrielle du modèle.

La connexion aux passerelles (`10.0.1.33`, `10.0.1.34`) et au broker d'entreprise ne peut pas être testée depuis une machine hors réseau ou sans VPN autorisé. Cette limite est réseau, pas un défaut à contourner dans le code.

Les paramètres suivants restent à confirmer sur le terrain :

- `PUMP` comme fréquence pompe / ventilateur ;
- gains PID ;
- seuil critique ;
- bandes de température extérieure, décalages, hystérésis et fréquences conseillées (`OUTDOOR_BANDS`) ;
- seuils de la règle pompe sans débit ;
- formule et plage d'efficacité (règle désactivée d'ici là) ;
- fréquence et dynamique réelles de la pompe ;
- pont MQTT vers l'automate ;
- politique d'archivage hors site.

---

## 16. Livrables

- code source et commande unique `./voltaneum` ;
- `README.md` d'exploitation ;
- ce cahier des charges ;
- `.env.example` sans secrets ;
- `requirements.txt` épinglé ;
- tests automatisés ;
- modèles ML validés (non livrés tant qu'aucune collecte avec cycles de pompe n'a abouti) ;
- base réelle uniquement si sa conservation est autorisée (non livrée) ;
- procédure de sauvegarde et de restauration à établir pour le site.
