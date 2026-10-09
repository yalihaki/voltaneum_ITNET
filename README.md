# Voltaneum / ITNET

Système de télémétrie industrielle pour échangeur thermique : collecte des capteurs, stockage SQLite, prédiction de température, supervision des erreurs, alertes email et calcul d'une consigne de refroidissement publiée sur MQTT.

> Le logiciel ne commande jamais directement un variateur ou un automate. Il publie uniquement une suggestion de consigne ; l'automate reste décisionnaire.

Le cahier des charges maître du projet est [CAHIER_DES_CHARGES_FINAL.md](CAHIER_DES_CHARGES_FINAL.md). Ce README fournit les commandes et procédures d'exploitation associées.

**Démarrage rapide / démonstration** : `python3 simulate.py` fait tourner tout le
pipeline (collecte, stockage, entraînement, prédiction, régulation, alertes,
sauvegarde) en local en quelques secondes, sans passerelle ni réseau réel —
voir [SIMULATION.md](SIMULATION.md). Pour l'historique des corrections
apportées lors de la dernière revue d'ingénierie, voir
[CHANGELOG.md](CHANGELOG.md). Le mode d'emploi pas à pas est dans [GUIDE_UTILISATION.md](GUIDE_UTILISATION.md).

## 1. Fonctionnement général

```text
Passerelle HinetGateway
        |
        | HTTP login + catalogue / WebSocket
        v
GatewayCollector
        |
        +--> SQLite : telemetry
        +--> MQTT : voltaneum/telemetry
                         |
                         +--> prédiction ML
                         +--> contrôle PID
                         +--> alertes email

SQLite --> monitoring des prédictions et gestion automatique du stockage
```

Le processus principal démarre :

- le collecteur passerelle ;
- la boucle de contrôle PID ;
- le superviseur de stockage SQLite.

Le notifier email et le monitoring des modèles sont des modules indépendants, lançables comme processus séparés lorsqu'un broker MQTT est disponible.

## 2. Prérequis

- macOS, Linux ou Windows 10/11 ;
- Python 3.12 recommandé ;
- accès réseau à la passerelle industrielle ;
- broker MQTT (Mosquitto, par exemple) ;
- compte de lecture sur la passerelle ;
- compte SMTP avec authentification TLS pour les emails.

Pour une simulation sans matériel, seuls Python et les dépendances sont nécessaires. Les appels passerelle, MQTT et SMTP pourront rester indisponibles ; le système fonctionnera alors en mode dégradé et journalisera les erreurs.

## 3. Installation pas à pas

Le dépôt livré reste volontairement propre : les environnements virtuels, caches Python, bases de démonstration et fichiers générés ne sont pas conservés. Il faut recréer l'environnement sur chaque machine d'exploitation.

Depuis la racine du projet :

```bash
cd /Users/yalihaki/Desktop/MQTT_Itnet
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3.1 Windows PowerShell

Depuis PowerShell, dans le dossier du projet :

```powershell
cd C:\Voltaneum\MQTT_Itnet
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Si PowerShell bloque l'activation des scripts, utiliser uniquement pour la session courante :

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
```

Alternative avec l'invite de commandes Windows (`cmd.exe`) :

```bat
py -3.12 -m venv venv
venv\Scripts\activate.bat
python -m pip install -r requirements.txt
```

Vérifier l'installation :

```bash
python -m ruff check .
python -m mypy .
pytest -q
python -m pip_audit -r requirements.txt
```

Le résultat attendu au moment de la rédaction est : `86 passed, 3 skipped`, avec `ruff`, `mypy` et `pip-audit` sans erreur connue.

Après une installation propre, les commandes de validation créent temporairement des caches locaux ; ils peuvent être supprimés avant transfert :

```bash
rm -rf .pytest_cache .mypy_cache .ruff_cache __pycache__
find . -type d -name __pycache__ -prune -exec rm -rf {} +
```

Sous Windows PowerShell :

```powershell
Remove-Item -Recurse -Force .pytest_cache, .mypy_cache, .ruff_cache -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
```

## 4. Configuration

Créer le fichier local contenant les secrets :

```bash
cp .env.example .env
chmod 600 .env
```

Sous Windows :

```powershell
Copy-Item .env.example .env
notepad .env
icacls .env /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

