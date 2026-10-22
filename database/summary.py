"""database/summary.py — Résumé lisible des mesures en base (`./voltaneum data`)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pandas as pd

import config

# Mesures résumées en bas de l'affichage : colonne -> (libellé, unité).
MEASURES: dict[str, tuple[str, str]] = {
    **{column: (labels[0], "°C") for column, labels in config.SENSOR_LABELS.items()},
    "ht01": ("HT01 · débit circuit diélectrique", "m³/h"),
    "freq_pompe": ("Fréquence pompe", "Hz"),
    "temp_outdoor": ("Température extérieure", "°C"),
    "thermal_power_kw": ("Puissance dissipée (estimée)", "kW"),
}


def summarize(df: pd.DataFrame) -> str:
    """Tableau lisible : min et max (avec leur horodatage), moyenne et nombre de valeurs
    de chaque mesure ; une mesure qui n'a jamais varié est signalée."""
    lines = [
        f"Résumé par mesure — du {df['timestamp'].min()} au {df['timestamp'].max()} (UTC), "
        f"{len(df)} ligne(s)",
        "",
        f"{'Mesure':<38} {'Minimum':>12}  {'le':<19}  {'Maximum':>12}  {'le':<19}  "
        f"{'Moyenne':>12}  {'Valeurs':>7}",
        "-" * 130,
    ]
    for column, (label, unit) in MEASURES.items():
        if column not in df:
            continue
        values = pd.to_numeric(df[column], errors="coerce")
        valid = values.dropna()
        if valid.empty:
            lines.append(f"{label:<38} {'aucune valeur':>12}")
            continue
        min_at = df.loc[valid.idxmin(), "timestamp"]
        max_at = df.loc[valid.idxmax(), "timestamp"]
        line = (
            f"{label:<38} {valid.min():>7.1f} {unit:<4}  {min_at:<19}  "
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
    lines.extend(coherence(df))
    return "\n".join(lines)


def coherence(df: pd.DataFrame) -> list[str]:
    """Contrôle physique AU MÊME INSTANT : la sonde chaude de chaque circuit doit être au moins égale
    à la sonde froide (PT02 ≥ PT01 côté diélectrique, PT03 ≥ PT04 côté refroidissement), et une
    sonde lue au degré près est signalée. Une inversion au-delà de 0,5 °C ne s'explique pas par un
    simple arrondi au degré : troncature de la valeur ou décalage d'étalonnage, à voir avec
    l'automaticien."""
    lines = ["", "Cohérence physique (au même instant) :"]
    for hot, cold, circuit in ((config.DIELECTRIC_HOT_COLUMN, config.DIELECTRIC_COLD_COLUMN, "diélectrique"),
                               (config.COOLANT_HOT_COLUMN, config.COOLANT_COLD_COLUMN, "refroidissement")):
        if hot not in df or cold not in df:
            continue
        both = df[["timestamp", hot, cold]].apply(pd.to_numeric, errors="coerce").assign(timestamp=df["timestamp"])
        both = both.dropna(subset=[hot, cold])
        if both.empty:
            continue
        gap = both[cold] - both[hot]                        # > 0 : sonde froide au-dessus de la chaude
        inverted = gap[gap > 0]
        head = f"  {circuit:<15} {hot.upper()} ≥ {cold.upper()} attendu : "
        if inverted.empty:
            lines.append(head + "toujours respecté")
            continue
        worst = inverted.idxmax()
        row = both.loc[worst]
        line = (head + f"inversé sur {len(inverted)} mesure(s) ({len(inverted) / len(both):.1%}), "
                f"au pire de {gap[worst]:.1f} °C le {row['timestamp']} "
                f"({cold.upper()} {row[cold]:.1f}, {hot.upper()} {row[hot]:.1f})")
        if gap[worst] > 0.5:
            line += " -> plus qu'un arrondi au degré : troncature ou étalonnage à vérifier"
        lines.append(line)
    for column in config.DIELECTRIC_COLUMNS + config.COOLANT_COLUMNS:
        if column in df:
            valid = pd.to_numeric(df[column], errors="coerce").dropna()
            if len(valid) > 100 and (valid == valid.round()).mean() > 0.99:
                lines.append(f"  {column.upper()} : {(valid == valid.round()).mean():.0%} de valeurs entières "
                             "-> lue au degré près (au dixième pour un usage fiable)")
    return lines if len(lines) > 2 else []


def load_telemetry(db_path: str, hours: float | None = None) -> pd.DataFrame:
    """Mesures de la base (toutes, ou les `hours` dernières heures), en LECTURE SEULE : une base
    absente n'est jamais créée par erreur."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"base introuvable : {path}")
    query = "SELECT * FROM telemetry"
    params: tuple[str, ...] = ()
    if hours:
        query += " WHERE timestamp >= datetime('now', ?)"
        params = (f"-{hours} hours",)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        return pd.read_sql_query(query + " ORDER BY timestamp", connection, params=params)


def latest_rows(df: pd.DataFrame, count: int) -> str:
    """Les `count` dernières mesures, heure LOCALE, une ligne par instantané."""
    columns = ["timestamp", *[c for c in MEASURES if c in df], *(["temp_outdoor_source"] if "temp_outdoor_source" in df else [])]
    tail = df[columns].tail(count).copy()
    tail["timestamp"] = (pd.to_datetime(tail["timestamp"], utc=True).dt.tz_convert(_local_tz())
                         .dt.strftime("%d/%m %H:%M:%S"))
    tail = tail.rename(columns={"timestamp": "heure locale", "temp_outdoor": "ext", "freq_pompe": "freq_Hz",
                                "ht01": "ht01_m3h", "temp_outdoor_source": "source ext",
                                "thermal_power_kw": "P_kW"})
    return tail.to_string(index=False, na_rep="—", float_format=lambda v: f"{v:.2f}")


def _local_tz():
    return datetime.now().astimezone().tzinfo


def print_summary(db_path: str, rows: int = 10, hours: float | None = None, csv_path: str | None = None) -> int:
    """Affiche le résumé de la base et ses dernières mesures ; exporte en CSV si demandé.
    Retourne un code de sortie (0 = OK)."""
    try:
        df = load_telemetry(db_path, hours)
    except (sqlite3.Error, FileNotFoundError) as exc:
        print(f"Base de données indisponible ({db_path}) : {exc}")
        return 1
    if df.empty:
        period = f" sur les {hours:g} dernières heures" if hours else ""
        print(f"La base {db_path} ne contient aucune mesure{period}.")
        return 0
    print(f"Base : {db_path}" + (f"  (dernières {hours:g} h)" if hours else ""))
    print(summarize(df))
    if rows > 0:
        print(f"\nDernières mesures ({min(rows, len(df))} sur {len(df)}) :")
        print(latest_rows(df, rows))
    if csv_path:
        df.drop(columns=["humidity_outdoor"], errors="ignore").to_csv(csv_path, sep=";", index=False)
        print(f"\nExport CSV : {csv_path} ({len(df)} lignes, horodatage UTC, séparateur « ; »)")
    return 0
