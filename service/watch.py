"""service/watch.py — Supervision dans un simple terminal (`./voltaneum watch`).

Pour un contrôleur sans environnement graphique, joint seulement par un terminal (SSH, VPN) :
mesures, régulation PID, prédiction, conditions, alertes et courbes en caractères, rafraîchis
toutes les quelques secondes. Lecture seule (mêmes lectures que l'interface web).
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import config
from dashboard import data

BLOCKS = "▁▂▃▄▅▆▇█"
MODE_LABELS = {"pid": "PID", "feedforward": "PID + anticipation", "safety_bypass": "SÉCURITÉ 100 %"}
MODEL_LABELS = {"random_forest": "forêt aléatoire", "gradient_boosting": "gradient boosting"}


class _Style:
    """Couleurs ANSI, seulement si la sortie est un vrai terminal."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def ok(self, t: str) -> str:
        return self._wrap("32", t)

    def warn(self, t: str) -> str:
        return self._wrap("33", t)

    def crit(self, t: str) -> str:
        return self._wrap("1;31", t)

    def pred(self, t: str) -> str:
        return self._wrap("35", t)


def _num(value: float | None, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}".replace(".", ",").replace("-", "−") + (f" {unit}" if unit else "")


def _signed(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return ("+" if value >= 0 else "−") + _num(abs(value), digits)


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"


def sparkline(values: list[float | None], width: int) -> tuple[str, float | None, float | None]:
    """Courbe en caractères sur `width` colonnes (valeurs absentes = espace), avec min et max."""
    if width <= 0 or not values:
        return "", None, None
    buckets: list[float | None] = []
    if len(values) < width:  # moins de points que de colonnes : on étire au lieu de laisser des trous
        buckets = [values[i * len(values) // width] for i in range(width)]
    else:
        for i in range(width):
            chunk = [v for v in values[i * len(values) // width:(i + 1) * len(values) // width] if v is not None]
            buckets.append(sum(chunk) / len(chunk) if chunk else None)
    present = [v for v in buckets if v is not None]
    if not present:
        return " " * width, None, None
    lo, hi = min(present), max(present)
    span = hi - lo or 1.0
    line = "".join(" " if v is None else BLOCKS[min(7, int((v - lo) / span * 7.999))] for v in buckets)
    return line, lo, hi


def voltaneum_running() -> bool | None:
    """Vrai si le processus Voltaneum tient son verrou (logs/voltaneum.lock), Faux sinon,
    None si on ne peut pas le savoir (système sans fcntl)."""
    lock = Path(config.PROJECT_ROOT) / "logs" / "voltaneum.lock"
    if not lock.exists():
        return False
    try:
        import fcntl
    except ImportError:
        return None
    with open(lock) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def _ml_status(s: "_Style") -> list[str]:
    """État du modèle de prédiction et du dernier ré-entraînement (une information par ligne)."""
    from ml import auto_retrain

    installed = auto_retrain.installed_model(config.MODEL_DIR)
    parts = [f"modèle installé : prédiction à {auto_retrain.format_horizon(installed['horizon_seconds'])}"
             if installed else "aucun modèle installé"]
    try:
        proposal = auto_retrain.pending_proposal(config.MODEL_DIR)
        parts.append(s.pred(f"NOUVEAU MODÈLE PROPOSÉ ({auto_retrain.format_horizon(proposal['proposed']['horizon_seconds'])})"
                            " -> ./voltaneum model"))
    except auto_retrain.ProposalError:
        pass
    runs = [e for e in auto_retrain.read_history(config.MODEL_DIR, limit=50) if e.get("event") == "run"]
    if runs:
        last = runs[0]
        when = datetime.fromisoformat(last["started_at"]).astimezone().strftime("%d/%m %H:%M")
        parts.append(f"dernier ré-entraînement {when} : {last.get('reason', '?')}")
        refused = {c.get("reason") for c in last.get("candidates", []) if not c.get("eligible")}
        if len(refused) == 1:
            parts.append(f"cause : {refused.pop()}")
    elif config.RETRAIN_AT:
        parts.append("aucun ré-entraînement encore (lancé 5 min après le démarrage sans modèle, puis chaque "
                     f"jour à {config.RETRAIN_AT}) ; à la main : ./voltaneum retrain")
    else:
        parts.append("ré-entraînement automatique désactivé (RETRAIN_AT vide)")
    return parts


def _bar(percent: float, width: int = 30) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def render(db_path: str, minutes: int = 60, width: int = 100, color: bool = False) -> str:
    """Construit l'écran complet (texte). Lève data.DatabaseUnavailable si la base est absente."""
    s = _Style(color)
    snap = data.latest_snapshot(db_path)
    th = snap["thresholds"]
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    lines: list[str] = []
    rule = s.dim("─" * width)
    label_w = 20

    def row(label: str, text: str) -> None:
        lines.append(f"{s.dim(label.ljust(label_w))}{text}")

    if snap["empty"]:
        lines += [s.bold(f"VOLTANEUM · {now}"), rule, "Aucune mesure en base pour l'instant (./voltaneum status)."]
        return "\n".join(lines)

    age = snap["age_seconds"]
    fresh = (s.ok("OK") if age <= th["snapshot_interval_seconds"] * 3
             else s.warn("EN RETARD") if age <= th["stale_after_seconds"] else s.crit("MESURES PÉRIMÉES"))
    running = voltaneum_running()
    process = {True: s.ok("Voltaneum en cours"), False: s.crit("Voltaneum ARRÊTÉ"), None: ""}[running]
    lines.append(f"{s.bold('VOLTANEUM')} · {now} · {process} · dernière mesure il y a {_ago(age)} · {fresh}")
    if running is False and age <= th["stale_after_seconds"]:
        lines.append(s.crit("Des mesures arrivent mais Voltaneum n'est pas démarré : une ANCIENNE collecte écrit "
                            "encore en base (pas de PID, pas d'alertes, pas de ML). Corriger : ./voltaneum restart"))
    lines.append(rule)

    v = snap["values"]
    pt02 = v["pt02"]
    pt02_txt = _num(pt02, 1, "°C")
    if pt02 is not None and pt02 > th["critical_temp"]:
        pt02_txt = s.crit(pt02_txt + " CRITIQUE")
    trend = snap.get("pt02_trend_10min")
    row("Régulée (PT02)", f"{s.bold(pt02_txt)}  cible {_num(th['target_temp'], 1, '°C')}"
        + (f"  tendance {_signed(trend, 1)} °C / 10 min" if trend is not None else ""))
    def pair(columns: tuple[str, ...]) -> str:
        text = "   ".join(f"{c.upper()} {_num(v[c], 1, '°C')}" for c in columns)
        known = [v[c] for c in columns if v[c] is not None]
        return text + (f"   (écart {_num(max(known) - min(known), 1)} °C)" if len(columns) == 2 == len(known) else "")

    # Liquide diélectrique (cuve) : seuil critique sur les sondes autres que PT02 (PT02 a le sien).
    others = [v[c] for c in config.DIELECTRIC_COLUMNS if c != config.TARGET_COLUMN and v[c] is not None]
    diel_txt = pair(config.DIELECTRIC_COLUMNS)
    if others and max(others) > th.get("dielectric_critical_temp", th["critical_temp"]):
        diel_txt = s.crit(diel_txt + "  CRITIQUE")
    row("Diélectrique", diel_txt)
    row("Refroidissement", pair(config.COOLANT_COLUMNS))
    source = {"sensor": "capteur", "weather": "météo"}.get(snap.get("temp_outdoor_source") or "", "absente")
    row("Pompe et air", f"fréquence {_num(v['freq_pompe'], 1, 'Hz')}   débit HT01 {_num(v['ht01'], 2, 'm³/h')}   "
        f"air extérieur {_num(v['temp_outdoor'], 1, '°C')} ({source})")
    tp = th.get("thermal_power", {})
    status = "confirmée" if tp.get("confirmed") else "ESTIMATION, fluide et sondes à confirmer"
    row("Puissance", f"{s.bold(_num(v.get('thermal_power_kw'), 1, 'kW'))} dissipée  "
        + s.dim(f"(débit × {tp.get('hot', 'pt03').upper()}−{tp.get('cold', 'pt04').upper()} ; {status})"))
    lines.append(rule)

    c = snap.get("control")
    if not c:
        if running is False:
            reason = "Voltaneum n'est pas démarré (./voltaneum restart)"
        elif pt02 is None:
            reason = "PT02 absente : aucune consigne n'est calculée sans la grandeur régulée"
        elif age > th["stale_after_seconds"]:
            reason = "mesures périmées : la régulation est suspendue"
        else:
            reason = "premier calcul dans moins d'une minute après le démarrage"
        row("Régulation PID", s.warn(f"aucun calcul enregistré — {reason}"))
    else:
        mode = MODE_LABELS.get(c["mode"], c["mode"])
        mode = s.crit(mode) if c["mode"] == "safety_bypass" else mode
        stale = c["age_seconds"] > c["interval_seconds"] * 3
        row("Régulation PID", f"sortie PID {s.bold(_num(c['setpoint'], 1, '%'))}  {_bar(c['setpoint'] or 0)}  {mode}"
            + (s.warn(f"  (calcul vieux de {_ago(c['age_seconds'])})") if stale else ""))
        if c.get("p_term") is not None:
            ff = (c.get("feedforward_prediction") or 0) + (c.get("feedforward_outdoor") or 0)
            row("", f"écart {_signed(c['error'], 2)} °C → P {_signed(c['p_term'])}  I {_signed(c['i_term'])}  "
                f"D {_signed(c['d_term'])}  anticipation {_signed(ff)}  = {_num(c['raw_output'], 1)} % avant bornage")
        gains = " / ".join(f"{c['gains'][k]:g}".replace(".", ",") for k in ("kp", "ki", "kd"))
        row("", f"gains Kp / Ki / Kd {gains} · calculé chaque minute, envoyé seulement aux étapes")
        # Régulation par étapes : étape en cours et dernier message réellement envoyé.
        if c.get("phase"):
            phase = {"repos": "repos (rien à envoyer, l'automate régule seul)",
                     "air_exterieur": "AIR EXTÉRIEUR SEUL (ventilateur arrêté)",
                     "ventilateur": f"VENTILATEUR {_num(c.get('command_percent'), 0, '%')}",
                     "critique": s.crit("CRITIQUE : ventilateur 100 %")}.get(c["phase"], c["phase"])
            row("Étape", s.bold(phase) if c["phase"] != "repos" else phase)
            if c.get("cooling_mode_reason"):
                row("", c["cooling_mode_reason"])
        last = c.get("last_event")
        if last:
            when = datetime.fromtimestamp(last["ts"] / 1000).strftime("%d/%m %H:%M")
            sent = {1: s.ok("transmis"), 0: s.warn("NON transmis (broker injoignable)")}.get(last.get("mqtt_sent"), "")
            row("Dernier envoi", f"{when} · {last.get('event')} · {sent}")
        elif c.get("phase"):
            row("Dernier envoi", s.dim("aucun : PT02 ne s'est pas réchauffée"))

    p = snap.get("predictions")
    if not p:
        row("Prédiction ML", s.dim("aucune prédiction récente"))
    else:
        parts = []
        for name, m in p["models"].items():
            mark = "*" if name == p["best_model"] else " "
            parts.append(f"{mark}{MODEL_LABELS.get(name, name)} {_num(m['value'], 2, '°C')}")
        horizon = next(iter(p["models"].values()))["horizon_seconds"]
        dis = p.get("disagreement")
        dis_txt = "" if dis is None else (s.warn(f"  désaccord {_num(dis, 2)} °C") if dis > p["disagreement_threshold"]
                                         else f"  écart {_num(dis, 2)} °C")
        row("Prédiction ML", s.pred(f"PT02 dans {horizon // 60} min : " + "   ".join(parts)) + dis_txt)

    for i, part in enumerate(_ml_status(s)):
        row("Modèle ML" if i == 0 else "", part)

    cool = snap.get("cooling")
    if cool:
        band = {"froid": "froid", "tempere": "tempéré", "chaud": "chaud"}.get(cool["outdoor_band"], "inconnue")
        row("Conditions", f"air {band} · ventilateur marche > {_num(cool['fan_on_above_temp'])} °C, "
            f"arrêt < {_num(cool['fan_off_below_temp'])} °C · fréquence min. "
            f"{_num(cool['fan_min_freq_hz'], 0, 'Hz') if cool['fan_min_freq_hz'] is not None else 'non conseillée'}")
    lines.append(rule)

    end = time.time()
    series = data.telemetry_series(db_path, end - minutes * 60, end, max_points=600)
    control = data.control_series(db_path, end - minutes * 60, end, max_points=600)
    graph_w = max(10, width - label_w - 22)
    for label, values, unit, digits in (
        (f"PT02 · {minutes} min", series["series"]["pt02"], "°C", 1),
        (f"Fréquence · {minutes} min", series["series"]["freq_pompe"], "Hz", 0),
        (f"Puissance · {minutes} min", series["series"]["thermal_power_kw"], "kW", 1),
        (f"Consigne · {minutes} min", control["setpoint"], "%", 0),
    ):
        line, lo, hi = sparkline(values, graph_w)
        if lo is None:
            scale = " pas de données"
        elif hi == lo:  # valeur strictement constante : ligne médiane, signalée comme telle
            line = "".join("▄" if ch != " " else " " for ch in line)
            scale = f" {_num(lo, digits)} {unit} CONSTANTE"
            scale = s.warn(scale) if label.startswith("PT02") else scale
        else:
            scale = f" {_num(lo, digits)}–{_num(hi, digits)} {unit}"
        row(label, line + s.dim(scale))
    lines.append(rule)

    recent = data.alerts(db_path, limit=5, since=end - 86400)
    if not recent["items"]:
        row("Alertes (24 h)", s.ok("aucune"))
    for i, a in enumerate(recent["items"]):
        when = datetime.fromtimestamp(a["ts"] / 1000).strftime("%d/%m %H:%M") if a["ts"] else "—"
        level = {"CRITIQUE": s.crit, "ATTENTION": s.warn}.get(a["level"], s.dim)(a["level"].lower())
        message = (a["message"] or a["rule"]).split("\n")[0]
        room = max(10, width - label_w - 25)
        row("Alertes (24 h)" if i == 0 else "", f"{when}  {level:<9}  {message[:room]}")
    return "\n".join(lines)


def run_watch(db_path: str, interval: float = 5.0, minutes: int = 60, once: bool = False) -> int:
    """Affiche l'écran et le rafraîchit toutes les `interval` secondes (Ctrl+C pour quitter)."""
    color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    try:
        while True:
            width = min(140, shutil.get_terminal_size((100, 40)).columns)
            try:
                screen = render(db_path, minutes, width, color)
            except data.DatabaseUnavailable as exc:
                screen = f"Base indisponible : {exc}"
            if once:
                print(screen)
                return 0
            sys.stdout.write("\033[H\033[2J" + screen + "\n\n" + "Ctrl+C pour quitter · rafraîchi toutes les "
                             f"{interval:g} s · lecture seule\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0