`icacls` remplace `chmod 600` : le fichier `.env` doit être accessible uniquement au compte qui exécute Voltaneum et aux administrateurs autorisés.

Modifier `.env` avec les valeurs de l'installation. Ne jamais committer ce fichier.

### 4.1 Passerelle

```env
VOLTANEUM_USER=compte_lecture
VOLTANEUM_PASSWORD=mot_de_passe
GATEWAY_HOST=10.0.1.33
MANAGE_API_URL=http://10.0.1.33/ManageApi/Base.Auth.php
IOT_API_URL=http://10.0.1.33:8001/iot/query_all_points
WS_URL=ws://10.0.1.33:91
SENSOR_DRIVER_NODE=idospV1_1
```

Les URLs HTTP/WebSocket de la passerelle sont celles du réseau industriel local. Cette limitation doit rester documentée et ne doit pas être reproduite pour des services exposés sur Internet.

### 4.2 MQTT

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

Les identifiants MQTT sont appliqués par le client. Le démarrage sans identifiant est possible si le broker est configuré ainsi, mais l'accès anonyme doit être un choix explicite du réseau d'exploitation. Pour un broker distant, activer TLS et fournir le certificat CA :

```env
MQTT_TLS_ENABLED=true
MQTT_TLS_CA_CERT=/chemin/vers/ca.crt
MQTT_TLS_INSECURE=false
```

`MQTT_TLS_INSECURE=true` est refusé par la validation de configuration.

### 4.3 Météo

```env
VOLTANEUM_LAT=48.8566
VOLTANEUM_LON=2.3522
```

La température extérieure vient d'un capteur physique indépendant (pas Open-Meteo, pas un point de l'automate) : soit il publie lui-même sur MQTT (`OUTDOOR_SENSOR_MODE=mqtt`, recommandé), soit il expose une petite API HTTP interrogée à chaque cycle (`OUTDOOR_SENSOR_MODE=http`). En cas d'indisponibilité, la dernière valeur connue est utilisée ; si aucune valeur n'existe encore, le cycle de contrôle est reporté. Voir `collectors/outdoor_sensor_collector.py`.

### 4.4 Email SMTP

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=utilisateur_smtp
SMTP_PASSWORD=mot_de_passe_smtp
ALERT_EMAIL_FROM=voltaneum@example.com
ALERT_EMAIL_TO=destinataire@example.com
```

Le mot de passe SMTP reste uniquement dans `.env`. TLS est utilisé par le notifier.

## 5. Démarrer le système

Initialiser la base puis démarrer l'application :

```bash
source venv/bin/activate
python main.py
```

Ou :

```bash
python -m main
```

Arrêter proprement avec `Ctrl+C`. L'application demande l'arrêt du collecteur, attend ses threads, arrête le superviseur de stockage et déconnecte MQTT.

Au premier démarrage sans modèles entraînés, la collecte démarre tout de même. La boucle de contrôle fonctionne alors sans feedforward ML.

### 5.1 Diagnostiquer la connexion

Avant de lancer le système :

```bash
python diagnose_connectivity.py
```

Ce diagnostic ne transmet aucun secret et n'envoie aucune commande industrielle. Il teste seulement les ports MQTT, HTTP de la passerelle et WebSocket.

Si MQTT indique `localhost:1883: Connection refused`, aucun broker local n'est démarré. Installez et démarrez un broker local pour les essais, ou configurez `MQTT_HOST` vers un broker autorisé.

Si les adresses `10.x.x.x`, `172.16.x.x` ou `192.168.x.x` sont injoignables, connectez l'ordinateur au réseau industriel ou au VPN officiel. Ne rendez jamais la passerelle publique et ne contournez pas le pare-feu.

### 5.2 Essai local sans passerelle

Pour tester le stockage et l'entraînement sans réseau industriel :

```bash
python generate_test_data.py --db voltaneum_test.db --rows 4000
python train_from_local_db.py voltaneum_test.db 600 /tmp/voltaneum_test_models
```

Le générateur crée des cycles de pompe synthétiques. Il refuse d'écraser une base existante sans `--force`. Les modèles de test sont écrits dans `/tmp` et ne remplacent pas ceux du dossier `models/`.

## 6. Vérifier la base SQLite

La base par défaut est `voltaneum.db`. Elle contient notamment :

- `telemetry` : instantané des capteurs ;
- `predictions_log` : prédictions, valeurs réelles et erreurs ;
- `alerts_log` : alertes, emails et événements de stockage.

Inspecter rapidement la base :

```bash
# Base configurée par défaut dans .env
python check_data.py

