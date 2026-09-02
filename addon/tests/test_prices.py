import unittest

from varmeopt.prices import EXPORT_FLOOR, Grid, Plan, Slot


def row(state="", import_rate=200, export_rate=80, soc=50):
    return {
        "state": state,
        "import_rate": import_rate,
        "export_rate": export_rate,
        "soc_percent": soc,
    }


def plan(*rows, battery_average=1.0):
    return Plan.from_predbat({"raw": {"rows": list(rows)}}, battery_average=battery_average)


class ParseTest(unittest.TestCase):
    def test_rates_come_in_oere_and_are_converted(self):
        p = plan(row(import_rate=250, export_rate=95))

        self.assertAlmostEqual(p.slots[0].import_price, 2.50, places=9)
        self.assertAlmostEqual(p.slots[0].export_price, 0.95, places=9)

    def test_slots_are_half_hours_from_now(self):
        p = plan(row(), row(), row())

        self.assertEqual([s.minutes_ahead for s in p.slots], [0, 30, 60])
        self.assertEqual(p.horizon_minutes, 90)

    def test_missing_rates_survive_as_none(self):
        p = plan({"state": "chrg"})

        self.assertIsNone(p.slots[0].import_price)
        self.assertEqual(p.slots[0].state, "chrg")

    def test_garbage_gives_an_empty_plan(self):
        for junk in (None, "ikke en plan", {}, {"raw": "noget"}, {"raw": {"rows": "aeh"}}):
            self.assertEqual(len(Plan.from_predbat(junk)), 0)

    def test_battery_average_never_drops_below_the_export_floor(self):
        # Der er altid den mulighed at saelge energien i stedet.
        p = plan(row(), battery_average=0.20)

        self.assertAlmostEqual(p.battery_average, EXPORT_FLOOR, places=9)


class SlotStateTest(unittest.TestCase):
    def test_states_are_recognised(self):
        self.assertTrue(Slot(0, "chrg", None, None, None).charging)
        self.assertTrue(Slot(0, "holdchrg", None, None, None).locked)
        self.assertTrue(Slot(0, "exp", None, None, None).exporting)
        self.assertFalse(Slot(0, "", None, None, None).locked)


