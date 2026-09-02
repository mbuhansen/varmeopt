import unittest

from varmeopt.solar import Geometry, Plane, SolarModel, daily_incidence

# Anlægget på Fyn: fire solfangere i syd med 45°, mod 6,4 kW syd/20° og
# 4 kW vest/15° solceller.
FYN = Geometry(
    latitude=55.4,
    thermal=Plane(tilt=45.0, azimuth=0.0),
    pv=(Plane(20.0, 0.0, 6.4), Plane(15.0, 90.0, 4.0)),
)

MIDSUMMER = 172
MIDWINTER = 355


def model(scale=None, days=0.0):
    return SolarModel(FYN, scale=scale, days=days)


class IncidenceTest(unittest.TestCase):
    def test_a_steep_south_panel_beats_a_flat_one_in_winter(self):
        steep = daily_incidence(MIDWINTER, 55.4, Plane(45.0, 0.0))
        flat = daily_incidence(MIDWINTER, 55.4, Plane(15.0, 0.0))

        self.assertGreater(steep, flat)

    def test_and_loses_to_it_in_summer(self):
        steep = daily_incidence(MIDSUMMER, 55.4, Plane(45.0, 0.0))
        flat = daily_incidence(MIDSUMMER, 55.4, Plane(15.0, 0.0))

        self.assertLess(steep, flat)

    def test_the_sun_is_up_far_longer_in_june(self):
        self.assertGreater(
            daily_incidence(MIDSUMMER, 55.4, Plane(0.0, 0.0)),
            3 * daily_incidence(MIDWINTER, 55.4, Plane(0.0, 0.0)),
        )


class GeometryTest(unittest.TestCase):
    def test_the_ratio_swings_across_the_year(self):
        # Det er hele grunden til at geometrien regnes i stedet for at læres:
        # en fast faktor ville være groft forkert det halve af året.
        summer = FYN.ratio(MIDSUMMER)
        winter = FYN.ratio(MIDWINTER)

        self.assertLess(summer, 1.0)
        self.assertGreater(winter, 2.0)
        self.assertGreater(winter / summer, 2.0)

    def test_identical_planes_give_a_ratio_of_one(self):
        same = Geometry(55.4, Plane(30.0, 0.0), (Plane(30.0, 0.0, 1.0),))

        self.assertAlmostEqual(same.ratio(MIDSUMMER), 1.0, places=9)

    def test_no_pv_planes_gives_no_ratio(self):
        self.assertIsNone(Geometry(55.4, Plane(45.0, 0.0), ()).ratio(MIDSUMMER))


class LearnTest(unittest.TestCase):
    def test_the_first_day_sets_the_scale(self):
        m = model()
        ratio = m.geometric_ratio(MIDSUMMER)

        m.learn(thermal_kwh=20.0, pv_forecast_kwh=50.0, day_of_year=MIDSUMMER)

        self.assertAlmostEqual(m.scale, 20.0 / (50.0 * ratio), places=9)
        self.assertTrue(m.known)

    def test_a_full_store_teaches_nothing(self):
        # Kernen: en dag hvor tanken var fuld siger "der var ikke plads",
        # ikke "solen var dårlig". Lærer vi af den, forgifter vi tallet.
        m = model(scale=0.4, days=20.0)

        note = m.learn(5.0, 50.0, MIDSUMMER, store_was_full=True)

        self.assertAlmostEqual(m.scale, 0.4, places=9)
        self.assertIn("fuldt", note)

    def test_a_grey_day_teaches_nothing_either(self):
        m = model(scale=0.4, days=20.0)

        m.learn(0.1, 0.2, MIDSUMMER)

        self.assertAlmostEqual(m.scale, 0.4, places=9)

    def test_a_good_day_is_believed_quickly(self):
        # Et hoejt udbytte kan ikke skyldes regulering - det er aegte.
        m = model(scale=0.30, days=20.0)

        m.learn(40.0, 50.0, MIDSUMMER)
        observed = 40.0 / (50.0 * m.geometric_ratio(MIDSUMMER))

        self.assertAlmostEqual(m.scale, 0.30 + 0.5 * (observed - 0.30), places=9)

    def test_a_poor_day_is_believed_slowly(self):
        # Et lavt udbytte kan lige saa godt vaere en fuld tank som en graa dag.
        m = model(scale=0.50, days=20.0)

        m.learn(10.0, 50.0, MIDSUMMER)
        observed = 10.0 / (50.0 * m.geometric_ratio(MIDSUMMER))

        self.assertAlmostEqual(m.scale, 0.50 + 0.05 * (observed - 0.50), places=9)

    def test_the_two_august_days_from_the_real_plant(self):
        # 24. august koerte frit: PV 60,9 kWh, solvarme 29 kWh, top 5,4 kW.
        # 27. august var reguleret: PV faldt kun 9 %, solvarmen 34 %, og
        # toppen naaede kun 3,6 kW paa et anlaeg der kan 5,4.
        #
        # Symmetrisk laering ville have trukket tallet ned mod den regulerede
        # dag. Asymmetrien holder det taet paa den frie.
        free = model()
        free.learn(29.0, 60.9, 236)
        truth = free.scale

        both = model()
        both.learn(29.0, 60.9, 236)
        both.learn(19.0, 55.4, 239)

        self.assertAlmostEqual(truth, 0.428, places=2)
        self.assertGreater(both.scale, 0.42)
        self.assertLess(abs(both.scale - truth), 0.01)


class ExpectTest(unittest.TestCase):
    def test_nothing_is_predicted_before_anything_is_learned(self):
        self.assertIsNone(model().expected_kwh(50.0, MIDSUMMER))
        self.assertFalse(model().known)

    def test_prediction_follows_the_forecast_and_the_season(self):
        m = model(scale=0.4, days=20.0)

        summer = m.expected_kwh(50.0, MIDSUMMER)
        winter = m.expected_kwh(50.0, MIDWINTER)

        # Samme PV-prognose giver langt mere solvarme om vinteren, fordi 45°
        # møder den lave sol naer vinkelret.
        self.assertGreater(winter, summer * 2)

    def test_no_forecast_no_prediction(self):
        m = model(scale=0.4, days=20.0)

        self.assertIsNone(m.expected_kwh(None, MIDSUMMER))


class StorageTest(unittest.TestCase):
    def test_round_trip(self):
        m = model(scale=0.412, days=7.0)

        back = SolarModel.from_raw(m.to_raw(), FYN)

        self.assertAlmostEqual(back.scale, 0.412, places=9)
        self.assertEqual(back.days, 7.0)

    def test_an_unlearned_model_survives_a_round_trip(self):
        back = SolarModel.from_raw(model().to_raw(), FYN)

        self.assertIsNone(back.scale)
        self.assertFalse(back.known)

    def test_garbage_gives_an_unlearned_model(self):
        self.assertIsNone(SolarModel.from_raw("ikke en model", FYN).scale)
        self.assertIsNone(SolarModel.from_raw({"scale": "aeh"}, FYN).scale)


if __name__ == "__main__":
    unittest.main()
