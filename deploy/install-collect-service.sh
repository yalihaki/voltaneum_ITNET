#!/usr/bin/env bash
# Collecte de données en continu, en arrière-plan, SANS droits root.
#
# Usage, depuis le serveur Linux (utilisateur normal) :
#     cd ~/voltaneum_ITNET && git pull && bash deploy/install-collect-service.sh
#
# Installe une tâche cron (crontab de l'utilisateur) qui vérifie chaque minute que la collecte
# tourne et la relance sinon (deploy/collect-watchdog.sh). La collecte continue donc après la
# déconnexion SSH, et repart d'elle-même après un plantage ou un redémarrage du serveur.
# Relançable sans risque après chaque `git pull` : dépendances mises à jour, collecte
# redémarrée sur le nouveau code.
#
# Options :
#     --db <chemin>   base SQLite de sortie (défaut : voltaneum.db, dans le projet)
#     --status        état de la collecte (processus, dernières mesures, logs) ; "status" accepté
#     --stop          arrête la collecte et retire la tâche cron ; "stop" accepté
#     --dry-run       affiche la tâche cron sans rien modifier
set -euo pipefail

CRON_MARKER="# voltaneum-collect"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCHDOG="$PROJECT_DIR/deploy/collect-watchdog.sh"
DB_PATH="voltaneum.db"
ACTION="install"
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --db) DB_PATH="${2:?--db attend un chemin}"; shift 2 ;;
        --status|status) ACTION="status"; shift ;;
        --stop|stop) ACTION="stop"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "Option inconnue : $1" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERREUR : %s\n' "$*" >&2; exit 1; }

cd "$PROJECT_DIR"
CRON_LINE="* * * * * '$WATCHDOG' '$DB_PATH' $CRON_MARKER"

collector_pids() {
    # Processus Python de la collecte (et non `flock`, qui ne fait que tenir le verrou).
    pgrep -u "$(id -un)" -f '^[^ ]*python[^ ]* collect_data\.py' || true
}

remove_cron_line() {
    local current
    current="$(crontab -l 2> /dev/null || true)"
    printf '%s\n' "$current" | { grep -vF "$CRON_MARKER" || true; } | sed '/^$/d' | crontab -
}

stop_collectors() {
    local pids
    pids="$(collector_pids)"
    [ -n "$pids" ] || return 0
    echo "Arrêt de la collecte en cours (PID : $(echo $pids))..."
    # SIGTERM : arrêt propre (la collecte termine son instantané et ferme la base).
    kill -TERM $pids 2> /dev/null || true
    for _ in $(seq 1 30); do
        [ -n "$(collector_pids)" ] || return 0
        sleep 1
    done
    echo "Arrêt forcé (la collecte n'a pas répondu en 30 s)."
    kill -KILL $(collector_pids) 2> /dev/null || true
}

show_status() {
    local pids python
    pids="$(collector_pids)"
    if crontab -l 2> /dev/null | grep -qF "$CRON_MARKER"; then
        echo "Relance automatique (cron) : installée"
    else
        echo "Relance automatique (cron) : NON installée"
    fi
    if [ -n "$pids" ]; then
        echo "Collecte : EN COURS (PID $(echo $pids), depuis $(ps -o etime= -p "${pids%%$'\n'*}" | tr -d ' '))"
    else
        echo "Collecte : ARRÊTÉE"
    fi
    python="$PROJECT_DIR/venv/bin/python"
    [ -x "$python" ] || python="$PROJECT_DIR/.venv/bin/python"
    if [ -x "$python" ] && [ -f "$DB_PATH" ]; then
        "$python" - "$DB_PATH" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    total, last = conn.execute("SELECT COUNT(*), MAX(timestamp) FROM telemetry").fetchone()
    recent = conn.execute(
        "SELECT COUNT(*) FROM telemetry WHERE timestamp >= datetime('now', '-5 minutes')").fetchone()[0]
print(f"Base : {total} ligne(s), dernière mesure {last} UTC, {recent} ligne(s) sur les 5 dernières minutes")
PY
    fi
    if [ -f logs/collect.log ]; then
        echo; echo "Dernières lignes de logs/collect.log :"; tail -n 8 logs/collect.log
    fi
    if [ -s logs/collect.err ]; then
        echo; echo "Erreurs de démarrage (logs/collect.err) :"; tail -n 8 logs/collect.err
    fi
}

command -v crontab > /dev/null || fail "la commande crontab est introuvable sur ce serveur."
command -v flock > /dev/null || fail "la commande flock (paquet util-linux) est introuvable."

case "$ACTION" in
    status) show_status; exit 0 ;;
    stop)
        step "Arrêt de la collecte en arrière-plan"
        remove_cron_line
        stop_collectors
        echo "Collecte arrêtée et relance automatique retirée."
        exit 0 ;;
esac

if [ "$DRY_RUN" -eq 1 ]; then
    step "Tâche cron qui serait installée (--dry-run : rien n'est modifié)"
    echo "$CRON_LINE"
    exit 0
fi

step "Vérification de la configuration"
[ -f .env ] || fail "fichier .env absent dans $PROJECT_DIR (copiez .env.example en .env et renseignez les identifiants)."
chmod 600 .env

step "Environnement Python"
if [ -x venv/bin/python ]; then
    VENV_DIR="venv"
elif [ -x .venv/bin/python ]; then
    VENV_DIR=".venv"
else
    command -v python3 > /dev/null || fail "python3 introuvable."
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail "Python 3.11 ou plus récent requis."
    VENV_DIR="venv"
    echo "Création de l'environnement virtuel $VENV_DIR/ ..."
    python3 -m venv "$VENV_DIR"
fi
PYTHON="$PROJECT_DIR/$VENV_DIR/bin/python"
"$PYTHON" -m pip install --quiet --disable-pip-version-check -r requirements.txt
echo "Dépendances à jour ($VENV_DIR/)."

# Configuration vérifiée AVANT de lancer quoi que ce soit : un .env incomplet est signalé ici,
# au lieu d'une collecte relancée chaque minute pour échouer aussitôt.
CONFIG_ERROR="$("$PYTHON" - <<'PY' 2>&1
import config
try:
    config.validate_config()
except ValueError as exc:
    print(exc)
    raise SystemExit(1)
PY
)" || fail "${CONFIG_ERROR:-configuration invalide} -> corrigez .env puis relancez."
echo "Configuration valide."

step "Redémarrage de la collecte sur le code actuel"
chmod +x "$WATCHDOG"
remove_cron_line          # évite une relance par cron pendant l'arrêt
stop_collectors
mkdir -p logs
: > logs/collect.err

step "Installation de la relance automatique (crontab, chaque minute)"
{ crontab -l 2> /dev/null || true; echo "$CRON_LINE"; } | sed '/^$/d' | crontab -
crontab -l | grep -F "$CRON_MARKER"

step "Démarrage immédiat (sans attendre la prochaine minute)"
nohup setsid "$WATCHDOG" "$DB_PATH" > /dev/null 2>&1 < /dev/null &
sleep 8
[ -n "$(collector_pids)" ] || { show_status; fail "la collecte ne s'est pas lancée (voir ci-dessus)."; }

step "État"
show_status

cat <<EOF

Collecte en continu : OK. Vous pouvez vous déconnecter, elle continue.
  État + dernières mesures : bash deploy/install-collect-service.sh --status
  Logs en direct           : tail -f logs/collect.log
  Arrêter                  : bash deploy/install-collect-service.sh --stop
  Mettre à jour            : git pull && bash deploy/install-collect-service.sh
EOF
