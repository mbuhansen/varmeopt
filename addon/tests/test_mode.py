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
        # Selv hvis setpunktet tilfaeldigvis staar paa 56: begge udgange er
        # slukket, saa det *er* varmedrift, og maalingen hoerer til i kurven.
        mode, dhw = _mode(dhw=False, spa=False, setpoint=56.0, curve=CURVE)

        self.assertEqual(mode, "varme")
        self.assertFalse(dhw)

    def test_without_sensors_we_fall_back_to_the_setpoint(self):
        mode, dhw = _mode(dhw=None, spa=None, setpoint=56.0, curve=CURVE)

        self.assertIn("gættet", mode)
        # None betyder "lad kurven afgoere det selv ud fra setpunktet".
        self.assertIsNone(dhw)

    def test_the_fallback_still_recognises_space_heating(self):
        mode, dhw = _mode(dhw=None, spa=None, setpoint=38.0, curve=CURVE)

        self.assertEqual(mode, "varme")
        self.assertIsNone(dhw)

    def test_one_sensor_answering_is_enough_to_be_certain(self):
        # Spaets foeler mangler, men varmtvandsudgangen svarer. Saa behoever
        # vi ikke gaette.
        mode, dhw = _mode(dhw=False, spa=None, setpoint=56.0, curve=CURVE)

        self.assertEqual(mode, "varme")
        self.assertFalse(dhw)

    def test_no_setpoint_and_no_sensors_gives_nothing(self):
        self.assertEqual(_mode(None, None, None, CURVE), (None, None))


class CurveTrustsTheFactTest(unittest.TestCase):
    def test_an_explicit_flag_beats_the_setpoint(self):
        curve = HeatCurve(dhw_setpoint=56.0)

        # 56 grader, men anlaegget siger at det ikke er varmt vand.
        note = curve.learn(outdoor=-8.0, setpoint=56.0, dhw=False)

        self.assertNotIn("ignoreret", note)
        self.assertEqual(curve.point_count, 1)

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
