import unittest

from varmeopt.solar import (
    SUSPICIOUS,
    DayTracker,
    Geometry,
    Plane,
    SolarModel,
    daily_irradiance,
    diffuse_fraction,
    seed_scale,
)

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
        steep = daily_irradiance(MIDWINTER, 55.4, Plane(45.0, 0.0))
        flat = daily_irradiance(MIDWINTER, 55.4, Plane(15.0, 0.0))

        self.assertGreater(steep, flat)

    def test_and_loses_to_it_in_summer(self):
        steep = daily_irradiance(MIDSUMMER, 55.4, Plane(45.0, 0.0))
        flat = daily_irradiance(MIDSUMMER, 55.4, Plane(15.0, 0.0))

        self.assertLess(steep, flat)

    def test_the_sun_is_up_far_longer_in_june(self):
        self.assertGreater(
            daily_irradiance(MIDSUMMER, 55.4, Plane(0.0, 0.0)),
            3 * daily_irradiance(MIDWINTER, 55.4, Plane(0.0, 0.0)),
        )


class DiffuseTest(unittest.TestCase):
    def test_the_sky_carries_most_of_the_light_here(self):
        # 55,4° nord. Under halvdelen kommer fra solskiven, selv midt om
        # sommeren, og om vinteren er det fire femtedele.
        self.assertGreater(diffuse_fraction(MIDSUMMER), 0.45)
        self.assertGreater(diffuse_fraction(MIDWINTER), diffuse_fraction(MIDSUMMER))
        self.assertLess(diffuse_fraction(MIDWINTER), 0.95)

    def test_a_flat_plane_sees_the_whole_sky_and_a_steep_one_does_not(self):
        # Udsynsfaktoren er hele grunden til at årstidsudsvinget er mindre
        # end den direkte stråling alene siger.
        for doy in (MIDSUMMER, MIDWINTER):
            flat = daily_irradiance(doy, 55.4, Plane(0.0, 0.0))
            steep = daily_irradiance(doy, 55.4, Plane(90.0, 0.0))
            self.assertGreater(flat, 0.0)
            self.assertGreater(steep, 0.0)


class GeometryTest(unittest.TestCase):
    def test_the_ratio_swings_across_the_year(self):
        # Det er hele grunden til at geometrien regnes i stedet for at læres:
        # en fast faktor ville være forkert det halve af året.
        summer = FYN.ratio(MIDSUMMER)
        winter = FYN.ratio(MIDWINTER)

        self.assertLess(summer, 1.0)
        self.assertGreater(winter, 1.2)
        self.assertGreater(winter / summer, 1.3)

    def test_the_swing_is_not_the_one_beam_alone_would_predict(self):
        # Regnet på kun den direkte stråling svinger forholdet 2,5x og
        # december lander på 2,27. Det lovede planlæggeren 74 % mere
        # solvarme end anlægget kan levere, i den måned hvor et forkert
        # løfte er dyrest.
        winter = FYN.ratio(MIDWINTER)

        self.assertLess(winter, 1.6)

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

    def test_good_and_bad_days_move_the_scale_equally_far(self):
        # Læringen er symmetrisk. Det var den ikke: op med alfa 0,5, ned med
        # 0,05, så mætning ikke skulle trække tallet ned. Men mætning
        # filtreres allerede fra, og en skæv EMA er ikke en filtrering.
        up, down = model(scale=0.40, days=20.0), model(scale=0.40, days=20.0)
        ratio = up.geometric_ratio(MIDSUMMER)

        up.learn(0.50 * 50.0 * ratio, 50.0, MIDSUMMER)
        down.learn(0.30 * 50.0 * ratio, 50.0, MIDSUMMER)

        self.assertAlmostEqual(up.scale - 0.40, 0.40 - down.scale, places=9)

    def test_symmetric_noise_no_longer_biases_the_scale_upward(self):
        # Med 0,5 op mod 0,05 ned lagde en sand værdi på 0,40 sig 12-37 %
        # for højt afhængigt af spredningen, også når støjen var helt
        # symmetrisk: hvert udsving opad blev troet ti gange så meget som
        # det tilsvarende nedad.
        m = model(scale=0.40, days=20.0)
        ratio = m.geometric_ratio(MIDSUMMER)

        for step in range(200):
            wobble = 0.10 if step % 2 else -0.10
            m.learn((0.40 + wobble) * 50.0 * ratio, 50.0, MIDSUMMER)

        self.assertLess(abs(m.scale - 0.40), 0.02)

    def test_a_regulated_day_is_filtered_not_smoothed_away(self):
        # 24. august kørte frit: PV 60,9 kWh, solvarme 29 kWh, top 5,4 kW.
        # 27. august var reguleret: PV faldt kun 9 %, solvarmen 34 %, og
        # toppen nåede kun 3,6 kW på et anlæg der kan 5,4.
        #
        # Den dag skal kasseres, ikke dæmpes. Det er det store_was_full er til.
        free = model()
        free.learn(29.0, 60.9, 236)
        truth = free.scale

        both = model()
        both.learn(29.0, 60.9, 236)
        both.learn(19.0, 55.4, 239, store_was_full=True)

        self.assertEqual(both.scale, truth)


