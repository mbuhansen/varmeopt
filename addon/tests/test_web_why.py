"""Hvorfor-kolonnen i planen.

Den siger hvad der gøres og hvorfor, ikke hvad det koster — tallene står i
detaljerne ovenfor. Reglerne er små, men de er dem brugeren læser tabellen
igennem, og de har været forkerte tre gange.
"""

import unittest
from dataclasses import dataclass

from varmeopt.web import _charge_because, _highlight_basis, _now_note, _switch_note


@dataclass
class FakeRow:
    reason: str = "net: import"
    power: str = "net"
    source: str = "varmepumpe"


@dataclass
class FakeDecision:
    charge: bool = False
    charge_kwh: float | None = None
    saving_kr: float | None = None


class BasisTest(unittest.TestCase):
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
            FakeDecision(charge=True),
            FakeRow(power="batteri", reason="eksport: mistet indtjening"),
        )
        against_price = _now_note(FakeDecision(charge=True), FakeRow(power="net"))

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
        note = _switch_note(FakeRow(power="net", source="pillefyr"))

        self.assertIn("pillefyr", note)
        self.assertNotIn(">", note)
        self.assertNotIn("0.", note)

    def test_the_three_reasons_are_told_apart(self):
        # Eksport er ikke en kilde men en grund - stroemmen kommer fra
        # batteriet eller solen - saa den laeses af begrundelsen.
        notes = {
            "net": _switch_note(FakeRow(power="net", source="pillefyr")),
            "eksport": _switch_note(
                FakeRow(
                    power="batteri",
                    reason="eksport: mistet indtjening",
                    source="pillefyr",
                )
            ),
            "batteri": _switch_note(
                FakeRow(power="batteri", reason="batteri: frit", source="pillefyr")
            ),
        }

        self.assertEqual(len(set(notes.values())), 3)
        self.assertIn("for dyr", notes["net"])
        self.assertIn("sælges", notes["eksport"])
        self.assertIn("batteriet", notes["batteri"])

    def test_switching_back_says_the_pump_took_over(self):
        note = _switch_note(FakeRow(power="net", source="varmepumpe"))

        self.assertIn("varmepumpen", note)


class ChargeBecauseTest(unittest.TestCase):
    def test_without_a_target_it_does_not_invent_one(self):
        self.assertIn("dyrere varme", _charge_because(None))



class PlanTableTest(unittest.TestCase):
    """Tabellen viser Predbats egen raekke ved siden af vores pris.

    Uden ladetilstanden og tilstandsordet kan man ikke se *hvorfor* kilden er
    som den er - at der staar hold charge ved 16 % - uden at gaa over i
    Predbats egen tabel og finde den samme halvtime.
    """

    def html(self, rows):
        import asyncio

        from varmeopt.web import WebUI

        ui = WebUI(lambda: {"projection": rows, "decision": None}, lambda: None)
        return asyncio.run(ui.plan(None)).text

    def row(self, **over):
        from varmeopt.planner import Projection

        values = dict(
            minutes=0,
            electricity=1.85,
            reason="net: afladning er slaaet fra",
            power="net",
            import_price=1.85,
            export_price=1.09,
            heat_price=0.58,
            state="holdchrg",
            soc_percent=16.0,
        )
        values.update(over)
        return Projection(**values)

    def test_the_plan_shows_predbats_state_and_the_charge_level(self):
        html = self.html([self.row()])

        self.assertIn("<th>SOC</th>", html)
        self.assertIn("<th>Predbat</th>", html)
        self.assertIn("holdchrg", html)
        self.assertIn("16 %", html)

    def test_a_charging_slot_just_says_lad_op(self):
        # Kort, som Predbats egen plan. Hvorfor der lades, staar paa
        # maalraekken; her staar bare at der bliver ladet.
        html = self.html([self.row(charging=True)])

        self.assertIn("lad op", html)

    def test_it_says_lad_op_even_on_the_row_we_stand_in(self):
        html = self.html([self.row(charging=True, minutes=0)])

        self.assertIn("lad op", html)
        self.assertNotIn("afventer", html)

    def test_the_why_column_names_the_source(self):
        # "net" saettes i roedt af _highlight_basis, saa ordet staar i sit
        # eget element - men det staar der, og det kommer fra kildefeltet.
        html = self.html([self.row()])

        self.assertIn(">net</span> ·", html)

    def test_a_battery_row_that_must_be_bought_back_says_both(self):
        # Predbat staar paa demand og inverteren leverer, saa stroemmen
        # kommer fra batteriet - det er hvad raekken skal sige. At den saa
        # koster importprisen i bunden, er en anden oplysning, og den hoerer
        # ogsaa til: uden den ser 2,23 kr ud som en fejl.
        html = self.html([
            self.row(),
            self.row(
                minutes=30,
                power="batteri",
                reason="koebes tilbage",
                state="demand",
                soc_percent=30.0,
            ),
        ])

        self.assertIn("batteri · koebes tilbage", html)

    def test_a_row_where_the_two_agree_says_the_word_once(self):
        html = self.html([
            self.row(),
            self.row(minutes=30, power="batteri", reason="batteri"),
        ])

        self.assertNotIn("batteri · batteri", html)

    def test_a_row_without_a_plan_state_leaves_a_dash(self):
        html = self.html([self.row(state="", soc_percent=None)])

        self.assertIn('<td class="raw">—</td><td class="raw">—</td>', html)


