"""Regressionstests for de tre sikkerhedsfejl gennemgangen fandt.

Alle tre lå i kode der havde grønne tests. De blev ikke fanget fordi
attrapperne aldrig fejlede, og fordi ingen sammenlignede den pris
beslutningen brugte med den pris sensoren viste.
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from varmeopt.__main__ import SENSOR_CHARGE, SENSOR_DECISION, Varmeopt
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
                            "soc_percent": 50,
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
        # Plus slitagen: varmepumpevarme koster 0,15 kr/kWh mere end
        # elprisen alene siger.
        self.assertAlmostEqual(decision.heat_price, 3.50 / 3.0 + 0.15, places=9)

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

        published = [entity for entity, _ in self.ha.published]
        self.assertEqual(published, [SENSOR_CHARGE, SENSOR_DECISION])
        self.assertIs(self.ha.attributes[SENSOR_DECISION]["styrer"], False)
        self.assertIsNone(self.ha.attributes[SENSOR_DECISION]["styr_til"])
        # Opladningsflaget er det farligste at efterlade taendt: en frossen
        # kilde ville bare fortsaette, men det her ville blive ved med at
        # fylde tankene. Derfor slippes det foerst.
        self.assertEqual(dict(self.ha.published)[SENSOR_CHARGE], "off")

    def test_a_failing_decision_release_still_drops_the_charge_flag(self):
        # Laa de to i samme forsoeg, ville en fejl paa det ene efterlade det
        # andet frosset - praecis den tilstand det hele er til for at undgaa.
        asyncio.run(self.app.cycle(self.ha, self.nodered))
        self.ha.published.clear()
        self.ha.fail_on = SENSOR_DECISION

        asyncio.run(self.app.release_control(self.ha))

        self.assertEqual(dict(self.ha.published)[SENSOR_CHARGE], "off")

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



class PublishOrderTest(unittest.TestCase):
    """Flaget skal ud, også når de andre skrivninger fejler."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        o = self.app.options
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "f"),
                COP: State(COP, "4.4", {}, "m"),
                o.entity_tank_a_top: State(o.entity_tank_a_top, "55", {}, "t"),
                o.entity_tank_a_mid: State(o.entity_tank_a_mid, "45", {}, "t"),
                o.entity_tank_a_bottom: State(o.entity_tank_a_bottom, "35", {}, "t"),
            }
        )
        self.nodered = FakeNodeRed({"udeTemp": 17.2})

    def test_the_flag_is_published_before_everything_else(self):
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        first = self.ha.published[0][0]
        self.assertEqual(first, SENSOR_DECISION)

    def test_a_failing_tank_write_does_not_swallow_the_flag(self):
        # Foer laa flaget sidst af seks skrivninger, saa én HaError i en af de
        # andre sprang det over.
        original = self.app._publish_tank

        async def boom(*_a, **_k):
            raise HaError("HA svarer ikke")

        self.app._publish_tank = boom
        asyncio.run(self.app.cycle(self.ha, self.nodered))
        self.app._publish_tank = original

        published = dict(self.ha.published)
        self.assertIn(SENSOR_DECISION, published)
        self.assertNotIn("sensor.varmeopt_lager", published)


class GuardSurvivesRestartTest(unittest.TestCase):
    """Opholdstiden skal fortsætte hvor den slap."""

    def setUp(self):
        from varmeopt.guard import Guard

        self.Guard = Guard

    def test_the_commitment_is_carried_across(self):
        import time as _time

        before = self.Guard(enabled=True, warmup_minutes=0.0)
        before.check(_decision("pillefyr"), object(), None, _time.time())

        after = self.Guard(enabled=True, warmup_minutes=0.0)
        after.restore(before.to_raw())

        self.assertEqual(after.committed, "pillefyr")
        self.assertEqual(after.committed_at, before.committed_at)

    def test_a_switch_is_still_held_after_a_restart(self):
        import time as _time

        now = _time.time()
        after = self.Guard(enabled=True, min_dwell_minutes=15.0, warmup_minutes=0.0)
        after.restore({"committed": "pillefyr", "committed_at": now - 3 * 60})

        cmd = after.check(_decision("varmepumpe"), object(), None, now)

        # Tre minutter er gaaet af de femten - ikke nul, som foer.
        self.assertEqual(cmd.source, "pillefyr")
        self.assertIn("holder", cmd.reason)

    def test_warmup_still_applies_after_a_restart(self):
        # Bindingen genoptages, men opvarmningen skal gaelde forfra.
        import time as _time

        now = _time.time()
        after = self.Guard(enabled=True, warmup_minutes=5.0)
        after.restore({"committed": "pillefyr", "committed_at": now - 60 * 60})

        cmd = after.check(_decision("varmepumpe"), object(), None, now)

        self.assertFalse(cmd.acting)
        self.assertIn("varmer op", cmd.reason)

    def test_garbage_restores_to_nothing(self):
        g = self.Guard()
        for junk in (None, "ikke en binding", {"committed": "noget andet"}):
            g.restore(junk)
            self.assertIsNone(g.committed)