class PersistenceTest(unittest.TestCase):
    def test_a_scale_survives_a_restart(self):
        m = SolarModel.from_raw(model(scale=0.42, days=9.0).to_raw(), FYN)

        self.assertAlmostEqual(m.scale, 0.42)
        self.assertEqual(m.days, 9.0)

    def test_a_scale_from_the_old_geometry_is_discarded(self):
        # Version 1 regnede kun direkte stråling, så 0,42 betød noget
        # andet end det gør nu. At læse det videre ville blande to
        # malestokke; det koster et døgn at lære forfra.
        m = SolarModel.from_raw({"scale": 0.42, "days": 40.0}, FYN)

        self.assertIsNone(m.scale)
        self.assertFalse(m.known)


class ExpectTest(unittest.TestCase):
    def test_nothing_is_predicted_before_anything_is_learned(self):
        self.assertIsNone(model().expected_kwh(50.0, MIDSUMMER))
        self.assertFalse(model().known)

    def test_prediction_follows_the_forecast_and_the_season(self):
        m = model(scale=0.4, days=20.0)

        summer = m.expected_kwh(50.0, MIDSUMMER)
        winter = m.expected_kwh(50.0, MIDWINTER)

        # Samme PV-prognose giver mere solvarme om vinteren, fordi 45° møder
        # den lave sol nærmere vinkelret. Men kun omkring 45 % mere - ikke
        # de over 100 % den rene direkte stråling ville love, for om
        # vinteren kommer fire femtedele af lyset fra hele himlen, og der
        # ser en flad flade mere end en stejl.
        self.assertGreater(winter, summer * 1.3)
        self.assertLess(winter, summer * 1.7)

    def test_no_forecast_no_prediction(self):
        m = model(scale=0.4, days=20.0)

        self.assertIsNone(m.expected_kwh(None, MIDSUMMER))


