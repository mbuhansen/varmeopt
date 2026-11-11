import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from varmeopt import vpforecast
from varmeopt.vpforecast import (
    HORIZON_SLOTS,
    PELLET,
    SLOT_SECONDS,
    STANDBY_KW,
    VP,
    Inputs,
    SourceMemory,
    build,
    fallback,
)

PELLET_PRICE = 0.71
# En halvtimes start - 1. januar 2027 kl. 17:00 UTC.
T0 = 1_798_822_800.0


def inputs(**over):
    base = Inputs(
        now=T0,
        sources={},
        demand_kw_at=lambda m: 3.0,
        cop_at=lambda m: 4.0,
        charge_cop_at=lambda m: 3.0,
        vessel_kwh=lambda at, hours: 0.6,
        acting=True,
        cop_now=4.0,
    )
    return replace(base, **over)


class SourceMemoryTest(unittest.TestCase):
    """En halvtime skifter først når båndet er krydset og to beregninger er enige."""

    def test_a_new_half_hour_takes_the_projections_word(self):
        memory = SourceMemory(band=0.05, confirm=2)

        held = memory.update(T0, [(T0 + SLOT_SECONDS, VP, 0.69)], PELLET_PRICE, "plan-1")

        self.assertEqual(held[T0 + SLOT_SECONDS], VP)

    def test_a_switch_waits_for_two_predbat_plans(self):
        # Rundgangen: varmeopt melder varmepumpe, Predbat holder strøm, og
        # næste beregning siger pillefyr. Én beregning er ikke nok.
        memory = SourceMemory(band=0.05, confirm=2)
        slot = T0 + SLOT_SECONDS
        memory.update(T0, [(slot, VP, 0.60)], PELLET_PRICE, "plan-1")

        once = memory.update(T0, [(slot, PELLET, 0.80)], PELLET_PRICE, "plan-2")
        again = memory.update(T0, [(slot, PELLET, 0.80)], PELLET_PRICE, "plan-2")
        twice = memory.update(T0, [(slot, PELLET, 0.80)], PELLET_PRICE, "plan-3")

        self.assertEqual(once[slot], VP)
        self.assertEqual(again[slot], VP, "samme Predbat-beregning tæller én gang")
        self.assertEqual(twice[slot], PELLET)

    def test_a_price_inside_the_band_does_not_switch(self):
        # 0,69 mod 0,76 den 11. november: begge ligger tæt ved pillefyret. Et
        # skift inden for båndet er netop den vippen dæmpningen skal holde ude.
        memory = SourceMemory(band=0.05, confirm=2)
        slot = T0 + SLOT_SECONDS
        memory.update(T0, [(slot, VP, 0.69)], PELLET_PRICE, "plan-1")
        for stamp in ("plan-2", "plan-3", "plan-4"):
            held = memory.update(T0, [(slot, PELLET, 0.74)], PELLET_PRICE, stamp)

        self.assertEqual(held[slot], VP)

    def test_a_returning_word_cancels_the_pending_switch(self):
        memory = SourceMemory(band=0.05, confirm=2)
        slot = T0 + SLOT_SECONDS
        memory.update(T0, [(slot, VP, 0.60)], PELLET_PRICE, "plan-1")
        memory.update(T0, [(slot, PELLET, 0.80)], PELLET_PRICE, "plan-2")
        memory.update(T0, [(slot, VP, 0.60)], PELLET_PRICE, "plan-3")
        held = memory.update(T0, [(slot, PELLET, 0.80)], PELLET_PRICE, "plan-4")

        self.assertEqual(held[slot], VP)

    def test_past_half_hours_are_forgotten(self):
        memory = SourceMemory()
        memory.update(T0, [(T0, VP, 0.6)], PELLET_PRICE, "plan-1")

        held = memory.update(T0 + SLOT_SECONDS, [], PELLET_PRICE, "plan-2")

        self.assertNotIn(T0, held)


