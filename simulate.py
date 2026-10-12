#!/usr/bin/env python3
"""
simulate.py — Démonstration bout-en-bout de Voltaneum / ITNET, en console.

En clair : ce script fait tourner TOUTE la chaîne logicielle (collecte,
stockage, entraînement, prédiction, régulation, alertes, sauvegarde) sans
avoir besoin d'une vraie passerelle, d'un vrai broker MQTT ou d'un vrai
serveur SMTP. Chaque étape est imprimée sur la console avec son résultat,
pour pouvoir suivre — ou faire une démonstration — de ce que fait chaque
brique du projet, une par une, en quelques secondes.

Documentation détaillée : voir SIMULATION.md.

Usage :
    python3 simulate.py

Ce script est en LECTURE SEULE vis-à-vis du projet réel : il travaille
exclusivement dans un dossier temporaire (base SQLite, modèles, sauvegardes
de démonstration), automatiquement supprimé à la fin. Il ne touche jamais
à voltaneum.db, à models/, ni à backups/.
"""

from __future__ import annotations

import subprocess  # nosec B404 - lance uniquement nos propres scripts, arguments fixes
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
from database import sqlite_client
from database.backup import create_backup, restore_backup, verify_backup
from database.storage_manager import StorageManager, database_size_bytes
from ml.train import train_and_compare
from ml.predictor import DualModelPredictor
from control.cooling_advice import compute_cooling_conditions
from control.pid_controller import compute_setpoint
from notifications.email_notifier import evaluate_rules

WIDTH = 78


def banner(step: str, title: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"ÉTAPE {step} — {title}")
    print("=" * WIDTH)


