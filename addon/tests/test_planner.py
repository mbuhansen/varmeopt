import unittest

from varmeopt.planner import Decision, Planner, source_now
from varmeopt.prices import Plan

PELLET = 0.706


def plan(*rates, battery_average=1.0):
    """En plan hvor batteriet er bundet, saa importprisen gaelder direkte."""
    rows = [
        {"state": "holdchrg", "import_rate": rate, "export_rate": 50} for rate in rates
    ]
    return Plan.from_predbat({"raw": {"rows": rows}}, battery_average=battery_average)


def planner(**over):
    values = dict(
        pellet_price=PELLET,
        hysteresis=0.05,
        wear_kr_per_kwh=0.15,
        min_charge_kwh=4.0,
        charge_kw=16.0,
    )
    values.update(over)
    return Planner(**values)


class SourceTest(unittest.TestCase):
    def test_the_heat_pump_wins_when_it_is_cheaper(self):
        source, why = source_now(0.30, PELLET, 0.05)

        self.assertEqual(source, "varmepumpe")
        self.assertIn("<", why)

    def test_the_boiler_wins_when_the_pump_is_dearer(self):
        source, _ = source_now(1.20, PELLET, 0.05)

        self.assertEqual(source, "pillefyr")

    def test_a_close_race_goes_to_the_heat_pump(self):
        source, why = source_now(PELLET + 0.02, PELLET, 0.05)

        self.assertEqual(source, "varmepumpe")
        self.assertIn("tæt", why)

    def test_without_a_cop_we_assume_the_heat_pump(self):
        source, why = source_now(None, PELLET, 0.05)

        self.assertEqual(source, "varmepumpe")
        self.assertIn("ingen COP", why)


class CheapestHeatTest(unittest.TestCase):
    def test_the_boiler_caps_the_price(self):
        # Uanset hvor dyr stroemmen bliver, betaler man aldrig mere end pille.
        self.assertAlmostEqual(planner().cheapest_heat(20.0, 4.0), PELLET, places=9)

    def test_the_pump_wins_when_it_is_cheaper(self):
        # 1,20/4 = 0,30 i stroem, plus 0,15 i slitage. Varmepumpevarme
        # koster mere end elprisen alene siger.
        self.assertAlmostEqual(planner().cheapest_heat(1.20, 4.0), 0.45, places=9)

    def test_wear_belongs_to_the_pump_and_not_to_the_boiler(self):
        # Pillefyret baerer ikke tallet - der er braendslet og
        # virkningsgraden hele historien.
        self.assertAlmostEqual(planner().cheapest_heat(20.0, 4.0), PELLET, places=9)

    def test_wear_can_decide_the_source(self):
        # 2,50/4 = 0,625 i ren stroem: klart under pillefyrets 0,706, og saa
        # havde varmepumpen vundet. Med slitagen er varmen 0,775, og saa er
        # pillefyret billigst. Det er hele pointen i at flytte tallet - de
        # to regnestykker gav foer to forskellige svar.
        p = planner()

        self.assertLess(p.heat_price(2.50, 4.0) - 0.15, PELLET - p.hysteresis)
        self.assertGreater(p.heat_price(2.50, 4.0), PELLET + p.hysteresis)

        self.assertEqual(p.decide(plan(250), cop_now=4.0).source, "pillefyr")

    def test_no_cop_falls_back_to_the_boiler(self):
        self.assertAlmostEqual(planner().cheapest_heat(1.20, None), PELLET, places=9)


