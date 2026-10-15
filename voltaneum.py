"""Voltaneum / ITNET — point d'entrée unique (appelé par le script `./voltaneum`).

    python voltaneum.py run                    # tout le système, au premier plan
    python voltaneum.py retrain                # une nuit de ré-entraînement (proposition)
    python voltaneum.py model [accept|reject|rollback]
    python voltaneum.py watch                  # supervision en direct dans le terminal
    python voltaneum.py data                   # résumé des mesures en base
    python voltaneum.py diagnose               # réseau, services, base, modèle
    python voltaneum.py restore [sauvegarde]   # lister / restaurer une sauvegarde
    python voltaneum.py demo                   # interface sur données fictives

Au quotidien, utiliser `./voltaneum` (démarrage en arrière-plan, relance automatique, état).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

import config

LOG_DIR = Path(config.PROJECT_ROOT) / "logs"


def _setup_logging(log_file: str | None) -> None:
    handler: logging.Handler = logging.StreamHandler()
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5,
                                                       encoding="utf-8")
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=[handler], force=True)


def _single_instance_lock():
    """Verrou exclusif sur logs/voltaneum.lock : jamais deux processus Voltaneum (deux collectes
    écriraient chaque mesure en double). Retourne le fichier verrouillé, ou None si déjà pris."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOG_DIR / "voltaneum.lock", "w")  # noqa: SIM115 - gardé ouvert pendant toute la vie du processus
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:  # Windows : pas de verrou, un seul lancement est de la responsabilité de l'opérateur
        return handle
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    _setup_logging(args.log_file)
    lock = _single_instance_lock()
    if lock is None:
        if not args.quiet_if_running:
            print("Voltaneum tourne déjà (verrou logs/voltaneum.lock).")
        return 0
    from service.app import VoltaneumApp

    try:
        VoltaneumApp().run_forever()
    except ValueError as exc:  # configuration invalide : message clair, pas de trace
        logging.getLogger("voltaneum").error("%s -> corriger .env puis relancer.", exc)
        return 2
    return 0


def cmd_retrain(args: argparse.Namespace) -> int:
    _setup_logging(args.log_file)
    from ml.auto_retrain import RetrainBusy, format_horizon, run_auto_retrain

    if not Path(args.db).is_file():
        print(f"Base introuvable : {args.db}", file=sys.stderr)
        return 2
    horizons = tuple(int(h) for h in args.horizons.split(",")) if args.horizons else None
    try:
        summary = run_auto_retrain(db_path=args.db, model_dir=args.model_dir, horizons=horizons)
    except RetrainBusy as exc:
        print(f"Rien à faire : {exc}")
        return 1
    print(f"\nRé-entraînement {summary['run_id']} : {summary['rows']} mesures.")
    for c in summary["candidates"]:
        print(f"  {format_horizon(c['horizon_seconds']):>8}  {'FIABLE ' if c['eligible'] else 'refusé '}  {c['reason']}")
    print(f"\nDécision : {summary['reason']}.")
    if summary["decision"] == "proposed":
        print("Proposition en attente : ./voltaneum model   (puis accept ou reject)")
    return 0


def _describe_proposal(proposal: dict) -> str:
    from ml.auto_retrain import format_horizon

    p, cur = proposal["proposed"], proposal.get("current")
    lines = [
        f"Proposition du {proposal['created_at'][:16].replace('T', ' ')} UTC",
        f"  Raison   : {proposal['reason']}",
        f"  Proposé  : prédiction à {format_horizon(p['horizon_seconds'])} ({p['best_model']}), erreur "
        f"{p['rmse']:.3f} °C, {p['gain']:.0%} de mieux que la persistance, {p['n_samples']} échantillons",
        (f"  Installé : prédiction à {format_horizon(cur['horizon_seconds'])} ({cur['best_model']}), erreur récente "
         f"{cur['rmse']:.3f} °C, {cur['gain']:.0%} de mieux que la persistance") if cur else "  Installé : aucun modèle",
    ]
    lines += [f"\n  ATTENTION : {w}" for w in proposal.get("warnings", [])]
    return "\n".join(lines)


def cmd_model(args: argparse.Namespace) -> int:
    from ml import auto_retrain
    from ml.auto_retrain import ProposalError, format_horizon

    operator = getpass.getuser()

    def confirm(question: str) -> bool:
        return args.yes or input(f"{question} [oui/non] ").strip().lower() == "oui"

    try:
        if args.action == "rollback":
            if not confirm("Réinstaller le modèle précédent ?"):
                print("Annulé.")
                return 1
            report = auto_retrain.rollback(args.model_dir, operator)
            print(f"Modèle précédent réinstallé (prédiction à {format_horizon(report.get('horizon_seconds', 600))}).")
            return 0
        installed = auto_retrain.installed_model(args.model_dir)
        if args.action == "show":
            print("Modèle installé :", f"prédiction à {format_horizon(installed['horizon_seconds'])} "
                  f"({installed.get('best_model')})" if installed else "aucun")
        proposal = auto_retrain.pending_proposal(args.model_dir)
        print(_describe_proposal(proposal))
        if args.action == "accept":
            if not confirm("\nInstaller ce modèle ?"):
                print("Annulé : rien n'a été modifié.")
                return 1
            auto_retrain.accept_proposal(args.model_dir, operator)
            print("\nModèle installé, pris en compte sans redémarrage. Retour arrière : ./voltaneum model rollback")
        elif args.action == "reject":
            auto_retrain.reject_proposal(args.model_dir, operator, args.note)
            print("\nProposition refusée. Le modèle installé ne change pas.")
        else:
            print("\nPour décider : ./voltaneum model accept   ou   ./voltaneum model reject")
        return 0
    except ProposalError as exc:
        print(f"Rien à faire : {exc}")
        return 0 if args.action == "show" else 1