class MarginalTest(unittest.TestCase):
    def test_planned_export_costs_the_lost_income(self):
        p = plan(row(state="exp", export_rate=140))

        price = p.marginal(0)

        self.assertAlmostEqual(price.kr_per_kwh, 1.40, places=9)
        self.assertIn("eksport", price.reason)

    def test_a_locked_battery_means_the_pump_runs_on_the_grid(self):
        p = plan(row(state="holdchrg", import_rate=180))

        price = p.marginal(0)

        self.assertAlmostEqual(price.kr_per_kwh, 1.80, places=9)
        self.assertIn("bundet", price.reason)

    def test_a_free_battery_costs_its_average(self):
        p = plan(row(), row(), battery_average=1.15)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.15, places=9)
        self.assertIn("frit", price.reason)

    def test_energy_is_valued_against_a_coming_export(self):
        # Eksport om en time til 1,60 er mere vaerd end batteriets 1,00.
        #
        # Det er en vaerdisaettelse, ikke en beslutning: om energien faktisk
        # bliver gemt, afgoeres af hvad den ellers skulle bruges til.
        p = plan(row(), row(), row(state="exp", export_rate=160), battery_average=1.0)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.60 * 0.90, places=9)
        self.assertIn("værdisat mod eksport", price.reason)

    def test_a_cheap_charge_soon_frees_the_battery(self):
        p = plan(row(), row(state="chrg", import_rate=40), battery_average=1.0)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        # max(1,00 batterisnit, 0,40 x 1,10) = 1,00
        self.assertAlmostEqual(price.kr_per_kwh, 1.0, places=9)
        self.assertIn("lades om", price.reason)

    def test_a_low_battery_is_priced_at_its_own_average(self):
        # Er der ikke energi nok til baade at varme og saelge, er eksporten
        # ikke et reelt alternativ. Betingelsen fandtes i Node-RED som
        # currentSOC > 40 og faldt paa gulvet ved portningen.
        p = plan(
            row(soc=25),
            row(),
            row(state="exp", export_rate=160),
            battery_average=1.0,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.0, places=9)
        self.assertIn("frit", price.reason)

    def test_a_full_battery_can_afford_to_be_valued_against_export(self):
        p = plan(
            row(soc=75),
            row(),
            row(state="exp", export_rate=160),
            battery_average=1.0,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.60 * 0.90, places=9)
        self.assertIn("SOC 75 %", price.reason)

    def test_an_unknown_soc_is_assumed_to_be_enough(self):
        p = plan(
            {"state": "", "import_rate": 200, "export_rate": 80},
            row(),
            row(state="exp", export_rate=160),
            battery_average=1.0,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("værdisat mod eksport", price.reason)

    def test_an_export_that_pays_less_than_the_battery_is_not_worth_saving_for(self):
        p = plan(row(), row(state="exp", export_rate=60), battery_average=1.20)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("frit", price.reason)

    def test_physical_export_beats_the_plan(self):
        # Planen siger ingenting, men maaleren siger at der gaar stroem ud.
        p = plan(row(export_rate=120))

        price = p.marginal(0, grid=Grid(grid_power=-4000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.20, places=9)
        self.assertIn("eksport", price.reason)

    def test_physical_import_is_priced_at_the_grid(self):
        p = plan(row(import_rate=210))

        price = p.marginal(0, grid=Grid(grid_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 2.10, places=9)
        self.assertIn("import", price.reason)

    def test_neither_direction_takes_the_cheaper_of_the_two(self):
        p = plan(row(import_rate=60), battery_average=1.30)

        price = p.marginal(0, grid=Grid())

        self.assertAlmostEqual(price.kr_per_kwh, 0.60, places=9)
        self.assertEqual(price.reason, "balanceret")

    def test_beyond_the_horizon_there_is_no_price(self):
        self.assertIsNone(plan(row()).marginal(600))


class FutureTest(unittest.TestCase):
    """Det nye: en pris for en halvtime vi endnu ikke er naaet til."""

    def test_a_future_slot_is_priced_from_the_plan_alone(self):
        p = plan(row(), row(state="exp", export_rate=150), row(state="chrg", import_rate=30))

        self.assertIn("eksport", p.marginal(30).reason)
        self.assertIn("bundet", p.marginal(60).reason)

    def test_the_physical_reading_only_applies_to_the_slot_we_are_in(self):
        # Grid gaelder nu. En halvtime frem maa planen staa alene.
        p = plan(row(import_rate=200), row(import_rate=50), battery_average=1.0)

        now = p.marginal(0, grid=Grid(grid_power=4000))
        later = p.marginal(30, grid=Grid(grid_power=4000))

        self.assertAlmostEqual(now.kr_per_kwh, 2.00, places=9)
        self.assertNotEqual(later.reason, "net: import")


class WindowTest(unittest.TestCase):
    def test_finds_the_cheapest_stretch(self):
        p = plan(
            row(state="holdchrg", import_rate=300),
            row(state="holdchrg", import_rate=100),
            row(state="holdchrg", import_rate=90),
            row(state="holdchrg", import_rate=400),
        )

        start, average = p.cheapest_window(60)

        self.assertEqual(start, 30)
        self.assertAlmostEqual(average, (1.00 + 0.90) / 2, places=9)

    def test_a_deadline_rules_out_later_windows(self):
        p = plan(
            row(state="holdchrg", import_rate=300),
            row(state="holdchrg", import_rate=280),
            row(state="holdchrg", import_rate=10),
        )

        start, _ = p.cheapest_window(30, before_minutes=60)

        self.assertEqual(start, 30)

    def test_a_window_longer_than_the_horizon_has_no_answer(self):
        p = plan(row(state="holdchrg"), row(state="holdchrg"))

        self.assertIsNone(p.cheapest_window(300))

    def test_a_deadline_that_leaves_no_room_has_no_answer(self):
        p = plan(row(state="holdchrg"), row(state="holdchrg"))

        self.assertIsNone(p.cheapest_window(60, before_minutes=30))


if __name__ == "__main__":
    unittest.main()
