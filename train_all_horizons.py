"""
    Le script train_all_horizons.py est un point d'entrée pour entraîner 
    les modèles pour tous les horizons de prédiction. Il importe la fonction 
    train_all_horizons_from_db depuis le module ml.multi_horizon et l'appelle 
    lorsque le script est exécuté directement.

    Le script est conçu pour être exécuté en tant que programme autonome, 
    et il peut être intégré dans des pipelines d'entraînement automatisés ou
    utilisé pour des expériences de formation manuelles. Il prend en charge
    la spécification d'une base de données SQLite et d'un répertoire de modèles
    en tant qu'arguments de ligne de commande, avec des valeurs par défaut si
    aucun argument n'est fourni.
"""

from __future__ import annotations

import argparse
import json

from ml.multi_horizon import train_all_horizons_from_db


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("db", nargs="?", default="voltaneum.db")
parser.add_argument("model_dir", nargs="?", default="models")
args = parser.parse_args()

report = train_all_horizons_from_db(db_path=args.db, model_dir=args.model_dir)
print(json.dumps(report, indent=2, default=str))