def cmd_data(args: argparse.Namespace) -> int:
    from database.summary import print_summary

    return print_summary(args.db)


def cmd_diagnose(_args: argparse.Namespace) -> int:
    from service.diagnose import run_diagnostic

    return run_diagnostic()


def cmd_watch(args: argparse.Namespace) -> int:
    from service.watch import run_watch

    return run_watch(args.db, interval=args.interval, minutes=args.minutes, once=args.once)


def cmd_restore(args: argparse.Namespace) -> int:
    """Liste les sauvegardes, ou restaure l'une d'elles (Voltaneum doit être arrêté)."""
    from database.backup import create_backup, restore_backup

    backup_root = Path(config.STORAGE_BACKUP_DIR)
    archives = sorted(backup_root.rglob("voltaneum-*.db.gz"), key=lambda p: p.name, reverse=True)
    if not args.archive:
        if not archives:
            print(f"Aucune sauvegarde dans {backup_root}.")
            return 1
        print("Sauvegardes disponibles (plus récentes d'abord) :")
        for archive in archives:
            print(f"  {archive.relative_to(backup_root)}   ({archive.stat().st_size / 1024**2:.1f} Mo)")
        print("\nRestaurer : ./voltaneum stop   puis   ./voltaneum restore <nom>   puis   ./voltaneum")
        return 0
    candidates = [p for p in archives if args.archive in (p.name, str(p.relative_to(backup_root)))]
    archive = candidates[0] if candidates else Path(args.archive)
    if not archive.is_file():
        print(f"Sauvegarde introuvable : {args.archive}")
        return 1
    lock = _single_instance_lock()
    if lock is None:
        print("Voltaneum tourne : arrêtez-le d'abord (./voltaneum stop).")
        return 1
    if not args.yes and input(f"Remplacer la base actuelle par {archive.name} ? [oui/non] ").strip().lower() != "oui":
        print("Annulé : rien n'a été modifié.")
        return 1
    if Path(config.SQLITE_DB_PATH).exists():
        safety = create_backup(config.SQLITE_DB_PATH, str(backup_root / "avant-restauration"), 5, 0)
        print(f"Base actuelle sauvegardée au préalable : {safety}")
    restore_backup(str(archive), config.SQLITE_DB_PATH)
    print(f"Base restaurée depuis {archive.name}. Redémarrer : ./voltaneum")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Interface sur une base FICTIVE (demo_dashboard.db), alimentée en continu."""
    _setup_logging(None)
    from dashboard import demo
    from dashboard.server import make_server

    config.MODEL_DIR = demo.DEMO_MODEL_DIR
    config.STORAGE_BACKUP_DIR = str(Path(demo.DEMO_MODEL_DIR) / "sauvegardes")  # jamais les vraies
    process = demo.create_demo_database()
    stop = threading.Event()
    threading.Thread(target=demo.run_live_feed, args=(process, stop), daemon=True).start()
    server = make_server("127.0.0.1", args.port, demo.DEMO_DB_PATH)
    print(f"Démonstration (données fictives) : http://localhost:{args.port}   — Ctrl+C pour arrêter")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voltaneum", description="Voltaneum / ITNET — supervision de l'échangeur.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="tout le système au premier plan (Ctrl+C pour arrêter)")
    run.add_argument("--log-file", help="journal dans un fichier (rotation) au lieu de la console")
    run.add_argument("--quiet-if-running", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

    retrain = sub.add_parser("retrain", help="ré-entraîner les modèles et PROPOSER le meilleur")
    retrain.add_argument("--db", default=config.SQLITE_DB_PATH)
    retrain.add_argument("--model-dir", default=config.MODEL_DIR)
    retrain.add_argument("--horizons", help="horizons en secondes, ex. 600,1800,3600")
    retrain.add_argument("--log-file")
    retrain.set_defaults(func=cmd_retrain)

    model = sub.add_parser("model", help="voir / accepter / refuser le modèle proposé, ou revenir en arrière")
    model.add_argument("action", nargs="?", default="show", choices=("show", "accept", "reject", "rollback"))
    model.add_argument("--yes", action="store_true", help="ne pas demander de confirmation")
    model.add_argument("--note", default="", help="commentaire enregistré avec un refus")
    model.add_argument("--model-dir", default=config.MODEL_DIR)
    model.set_defaults(func=cmd_model)

    data = sub.add_parser("data", help="résumé des mesures enregistrées")
    data.add_argument("--db", default=config.SQLITE_DB_PATH)
    data.set_defaults(func=cmd_data)

    sub.add_parser("diagnose", help="vérifier réseau, services, base et modèle").set_defaults(func=cmd_diagnose)

    watch = sub.add_parser("watch", help="supervision en direct dans le terminal (sans navigateur)")
    watch.add_argument("--db", default=config.SQLITE_DB_PATH)
    watch.add_argument("--interval", type=float, default=5.0, help="rafraîchissement en secondes")
    watch.add_argument("--minutes", type=int, default=60, help="durée des courbes")
    watch.add_argument("--once", action="store_true", help="afficher une seule fois puis quitter")
    watch.set_defaults(func=cmd_watch)

    restore = sub.add_parser("restore", help="lister les sauvegardes, ou en restaurer une (Voltaneum arrêté)")
    restore.add_argument("archive", nargs="?", help="nom de la sauvegarde (voir la liste)")
    restore.add_argument("--yes", action="store_true", help="ne pas demander de confirmation")
    restore.set_defaults(func=cmd_restore)

    demo = sub.add_parser("demo", help="interface sur données fictives, sans passerelle")
    demo.add_argument("--port", type=int, default=8090)
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
