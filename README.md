# Voltaneum / ITNET

Supervision et aide à la régulation d'un refroidissement par immersion (cuve de liquide diélectrique → échangeur → boucle de refroidissement → dry cooler) :
collecte des mesures de la passerelle, historique SQLite, alertes, prédiction de PT02,
conditions de refroidissement et consigne PID **informatives** pour l'automate, interface web.

> Voltaneum ne commande jamais l'automate ni le variateur. Il publie des conditions et une
> consigne indicative ; l'automate (ou son pont) reste seul décisionnaire.

Le cahier des charges complet est [CAHIER_DES_CHARGES_FINAL.md](CAHIER_DES_CHARGES_FINAL.md).

## 1. Démarrer : une seule commande

Sur le contrôleur Linux (utilisateur normal, aucun droit root), avec Python 3.11 ou plus récent :

```bash
cd ~/voltaneum_ITNET
./voltaneum
```

Au premier lancement, `.env` est créé depuis `.env.example` : renseigner au minimum
`VOLTANEUM_USER` et `VOLTANEUM_PASSWORD` (et l'e-mail des alertes), puis relancer `./voltaneum`.

La commande crée l'environnement Python, installe les dépendances, vérifie la configuration,
arrête une éventuelle ancienne installation du projet, installe la **relance automatique**
(crontab de l'utilisateur : redémarrage après un plantage ou un redémarrage du serveur) et
démarre tout en arrière-plan. Mise à jour : `git pull && ./voltaneum restart`.

## 2. Commandes

| Commande | Rôle |
|---|---|
| `./voltaneum` | démarre tout en arrière-plan (= `start`) |
| `./voltaneum status` | état, dernières mesures, alertes du jour, modèle proposé, journal |
| `./voltaneum stop` / `restart` | arrête / redémarre (relance automatique comprise) |
| `./voltaneum logs` | journal en direct (`logs/voltaneum.log`) |
| `./voltaneum watch` | **supervision en direct dans le terminal** (sans navigateur) |
| `./voltaneum run` | tout au premier plan (Ctrl+C) |
| `./voltaneum diagnose` | configuration, passerelle, broker, météo, e-mail, base, modèle (lecture seule) |
| `./voltaneum data` | résumé des mesures (min / max / moyenne, mesures figées) et 10 dernières lignes (remplace `check_data.py`) ; options `--rows 50`, `--hours 24`, `--csv export.csv` |
| `./voltaneum export` | **un fichier CSV par élément** (mesures, consignes PID, alertes, prédictions, ré-entraînements) dans `exports/` ; `--days 7` (défaut), `--hours 24`, `--all`, `--only mesures,alertes` |
| `./voltaneum model` | modèle proposé : `accept`, `reject --note "…"`, `rollback` |
| `./voltaneum restore` | lister les sauvegardes ; `restore <nom>` pour en restaurer une (après `stop`) |
| `./voltaneum retrain` | ré-entraîner maintenant (proposition seulement) |
| `./voltaneum demo` | interface sur données fictives, sans passerelle (port 8090) |
| `./voltaneum test` | tests et contrôles qualité |

## 3. Supervision

### 3.1 Dans un terminal (contrôleur joint seulement par SSH / VPN)

```bash
./voltaneum watch
```

Mesures, régulation PID (consigne, termes, envoi MQTT), prédiction, conditions de
refroidissement, courbes de la dernière heure en caractères et alertes des 24 h, rafraîchis
toutes les 5 s (Ctrl+C pour quitter). Options : `--minutes 360` (courbes sur 6 h),
`--interval 10`, `--once` (une seule fois). Lecture seule.

### 3.2 Dans un navigateur

`http://localhost:8080` sur le contrôleur. Depuis un autre poste, par tunnel SSH :

```bash
ssh -N -L 8080:localhost:8080 itnet@controller
```

Écrans : **Supervision** (mesures en direct, synoptique, tendance et prévision de PT02, consigne
PID expliquée, prédiction, conditions de refroidissement, alertes, état du système),
**Historique** (période au choix, **exports CSV** des cinq éléments sur cette période), **Alertes**
(export), **PID** (réglages, messages MQTT exacts, consignes envoyées, export), **Modèles**
(ré-entraînement, erreur en production, exports), **Réglages**.

Exports au format Excel français : séparateur « ; », virgule décimale, UTF-8 avec BOM, heure UTC
et heure locale sur chaque ligne.

L'interface est en **lecture seule** (base ouverte en mode `ro`) et sans authentification :
elle écoute sur `127.0.0.1` par défaut (`DASHBOARD_HOST`), à n'ouvrir au réseau que sur un réseau de confiance.

## 4. Fonctionnement

Un seul processus, dont chaque composant est supervisé et relancé s'il s'arrête :

- **collecte** : login, catalogue et flux WebSocket de la passerelle ; instantané toutes les 15 s ;
  une valeur hors plage, non finie ou trop ancienne (`SENSOR_MAX_AGE_SECONDS`) est enregistrée comme absente ;
- **régulation** (60 s) : prédiction (si un modèle validé est installé), PID, conditions de
  refroidissement selon l'air extérieur ; rien n'est calculé ni publié sur des mesures périmées
  ou sans PT02, et le PID repart de zéro après une telle coupure ;
- **alertes** : évaluées dans le processus, sans broker (voir §5) ;
- **sauvegarde quotidienne** vérifiée de la base (`backups/daily`, 7 gardées) ;
- **stockage** : sauvegarde vérifiée puis purge des plus anciennes lignes quand la base approche
  de sa limite effective (jamais plus que 80 % du disque réel, `STORAGE_DISK_MAX_SHARE`) ;
- **monitoring ML** : chaque prédiction est comparée à la mesure réelle à son échéance ;
- **ré-entraînement nocturne** (`RETRAIN_AT`, 3 h 15) : sous-processus basse priorité (voir §6) ;
- **interface web**.

**Mode dégradé.** Sans broker MQTT, tout fonctionne sauf la publication vers l'automate. Sans
modèle, le PID tourne sans anticipation. Sans e-mail, les alertes restent dans l'interface. Sans
capteur extérieur (cas actuel, `OUTDOOR_SENSOR_MODE=none`), la température vient de l'API météo
(interrogée toutes les 10 min, la source est enregistrée).

**Mesures et physique.** Les équipements chauffent le **liquide diélectrique** de la cuve ; la pompe
l'envoie à l'échangeur, où un **liquide de refroidissement** emporte la chaleur vers le dry cooler,
qui la rend à l'air. PT01 / PT02 = diélectrique (PT02 = bain, grandeur régulée et prédite ; PT01 =
retour refroidi), PT03 / PT04 = refroidissement (PT03 vers le dry cooler, PT04 en sortie du dry
cooler). Circuits confirmés par l'automaticien ; côté chaud / froid déduit des mesures (PT02 > PT01 >
PT03 > PT04), à confirmer. Détail complet : cahier des charges §2.2 bis. S'y ajoute la **puissance
dissipée estimée** (débit HT01 × écart PT02 − PT01 × propriétés du diélectrique), affichée comme
estimation tant que débit, fluide et sondes ne sont pas confirmés (`THERMAL_POWER_CONFIRMED`).

