"""Den lave opladning: lageret ladet ved varmekurvens fremløb, ikke 56 grader.

Den 24. november 2026 løb lageret tørt kl. 6, og varmepumpen kørte to timer
på batteriets strøm til 2,3-3,1 kr. Natten havde kostet 2,17 fra nettet. Den
høje opladning kunne ikke betale sig - ved 56 grader æder COP-straffen hele
forskellen - men en opladning ved kurvens 46 grader havde sparet ~1,8 kr.
"""

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from varmeopt.__main__ import SENSOR_CHARGE, SENSOR_CHARGE_LOW, SENSOR_DECISION, Varmeopt
from varmeopt.charge import ChargePlan, slot_start
from varmeopt.planner import PLACING_RULES, Decision, choose_charge
from varmeopt.store import Store

from tests.test_charge import plan as charge_plan_rows
from tests.test_cycle import COP, FLOW, OUT, FakeHa, options
from varmeopt.charge import Block
from varmeopt.cop import Cell, CopTable
from varmeopt.ha import State
import time
from tests.test_planner import plan, planner


def decision(rule, dhw_short=0.0, low=False):
    return Decision(
        source="varmepumpe",
        heat_price=0.5,
        pellet_price=0.7,
        rule=rule,
        dhw_short_kwh=dhw_short,
        low=low,
    )


class ChooseChargeTest(unittest.TestCase):
    def test_low_when_the_high_does_not_pay(self):
        chosen = choose_charge(decision("margin_small"), decision("charge_now", low=True))
        self.assertTrue(chosen.low)

    def test_low_when_only_room_heat_is_missing(self):
        chosen = choose_charge(decision("charge_now"), decision("wait_grid", low=True))
        self.assertTrue(chosen.low)

    def test_high_when_hot_water_is_missing(self):
        chosen = choose_charge(
            decision("charge_now", dhw_short=3.0), decision("charge_now", low=True)
        )
        self.assertFalse(chosen.low)

    def test_high_when_the_low_places_nothing(self):
        high = decision("charge_now")
        self.assertIs(choose_charge(high, decision("saving_small", low=True)), high)
        self.assertIs(choose_charge(high, None), high)


class MorningOfTheTwentyFourthTest(unittest.TestCase):
    """Natten 2,17 fra nettet, morgenen 2,70 - tallene fra den 24. november.

    Varmepumpen kostede 0,68 kr/kWh varme om natten og 0,72-0,91 om
    morgenen, pillefyret 0,756. Pillefyret er loftet over hvad morgenens varme
    ellers kostede, så gevinsten er ~0,07 kr/kWh - på ~10 kWh plads i
    lageret ~0,7 kr. Under det gulv den høje opladning har.
    """

    def setUp(self):
        # Fire billige halvtimer nu, så seks dyre. Udetemperatur 2 grader:
        # COP 4,09 ved kurvens 46,5, 3,45 ved 57,5.
        self.plan = plan(*[217] * 4, *[270] * 6, *[217] * 4)
        self.common = dict(
            cop_now=4.09,
            cop_later=lambda m: 4.09,
            stored_kwh=0.0,
            demand_kw=3.3,
        )

    def decide(self, **over):
        values = dict(self.common)
        values.update(over)
        return planner(
            horizon_minutes=420, charge_kw=8.0, pellet_price=0.756, min_saving_kr=1.0
        ).decide(self.plan, **values)

    def high(self):
        return self.decide(charge_cop_at=lambda m: 3.45, headroom_kwh=20.0)

    def low(self, **over):
        return self.decide(charge_cop_at=lambda m: 4.09, headroom_kwh=10.0, **over)

    def test_the_high_charge_does_not_pay(self):
        self.assertNotIn(self.high().rule, PLACING_RULES, self.high().reason)

    def test_the_low_charge_is_under_the_one_krone_floor(self):
        # Ærligt: med gulvet på 1 kr var der ikke blevet ladet den morgen.
        low = self.low()
        self.assertEqual(low.rule, "saving_small", low.reason)

    def test_with_its_own_floor_the_low_charge_is_placed(self):
        low = self.low(min_saving_kr=0.5)
        self.assertIn(low.rule, PLACING_RULES, low.reason)
        chosen = choose_charge(self.high(), replace(low, low=True, charge_temp=46.5))
        self.assertTrue(chosen.low)
        self.assertAlmostEqual(chosen.saving_kr or 0.0, 0.7, delta=0.15)


