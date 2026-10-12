#!/usr/bin/env bash
# Lancé par cron chaque minute (voir deploy/install-collect-service.sh) : démarre la collecte
# si elle ne tourne pas, sinon ne fait rien. Aucun droit root nécessaire.
#
# Le verrou (flock) est tenu par la collecte elle-même pendant toute sa durée : tant qu'elle
# tourne, les lancements suivants échouent immédiatement sur le verrou et s'arrêtent sans
# rien faire ; si elle s'arrête (plantage, redémarrage du serveur), la minute suivante la relance.
#
# Usage : deploy/collect-watchdog.sh [base SQLite, défaut voltaneum.db]
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1
DB_PATH="${1:-voltaneum.db}"

if [ -x venv/bin/python ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python"
elif [ -x .venv/bin/python ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
else
    exit 1
fi

mkdir -p logs
# Logs applicatifs : logs/collect.log (rotation). collect.err ne reçoit que ce qui survient
# avant que la journalisation soit en place (ex. erreur d'import, .env invalide).
exec flock -n logs/collect.lock \
    "$PYTHON" collect_data.py --db "$DB_PATH" --log-file logs/collect.log \
    >> logs/collect.err 2>&1
