import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from varmeopt.__main__ import Varmeopt
from varmeopt.cop import Cell, CopTable
from varmeopt.ha import HaError, State
from varmeopt.options import Options
from varmeopt.store import Store

FLOW = "sensor.flow"
COP = "sensor.cop"


def options(**over):
    # Tag defaults fra Options selv i stedet for at ramse felterne op her.
    # Ellers vælter hele denne fil hver gang der kommer en ny indstilling.
    base = Options.load(Path("findes-ikke.json"))
    return replace(
        base,
        entity_flow_temp=FLOW,
        entity_cop_measured=COP,
        entity_outdoor_temp="",
        **over,
    )


class FakeHa:
    """Nok af HomeAssistant til at cycle() kan køre uden netværk."""

    def __init__(self, states: dict[str, State]) -> None:
        self._states = states
        self.published: list[tuple[str, object]] = []
        self.attributes: dict[str, dict] = {}
        # Vejrudsigten hentes med et service-kald, ikke som en tilstand.
        self.forecast_response: dict = {}
        self.services: list[tuple[str, str]] = []
        # Naar den er sat, fejler skrivningen til netop den entitet. Bruges
        # til at proeve at én fejlet udgivelse ikke tager de andre med sig.
        self.fail_on: str | None = None

    def measure(self, cop: object, last_changed: str | None) -> None:
        self._states[COP] = State(COP, str(cop), {}, last_changed)

    async def get_state(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)

    async def set_state(self, entity_id, state, attributes=None) -> None:
        if self.fail_on is not None and entity_id == self.fail_on:
            raise HaError(f"skrivning til {entity_id} fejlede (proeve)")
        self.published.append((entity_id, state))
        self.attributes[entity_id] = attributes or {}

    async def call_service(self, domain, service, data):
        self.services.append((domain, service))
        return self.forecast_response


class FakeNodeRed:
    """Udetemperaturen findes kun i Node-REDs flow-context, ikke som entitet."""

    def __init__(self, context: dict) -> None:
        self._context = context

    async def flow_context(self) -> dict:
        return dict(self._context)


class CycleTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        # Én belagt celle, så vi kan se præcis hvor meget en cyklus lægger til.
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "flow-1"),
                COP: State(COP, "4.4", {}, "maaling-1"),
            }
        )
        self.nodered = FakeNodeRed({"udeTemp": 17.2})

    def cycle(self, times: int = 1):
        for _ in range(times):
            asyncio.run(self.app.cycle(self.ha, self.nodered))

    @property
    def samples(self) -> float:
        return self.app.table.sample_count

    # -------------------------------------------------------------- dedup

    def test_same_measurement_is_only_learned_once(self):
        # Kernen: pumpen kører stabilt, sensoren står stille, vi poller videre.
        self.cycle(times=5)

        self.assertEqual(self.samples, 11.0)

    def test_new_last_changed_is_learned_again(self):
        self.cycle()
        self.ha.measure(4.6, "maaling-2")
        self.cycle()

        self.assertEqual(self.samples, 12.0)

    def test_without_last_changed_we_learn_every_cycle(self):
        # Lokal afprøvning mod en attrap: uden tidsstempel kan to målinger
        # ikke kendes fra hinanden, og så lærer vi hellere for meget.
        self.ha.measure(4.4, None)
        self.cycle(times=3)

        self.assertEqual(self.samples, 13.0)

    def test_stopped_pump_is_not_remembered_as_learned(self):
        # En ignoreret måling må ikke optage pladsen som "sidst lært", ellers
        # kunne den spærre for en rigtig måling bagefter.
        self.ha.measure(0, "maaling-1")
        self.cycle()

        self.assertEqual(self.samples, 10.0)
        self.assertIsNone(self.app._last_learned_stamp)

    def test_implausible_measurement_is_not_remembered_as_learned(self):
        self.ha.measure(99, "maaling-1")
        self.cycle()

        self.assertEqual(self.samples, 10.0)
        self.assertIsNone(self.app._last_learned_stamp)

    # ------------------------------------------------------------- oevrigt

    def test_lookup_is_published_to_home_assistant(self):
        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_cop", published)
        self.assertIsInstance(published["sensor.varmeopt_cop"], float)

    def test_tank_is_published_when_the_sensors_answer(self):
        o = self.app.options
        for eid, temp in (
            (o.entity_tank_a_top, 60),
            (o.entity_tank_a_mid, 45),
            (o.entity_tank_a_bottom, 30),
            (o.entity_tank_a_outlet, 58),
        ):
            self.ha._states[eid] = State(eid, str(temp), {}, "tank-1")
        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_lager", published)
        self.assertGreater(published["sensor.varmeopt_lager"], 0)

    def test_tank_is_skipped_when_no_sensor_answers(self):
        # Standardopsætningen i denne test har ingen tankfølere i FakeHa.
        self.cycle()

        self.assertIsNone(self.app.status["tank"])
        self.assertNotIn("sensor.varmeopt_lager", dict(self.ha.published))

    def test_the_price_and_the_source_decision_are_published(self):
        # Hele kaeden: Predbats plan -> marginalpris -> varmepris via COP ->
        # valg mod pillefyret. Det er den beslutning Node-RED traeffer i dag,
        # regnet paa den rettede COP.
        o = self.app.options
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {
                "raw": {
                    "rows": [
                        {"state": "holdchrg", "import_rate": 180, "export_rate": 60},
                        {"state": "", "import_rate": 40, "export_rate": 55},
                    ]
                }
            },
            "plan-1",
        )
        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_elpris", published)
        # Batteriet er bundet, saa varmepumpen koerer paa nettet: 1,80 kr.
        self.assertAlmostEqual(published["sensor.varmeopt_elpris"], 1.80, places=3)

        status = self.app.status
        self.assertEqual(status["price_now"].reason, "net: batteriet er bundet")
        # 1,80 delt med den lærte COP mod pillefyrets 0,706.
        self.assertIsNotNone(status["heat_price"])
        self.assertIn(status["decision"].source, ("varmepumpe", "pillefyr"))
        self.assertEqual(published["sensor.varmeopt_beslutning"], status["decision"].source)

    def test_a_missing_predbat_plan_is_not_fatal(self):
        # Predbat kan vaere nede eller endnu ikke have lagt en plan. Cyklussen
        # skal koere videre - COP-laeringen afhaenger ikke af priser.
        self.cycle()

        self.assertNotIn("sensor.varmeopt_elpris", dict(self.ha.published))
        self.assertIsNone(self.app.status.get("price_now"))
        # Men kildevalget staar stadig - det kraever ingen plan.
        self.assertEqual(self.app.status["decision"].source, "varmepumpe")

    def test_battery_average_comes_from_nodered(self):
        o = self.app.options
        self.nodered = FakeNodeRed({"udeTemp": 17.2, "battery_avg_price": 1.35})
        # Batteriet aflader maalbart - ellers staar anlaegget i balance, og saa
        # er det den billigste af net og batteri der gaelder, ikke batteriet.
        self.ha._states[o.entity_battery_power] = State(o.entity_battery_power, "3000", {}, "b")
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {"raw": {"rows": [{"state": "", "import_rate": 300, "export_rate": 50}]}},
            "plan-1",
        )
        self.cycle()

        # 1,35 er hvad energien kostede pr. lagret kWh; leveret igen koster
        # den 1/0,85 af det, for inverteren taber 15 % hele vejen rundt.
        self.assertAlmostEqual(
            self.app.status["price_now"].kr_per_kwh, 1.35 / 0.85, places=3
        )
        self.assertIn("batteri", self.app.status["price_now"].reason)

    def test_a_balanced_plant_takes_the_cheaper_of_grid_and_battery(self):
        # Ingen maalbar stroem nogen vej: solen daekker. Saa er svaret den
        # billigste af de to muligheder.
        o = self.app.options
        self.nodered = FakeNodeRed({"udeTemp": 17.2, "battery_avg_price": 1.35})
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {"raw": {"rows": [{"state": "", "import_rate": 40, "export_rate": 50}]}},
            "plan-1",
        )
        self.cycle()

        self.assertAlmostEqual(self.app.status["price_now"].kr_per_kwh, 0.40, places=3)
        self.assertEqual(self.app.status["price_now"].reason, "balanceret")

    def test_outdoor_temp_falls_back_to_nodered(self):
        self.cycle()

        self.assertEqual(self.app.status["outdoor_temp"], 17.2)
        self.assertEqual(self.app.status["flow_temp"], 31.0)

    def test_missing_temperatures_skip_the_cycle_without_raising(self):
        self.nodered = FakeNodeRed({})
        self.ha._states.pop(FLOW)
        self.cycle()

        self.assertIsNone(self.app.status["lookup"])
        self.assertEqual(self.samples, 10.0)
        # Uden temperaturer er der ingen COP at udgive - men styringen har
        # stadig et svar, og det er med vilje.
        self.assertNotIn("sensor.varmeopt_cop", dict(self.ha.published))
        self.assertIn("sensor.varmeopt_beslutning", dict(self.ha.published))


