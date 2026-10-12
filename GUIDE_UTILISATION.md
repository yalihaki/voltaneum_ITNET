# Guide d'utilisation — Voltaneum / ITNET

Ce guide explique, pas à pas, comment installer, configurer, tester et exploiter le projet.
Pour la conception détaillée, voir `CAHIER_DES_CHARGES_FINAL.md` ; pour l'historique des
corrections, `CHANGELOG.md`.

> **Principe de sécurité à retenir** : Voltaneum **ne commande jamais l'automate**. Il publie sur MQTT
> la **température cible et les conditions à suivre** (seuils de marche / d'arrêt du ventilateur selon la
> température extérieure) ainsi qu'une consigne PID indicative (0-100 %) ; l'automate garde seul la décision.

---

## 1. Ce que fait le logiciel

| Étape | Rôle | Module |
|---|---|---|
| Collecter | Se connecte à la passerelle, lit PT01-PT04, HT01 (débit, 0-15 m³/h) et la fréquence pompe / ventilateur (0-50 Hz) | `collectors/gateway_collector.py` |
| Stocker | Écrit un instantané toutes les 15 s dans SQLite, publie sur MQTT | `database/sqlite_client.py` |
| Prédire | Estime la température de l'huile (PT02) à +10 min (2 modèles comparés) | `ml/` |
| Vérifier | Compare chaque prédiction à la mesure réelle à son échéance | `ml/monitor.py` |
| Réguler | Conditions de refroidissement (cible + seuils selon la température extérieure) et PID indicatif | `control/cooling_advice.py`, `control/pid_controller.py` |
| Alerter | E-mails (température critique, pompe sans débit, perte de données, dérive…) | `notifications/email_notifier.py` |
| Protéger | Surveille la taille de la base, sauvegarde puis purge si besoin | `database/storage_manager.py` |

---

## 2. Installation

Prérequis : **Python 3.11 ou plus récent (3.12 recommandé)**, accès au réseau industriel (ou VPN autorisé) pour la passerelle, un
**broker MQTT** (ex. Mosquitto).

```bash
cd Voltaneum_ITNET
python3 -m venv .venv
source .venv/bin/activate            # Windows : .venv\Scripts\activate
pip install -r requirements.txt      # exécution
pip install -r requirements-dev.txt  # + tests et contrôle qualité (facultatif)
```

## 3. Configuration

```bash
cp .env.example .env
chmod 600 .env                       # le fichier contient des mots de passe
```

Ouvrir `.env` et renseigner au minimum :

| Variable | Obligatoire | Rôle |
|---|---|---|
| `VOLTANEUM_USER`, `VOLTANEUM_PASSWORD` | oui | compte (lecture seule) de la passerelle |
| `MQTT_HOST` / `MQTT_PORT` | oui | broker MQTT (`localhost:1883` par défaut) |
| `SMTP_*`, `ALERT_EMAIL_FROM/TO` | non | alertes e-mail : **tout renseigner, ou rien** |
| `OUTDOOR_SENSOR_MODE` | non | `mqtt` (capteur publie), `http` (repli) ou `none` (pas encore de capteur : API météo seule, aucune connexion MQTT pour le capteur) |

Toutes les variables sont commentées dans `.env.example`. Au démarrage, une configuration absente
ou incohérente est **refusée avec un message explicite** (ex. `CRITICAL_TEMP` ≤ `TARGET_TEMP`).

