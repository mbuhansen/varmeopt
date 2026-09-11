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

from tests.test_cycle import COP, FLOW, OUT, FakeHa, options
from varmeopt.ha import State

PELLET = 0.706


class PlanReachesTheDecisionTest(unittest.TestCase):
    """Beslutningen skal bruge den samme pris som sensoren viser.

    Klassen hed før ``MeterReachesTheDecisionTest`` og stillede den samme
    halvtime op med ``Grid(grid_power=9000)``: måleren så import, og
    importgrenen svarede 3,50 hvor batterigrenen ville have sagt 0,80.

    Den gren findes ikke mere - se ``marginal`` for hvorfor - men
    *invarianten* er uændret og er stadig den vigtigste i filen: den pris
    beslutningen regner på, skal være den pris sensoren viser. Her tvinges
    de fra hinanden af planen i stedet, som er det eneste der må gøre det:
    halvtimen er "holdchrg", så afladningen er slået fra og strømmen
    købes - 3,50 - selv om batteriets egen energi ville koste 0,80.
    """

    def setUp(self):
        # Nu er batteriet bundet og nettet koster 3,50; om en halv time er
        # der import til 0,67, og det er den pris batteriets energi ville
        # skulle lægges tilbage til - 0,67 / 0,832 = 0,80 leveret. De to tal
        # skal ikke kunne forveksles.
        self.plan = Plan.from_predbat(
            {
                "raw": {
                    "rows": [
                        {
                            "state": "holdchrg",
                            "import_rate": 350,
                            "export_rate": 60,
                            "soc_percent": 50,
                        },
                        {
                            "state": "",
                            "import_rate": 67,
                            "export_rate": 60,
                            "soc_percent": 50,
                        },
                    ]
                }
            },
        )
        self.planner = Planner(pellet_price=PELLET, charge_kw=16.0)

    def test_the_decision_prices_now_the_same_way_the_sensor_does(self):
        sensor_price = self.plan.marginal(0).kr_per_kwh
        decision = self.planner.decide(self.plan, cop_now=3.0)

        self.assertAlmostEqual(sensor_price, 3.50, places=9)
        # Plus slitagen: varmepumpevarme koster 0,15 kr/kWh mere end
        # elprisen alene siger.
        self.assertAlmostEqual(decision.heat_price, 3.50 / 3.0 + 0.15, places=9)

    def test_and_therefore_picks_the_boiler_when_the_grid_is_dear(self):
        # 3,50/3 = 1,17 kr/kWh varme mod pillefyrets 0,71.
        decision = self.planner.decide(self.plan, cop_now=3.0)

        self.assertEqual(decision.source, "pillefyr")

    def test_the_meter_cannot_move_any_of_it(self):
        """Afløseren for ``test_without_the_meter_it_would_have_chosen_...``.

        Den gamle test dokumenterede at måleren *ændrede* svaret. Nu er
        kravet det modsatte, og det er skarpere: hvad måleren end siger,
        skal prisen, beslutningen og fremskrivningen være de samme.
        """
        for power in (-9000, -300, 0, 300, 9000):
            with self.subTest(grid_power=power):
                grid = Grid(grid_power=power)
                self.assertAlmostEqual(
                    self.plan.marginal(0, grid=grid).kr_per_kwh, 3.50, places=9
                )
                self.assertEqual(
                    self.planner.decide(self.plan, cop_now=3.0, grid=grid).source,
                    "pillefyr",
                )

    def test_the_projection_prices_the_now_row_the_same_way(self):
        rows = self.planner.project(self.plan, cop_now=3.0)

        self.assertAlmostEqual(rows[0].electricity, 3.50, places=9)
        self.assertEqual(rows[0].reason, "net")


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
                OUT: State(OUT, "17.2", {}, "u"),
            }
        )

    def test_releasing_publishes_a_false_flag(self):
        asyncio.run(self.app.cycle(self.ha))
        self.ha.published.clear()

        asyncio.run(self.app.release_control(self.ha))

        published = [entity for entity, _ in self.ha.published]
        self.assertEqual(published, [SENSOR_CHARGE, SENSOR_DECISION])
        self.assertIs(self.ha.attributes[SENSOR_DECISION]["styrer"], False)
        self.assertIsNone(self.ha.attributes[SENSOR_DECISION]["styr_til"])
        # Opladningsflaget er det farligste at efterlade tændt: en frossen
        # kilde ville bare fortsætte, men det her ville blive ved med at
        # fylde tankene. Derfor slippes det først.
        self.assertEqual(dict(self.ha.published)[SENSOR_CHARGE], "off")

    def test_a_failing_decision_release_still_drops_the_charge_flag(self):
        # Lå de to i samme forsøg, ville en fejl på det ene efterlade det
        # andet frosset - præcis den tilstand det hele er til for at undgå.
        asyncio.run(self.app.cycle(self.ha))
        self.ha.published.clear()
        self.ha.fail_on = SENSOR_DECISION

        asyncio.run(self.app.release_control(self.ha))

        self.assertEqual(dict(self.ha.published)[SENSOR_CHARGE], "off")

    def test_releasing_also_drops_the_guard_commitment(self):
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        asyncio.run(self.app.cycle(self.ha))

        asyncio.run(self.app.release_control(self.ha))

        self.assertIsNone(self.app.guard.committed)

    def test_a_failing_release_is_logged_not_raised(self):
        # Kan vi ikke give slip, er der ikke mere at gøre - men cyklussen
        # må ikke vælte på vej ud.
        async def boom(*_args, **_kwargs):
            raise HaError("HA svarer ikke")

        self.ha.set_state = boom

        asyncio.run(self.app.release_control(self.ha))  # må ikke rejse


