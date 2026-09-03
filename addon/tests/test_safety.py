"""Regressionstests for de tre sikkerhedsfejl gennemgangen fandt.

Alle tre lå i kode der havde grønne tests. De blev ikke fanget fordi
attrapperne aldrig fejlede, og fordi ingen sammenlignede den pris
beslutningen brugte med den pris sensoren viste.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from varmeopt.__main__ import SENSOR_DECISION, Varmeopt
from varmeopt.cop import Cell, CopTable
from varmeopt.ha import HaError, HomeAssistant
from varmeopt.planner import Planner
from varmeopt.prices import Grid, Plan
from varmeopt.store import Store

from tests.test_cycle import COP, FLOW, FakeHa, FakeNodeRed, options
from varmeopt.ha import State

PELLET = 0.706


class MeterReachesTheDecisionTest(unittest.TestCase):
    """Beslutningen skal bruge den samme pris som sensoren viser."""

    def setUp(self):
        self.plan = Plan.from_predbat(
            {
                "raw": {
                    "rows": [
                        {
                            "state": "",
                            "import_rate": 350,
                            "export_rate": 60,
                            "soc_percent": 4,
                        }
                    ]
                }
            },
            battery_average=0.80,
        )
        self.planner = Planner(pellet_price=PELLET, charge_kw=16.0)

    def test_the_decision_prices_now_the_same_way_the_sensor_does(self):
        # Foer rettelsen: sensoren sagde 3,50 "net: import", beslutningen
        # regnede paa 0,80 "batteri: frit" — i samme cyklus.
        grid = Grid(grid_power=9000)

        sensor_price = self.plan.marginal(0, grid=grid).kr_per_kwh
        decision = self.planner.decide(self.plan, cop_now=3.0, grid=grid)

        self.assertAlmostEqual(sensor_price, 3.50, places=9)
        self.assertAlmostEqual(decision.heat_price, 3.50 / 3.0, places=9)

    def test_and_therefore_picks_the_boiler_when_the_grid_is_dear(self):
        # 3,50/3 = 1,17 kr/kWh varme mod pillefyrets 0,71.
        decision = self.planner.decide(
            self.plan, cop_now=3.0, grid=Grid(grid_power=9000)
        )

        self.assertEqual(decision.source, "pillefyr")

    def test_without_the_meter_it_would_have_chosen_the_heat_pump(self):
        # Dokumenterer selve fejlen, saa den ikke kan snige sig ind igen.
        decision = self.planner.decide(self.plan, cop_now=3.0, grid=None)

        self.assertEqual(decision.source, "varmepumpe")

    def test_the_projection_prices_the_now_row_with_the_meter_too(self):
        rows = self.planner.project(
            self.plan, cop_now=3.0, grid=Grid(grid_power=9000)
        )

        self.assertAlmostEqual(rows[0].electricity, 3.50, places=9)
        self.assertEqual(rows[0].reason, "net: import")


class ReleaseOnShutdownTest(unittest.TestCase):
    """Flaget skal falde når add-on'en stopper."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "f"),
                COP: State(COP, "4.4", {}, "m"),
            }
        )
        self.nodered = FakeNodeRed({"udeTemp": 17.2})

    def test_releasing_publishes_a_false_flag(self):
        asyncio.run(self.app.cycle(self.ha, self.nodered))
        self.ha.published.clear()

        asyncio.run(self.app.release_control(self.ha))

        self.assertEqual(len(self.ha.published), 1)
        entity, _ = self.ha.published[0]
        self.assertEqual(entity, SENSOR_DECISION)
        self.assertIs(self.ha.attributes[SENSOR_DECISION]["styrer"], False)
        self.assertIsNone(self.ha.attributes[SENSOR_DECISION]["styr_til"])

    def test_releasing_also_drops_the_guard_commitment(self):
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        asyncio.run(self.app.release_control(self.ha))

        self.assertIsNone(self.app.guard.committed)

    def test_a_failing_release_is_logged_not_raised(self):
        # Kan vi ikke give slip, er der ikke mere at goere - men cyklussen
        # maa ikke vaelte paa vej ud.
        async def boom(*_args, **_kwargs):
            raise HaError("HA svarer ikke")

        self.ha.set_state = boom

        asyncio.run(self.app.release_control(self.ha))  # maa ikke rejse


class TimeoutTest(unittest.TestCase):
    """En timeout må ramme det ene opslag, ikke hele cyklussen."""

    def setUp(self):
        os.environ["VARMEOPT_HA_TOKEN"] = "test"
        os.environ["VARMEOPT_HA_URL"] = "http://ha.test"

    def tearDown(self):
        os.environ.pop("VARMEOPT_HA_TOKEN", None)
        os.environ.pop("VARMEOPT_HA_URL", None)

    def test_a_timeout_becomes_a_haerror(self):
        # TimeoutError er ikke en ClientError. Foer rettelsen slap den forbi
        # og vaeltede cyklussen.
        class HangingSession:
            def get(self, *_args, **_kwargs):
                raise TimeoutError("for laenge")

            def post(self, *_args, **_kwargs):
                raise TimeoutError("for laenge")

        ha = HomeAssistant(HangingSession())

        with self.assertRaises(HaError):
            asyncio.run(ha.get_state("sensor.noget"))
        with self.assertRaises(HaError):
            asyncio.run(ha.set_state("sensor.noget", 1))

    def test_a_timeout_on_a_reading_does_not_stop_the_cycle(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        app = Varmeopt(options(), Store(tmp))
        app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})

        class FlakyHa(FakeHa):
            async def get_state(self, entity_id):
                if entity_id == FLOW:
                    raise HaError("timeout")
                return self._states.get(entity_id)

        ha = FlakyHa({COP: State(COP, "4.4", {}, "m")})

        # _state fanger HaError og giver None; cyklussen skal koere videre og
        # falde tilbage paa Node-REDs flowTemp.
        asyncio.run(app.cycle(ha, FakeNodeRed({"udeTemp": 17.2, "flowTemp": 31.0})))

        self.assertIsNotNone(app.status["lookup"])


if __name__ == "__main__":
    unittest.main()