# Base précise à inspecter
python check_data.py voltaneum_test.db
```

Ou avec SQLite :

```bash
sqlite3 voltaneum.db
.tables
.schema telemetry
SELECT COUNT(*) FROM telemetry;
SELECT * FROM telemetry ORDER BY timestamp DESC LIMIT 5;
.quit
```

Le fichier de base doit avoir des permissions restrictives :

```bash
chmod 600 voltaneum.db
```

Sous Windows :

```powershell
icacls voltaneum.db /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

## 7. Collecte et trous de données

### 7.1 Collecte seule pour constituer les données ML

Utiliser `collect_data.py` avant d'activer le contrôle. Ce script démarre uniquement le collecteur passerelle et écrit dans SQLite : il ne démarre pas le PID et ne publie aucune consigne.

```bash
# Jusqu'à Ctrl+C
python collect_data.py --db voltaneum.db

# Exemple limité à 24 heures, snapshots toutes les 15 secondes
python collect_data.py --db voltaneum.db --hours 24 --interval 15
```

Arrêter avec `Ctrl+C`, puis vérifier les lignes, les trous, les plages et les cycles de pompe avant tout entraînement.

Le collecteur :

1. s'authentifie auprès de la passerelle ;
2. récupère le mapping des points `K0`, `K1`, etc. ;
3. écoute le WebSocket ;
4. valide les valeurs et rejette les valeurs aberrantes ;
5. conserve les dernières valeurs connues sous verrou ;
6. écrit des instantanés dans SQLite ;
7. publie la télémétrie sur MQTT ;
8. se reconnecte avec un backoff en cas de coupure.

Les trous ne sont pas masqués. L'entraînement utilise les vrais horodatages et refuse de considérer un segment interrompu comme continu.

Analyser la qualité d'une base existante :

```bash
python - <<'PY'
import sqlite3
import pandas as pd

with sqlite3.connect("voltaneum.db") as connection:
    data = pd.read_sql_query(
        "SELECT * FROM telemetry ORDER BY timestamp", connection
    )

data["timestamp"] = pd.to_datetime(data["timestamp"])
print("lignes:", len(data))
print("de:", data["timestamp"].min())
print("a:", data["timestamp"].max())
print("valeurs manquantes:\n", data.isna().sum())
print("plus grand trou:", data["timestamp"].diff().max())
PY
```

## 8. Entraîner les modèles

### 8.1 Entraînement depuis SQLite

Pour l'horizon de production configuré, généralement 600 secondes :

```bash
python -m ml.train
```

Le programme :

- charge les données de SQLite ;
- construit la cible à partir des horodatages réels ;
- refuse les segments trop courts ou trop troués ;
- utilise un split chronologique 80/20 ;
- entraîne Random Forest et Gradient Boosting ;
- compare les deux modèles à la persistance ;
- écrit les modèles et `training_report.json` dans `models/`.

### 8.2 Entraînement direct sur une base locale

```bash
python train_from_local_db.py voltaneum.db 600
```

Le premier argument est le chemin de la base et le second l'horizon en secondes. Un troisième argument permet de choisir explicitement le dossier de sortie des modèles ; utilisez-le pour éviter d'écraser les modèles de production pendant un test :

