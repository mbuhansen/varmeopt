import unittest

from varmeopt.guard import Guard
from varmeopt.planner import Decision
from varmeopt.prices import Plan

PELLET = 0.706
LOOKUP = object()  # vagten bruger den kun til at se at der *er* en COP


def decision(source="varmepumpe", heat_price=0.30):
    return Decision(source=source, heat_price=heat_price, pellet_price=PELLET)


def plan(rows=2):
    data = [{"state": "holdchrg", "import_rate": 100, "export_rate": 50}] * rows
    return Plan.from_predbat({"raw": {"rows": data}})


def guard(**over):
    values = dict(enabled=True, min_dwell_minutes=15.0, warmup_minutes=5.0)
    values.update(over)
    g = Guard(**values)
    g.start(0.0)
    return g


MIN = 60.0  # sekunder


class DisabledTest(unittest.TestCase):
    def test_control_is_off_by_default(self):
        g = Guard()
        g.start(0.0)

        cmd = g.check(decision(), LOOKUP, plan(), now=100 * MIN)

        self.assertFalse(cmd.acting)
        self.assertIn("slået fra", cmd.reason)

    def test_the_decision_is_still_reported_when_off(self):
        # Vagten siger ikke hvad der skal goeres - kun om nogen boer goere det.
        g = Guard()
        g.start(0.0)

        cmd = g.check(decision("pillefyr"), LOOKUP, plan(), now=100 * MIN)

        self.assertEqual(cmd.source, "pillefyr")


class WarmupTest(unittest.TestCase):
    def test_nothing_is_commanded_while_warming_up(self):
        # Lige efter opstart er tilstanden den mindst oplyste vi har.
        g = guard()

        cmd = g.check(decision(), LOOKUP, plan(), now=2 * MIN)

        self.assertFalse(cmd.acting)
        self.assertIn("varmer op", cmd.reason)

    def test_control_begins_when_warmup_is_over(self):
        g = guard()

        cmd = g.check(decision(), LOOKUP, plan(), now=6 * MIN)

        self.assertTrue(cmd.acting)
        self.assertEqual(cmd.source, "varmepumpe")
        self.assertIn("overtager", cmd.reason)


class SafetyTest(unittest.TestCase):
    def test_no_cop_means_no_control(self):
        g = guard()

        cmd = g.check(decision(), None, plan(), now=10 * MIN)

        self.assertFalse(cmd.acting)
        self.assertIn("ingen COP", cmd.reason)

    def test_a_decision_without_a_heat_price_is_not_acted_on(self):
        g = guard()

        cmd = g.check(decision(heat_price=None), LOOKUP, plan(), now=10 * MIN)

        self.assertFalse(cmd.acting)

    def test_a_missing_plan_still_allows_the_source_choice(self):
        # Planlaeggeren er bygget til at svare uden en plan. Det skal bare
        # siges, saa det ikke ligner mere end det er.
        g = guard()

        cmd = g.check(decision(), LOOKUP, None, now=10 * MIN)

        self.assertTrue(cmd.acting)
        self.assertIn("uden plan", cmd.reason)


class DwellTest(unittest.TestCase):
    def setUp(self):
        self.g = guard()
        self.g.check(decision("varmepumpe"), LOOKUP, plan(), now=6 * MIN)

    def test_the_same_source_passes_straight_through(self):
        cmd = self.g.check(decision("varmepumpe"), LOOKUP, plan(), now=7 * MIN)

        self.assertTrue(cmd.acting)
        self.assertIn("uændret", cmd.reason)

    def test_a_switch_too_soon_is_held(self):
        # Hysteresen daemper prisstoej; det her saetter en bund under hvor tit
        # kilden overhovedet faar lov at skifte.
        cmd = self.g.check(decision("pillefyr"), LOOKUP, plan(), now=12 * MIN)

        self.assertTrue(cmd.acting)
        self.assertEqual(cmd.source, "varmepumpe")
        self.assertIn("holder", cmd.reason)

    def test_the_switch_goes_through_once_the_dwell_has_passed(self):
        cmd = self.g.check(decision("pillefyr"), LOOKUP, plan(), now=22 * MIN)

        self.assertTrue(cmd.acting)
        self.assertEqual(cmd.source, "pillefyr")
        self.assertIn("skifter", cmd.reason)

    def test_the_dwell_restarts_after_a_switch(self):
        self.g.check(decision("pillefyr"), LOOKUP, plan(), now=22 * MIN)

        cmd = self.g.check(decision("varmepumpe"), LOOKUP, plan(), now=30 * MIN)

        self.assertEqual(cmd.source, "pillefyr")
        self.assertIn("holder", cmd.reason)

    def test_releasing_makes_the_next_take_over_start_fresh(self):
        self.g.release()

        cmd = self.g.check(decision("pillefyr"), LOOKUP, plan(), now=7 * MIN)

        self.assertEqual(cmd.source, "pillefyr")
        self.assertIn("overtager", cmd.reason)


class ShapeTest(unittest.TestCase):
    def test_the_note_reads_sensibly(self):
        g = guard()

        self.assertIn("varmepumpe", g.check(decision(), LOOKUP, plan(), 6 * MIN).note)


if __name__ == "__main__":
    unittest.main()
