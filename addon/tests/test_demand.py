import unittest

from varmeopt.demand import Balance, Load, thermal_kw


class ThermalPowerTest(unittest.TestCase):
    def test_matches_the_plant_display(self):
        # UVR'ens eget display viser 2,73 kW ved 130 l/h. Med fremløb 44,4 og
        # retur 26,2 er faldet 18,2 K. Rammer vi ikke det, er konstanten gal.
        self.assertAlmostEqual(thermal_kw(130, 18.2), 2.72, places=2)

    def test_no_flow_is_no_power(self):
        self.assertEqual(thermal_kw(0, 20), 0.0)

    def test_missing_input_gives_nothing(self):
        self.assertIsNone(thermal_kw(None, 20))
        self.assertIsNone(thermal_kw(130, None))


class LoadTest(unittest.TestCase):
    def test_delta_and_power(self):
        load = Load(flow=44.4, ret=26.2, litres_per_hour=130)

        self.assertAlmostEqual(load.delta, 18.2, places=6)
        self.assertAlmostEqual(load.kw, 2.72, places=2)

    def test_a_negative_delta_is_not_a_negative_demand(self):
        # Ved stilstand kan returen måle varmere end fremløbet. Det er ikke et
        # forbrug på minus to kilowatt.
        load = Load(flow=30.0, ret=32.0, litres_per_hour=100)

        self.assertEqual(load.kw, 0.0)

    def test_standstill_is_not_circulation(self):
        self.assertFalse(Load(flow=44.0, ret=26.0, litres_per_hour=0).circulating)
        self.assertFalse(Load(flow=44.0, ret=26.0, litres_per_hour=None).circulating)
        self.assertTrue(Load(flow=44.0, ret=26.0, litres_per_hour=130).circulating)

    def test_missing_sensors_give_no_answer(self):
        self.assertIsNone(Load().kw)
        self.assertIsNone(Load(flow=44.0).delta)


class BalanceTest(unittest.TestCase):
    def setUp(self):
        self.load = Load(flow=44.4, ret=26.2, litres_per_hour=130)

    def test_only_sources_that_actually_deliver_are_counted(self):
        balance = Balance(
            load=self.load, solar_kw=0.96, element_kw=0.0, heatpump_kw=None, boiler_kw=2.5
        )

        self.assertEqual(set(balance.sources), {"solvarme", "pillefyr"})
        self.assertAlmostEqual(balance.input_kw, 3.46, places=6)

    def test_free_heat_is_kept_apart_from_bought(self):
        # Hele pointen med at skille kilderne ad: solvarme er gratis, og en
        # plan der lader op med varmepumpen fortraenger den.
        balance = Balance(load=self.load, solar_kw=0.96, heatpump_kw=3.0)

        self.assertAlmostEqual(balance.free_kw, 0.96, places=6)
        self.assertAlmostEqual(balance.input_kw, 3.96, places=6)

    def test_net_is_input_minus_demand(self):
        balance = Balance(load=self.load, solar_kw=0.96)

        self.assertAlmostEqual(balance.net_kw, 0.96 - 2.7185, places=3)

    def test_no_demand_reading_means_no_net(self):
        self.assertIsNone(Balance(load=Load(), solar_kw=1.0).net_kw)

    def test_hours_left_while_draining(self):
        # 2,72 kW ud, intet ind: 11,6 kWh raekker godt fire timer.
        balance = Balance(load=self.load)

        self.assertAlmostEqual(balance.hours_left(11.6), 11.6 / 2.7185, places=3)
        self.assertIsNone(balance.hours_to_full(22.9))

    def test_hours_to_full_while_charging(self):
        balance = Balance(load=self.load, heatpump_kw=8.0)

        self.assertIsNone(balance.hours_left(11.6))
        self.assertAlmostEqual(balance.hours_to_full(22.9), 22.9 / (8.0 - 2.7185), places=3)

    def test_a_balanced_system_has_no_horizon(self):
        # Gaar det lige op, er svaret hverken "raekker to timer" eller "fuld om
        # to timer" - det er "uaendret", og der er intet tal at give.
        balance = Balance(load=self.load, heatpump_kw=2.7185)

        self.assertIsNone(balance.hours_left(11.6))
        self.assertIsNone(balance.hours_to_full(22.9))

    def test_no_stored_energy_reading_gives_no_horizon(self):
        balance = Balance(load=self.load)

        self.assertIsNone(balance.hours_left(None))


if __name__ == "__main__":
    unittest.main()


class MeterFloorTest(unittest.TestCase):
    """Et nul fra en maaler der foerst taeller fra 100 l/h er ikke et nul."""

    def test_a_zero_below_the_floor_is_unknown_not_no_demand(self):
        # Maaleren kan vise nul ved reelle stroemme op mod 100 l/h. Ved 15 K
        # er det op mod 1,7 kW, altsaa ikke noget man kan kalde ingenting.
        load = Load(flow=45.0, ret=30.0, litres_per_hour=0.0)

        self.assertIsNone(load.kw)
        self.assertFalse(load.trustworthy)

    def test_a_stuck_meter_is_unknown_too(self):
        # 5 l/h gav foer 0,06 kW, som ser ud som et rigtigt forbrug: 101
        # timers restlevetid paa lageret.
        load = Load(flow=45.0, ret=30.0, litres_per_hour=5.0)

        self.assertIsNone(load.kw)

    def test_above_the_floor_the_reading_counts(self):
        load = Load(flow=45.0, ret=30.0, litres_per_hour=130.0)

        self.assertIsNotNone(load.kw)
        self.assertTrue(load.trustworthy)

    def test_the_floor_is_the_meters_property_not_the_plants(self):
        # En bedre maaler ville have et lavere gulv, og saa er 50 l/h en
        # maaling. Derfor er tallet en indstilling.
        load = Load(flow=45.0, ret=30.0, litres_per_hour=50.0, meter_floor=10.0)

        self.assertIsNotNone(load.kw)