class UsagePageTest(unittest.TestCase):
    """Forbrugsfanen: hvad der er målt, og hvad kurven har lært."""

    def html(self, model, status=None):
        import asyncio

        from varmeopt.web import WebUI

        ui = WebUI(
            lambda: status or {},
            lambda: None,
            house_load=lambda: model,
        )
        return asyncio.run(ui.usage(None)).text

    def model(self):
        from varmeopt.houseload import HouseLoad

        load = HouseLoad()
        start = 1_757_000_000.0
        for step in range(8):
            load.history.append(
                (start + step * 1800.0, 2.0 + step * 0.1, 5.0, step == 3)
            )
        for outdoor, kw in ((-2, 6.0), (2, 5.0), (6, 4.0), (10, 3.0)):
            load.curve.learn(float(outdoor), kw)
        return load

    def test_both_charts_are_drawn(self):
        html = self.html(self.model(), {"house_load_kw": 2.4})

        self.assertIn("Målt over tid", html)
        self.assertIn("Mod udetemperatur", html)
        self.assertEqual(html.count("<svg"), 2)

    def test_a_modelled_window_is_drawn_open(self):
        # Et vindue hvor spaen koerte, skal kunne kendes fra et der er maalt
        # rent - ellers ser et skoen ud som en maaling.
        html = self.html(self.model())

        self.assertIn('fill="none" stroke="', html)

    def test_it_says_so_when_there_is_nothing_to_draw_yet(self):
        from varmeopt.houseload import HouseLoad

        html = self.html(HouseLoad())

        self.assertIn("For få målinger endnu", html)
        self.assertIn("For få punkter", html)


class BalanceCardTest(unittest.TestCase):
    """Effektbalancen siger hvor husets tal kommer fra."""

    def section(self, **over):
        from varmeopt.demand import Balance, Load
        from varmeopt.web import _balance_section

        load = Load(**over.pop("load", {}))
        status = over.pop("status", {})
        return _balance_section(Balance(load=load, **over), None, status)

    def test_the_meter_is_named_when_it_answers(self):
        html = self.section(load={"flow": 45.0, "ret": 30.0, "litres_per_hour": 300.0})

        self.assertIn("flowmåleren", html)

    def test_the_store_is_named_when_it_stands_in(self):
        # Under maalerens bund traeder lageret til, og det skal kunne ses -
        # de to er ikke lige sikre.
        html = self.section(
            load={"litres_per_hour": 0.0, "fallback_kw": 2.4},
            status={"house_load_kw": 2.4, "house_load": "maalt 2,40 kW over 30 min"},
        )

        self.assertIn("lagerets energiændring", html)
        self.assertIn("2.40 kW", html)

    def test_the_bias_against_the_meter_is_shown_when_it_is_known(self):
        html = self.section(status={"house_load_bias": 0.18})

        self.assertIn("+0.18 kW", html)