## 5. Alertes

| Règle | Niveau | Déclenchement |
|---|---|---|
| `critical_temp` | CRITIQUE | PT02 > `CRITICAL_TEMP` (immédiat) |
| `dielectric_critical_temp` | CRITIQUE | PT01 (diélectrique ; PT02 relève de `CRITICAL_TEMP`) > `DIELECTRIC_CRITICAL_TEMP` (immédiat) |
| `pump_without_flow` | CRITIQUE | fréquence > 5 Hz et débit < 0,5 m³/h, confirmé |
| `telemetry_lost` | CRITIQUE | aucune mesure depuis `TELEMETRY_TIMEOUT_SECONDS` |
| `oil_sensor_missing` | ATTENTION | PT02 absente alors que la passerelle émet (aucune consigne publiée) |
| `oil_sensor_frozen` | ATTENTION | PT02 strictement constante depuis `SENSOR_FROZEN_ALERT_MINUTES` alors que PT01 a bougé d'au moins 1 °C |
| `cooling_ineffective` | ATTENTION | consigne au maximum et PT02 ne baisse toujours pas (ventilateur, dry cooler ou automate à vérifier) |
| `model_disagreement` | ATTENTION | les deux modèles divergent (la prédiction n'est alors pas utilisée) |
| `outdoor_sensor_weather_mismatch` | ATTENTION | capteur extérieur et météo très différents |
| `storage_*` | ATTENTION / CRITIQUE | stockage presque plein, purge, sauvegarde impossible (dont la quotidienne) |

Chaque alerte est journalisée une fois (onglet Alertes) et envoyée par e-mail si SMTP est
configuré, avec un délai entre deux e-mails d'une même règle (un jour pour PT02 figée). Les
e-mails partent d'un thread dédié : un serveur SMTP lent ne retarde jamais la collecte. La règle d'efficacité de
l'échangeur est désactivée tant que sa formule n'est pas validée (`EFFICIENCY_ALERT_ENABLED`).

## 6. Prédiction et ré-entraînement

Sans modèle installé, un premier ré-entraînement a lieu 5 min après le démarrage (pas plus d'une
fois par 24 h). Ensuite, chaque nuit, les modèles sont ré-entraînés sur les 90 derniers jours pour plusieurs horizons
(10 min à 24 h). L'horizon **le plus long qui reste fiable** est **proposé**, jamais installé seul :
il faut que l'erreur soit au moins 20 % plus faible que la persistance (« la température ne bouge
pas »), sous 0,5 °C, et mesurée sur une période de test d'au moins 3 fois l'horizon.