class TimeoutTest(unittest.TestCase):
    """En timeout må ramme det ene opslag, ikke hele cyklussen."""

    def setUp(self):
        os.environ["VARMEOPT_HA_TOKEN"] = "test"
        os.environ["VARMEOPT_HA_URL"] = "http://ha.test"

    def tearDown(self):
        os.environ.pop("VARMEOPT_HA_TOKEN", None)
        os.environ.pop("VARMEOPT_HA_URL", None)

    def test_a_timeout_becomes_a_haerror(self):
        # TimeoutError er ikke en ClientError. Før rettelsen slap den forbi
        # og væltede cyklussen.
        class HangingSession:
            def get(self, *_args, **_kwargs):
                raise TimeoutError("for længe")

            def post(self, *_args, **_kwargs):
                raise TimeoutError("for længe")

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

        ha = FlakyHa(
            {
                COP: State(COP, "4.4", {}, "m"),
                OUT: State(OUT, "17.2", {}, "u"),
            }
        )

        # _state fanger HaError og giver None. Uden setpunkt er der intet at
        # slå op på - men cyklussen skal køre videre og stadig udgive en
        # beslutning i stedet for at rejse.
        asyncio.run(app.cycle(ha))

        self.assertIsNone(app.status["lookup"])
        self.assertIn(SENSOR_DECISION, dict(ha.published))



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
                OUT: State(OUT, "17.2", {}, "u"),
            }
        )

    def test_the_flag_is_published_before_everything_else(self):
        asyncio.run(self.app.cycle(self.ha))

        first = self.ha.published[0][0]
        self.assertEqual(first, SENSOR_DECISION)

    def test_a_failing_tank_write_does_not_swallow_the_flag(self):
        # Før lå flaget sidst af seks skrivninger, så én HaError i en af de
        # andre sprang det over.
        original = self.app._publish_tank

        async def boom(*_a, **_k):
            raise HaError("HA svarer ikke")

        self.app._publish_tank = boom
        asyncio.run(self.app.cycle(self.ha))
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
        after = self.Guard(
            enabled=True,
            min_dwell_minutes=15.0,
            warmup_minutes=0.0,
            confirm_minutes=0.0,
        )
        after.restore({"committed": "pillefyr", "committed_at": now - 3 * 60})

        cmd = after.check(_decision("varmepumpe"), object(), None, now)

        # Tre minutter er gået af de femten - ikke nul, som før.
        self.assertEqual(cmd.source, "pillefyr")
        self.assertIn("holder", cmd.reason)

    def test_warmup_still_applies_after_a_restart(self):
        # Bindingen genoptages, men opvarmningen skal gælde forfra.
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

    def _ha_with_plan(self, age_minutes):
        o = self.app.options
        stamp = (self.now - self.timedelta(minutes=age_minutes)).isoformat()
        return FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "f"),
                COP: State(COP, "4.4", {}, "m"),
                OUT: State(OUT, "17.2", {}, "u"),
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
        asyncio.run(self.app.cycle(ha))

        self.assertIsNotNone(self.app.status.get("price_now"))

    def test_a_stale_plan_is_dropped(self):
        # Priserne ser gyldige ud, men de er fra et andet tidspunkt.
        ha = self._ha_with_plan(age_minutes=180)
        asyncio.run(self.app.cycle(ha))

        self.assertIsNone(self.app.status.get("price_now"))

    def test_and_then_the_guard_refuses_to_control(self):
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        ha = self._ha_with_plan(age_minutes=180)
        asyncio.run(self.app.cycle(ha))

        self.assertFalse(self.app.status["command"].acting)

    def test_age_uses_last_updated_not_last_changed(self):
        # Predbats plan ligger i attributterne. last_changed står stille når
        # kun de ændrer sig, så den ville sige at planen var timer gammel.
        old = (self.now - self.timedelta(hours=6)).isoformat()
        fresh = (self.now - self.timedelta(minutes=2)).isoformat()
        state = State("x", "ok", {}, last_changed=old, last_updated=fresh)

        self.assertLess(state.age_seconds(self.now), 300)

    def test_a_missing_timestamp_is_not_treated_as_stale(self):
        self.assertIsNone(State("x", "ok", {}).age_seconds())


