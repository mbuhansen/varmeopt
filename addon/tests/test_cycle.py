import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from varmeopt.__main__ import Varmeopt
from varmeopt.cop import Cell, CopTable
from varmeopt.ha import State
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

    def measure(self, cop: object, last_changed: str | None) -> None:
        self._states[COP] = State(COP, str(cop), {}, last_changed)

    async def get_state(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)

    async def set_state(self, entity_id, state, attributes=None) -> None:
        self.published.append((entity_id, state))


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

        self.assertEqual(len(self.ha.published), 1)
        entity_id, value = self.ha.published[0]
        self.assertEqual(entity_id, "sensor.varmeopt_cop")
        self.assertIsInstance(value, float)

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
        self.assertEqual(self.ha.published, [])


if __name__ == "__main__":
    unittest.main()
