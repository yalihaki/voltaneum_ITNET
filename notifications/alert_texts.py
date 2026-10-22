"""notifications/alert_texts.py — Contenu des e-mails d'alerte, explicite et autonome.

Un e-mail d'alerte doit se comprendre SANS ouvrir l'interface ni connaître le nom interne de la
règle. Avant (jusqu'au 22/10/2026) : sujet « [Voltaneum][CRITIQUE] safety_bypass_active », corps
d'une ligne. Désormais, chaque e-mail contient :

  - un SUJET lisible : niveau et titre en clair (« Shunt de sécurité actif sur l'automate ») ;
  - CE QUI SE PASSE : le message de la règle, avec les valeurs mesurées et le seuil ;
  - POURQUOI C'EST IMPORTANT : la conséquence pour l'installation ;
  - QUE FAIRE : les vérifications concrètes, dans l'ordre ;
  - LES MESURES AU MOMENT DE L'ALERTE : toutes les sondes, la cible, l'étape de régulation ;
  - POUR VÉRIFIER SUR LE SERVEUR : les commandes `./voltaneum` utiles pour cette alerte ;
  - QUAND IL SERA RAPPELÉ : le délai avant un nouvel e-mail si la situation dure.

Le journal (alerts_log, onglet Alertes) garde le message court ; seul l'e-mail est enrichi.
"""

from __future__ import annotations

import socket
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone

import config


@dataclass(frozen=True)
class RuleText:
    title: str          # titre en clair (sujet de l'e-mail)
    why: str            # pourquoi c'est important
    actions: tuple[str, ...]   # que faire, dans l'ordre
    commands: tuple[str, ...] = ("./voltaneum watch",)   # pour vérifier sur le serveur


_SITE = ("vérifier sur site : ventilateur du dry cooler (tourne-t-il ?), pompe et débit HT01, CDU, "
         "charge des équipements de l'armoire")

