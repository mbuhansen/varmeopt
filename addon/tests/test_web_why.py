"""Hvorfor-kolonnen i planen.

Den siger hvad der gøres og hvorfor, ikke hvad det koster — tallene står i
detaljerne ovenfor. Reglerne er små, men de er dem brugeren læser tabellen
igennem, og de har været forkerte tre gange.
"""

import unittest
from dataclasses import dataclass

from varmeopt.web import _basis, _charge_because, _highlight_basis, _now_note, _switch_note


@dataclass
class FakeRow:
    reason: str = "net: import"
    source: str = "varmepumpe"


@dataclass
class FakeDecision:
    charge: bool = False
    charge_kwh: float | None = None
    saving_kr: float | None = None


class BasisTest(unittest.TestCase):
    def test_the_basis_is_the_word_before_the_colon(self):
        self.assertEqual(_basis("net: batteriet er bundet"), "net")
        self.assertEqual(_basis("eksport: mistet indtjening"), "eksport")
        self.assertEqual(_basis("batteri: frit"), "batteri")

    def test_net_is_set_in_red(self):
        # Den dyre vej: hverken batteri eller sol daekker, og hver kWh koebes
        # til fuld importpris. Det skal kunne ses paa een gang.
        self.assertIn("#c0392b", _highlight_basis("net · lader op"))

    def test_the_others_are_not(self):
        for text in ("batteri", "eksport · dyreste time", "sol"):
            with self.subTest(text=text):
                self.assertNotIn("#c0392b", _highlight_basis(text))

    def test_the_text_is_escaped(self):
        self.assertNotIn("<b>", _highlight_basis("net <b>x</b>"))


class NowNoteTest(unittest.TestCase):
    def test_it_says_when_nothing_is_being_done(self):
        note = _now_note(FakeDecision(charge=False), None)

        self.assertIn("ingen grund", note)

    def test_charging_says_why_the_dear_hour_is_dear(self):
        # De to grunde foerer til samme handling, men er ikke samme historie.
        against_export = _now_note(
            FakeDecision(charge=True), FakeRow(reason="eksport: mistet indtjening")
        )
        against_price = _now_note(
            FakeDecision(charge=True), FakeRow(reason="net: import")
        )

        self.assertIn("eksport", against_export)
        self.assertIn("strøm", against_price)
        self.assertNotEqual(against_export, against_price)

    def test_no_price_appears_in_the_column(self):
        note = _now_note(FakeDecision(charge=True, charge_kwh=13.0, saving_kr=1.45), None)

        self.assertNotIn("13", note)
        self.assertNotIn("1.45", note)
        self.assertNotIn("kr", note)


class SwitchNoteTest(unittest.TestCase):
    def test_switching_to_pellets_says_so_not_the_two_prices(self):
        # Her stod "VP 0.84 > pille 0.71" - de tal hoerer i varmekolonnen ved
        # siden af, ikke i en kolonne der skal skimmes.
        note = _switch_note(FakeRow(reason="net: import", source="pillefyr"))

        self.assertIn("pillefyr", note)
        self.assertNotIn(">", note)
        self.assertNotIn("0.", note)

    def test_the_three_reasons_are_told_apart(self):
        notes = {
            b: _switch_note(FakeRow(reason=f"{b}: noget", source="pillefyr"))
            for b in ("net", "eksport", "batteri")
        }

        self.assertEqual(len(set(notes.values())), 3)
        self.assertIn("for dyr", notes["net"])
        self.assertIn("sælges", notes["eksport"])
        self.assertIn("batteriet", notes["batteri"])

    def test_switching_back_says_the_pump_took_over(self):
        note = _switch_note(FakeRow(reason="net: import", source="varmepumpe"))

        self.assertIn("varmepumpen", note)


class ChargeBecauseTest(unittest.TestCase):
    def test_without_a_target_it_does_not_invent_one(self):
        self.assertIn("dyrere varme", _charge_because(None))


if __name__ == "__main__":
    unittest.main()
