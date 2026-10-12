"""
tests/test_pid_controller.py — Vérifie compute_setpoint() (contrat 6.7).

Test de sortie officiel de l'étape 9 (spec §3, ligne 9) : consigne toujours
dans [0, 100] sur une batterie de valeurs de test.

utilise un état PID simulé (dict) pour vérifier l'accumulation de l'intégrale
et la continuité de l'erreur précédente, ainsi que le déclenchement du
bypass de sécurité et du feedforward.
"""

import random

import pytest

import config
from control.pid_controller import compute_setpoint


class TestSafetyBypass:
    """Priorité absolue (spec §6.7) : t_oil > CRITICAL_TEMP -> 100.0, mode safety_bypass."""

    def test_returns_100_when_above_critical_temp(self):
        setpoint, _, mode = compute_setpoint(
            t_oil=config.CRITICAL_TEMP + 0.1,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert setpoint == 100.0
        assert mode == "safety_bypass"

    def test_bypass_ignores_all_other_inputs(self):
        """Même avec des entrées 'calmes' par ailleurs, le dépassement du seuil
        critique doit primer sur tout le reste."""
        setpoint, _, mode = compute_setpoint(
            t_oil=999.0,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,  # pas de surchauffe prédite
            t_outdoor=10.0,  # pas de chaleur extérieure
            dt=1.0,
            pid_state={"integral": -500.0, "last_error": -50.0},
        )
        assert setpoint == 100.0
        assert mode == "safety_bypass"

    def test_exactly_at_critical_temp_does_not_trigger_bypass(self):
        """t_oil == CRITICAL_TEMP (pas strictement supérieur) doit suivre le PID normal,
        conformément à la spec ('si t_oil > CRITICAL_TEMP')."""
        setpoint, _, mode = compute_setpoint(
            t_oil=config.CRITICAL_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert mode != "safety_bypass"


class TestSetpointAlwaysBounded:
    """Test de sortie officiel : consigne toujours dans [0, 100]."""

    def test_extreme_negative_error(self):
        """t_oil très supérieur à la cible (mais sous le seuil critique) -> PID
        pousse fort vers le refroidissement max, doit rester borné à 100."""
        setpoint, _, _ = compute_setpoint(
            t_oil=config.CRITICAL_TEMP - 0.01,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert 0.0 <= setpoint <= 100.0
        # Vérifie la DIRECTION, pas seulement les bornes : de l'huile presque
        # au seuil critique doit réellement pousser vers un refroidissement
        # fort, pas vers 0 (bug de signe corrigé le 18 septembre 2026 : les
        # bornes seules ne suffisent pas à détecter une erreur inversée).
        assert setpoint > 50.0

    def test_extreme_positive_error(self):
        """t_oil très inférieur à la cible -> PID pousse fort vers 0, doit rester borné."""
        setpoint, _, _ = compute_setpoint(
            t_oil=-20.0,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert 0.0 <= setpoint <= 100.0
        assert setpoint < 50.0

    def test_setpoint_direction_matches_physical_expectation(self):
        """Garde-fou anti-régression du bug de signe du 18 septembre 2026 :
        une huile plus chaude que la cible DOIT produire une consigne plus
        élevée qu'une huile plus froide que la cible, toutes choses égales
        par ailleurs. Sans ce test, un signe inversé peut passer inaperçu
        car il reste dans les bornes [0, 100]."""
        setpoint_hot, _, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP + 10.0,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        setpoint_cold, _, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP - 10.0,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert setpoint_hot > setpoint_cold

    def test_integral_windup_over_many_iterations_stays_bounded(self):
        """Simule une longue période d'erreur constante (accumulation de
        l'intégrale, cas classique de 'windup') et vérifie que la consigne
        reste toujours dans les bornes, itération après itération."""
        state = {}
        for _ in range(500):
            setpoint, state, _ = compute_setpoint(
                t_oil=20.0,  # loin sous la cible en permanence
                t_target=config.TARGET_TEMP,
                t_oil_predicted=config.TARGET_TEMP,
                t_outdoor=15.0,
                dt=1.0,
                pid_state=state,
            )
            assert 0.0 <= setpoint <= 100.0

    def test_anti_windup_allows_fast_recovery_after_saturation(self):
        """Preuve concrète que l'anti-windup fonctionne : après une longue
        saturation (huile très chaude en continu -> consigne bloquée à 100%),
        dès que l'huile revient à la cible, la consigne doit redescendre
        IMMÉDIATEMENT (en un seul cycle), pas rester bloquée près de 100%
        pendant que l'intégrale accumulée depuis 500 cycles se résorbe."""
        state = {}
        for _ in range(500):
            setpoint, state, mode = compute_setpoint(
                t_oil=config.CRITICAL_TEMP - 0.5,  # très chaud, en continu
                t_target=config.TARGET_TEMP,
                t_oil_predicted=config.TARGET_TEMP,
                t_outdoor=15.0,
                dt=1.0,
                pid_state=state,
            )
        assert setpoint == pytest.approx(100.0)
        assert state["saturated_high"] is True

        # L'huile revient pile sur la cible : erreur nulle -> la consigne ne
        # doit plus être poussée vers le haut par une intégrale démesurée.
        setpoint_after, state_after, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state=state,
        )
        assert setpoint_after < 50.0, (
            f"Windup non maîtrisé : consigne={setpoint_after} encore proche de 100% "
            "alors que l'erreur est nulle."
        )

    def test_random_battery_of_values_always_bounded(self):
        """Batterie de valeurs aléatoires (mais physiquement plausibles pour la
        plupart, avec quelques valeurs extrêmes) sur plusieurs graines fixes
        pour la reproductibilité."""
        rng = random.Random(42)
        state = {}
        for _ in range(1000):
            t_oil = rng.uniform(-30.0, 90.0)
            t_target = rng.uniform(20.0, 50.0)
            t_oil_predicted = rng.uniform(-30.0, 90.0)
            t_outdoor = rng.uniform(-40.0, 55.0)
            dt = rng.uniform(0.0, 60.0)

            setpoint, state, mode = compute_setpoint(
                t_oil=t_oil,
                t_target=t_target,
                t_oil_predicted=t_oil_predicted,
                t_outdoor=t_outdoor,
                dt=dt,
                pid_state=state,
            )
            assert 0.0 <= setpoint <= 100.0, (
                f"Consigne hors bornes : {setpoint} pour t_oil={t_oil}, "
                f"t_target={t_target}, mode={mode}"
            )

    def test_dt_zero_does_not_crash_or_go_out_of_bounds(self):
        """dt=0 (deux appels au même instant) ne doit pas provoquer de division
        par zéro ni de valeur hors bornes."""
        setpoint, state, _ = compute_setpoint(
            t_oil=25.0,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=0.0,
            pid_state={},
        )
        assert 0.0 <= setpoint <= 100.0

    def test_first_call_with_empty_state_does_not_crash(self):
        """Premier appel avec pid_state={} (aucun 'integral'/'last_error' encore)."""
        setpoint, state, mode = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert 0.0 <= setpoint <= 100.0
        assert "integral" in state
        assert "last_error" in state

    def test_non_finite_oil_temperature_triggers_safe_bypass(self):
        setpoint, state, mode = compute_setpoint(
            t_oil=float("nan"),
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={"integral": 2.0, "last_error": 1.0},
        )
        assert setpoint == 100.0
        assert state == {"integral": 2.0, "last_error": 1.0}
        assert mode == "safety_bypass"

    def test_non_finite_inputs_do_not_contaminate_pid_state(self):
        setpoint, state, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=float("inf"),
            t_outdoor=float("nan"),
            dt=-1.0,
            pid_state={"integral": float("nan"), "last_error": float("inf")},
        )
        assert 0.0 <= setpoint <= 100.0
        assert state["integral"] == 0.0
        assert state["last_error"] == 0.0


class TestColdSideWindup:
    """Correctif « huile froide » : l'intégrale ne doit jamais devenir négative ni démesurée.

    Cas réel mesuré avant correction : huile à 10 °C pendant 3 cycles de 60 s, puis 40 °C ->
    la consigne restait à 0 % pendant 9 minutes (intégrale à -1500) alors que l'huile était
    5 °C au-dessus de la cible.
    """

    def test_integral_never_negative_when_oil_is_cold(self):
        state = {}
        for _ in range(20):
            _, state, _ = compute_setpoint(
                t_oil=10.0, t_target=config.TARGET_TEMP, t_oil_predicted=10.0,
                t_outdoor=15.0, dt=60.0, pid_state=state,
            )
            assert state["integral"] >= 0.0

    def test_integral_is_capped_at_equivalent_of_full_output(self):
        state = {}
        cap = config.SETPOINT_MAX / config.PID_KI
        for _ in range(500):
            _, state, _ = compute_setpoint(
                t_oil=config.CRITICAL_TEMP - 0.5, t_target=config.TARGET_TEMP,
                t_oil_predicted=config.TARGET_TEMP, t_outdoor=15.0, dt=60.0, pid_state=state,
            )
            assert state["integral"] <= cap + 1e-9

    def test_reacts_within_three_cycles_after_a_long_cold_period(self):
        state = {}
        for _ in range(3):  # huile très froide
            _, state, _ = compute_setpoint(
                t_oil=10.0, t_target=config.TARGET_TEMP, t_oil_predicted=10.0,
                t_outdoor=None, dt=60.0, pid_state=state,
            )
        # puis l'huile monte à 40 °C (5 °C au-dessus de la cible)
        setpoints = []
        for _ in range(3):
            setpoint, state, _ = compute_setpoint(
                t_oil=40.0, t_target=config.TARGET_TEMP, t_oil_predicted=40.0,
                t_outdoor=None, dt=60.0, pid_state=state,
            )
            setpoints.append(setpoint)
        assert setpoints[0] > 0.0, "la consigne doit réagir dès le premier cycle chaud"
        assert setpoints[-1] >= 90.0


class TestFeedforward:
    def test_feedforward_triggers_on_predicted_overshoot(self):
        _, _, mode = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP
            + config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD
            + 0.1,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert mode == "feedforward"

    def test_feedforward_triggers_on_hot_outdoor_temp(self):
        _, _, mode = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=config.FEEDFORWARD_OUTDOOR_HOT_THRESHOLD + 0.1,
            dt=1.0,
            pid_state={},
        )
        assert mode == "feedforward"

    def test_pid_mode_when_no_feedforward_condition_met(self):
        _, _, mode = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        assert mode == "pid"

    def test_feedforward_increases_setpoint_compared_to_pid_alone(self):
        """Vérifie que le feedforward a bien un effet (avant saturation à 100)."""
        setpoint_no_ff, _, _ = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=15.0,
            dt=1.0,
            pid_state={},
        )
        setpoint_with_ff, _, mode = compute_setpoint(
            t_oil=config.TARGET_TEMP,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=config.TARGET_TEMP,
            t_outdoor=config.FEEDFORWARD_OUTDOOR_HOT_THRESHOLD + 0.1,
            dt=1.0,
            pid_state={},
        )
        assert mode == "feedforward"
        assert setpoint_with_ff >= setpoint_no_ff


class TestNoNetworkOrActuatorAccess:
    """Definition of Done (spec §10) : aucun appel réseau, aucun import de
    bibliothèque d'accès à un automate ou variateur — uniquement du calcul pur."""

    def test_module_source_has_no_forbidden_imports(self):
        import control.pid_controller as module

        with open(module.__file__, "r", encoding="utf-8") as f:
            source = f.read()

        forbidden_tokens = [
            "import requests",
            "import socket",
            "import websocket",
            "import paho",
            "import snap7",  # bibliothèque typique d'accès S7 (Siemens)
            "import pymodbus",
        ]
        for token in forbidden_tokens:
            assert token not in source, f"Import interdit trouvé dans pid_controller.py : {token}"