#!/usr/bin/env bash
# Installe (ou met à jour) la collecte de données en arrière-plan, comme service systemd.
#
# Usage, depuis le serveur Linux :
#     cd ~/voltaneum_ITNET && git pull && bash deploy/install-collect-service.sh
#
# Relançable sans risque (après chaque `git pull`) : il met à jour les dépendances, réécrit
# le service avec les bons chemins et le redémarre sur le nouveau code. Le service tourne
# sous l'utilisateur courant, redémarre seul en cas d'arrêt et au démarrage du serveur.
#
# Options :
#     --db <chemin>   base SQLite de sortie (défaut : voltaneum.db, dans le projet)
#     --dry-run       affiche le service sans rien installer
set -euo pipefail

SERVICE_NAME="voltaneum-collect"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="voltaneum.db"
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --db) DB_PATH="${2:?--db attend un chemin}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "Option inconnue : $1" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERREUR : %s\n' "$*" >&2; exit 1; }

RUN_USER="$(id -un)"
[ "$RUN_USER" != "root" ] || fail "lancez ce script avec votre utilisateur habituel (pas root, ni sudo) : il demandera sudo lui-même."

cd "$PROJECT_DIR"

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

# La configuration est vérifiée AVANT d'installer le service : un .env incomplet est signalé
# ici, au lieu d'un service qui redémarre en boucle.
CONFIG_ERROR="$("$PYTHON" - <<'PY' 2>&1
import config
try:
    config.validate_config()
except ValueError as exc:
    print(exc)
    raise SystemExit(1)
PY
)" || fail "${CONFIG_ERROR:-configuration invalide} -> corrigez .env puis relancez."

UNIT="[Unit]
Description=Voltaneum - collecte des donnees de la passerelle
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON collect_data.py --db $DB_PATH
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"

if [ "$DRY_RUN" -eq 1 ]; then
    step "Service qui serait installé dans $UNIT_PATH (--dry-run : rien n'est modifié)"
    printf '%s' "$UNIT"
    exit 0
fi

command -v systemctl > /dev/null || fail "systemd introuvable sur ce serveur."

# Deux collectes sur la même base écriraient chaque mesure en double.
SERVICE_PID="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2> /dev/null || echo 0)"
OTHERS="$(pgrep -u "$RUN_USER" -f 'collect_data\.py' | grep -vx "${SERVICE_PID:-0}" || true)"
if [ -n "$OTHERS" ]; then
    fail "une collecte lancée à la main tourne déjà (PID : $(echo $OTHERS)). Arrêtez-la (Ctrl+C dans son terminal, ou : kill $(echo $OTHERS)) puis relancez ce script."
fi

step "Installation du service $SERVICE_NAME (sudo peut demander votre mot de passe)"
printf '%s' "$UNIT" | sudo tee "$UNIT_PATH" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" > /dev/null 2>&1
sudo systemctl restart "$SERVICE_NAME"

step "Vérification"
sleep 5
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    fail "le service ne tourne pas (voir les logs ci-dessus)."
fi
journalctl -u "$SERVICE_NAME" -n 10 --no-pager || true

cat <<EOF

Collecte en arrière-plan : OK (service $SERVICE_NAME, base $DB_PATH).
  Logs en direct    : journalctl -u $SERVICE_NAME -f
  État              : systemctl status $SERVICE_NAME
  Données           : $VENV_DIR/bin/python check_data.py $DB_PATH
  Arrêter           : sudo systemctl stop $SERVICE_NAME
  Mettre à jour     : git pull && bash deploy/install-collect-service.sh
EOF
