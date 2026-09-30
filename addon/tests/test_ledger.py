"""Regnskabet: hvad opladningen kostede, mod hvad varmen ellers havde kostet."""

import unittest
from dataclasses import dataclass

from varmeopt.ledger import Ledger
from varmeopt.web import _ledger_body

NOW = 1_757_000_400.0


@dataclass
class FakeBlock:
    began: float
    dear_from: float
    dear_until: float
    manual: bool = False
    top_up: bool = False


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        # Opladningen kl. 0-30 min, strækket den er lagt imod kl. 300-360.
        self.block = FakeBlock(
            began=NOW, dear_from=NOW + 300 * 60, dear_until=NOW + 360 * 60
        )

    def step(self, minute, block=None, price=0.5, el=4.0, heat=16.0, hp=0.9,
             pellet=0.7, stretch=None, expected=1.5):
        self.ledger.observe(
            NOW + minute * 60,
            block,
            stretch=stretch,
            expected_kr=expected,
            price_kr=price,
            el_kw=el,
            heat_kw=heat,
            hp_heat_price=hp,
            pellet_price=pellet,
            wear=0.15,
        )

    def run_charge(self):
        for minute in range(0, 31):
            self.step(minute, block=self.block)
        # Blokken er slut; pumpen står stille resten af vejen.
        for minute in range(31, 400):
            self.step(minute, el=0.0, heat=0.0)

    def test_what_the_charge_cost(self):
        self.run_charge()
        entry = self.ledger.entries[0]

        # 30 minutter à 4 kW el og 16 kW varme til 0,50 kr/kWh.
        self.assertAlmostEqual(entry.el_kwh, 2.0, places=2)
        self.assertAlmostEqual(entry.heat_kwh, 8.0, places=2)
        self.assertAlmostEqual(entry.el_kr, 1.0, places=2)
        # Plus slitagen: 8 kWh à 0,15.
        self.assertAlmostEqual(entry.paid_per_kwh, 2.2 / 8, places=3)
        self.assertAlmostEqual(entry.cop, 4.0, places=2)

    def test_the_alternative_is_the_cheapest_heat_in_the_stretch(self):
        # Varmepumpen koster 0,90 i strækket, pillefyret 0,70.
        self.run_charge()
        entry = self.ledger.entries[0]

        self.assertAlmostEqual(entry.alt_per_kwh, 0.7, places=3)
        self.assertAlmostEqual(entry.saving_kr, 8 * (0.7 - 2.2 / 8), places=2)
        self.assertTrue(entry.settled(NOW + 400 * 60))

    def test_a_stretch_not_yet_over_is_not_counted(self):
        for minute in range(0, 31):
            self.step(minute, block=self.block)
        for minute in range(31, 320):
            self.step(minute, el=0.0, heat=0.0)

        at = NOW + 320 * 60
        self.assertFalse(self.ledger.entries[0].settled(at))
        self.assertEqual(self.ledger.totals(at)["count"], 0)

    def test_a_charge_that_did_not_pay_shows_a_loss(self):
        # Strømmen var dyr, og strækket blev billigere end ventet.
        for minute in range(0, 31):
            self.step(minute, block=self.block, price=2.0)
        for minute in range(31, 400):
            self.step(minute, el=0.0, heat=0.0, hp=0.3)

        self.assertLess(self.ledger.entries[0].saving_kr, 0)

    def test_a_manual_charge_borrows_the_plans_stretch_and_promises_nothing(self):
        manual = FakeBlock(began=NOW, dear_from=NOW, dear_until=NOW + 1800, manual=True)
        stretch = (NOW + 300 * 60, NOW + 360 * 60)
        for minute in range(0, 31):
            self.step(minute, block=manual, stretch=stretch)

        entry = self.ledger.entries[0]
        self.assertEqual(entry.stretch_from, stretch[0])
        self.assertIsNone(entry.expected_kr)

    def test_a_gap_is_not_counted(self):
        self.step(0, block=self.block)
        self.step(1, block=self.block)
        # Add-on'en var nede i ti minutter.
        self.step(11, block=self.block)

        self.assertAlmostEqual(self.ledger.entries[0].el_kwh, 4.0 / 60, places=3)

    def test_two_blocks_are_two_entries(self):
        for minute in range(0, 31):
            self.step(minute, block=self.block)
        self.step(31)
        second = FakeBlock(
            began=NOW + 40 * 60, dear_from=self.block.dear_from,
            dear_until=self.block.dear_until, top_up=True,
        )
        for minute in range(40, 60):
            self.step(minute, block=second)

        self.assertEqual(len(self.ledger.entries), 2)
        self.assertTrue(self.ledger.entries[1].top_up)
        self.assertIsNotNone(self.ledger.entries[0].ended)

    def test_it_survives_a_restart(self):
        for minute in range(0, 15):
            self.step(minute, block=self.block)
        back = Ledger.from_raw(self.ledger.to_raw())

        self.assertEqual(len(back.entries), 1)
        self.assertIsNone(back.entries[0].ended, "blokken kører stadig")
        self.assertAlmostEqual(back.entries[0].el_kwh, self.ledger.entries[0].el_kwh, 3)

    def test_the_page_shows_the_entry(self):
        self.run_charge()
        html = _ledger_body(self.ledger, NOW + 400 * 60)

        self.assertIn("Regnskab", html)
        self.assertIn("afregnet", html)
        self.assertIn(f"{self.ledger.entries[0].saving_kr:.2f} kr", html)

    def test_an_empty_ledger_says_so(self):
        self.assertIn("Ingen opladninger", _ledger_body(Ledger(), NOW))