class ForecastTest(unittest.TestCase):
    """Vejrudsigten: hver time i planen faar sin egen COP."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone

        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table = CopTable(
            {44: {5: Cell(3.9, 300.0)}, 32: {15: Cell(4.6, 300.0)}}
        )
        from varmeopt.curve import HeatCurve, Point

        self.app.curve = HeatCurve({5: Point(44.0, 500.0), 15: Point(32.0, 500.0)})
        self.ha = FakeHa({FLOW: State(FLOW, "32.0", {}, "f")})
        now = datetime.now(timezone.utc)
        self.ha.forecast_response = {
            self.app.options.entity_weather: {
                "forecast": [
                    {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": t}
                    for h, t in ((0, 15.0), (6, 5.0))
                ]
            }
        }
        self.nodered = FakeNodeRed({"udeTemp": 15.0})

    def test_the_forecast_is_fetched_once_and_then_cached(self):
        asyncio.run(self.app.cycle(self.ha, self.nodered))
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        # Udsigten aendrer sig i timer, ikke i minutter.
        self.assertEqual(self.ha.services, [("weather", "get_forecasts")])
        self.assertGreater(len(self.app.forecast), 0)

    def test_a_colder_evening_gives_a_lower_cop_six_hours_out(self):
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        # 15 grader nu -> setpunkt 32 -> COP 4,6.
        # 5 grader om seks timer -> setpunkt 44 -> COP 3,9.
        self.assertAlmostEqual(self.app._cop_at(0), 4.6, places=1)
        self.assertAlmostEqual(self.app._cop_at(360), 3.9, places=1)

    def test_without_a_forecast_there_is_no_answer(self):
        self.ha.forecast_response = {}
        asyncio.run(self.app.cycle(self.ha, self.nodered))

        # Planlaeggeren falder saa tilbage paa den COP vi har nu.
        self.assertIsNone(self.app._cop_at(360))


if __name__ == "__main__":
    unittest.main()


class ControlTest(unittest.TestCase):
    """Styringen: add-on'en udstiller et flag, Node-RED følger det."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        o = self.app.options
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "flow-1"),
                COP: State(COP, "4.4", {}, "maaling-1"),
                # Uden en plan er der ingen pris, og uden en pris ingen
                # varmepris - saa naegter vagten med rette at styre.
                o.entity_predbat_plan: State(
                    o.entity_predbat_plan,
                    "ok",
                    {"raw": {"rows": [{"state": "holdchrg", "import_rate": 40,
                                       "export_rate": 50}]}},
                    "plan-1",
                ),
            }
        )
        self.nodered = FakeNodeRed({"udeTemp": 17.2})

    def cycle(self):
        asyncio.run(self.app.cycle(self.ha, self.nodered))

    def test_control_is_off_by_default(self):
        self.cycle()

        command = self.app.status["command"]
        self.assertFalse(command.acting)
        self.assertIn("slået fra", command.reason)

    def test_the_decision_is_still_published_when_not_controlling(self):
        # Vagten siger ikke hvad der skal goeres - kun om nogen boer goere det.
        self.cycle()

        self.assertIn("sensor.varmeopt_beslutning", dict(self.ha.published))
        self.assertIsNotNone(self.app.status["decision"].source)

    def test_control_holds_off_until_warmed_up(self):
        self.app.guard.enabled = True
        self.cycle()

        command = self.app.status["command"]
        self.assertFalse(command.acting)
        self.assertIn("varmer op", command.reason)

    def test_control_takes_over_once_warm(self):
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        self.cycle()

        command = self.app.status["command"]
        self.assertTrue(command.acting)
        self.assertEqual(command.source, self.app.status["decision"].source)

    def test_no_price_means_no_control_even_when_enabled(self):
        # Uden Predbats plan er der ingen varmepris. At handle paa en
        # antagelse er ikke styring, det er et gaet.
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        self.ha._states.pop(self.app.options.entity_predbat_plan)
        self.cycle()

        command = self.app.status["command"]
        self.assertFalse(command.acting)
        self.assertIn("ingen COP", command.reason)


if __name__ == "__main__":
    unittest.main()