def _decision(source):
    from varmeopt.planner import Decision

    return Decision(source=source, heat_price=0.30, pellet_price=PELLET)


class StalePlanTest(unittest.TestCase):
    """En forældet plan er farligere end ingen plan."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone

        self.now = datetime.now(timezone.utc)
        self.timedelta = timedelta
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        self.nodered = FakeNodeRed({"udeTemp": 17.2})

    def _ha_with_plan(self, age_minutes):
        o = self.app.options
        stamp = (self.now - self.timedelta(minutes=age_minutes)).isoformat()
        return FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "f"),
                COP: State(COP, "4.4", {}, "m"),
                o.entity_predbat_plan: State(
                    o.entity_predbat_plan,
                    "ok",
                    {"raw": {"rows": [{"state": "holdchrg", "import_rate": 40,
                                       "export_rate": 50}]}},
                    stamp,
                    stamp,
                ),
            }
        )

    def test_a_fresh_plan_is_used(self):
        ha = self._ha_with_plan(age_minutes=5)
        asyncio.run(self.app.cycle(ha, self.nodered))

        self.assertIsNotNone(self.app.status.get("price_now"))

    def test_a_stale_plan_is_dropped(self):
        # Priserne ser gyldige ud, men de er fra et andet tidspunkt.
        ha = self._ha_with_plan(age_minutes=180)
        asyncio.run(self.app.cycle(ha, self.nodered))

        self.assertIsNone(self.app.status.get("price_now"))

    def test_and_then_the_guard_refuses_to_control(self):
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        ha = self._ha_with_plan(age_minutes=180)
        asyncio.run(self.app.cycle(ha, self.nodered))

        self.assertFalse(self.app.status["command"].acting)

    def test_age_uses_last_updated_not_last_changed(self):
        # Predbats plan ligger i attributterne. last_changed staar stille naar
        # kun de aendrer sig, saa den ville sige at planen var timer gammel.
        old = (self.now - self.timedelta(hours=6)).isoformat()
        fresh = (self.now - self.timedelta(minutes=2)).isoformat()
        state = State("x", "ok", {}, last_changed=old, last_updated=fresh)

        self.assertLess(state.age_seconds(self.now), 300)

    def test_a_missing_timestamp_is_not_treated_as_stale(self):
        self.assertIsNone(State("x", "ok", {}).age_seconds())


if __name__ == "__main__":
    unittest.main()


class ChargeFlagTest(unittest.TestCase):
    """Opladningen som sin egen entitet, saa den ikke skal graves ud."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-lad-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table.learn(31, 17, 4.4)
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "f"),
                COP: State(COP, "4.4", {}, "m"),
            }
        )
        # Uden tanke er der ingen plads at lade op i, og saa vil
        # planlaeggeren aldrig sige ja uanset prisen. Halvtomme tanke:
        # 1000 L mellem 30 og 60 grader med rigelig plads.
        o = self.app.options
        for entity, temp in (
            (o.entity_tank_a_top, 44.0), (o.entity_tank_a_mid, 40.0),
            (o.entity_tank_a_bottom, 34.0), (o.entity_tank_b_top, 42.0),
            (o.entity_tank_b_mid, 38.0), (o.entity_tank_b_bottom, 33.0),
        ):
            self.ha._states[entity] = State(entity, str(temp), {}, "t")
        self.nodered = FakeNodeRed({"udeTemp": 17.2})

    def plan(self, *rates):
        entity = self.app.options.entity_predbat_plan
        self.ha._states[entity] = State(
            entity,
            "ok",
            {"raw": {"rows": [
                {"state": "holdchrg", "import_rate": r, "export_rate": 40,
                 "soc_percent": 60} for r in rates
            ]}},
            "plan",
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

    def flag(self):
        return dict(self.ha.published).get(SENSOR_CHARGE)

    def test_it_is_off_when_there_is_nothing_to_gain(self):
        # Flad pris: intet at hente ved at flytte varmen.
        self.plan(80, 80, 80, 80)
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        self.assertEqual(self.flag(), "off")

    def test_it_is_on_when_the_planner_wants_to_charge(self):
        # Billigt nu, dyrt om lidt.
        self.plan(40, 40, 300, 300)
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        self.assertEqual(self.flag(), "on")

    def test_it_carries_the_same_gate_as_the_decision(self):
        # Tilstanden er hvad planlaeggeren vil; "styrer" siger om det maa
        # foelges. De to skal aldrig kunne sige hver sit.
        self.plan(40, 40, 300, 300)
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        self.assertEqual(
            self.ha.attributes[SENSOR_CHARGE]["styrer"],
            self.ha.attributes[SENSOR_DECISION]["styrer"],
        )

    def test_the_numbers_ride_along_for_those_who_want_them(self):
        self.plan(40, 40, 300, 300)
        asyncio.run(self.app.cycle(self.ha, self.nodered))
        attrs = self.ha.attributes[SENSOR_CHARGE]

        self.assertGreater(attrs["lad_kwh"], 0)
        self.assertIsNotNone(attrs["vindue_min"])

