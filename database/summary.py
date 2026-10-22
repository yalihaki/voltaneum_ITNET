"""database/summary.py — Résumé lisible des mesures en base (`./voltaneum data`)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from database.quality import SPIKE_COLUMNS, drop_spikes, spike_mask

# Mesures résumées en bas de l'affichage : colonne -> (libellé, unité).
MEASURES: dict[str, tuple[str, str]] = {
    **{column: (labels[0], "°C") for column, labels in config.SENSOR_LABELS.items()},
    "ht01": ("HT01 · débit circuit diélectrique", "m³/h"),
    "freq_pompe": ("Fréquence pompe", "Hz"),
    "temp_outdoor": ("Température extérieure", "°C"),
    "thermal_power_kw": ("Puissance (estimée, moy. 10 min)", "kW"),
}

# Fenêtre de la moyenne glissante de la puissance (s) et nombre minimal de mesures pour la calculer.
POWER_MEAN_WINDOW_SECONDS = 600
POWER_MEAN_MIN_SAMPLES = 20


def power_mean(df: pd.DataFrame) -> pd.Series:
    """Puissance dissipée en MOYENNE GLISSANTE sur 10 min, comme la tuile de l'interface.

    Pourquoi : la puissance instantanée vaut débit × (PT02 − PT01) avec un écart d'environ 0,6 °C et
    PT02 lue au degré près ; une marche de PT02 fait passer la valeur instantanée de −2 à +7 kW, ce
    qui n'a aucun sens physique (les équipements ne refroidissent pas le liquide). La moyenne sur
    10 min (≈ 40 mesures) lisse cette marche. Les mesures où PT02 ou PT01 est un pic isolé
    (database/quality.py) sont écartées avant la moyenne ; une fenêtre trop pauvre (moins de
    POWER_MEAN_MIN_SAMPLES mesures, par exemple après un trou de collecte) ne donne pas de valeur."""
    if "thermal_power_kw" not in df:
        return pd.Series(dtype=float)
    power = pd.to_numeric(df["thermal_power_kw"], errors="coerce")
    for column in (config.THERMAL_POWER_HOT, config.THERMAL_POWER_COLD):
        if column in df:
            power = power.where(~spike_mask(df[column]))
    indexed = pd.Series(power.to_numpy(), index=pd.to_datetime(df["timestamp"]))
    mean = indexed.rolling(f"{POWER_MEAN_WINDOW_SECONDS}s", min_periods=POWER_MEAN_MIN_SAMPLES).mean()
    return pd.Series(mean.to_numpy(), index=df.index)


def summarize(df: pd.DataFrame) -> str:
    """Tableau lisible : min et max (avec leur horodatage), moyenne et nombre de valeurs
    de chaque mesure ; une mesure qui n'a jamais varié est signalée.

    Les pics isolés des sondes de température (database/quality.py) sont écartés du minimum, du
    maximum et de la moyenne — sinon une seule mesure parasite devient le « record » de la période —
    puis comptés et datés dans la section « Qualité des mesures ». La puissance est résumée en
    moyenne glissante sur 10 min (voir `power_mean`)."""
    raw = df.sort_values("timestamp").reset_index(drop=True)
    df, _ = drop_spikes(raw)
    if "thermal_power_kw" in df:
        df["thermal_power_kw"] = power_mean(raw)
    width = max(38, *(len(label) for label, _ in MEASURES.values()))     # noms réglables dans .env
    lines = [
        f"Résumé par mesure — du {df['timestamp'].min()} au {df['timestamp'].max()} (UTC), "
        f"{len(df)} ligne(s)",
        "",
        f"{'Mesure':<{width}} {'Minimum':>12}  {'le':<19}  {'Maximum':>12}  {'le':<19}  "
        f"{'Moyenne':>12}  {'Valeurs':>7}",
        "-" * (width + 92),
    ]
    for column, (label, unit) in MEASURES.items():
        if column not in df:
            continue
        values = pd.to_numeric(df[column], errors="coerce")
        valid = values.dropna()
        if valid.empty:
            lines.append(f"{label:<{width}} {'aucune valeur':>12}")
            continue
        min_at = df.loc[valid.idxmin(), "timestamp"]
        max_at = df.loc[valid.idxmax(), "timestamp"]
        line = (
            f"{label:<{width}} {valid.min():>7.1f} {unit:<4}  {min_at:<19}  "
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
    lines.extend(spike_report(raw))
    lines.extend(coherence(df))
    return "\n".join(lines)


def spike_report(df: pd.DataFrame, shown: int = 5) -> list[str]:
    """Pics isolés par sonde, avec leurs premières dates (à transmettre à l'automaticien : ce sont des
    erreurs de lecture de la sonde, du module d'entrée ou de la transmission)."""
    lines = ["", f"Qualité des mesures (pic isolé : écart de plus de {config.SPIKE_THRESHOLD_CELSIUS:g} °C avec la "
                 "moyenne des deux mesures voisines, qui sont d'accord entre elles) :"]
    found = False
    for column in SPIKE_COLUMNS:
        if column not in df:
            continue
        mask = spike_mask(df[column])
        if not mask.any():
            continue
        found = True
        when = ", ".join(f"{df.loc[i, 'timestamp']} ({pd.to_numeric(df.loc[i, column]):.1f} °C)"
                         for i in mask[mask].index[:shown])
        more = f" … (+{int(mask.sum()) - shown})" if mask.sum() > shown else ""
        lines.append(f"  {column.upper()} : {int(mask.sum())} pic(s), écartés des statistiques — {when}{more}")
    if not found:
        lines.append("  aucun pic isolé")
    return lines


# Précision cumulée de deux sondes de température industrielles (°C) : deux sondes saines peuvent
# différer d'autant en mesurant le même liquide (PT100 classe A : ±0,15 °C vers 35 °C chacune).
PROBE_ACCURACY_CELSIUS = 0.2


def resolution(values: pd.Series) -> float:
    """Résolution apparente d'une sonde : 1 °C si (presque) toutes ses valeurs sont entières (cas de
    PT02), sinon 0,1 °C."""
    valid = pd.to_numeric(values, errors="coerce").dropna()
    return 1.0 if len(valid) > 100 and (valid == valid.round()).mean() > 0.99 else 0.1


def coherence(df: pd.DataFrame) -> list[str]:
    """Contrôle physique AU MÊME INSTANT, sur les mesures sans pics isolés : la sonde chaude de chaque
    circuit doit être au moins égale à la froide (PT02 ≥ PT01 côté diélectrique, PT03 ≥ PT04 côté
    refroidissement).

    Une inversion peut avoir trois causes, que le message distingue :
      - l'ARRONDI et la PRÉCISION des sondes : demi-résolution de chaque sonde (0,5 °C pour PT02 lue
        au degré, 0,05 °C pour une sonde au dixième) plus PROBE_ACCURACY_CELSIUS ; une inversion
        sous ce total (0,75 °C côté diélectrique, 0,3 °C côté refroidissement) est normale ;
      - un TRANSITOIRE : chaque sonde voit le liquide qui passe devant elle maintenant, le liquide
        met du temps à faire le tour ; au démarrage / arrêt du ventilateur, la sonde froide peut
        passer brièvement au-dessus de la chaude (vu sur PT03 / PT04, au dixième toutes les deux) ;
      - un ÉTALONNAGE ou une troncature : inversion durable et fréquente.
    La commande `--around` proposée affiche les mesures autour du pire cas pour trancher."""
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
        rounding = (resolution(both[hot]) + resolution(both[cold])) / 2 + PROBE_ACCURACY_CELSIUS
        lines.append(head + f"inversé sur {len(inverted)} mesure(s) ({len(inverted) / len(both):.1%}), "
                     f"au pire de {gap[worst]:.1f} °C le {row['timestamp']} "
                     f"({cold.upper()} {row[cold]:.1f}, {hot.upper()} {row[hot]:.1f})")
        if gap[worst] <= rounding + 1e-9:
            lines.append(f"{'':<18}-> dans l'arrondi et la précision des sondes (jusqu'à {rounding:.2f} °C) : normal")
        else:
            lines.append(f"{'':<18}-> au-delà de l'arrondi et de la précision des sondes ({rounding:.2f} °C) : transitoire probable "
                         "(démarrage / arrêt du ventilateur) ; si l'écart dure, étalonnage ou troncature")
            lines.append(f"{'':<21}vérifier : ./voltaneum data --around \"{str(row['timestamp'])[:16]}\"")
    for column in config.DIELECTRIC_COLUMNS + config.COOLANT_COLUMNS:
        if column in df and resolution(df[column]) == 1.0:
            valid = pd.to_numeric(df[column], errors="coerce").dropna()
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


def around(db_path: str, at: str, minutes: float = 10) -> str:
    """Mesures de `minutes` avant à `minutes` après l'instant `at` (UTC, comme les dates du
    résumé), avec les écarts chaud − froid de chaque circuit : un écart négatif est une inversion.
    Sert à voir si une inversion est un transitoire (évolution progressive) ou un pic isolé."""
    center = pd.Timestamp(at)
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"base introuvable : {path}")
    start = (center - pd.Timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    end = (center + pd.Timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        df = pd.read_sql_query("SELECT * FROM telemetry WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
                               connection, params=(start, end))
    if df.empty:
        return f"Aucune mesure entre {start} et {end} (UTC)."
    hot_d, cold_d = config.DIELECTRIC_HOT_COLUMN, config.DIELECTRIC_COLD_COLUMN
    hot_c, cold_c = config.COOLANT_HOT_COLUMN, config.COOLANT_COLD_COLUMN
    table = pd.DataFrame({"heure UTC": pd.to_datetime(df["timestamp"]).dt.strftime("%H:%M:%S")})
    for column in ("pt01", "pt02", "pt03", "pt04", "ht01", "freq_pompe"):
        if column in df:
            table[column] = pd.to_numeric(df[column], errors="coerce")
    table[f"{hot_d}-{cold_d}"] = pd.to_numeric(df[hot_d], errors="coerce") - pd.to_numeric(df[cold_d], errors="coerce")
    table[f"{hot_c}-{cold_c}"] = pd.to_numeric(df[hot_c], errors="coerce") - pd.to_numeric(df[cold_c], errors="coerce")
    nearest = (pd.to_datetime(df["timestamp"]) - center).abs().idxmin()
    table[""] = ["<- instant demandé" if i == nearest else "" for i in table.index]
    header = (f"Mesures du {start} au {end} (UTC), {len(df)} ligne(s). Écart chaud − froid négatif = "
              "sonde froide au-dessus de la chaude.")
    return header + "\n" + table.to_string(index=False, na_rep="—", float_format=lambda v: f"{v:.2f}")


def _local_tz():
    return datetime.now().astimezone().tzinfo


def print_summary(db_path: str, rows: int = 10, hours: float | None = None, csv_path: str | None = None,
                  at: str | None = None, minutes: float = 10) -> int:
    """Affiche le résumé de la base et ses dernières mesures ; exporte en CSV si demandé.
    Avec `at` : seulement les mesures autour de cet instant (voir `around`).
    Retourne un code de sortie (0 = OK)."""
    if at:
        try:
            print(around(db_path, at, minutes))
        except (sqlite3.Error, FileNotFoundError, ValueError) as exc:
            print(f"Impossible d'afficher les mesures autour de {at!r} : {exc}")
            return 1
        return 0
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