```bash
python train_from_local_db.py voltaneum_test.db 600 /tmp/voltaneum_test_models
```

Le rapport produit contient `status: ok`, requis par le chargeur de modèles. Si les données sont insuffisantes, aucun modèle ne doit être utilisé en production.

Exemple pour un test plus court :

```bash
python train_from_local_db.py voltaneum.db 120
```

### 8.3 Interpréter le résultat

Un modèle n'est pas validé parce qu'il produit un fichier `.joblib`. Vérifier :

- `status` doit être `ok` ;
- `n_samples` doit être suffisant ;
- `beats_persistence` doit être `true` ;
- la température cible doit réellement varier ;
- les variables d'entrée doivent couvrir des cycles de fonctionnement ;
- la pompe ne doit pas être constamment à zéro sur toute la période.

Afficher le rapport :

```bash
python -m json.tool models/training_report.json
```

Une base où `pt02`, `freq_pompe` ou `ht01` restent constants peut valider le pipeline, mais pas la qualité industrielle du modèle.

### 8.4 Validation multi-horizon

Entraîner les horizons `30`, `60`, `120`, `300` et `600` secondes dans des dossiers séparés :

```bash
python train_all_horizons.py voltaneum.db /tmp/voltaneum_grid_real
```

Le rapport global est écrit dans `horizons/grid_report.json`. Pour un essai isolé :

```bash
python generate_test_data.py --db /tmp/voltaneum_grid.db --rows 4000
python train_all_horizons.py /tmp/voltaneum_grid.db /tmp/voltaneum_grid_synthetic
```

Chaque horizon doit être interprété séparément. Sur la base capteur actuelle, les horizons courts ne sont pas validés si la persistance obtient déjà un RMSE nul ; cela indique une cible trop stable, pas une performance ML.

## 9. Prédiction et monitoring

Quand `models/random_forest.joblib`, `models/gradient_boosting.joblib` et `models/training_report.json` existent, l'application charge les deux modèles et publie :

- la prédiction Random Forest ;
- la prédiction Gradient Boosting ;
- la prédiction du meilleur modèle ;
- leur désaccord ;
- l'horizon utilisé.

Le monitoring des prédictions peut être lancé séparément :

```bash
python - <<'PY'
from ml.monitor import run_monitor_loop

run_monitor_loop()
PY
```

Il complète les prédictions échues et publie le RMSE glissant sur `voltaneum/model_health`. Arrêter avec `Ctrl+C`.

## 10. Alertes email

Lancer le notifier dans un terminal séparé :

```bash
source venv/bin/activate
python -m notifications.email_notifier
```

Le notifier écoute les topics de télémétrie, prédiction et consigne. Il gère :

- température critique ;
- perte de télémétrie ;
- dérive d'efficacité ;
- désaccord entre modèles ;
- cooldown anti-spam ;
- confirmation sur plusieurs cycles pour les alertes d'attention ;
- journalisation dans `alerts_log`.

Si SMTP est indisponible, l'échec est journalisé et ne stoppe pas la collecte.

## 11. Gestion automatique du stockage

Le superviseur est démarré automatiquement par `main.py`. Il surveille la taille cumulée de :

- `voltaneum.db` ;
- `voltaneum.db-wal` ;
- `voltaneum.db-shm`.

Configuration par défaut :

```env
STORAGE_LIMIT_BYTES=1099511627776
STORAGE_WARNING_RATIO=0.90
STORAGE_PURGE_TARGET_RATIO=0.75
STORAGE_GRACE_PERIOD_SECONDS=3600
STORAGE_CHECK_INTERVAL_SECONDS=300
STORAGE_PURGE_BATCH_SIZE=1000
```

Le comportement est le suivant :