RULES: dict[str, RuleText] = {
    # --- Températures -------------------------------------------------------------------------
    "critical_temp": RuleText(
        "PT02 au-delà du seuil critique",
        "PT02 est la température du diélectrique à la sortie de l'armoire : celle que voient les "
        "équipements. Au-delà du seuil critique, ils risquent une surchauffe ou un arrêt de protection.",
        ("intervenir sans attendre : " + _SITE,
         "Voltaneum conseille déjà le refroidissement maximal (étape « critique ») mais ne commande rien "
         "lui-même : c'est l'automate qui agit",
         "si la température continue de monter, réduire la charge de l'armoire"),
        ("./voltaneum watch", "./voltaneum data --hours 2 --rows 20")),
    "dielectric_critical_temp": RuleText(
        "Diélectrique renvoyé dans l'armoire trop chaud (PT01)",
        "PT01 est le diélectrique refroidi par la CDU puis renvoyé dans l'armoire. Trop chaud, il ne "
        "refroidit plus les équipements : la CDU ou le dry cooler n'évacuent plus la chaleur.",
        ("vérifier la CDU et le circuit d'eau glycolée (PT03, PT04)", _SITE),
        ("./voltaneum watch", "./voltaneum data --hours 2 --rows 20")),
    "pump_without_flow": RuleText(
        "Pompe en marche sans débit",
        "La pompe tourne mais le diélectrique ne circule pas : l'armoire n'est plus refroidie et la pompe "
        "risque de tourner à sec.",
        ("vérifier les vannes du circuit diélectrique, l'amorçage de la pompe et le capteur de débit HT01",
         "si le débit ne revient pas, arrêter la pompe (décision sur site)"),
        ("./voltaneum watch",)),
    "telemetry_lost": RuleText(
        "Plus aucune mesure de la passerelle",
        "Voltaneum ne reçoit plus rien : ni surveillance, ni alerte de température, ni prédiction tant que "
        "la liaison n'est pas revenue. L'automate continue de réguler seul.",
        ("vérifier que la passerelle est alimentée et joignable (réseau, VPN)",
         "lancer ./voltaneum diagnose sur le serveur",
         "consulter le journal : ./voltaneum logs"),
        ("./voltaneum diagnose", "./voltaneum logs")),
    "oil_sensor_missing": RuleText(
        "PT02 absente",
        "Sans PT02, Voltaneum ne peut ni surveiller l'armoire ni conseiller l'automate : aucune consigne "
        "n'est publiée tant qu'elle manque.",
        ("vérifier la sonde PT02 et son point sur la passerelle (avec l'automaticien)",),
        ("./voltaneum catalogue", "./voltaneum watch")),
    "oil_sensor_frozen": RuleText(
        "PT02 ne bouge plus (sonde ou point suspect)",
        "PT02 garde exactement la même valeur depuis longtemps alors que la moyenne de PT01 a nettement "
        "changé : la valeur reçue ne suit peut-être plus la réalité (sonde bloquée, mauvais point lu).",
        ("comparer PT02 sur l'automate et dans Voltaneum",
         "vérifier le point PT02 de la passerelle avec l'automaticien"),
        ("./voltaneum data --hours 3 --rows 0", "./voltaneum catalogue")),
    "cooling_ineffective": RuleText(
        "Refroidissement insuffisant malgré le maximum conseillé",
        "La régulation par étapes est arrivée à 100 % et PT02 ne baisse toujours pas : soit la consigne "
        "n'est pas appliquée, soit l'installation ne peut pas évacuer plus de chaleur.",
        ("vérifier que le ventilateur du dry cooler tourne vraiment",
         "vérifier que l'automate reçoit et applique les conseils de Voltaneum (broker MQTT)",
         "par air chaud, la limite physique du dry cooler peut être atteinte : réduire la charge"),
        ("./voltaneum watch",)),
    # --- Installation (catalogue de l'automate, PT04) -------------------------------------------
    "safety_bypass_active": RuleText(
        "Shunt de sécurité actif sur l'automate",
        "Un shunt supprime une protection : avec l'arrêt d'urgence shunté, un appui sur l'arrêt d'urgence "
        "n'arrête plus l'installation ; avec le contrôle d'ordre des phases shunté, une inversion de "
        "phases n'est plus détectée (moteurs qui tournent à l'envers).",
        ("vérifier avec l'automaticien pourquoi le shunt est actif",
         "le lever dès que sa raison a disparu (intervention, essai...)"),
        ("./voltaneum catalogue",)),
    "automate_setpoint_mismatch": RuleText(
        "La consigne de l'automate n'est pas la cible de Voltaneum",
        "L'automate et Voltaneum ne visent pas la même température : les conseils de Voltaneum et la "
        "régulation de l'automate peuvent se contredire.",
        ("choisir la bonne température avec l'automaticien",
         "mettre la même valeur dans TARGET_TEMP (fichier .env) puis ./voltaneum restart"),
        ("./voltaneum catalogue",)),
    "fan_short_cycling": RuleText(
        "Ventilateur du dry cooler : démarrages trop fréquents",
        "Chaque démarrage sollicite le moteur et ses contacteurs. Des démarrages toutes les quelques "
        "minutes réduisent fortement leur durée de vie (court-cycle).",
        ("demander à l'automaticien d'élargir l'hystérésis du ventilateur, ou d'imposer une durée "
         "minimale de marche et d'arrêt",),
        ("./voltaneum data --hours 1 --rows 0",)),
    "automate_mode_changed": RuleText(
        "Un réglage ou le mode de l'automate a changé",
        "Un changement de mode (automatique / manuel), de marche / arrêt, de consigne ou d'un shunt modifie "
        "la façon dont l'installation est pilotée. En mode manuel, l'automate ne régule plus seul.",
        ("vérifier que le changement est voulu (intervention en cours ?)",
         "s'il ne l'est pas, prévenir l'automaticien"),
        ("./voltaneum catalogue --changes", "./voltaneum catalogue")),
    # --- Mesures et modèles ---------------------------------------------------------------------
    "efficiency_drift": RuleText(
        "Efficacité de la CDU hors de la plage attendue",
        "La CDU échange moins bien la chaleur qu'attendu : encrassement, débit insuffisant ou sonde "
        "défaillante.",
        ("vérifier les débits des deux circuits et l'état de la CDU",),
        ("./voltaneum data --hours 24 --rows 0",)),
    "model_disagreement": RuleText(
        "Les deux modèles de prédiction ne sont pas d'accord",
        "Les deux modèles prévoient des températures très différentes : la situation ne ressemble pas à "
        "ce qu'ils ont appris. Leur prédiction est ignorée par la régulation tant que le désaccord dure.",
        ("rien d'urgent ; si cela dure, lancer un ré-entraînement (./voltaneum retrain)",),
        ("./voltaneum predictions", "./voltaneum watch")),
    "outdoor_sensor_weather_mismatch": RuleText(
        "Capteur extérieur et météo en désaccord",
        "Le capteur de température extérieure et l'API météo donnent des valeurs très différentes : le "
        "capteur est peut-être au soleil, mal placé ou défaillant.",
        ("vérifier l'emplacement et l'état du capteur extérieur",),
        ("./voltaneum watch",)),
    # --- Stockage -------------------------------------------------------------------------------
    "storage_warning": RuleText(
        "Stockage de la base presque plein",
        "Si la base atteint sa limite, les mesures les plus anciennes seront purgées.",
        ("libérer de la place sur le disque ou augmenter STORAGE_LIMIT_BYTES",),
        ("./voltaneum diagnose",)),
    "storage_backup_failed": RuleText(
        "Sauvegarde de la base impossible",
        "Sans sauvegarde, une panne de disque ferait perdre l'historique des mesures.",
        ("vérifier l'espace disque et les droits du dossier des sauvegardes",),
        ("./voltaneum diagnose", "./voltaneum restore")),
    "storage_daily_backup_failed": RuleText(
        "Sauvegarde quotidienne impossible",
        "Sans sauvegarde, une panne de disque ferait perdre l'historique des mesures.",
        ("vérifier l'espace disque et les droits du dossier des sauvegardes",),
        ("./voltaneum diagnose", "./voltaneum restore")),
    "storage_auto_purge": RuleText(
        "Purge automatique des mesures les plus anciennes",
        "La base a atteint sa limite : les mesures les plus anciennes ont été supprimées.",
        ("si cet historique est utile, augmenter STORAGE_LIMIT_BYTES ou exporter avant purge "
         "(./voltaneum export --all)",),
        ("./voltaneum diagnose",)),
    "storage_purge_blocked_by_floor": RuleText(
        "Purge bloquée par la durée de conservation minimale",
        "La base est pleine mais les mesures ne peuvent pas être purgées (durée de conservation "
        "minimale) : le disque risque de se remplir.",
        ("libérer de la place sur le disque, ou réduire RETENTION_DAYS",),
        ("./voltaneum diagnose",)),
    "storage_purge_ineffective": RuleText(
        "Purge inefficace",
        "La purge n'a pas libéré assez de place.",
        ("vérifier l'espace disque",),
        ("./voltaneum diagnose",)),
}