class DayTrackerTest(unittest.TestCase):
    def setUp(self):
        self.t = DayTracker()

    def test_a_day_is_only_closed_when_the_next_one_starts(self):
        self.assertIsNone(self.t.observe("2026-08-24", 0, 60.9, 0.0))
        self.assertIsNone(self.t.observe("2026-08-24", 12, 30.0, 18.0))
        self.assertIsNone(self.t.observe("2026-08-24", 23, 0.0, 29.0))

        done = self.t.observe("2026-08-25", 0, 55.0, 0.0)

        self.assertEqual(done, (29.0, 60.9, "2026-08-24", False))

    def test_the_forecast_is_the_one_captured_at_midnight(self):
        # "Resten af dagen" er kun hele dagen hvis man spørger før solopgang.
        self.t.observe("2026-08-24", 0, 60.9, 0.0)
        self.t.observe("2026-08-24", 14, 20.0, 22.0)

        done = self.t.observe("2026-08-25", 0, 55.0, 0.0)

        self.assertEqual(done[1], 60.9)

    def test_a_day_started_in_the_afternoon_is_not_learned(self):
        # Add-on'en blev startet kl. 14. Da er "resten af dagen" ikke hele
        # dagen, og forholdet ville blive helt skævt.
        self.t.observe("2026-08-24", 14, 20.0, 22.0)
        self.t.observe("2026-08-24", 23, 0.0, 29.0)

        self.assertIsNone(self.t.observe("2026-08-25", 0, 55.0, 0.0))

    def test_saturation_seen_during_the_day_is_carried_to_the_end(self):
        # Ved midnat er tankene kølet af. Så mætningen skal huskes fra da
        # den skete, ikke aflæses når døgnet gores op.
        self.t.observe("2026-08-27", 0, 55.4, 0.0)
        self.t.observe("2026-08-27", 13, 20.0, 15.0, store_full=True)
        self.t.observe("2026-08-27", 23, 0.0, 19.0, store_full=False)

        done = self.t.observe("2026-08-28", 0, 50.0, 0.0)

        self.assertTrue(done[3])

    def test_saturation_resets_with_the_new_day(self):
        self.t.observe("2026-08-27", 0, 55.4, 0.0, store_full=True)
        self.t.observe("2026-08-28", 0, 50.0, 0.0)

        self.assertFalse(self.t.saturated)

    def test_round_trip_survives_a_restart(self):
        self.t.observe("2026-08-24", 0, 60.9, 5.0, store_full=True)

        back = DayTracker.from_raw(self.t.to_raw())
        done = back.observe("2026-08-25", 0, 55.0, 0.0)

        self.assertEqual(done, (5.0, 60.9, "2026-08-24", True))

    def test_garbage_gives_a_fresh_tracker(self):
        self.assertIsNone(DayTracker.from_raw("ikke en dag").date)


class CounterResetsBeforeMidnightTest(unittest.TestCase):
    """Dagstælleren står på nul når døgnet gøres op - og det kostede modellen.

    Anlæggets ``sensor.solvarme_produktion_idag`` tæller op gennem dagen: den
    17. september 0,3 kWh kl. 09:55 og 2,6 kl. 13:26. Men kl. 23:59 stod den
    på nul, og det er dér døgnet bliver gjort op. Loggen natten til den 20.:

        solvarme, døgnet 2026-09-19: skalafaktor 0.030 (dag 17, i dag 0.000)

    Fire døgn i træk blev lært som nøjagtig nul, og skalafaktoren faldt
    0,049 → 0,041 → 0,035 → 0,030 → 0,026 - nøjagtig 0,85 pr. døgn, som
    udglatningen gør når den fodres med nul. Modellen lovede så 1,3 kWh
    solvarme af 46 kWh solcelleprognose, dagen efter at anlægget havde lavet
    16,4.

    Svaret er ikke at læse tælleren på et bestemt klokkeslæt før midnat - vi
    ved ikke hvornår den springer tilbage. Det er at huske døgnets **højeste**
    aflæsning. En dagstæller går kun opad.
    """

    def setUp(self):
        self.t = DayTracker()

    def test_a_counter_that_resets_in_the_evening_still_gives_the_day(self):
        self.t.observe("2026-09-19", 0, 35.0, 0.0)
        self.t.observe("2026-09-19", 10, 20.0, 4.2)
        self.t.observe("2026-09-19", 14, 8.0, 16.4)
        # Og så står den på nul resten af aftenen.
        self.t.observe("2026-09-19", 20, 0.0, 0.0)
        self.t.observe("2026-09-19", 23, 0.0, 0.0)

        done = self.t.observe("2026-09-20", 0, 40.0, 0.0)

        self.assertEqual(done[0], 16.4, "døgnets højeste, ikke den sidste aflæsning")

    def test_a_sensor_that_drops_out_does_not_set_the_day_back(self):
        self.t.observe("2026-09-19", 0, 35.0, 0.0)
        self.t.observe("2026-09-19", 14, 8.0, 16.4)
        self.t.observe("2026-09-19", 23, 0.0, None)

        done = self.t.observe("2026-09-20", 0, 40.0, 0.0)

        self.assertEqual(done[0], 16.4)

    def test_the_new_day_starts_over(self):
        # Det højeste må ikke bæres med over i næste døgn.
        self.t.observe("2026-09-19", 0, 35.0, 0.0)
        self.t.observe("2026-09-19", 14, 8.0, 16.4)
        self.t.observe("2026-09-20", 0, 40.0, 0.0)
        self.t.observe("2026-09-20", 14, 9.0, 2.6)

        done = self.t.observe("2026-09-21", 0, 46.0, 0.0)

        self.assertEqual(done[0], 2.6)


