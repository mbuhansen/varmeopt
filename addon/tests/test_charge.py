"""Opladningen som en blok.

Den 6. september tændte og slukkede flaget fem gange på en eftermiddag, fordi
beslutningen blev taget forfra hvert minut og `need` vippede omkring nul. En
kompressor må ikke behandles sådan — og rettelsen er ikke hysterese, men at
stille spørgsmålet én gang: find de billigste halvtimer inden prisen stiger,
læg dem fast, kør dem.
"""

import unittest
from dataclasses import dataclass

from varmeopt.charge import ChargePlan
from varmeopt.prices import Plan


@dataclass
class FakeDecision:
    planned_kwh: float | None = 12.0
    window_starts_in: int | None = 240
    window_minutes: int | None = 300
    source: str = "varmepumpe"


def plan(*rates):
    """En plan hvor batteriet er bundet, saa importprisen gaelder direkte."""
    rows = [
        {"state": "holdchrg", "import_rate": rate, "export_rate": 50} for rate in rates
    ]
    return Plan.from_predbat({"raw": {"rows": rows}}, battery_average=1.0)


# Dyrt nu, billigt i slot 4-7, dyrt igen fra slot 8.
RATES = (90, 90, 90, 90, 35, 35, 35, 35, 155, 155, 155, 155)


class BlockTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(*RATES)
        self.charge = ChargePlan()

    def step(self, at=None, decision=None, rate=16.0, full=False):
        return self.charge.update(
            at if at is not None else self.now,
            decision if decision is not None else FakeDecision(),
            self.plan,
            rate,
            full=full,
        )

    def test_it_waits_for_the_cheapest_window(self):
        # De billige halvtimer begynder 120 minutter frem.
        self.assertFalse(self.step())

        starts, ends = self.charge.slots()
        self.assertAlmostEqual((starts - self.now) / 60, 120, delta=1)
        self.assertAlmostEqual((ends - starts) / 60, 45, delta=1)

    def test_and_then_it_charges_without_flapping(self):
        # Kernen. Behovet vipper omkring nul minut for minut, praecis som den
        # 6. september - og flaget skal alligevel taende én gang og blive
        # taendt blokken ud.
        #
        # Tiden gaar rigtigt: planen skydes frem som halvtimerne gaar, og
        # fristen taeller ned. Ellers ville blokken skubbe sig selv foran sig.
        dear_at = 8 * 30  # de dyre halvtimer begynder her
        flags = []
        for minute in range(dear_at):
            want = 12.0 if minute % 2 == 0 else None
            slot = minute // 30
            flags.append(
                self.charge.update(
                    self.now + minute * 60,
                    FakeDecision(
                        planned_kwh=want,
                        window_starts_in=dear_at - minute,
                        window_minutes=dear_at - minute,
                    ),
                    plan(*RATES[slot:]),
                    16.0,
                )
            )

        starts = sum(1 for a, b in zip(flags, flags[1:]) if b and not a)
        self.assertEqual(starts, 1, f"flaget taendte {starts} gange")
        self.assertEqual(sum(flags), 45, "blokken skal vare 45 minutter")

    def test_a_slower_pump_gets_a_longer_block(self):
        # 12 kWh er 45 minutter ved 16 kW og 60 ved 12.
        self.step(rate=16.0)
        fast = self.charge.slots()

        self.charge = ChargePlan()
        self.step(rate=12.0)
        slow = self.charge.slots()

        self.assertAlmostEqual((fast[1] - fast[0]) / 60, 45, delta=1)
        self.assertAlmostEqual((slow[1] - slow[0]) / 60, 60, delta=1)

    def test_it_moves_while_it_waits(self):
        # En blok der ikke er begyndt, er ikke et loefte: bliver en anden
        # halvtime billigere, flytter den sig.
        self.step()
        first = self.charge.slots()[0]

        self.plan = plan(90, 20, 20, 90, 35, 35, 35, 35, 155, 155, 155, 155)
        self.step()

        self.assertLess(self.charge.slots()[0], first)


class InterruptionTest(unittest.TestCase):
    """Kun to ting maa bryde en igangvaerende blok."""

    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(35, 35, 35, 155, 155, 155)
        self.charge = ChargePlan()

    def start(self):
        self.assertTrue(
            self.charge.update(
                self.now, FakeDecision(window_starts_in=90, window_minutes=90),
                self.plan, 16.0,
            )
        )

    def test_a_full_store_stops_it(self):
        self.start()

        self.assertFalse(
            self.charge.update(
                self.now + 300, FakeDecision(), self.plan, 16.0, full=True
            )
        )
        self.assertIn("fuldt", self.charge.note)

    def test_the_pellet_boiler_winning_stops_it(self):
        self.start()

        self.assertFalse(
            self.charge.update(
                self.now + 300, FakeDecision(source="pillefyr"), self.plan, 16.0
            )
        )
        self.assertIn("pillefyret", self.charge.note)

    def test_nothing_else_does(self):
        # Behovet forsvinder midt i blokken. Den koerer alligevel faerdig.
        self.start()

        self.assertTrue(
            self.charge.update(
                self.now + 300, FakeDecision(planned_kwh=None), self.plan, 16.0
            )
        )


class OnceTest(unittest.TestCase):
    """Ét billigt vindue giver én opladning."""

    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(35, 35, 155, 155, 155, 155)
        self.charge = ChargePlan()
        self.decision = FakeDecision(planned_kwh=8.0, window_starts_in=60, window_minutes=60)

    def at(self, minute):
        """Beslutningen som den ser ud det minut - fristen taeller ned."""
        return FakeDecision(
            planned_kwh=8.0,
            window_starts_in=max(1, 60 - minute),
            window_minutes=max(1, 60 - minute),
        )

    def run_block(self):
        for minute in (0, 30, 45, 60):
            self.charge.update(
                self.now + minute * 60, self.at(minute), self.plan, 16.0
            )

    def test_it_does_not_charge_twice_against_the_same_top(self):
        self.run_block()

        self.assertFalse(
            self.charge.update(self.now + 61 * 60, self.at(61), self.plan, 16.0)
        )
        self.assertIn("allerede ladet op", self.charge.note)
        self.assertIsNone(self.charge.slots())

    def test_but_a_new_top_gets_its_own_block(self):
        self.run_block()

        # Fire timer senere er der en ny pristop forude.
        later = self.now + 4 * 3600
        self.charge.update(later, self.at(0), self.plan, 16.0)

        self.assertIsNotNone(self.charge.slots())


class StorageTest(unittest.TestCase):
    def test_a_running_block_survives_a_restart(self):
        # En genstart midt i en opladning maa ikke starte kompressoren forfra
        # paa den anden side.
        now = 1_757_000_000.0
        charge = ChargePlan()
        charge.update(
            now, FakeDecision(window_starts_in=90, window_minutes=90), plan(35, 35, 155, 155), 16.0
        )

        back = ChargePlan.from_raw(charge.to_raw())

        self.assertEqual(back.slots(), charge.slots())

    def test_garbage_gives_an_empty_plan(self):
        for junk in (None, "aeh", {}, {"block": "nej"}, {"block": {"kwh": -1}}):
            self.assertIsNone(ChargePlan.from_raw(junk).slots())


if __name__ == "__main__":
    unittest.main()
