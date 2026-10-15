"""Garde-fous de sécurité transverses : modèles, secrets, configuration, injections SQL."""

import os
import re
import stat
from pathlib import Path

import pytest

import config
from ml.train import train_and_compare
from tests.test_train import make_synthetic_dataframe

ROOT = Path(__file__).resolve().parent.parent


def test_saved_models_are_private_and_complete(tmp_path):
    """Un modèle est un pickle : modifiable par un autre utilisateur = exécution de code à son chargement."""
    model_dir = tmp_path / "models"
    assert train_and_compare(make_synthetic_dataframe(), 600, str(model_dir))["status"] == "ok"
    for name in ("random_forest.joblib", "gradient_boosting.joblib", "training_report.json"):
        assert stat.S_IMODE((model_dir / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(model_dir.stat().st_mode) == 0o700
    assert not list(model_dir.glob("*.tmp"))


def test_no_credential_is_hardcoded_in_the_source():
    pattern = re.compile(r"(password|passwd|secret|token|api_key)\s*=\s*[\'\"][^\'\"\s]{4,}[\'\"]", re.I)
    offenders = [f"{p.relative_to(ROOT)}:{i}" for p in ROOT.rglob("*.py")
                 if "tests" not in p.relative_to(ROOT).parts and not any(
                     part.startswith(".") or part in ("venv", "site-packages") for part in p.relative_to(ROOT).parts)
                 for i, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert offenders == []


def test_env_example_documents_every_setting():
    source = (ROOT / "config.py").read_text()
    names = set(re.findall(r"_env_\w+\(\s*\"([A-Z0-9_]+)\"", source))
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert names - documented == set(), "variables absentes de .env.example"
    assert documented - names == set(), "variables de .env.example jamais lues par config.py"


def test_env_file_is_never_versioned():
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in gitignore and "*.db" in gitignore


@pytest.mark.parametrize("name,value,fragment", [
    ("CRITICAL_TEMP", 30.0, "CRITICAL_TEMP"),
    ("SNAPSHOT_INTERVAL_SECONDS", 0, "SNAPSHOT_INTERVAL_SECONDS"),
    ("MQTT_TLS_INSECURE", True, "MQTT_TLS_INSECURE"),
    ("OUTDOOR_SENSOR_MODE", "ftp", "OUTDOOR_SENSOR_MODE"),
    ("LOG_LEVEL", "BAVARD", "LOG_LEVEL"),
    ("SMTP_HOST", "smtp.example.org", "e-mail"),   # SMTP à moitié configuré
])
def test_inconsistent_configuration_is_refused(monkeypatch, name, value, fragment):
    monkeypatch.setattr(config, "VOLTANEUM_USER", "u")
    monkeypatch.setattr(config, "VOLTANEUM_PASSWORD", "p")
    monkeypatch.setattr(config, name, value)
    with pytest.raises(ValueError, match=fragment):
        config.validate_config()


def test_invalid_number_in_environment_names_the_variable(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "beaucoup")
    with pytest.raises(ValueError, match="SNAPSHOT_INTERVAL_SECONDS"):
        config._env_int("SNAPSHOT_INTERVAL_SECONDS", 15)


def test_relative_paths_are_resolved_from_the_project_root(monkeypatch):
    monkeypatch.setenv("MODEL_DIR", "modeles_x")
    assert config._env_path("MODEL_DIR", "models") == str(config.PROJECT_ROOT / "modeles_x")
    assert os.path.isabs(config.SQLITE_DB_PATH)
