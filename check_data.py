"""Inspect a telemetry SQLite database.

Usage:
    python check_data.py [database_path]
"""

import argparse
import sqlite3

import pandas as pd

from config import SQLITE_DB_PATH
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database",
        nargs="?",
        default=SQLITE_DB_PATH,
        help=f"SQLite database path (default: {SQLITE_DB_PATH})",
    )
    args = parser.parse_args()

    try:
        with sqlite_client.connect(args.database) as connection:
            df = pd.read_sql_query("SELECT * FROM telemetry", connection)

            if df.empty:
                print(f"La table telemetry de {args.database} est vide.")
                return

            pd.set_option("display.max_columns", None)
            pd.set_option("display.width", 1000)

            print(f"Base : {args.database}")
            print(f"Lignes : {len(df)}")
            print(df.head())
            if len(df) > 5:
                print("...")
                print(df.tail())

            numeric_columns = [
                column
                for column in ("pt02", "freq_pompe", "temp_outdoor")
                if column in df
            ]
            if numeric_columns:
                print("\nStatistiques de simulation/capteurs :")
                print(df[numeric_columns].describe().round(3).to_string())

            print()
            print(summarize(df))

    except (sqlite3.Error, FileNotFoundError) as exc:
        print(f"Base de données indisponible ({args.database}) : {exc}")


if __name__ == "__main__":
    main()