if __name__ == "__main__":
    unittest.main()


class ChargeFlagTest(unittest.TestCase):
    """Opladningen som sin egen entitet, så den ikke skal graves ud."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-lad-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table.learn(31, 17, 4.4)
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "f"),
                COP: State(COP, "4.4", {}, "m"),
                OUT: State(OUT, "17.2", {}, "u"),
            }
        )
        # Uden tanke er der ingen plads at lade op i, og så vil
        # planlæggeren aldrig sige ja uanset prisen. Halvtomme tanke:
        # 1000 L mellem 30 og 60 grader med rigelig plads.
        o = self.app.options
        for entity, temp in (
            (o.entity_tank_a_top, 44.0), (o.entity_tank_a_mid, 40.0),
            (o.entity_tank_a_bottom, 34.0), (o.entity_tank_b_top, 42.0),
            (o.entity_tank_b_mid, 38.0), (o.entity_tank_b_bottom, 33.0),
        ):
            self.ha._states[entity] = State(entity, str(temp), {}, "t")

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
        asyncio.run(self.app.cycle(self.ha))

        self.assertEqual(self.flag(), "off")

    def test_it_is_on_when_the_planner_wants_to_charge(self):
        # Billigt nu, dyrt om lidt.
        self.plan(40, 40, 300, 300)
        asyncio.run(self.app.cycle(self.ha))

        self.assertEqual(self.flag(), "on")

    def test_it_carries_the_same_gate_as_the_decision(self):
        # Tilstanden er hvad planlæggeren vil; "styrer" siger om det må
        # følges. De to skal aldrig kunne sige hver sit.
        self.plan(40, 40, 300, 300)
        asyncio.run(self.app.cycle(self.ha))

        self.assertEqual(
            self.ha.attributes[SENSOR_CHARGE]["styrer"],
            self.ha.attributes[SENSOR_DECISION]["styrer"],
        )

    def test_the_numbers_ride_along_for_those_who_want_them(self):
        self.plan(40, 40, 300, 300)
        asyncio.run(self.app.cycle(self.ha))
        attrs = self.ha.attributes[SENSOR_CHARGE]

        self.assertGreater(attrs["lad_kwh"], 0)
        self.assertIsNotNone(attrs["vindue_min"])