1. à 90 % de la limite, un email d'avertissement est envoyé ;
2. l'événement est enregistré dans `alerts_log` ;
3. le système attend le délai de grâce ;
4. si la base reste trop volumineuse, les télémétries les plus anciennes sont supprimées par lots ;
5. SQLite est checkpointé et compacté ;
6. un email de confirmation est envoyé avec le nombre de lignes supprimées ;
7. la collecte continue pendant toute l'opération, sous réserve du verrouillage SQLite normal.

Pour tester sans attendre 1 Tio, utiliser temporairement dans `.env` une limite basse sur une base de test. Ne jamais tester une purge automatique directement sur la seule base de production sans sauvegarde.

### 11.1 Sauvegarde avant purge

Avant toute purge, le système crée une copie SQLite cohérente, la compresse en `.db.gz`, vérifie `PRAGMA integrity_check`, conserve les dernières versions configurées et annule la purge si la sauvegarde est invalide.

```env
STORAGE_BACKUP_DIR=backups
STORAGE_BACKUP_RETENTION_COUNT=7
```

Test manuel :

```bash
python - <<'PY'
from database.backup import create_backup, restore_backup, verify_backup

archive = create_backup("voltaneum.db", "backups", retention_count=7)
assert verify_backup(str(archive))
restore_backup(str(archive), "/tmp/voltaneum-restored.db")
print(archive)
PY
```

## 12. Topics MQTT

| Topic | Contenu |
|---|---|
| `voltaneum/telemetry` | instantané des capteurs |
| `voltaneum/prediction` | prédictions des deux modèles |
| `voltaneum/control/setpoint` | suggestion de consigne, jamais une écriture automate |
| `voltaneum/model_health` | RMSE glissant et état du monitoring |
| `voltaneum/sensors/outdoor_temperature` | valeur publiée par le capteur extérieur physique (mode `mqtt`) |
| `voltaneum/sensors/outdoor_consistency` | écart mesuré entre le capteur extérieur et l'API météo (vérification uniquement, jamais une substitution) |

Observer les messages avec Mosquitto :

```bash
mosquitto_sub -h localhost -t 'voltaneum/#' -v
```

## 13. Contrôle PID et sécurité

Le PID calcule une suggestion entre 0 et 100 %.

- température critique : bypass de sécurité à 100 % ;
- données `NaN` ou infinies : comportement sûr, sans contamination de l'état ;
- météo indisponible : dernière valeur connue ou cycle reporté ;
- modèle absent ou défaillant : fonctionnement PID dégradé ;
- aucune fonction du projet n'écrit directement sur un actionneur.

Les constantes `PID_KP`, `PID_KI`, `PID_KD`, `TARGET_TEMP` et `CRITICAL_TEMP` doivent être calibrées avec l'installation réelle et validées par l'exploitant industriel.

## 14. Tests et qualité

Suite complète :

```bash
pytest -q
```

Analyse statique :

```bash
python -m ruff check .
python -m mypy .
```

Audit des dépendances :

```bash
python -m pip_audit -r requirements.txt
```

Les tests couvrent la base SQLite, les trous de collecte, le collecteur, la météo, l'entraînement, la prédiction, le monitoring, le PID, les emails, le cycle de vie et le stockage automatique.

## 15. Exploitation recommandée

### Conseil à donner à l'installateur avant démarrage

1. Confirmer le réseau industriel ou le VPN autorisé et ne pas exposer la passerelle sur Internet.
2. Confirmer les `KeyName` réels et le sens de chaque point `PT01` à `PT04`, `HT01` et fréquence.
3. Vérifier que l'utilisateur passerelle est strictement en lecture seule.
4. Vérifier le sens des valeurs et les unités avec l'automaticien avant d'entraîner un modèle.
5. Installer et tester le broker MQTT, ses identifiants et ses certificats TLS si nécessaire.
6. Tester l'envoi SMTP avec une adresse de test et vérifier le cooldown.
7. Faire une sauvegarde de la base avant la campagne.
8. Lancer uniquement `collect_data.py` pendant la campagne initiale ; ne pas activer le PID au premier démarrage.
9. Prévoir au minimum 72 heures pour vérifier la collecte, 14 jours pour un premier modèle exploitable et idéalement 30 jours pour une validation industrielle.
10. Faire fonctionner la pompe à plusieurs niveaux et enregistrer des démarrages, arrêts et changements de charge.
11. Ne valider le modèle que si `PT02` varie réellement et si les modèles battent la persistance à chaque horizon retenu.
12. Après validation, activer les services secondaires puis `main.py`, en gardant l'automate comme seul décideur physique.

