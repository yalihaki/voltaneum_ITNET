"""collectors/catalogue.py — Description des points du catalogue de l'automate (calcul pur).

En clair : la passerelle publie un « catalogue » de points (un par variable de l'automate :
sondes, pompe, consignes, boutons...). Chaque point a un code interne (KeyNode, ex. « K0 »),
un nom (KeyName, ex. « PT01 »), un nœud d'appareil (DevNode) et un indicateur d'écriture
(WrEnable). Ce module dit, pour chaque nom connu, CE QUE C'EST et CE QUE VOLTANEUM EN FAIT, pour
l'onglet « Catalogue » de l'interface et `./voltaneum catalogue`.

Les descriptions des points NON utilisés sont déduites de leur nom (non confirmées par
l'automaticien) : elles sont marquées comme telles.
"""

from __future__ import annotations

import config

# Ce que Voltaneum fait de chaque colonne alimentée par l'automate.
COLUMN_USAGE: dict[str, str] = {
    "pt02": "Grandeur SURVEILLÉE : comparée à la cible, tendance sur 10 min, régulation par étapes, "
            "conditions publiées en MQTT, seuil critique, cible et entrée des modèles de prédiction.",
    "pt01": "Échauffement de l'armoire (PT02 − PT01) et consigne PT01 conseillée (cascade), puissance "
            "dissipée, alerte « diélectrique critique », contrôle de l'alerte « PT02 figée », entrée des modèles.",
    "pt03": "Efficacité de la CDU (alerte désactivée par défaut), entrée des modèles, affichage.",
    "pt04": "Entrée des modèles (sa tendance révèle les marches / arrêts du ventilateur), efficacité de la CDU, affichage.",
    "ht01": "Puissance dissipée (débit), alerte « pompe sans débit », animation du synoptique, entrée des modèles.",
    "freq_pompe": "Alerte « pompe sans débit », affichage, entrée des modèles (écartée tant qu'elle reste constante).",
}

# Description des points connus, par KeyName : (description, confirmée ?). Les descriptions non
# confirmées sont déduites du nom du point et à vérifier avec l'automaticien.
POINT_DESCRIPTIONS: dict[str, tuple[str, bool]] = {
    "PT01": ("Diélectrique refroidi par la CDU, renvoyé dans l'armoire (°C), régulé par l'automate", True),
    "PT02": ("Diélectrique sortant de l'armoire, vers la CDU (°C) : état de l'armoire", True),
    "PT03": ("Liquide de refroidissement, sortie CDU vers le dry cooler (°C)", True),
    "PT04": ("Liquide de refroidissement, retour du dry cooler vers la CDU (°C)", True),
    "HT01": ("Débit du diélectrique, de l'armoire vers la CDU (m³/h)", True),
    "PUMP": ("Fréquence de la pompe (Hz)", True),
    "Pump frequency": ("Fréquence de la pompe (Hz)", True),
    "Frequence": ("Fréquence de la pompe (Hz)", True),
    "ON/OFF": ("Marche / arrêt de l'installation", False),
    "Temperature setpoint": ("Consigne de température de l'automate (°C), probablement celle de PT01", False),
    "DIAG Oil temperature setting VD500": ("Consigne de température d'huile utilisée par l'automate (°C)", False),
    "Emergency stop bypass": ("Shunt de l'arrêt d'urgence (sécurité)", False),
    "Phase sequence bypass": ("Shunt du contrôle d'ordre des phases (sécurité électrique)", False),
    "AUTO REQUEST M4.2": ("Demande de passage en mode automatique (bit M4.2)", False),
    "DIAG Auto mode M4.2": ("État du mode automatique (bit M4.2)", False),
    "DIAG Auto intermediate VD430": ("Valeur intermédiaire du calcul automatique (mot VD430)", False),
    "MANUAL PUMP REQUEST M4.3": ("Demande de marche manuelle de la pompe (bit M4.3)", False),
    "DIAG Manual frequency VD400": ("Fréquence demandée en mode manuel (mot VD400)", False),
    "Manual-Auto": ("Sélection mode manuel / automatique", False),
    "DIAG Actual frequency VW12": ("Fréquence réelle du variateur (mot VW12)", False),
    "DIAG VFD register VW10": ("Registre du variateur (mot VW10)", False),
    "DIAG Motor register VW130": ("Registre moteur (mot VW130)", False),
    "Maximum frequency": ("Fréquence maximale autorisée du variateur (Hz)", False),
    "Minimum frequency": ("Fréquence minimale autorisée du variateur (Hz)", False),
    "Minimum primary flow": ("Seuil de débit minimal du circuit primaire", False),
    "Pump downtime": ("Durée d'arrêt de la pompe", False),
    "Alarm delay": ("Temporisation avant déclenchement des alarmes", False),
    "Clear alarm timers": ("Remise à zéro des temporisations d'alarme", False),
    "Dielectric inlet high": ("Seuil ou alarme : diélectrique trop chaud à l'entrée de l'armoire", False),
    "Dielectric fluid high": ("Seuil ou alarme : diélectrique trop chaud", False),
    "Water/glycol inlet high": ("Seuil ou alarme : eau glycolée trop chaude à l'entrée de la CDU", False),
    "Water/glycol high": ("Seuil ou alarme : eau glycolée trop chaude", False),
}
for _short in ("PT01", "PT02", "PT03", "PT04"):
    POINT_DESCRIPTIONS[f"{_short}-temperature"] = POINT_DESCRIPTIONS[_short]


def describe(key_name: str, column: str | None, writable: bool, gateway_desc: str | None = None,
             on_sensor_node: bool = True) -> dict:
    """Description, usage et statut d'un point du catalogue."""
    known = POINT_DESCRIPTIONS.get(key_name)
    if known:
        description, confirmed = known
        if not confirmed:
            description += " (d'après le nom)"
    elif gateway_desc:
        description, confirmed = gateway_desc, False
    else:
        description, confirmed = "Point non documenté", False
    if column and on_sensor_node:
        label = config.SENSOR_LABELS.get(column, (column,))[0]
        usage = f"→ colonne « {column} » ({label}). " + COLUMN_USAGE.get(column, "Enregistré et affiché.")
        used = True
    else:
        usage, used = "Non utilisé", False
    return {"description": description, "confirmed": confirmed, "usage": usage, "used": used}


def reference_points() -> list[dict]:
    """Points connus du projet quand aucun catalogue n'a encore été relevé sur cette base."""
    from collectors.gateway_collector import KEYNAME_TO_COLUMN

    names = list(dict.fromkeys(["PT01", "PT02", "PT03", "PT04", "HT01", "PUMP", *KEYNAME_TO_COLUMN,
                                "DIAG Actual frequency VW12", "ON/OFF", "Temperature setpoint",
                                "Emergency stop bypass", "AUTO REQUEST M4.2", "Manual-Auto"]))
    writable = {"ON/OFF", "Temperature setpoint", "Emergency stop bypass", "AUTO REQUEST M4.2", "Manual-Auto"}
    return [{"key_name": n, "column": KEYNAME_TO_COLUMN.get(n), "writable": n in writable} for n in names]