class ScaleIsDiscardedWithTheOldCounterTest(unittest.TestCase):
    """Det der blev lært af den sidste aflæsning, må ikke bæres med over.

    De atten døgn modellen havde lært pr. 21. september, var regnet af
    dagstællerens *sidste* aflæsning - og den står på nul ved midnat. Fire af
    dem var nøjagtig 0,000, og skalafaktoren stod på 0,026 mod de 0,476
    kalibreringen giver.

    Udbyttet gøres nu op som døgnets højeste, og det er en anden målestok.
    ``MODEL_VERSION`` findes til præcis det: et tal lært under en ældre
    version kastes væk, og modellen seedes igen. Det koster ét døgn at lære
    forfra og er billigere end fjorten dages forkerte forudsigelser, mens
    udglatningen langsomt kravler tilbage.
    """

    def test_a_model_learned_with_the_old_counter_is_dropped(self):
        gammel = {"model": 2, "scale": 0.0256, "days": 18.0}

        m = SolarModel.from_raw(gammel, FYN)

        self.assertIsNone(m.scale, "den ødelagte skalafaktor må ikke overleve")
        self.assertEqual(m.days, 0.0)

    def test_and_the_seed_is_close_to_what_the_plant_actually_did(self):
        # Kalibreringsdagen giver 0,476 for den her geometri. Brugerens egen
        # måling den 20. september - 16,4 kWh solvarme ved 35 kWh sol - giver
        # 0,433. Seeden er altså et brugbart sted at begynde forfra.
        målt = 16.4 / (35.0 * FYN.ratio(263))

        self.assertAlmostEqual(seed_scale(FYN), 0.476, places=3)
        self.assertLess(abs(seed_scale(FYN) - målt), 0.05)

    def test_a_model_learned_with_the_new_counter_survives(self):
        m = model(scale=0.45, days=6.0)

        igen = SolarModel.from_raw(m.to_raw(), FYN)

        self.assertAlmostEqual(igen.scale, 0.45, places=9)
        self.assertEqual(igen.days, 6.0)