> **Migration depuis l'ancien nom** : les variables `VAULTANIUM_*` deviennent `VOLTANEUM_*`, les
> topics MQTT `vaultanium/…` deviennent `voltaneum/…`, et la base par défaut `vaultanium.db` devient
> `voltaneum.db` (renommer le fichier existant pour conserver l'historique).

Les chemins relatifs (base, modèles, sauvegardes) sont résolus **depuis le dossier du projet**, quel
que soit le répertoire d'où l'on lance le programme.

## 4. Premier lancement : toujours dans cet ordre

**① Simulation hors réseau (6 secondes, aucun équipement touché)**
```bash
python3 simulate.py        # attendu : « 24/24 vérifications réussies »
```
**② Diagnostic réseau (n'envoie aucun identifiant)**
```bash
python3 diagnose_connectivity.py
```
**③ Collecte seule, sans régulation** (recommandé avant tout le reste)
```bash
python3 collect_data.py --hours 72        # 0 = jusqu'à Ctrl+C
python3 check_data.py                     # qualité de la base : trous, variables figées
```
**④ Entraînement des modèles** (après une collecte avec une **vraie** pompe qui tourne)
```bash
python3 train_from_local_db.py            # base de production, horizon 600 s
```
Le résultat est **refusé** (aucun modèle sauvegardé) si les données sont insuffisantes, trop plates, ou
si une variable n'a jamais varié (`insufficient_data`, `insufficient_variance`,
`no_actionable_signal`). C'est voulu : un score parfait sur des données plates ne prouve rien.

**⑤ Exploitation** (ne démarrer qu'après validation avec l'automaticien)
```bash
python3 main.py                           # collecte + contrôle + protection du stockage
python3 -m notifications.email_notifier   # alertes e-mail (processus séparé)
python3 run_model_monitor.py              # suivi prédiction/réalité (processus séparé)
```
Si la passerelle ne répond plus depuis plus de `CONTROL_MAX_DATA_AGE_SECONDS` (120 s), **rien n'est publié**
(les mesures sont périmées) : l'automate doit alors retomber sur sa propre régulation.
Sans modèle entraîné, le contrôle fonctionne en **PID seul** ; les modèles sont rechargés
automatiquement dès que `ml.train` en écrit de nouveaux (pas de redémarrage).

## 5. Services permanents

| Système | Fichiers (dossier `deploy/`) |
|---|---|
| Linux | `voltaneum*.service` → `/etc/systemd/system/`, puis `systemctl enable --now voltaneum` |
| macOS | `com.voltaneum.*.plist` → `~/Library/LaunchAgents/`, puis `launchctl load` |
| Windows | `install-windows-tasks.ps1` (Planificateur de tâches) |

Adapter les chemins (`/opt/voltaneum`) et créer un compte de service dédié. Les services utilisent
`UMask=0077` : fichiers créés lisibles par leur propriétaire seulement.

## 6. Ce que le système publie pour l'automate

| Topic | Contenu | Usage |
|---|---|---|
| `voltaneum/control/cooling_conditions` | température cible, seuil critique, bande extérieure, seuil de marche, seuil d'arrêt, fréquence minimale conseillée, durée de validité | **conditions à suivre** : l'automate compare lui-même la température d'huile à ces seuils |
| `voltaneum/control/setpoint` | consigne PID en % | **indicatif** (désactivable : `PUBLISH_PID_SETPOINT=false`) |

Exemple : par air chaud (≥ 25 °C), cible 35 °C → ventilateur à partir de 33 °C, arrêt sous 31 °C,
fréquence minimale conseillée 40 Hz. Par air froid (< 10 °C) → à partir de 37 °C, arrêt sous 35 °C, 20 Hz.
Température extérieure inconnue → seuils neutres, aucune fréquence imposée (`null`).

> **Valeurs indicatives** : les bandes, décalages, hystérésis et fréquences (`OUTDOOR_BANDS`,
> `FAN_HYSTERESIS_CELSIUS` dans `config.py`) sont des valeurs de départ **à valider avec l'automaticien**.
> Le message porte `valid_for_seconds` : l'automate (ou son pont) doit ignorer un message plus ancien et
> reprendre sa propre régulation. À ma connaissance, le S7-200 SMART n'a pas de client MQTT natif : un
> pont est probablement nécessaire (à confirmer). Voir README §13.

## 7. Capteur de température extérieure

Le **capteur physique** est prioritaire. Mode `mqtt` recommandé : le capteur publie un nombre
(`18.4`) ou un JSON (`{"temperature": 18.4}`) sur `voltaneum/sensors/outdoor_temperature`. Une
mesure plus ancienne que `OUTDOOR_SENSOR_MAX_AGE_SECONDS` (30 min) est ignorée.

**Repli météo** : tant que le capteur n'a pas de valeur (non installé, muet, défaillant), la
température de l'API Open-Meteo (position `VOLTANEUM_LAT` / `VOLTANEUM_LON`) est utilisée à sa
place, à chaque instantané : toutes les `OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS` (15 s), avec `main.py`
comme avec `collect_data.py`. Dès que le capteur
répond, il reprend la main. La colonne `temp_outdoor_source` indique pour chaque ligne la source
(`sensor` ou `weather`). Désactivable avec `OUTDOOR_WEATHER_FALLBACK_ENABLED=false`.

Quand le capteur fournit une valeur, l'API météo sert aussi à **comparer** : un écart > 8 °C sur
3 vérifications déclenche une alerte de vérification, jamais un remplacement de la mesure.

Test sans capteur : `mosquitto_pub -t voltaneum/sensors/outdoor_temperature -m 18.4`

## 8. Alertes e-mail

| Règle | Niveau | Déclenchement |
|---|---|---|
| `critical_temp` | CRITIQUE | PT02 > `CRITICAL_TEMP`, immédiat |
| `pump_without_flow` | CRITIQUE | fréquence > 5 Hz et débit < 0,5 m³/h, 3 mesures consécutives (marche à sec possible) |
| `telemetry_lost` | CRITIQUE | aucune donnée depuis 5 min (y compris depuis le démarrage) |
| `efficiency_drift` | ATTENTION | efficacité hors plage, 3 mesures consécutives |
| `model_disagreement` | ATTENTION | les 2 modèles divergent, 3 prédictions consécutives |
| `outdoor_sensor_weather_mismatch` | ATTENTION | écart capteur/météo, 3 vérifications consécutives |
| `storage_*` | ATTENTION/CRITIQUE | base presque pleine, purge, échec de sauvegarde |

Chaque règle a un délai anti-spam (`ALERT_COOLDOWN_MINUTES`) ; l'envoi utilise STARTTLS avec
**vérification du certificat**. Toute alerte est aussi enregistrée dans la table `alerts_log`.
**Prévoir une personne qui lit réellement ces e-mails** (astreinte).

## 9. Gestion de l'espace disque (par taille, jamais par date)

Quand la base atteint 90 % de `STORAGE_LIMIT_BYTES` (1 Tio par défaut) : e-mail, délai de grâce (1 h),
**sauvegarde compressée et vérifiée** (si elle échoue, ou si le volume de sauvegarde manque d'espace,
rien n'est supprimé), puis suppression des lignes les plus anciennes **jusqu'à 75 % seulement**, puis
compactage. `RETENTION_DAYS` & co. sont des **planchers** : une donnée plus récente n'est jamais
supprimée (alerte `storage_purge_blocked_by_floor`).

**Conservation d'un an** : une sauvegarde compressée (`.db.gz`) n'est jamais supprimée avant
`STORAGE_BACKUP_MIN_AGE_DAYS` (365 jours), quel que soit leur nombre ; les `STORAGE_BACKUP_RETENTION_COUNT`
plus récentes sont de plus toujours gardées. Placer `STORAGE_BACKUP_DIR` **sur un autre disque**, avec au
moins la taille de la base × `STORAGE_BACKUP_FREE_SPACE_FACTOR` en espace libre, et copier aussi les archives
sur un autre support (une archive sur le même disque ne protège pas d'une panne de disque).
Ordre de grandeur : environ 2,1 millions de lignes de télémétrie par an, soit quelques centaines de Mo ;
régler `STORAGE_LIMIT_BYTES` sur la part de disque réellement disponible.

## 10. Sauvegarde et restauration

```python
from database.backup import create_backup, restore_backup, verify_backup
archive = create_backup("voltaneum.db", "backups", retention_count=7, min_age_days=365)   # .db.gz, droits 0600
verify_backup(str(archive))                                  # True si intègre
# Restauration : ARRÊTER main.py d'abord, puis
restore_backup(str(archive), "voltaneum.db")
```

## 11. Contrôle qualité (développeurs)

```bash
python3 -m pytest -q          # tests
python3 -m ruff check .       # style et bugs courants
python3 -m mypy .             # typage
python3 -m bandit -r . -x ./tests -q          # analyse de sécurité
python3 -m pip_audit -r requirements.txt      # vulnérabilités des dépendances
```
Tests réseau réels (facultatifs, depuis le bon réseau) : `RUN_NETWORK_TESTS=1 python3 -m pytest`.

## 12. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| `Configuration invalide : …` au démarrage | variable manquante/incohérente | lire le message, corriger `.env` |
| Aucune ligne en base | passerelle injoignable | `diagnose_connectivity.py`, VPN, identifiants |
| `no_actionable_signal` à l'entraînement | pompe jamais activée pendant la collecte | refaire une collecte avec cycles réels |
| Alerte `telemetry_lost` | flux coupé ou broker arrêté | vérifier broker MQTT et passerelle |
| Pas de consigne publiée | PT02 jamais reçu, ou mesures périmées (> 120 s sans message) | vérifier la collecte (`check_data.py`) et les logs « mesures périmées » |
| Colonnes à `None` en base (« 1 point reconnu ») | noms de points non reconnus | comparer le catalogue de la passerelle à `KEYNAME_TO_COLUMN` |
| « Valeur hors plage pour … ignorée » | plage de `SENSOR_VALID_RANGES` trop étroite, ou capteur défaillant | comparer avec l'affichage de l'automate |
| Alerte `pump_without_flow` | vanne fermée, circuit bouché, désamorçage, capteur de débit en panne | vérifier sur site avant tout redémarrage |
| `storage_purge_blocked_by_floor` | données toutes plus récentes que les planchers | augmenter la capacité ou réduire `RETENTION_DAYS` |

## 13. Limites connues

Données réelles encore insuffisantes (il faut des périodes avec des fréquences de pompe variées) ;
seuils PID, bandes de température extérieure et seuils de la règle pompe sans débit non calibrés sur le
vrai système ; `PUMP` supposé être la fréquence pompe / ventilateur ; pont MQTT vers l'automate à définir ;
protocole exact du capteur extérieur à confirmer à l'installation. Voir `CAHIER_DES_CHARGES_FINAL.md`
§14-15 et `CHANGELOG.md` (M9).
