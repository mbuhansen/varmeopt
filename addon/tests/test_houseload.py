"""Husets forbrug målt på lageret.

Målingen er et kalorimeter bygget af otte følere, og den skal kunne holde til
at der sker noget andet imens: et bad, en genstart, en føler der falder ud.
Testene her er mest af alt en liste over hvad der *ikke* er en måling af
husets forbrug.
"""

import unittest

from varmeopt.houseload import (
    MAX_AGE_MINUTES,
    WINDOW_MINUTES,
    HouseLoad,
    LoadCurve,
)
from varmeopt.tank import WH_PER_LITER_K

# 1000 L svarer til det her mange kWh pr. kelvin. Bruges til at oversaette
# foelerstoej til energi, saa stoejbudgettet i modulets docstring kan proeves.
KWH_PER_K = 1000 * WH_PER_LITER_K / 1000


def drive(
    load: HouseLoad,
    minutes: float = WINDOW_MINUTES,
    draw_kw: float = 2.5,
    input_kw: float = 0.0,
    step: float = 60.0,
    start_energy: float = 60.0,
    noise: float = 0.0,
    start_at: float = 0.0,
    **kwargs,
) -> list[str]:
    """Kør et vindue hvor huset trækker ``draw_kw`` og kilderne giver ``input_kw``.

    Returnerer hver eneste note undervejs, ikke kun den sidste: et vindue der
    bliver kasseret midtvejs, begynder forfra, og så siger den sidste note
    «måler» igen. Det der skal prøves, er *at* det blev kasseret.
    """
    energy = start_energy
    now = start_at
    notes = []
    sign = 1.0
    for _ in range(int(minutes * 60 / step) + 1):
        sources = {"varmepumpe": input_kw} if input_kw else {}
        notes.append(load.observe(now, energy + sign * noise, sources, **kwargs))
        sign = -sign
        energy += (input_kw - draw_kw) * step / 3600
        now += step
    return notes


class MeasurementTest(unittest.TestCase):
    def test_a_quiet_window_gives_the_draw_back(self):
        # Ingen kilder, lageret taber 2,5 kW. Saa er det huset der tager dem.
        load = HouseLoad()

        drive(load, draw_kw=2.5)

        self.assertAlmostEqual(load.kw, 2.5, places=6)

    def test_the_sources_are_subtracted_not_ignored(self):
        # Varmepumpen giver 4 kW mens huset tager 2,5: lageret vokser med 1,5,
        # og forbruget er stadig 2,5.
        load = HouseLoad()

        drive(load, draw_kw=2.5, input_kw=4.0)

        self.assertAlmostEqual(load.kw, 2.5, places=6)

    def test_sensor_noise_does_not_move_the_answer(self):
        # +/- 0,05 K paa middeltemperaturen er mere end de ~0,04 K der er
        # regnet med. Haeldningen over tredive aflaesninger skal baere det.
        load = HouseLoad()

        drive(load, draw_kw=2.5, noise=0.05 * KWH_PER_K)

        self.assertLess(abs(load.kw - 2.5), 0.1)

    def test_a_short_window_says_it_is_waiting(self):
        load = HouseLoad()

        notes = drive(load, minutes=8)

        self.assertIsNone(load.kw)
        self.assertIn("maaler", notes[-1])

    def test_the_standby_loss_belongs_to_the_tanks_not_the_house(self):
        load = HouseLoad()

        drive(load, draw_kw=2.5, standby_kw=0.2)

        self.assertAlmostEqual(load.kw, 2.3, places=6)


class RejectionTest(unittest.TestCase):
    """Alt det der ligner en måling men ikke er en."""

    def test_a_bath_empties_the_same_tanks(self):
        # VVB og spa tapper bufferen, og en energibalance kan ikke se forskel
        # paa et brusebad og en radiator.
        load = HouseLoad()
        drive(load, minutes=20)

        self.assertIn("bad eller spa", load.observe(1260.0, 50.0, {}, dhw=True))

        # Vinduet begynder forfra: de naeste ti minutter er ikke nok til et
        # nyt tal. Den gamle maaling staar tilbage indtil den bliver for
        # gammel - det er ``kw_at`` der afgoer, ikke badet.
        after = drive(load, minutes=10, start_at=1320.0, start_energy=50.0)

        self.assertIn("maaler", after[-1])

    def test_the_spa_counts_too(self):
        load = HouseLoad()

        note = load.observe(0.0, 60.0, {}, spa=True)

        self.assertIn("bad eller spa", note)

    def test_a_heat_pump_without_a_cop_is_an_unknown_source(self):
        load = HouseLoad()

        note = load.observe(0.0, 60.0, {}, inputs_known=False)

        self.assertIn("COP", note)

    def test_a_sensor_falling_out_changes_the_basis(self):
        # Et lag der falder ud, skifter energigrundlaget midt i maalingen:
        # forskellen ville vaere foelerens og ikke husets.
        load = HouseLoad()
        drive(load, minutes=20, sensors=6)

        self.assertIn("foelere", load.observe(1260.0, 50.0, {}, sensors=5))

        after = drive(load, minutes=10, start_at=1320.0, start_energy=50.0, sensors=5)

        self.assertIn("maaler", after[-1])

    def test_a_gap_in_the_readings_starts_over(self):
        # Et hul betyder at vi ikke ved hvad der loeb ind imens.
        load = HouseLoad()
        drive(load, minutes=20)

        self.assertIn("hul", load.observe(9999.0, 50.0, {}))

        after = drive(load, minutes=10, start_at=10059.0, start_energy=50.0)

        self.assertIn("maaler", after[-1])

    def test_energy_appearing_from_nowhere_is_not_a_measurement(self):
        # Lageret vokser meget mere end kilderne kan forklare. Saa gik der
        # noget ind vi ikke saa, og det er ikke husets forbrug.
        load = HouseLoad()

        notes = drive(load, draw_kw=-3.0)

        self.assertIsNone(load.kw)
        self.assertTrue(any("forklarer" in n for n in notes), notes)

    def test_a_small_negative_is_just_zero(self):
        # Lidt stoej den forkerte vej er ikke et hus der leverer varme.
        load = HouseLoad()

        drive(load, draw_kw=-0.2)

        self.assertEqual(load.kw, 0.0)


