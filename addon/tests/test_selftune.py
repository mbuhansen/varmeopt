"""Simulatoren, prøven og nattens justering - på syntetiske logbogsdage.

Dagene er lavet af et kendt hus: UVR'en starter pumpen ved 6 kWh og stopper
ved 14, pumpen leverer 8 kW, huset trækker 3 kW. Så ved vi hvad simulatoren
skal ramme.
"""

import time
import unittest
from datetime import datetime

from varmeopt.prices import Plan, Slot
from varmeopt.replay import (
    Fixed,
    Knobs,
    PlannerPolicy,
    draw_kwh,
    learn_model,
    simulate,
    usable_days,
    validate,
)
from varmeopt.selftune import BOUNDS, Tuner

START, STOP, HP_KW, DRAW_KW = 6.0, 14.0, 8.0, 3.0
FIXED = Fixed(
    wear_kr_per_kwh=0.15,
    min_runtime_minutes=15,
    horizon_minutes=1440,
    dhw_temp=55,
    store_full_by_hour=-1,
)
KNOBS = Knobs(source_hysteresis=0.05, charge_min_saving_kr=1.0, low_charge_min_saving_kr=1.0)


def price_at(t):
    hour = datetime.fromtimestamp(t).hour
    if hour < 6:
        return 2.0
    if hour < 9:
        return 3.0
    if 17 <= hour < 21:
        return 3.2
    return 2.2


def house(days, first=None, uvr_start=START):
    """Logbogslinjer fra et hus med en UVR-termostat, minut for minut."""
    t0 = first or int(datetime(2026, 12, 1).timestamp())
    store, on, rows = 10.0, False, []
    for i in range(days * 48):
        t = t0 + i * 1800
        prices = [price_at(t + k * 1800) for k in range(48)]
        plan = Plan(tuple(Slot(k, "holdchrg", p, 0.5, 50.0) for k, p in enumerate(prices)))
        begin, heat, minutes = store, 0.0, 0
        for _ in range(30):
            if store <= uvr_start:
                on = True
            elif store >= STOP:
                on = False
            if on:
                heat += HP_KW / 60
                minutes += 1
            store += (HP_KW / 60 if on else 0.0) - DRAW_KW / 60
        rows.append({
            "t": t,
            "dækning": 1.0,
            "så": {
                "plan": plan.to_replay(),
                "frem": {
                    "cop": [4.0] * 48, "lade_cop": [3.4] * 48,
                    "behov_kw": [DRAW_KW] * 48, "vvb_kwh": [0.0] * 48,
                },
                "lager_kwh": begin,
                "lager_plads_kwh": max(0.0, 30 - begin),
                "lager_plads_lav_kwh": max(0.0, 18 - begin),
                "lager_lagret_kwh": begin,
                "lager_varmt_kwh": 0.0,
                "lager_top_plads_kwh": max(0.0, 60 - begin),
                "lav_temp": 46.0,
                "pille_varmepris": 0.95,
                "låst_min": 1440,
                "behov_kw": DRAW_KW,
                "vagt_kilde": "varmepumpe",
            },
            "skete": {
                "vp_varme_kwh": heat, "solvarme_kwh": 0.0, "elpatron_kwh": 0.0,
                "pillefyr_kwh": 0.0, "behov_kwh": DRAW_KW / 2,
                "lager_slut_kwh": store, "kompressor_min": float(minutes),
                "lad_op_min": 0.0, "lad_op_lav_min": 0.0,
                "kilde_min": {"varmepumpe": 30.0},
            },
        })
    return rows


class ReplayTest(unittest.TestCase):
    def test_the_draw_is_what_must_have_left_the_store(self):
        row = house(1)[0]
        self.assertAlmostEqual(draw_kwh(row), DRAW_KW / 2, places=6)

    def test_the_uvr_is_learned_from_the_log(self):
        model = learn_model(usable_days(house(3)))
        self.assertAlmostEqual(model.hp_kw, HP_KW, delta=0.5)
        self.assertAlmostEqual(model.start_kwh, START, delta=1.0)
        self.assertAlmostEqual(model.stop_kwh, STOP, delta=1.5)

    def test_the_replay_plan_gives_the_same_price(self):
        plan = Plan(tuple(Slot(k, "holdchrg", 2.0 + k / 10, 0.5, 50.0) for k in range(5)))
        again = Plan.from_replay(plan.to_replay())
        for m in (0, 30, 120):
            self.assertAlmostEqual(again.marginal(m).kr_per_kwh, plan.marginal(m).kr_per_kwh)


class ValidationTest(unittest.TestCase):
    def test_a_simulator_that_matches_the_house_passes(self):
        days = usable_days(house(8))
        check = validate(days, learn_model(days))
        self.assertTrue(check.ok, check.why)

    def test_too_few_days_is_no(self):
        days = usable_days(house(3))
        check = validate(days, learn_model(days))
        self.assertFalse(check.ok)
        self.assertIn("hele dage", check.why)

    def test_a_simulator_that_misses_fails(self):
        # Huset starter pumpen ved 6 kWh, men modellen tror 0: lageret
        # løber langt lavere i simulatoren end det gjorde.
        days = usable_days(house(8))
        model = learn_model(days)
        model.start_kwh, model.stop_kwh = -10.0, -2.0
        self.assertFalse(validate(days, model).ok)


