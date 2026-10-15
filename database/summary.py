"""database/summary.py — Résumé lisible des mesures en base (`./voltaneum data`)."""

from __future__ import annotations

import sqlite3

import pandas as pd

from database import sqlite_client

# Mesures résumées en bas de l'affichage : colonne -> (libellé, unité).
MEASURES: dict[str, tuple[str, str]] = {
    "pt01": ("PT01 - température circuit huile", "°C"),
    "pt02": ("PT02 - température huile (cible)", "°C"),
    "pt03": ("PT03 - température dry cooler", "°C"),
    "pt04": ("PT04 - température dry cooler", "°C"),
    "ht01": ("HT01 - débit circuit huile", "m³/h"),
    "freq_pompe": ("Fréquence pompe / ventilateur", "Hz"),
    "temp_outdoor": ("Température extérieure", "°C"),
}


def summarize(df: pd.DataFrame) -> str:
    """Tableau lisible : min et max (avec leur horodatage), moyenne et nombre de valeurs
    de chaque mesure ; une mesure qui n'a jamais varié est signalée."""
    lines = [
        f"Résumé par mesure — du {df['timestamp'].min()} au {df['timestamp'].max()} (UTC), "
        f"{len(df)} ligne(s)",
        "",
        f"{'Mesure':<34} {'Minimum':>12}  {'le':<19}  {'Maximum':>12}  {'le':<19}  "
        f"{'Moyenne':>12}  {'Valeurs':>7}",
        "-" * 130,
    ]
    for column, (label, unit) in MEASURES.items():
        if column not in df:
            continue
        values = pd.to_numeric(df[column], errors="coerce")
        valid = values.dropna()
        if valid.empty:
            lines.append(f"{label:<34} {'aucune valeur':>12}")
            continue
        min_at = df.loc[valid.idxmin(), "timestamp"]
        max_at = df.loc[valid.idxmax(), "timestamp"]
        line = (
            f"{label:<34} {valid.min():>7.1f} {unit:<4}  {min_at:<19}  "
            f"{valid.max():>7.1f} {unit:<4}  {max_at:<19}  "
            f"{valid.mean():>7.1f} {unit:<4}  {len(valid):>7}"
        )
        if len(valid) > 1 and valid.min() == valid.max():
            line += "   <- n'a jamais varié"
        lines.append(line)

    if "temp_outdoor_source" in df:
        sources = df["temp_outdoor_source"].value_counts()
        lines.append("")
        lines.append(
            "Température extérieure : "
            f"{int(sources.get('sensor', 0))} mesure(s) du capteur, "
            f"{int(sources.get('weather', 0))} de l'API météo, "
            f"{int(df['temp_outdoor'].isna().sum())} absente(s)."
        )
    return "\n".join(lines)


def print_summary(db_path: str) -> int:
    """Affiche le résumé de la base ; retourne un code de sortie (0 = OK)."""
    try:
        with sqlite_client.connect(db_path) as connection:
            df = pd.read_sql_query("SELECT * FROM telemetry", connection)
    except (sqlite3.Error, FileNotFoundError) as exc:
        print(f"Base de données indisponible ({db_path}) : {exc}")
        return 1
    if df.empty:
        print(f"La base {db_path} ne contient encore aucune mesure.")
        return 0
    print(f"Base : {db_path}")
    print(summarize(df))
    return 0