def rule_text(rule_name: str) -> RuleText:
    """Texte de la règle, ou un texte générique lisible pour une règle non décrite."""
    return RULES.get(rule_name, RuleText(
        rule_name.replace("_", " ").capitalize(), "Alerte de Voltaneum.",
        ("consulter le message ci-dessus et l'interface (onglet Alertes)",)))


def _num(value: float | int | str | None, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}".replace(".", ",") + (f" {unit}" if unit else "")


def _measures(snapshot: dict | None) -> list[str]:
    """Mesures au moment de l'alerte, en clair (dernière ligne en base)."""
    if not snapshot or snapshot.get("empty"):
        return ["  aucune mesure en base"]
    v, th = snapshot["values"], snapshot["thresholds"]
    labels = config.SENSOR_LABELS
    age = float(snapshot.get("age_seconds") or 0)
    ago = f"{age:.0f} s" if age < 120 else f"{age / 60:.0f} min" if age < 7200 else f"{age / 3600:.1f} h".replace(".", ",")
    lines = [f"  (dernière mesure il y a {ago})"]
    target = config.TARGET_COLUMN
    estimate = snapshot.get("target_estimate")
    trend = snapshot.get("target_trend_10min")
    lines.append(f"  {labels[target][0]} : {_num(v.get(target), 1, '°C')} (cible {_num(th['target_temp'], 1, '°C')}, "
                 f"seuil critique {_num(th['critical_temp'], 0, '°C')})"
                 + (f", estimée {_num(estimate, 2, '°C')}" if estimate is not None else "")
                 + (f", tendance {trend:+.1f} °C / 10 min".replace(".", ",") if trend is not None else ""))
    for column in ("pt01", "pt03", "pt04"):
        if column != target:
            lines.append(f"  {labels[column][0]} : {_num(v.get(column), 1, '°C')}")
    lines.append(f"  Débit HT01 : {_num(v.get('ht01'), 2, 'm³/h')} · pompe : {_num(v.get('freq_pompe'), 0, 'Hz')}"
                 f" · air extérieur : {_num(v.get('temp_outdoor'), 1, '°C')}")
    power = snapshot.get("thermal_power_kw_10min")
    if power is not None:
        lines.append(f"  Puissance dissipée (estimée, moyenne 10 min) : {_num(power, 1, 'kW')}")
    control = snapshot.get("control") or {}
    if control.get("phase"):
        lines.append(f"  Régulation par étapes : {control['phase']}"
                     + (f" — {control['cooling_mode_reason']}" if control.get("cooling_mode_reason") else ""))
    return lines