class DecideTest(unittest.TestCase):
    def test_no_plan_still_gives_a_source(self):
        # Predbat kan vaere nede. Styringen skal stadig kunne vaelge.
        decision = planner().decide(plan=None, cop_now=4.0)

        self.assertEqual(decision.source, "varmepumpe")
        self.assertFalse(decision.charge)

    def test_flat_prices_give_nothing_to_gain(self):
        decision = planner().decide(plan(100, 100, 100), cop_now=4.0, headroom_kwh=20)

        self.assertFalse(decision.charge)
        self.assertIn("intet at hente", decision.reason)

    def test_a_margin_inside_the_noise_is_not_worth_moving_heat_for(self):
        # 1,00 -> 1,16 kr/kWh stroem ved COP 4 er 0,04 kr/kWh varme. Det er
        # under hysteresen: de to halvtimer er ikke til at skelne med de tal
        # vi har, og saa saettes 20 kWh ikke i bevaegelse paa forskellen.
        decision = planner().decide(plan(100, 116), cop_now=4.0, headroom_kwh=20)

        self.assertFalse(decision.charge)
        self.assertIn("for tæt", decision.reason)

    def test_a_margin_above_the_noise_still_charges(self):
        # 0,06 kr/kWh varme er over snittet, og saa lades der.
        decision = planner().decide(plan(100, 125), cop_now=4.0, headroom_kwh=20)

        self.assertTrue(decision.charge)

    def test_a_dearer_hour_ahead_is_worth_charging_for(self):
        # 0,40 kr nu mod 2,40 senere ved COP 4: 0,10 mod 0,60 pr. kWh varme.
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=20)

        self.assertTrue(decision.charge)
        self.assertEqual(decision.window_minutes, 30)
        self.assertGreater(decision.saving_kr, 0)

    def test_the_boiler_caps_what_is_worth_avoiding(self):
        # To absurde elpriser senere. Begge ligger over pillevarmen, saa begge
        # klemmes til den - og saa er der praecis lige meget at spare.
        høj = planner().decide(plan(40, 4000), cop_now=4.0, headroom_kwh=20)
        højere = planner().decide(plan(40, 8000), cop_now=4.0, headroom_kwh=20)

        self.assertAlmostEqual(høj.saving_kr, højere.saving_kr, places=9)

    def test_below_the_cap_a_dearer_hour_is_worth_more(self):
        # Under loftet slaar prisen stadig igennem. Loftet ligger nu ved
        # 0,706 - 0,15 = 0,556 kr/kWh varme, altsaa 2,22 kr/kWh stroem ved
        # COP 4; begge raekker her er under.
        mild = planner().decide(plan(40, 180), cop_now=4.0, headroom_kwh=20)
        værre = planner().decide(plan(40, 210), cop_now=4.0, headroom_kwh=20)

        self.assertGreater(værre.saving_kr, mild.saving_kr)

    def test_moving_pump_heat_in_time_costs_no_extra_wear(self):
        # 0,40 -> 0,80 kr/kWh stroem ved COP 4. Slitagen er den samme om
        # pumpen koerer nu eller om en halv time - den samme kWh gaar
        # igennem den samme maskine - saa de 0,10 kr er en aegte gevinst.
        #
        # Foer blev slitagen trukket fra her *og* talt i varmeprisen, og saa
        # blev det til -0,05 og ingen opladning.
        decision = planner(wear_kr_per_kwh=0.15).decide(
            plan(40, 80), cop_now=4.0, headroom_kwh=20
        )

        self.assertTrue(decision.charge)

    def test_but_displacing_pellet_heat_does_pay_the_wear(self):
        # Her er den senere varme pillefyrets, og saa staar slitagen
        # tilbage i marginen: 0,706 - (0,40/4 + 0,15) = 0,456, ikke 0,606.
        decision = planner(wear_kr_per_kwh=0.15).decide(
            plan(40, 300), cop_now=4.0, cop_later=4.0, headroom_kwh=20, demand_kw=None
        )

        self.assertTrue(decision.charge)
        self.assertAlmostEqual(decision.saving_kr / decision.charge_kwh, 0.456, places=6)

    def test_solar_gets_its_share_first(self):
        # 20 kWh plads, men solen venter med 18. Saa er der 2 tilbage, og det
        # er under minimumstraekket.
        decision = planner().decide(
            plan(40, 240), cop_now=4.0, headroom_kwh=20, solar_expected_kwh=18.0
        )

        self.assertFalse(decision.charge)
        self.assertIn("under minimumstrækket", decision.reason)

    def test_a_full_store_cannot_be_charged(self):
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=0.0)

        self.assertFalse(decision.charge)

    def test_the_charge_rate_limits_a_short_window(self):
        # 16 kW i en halv time er 8 kWh, uanset at der er 40 kWh plads.
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=40)

        self.assertAlmostEqual(decision.charge_kwh, 8.0, places=9)

    def test_a_worse_cop_later_makes_charging_more_attractive(self):
        # Samme priser, men COP falder til aften: saa er der mere at hente.
        same = planner().decide(plan(40, 100), cop_now=4.0, cop_later=4.0, headroom_kwh=20)
        worse = planner().decide(plan(40, 100), cop_now=4.0, cop_later=2.5, headroom_kwh=20)

        self.assertTrue(worse.charge)
        self.assertGreater(worse.saving_kr, same.saving_kr)

    def test_the_reason_says_what_was_decided(self):
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=20)

        self.assertIn("lad", decision.reason)
        self.assertIn("spar", decision.reason)
        self.assertIn("kWh", decision.charging_note)