class BuildTest(unittest.TestCase):
    def test_a_heat_pump_half_hour_is_demand_and_vessels_over_cop(self):
        slot = T0 + SLOT_SECONDS
        forecast = build(inputs(sources={slot: VP}))

        _, source, kwh = forecast.slots[1]
        self.assertEqual(source, VP)
        # 3 kW ÷ 4 i en halv time, plus 0,6 kWh beholder ÷ lade-COP 3.
        self.assertAlmostEqual(kwh, 3.0 / 4.0 * 0.5 + 0.6 / 3.0, places=9)

    def test_a_pellet_half_hour_is_standby_only(self):
        slot = T0 + SLOT_SECONDS
        forecast = build(inputs(sources={slot: PELLET}))

        self.assertAlmostEqual(forecast.slots[1][2], STANDBY_KW * 0.5, places=9)

    def test_a_charge_block_is_its_own_draw(self):
        # En blok lader ved 56 grader med ladeeffekten, ikke husets behov.
        forecast = build(inputs(charge_window=(30, 90), charge_kw=11.0))

        self.assertAlmostEqual(forecast.slots[1][2], 11.0 / 3.0 * 0.5, places=9)
        self.assertAlmostEqual(forecast.slots[2][2], 11.0 / 3.0 * 0.5, places=9)
        self.assertNotAlmostEqual(forecast.slots[3][2], 11.0 / 3.0 * 0.5, places=3)

    def test_behind_the_projection_it_is_the_heat_pump(self):
        # Fejlene er skæve: mangler varmepumpen, sælger Predbat batteriet tomt.
        forecast = build(inputs(sources={}))

        self.assertTrue(all(source == VP for _, source, _ in forecast.slots))

    def test_without_control_it_is_the_heat_pump_everywhere(self):
        # Slået fra eller i opvarmning efter en genstart: Node-RED kører pumpen.
        slot = T0 + SLOT_SECONDS
        forecast = build(inputs(sources={slot: PELLET}, acting=False))

        self.assertEqual(forecast.slots[1][1], VP)
        self.assertIn("tilbagefald", forecast.reason)

    def test_the_current_half_hour_counts_only_what_is_left(self):
        # Ti minutter inde i halvtimen, pumpen trækker 1,5 kW målt.
        forecast = build(inputs(now=T0 + 600, hp_power_kw=1.5))

        self.assertAlmostEqual(forecast.slots[0][2], 1.5 * 0.5 * (20 / 30), places=9)

    def test_missing_lookups_fall_back_rather_than_vanish(self):
        forecast = build(
            inputs(
                demand_kw_at=lambda m: None,
                cop_at=lambda m: None,
                charge_cop_at=lambda m: None,
                vessel_kwh=lambda at, hours: None,
                fallback_demand_kw=2.0,
                cop_now=None,
            )
        )

        _, _, kwh = forecast.slots[1]
        self.assertAlmostEqual(kwh, 2.0 / vpforecast.FALLBACK_COP * 0.5, places=9)

    def test_fallback_on_stop_is_the_heat_pump(self):
        slot = T0 + SLOT_SECONDS
        forecast = fallback(inputs(sources={slot: PELLET}, hp_power_kw=0.0))

        self.assertEqual(forecast.slots[1][1], VP)
        self.assertIn("stoppet", forecast.reason)


class ExternalFormatTest(unittest.TestCase):
    """PredHeats format, som Predbat læser det (fetch.py: fetch_extra_load_forecast)."""

    def test_it_starts_at_zero_now_and_only_grows(self):
        points = build(inputs(now=T0 + 600)).external()

        self.assertEqual(points[0]["energy"], 0.0)
        energies = [p["energy"] for p in points]
        self.assertEqual(energies, sorted(energies))
        self.assertTrue(all(set(p) == {"last_updated", "energy"} for p in points))

    def test_it_reaches_past_predbats_48_and_a_half_hours(self):
        points = build(inputs()).external()

        last = datetime.strptime(points[-1]["last_updated"], vpforecast.TIME_FORMAT)
        first = datetime.strptime(points[0]["last_updated"], vpforecast.TIME_FORMAT)
        self.assertGreaterEqual((last - first).total_seconds() / 3600, 49)
        self.assertEqual(len(points), HORIZON_SLOTS + 1)

    def test_the_stamps_carry_an_offset(self):
        stamp = build(inputs()).external()[0]["last_updated"]

        self.assertIsNotNone(datetime.strptime(stamp, vpforecast.TIME_FORMAT).tzinfo)


class PublishTest(unittest.TestCase):
    """Sensoren kommer ud af en cyklus, og et stop efterlader varmepumpe overalt."""

    def setUp(self):
        from tests.test_cycle import COP, FLOW, OUT, FakeHa, options
        from varmeopt.__main__ import SENSOR_VP_FORECAST, Varmeopt
        from varmeopt.ha import State
        from varmeopt.store import Store

        self.sensor = SENSOR_VP_FORECAST
        self.app = Varmeopt(options(), Store(Path(tempfile.mkdtemp(prefix="varmeopt-vp-"))))
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "40.0", {}, "f"),
                COP: State(COP, "0", {}, "c"),
                OUT: State(OUT, "7.0", {}, "u"),
            }
        )

    def test_a_cycle_publishes_the_forecast(self):
        asyncio.run(self.app.cycle(self.ha))

        attributes = self.ha.attributes[self.sensor]
        self.assertEqual(attributes["unit_of_measurement"], "kWh")
        self.assertEqual(len(attributes["external"]), HORIZON_SLOTS + 1)
        # Ingen Predbat-plan i prøven: så står varmeopt ikke inde for andet.
        self.assertIn("tilbagefald", attributes["kilde"])
        self.assertIn("vp_forecast", self.app.status)

    def test_a_stop_writes_the_fallback(self):
        asyncio.run(self.app.cycle(self.ha))
        self.ha.attributes.pop(self.sensor)

        asyncio.run(self.app.release_control(self.ha))

        self.assertIn("stoppet", self.ha.attributes[self.sensor]["kilde"])


if __name__ == "__main__":
    unittest.main()