### Première installation

1. Installer Python et créer le virtualenv.
2. Installer `requirements.txt`.
3. Copier `.env.example` vers `.env`.
4. Renseigner les secrets et les adresses réelles.
5. Vérifier les permissions de `.env`.
6. Vérifier la connectivité passerelle et MQTT.
7. Lancer `python collect_data.py --db voltaneum.db` en mode collecte seule.
8. Laisser fonctionner assez longtemps pour obtenir plusieurs cycles réels de pompe.
9. Contrôler les trous et les valeurs dans `voltaneum.db`.
10. Entraîner les modèles avec `python -m ml.train` ou `python train_all_horizons.py`.
11. Vérifier `training_report.json` contre la persistance.
12. Activer le notifier email et le monitoring des modèles.
13. Démarrer `python main.py` seulement après validation du rapport ML.

### Avant mise en production

- tester une coupure passerelle ;
- tester une coupure MQTT ;
- tester une panne SMTP ;
- tester un disque presque plein ;
- vérifier que la purge est précédée d'une sauvegarde ;
- vérifier les seuils PID avec l'exploitant ;
- vérifier que l'automate reste le seul à appliquer une consigne ;
- exécuter `pytest`, `ruff`, `mypy` et `pip-audit` ;
- documenter les valeurs réelles des points `KeyName` de la passerelle.

### Contenu conservé dans le projet

- `voltaneum.db` : base réelle fournie par le capteur ; elle est conservée comme donnée de référence et protégée en `0600` ;
- `models/` : modèles actuellement entraînés ;
- `tests/` : tests automatisés ;
- `Voltaneum_Cahier_des_charges.pdf`, `mqtt_final.md` et `itnet_mqtt.md` : spécifications et documentation ;
- les environnements virtuels et caches sont recréés localement et ne doivent pas être copiés sur le terrain.

Avant tout transfert, vérifier :

```bash
chmod 600 .env voltaneum.db
python diagnose_connectivity.py
```

Le test terrain doit être réalisé depuis le réseau industriel ou le VPN officiellement autorisé. Aucun contournement réseau n'est prévu.

### Services permanents

Choix retenu : `main.py` supervise la collecte, le contrôle, le stockage et ses composants internes ; le notifier email et le monitoring ML sont des processus indépendants supervisés par le système d'exploitation.

- Linux : copier les unités `deploy/voltaneum*.service` vers `/etc/systemd/system/`, adapter le chemin et l'utilisateur, puis exécuter `sudo systemctl daemon-reload` et `sudo systemctl enable --now voltaneum.service voltaneum-email-notifier.service voltaneum-model-monitor.service`.
- macOS : adapter les chemins des fichiers `deploy/com.voltaneum*.plist`, créer `logs/`, puis les charger avec `launchctl bootstrap`.
- Windows : depuis PowerShell administrateur, exécuter `.\deploy\install-windows-tasks.ps1 -ProjectRoot C:\Voltaneum` avec un compte de service et des ACL NTFS limitées.

## 15.1 Différences entre systèmes

Le code Python, SQLite, MQTT, WebSocket et les tests sont communs aux systèmes. Seuls l'installation, les chemins, les permissions et le démarrage automatique changent :