class DecisionShapeTest(unittest.TestCase):
    def test_a_plain_decision_reads_sensibly(self):
        decision = Decision(source="pillefyr", heat_price=1.0, pellet_price=PELLET)

        self.assertEqual(decision.charging_note, "lad ikke op")


if __name__ == "__main__":
    unittest.main()


class WaitForTheCheapestTest(unittest.TestCase):
    """Prisen falder foer den stiger. Saa er nu ikke tidspunktet."""

    def setUp(self):
        # 1,00 -> 0,30 -> 0,30 -> 3,00 kr/kWh. COP 3 hele vejen.
        self.plan = plan(100, 30, 30, 300)
        self.planner = planner()

    def test_it_waits_for_the_cheap_slot_instead_of_charging_now(self):
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0
        )

        self.assertFalse(d.charge)
        self.assertIn("venter", d.reason)

    def test_and_charges_once_the_cheap_slot_is_the_one_it_stands_in(self):
        # Samme raekke set et kvarter senere: nu *er* 0,30 den billigste.
        d = self.planner.decide(
            plan(30, 30, 300), cop_now=3.0, cop_later=3.0, headroom_kwh=24.0
        )

        self.assertTrue(d.charge)

    def test_a_cheaper_slot_too_late_to_use_is_not_worth_waiting_for(self):
        # Naar den billige halvtime foerst kommer lige inden toppen, er der
        # ikke tid til mindstetraekket, og saa er den uden vaerdi.
        d = planner(charge_kw=2.0).decide(
            plan(100, 30, 300), cop_now=3.0, cop_later=3.0, headroom_kwh=24.0
        )

        self.assertNotIn("venter", d.reason)


class SavingIsWhatGetsDisplacedTest(unittest.TestCase):
    """Gevinsten gaelder den fortraengte varme, ikke hele lagerpladsen."""

    def setUp(self):
        # Een dyr halvtime forude. Huset bruger 3 kW.
        self.plan = plan(30, 300, 30)
        self.planner = planner()

    def test_the_saving_counts_only_the_dear_half_hour(self):
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0, demand_kw=3.0
        )

        self.assertTrue(d.charge)
        # 3 kW i en halv time er 1,5 kWh fortraengt - ikke de 24 der er plads
        # til. Marginen er den samme; det er gangefaktoren der var forkert.
        self.assertLess(d.saving_kr, d.charge_kwh * 0.35)

    def test_two_dear_half_hours_displace_twice_as_much(self):
        one = self.planner.decide(
            plan(30, 300, 30), cop_now=3.0, cop_later=3.0,
            headroom_kwh=24.0, demand_kw=3.0,
        )
        two = self.planner.decide(
            plan(30, 300, 300, 30), cop_now=3.0, cop_later=3.0,
            headroom_kwh=24.0, demand_kw=3.0,
        )

        self.assertAlmostEqual(two.saving_kr, 2 * one.saving_kr, places=6)

    def test_without_a_demand_it_says_so_by_not_pretending(self):
        # Uden et behov kan spoergsmaalet ikke besvares. Saa staar det gamle
        # tal - men det er nu det eneste tilfaelde, ikke reglen.
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0, demand_kw=None
        )

        self.assertTrue(d.charge)
        self.assertGreater(d.saving_kr, 0.0)

