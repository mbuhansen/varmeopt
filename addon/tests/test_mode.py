import unittest

from varmeopt.__main__ import _mode
from varmeopt.curve import HeatCurve

CURVE = HeatCurve(dhw_setpoint=56.0)


class ModeTest(unittest.TestCase):
    """Hvad varmepumpen laver, og om målingen hører til i varmekurven."""

    def test_the_hot_water_output_is_a_fact(self):
        mode, dhw = _mode(dhw=True, spa=False, setpoint=42.0, curve=CURVE)

        # Setpunktet siger 42, men udgangen staar taendt. Udgangen vinder.
        self.assertEqual(mode, "varmt vand")
        self.assertTrue(dhw)

    def test_the_spa_is_told_apart_from_the_tank(self):
        mode, dhw = _mode(dhw=False, spa=True, setpoint=56.0, curve=CURVE)

        self.assertEqual(mode, "spa")
        self.assertTrue(dhw)

    def test_both_at_once_says_both(self):
        mode, _ = _mode(dhw=True, spa=True, setpoint=56.0, curve=CURVE)

        self.assertEqual(mode, "varmt vand + spa")

    def test_neither_output_on_is_space_heating(self):
        mode, dhw = _mode(dhw=False, spa=False, setpoint=38.0, curve=CURVE)

        self.assertEqual(mode, "varme")
        self.assertFalse(dhw)

    def test_a_negative_flag_does_not_make_56_degrees_the_weather(self):
        # Her stod det modsatte, og det var fejlen: udgangen kan staa paa nul
        # mens spaen varmer, og saa blev 56 °C laert som om huset havde bedt
        # om det. Ved 19 °C ude kom kurven til at staa paa 44 i stedet for 27.
        mode, dhw = _mode(dhw=False, spa=False, setpoint=56.0, curve=CURVE)

        self.assertIn("varmt vand", mode)
        self.assertTrue(dhw)

    def test_without_sensors_we_fall_back_to_the_setpoint(self):
        mode, dhw = _mode(dhw=None, spa=None, setpoint=56.0, curve=CURVE)

        self.assertIn("gættet", mode)
        self.assertTrue(dhw)

    def test_the_fallback_still_recognises_space_heating(self):
        mode, dhw = _mode(dhw=None, spa=None, setpoint=38.0, curve=CURVE)

        self.assertEqual(mode, "varme")
        self.assertFalse(dhw)

    def test_a_setpoint_far_above_the_curve_is_hot_water_wherever_it_sits(self):
        # 44 °C er husets rigtige fremloeb om vinteren, men ved 19 °C ude
        # beder huset om 27. Det er den slags varmtvand der ikke kan kendes
        # paa vaerdien - kun paa stedet.
        curve = HeatCurve(dhw_setpoint=56.0)
        for _ in range(10):
            curve.learn(outdoor=19.0, setpoint=27.0)

        mode, dhw = _mode(
            dhw=False, spa=False, setpoint=44.0, curve=curve, outdoor=19.0
        )

        self.assertTrue(dhw)
        self.assertIn("varmt vand", mode)

    def test_no_setpoint_and_no_sensors_gives_nothing(self):
        self.assertEqual(_mode(None, None, None, CURVE), (None, None))


class CurveTrustsTheFactTest(unittest.TestCase):
    def test_a_flag_may_add_suspicion_but_never_remove_it(self):
        # Et *ja* fra anlaegget er en kendsgerning. Et *nej* er ikke den samme
        # slags: udgangen kan staa paa nul mens spaen varmer. Her stod
        # ``if self.is_dhw(setpoint) if dhw is None else dhw``, og det lod
        # nejet slaa vaerditjekket fra.
        curve = HeatCurve(dhw_setpoint=56.0)

        note = curve.learn(outdoor=-8.0, setpoint=56.0, dhw=False)

        self.assertIn("varmtvand", note)
        self.assertEqual(curve.point_count, 0)

    def test_and_can_also_exclude_a_normal_looking_setpoint(self):
        curve = HeatCurve(dhw_setpoint=56.0)

        note = curve.learn(outdoor=5.0, setpoint=42.0, dhw=True)

        self.assertIn("varmtvand", note)
        self.assertEqual(curve.point_count, 0)

    def test_without_the_flag_the_setpoint_decides(self):
        curve = HeatCurve(dhw_setpoint=56.0)

        curve.learn(outdoor=5.0, setpoint=56.0)
        curve.learn(outdoor=5.0, setpoint=42.0)

        self.assertEqual(curve.point_count, 1)


if __name__ == "__main__":
    unittest.main()
