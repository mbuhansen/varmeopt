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
        self.assertIn("taet", why)

    def test_without_a_cop_we_assume_the_heat_pump(self):
        source, why = source_now(None, PELLET, 0.05)

        self.assertEqual(source, "varmepumpe")
        self.assertIn("ingen COP", why)


class CheapestHeatTest(unittest.TestCase):
    def test_the_boiler_caps_the_price(self):
        # Uanset hvor dyr stroemmen bliver, betaler man aldrig mere end pille.
        self.assertAlmostEqual(planner().cheapest_heat(20.0, 4.0), PELLET, places=9)

    def test_the_pump_wins_when_it_is_cheaper(self):
        self.assertAlmostEqual(planner().cheapest_heat(1.20, 4.0), 0.30, places=9)

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
        # Under loftet slaar prisen stadig igennem.
        mild = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=20)
        værre = planner().decide(plan(40, 280), cop_now=4.0, headroom_kwh=20)

        self.assertGreater(værre.saving_kr, mild.saving_kr)

    def test_wear_has_to_be_covered_first(self):
        # 0,10 kr at hente pr. kWh, men slitagen er 0,15. Saa lad vaere.
        decision = planner(wear_kr_per_kwh=0.15).decide(
            plan(40, 80), cop_now=4.0, headroom_kwh=20
        )

        self.assertFalse(decision.charge)

    def test_solar_gets_its_share_first(self):
        # 20 kWh plads, men solen venter med 18. Saa er der 2 tilbage, og det
        # er under minimumstraekket.
        decision = planner().decide(
            plan(40, 240), cop_now=4.0, headroom_kwh=20, solar_expected_kwh=18.0
        )

        self.assertFalse(decision.charge)
        self.assertIn("under minimumstraekket", decision.reason)

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

        self.assertFalse(same.charge)
        self.assertTrue(worse.charge)

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