def line(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✅" if ok else "❌"
    text = f"{mark} {label}"
    if detail:
        text += f" — {detail}"
    print(text)
    return ok


def make_frozen_pump_dataframe(n: int = 1500) -> pd.DataFrame:
    """Reproduit fidèlement la collecte réelle du 9-10 septembre 2026 sur
    l'automate de TEST : freq_pompe et ht01 n'ont jamais varié (la pompe
    n'est pas encore pilotée sur ce banc), seule la météo fait bouger la
    température. C'est le cas exact qui a produit un RMSE trompeur de 0.0
    avant l'ajout du garde-fou "no_actionable_signal" (voir CHANGELOG.md)."""
    rng = np.random.default_rng(7)
    t = pd.date_range("2026-09-09 15:29:10", periods=n, freq="47s")
    outdoor = 20 + 6 * np.sin(np.linspace(0, 4 * np.pi, n)) + rng.normal(0, 0.3, n)
    pt02 = 27 + 0.15 * outdoor + rng.normal(0, 0.1, n)
    return pd.DataFrame({
        "timestamp": t,
        "pt01": pt02 - 0.3, "pt02": pt02,
        "pt03": pt02 - 1.5, "pt04": pt02 - 2.0,
        "ht01": np.full(n, 39.0),
        "freq_pompe": np.zeros(n),  # jamais activée sur l'automate de test
        "temp_outdoor": outdoor,
    })


def main() -> int:
    print("VOLTANEUM / ITNET — Simulation complète du pipeline (console)")
    print("Aucun réseau réel utilisé (passerelle, MQTT, SMTP) : tout tourne en local,")
    print("dans un dossier temporaire automatiquement supprimé en fin d'exécution.")

    checks: list[bool] = []

    with tempfile.TemporaryDirectory(prefix="voltaneum_sim_") as tmp:
        tmp_path = Path(tmp)
        db_realistic = tmp_path / "sim_realistic.db"
        model_dir = tmp_path / "models"
        backup_dir = tmp_path / "backups"

        # ================= ÉTAPE 1 =================
        banner("1", "Génération de données synthétiques (collectors + database)")
        print("Génération d'un jeu de données AVEC un vrai cycle de pompe (0-50 Hz),")
        print("comme si le collecteur avait tourné plusieurs jours sur une installation")
        print("où la régulation est réellement active (generate_test_data.py) :")
        proc = subprocess.run(  # nosec B603 - exécutable et arguments maîtrisés, aucune entrée externe
            [sys.executable, "generate_test_data.py", "--db", str(db_realistic),
             "--rows", "3000", "--interval", "30", "--force"],
            capture_output=True, text=True,
        )
        checks.append(line("generate_test_data.py exécuté sans erreur", proc.returncode == 0))
        df_check = sqlite_client.get_dataframe("1970-01-01", "2100-01-01", db_path=str(db_realistic))
        checks.append(line(
            f"{len(df_check)} lignes écrites dans {db_realistic.name} via database/sqlite_client.py",
            len(df_check) > 0,
        ))
        print(f"   Plage réelle de freq_pompe générée : "
              f"{df_check['freq_pompe'].min():.1f} - {df_check['freq_pompe'].max():.1f} Hz "
              f"(plage attendue de l'automate : {config.SENSOR_VALID_RANGES['freq_pompe']})")

        # ================= ÉTAPE 2 =================
        banner("2", "Surveillance du stockage (database/storage_manager.py)")
        size = database_size_bytes(str(db_realistic))
        limit = config.STORAGE_LIMIT_BYTES
        print(f"Taille de la base : {size:,} octets | seuil configuré : "
              f"{limit:,} octets ({limit / 1024**4:.2f} Tio)")
        checks.append(line(
            "Base largement sous le seuil d'alerte : aucune purge déclenchée",
            size < limit * config.STORAGE_WARNING_RATIO,
        ))

        print("Démonstration du déclenchement réel (seuil artificiellement abaissé,")
        print("délai de grâce mis à 0, sans toucher à la vraie configuration) :")
        captured: list[tuple[str, str, str]] = []

        def fake_alert_sender(subject: str, body: str, level: str) -> bool:
            captured.append((subject, body, level))
            return True

        original_limit, original_grace = config.STORAGE_LIMIT_BYTES, config.STORAGE_GRACE_PERIOD_SECONDS
        original_backup_dir = config.STORAGE_BACKUP_DIR
        config.STORAGE_LIMIT_BYTES = max(int(size * 1.02), 1)
        config.STORAGE_GRACE_PERIOD_SECONDS = 0
        # La purge démontrée crée une sauvegarde : elle doit aller dans le dossier TEMPORAIRE,
        # jamais dans le vrai dossier backups/ du projet (correctif : une archive y était laissée).
        config.STORAGE_BACKUP_DIR = str(backup_dir)
        try:
            demo_manager = StorageManager(db_path=str(db_realistic), alert_sender=fake_alert_sender)
            demo_manager.check_once()  # déclenche l'avertissement
            result = demo_manager.check_once()  # tente la purge (délai de grâce nul)
        finally:
            config.STORAGE_LIMIT_BYTES, config.STORAGE_GRACE_PERIOD_SECONDS = original_limit, original_grace
            config.STORAGE_BACKUP_DIR = original_backup_dir
        print(f"   {len(captured)} alerte(s) émise(s) (email SIMULÉ, non envoyé) : "
              f"{[level for _, _, level in captured]}")
        print(f"   Résultat de la purge : {result['purged']} ligne(s) supprimée(s) "
              f"(planchers de rétention appliqués — voir CHANGELOG.md, décisions A/B).")
        checks.append(line(
            "Le superviseur réagit (avertissement + tentative de purge) sous seuil abaissé",
            len(captured) >= 1,
        ))

        # ================= ÉTAPE 3 =================
        banner("3a", "Entraînement ML — cas réel actuel (ml/train.py, pompe figée)")
        print("Reproduction fidèle de la collecte du 9-10 septembre 2026 sur l'automate")
        print("de TEST : freq_pompe et ht01 n'ont jamais varié pendant la collecte.")
        frozen_df = make_frozen_pump_dataframe()
        frozen_model_dir = tmp_path / "models_frozen"
        frozen_report = train_and_compare(frozen_df, model_dir=str(frozen_model_dir))
        print(f"   Statut retourné : {frozen_report['status']}")
        print(f"   Variables figées détectées : {frozen_report.get('degenerate_features')}")
        checks.append(line(
            "Garde-fou déclenché : statut != 'ok' malgré un score arithmétique excellent",
            frozen_report["status"] != "ok",
        ))
        checks.append(line(
            "Aucun modèle sauvegardé pour ce cas (mode dégradé forcé)",
            not any(frozen_model_dir.glob("*.joblib")),
        ))

        banner("3b", "Entraînement ML — cas avec vraie variabilité (ml/train.py)")
        real_report = train_and_compare(df_check, model_dir=str(model_dir))
        print(f"   Statut : {real_report['status']} | "
              f"meilleur modèle : {real_report.get('best_model')} | "
              f"bat la persistance : {real_report.get('beats_persistence')}")
        print(f"   RMSE persistance={real_report['baseline_persistence']['rmse']:.4f} | "
              f"RandomForest={real_report['random_forest']['rmse']:.4f} | "
              f"GradientBoosting={real_report['gradient_boosting']['rmse']:.4f}")
        checks.append(line(
            "Modèle jugé valide (données variées, aucune variable figée)",
            real_report["status"] == "ok",
        ))

        # ================= ÉTAPE 4 =================
        banner("4", "Prédiction (ml/predictor.py)")
        predictor = DualModelPredictor(model_dir=str(model_dir))
        sample_features = {
            col: float(df_check[col].iloc[-1]) for col in config.FEATURE_COLUMNS
        }
        prediction = predictor.predict(sample_features)
        print(f"   Features d'entrée : { {k: round(v, 2) for k, v in sample_features.items()} }")
        print(f"   Prédiction combinée à +{predictor.horizon_seconds}s : "
              f"{prediction['combined_prediction']:.2f}°C "
              f"(désaccord RF/GB : {prediction['disagreement']:.3f}°C)")
        checks.append(line(
            "Prédiction obtenue sans exception (modèles chargés correctement)",
            "combined_prediction" in prediction,
        ))

        # ================= ÉTAPE 4b =================
        banner("4b", "Vérification en temps réel (ml/monitor.py)")
        print("Journalisation des prédictions, simulation du temps qui passe, puis")
        print("rapprochement avec la valeur réellement mesurée à l'échéance :")
        made_at = datetime.now(timezone.utc)
        target_ts = made_at + timedelta(seconds=predictor.horizon_seconds)
        for model_name, value in prediction["predictions"].items():
            sqlite_client.log_prediction(
                made_at=made_at.strftime("%Y-%m-%d %H:%M:%S"),
                target_ts=target_ts.strftime("%Y-%m-%d %H:%M:%S"),
                horizon_seconds=predictor.horizon_seconds,
                model_name=model_name,
                predicted_value=value,
                db_path=str(db_realistic),
            )
        sqlite_client.insert_reading(
            pt01=sample_features["pt01"] - 0.2,
            pt02=prediction["combined_prediction"] + 0.1,  # écart volontaire, réaliste
            pt03=sample_features["pt03"], pt04=sample_features["pt04"],
            ht01=sample_features["ht01"], freq_pompe=sample_features["freq_pompe"],
            temp_outdoor=sample_features["temp_outdoor"],
            timestamp=target_ts.strftime("%Y-%m-%d %H:%M:%S"),
            db_path=str(db_realistic),
        )
        completed = sqlite_client.complete_due_predictions(
            (target_ts + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            db_path=str(db_realistic),
        )
        rmse_rf = sqlite_client.get_rolling_rmse(
            predictor.horizon_seconds, "random_forest", db_path=str(db_realistic)
        )
        rmse_gb = sqlite_client.get_rolling_rmse(
            predictor.horizon_seconds, "gradient_boosting", db_path=str(db_realistic)
        )
        print(f"   {completed} prédiction(s) rapprochée(s) de la valeur réelle mesurée.")
        print(f"   RMSE glissant (échantillon minime, à titre indicatif) : "
              f"RandomForest={rmse_rf}, GradientBoosting={rmse_gb}")
        checks.append(line(
            "Les 2 prédictions journalisées ont bien été complétées avec la valeur réelle",
            completed == 2,
        ))

        # ================= ÉTAPE 5 =================
        banner("5", "Régulation PID (control/pid_controller.py)")
        setpoint_hot, _, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP + 10, t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP, t_outdoor=15.0, dt=1.0, pid_state={},
        )
        setpoint_cold, _, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP - 10, t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP, t_outdoor=15.0, dt=1.0, pid_state={},
        )
        print(f"   Huile à {config.TARGET_TEMP + 10}°C (chaude) -> consigne = {setpoint_hot:.1f}%")
        print(f"   Huile à {config.TARGET_TEMP - 10}°C (froide) -> consigne = {setpoint_cold:.1f}%")
        checks.append(line(
            "Direction correcte : huile chaude -> consigne plus élevée que huile froide",
            setpoint_hot > setpoint_cold,
        ))

        print("   Simulation de 500 cycles d'huile très chaude en continu (saturation)...")
        state: dict = {}
        for _ in range(500):
            setpoint_sat, state, _ = compute_setpoint(
                t_oil=config.CRITICAL_TEMP - 0.5, t_target=config.TARGET_TEMP,
                t_oil_predicted=config.TARGET_TEMP, t_outdoor=15.0, dt=1.0, pid_state=state,
            )
        setpoint_recovery, _, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP, t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP, t_outdoor=15.0, dt=1.0, pid_state=state,
        )
        print(f"   Consigne après 500 cycles saturés : {setpoint_sat:.1f}%")
        print(f"   Consigne 1 cycle après retour à la cible : {setpoint_recovery:.1f}%")
        checks.append(line(
            "Anti-windup efficace : la consigne redescend immédiatement, pas de dette cachée",
            setpoint_recovery < 50.0,
        ))

        # ================= ÉTAPE 6 =================
        banner("6", "Évaluation des règles d'alerte (notifications/email_notifier.py)")
        alert_state: dict = {}
        hot_telemetry = {"pt02": config.CRITICAL_TEMP + 2.0}
        alerts = evaluate_rules(hot_telemetry, None, alert_state)
        print(f"   Température huile simulée : {hot_telemetry['pt02']}°C "
              f"(seuil critique : {config.CRITICAL_TEMP}°C)")
        print(f"   Alerte(s) déclenchée(s) : {[a['rule_name'] for a in alerts]}")
        checks.append(line("Alerte CRITIQUE générée pour dépassement de seuil", len(alerts) == 1))

        alerts_repeat = evaluate_rules(hot_telemetry, None, alert_state)
        checks.append(line(
            "Cooldown respecté : pas de second envoi immédiat pour la même condition",
            len(alerts_repeat) == 0,
        ))

        # ================= ÉTAPE 7 =================
        banner("7", "Sauvegarde et restauration réelles (database/backup.py)")
        conn_check = sqlite_client.get_dataframe("1970-01-01", "2100-01-01", db_path=str(db_realistic))
        count_before = len(conn_check)
        backup_path = create_backup(str(db_realistic), str(backup_dir), retention_count=3)
        print(f"   Sauvegarde créée : {backup_path.name} ({backup_path.stat().st_size:,} octets)")
        checks.append(line("Sauvegarde vérifiée intègre (verify_backup)", verify_backup(str(backup_path))))

        db_realistic.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{db_realistic}{suffix}").unlink(missing_ok=True)
        print("   Base originale supprimée (sinistre simulé).")
        restore_backup(str(backup_path), str(db_realistic))
        restored = sqlite_client.get_dataframe("1970-01-01", "2100-01-01", db_path=str(db_realistic))
        checks.append(line(
            f"Restauration réussie : {len(restored)} lignes récupérées (attendu {count_before})",
            len(restored) == count_before,
        ))


        # ================= ÉTAPE 8 =================
        banner("8", "Conditions de refroidissement publiées (control/cooling_advice.py)")
        cold_air = compute_cooling_conditions(36.0, 3.0)
        hot_air = compute_cooling_conditions(36.0, 33.0)
        unknown_air = compute_cooling_conditions(36.0, None)
        if cold_air is None or hot_air is None or unknown_air is None:
            raise SystemExit("compute_cooling_conditions() n'a rien retourné pour une température d'huile valide.")
        print(f"   Air froid ({cold_air['outdoor_band']}) : ventilateur à partir de "
              f"{cold_air['fan_on_above_temp']:.1f} °C, arrêt sous {cold_air['fan_off_below_temp']:.1f} °C")
        print(f"   Air chaud ({hot_air['outdoor_band']}) : ventilateur à partir de "
              f"{hot_air['fan_on_above_temp']:.1f} °C, arrêt sous {hot_air['fan_off_below_temp']:.1f} °C")
        checks.append(line(
            "Air froid : le seuil de marche est plus haut que par air chaud (démarrage plus tardif)",
            cold_air["fan_on_above_temp"] > hot_air["fan_on_above_temp"],
        ))
        checks.append(line(
            "Hystérésis : seuil d'arrêt < seuil de marche < seuil critique, pour chaque bande",
            all(c["fan_off_below_temp"] < c["fan_on_above_temp"] < c["critical_temp"]
                for c in (cold_air, hot_air, unknown_air)),
        ))
        checks.append(line(
            "Température extérieure inconnue : seuils neutres, aucune fréquence imposée",
            unknown_air["outdoor_band"] == "inconnue" and unknown_air["fan_min_freq_hz"] is None,
        ))
        checks.append(line(
            "Température d'huile invalide : rien n'est publié",
            compute_cooling_conditions(float("nan"), 20.0) is None,
        ))

        # ================= ÉTAPE 9 =================
        banner("9", "Règle d'alerte « pompe en marche sans débit »")
        pump_state: dict = {}
        pump_alerts: list[dict] = []
        for index in range(config.ALERT_CONFIRMATION_CYCLES):
            pump_alerts = evaluate_rules(
                {"pt02": 36.0, "freq_pompe": 30.0, "ht01": 0.0, "ts": float(index + 1)}, None, pump_state)
        print(f"   30 Hz sans débit pendant {config.ALERT_CONFIRMATION_CYCLES} mesures : "
              f"{[a['rule_name'] for a in pump_alerts]}")
        checks.append(line(
            "Alerte CRITIQUE « pump_without_flow » après la confirmation multi-mesures",
            [a["rule_name"] for a in pump_alerts] == ["pump_without_flow"],
        ))
        normal_state: dict = {}
        normal_alerts: list[dict] = []
        for index in range(config.ALERT_CONFIRMATION_CYCLES + 2):
            normal_alerts = evaluate_rules(
                {"pt02": 36.0, "freq_pompe": 30.0, "ht01": 10.4, "ts": float(index + 1)}, None, normal_state)
        checks.append(line("Aucune alerte quand le débit est normal (30 Hz, 10,4 m³/h)", normal_alerts == []))

        # ================= ÉTAPE 10 =================
        banner("10", "Conservation des sauvegardes compressées : plancher d'un an")
        keep_dir = tmp_path / "backups_retention"
        keep_dir.mkdir()

        def fake_archive(days_old: int) -> Path:
            created = datetime.now(timezone.utc) - timedelta(days=days_old)
            archive = keep_dir / f"voltaneum-{created.strftime('%Y%m%dT%H%M%SZ')}.db.gz"
            archive.write_bytes(b"x")
            return archive

        young_archives = [fake_archive(age) for age in (10, 120, 364)]
        old_archives = [fake_archive(age) for age in (366, 700)]
        create_backup(str(db_realistic), str(keep_dir), retention_count=1,
                      min_age_days=config.STORAGE_BACKUP_MIN_AGE_DAYS)
        checks.append(line(
            f"Archives de moins de {config.STORAGE_BACKUP_MIN_AGE_DAYS} jours : toutes conservées",
            all(path.exists() for path in young_archives),
        ))
        checks.append(line(
            "Archives de plus d'un an et hors des plus récentes : supprimées",
            not any(path.exists() for path in old_archives),
        ))

        # ================= ÉTAPE 11 =================
        banner("11", "Invariant de sécurité : une surchauffe reste mesurable")
        oil_upper = config.SENSOR_VALID_RANGES[config.TARGET_COLUMN][1]
        print(f"   Borne haute valide de {config.TARGET_COLUMN} : {oil_upper} °C ; seuil critique : "
              f"{config.CRITICAL_TEMP} °C")
        checks.append(line(
            "La borne haute valide dépasse le seuil critique (surchauffe jamais rejetée comme hors plage)",
            oil_upper > config.CRITICAL_TEMP,
        ))

    # ================= RÉSUMÉ =================
    banner("FINALE", "Résumé de la simulation")
    passed = sum(checks)
    total = len(checks)
    for ok in checks:
        pass  # déjà imprimé au fil de l'eau
    print(f"{passed}/{total} vérifications réussies.")
    if passed == total:
        print("✅ Simulation complète : tout le pipeline fonctionne comme attendu.")
        return 0
    print("❌ Au moins une vérification a échoué — voir le détail ci-dessus.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
