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
    "pt01": "Grandeur RÉGULÉE : comparée à la cible, tendance sur 10 min, régulation par étapes, "
            "conditions publiées en MQTT, seuil critique, cible et entrée des modèles de prédiction.",
    "pt02": "Alerte « diélectrique critique », puissance dissipée (PT02 − PT01), contrôle de l'alerte "
            "« PT01 figée », entrée des modèles, affichage.",
    "pt03": "Affichage (synoptique, historique), entrée des modèles, efficacité (alerte désactivée par défaut).",
    "pt04": "Affichage (synoptique, historique), entrée des modèles, efficacité (alerte désactivée par défaut).",
    "ht01": "Puissance dissipée (débit), alerte « pompe sans débit », animation du synoptique, entrée des modèles.",
    "freq_pompe": "Alerte « pompe sans débit », affichage, entrée des modèles (écartée tant qu'elle reste constante).",
}

# Description des points connus, par KeyName : (description, confirmée ?).
POINT_DESCRIPTIONS: dict[str, tuple[str, bool]] = {
    "PT01": ("Température du liquide diélectrique ENTRANT dans l'armoire (°C)", True),
    "PT02": ("Température du liquide diélectrique SORTANT de l'armoire (°C)", True),
    "PT03": ("Température de l'air SORTANT, chaud (°C)", True),
    "PT04": ("Température de l'air ENTRANT, froid (°C)", True),
    "HT01": ("Débit du circuit diélectrique (m³/h)", True),
    "PUMP": ("Fréquence de la pompe (Hz)", True),
    "Pump frequency": ("Fréquence de la pompe (Hz)", True),
    "Frequence": ("Fréquence de la pompe (Hz)", True),
    "ON/OFF": ("Marche / arrêt de l'installation (commande)", False),
    "Temperature setpoint": ("Consigne de température de l'automate (°C)", False),
    "Emergency stop bypass": ("Shunt de l'arrêt d'urgence (sécurité)", False),
    "AUTO REQUEST M4.2": ("Demande de passage en mode automatique (bit M4.2)", False),
    "Manual-Auto": ("Sélection mode manuel / automatique", False),
    "DIAG Actual frequency VW12": ("Fréquence réellement mesurée du variateur (diagnostic, mot VW12)", False),
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
            description += " (déduit du nom)" if "confirmer" not in description else ""
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