| Sujet | macOS / Linux | Windows |
|---|---|---|
| Python | `python3` | `py -3.12` ou `python` |
| Activation | `source venv/bin/activate` | `venv\\Scripts\\Activate.ps1` |
| Copier `.env` | `cp .env.example .env` | `Copy-Item .env.example .env` |
| Permissions secrets | `chmod 600 .env voltaneum.db` | `icacls` avec accès limité au compte de service |
| Chemins | `/opt/voltaneum/voltaneum.db` | `C:\\Voltaneum\\voltaneum.db` |
| MQTT local | Mosquitto service ou daemon | Mosquitto service Windows |
| Démarrage automatique | `systemd` Linux ou `launchd` macOS | Planificateur de tâches ou NSSM |
| Diagnostic | `python diagnose_connectivity.py` | `python diagnose_connectivity.py` |

Les chemins configurés dans `.env` doivent utiliser le format du système. Éviter les chemins relatifs pour une installation comme service ; utiliser un chemin absolu vers la base SQLite et le dossier des modèles.

Pour un service Windows, utiliser un compte dédié sans droits administrateur, avec accès uniquement au dossier du projet, à la base et aux certificats nécessaires. Pour macOS/Linux, utiliser également un compte de service dédié et limiter les permissions du dossier contenant `.env`, `voltaneum.db` et les certificats MQTT.

## 16. Limites connues

- La qualité du modèle dépend directement de données avec des cycles actifs et suffisamment peu de trous.
- Les noms de points de la passerelle doivent être confirmés sur l'installation réelle.
- Le topic MQTT exact (ou l'URL HTTP) et le format du payload du capteur extérieur physique restent à confirmer une fois le matériel choisi et raccordé (voir `config.py`, `OUTDOOR_SENSOR_MODE`).
- Les paramètres PID et les seuils d'efficacité sont des valeurs initiales à calibrer.
- La passerelle utilise actuellement HTTP/WebSocket sur le réseau industriel local.
- La purge automatique protège la disponibilité, mais elle ne remplace pas une stratégie de sauvegarde et d'archivage.
- La validation finale nécessite un essai contrôlé sur l'équipement réel.

### État actuel et niveaux d'acceptation

Le logiciel et ses tests ciblés sont opérationnels pour les essais terrain. La
validation industrielle reste conditionnée par une campagne réelle : 72 h
minimum pour le pipeline, 14 jours pour un premier modèle exploitable
(20–30 cycles actifs), puis 30 jours avant décision industrielle. Une base
volumineuse sans cycles de pompe ni variation de `PT02` ne compte pas.

L'accès à la passerelle `10.0.1.33` et au broker d'entreprise est limité au
réseau industriel ou au VPN officiellement autorisé. Cette contrainte réseau
est une limite d'environnement, jamais une raison d'exposer la passerelle ou
de contourner un pare-feu.

Les niveaux sont donc : **logiciel** (tests, résilience et stockage verts),
**terrain** (collecte continue, cycles actifs et données compatibles avec
`MAX_GAP_SECONDS`) et **industriel** (modèle meilleur que la persistance,
seuils validés par l'exploitant et recette sur équipement réel).

## 17. Arborescence utile

```text
config.py
main.py
collectors/
  gateway_collector.py
  outdoor_sensor_collector.py
  outdoor_consistency_checker.py
  weather_collector.py        # API météo : référence de COMPARAISON uniquement
database/
  sqlite_client.py
  storage_manager.py
  backup.py
ml/
  train.py
  predictor.py
  monitor.py
  multi_horizon.py
control/
  pid_controller.py
notifications/
  email_notifier.py
messaging/
  mqtt.py                     # création commune des clients MQTT (auth + TLS vérifié)
deploy/                       # services systemd / launchd / Planificateur de tâches
tests/
models/
.env.example
requirements.txt
```

## 18. Résumé rapide des commandes

```bash
# Installation
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env

# Vérification
pytest -q
python -m ruff check .
python -m mypy .
python -m pip_audit -r requirements.txt

# Production
python main.py

# Entraînement
python -m ml.train
python train_from_local_db.py voltaneum.db 600

# Observation
python check_data.py
mosquitto_sub -h localhost -t 'voltaneum/#' -v

# Services optionnels
python -m notifications.email_notifier
python - <<'PY'
from ml.monitor import run_monitor_loop
run_monitor_loop()
PY
```
