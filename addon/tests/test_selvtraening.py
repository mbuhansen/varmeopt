"""Det der er tilbage af selvtræningen: planen i logbogen, behovets faktor i
cyklussen og nattens gennemgang.

Genafspilningen og den natlige justering af knapperne er parkeret på
branchen ``simulator-parkeret`` - se ``Varmeopt._nightly``.
"""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from varmeopt.prices import Plan, Slot


class ReplayPlanTest(unittest.TestCase):
    """Planen i logbogen skal kunne bygges op igen med samme pris."""

    def test_the_replay_plan_gives_the_same_price(self):
        plan = Plan(tuple(Slot(k, "holdchrg", 2.0 + k / 10, 0.5, 50.0) for k in range(5)))
        again = Plan.from_replay(plan.to_replay())
        for m in (0, 30, 120):
            self.assertAlmostEqual(again.marginal(m).kr_per_kwh, plan.marginal(m).kr_per_kwh)

    def test_the_limits_come_back(self):
        plan = Plan(
            tuple(Slot(k, "exp", 2.0, 2.5, 60.0) for k in range(4)),
            starts_at=1_764_000_000.0,
            charge_limits=((1_764_000_000.0, 30.0),),
            export_limits=((1_764_000_000.0, 40.0),),
        )
        again = Plan.from_replay(json.loads(json.dumps(plan.to_replay(), ensure_ascii=False)))
        self.assertEqual(again.charge_limits, plan.charge_limits)
        self.assertEqual(again.export_limits, plan.export_limits)
        self.assertEqual(again.starts_at, plan.starts_at)
        self.assertAlmostEqual(again.marginal(0).kr_per_kwh, plan.marginal(0).kr_per_kwh)

    def test_an_old_logbook_plan_is_refused(self):
        # 0.101.0 gemte skærmudtrækket under det samme navn.
        old = {"kolonner": ["minutes", "state", "import"], "rækker": [[0, "holdchrg", 2.0]]}
        self.assertIsNone(Plan.from_replay(old))
        self.assertIsNone(Plan.from_replay({"rækker": [[0, "holdchrg", 2.0, 0.5, None]]}))

    def test_zero_is_a_number(self):
        plan = Plan((Slot(0, "holdchrg", 2.0, 0.5, 50.0),), empty_percent=0.0)
        self.assertEqual(Plan.from_replay(plan.to_replay()).empty_percent, 0.0)


class WiredIntoTheCycleTest(unittest.TestCase):
    def app(self, **over):
        from tests.test_cycle import options
        from varmeopt.__main__ import Varmeopt
        from varmeopt.store import Store

        return Varmeopt(options(**over), Store(Path(tempfile.mkdtemp(prefix="varmeopt-test-"))))

    def test_the_demand_factor_reaches_the_forecast(self):
        from varmeopt.demandfix import MIN_DAYS

        app = self.app()
        app.house_load.curve.predict = lambda temp: 2.0
        app._temperature_at = lambda m: 5.0
        self.assertEqual(app._demand_at(60), 2.0)
        app.demand.learned, app.demand.applied, app.demand.days = 1.2, 1.2, MIN_DAYS
        self.assertAlmostEqual(app._demand_at(60), 2.4)
        off = self.app(demand_correction_enabled=False)
        off.house_load.curve.predict = lambda temp: 2.0
        off._temperature_at = lambda m: 5.0
        off.demand.learned, off.demand.applied, off.demand.days = 1.2, 1.2, MIN_DAYS
        self.assertEqual(off._demand_at(60), 2.0)

    def test_a_modelled_window_is_not_logged_as_the_house(self):
        # Bad og spa trukket fra efter skøn: kurven lærer ikke af det, så
        # faktoren må heller ikke sammenligne med det.
        import time as _time

        app = self.app()
        app.house_load.kw, app.house_load.measured_at = 2.0, _time.time()
        self.assertEqual(app._house_measured(_time.time()), 2.0)
        app.house_load._modelled = True
        self.assertIsNone(app._house_measured(_time.time()))

    def test_the_night_runs_once_after_three(self):
        app = self.app()
        app.logbook.path = Path(__file__)  # findes
        two = datetime(2026, 12, 2, 2, 30).timestamp()
        three = datetime(2026, 12, 2, 3, 1).timestamp()
        self.assertIsNone(app._night_due(two))
        self.assertEqual(app._night_due(three), "2026-12-02")
        self.assertIsNone(app._night_due(three + 600), "kun én gang pr. nat")

    def test_the_cycle_writes_what_the_factor_learns_from(self):
        # Hele vejen: cyklussen skal skrive husets målte forbrug og den
        # faktor udsigten var ganget med, ellers har faktoren intet at lære af.
        from tests.test_cycle import COP, FLOW, OUT, FakeHa
        from varmeopt.ha import State
        from varmeopt.logbook import Logbook

        app = self.app()
        app.logbook = Logbook(None)
        app._house_measured = lambda now: 2.5
        ha = FakeHa({
            FLOW: State(FLOW, "31.0", {}, "f"),
            COP: State(COP, "4.4", {}, "m"),
            OUT: State(OUT, "5.0", {}, "u"),
        })
        slot = 1_764_000_000 - 1_764_000_000 % 1800
        import time as _time

        real = _time.time
        try:
            for minute in range(0, 32):
                _time.time = lambda m=minute: slot + m * 60
                asyncio.run(app.cycle(ha))
        finally:
            _time.time = real
        row = app.logbook.recent[-1]
        self.assertIn("behov_faktor", row["så"])
        self.assertAlmostEqual(row["skete"]["hus_kwh"], 1.25, places=2)


if __name__ == "__main__":
    unittest.main()