class CurveTest(unittest.TestCase):
    def test_it_learns_a_point_per_degree(self):
        curve = LoadCurve()

        curve.learn(-2.0, 5.0)
        curve.learn(12.0, 1.5)

        self.assertEqual(curve.point_count, 2)
        self.assertAlmostEqual(curve.predict(-2), 5.0, places=6)

    def test_it_interpolates_between_points(self):
        curve = LoadCurve()
        curve.learn(0.0, 4.0)
        curve.learn(10.0, 2.0)

        self.assertAlmostEqual(curve.predict(5.0), 3.0, places=6)

    def test_it_extends_the_line_below_the_measured_range(self):
        # Husets tab er proportionalt med forskellen inde-ude, saa linjen maa
        # forlaenges. Varmekurven klemmer fast; den her har fysik bag sig.
        curve = LoadCurve()
        curve.learn(0.0, 4.0)
        curve.learn(10.0, 2.0)

        self.assertAlmostEqual(curve.predict(-5.0), 5.0, places=6)

    def test_it_never_extends_upwards(self):
        # To punkter der peger den forkerte vej, er stoej i belaegningen og
        # ikke et hus der bruger mere varme naar det bliver varmere.
        curve = LoadCurve()
        curve.learn(0.0, 2.0)
        curve.learn(10.0, 3.0)

        self.assertAlmostEqual(curve.predict(20.0), 3.0, places=6)
        self.assertAlmostEqual(curve.predict(-10.0), 2.0, places=6)

    def test_a_settled_point_does_not_jump(self):
        curve = LoadCurve()
        for _ in range(20):
            curve.learn(5.0, 3.0)

        curve.learn(5.0, 8.0)

        self.assertLess(curve.predict(5.0), 3.3)

    def test_it_survives_a_round_trip(self):
        curve = LoadCurve()
        curve.learn(3.0, 3.5)

        back = LoadCurve.from_raw(curve.to_raw())

        self.assertEqual(back.to_raw(), curve.to_raw())

    def test_rubbish_is_skipped_not_believed(self):
        back = LoadCurve.from_raw({"aeh": {"kw": 2}, "3": {"kw": "nej"}, "4": {"kw": -1}})

        self.assertEqual(back.point_count, 0)


class FallbackTest(unittest.TestCase):
    def test_a_fresh_measurement_beats_the_curve(self):
        load = HouseLoad()
        load.curve.learn(10.0, 9.9)
        drive(load, draw_kw=2.5, outdoor=10.0)

        self.assertAlmostEqual(load.kw_at(WINDOW_MINUTES * 60, 10.0), 2.5, places=6)

    def test_a_stale_measurement_gives_way_to_the_curve(self):
        # Uden ``outdoor`` laerer maalingen ikke af sig selv, saa kurvens
        # punkt staar urort og det er den der proeves her.
        load = HouseLoad()
        load.curve.learn(10.0, 4.0)
        drive(load, draw_kw=2.5)

        later = WINDOW_MINUTES * 60 + MAX_AGE_MINUTES * 60 + 1

        self.assertAlmostEqual(load.kw_at(later, 10.0), 4.0, places=6)

    def test_without_either_it_says_nothing(self):
        self.assertIsNone(HouseLoad().kw_at(0.0, 10.0))

    def test_the_curve_learns_once_per_window_not_once_per_minute(self):
        # Den rullende maaling regnes hvert minut, og de tredive tal beskriver
        # den samme halve time. Lærte kurven af dem alle, ville én aften se ud
        # som tredive aftener.
        load = HouseLoad()

        drive(load, minutes=WINDOW_MINUTES, draw_kw=2.5, outdoor=5.0)

        self.assertLessEqual(load.curve.sample_count, 2.0)

    def test_it_scores_itself_against_the_meter(self):
        load = HouseLoad()

        drive(load, draw_kw=2.5, meter_kw=2.3)

        self.assertIsNotNone(load.bias_kw)
        self.assertAlmostEqual(load.bias_kw, 0.2, places=2)


class StorageTest(unittest.TestCase):
    def test_only_the_curve_and_the_score_survive_a_restart(self):
        # Et igangvaerende vindue gemmes med vilje ikke: en genstart betyder
        # et hul i aflaesningerne. Samme valg som staatabsmaalingen.
        load = HouseLoad()
        drive(load, draw_kw=2.5, outdoor=5.0, meter_kw=2.4)

        back = HouseLoad.from_raw(load.to_raw())

        self.assertIsNone(back.kw)
        self.assertEqual(back.curve.to_raw(), load.curve.to_raw())
        self.assertAlmostEqual(back.bias_kw, load.bias_kw, places=6)

    def test_garbage_gives_an_empty_model(self):
        for junk in (None, "ikke en model", {}, {"curve": "aeh"}):
            self.assertEqual(HouseLoad.from_raw(junk).curve.point_count, 0)


if __name__ == "__main__":
    unittest.main()
