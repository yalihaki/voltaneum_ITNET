"""
    Tests unitaires pour l'entraînement multi-horizon des modèles prédictifs.
    Vérifie que tous les horizons demandés sont entraînés et que le rapport global est correct.
    Utilise un DataFrame synthétique pour simuler les données de télémétrie.
    Les tests sont isolés et reproductibles, et ne laissent aucune trace sur le disque après l'exécution.
    
    Usage : pytest tests/test_multi_horizon.py
"""

import tempfile

from ml.multi_horizon import train_all_horizons
from tests.test_train import make_synthetic_dataframe


def test_multi_horizon_grid_trains_each_requested_horizon():
    report = train_all_horizons(
        make_synthetic_dataframe(n=1200),
        model_dir=tempfile.mkdtemp(prefix="voltaneum_grid_"),
        horizons=[30, 60],
    )
    assert report["status"] == "ok"
    assert set(report["reports"]) == {"30", "60"}
    assert all(item["status"] == "ok" for item in report["reports"].values())