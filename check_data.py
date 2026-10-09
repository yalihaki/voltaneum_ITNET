"""Inspect a telemetry SQLite database.

Usage:
    python check_data.py [database_path]
"""

import argparse
import sqlite3

import pandas as pd

from config import SQLITE_DB_PATH
from database import sqlite_client


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

    except (sqlite3.Error, FileNotFoundError) as exc:
        print(f"Base de données indisponible ({args.database}) : {exc}")


if __name__ == "__main__":
    main()