class VesselCardTest(unittest.TestCase):
    """Brugsvand og spa: begge beholdere siger om de varmer lige nu."""

    FULL = {
        "vvb_top": 55.4, "vvb_bottom": 48.1, "spa_temp": 37.2,
        "spa_target": 38.0, "dhw_active": True, "spa_heating": False,
    }

    def labels(self, status):
        import re
        from varmeopt.web import _vessel_section
        return [k for k, _ in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>",
                                         _vessel_section(status))]

    def value(self, status, label):
        import re
        from varmeopt.web import _vessel_section
        pairs = dict(re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>",
                                _vessel_section(status)))
        return re.sub("<[^>]+>", "", pairs[label])

    def test_both_vessels_say_whether_they_are_heating(self):
        self.assertEqual(self.value(self.FULL, "VVB varmer"), "ja")
        self.assertEqual(self.value(self.FULL, "Spa varmer"), "nej")

    def test_the_flag_sits_with_the_vessel_it_belongs_to(self):
        # Ikke nederst i en samlet klump - man laeser beholderen, ikke listen.
        labels = self.labels(self.FULL)

        self.assertEqual(labels.index("VVB varmer"), labels.index("VVB bund") + 1)
        self.assertEqual(labels.index("Spa varmer"), labels.index("Spa mål") + 1)

    def test_a_vessel_that_does_not_answer_gets_no_row(self):
        without = dict(self.FULL)
        del without["dhw_active"]

        self.assertNotIn("VVB varmer", self.labels(without))
        self.assertIn("Spa varmer", self.labels(without))

    def test_a_flag_alone_is_not_worth_a_card(self):
        # Foer talte flagene med i tomhedstjekket, saa et enkelt spa-flag
        # kunne holde et ellers tomt kort i live.
        from varmeopt.web import _vessel_section

        self.assertEqual(_vessel_section({"spa_heating": True}), "")
        self.assertEqual(_vessel_section({"dhw_active": False}), "")


class ChargeCardTest(unittest.TestCase):
    """Kortet paa lagersiden: hvorfor den vil lade op."""

    def card(self, **over):
        from varmeopt.planner import Decision
        from varmeopt.web import _charge_card

        fields = dict(
            source="varmepumpe",
            heat_price=0.42,
            pellet_price=0.706,
            charge=True,
            charge_kwh=21.3,
            saving_kr=12.40,
            window_starts_in=210,
            window_minutes=300,
            charge_state="lader 21,3 kWh op nu",
            dhw_need_kwh=6.0,
            dhw_have_kwh=0.0,
            dhw_short_kwh=21.3,
            space_need_kwh=5.2,
            space_have_kwh=13.3,
            space_short_kwh=0.0,
        )
        fields.update(over)
        return _charge_card({"decision": Decision(**fields)}, dhw_temp=55.0)

    def test_it_says_what_the_charge_is_for(self):
        card = self.card()

        self.assertIn("Varmt vand og spa", card)
        self.assertIn("skal bruge 6.0 kWh", card)
        self.assertIn("lageret har 0.0 over 55°", card)
        self.assertIn("mangler 21.3", card)

    def test_what_is_covered_says_so_instead_of_a_number(self):
        # Rumvarmen er daekket: 5,2 kWh mod 13,3 i lageret.
        self.assertIn("dækket", self.card())

    def test_the_window_is_a_clock_not_a_countdown(self):
        # «om 210 min» skal regnes; «kl. 17:30» kan laeses.
        card = self.card()

        self.assertIn("Strømmen bliver dyr", card)
        self.assertIn("fra kl. ", card)
        self.assertNotIn("210 min", card)

    def test_a_deadline_on_the_clock_does_not_claim_the_price_rises(self):
        # Fristen kan komme fra uret i stedet for fra prisraekken. Saa er
        # "stroemmen bliver dyr kl. 17" en paastand ingen har efterproevet -
        # tidspunktet kommer fra at der bades om aftenen.
        card = self.card(deadline_on_the_clock=True)

        self.assertIn("Lageret skal være fyldt", card)
        self.assertNotIn("Strømmen bliver dyr", card)
        self.assertIn("dyrest kl. ", card)

    def test_it_also_answers_when_it_does_not_charge(self):
        card = self.card(
            charge=False,
            charge_kwh=None,
            saving_kr=None,
            charge_state="lageret rækker - der er ikke noget at lade op til",
            dhw_have_kwh=9.4,
            dhw_short_kwh=0.0,
        )

        self.assertIn("lageret rækker", card)
        self.assertNotIn("Værd at hente", card)

    def test_without_an_answer_there_is_no_card(self):
        # Foer planlaeggeren har svaret én gang, er der ingenting at vise -
        # og et tomt kort er vaerre end intet kort.
        self.assertEqual(self.card(charge_state=""), "")


if __name__ == "__main__":
    unittest.main()