class PlannerReplayTest(unittest.TestCase):
    def test_the_planner_lays_a_low_charge_on_the_cheap_night(self):
        # Natten 2,00, morgenen 3,00: ved kurvens COP 4 er det 0,65 mod 0,90
        # kr/kWh varme. Med et lavt gulv skal genafspilningen lade lavt op.
        days = usable_days(house(2))
        model = learn_model(days)
        knobs = Knobs(0.05, 1.0, 0.3)
        policy = PlannerPolicy(knobs, FIXED, model)
        result = simulate(days[1], model, planner_policy=policy)
        base = simulate(days[1], model, planner_policy=PlannerPolicy(Knobs(0.05, 1.0, 2.0), FIXED, model))
        self.assertGreater(result.hp_heat_kwh, 0)
        self.assertLess(result.cost_kr, base.cost_kr, "den lave opladning skal kunne betale sig")


class TunerTest(unittest.TestCase):
    def test_no_tuning_without_a_passed_validation(self):
        tuner = Tuner()
        self.assertFalse(tuner.nightly(house(3), "n1", KNOBS, FIXED))
        self.assertIn("justerer ikke", tuner.note)
        self.assertEqual(tuner.knobs(KNOBS), KNOBS)

    def test_at_most_one_knob_one_step_and_within_bounds(self):
        tuner = Tuner()
        started = time.monotonic()
        tuner.nightly(house(10), "n1", Knobs(0.05, 1.0, 2.0), FIXED)
        took = time.monotonic() - started
        self.assertTrue(tuner.validation["ok"], tuner.validation)
        moved = [k for k, v in tuner.values.items() if v is not None]
        self.assertLessEqual(len(moved), 1)
        for name in moved:
            step, low, high = BOUNDS[name]
            self.assertTrue(low <= tuner.values[name] <= high)
        self.assertLess(took, 120, f"nattens justering tog {took:.0f} s")

    def test_once_a_night_and_round_trip(self):
        tuner = Tuner()
        tuner.nightly(house(3), "n1", KNOBS, FIXED)
        self.assertFalse(tuner.nightly(house(3), "n1", KNOBS, FIXED))
        tuner.values["source_hysteresis"] = 0.5
        back = Tuner.from_raw(tuner.to_raw())
        self.assertEqual(back.values["source_hysteresis"], 0.10, "holdes inden for grænsen")
        self.assertEqual(back.last_night, "n1")


class WiredIntoTheCycleTest(unittest.TestCase):
    """Justeringerne skal nå planlæggeren - og kunne slås fra."""

    def app(self, **over):
        import tempfile
        from pathlib import Path

        from tests.test_cycle import options
        from varmeopt.__main__ import Varmeopt
        from varmeopt.store import Store

        return Varmeopt(options(**over), Store(Path(tempfile.mkdtemp(prefix="varmeopt-test-"))))

    def cycle(self, app):
        import asyncio

        from tests.test_cycle import COP, FLOW, OUT, FakeHa
        from varmeopt.ha import State

        ha = FakeHa({
            FLOW: State(FLOW, "31.0", {}, "f"),
            COP: State(COP, "4.4", {}, "m"),
            OUT: State(OUT, "5.0", {}, "u"),
        })
        asyncio.run(app.cycle(ha))

    def test_tuned_knobs_reach_the_planner(self):
        app = self.app()
        app.tuner.values["source_hysteresis"] = 0.08
        app.tuner.values["charge_min_saving_kr"] = 0.7
        self.cycle(app)
        self.assertEqual(app.planner.hysteresis, 0.08)
        self.assertEqual(app.planner.min_saving_kr, 0.7)
        self.assertEqual(app.vp_memory.band, 0.08)

    def test_switched_off_the_settings_win(self):
        app = self.app(self_tune_enabled=False)
        app.tuner.values["source_hysteresis"] = 0.08
        self.cycle(app)
        self.assertEqual(app.planner.hysteresis, app.options.source_hysteresis)

    def test_the_demand_factor_reaches_the_forecast(self):
        from varmeopt.demandfix import MIN_DAYS

        app = self.app()
        app.house_load.curve.predict = lambda temp: 2.0
        app._temperature_at = lambda m: 5.0
        self.assertEqual(app._demand_at(60), 2.0)
        app.demand.learned, app.demand.days = 1.2, MIN_DAYS
        self.assertAlmostEqual(app._demand_at(60), 2.4)
        off = self.app(demand_correction_enabled=False)
        off.house_load.curve.predict = lambda temp: 2.0
        off._temperature_at = lambda m: 5.0
        off.demand.learned, off.demand.days = 1.2, MIN_DAYS
        self.assertEqual(off._demand_at(60), 2.0)


if __name__ == "__main__":
    unittest.main()