class SuspiciousDayTest(unittest.TestCase):
    """En dag der ikke ligner modellen, skal råbes op i loggen.

    Grænsen kan være stram fordi skalafaktoren netop *ikke* afhænger af
    vejret: observationen er udbytte divideret med solcelleprognosen, og en
    overskyet dag trækker begge dele ned. Ligger et døgn pludselig en faktor
    fire fra resten, er det ikke en grå dag - det er et input der svigter.

    Den filtrerer ikke. Fire nul-døgn i træk blev lært, og ikke én linje
    sagde at noget var galt. Det er hele forskellen mellem en fejl der
    opdages på dag 15 og en der opdages på dag 19.
    """

    def test_a_day_that_yields_nothing_is_called_out(self):
        m = model(scale=0.049, days=14.0)

        note = m.learn(0.0, 35.0, 262)

        self.assertTrue(note.startswith(SUSPICIOUS), note)
        self.assertIn("0.049", note, "modellens eget tal skal med")

    def test_but_it_is_still_learned(self):
        # Advarslen filtrerer ikke. Modellen kan ikke afgøre hvad der er
        # rigtigt - den siger bare til.
        m = model(scale=0.049, days=14.0)

        m.learn(0.0, 35.0, 262)

        self.assertEqual(m.days, 15.0)
        self.assertAlmostEqual(m.scale, 0.049 * 0.85, places=6)

    def test_an_ordinary_day_says_nothing(self):
        m = model(scale=0.45, days=14.0)
        ratio = FYN.ratio(262)

        note = m.learn(0.43 * 35.0 * ratio, 35.0, 262)

        self.assertFalse(note.startswith(SUSPICIOUS), note)

    def test_an_overcast_day_says_nothing_either(self):
        # Halvt så meget sol: både udbytte og prognose falder, og forholdet
        # står stille. Det er derfor grænsen kan være så stram.
        m = model(scale=0.45, days=14.0)
        ratio = FYN.ratio(262)

        note = m.learn(0.45 * 8.0 * ratio, 8.0, 262)

        self.assertFalse(note.startswith(SUSPICIOUS), note)

    def test_the_first_day_cannot_deviate_from_anything(self):
        note = model().learn(16.4, 35.0, 262)

        self.assertFalse(note.startswith(SUSPICIOUS), note)


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


class MinimumChargeTest(unittest.TestCase):
    """Mindste opladning der er værd at starte for."""

    def setUp(self):
        from dataclasses import replace
        from pathlib import Path

        from varmeopt.options import Options

        self.opts = Options.load(Path("findes-ikke.json"))
        self.replace = replace

    def test_minimum_follows_the_uvr_runtime(self):
        # 16 kW i 15 minutter er 4 kWh. Er der mindre plads end det, fylder
        # varmepumpen det og slukker igen.
        self.assertAlmostEqual(self.opts.min_charge_kwh, 4.0, places=6)

    def test_a_longer_minimum_runtime_raises_the_bar(self):
        slow = self.replace(self.opts, hp_min_runtime_minutes=30)

        self.assertAlmostEqual(slow.min_charge_kwh, 8.0, places=6)

    def test_a_modulating_pump_lowers_it(self):
        gentle = self.replace(self.opts, hp_charge_kw=4.0)

        self.assertAlmostEqual(gentle.min_charge_kwh, 1.0, places=6)


class SeedTest(unittest.TestCase):
    """Startværdien skal udledes, ikke skrives ned."""

    def test_the_seed_comes_out_of_the_calibration_day(self):
        # 24. august 2026: solcellerne lavede 60,9 kWh, solvarmen 29,0.
        seed = seed_scale(FYN)
        m = SolarModel(FYN, scale=seed, days=1.0)

        self.assertAlmostEqual(m.expected_kwh(60.9, 236), 29.0, places=6)

    def test_a_written_down_seed_goes_stale_when_the_geometry_moves(self):
        # Det er præcis det der skete i 0.19.0: den diffuse stråling kom
        # med, og 0,43 fra den gamle geometri blev 10 % for lavt.
        beam_only = seed_scale(FYN)

        self.assertGreater(beam_only, 0.43 * 1.05)
        self.assertAlmostEqual(beam_only, 0.476, places=3)

    def test_the_geometry_matches_the_plant(self):
        # Fire solfangere i syd med 45 grader, mod 6,4 kW syd/20 og
        # 4 kW vest/15, på 55,4 grader nord.
        self.assertEqual(FYN.latitude, 55.4)
        self.assertEqual((FYN.thermal.tilt, FYN.thermal.azimuth), (45.0, 0.0))
        self.assertEqual([(p.tilt, p.azimuth, p.weight) for p in FYN.pv],
                         [(20.0, 0.0, 6.4), (15.0, 90.0, 4.0)])

