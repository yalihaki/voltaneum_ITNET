# Guide d'utilisation — Voltaneum / ITNET

Ce guide explique, pas à pas, comment installer, configurer, tester et exploiter le projet.
Pour la conception détaillée, voir `CAHIER_DES_CHARGES_FINAL.md` ; pour l'historique des
corrections, `CHANGELOG.md`.

> **Principe de sécurité à retenir** : Voltaneum **ne commande jamais l'automate**. Il calcule une
> *suggestion* de consigne (0-100 %) et la publie sur MQTT ; l'automate garde seul la décision.

---

## 1. Ce que fait le logiciel

| Étape | Rôle | Module |
|---|---|---|
| Collecter | Se connecte à la passerelle, lit PT01-PT04, HT01 et la fréquence pompe (0-15 Hz) | `collectors/gateway_collector.py` |
| Stocker | Écrit un instantané toutes les 15 s dans SQLite, publie sur MQTT | `database/sqlite_client.py` |
| Prédire | Estime la température de l'huile (PT02) à +10 min (2 modèles comparés) | `ml/` |
| Vérifier | Compare chaque prédiction à la mesure réelle à son échéance | `ml/monitor.py` |
| Réguler | PID + anticipation → suggestion de consigne | `control/pid_controller.py` |
| Alerter | E-mails (température critique, perte de données, dérive…) | `notifications/email_notifier.py` |
| Protéger | Surveille la taille de la base, sauvegarde puis purge si besoin | `database/storage_manager.py` |

---

## 2. Installation

Prérequis : **Python 3.12**, accès au réseau industriel (ou VPN autorisé) pour la passerelle, un
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
| `OUTDOOR_SENSOR_MODE` | non | `mqtt` (capteur publie) ou `http` (repli) |

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
python3 simulate.py        # attendu : « 15/15 vérifications réussies »
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

**⑤ Exploitation**
```bash
python3 main.py                           # collecte + contrôle + protection du stockage
python3 -m notifications.email_notifier   # alertes e-mail (processus séparé)
python3 run_model_monitor.py              # suivi prédiction/réalité (processus séparé)
```
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

## 6. Capteur de température extérieure

Le **capteur physique** est la seule valeur utilisée par le PID et enregistrée en base. Mode `mqtt`
recommandé : le capteur publie un nombre (`18.4`) ou un JSON (`{"temperature": 18.4}`) sur
`voltaneum/sensors/outdoor_temperature`. Une mesure plus ancienne que
`OUTDOOR_SENSOR_MAX_AGE_SECONDS` (30 min) est ignorée : le PID continue sans l'anticipation météo.
L'API météo ne sert qu'à **comparer** : un écart > 8 °C sur 3 vérifications déclenche une alerte de
vérification, jamais un remplacement de la mesure.

Test sans capteur : `mosquitto_pub -t voltaneum/sensors/outdoor_temperature -m 18.4`

## 7. Alertes e-mail

| Règle | Niveau | Déclenchement |
|---|---|---|
| `critical_temp` | CRITIQUE | PT02 > `CRITICAL_TEMP`, immédiat |
| `telemetry_lost` | CRITIQUE | aucune donnée depuis 5 min (y compris depuis le démarrage) |
| `efficiency_drift` | ATTENTION | efficacité hors plage, 3 mesures consécutives |
| `model_disagreement` | ATTENTION | les 2 modèles divergent, 3 prédictions consécutives |
| `outdoor_sensor_weather_mismatch` | ATTENTION | écart capteur/météo, 3 vérifications consécutives |
| `storage_*` | ATTENTION/CRITIQUE | base presque pleine, purge, échec de sauvegarde |

Chaque règle a un délai anti-spam (`ALERT_COOLDOWN_MINUTES`) ; l'envoi utilise STARTTLS avec
**vérification du certificat**. Toute alerte est aussi enregistrée dans la table `alerts_log`.
**Prévoir une personne qui lit réellement ces e-mails** (astreinte).

## 8. Gestion de l'espace disque (par taille, jamais par date)

Quand la base atteint 90 % de `STORAGE_LIMIT_BYTES` (1 Tio par défaut) : e-mail, délai de grâce (1 h),
**sauvegarde vérifiée** (si elle échoue, rien n'est supprimé), puis suppression des lignes les plus
anciennes **jusqu'à 75 % seulement**, puis compactage. `RETENTION_DAYS` & co. sont des **planchers** :
une donnée plus récente n'est jamais supprimée (alerte `storage_purge_blocked_by_floor`).
Placer `STORAGE_BACKUP_DIR` **sur un autre disque** avec au moins la taille de la base en espace libre.

## 9. Sauvegarde et restauration

```python
from database.backup import create_backup, restore_backup, verify_backup
archive = create_backup("voltaneum.db", "backups")          # .db.gz, droits 0600
verify_backup(str(archive))                                  # True si intègre
# Restauration : ARRÊTER main.py d'abord, puis
restore_backup(str(archive), "voltaneum.db")
```

## 10. Contrôle qualité (développeurs)

```bash
python3 -m pytest -q          # tests
python3 -m ruff check .       # style et bugs courants
python3 -m mypy .             # typage
python3 -m bandit -r . -x ./tests -q          # analyse de sécurité
python3 -m pip_audit -r requirements.txt      # vulnérabilités des dépendances
```
Tests réseau réels (facultatifs, depuis le bon réseau) : `RUN_NETWORK_TESTS=1 python3 -m pytest`.

## 11. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| `Configuration invalide : …` au démarrage | variable manquante/incohérente | lire le message, corriger `.env` |
| Aucune ligne en base | passerelle injoignable | `diagnose_connectivity.py`, VPN, identifiants |
| `no_actionable_signal` à l'entraînement | pompe jamais activée pendant la collecte | refaire une collecte avec cycles réels |
| Alerte `telemetry_lost` | flux coupé ou broker arrêté | vérifier broker MQTT et passerelle |
| Pas de consigne publiée | PT02 jamais reçu | vérifier la collecte (`check_data.py`) |
| `storage_purge_blocked_by_floor` | données toutes plus récentes que les planchers | augmenter la capacité ou réduire `RETENTION_DAYS` |

## 12. Limites connues

Données réelles encore insuffisantes (pompe à 0 Hz sur l'automate de test) ; seuils PID non calibrés
sur le vrai système ; protocole exact du capteur extérieur à confirmer à l'installation. Voir
`CAHIER_DES_CHARGES_FINAL.md` §14-15.