@dataclass
class FakeDecision:
    planned_kwh: float | None = 6.0
    window_starts_in: int | None = 240
    window_minutes: int | None = 300
    source: str = "varmepumpe"
    dear_starts_in: int | None = 240
    dear_span_minutes: int | None = 120
    dear_ends_in: int | None = None
    low: bool = False
    charge_temp: float | None = None


# Dyrt nu, billigt i slot 4-7, dyrt igen fra slot 8.
RATES = (90, 90, 90, 90, 35, 35, 35, 35, 155, 155, 155, 155)


class LowQuotaTest(unittest.TestCase):
    """Den lave har sin egen kvote: én pr. stræk, og den rører ikke den høje."""

    def setUp(self):
        self.now = 1_757_000_000.0
        self.base = slot_start(self.now)
        self.plan = charge_plan_rows(*RATES)
        self.charge = ChargePlan()

    def at(self, t, **over):
        # Strækket ligger fast på væggen; minutterne tæller fra halvtimen vi står i.
        shift = int((slot_start(t) - self.base) // 60)
        d = FakeDecision(
            window_starts_in=240 - shift, dear_starts_in=240 - shift, **over
        )
        return self.charge.update(t, d, self.plan, 16.0)

    def run_low_block(self):
        self.at(self.now, low=True, charge_temp=46.5)
        block = self.charge.block
        self.assertIsNotNone(block)
        self.assertTrue(block.low)
        self.assertEqual(block.temp, 46.5)
        self.at(block.ends_at, low=True, charge_temp=46.5)
        return block

    def test_a_finished_low_block_leaves_the_high_quota_alone(self):
        block = self.run_low_block()
        self.assertIsNone(self.charge.block)
        self.assertEqual(self.charge.low_done_until, block.dear_until)
        self.assertIsNone(self.charge.done_until)

    def test_a_second_low_block_for_the_same_stretch_is_refused(self):
        block = self.run_low_block()
        self.at(block.ends_at + 60, low=True, charge_temp=46.5)
        self.assertIsNone(self.charge.block)
        self.assertEqual(self.charge.rule, "covered_low")

    def test_a_high_block_may_still_follow_for_hot_water(self):
        block = self.run_low_block()
        self.at(block.ends_at + 60)
        self.assertIsNotNone(self.charge.block, self.charge.note)
        self.assertFalse(self.charge.block.low)

    def test_the_low_quota_survives_a_restart(self):
        self.run_low_block()
        back = ChargePlan.from_raw(self.charge.to_raw())
        self.assertEqual(back.low_done_until, self.charge.low_done_until)

    def test_a_waiting_low_block_survives_a_restart(self):
        self.at(self.now, low=True, charge_temp=46.5)
        back = ChargePlan.from_raw(self.charge.to_raw())
        self.assertTrue(back.block.low)
        self.assertEqual(back.block.temp, 46.5)


class KindsDoNotStepOnEachOtherTest(unittest.TestCase):
    """Gennemgangen den 24. november: en kvote eller et ønske af den ene slags
    må ikke fjerne eller bekræfte en blok af den anden."""

    def setUp(self):
        self.now = 1_757_000_000.0
        self.base = slot_start(self.now)
        self.plan = charge_plan_rows(*RATES)
        self.charge = ChargePlan()

    def decision_at(self, t, **over):
        shift = int((slot_start(t) - self.base) // 60)
        return FakeDecision(window_starts_in=240 - shift, dear_starts_in=240 - shift, **over)

    def at(self, t, stamp=None, confirm=1, **over):
        return self.charge.update(
            t, self.decision_at(t, **over), self.plan, 16.0,
            plan_stamp=stamp, confirm_plans=confirm,
        )

    def run_block(self, **kind):
        self.at(self.now, **kind)
        block = self.charge.block
        self.at(block.ends_at, **kind)
        return block

    def test_the_low_quota_keeps_a_waiting_high_block(self):
        # Strækket er ladet lavt op; en høj blok til varmt vand venter.
        self.charge.low_done_until = self.base + 600 * 60
        self.at(self.now)
        self.assertFalse(self.charge.block.running(self.now), "blokken skal vente")
        self.at(self.now + 60, low=True, charge_temp=46.5)  # ét minut vipper valget
        self.assertIsNotNone(self.charge.block, self.charge.note)
        self.assertFalse(self.charge.block.low)

    def test_the_high_quota_keeps_a_waiting_low_block(self):
        # Strækket er ladet højt op; en lav blok venter.
        self.charge.done_until = self.base + 600 * 60
        self.at(self.now, low=True, charge_temp=46.5)
        self.assertTrue(self.charge.block.low)
        self.assertFalse(self.charge.block.running(self.now), "blokken skal vente")
        self.at(self.now + 60)
        self.assertIsNotNone(self.charge.block, self.charge.note)
        self.assertTrue(self.charge.block.low)

    def test_may_place_knows_each_quota(self):
        block = self.run_block()
        t = block.ends_at + 60
        self.assertFalse(self.charge.may_place(t, self.decision_at(t)))
        self.assertTrue(self.charge.may_place(t, self.decision_at(t, low=True)))

    def test_a_used_high_quota_lets_the_low_through(self):
        chosen = choose_charge(
            decision("charge_now", dhw_short=3.0),
            decision("charge_now", low=True),
            high_open=False,
        )
        self.assertTrue(chosen.low)
        self.assertFalse(
            choose_charge(
                decision("charge_now"), decision("charge_now", low=True), low_open=False
            ).low
        )

    def test_one_wish_of_each_kind_is_not_two_confirmations(self):
        self.at(self.now, stamp="A", confirm=2)
        self.at(self.now + 60, stamp="B", confirm=2, low=True, charge_temp=46.5)
        self.assertIsNone(self.charge.block)
        self.assertEqual(self.charge.rule, "waiting_predbat")

    def test_switching_a_waiting_block_needs_its_own_confirmation(self):
        self.at(self.now, stamp="A", confirm=1)
        self.assertFalse(self.charge.block.low)
        self.at(self.now + 60, stamp="B", confirm=2, low=True, charge_temp=46.5)
        self.assertFalse(self.charge.block.low, "ét minuts vip vender ikke blokken")
        self.at(self.now + 120, stamp="C", confirm=2, low=True, charge_temp=46.5)
        self.assertTrue(self.charge.block.low)

    def test_a_promise_of_the_other_kind_is_not_written_on_the_block(self):
        self.at(self.now)
        other = SimpleNamespace(low=True, charge=True, saving_kr=5.0,
                                expected_paid=0.5, expected_alt=0.7)
        self.charge.remember_promise(other, self.now)
        self.assertIsNone(self.charge.block.expected_kr)


class LowCountsHotWaterTest(unittest.TestCase):
    """Den lave må ikke regne badets del af lageret som rumvarme."""

    def decide(self, **over):
        values = dict(
            cop_now=4.0,
            cop_later=lambda m: 4.0,
            charge_cop_at=lambda m: 4.0,
            headroom_kwh=20.0,
            stored_kwh=6.0,
            demand_kw=2.0,
        )
        values.update(over)
        return planner(horizon_minutes=420, charge_kw=8.0).decide(
            plan(*[100] * 4, *[300] * 6, *[100] * 4), **values
        )

    def test_hot_water_in_the_stretch_is_taken_from_what_the_store_has(self):
        # 6 kWh over 55 grader, 4 af dem går til bad i strækket: kun 2 er
        # tilbage til gulvet.
        d = self.decide(hot_kwh=6.0, dhw_kwh_over=lambda start, hours: 4.0, space_only=True)
        self.assertAlmostEqual(d.space_have_kwh, 2.0)
        self.assertEqual(d.dhw_short_kwh, 0.0, "den lave laver ikke varmt vand")
        self.assertIn(d.rule, PLACING_RULES, d.reason)

    def test_the_low_block_is_judged_on_room_heat_only(self):
        from varmeopt.__main__ import _covers

        short = SimpleNamespace(dhw_kwh=3.0, space_kwh=0.0)
        self.assertFalse(_covers(short))
        self.assertTrue(_covers(short, space_only=True))


class LowFlagTest(unittest.TestCase):
    """To flag til UVR'en, og aldrig begge tændt."""

    def setUp(self):
        self.app = Varmeopt(options(), Store(Path(tempfile.mkdtemp(prefix="varmeopt-test-"))))
        self.ha = FakeHa({})
        self.command = SimpleNamespace(acting=True, source="varmepumpe", reason="")

    def publish(self, d):
        asyncio.run(self.app._publish_charge(self.ha, d, self.command))
        asyncio.run(self.app._publish_decision(self.ha, d, self.command))
        return dict(self.ha.published)

    def test_a_low_charge_raises_only_the_low_flag(self):
        d = replace(decision("charge_now", low=True), charge=True, charge_temp=46.5)
        published = self.publish(d)
        self.assertEqual(published[SENSOR_CHARGE_LOW], "on")
        self.assertEqual(published[SENSOR_CHARGE], "off")
        self.assertEqual(self.ha.attributes[SENSOR_CHARGE_LOW]["mål_temp"], 46.5)
        self.assertIs(self.ha.attributes[SENSOR_DECISION]["lad_op_lav"], True)
        self.assertIs(self.ha.attributes[SENSOR_DECISION]["lad_op"], False)

    def test_a_high_charge_raises_only_the_high_flag(self):
        d = replace(decision("charge_now"), charge=True, charge_temp=56.0)
        published = self.publish(d)
        self.assertEqual(published[SENSOR_CHARGE], "on")
        self.assertEqual(published[SENSOR_CHARGE_LOW], "off")
        self.assertIs(self.ha.attributes[SENSOR_DECISION]["lad_op"], True)

    def test_the_flag_to_turn_off_is_written_first(self):
        d = replace(decision("charge_now", low=True), charge=True, charge_temp=46.5)
        asyncio.run(self.app._publish_charge(self.ha, d, self.command))
        self.assertEqual(
            [e for e, _ in self.ha.published], [SENSOR_CHARGE, SENSOR_CHARGE_LOW]
        )

    def test_a_failing_low_write_still_turns_the_high_flag_off(self):
        # Lå de to i ét forsøg, nåede det høje flag aldrig at blive slukket.
        self.ha.fail_on = SENSOR_CHARGE_LOW
        d = decision("no_dear")
        asyncio.run(self.app._publish_charge(self.ha, d, self.command))
        self.assertEqual(dict(self.ha.published)[SENSOR_CHARGE], "off")

    def test_no_flag_is_raised_while_the_other_cannot_be_lowered(self):
        # Kan det lave ikke slukkes, må det høje ikke tændes - ellers står
        # begge tændt i HA.
        self.ha.fail_on = SENSOR_CHARGE_LOW
        d = replace(decision("charge_now"), charge=True, charge_temp=56.0)
        asyncio.run(self.app._publish_charge(self.ha, d, self.command))
        self.assertNotIn(SENSOR_CHARGE, dict(self.ha.published))

    def test_the_running_block_decides_the_flag(self):
        # En lav blok kører; planlæggeren ville i dette minut hellere have den
        # høje. Flaget følger blokken, ikke ønsket.
        self.app.charge_plan.block = SimpleNamespace(low=True, temp=46.5, manual=False)
        self.assertEqual(self.app._charge_temp(decision("charge_now")), 46.5)
        self.app.charge_plan.block = SimpleNamespace(low=False, temp=None, manual=False)
        self.assertEqual(self.app._charge_temp(decision("charge_now", low=True)), 56)


class LowBlockThroughACycleTest(unittest.TestCase):
    """Hele vejen: en lav blok kører, og kun det lave flag er tændt."""

    def test_a_running_low_block_raises_the_low_flag(self):
        app = Varmeopt(options(), Store(Path(tempfile.mkdtemp(prefix="varmeopt-test-"))))
        app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "f"),
                COP: State(COP, "4.4", {}, "m"),
                OUT: State(OUT, "17.2", {}, "u"),
            }
        )
        now = time.time()
        app.charge_plan.block = Block(
            dear_from=now + 3600,
            dear_until=now + 7200,
            starts_at=now - 60,
            ends_at=now + 1800,
            kwh=6.0,
            deadline=now + 3600,
            low=True,
            temp=46.5,
        )

        # Rumvarmens COP og ladetemperaturens, så prognosen kan skilles ad.
        app._cop_at = lambda m: 4.09
        app._charge_cop_at = lambda m: 3.45

        asyncio.run(app.cycle(ha))

        # Prognosen til Predbat: rumvarmens COP i blokkens halvtimer, ikke 56
        # graders - ellers ~18 % for meget strøm.
        window = app._charge_window()
        self.assertEqual(app._vp_inputs.charge_cop_at(window[0]), 4.09)
        self.assertEqual(app._vp_inputs.charge_cop_at(window[1] + 30), 3.45)

        published = dict(ha.published)
        self.assertEqual(published[SENSOR_CHARGE_LOW], "on")
        self.assertEqual(published[SENSOR_CHARGE], "off")
        self.assertEqual(ha.attributes[SENSOR_CHARGE_LOW]["mål_temp"], 46.5)
        self.assertTrue(app.status["decision"].low)


if __name__ == "__main__":
    unittest.main()