def compose(level: str, rule_name: str, message: str, snapshot: dict | None,
            now: datetime | None = None, reminder_minutes: float | None = None) -> tuple[str, str]:
    """(sujet, corps) de l'e-mail d'une alerte."""
    text = rule_text(rule_name)
    now = now or datetime.now().astimezone()
    host = socket.gethostname()
    subject = f"[Voltaneum] {level} — {text.title}"
    body = [
        f"{level} — {text.title}",
        f"Le {now.strftime('%d/%m/%Y à %H:%M')} (heure du serveur {host})",
        "",
        "CE QUI SE PASSE",
        *(f"  {line}" for line in message.splitlines()),
        "",
        "POURQUOI C'EST IMPORTANT",
        *textwrap.wrap(text.why, 100, initial_indent="  ", subsequent_indent="  "),
        "",
        "QUE FAIRE",
        *(line for i, action in enumerate(text.actions, 1)
          for line in textwrap.wrap(f"{i}. {action}", 100, initial_indent="  ", subsequent_indent="     ")),
        "",
        "MESURES AU MOMENT DE L'ALERTE",
        *_measures(snapshot),
        "",
        "POUR VÉRIFIER SUR LE SERVEUR",
        *(f"  {command}" for command in text.commands),
        f"  ./voltaneum data --around \"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\"   (mesures autour de "
        "cet instant, heure UTC)",
    ]
    if reminder_minutes:
        hours = reminder_minutes / 60
        delay = f"{hours:g} h" if hours >= 1 else f"{reminder_minutes:g} min"
        body += ["", "RAPPEL", f"  Si la situation dure, cette alerte sera renvoyée au plus tôt dans {delay}."]
    body += ["", "—", "Voltaneum surveille l'installation en lecture seule : il ne commande jamais l'automate.",
             f"Référence de la règle : {rule_name}."]
    return subject, "\n".join(body)