```bash
./voltaneum model            # voir la proposition (aussi dans l'onglet Modèles)
./voltaneum model accept     # l'installer ; l'ancien modèle est sauvegardé
./voltaneum model rollback   # revenir au précédent
```

Les modèles reçoivent la valeur actuelle de PT02 en plus des autres mesures. Un modèle qui ne
bat pas la persistance n'est jamais sauvegardé ; un seul ré-entraînement tourne à la fois. Changer d'horizon change
l'anticipation du PID (pensée pour 10 min) : la proposition le signale. Au-delà de quelques
heures, le modèle ne connaît ni la charge à venir ni la météo prévue.

## 7. Régulation et messages MQTT

**Régulation par étapes.** Le PID est calculé chaque minute, mais un message n'est envoyé qu'aux
étapes : rien au repos ; **activation** quand PT02 dépasse la cible, monte, ou est prédite au-dessus
(air extérieur seul si l'air est sous 10 °C, sinon ventilateur) ; puis **10 min d'attente** pour voir
l'effet ; **maintien** si PT02 baisse, sinon **renforcement** (air seul → ventilateur → +20 % →
100 %, puis alerte « refroidissement insuffisant ») ; **fin** quand PT02 revient 1 °C sous la cible ;
**critique** (100 %) immédiatement au-delà de 55 °C. Détail : cahier des charges §6.4.

Messages (QoS 0, non retenus, quand un broker est joignable) :

- `voltaneum/control/cooling_conditions` (contrat principal) : étape (`phase`, `event`,
  `command_percent`, `cooling_mode` et sa raison), cible, seuil critique, bande extérieure, seuils
  de marche / arrêt du ventilateur, fréquence minimale conseillée, validité 13 min ;
- `voltaneum/control/setpoint` : `{"percent", "pid_percent", "mode", "phase", "event", "ts"}` ;
- `voltaneum/telemetry`, `voltaneum/prediction`, `voltaneum/model_health`.

Le format exact et les derniers messages envoyés sont dans l'onglet **PID**. Gains par défaut :
Kp = 10 %/°C, Ki = 0,02 (temps d'intégration 500 s), Kd = 0, point de départ à régler sur site.
Bandes extérieures, hystérésis et seuils sont **indicatifs, à valider avec l'automaticien**.

## 8. Configuration

Tous les réglages sont dans `.env` (modèle commenté : `.env.example`), lus et vérifiés au
démarrage : une configuration incohérente est refusée avec un message qui nomme la variable.
Après modification : `./voltaneum restart`.

## 9. Sécurité

- aucune écriture vers l'automate : seuls des points en lecture seule de la passerelle sont lus ;
- aucun identifiant dans le code ; `.env` en `0600`, jamais versionné ;
- MQTT : authentification et TLS vérifié configurables (un broker distant sans TLS est signalé) ;
- e-mail en STARTTLS vérifié ; modèles en `0600` dans un dossier `0700` ;
- la passerelle ne parle qu'en HTTP / WebSocket non chiffrés : réseau industriel ou VPN uniquement.

## 10. Dépannage

| Symptôme | Action |
|---|---|
| `./voltaneum status` : ARRÊTÉ | `./voltaneum logs`, puis `logs/voltaneum.err` ; `./voltaneum diagnose` |
| bandeau rouge « Mesures périmées » | passerelle injoignable : réseau / VPN (`./voltaneum diagnose`) |
| alerte `oil_sensor_frozen` | vérifier le point PT02 sur la passerelle avec l'automaticien |
| aucune consigne PID | PT02 absente ou mesures périmées (voir l'onglet PID) |
| aucune prédiction | aucun modèle validé : `./voltaneum watch` donne la cause du refus (souvent : PT02 ou la pompe constantes, rien à apprendre) |
| interface injoignable | port `DASHBOARD_PORT` déjà utilisé (voir le journal) ; tunnel SSH |
| base corrompue ou fausse manœuvre | `./voltaneum stop`, `./voltaneum restore`, `./voltaneum restore <nom>`, `./voltaneum` |

## 11. Arborescence

```text
voltaneum                 commande unique
voltaneum.py              point d'entrée Python (run, retrain, model, data, diagnose, demo)
config.py                 configuration et validation
service/                  processus principal (app.py), diagnostic
collectors/               passerelle, température extérieure, météo
control/                  PID, conditions de refroidissement
ml/                       entraînement, prédiction, monitoring, ré-entraînement automatique
notifications/            règles et envoi des alertes
database/                 SQLite, stockage, sauvegardes, résumé
messaging/                clients MQTT
dashboard/                interface web (lecture seule) et démonstration
tests/                    tests automatisés (./voltaneum test)
```

Générés à l'exécution (non versionnés) : `voltaneum.db`, `models/`, `backups/`, `logs/`, `venv/`.
