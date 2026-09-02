import unittest

from varmeopt.tank import WH_PER_LITER_K, Buffer, Tank


def tank(name="A", liters=500.0, top=60.0, mid=45.0, bottom=30.0, outlet=None):
    return Tank(name=name, liters=liters, top=top, mid=mid, bottom=bottom, outlet=outlet)


class TankEnergyTest(unittest.TestCase):
    def test_stored_energy_over_reference(self):
        # 500 L delt på tre lag = 166,67 L pr. lag. Over 30 °C bidrager
        # top med 30 K og midt med 15 K, bunden med intet: 7500 L·K.
        t = tank()

        self.assertAlmostEqual(t.stored_kwh(30.0), 7500 * WH_PER_LITER_K / 1000, places=4)

    def test_headroom_up_to_ceiling(self):
        t = tank()

        self.assertAlmostEqual(t.headroom_kwh(60.0), 7500 * WH_PER_LITER_K / 1000, places=4)

    def test_layer_below_reference_never_counts_negative(self):
        # En bund koldere end referencen er ikke negativ energi — den er nul.
        t = tank(top=35.0, mid=30.0, bottom=20.0)

        self.assertAlmostEqual(t.stored_kwh(30.0), (500 / 3) * 5 * WH_PER_LITER_K / 1000, places=4)

    def test_missing_sensor_does_not_read_as_ice_cold(self):
        # Falder midterføleren ud, må de to andre dække tanken. Regnede vi
        # det manglende lag som 0 °C, ville estimatet styrtdykke uden at
        # tanken havde ændret sig.
        whole = tank()
        gap = tank(mid=None)

        self.assertAlmostEqual(gap.stored_kwh(30.0), whole.stored_kwh(30.0), places=4)
        self.assertEqual(len(gap.layers), 2)

    def test_uncovered_tank_stores_nothing(self):
        t = tank(top=None, mid=None, bottom=None)

        self.assertFalse(t.covered)
        self.assertEqual(t.stored_kwh(30.0), 0.0)
        self.assertIsNone(t.mean_temp)


class TankShapeTest(unittest.TestCase):
    def test_spread_is_top_minus_bottom(self):
        self.assertEqual(tank(top=58.0, bottom=31.0).spread, 27.0)

    def test_spread_needs_both_ends(self):
        self.assertIsNone(tank(bottom=None).spread)

    def test_outlet_wins_over_top_for_deliverable(self):
        # Toppen er hvad der står i tanken; afgangsrøret er hvad der faktisk
        # kommer ud. Det sidste er det der afgør om brugsvandet bliver varmt.
        self.assertEqual(tank(top=60.0, outlet=54.0).deliverable, 54.0)

    def test_top_stands_in_when_no_outlet_sensor(self):
        self.assertEqual(tank(top=60.0, outlet=None).deliverable, 60.0)


class BufferTest(unittest.TestCase):
    def setUp(self):
        self.buffer = Buffer(
            tanks=(
                tank("A", top=60.0, mid=45.0, bottom=30.0, outlet=58.0),
                tank("B", top=50.0, mid=40.0, bottom=30.0, outlet=48.0),
            ),
            reference=30.0,
            ceiling=60.0,
        )

    def test_stored_is_the_sum_of_measured_tanks(self):
        a, b = self.buffer.tanks

        self.assertAlmostEqual(
            self.buffer.stored_kwh, a.stored_kwh(30.0) + b.stored_kwh(30.0), places=6
        )

    def test_charge_percent_sits_between_reference_and_ceiling(self):
        pct = self.buffer.charge_percent

        self.assertIsNotNone(pct)
        self.assertGreater(pct, 0)
        self.assertLess(pct, 100)

    def test_full_buffer_is_a_hundred_percent(self):
        full = Buffer(
            tanks=(tank("A", top=60.0, mid=60.0, bottom=60.0),),
            reference=30.0,
            ceiling=60.0,
        )

        self.assertAlmostEqual(full.charge_percent, 100.0, places=6)
        self.assertAlmostEqual(full.headroom_kwh, 0.0, places=6)

    def test_room_remains_above_what_the_heat_pump_can_reach(self):
        # Solvarme og ACthor kan begge presse tankene til 90 °C. Er de allerede
        # over varmepumpens loft, er der nul plads *til varmepumpen* — men de
        # to andre har stadig et sted at gøre af varmen.
        hot = Buffer(
            tanks=(tank("A", top=65.0, mid=65.0, bottom=65.0),),
            reference=30.0,
            ceiling=60.0,
            peak_ceiling=90.0,
        )

        self.assertEqual(hot.headroom_kwh, 0.0)
        self.assertGreater(hot.peak_headroom_kwh, 0.0)
        self.assertTrue(hot.above_heatpump_ceiling)

    def test_a_cool_buffer_is_not_above_the_heat_pump_ceiling(self):
        self.assertFalse(self.buffer.above_heatpump_ceiling)
        self.assertGreater(self.buffer.peak_headroom_kwh, self.buffer.headroom_kwh)

    def test_an_unmeasured_buffer_is_not_declared_full(self):
        blind = Buffer(
            tanks=(tank("A", top=None, mid=None, bottom=None),),
            reference=30.0,
            ceiling=60.0,
        )

        self.assertFalse(blind.above_heatpump_ceiling)

    def test_imbalance_is_the_gap_between_tank_means(self):
        # A har middel 45, B har middel 40.
        self.assertAlmostEqual(self.buffer.imbalance, 5.0, places=6)

    def test_imbalance_needs_two_measured_tanks(self):
        lonely = Buffer(tanks=(tank("A"),), reference=30.0, ceiling=60.0)

        self.assertIsNone(lonely.imbalance)

    def test_deliverable_is_the_warmest_outlet(self):
        self.assertEqual(self.buffer.deliverable, 58.0)
        self.assertTrue(self.buffer.can_deliver(56.0))
        self.assertFalse(self.buffer.can_deliver(59.0))

    def test_sensor_count_reports_coverage(self):
        self.assertEqual(self.buffer.sensor_count, 6)

    def test_buffer_without_any_reading_is_not_covered(self):
        blind = Buffer(
            tanks=(tank("A", top=None, mid=None, bottom=None),),
            reference=30.0,
            ceiling=60.0,
        )

        self.assertFalse(blind.covered)
        self.assertEqual(blind.sensor_count, 0)
        self.assertIsNone(blind.charge_percent)


if __name__ == "__main__":
    unittest.main()
