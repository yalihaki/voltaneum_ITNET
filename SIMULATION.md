# SIMULATION.md — Démonstration bout-en-bout du pipeline

## À quoi ça sert

`simulate.py` fait tourner **toute la chaîne logicielle** de Voltaneum / ITNET
en quelques secondes, sans passerelle réelle, sans broker MQTT, sans serveur
SMTP — uniquement avec des données synthétiques et une base SQLite temporaire.

Deux usages principaux :
- **Vérification rapide** après une modification de code : si les 13 vérifications
  passent, les briques principales du projet fonctionnent toujours ensemble.
- **Démonstration** (soutenance, présentation à un tuteur) : chaque étape
  s'affiche sur la console avec son résultat, dans l'ordre logique du pipeline
  réel (collecte → stockage → entraînement → prédiction → régulation →
  alertes → sauvegarde).

## Lancer la simulation

```bash
python3 simulate.py
```

Code de sortie `0` si toutes les vérifications passent, `1` sinon (utilisable
dans un script ou une CI).

Le script ne touche **jamais** à `voltaneum.db`, `models/` ou `backups/` : il
travaille exclusivement dans un dossier temporaire système, automatiquement
supprimé à la fin de l'exécution (`tempfile.TemporaryDirectory`).

## Ce que chaque étape démontre

| Étape | Module exercé | Ce qui est prouvé |
|---|---|---|
| 1 | `generate_test_data.py`, `database/sqlite_client.py` | Génération de données réalistes (pompe variant réellement entre 0 et 15 Hz) et écriture en base |
| 2 | `database/storage_manager.py` | Comportement normal (sous seuil) **et** déclenchement réel démontré (seuil artificiellement abaissé, délai de grâce nul) : avertissement + tentative de purge, planchers de rétention respectés |
| 3a | `ml/train.py` | **Le garde-fou `no_actionable_signal`** : reproduit exactement la collecte réelle du 9-10 septembre 2026 (pompe figée à 0 Hz) et vérifie qu'aucun modèle trompeur n'est sauvegardé |
| 3b | `ml/train.py` | Sur des données avec une vraie variation de la pompe, l'entraînement aboutit à un statut `"ok"` et bat la persistance |
| 4 | `ml/predictor.py` | Chargement des modèles entraînés à l'étape 3b et prédiction réelle |
| 4b | `ml/monitor.py`, `database/sqlite_client.py` | Journalisation d'une prédiction, avance simulée du temps, rapprochement avec la valeur réelle mesurée, calcul du RMSE glissant |
| 5 | `control/pid_controller.py` | **Le sens correct du PID** (huile chaude → consigne plus haute que huile froide) et **l'anti-windup** (récupération immédiate après 500 cycles de saturation, pas de dette cachée) |
| 6 | `notifications/email_notifier.py` | Détection d'une alerte CRITIQUE (seuil dépassé) et respect du cooldown (pas de doublon immédiat) |
| 7 | `database/backup.py` | Un cycle complet sauvegarde → suppression réelle du fichier → restauration → vérification que les données sont identiques |

## Interpréter un échec

Chaque ligne `❌` indique précisément quelle vérification a échoué — c'est
suffisant pour savoir quel module regarder en premier. Si l'étape 3a échoue
(`no_actionable_signal` non déclenché), par exemple, c'est que le garde-fou de
`ml/train.py` a régressé. Si l'étape 5 échoue sur l'anti-windup, c'est que
`control/pid_controller.py` a changé de comportement.

## Ce que la simulation NE remplace PAS

Elle ne teste ni la connexion réelle à la passerelle (`collectors/gateway_collector.py`
en conditions réelles), ni un vrai envoi d'email (SMTP), ni un vrai broker MQTT,
ni le comportement sur plusieurs jours/semaines. Elle valide que les briques
fonctionnent **ensemble et correctement isolées du réseau** — la validation
terrain (cahier des charges, section recette à 3 niveaux) reste une étape
distincte et obligatoire avant toute mise en production.
