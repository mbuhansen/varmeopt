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
        # Vagten siger ikke hvad der skal gøres - kun om nogen bør gøre det.
        g = Guard()
        g.start(0.0)

        cmd = g.check(decision("pillefyr"), LOOKUP, plan(), now=100 * MIN)

        self.assertEqual(cmd.source, "pillefyr")

    def test_the_binding_is_kept_even_though_nobody_is_steering(self):
        # Det her er hele grunden til at bindingen regnes før ``enabled``
        # ses på: entiteten er det Node-RED hænger sin
        # ``server-state-changed`` på, og den skal være rolig uanset hvem
        # der styrer. Før stod den på planlæggerens rå svar hvert minut.
        g = Guard(confirm_minutes=0.0)
        g.start(0.0)
        g.check(decision("varmepumpe"), LOOKUP, plan(), now=100 * MIN)

        cmd = g.check(decision("pillefyr"), LOOKUP, plan(), now=105 * MIN)

        self.assertFalse(cmd.acting)
        self.assertEqual(cmd.source, "varmepumpe")
        self.assertIn("holder", cmd.reason)


class BindingGatesTest(unittest.TestCase):
    """Bindingen må ikke sættes på et dårligt oplyst svar."""

    def test_no_cop_does_not_bind(self):
        # ``source_now`` svarer "varmepumpe" som standard når COP mangler.
        # Bandt vi os til det, kunne en forældet Predbat-plan låse
        # anlægget på et prisløst gæt et kvarter.
        g = guard()

        g.check(decision(), None, plan(), now=10 * MIN)

        self.assertIsNone(g.committed)

    def test_warming_up_does_not_bind(self):
        g = guard()

        g.check(decision(), LOOKUP, plan(), now=2 * MIN)

        self.assertIsNone(g.committed)


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
        # Planlæggeren er bygget til at svare uden en plan. Det skal bare
        # siges, så det ikke ligner mere end det er.
        g = guard()

        cmd = g.check(decision(), LOOKUP, None, now=10 * MIN)

        self.assertTrue(cmd.acting)
        self.assertIn("uden plan", cmd.reason)


class ConfirmTest(unittest.TestCase):
    """Et ét-minuts udsving må ikke kunne binde vagten.

    Uden bekræftelsen gør hviletiden støjen *værre*: er den for længst
    udløbet, binder vagten sig til fejlen i samme øjeblik og holder den et
    helt kvarter. Et minuts støj blev til femten.
    """

    def setUp(self):
        self.g = guard(confirm_minutes=3.0)
        self.g.check(decision("varmepumpe"), LOOKUP, plan(), now=6 * MIN)

    def test_a_single_cycle_of_noise_is_refused(self):
        cmd = self.g.check(decision("pillefyr"), LOOKUP, plan(), now=60 * MIN)

        self.assertEqual(cmd.source, "varmepumpe")
        self.assertIn("afventer bekræftelse", cmd.reason)

    def test_and_it_does_not_restart_the_dwell(self):
        # Kernen i det hele: efter udsvinget skal den bundne kilde stå
        # uberørt, så et ægte skifte bagefter ikke er blevet forsinket.
        self.g.check(decision("pillefyr"), LOOKUP, plan(), now=60 * MIN)
        committed_at = self.g.committed_at

        self.g.check(decision("varmepumpe"), LOOKUP, plan(), now=61 * MIN)

        self.assertEqual(self.g.committed_at, committed_at)
        self.assertIsNone(self.g.pending)

    def test_a_source_that_keeps_asking_gets_through(self):
        for minute in (60, 61, 62, 63):
            cmd = self.g.check(decision("pillefyr"), LOOKUP, plan(), now=minute * MIN)

        self.assertEqual(cmd.source, "pillefyr")
        self.assertIn("skifter", cmd.reason)

    def test_the_wait_survives_a_restart(self):
        self.g.check(decision("pillefyr"), LOOKUP, plan(), now=60 * MIN)

        after = guard(confirm_minutes=3.0)
        after.restore(self.g.to_raw())
        cmd = after.check(decision("pillefyr"), LOOKUP, plan(), now=64 * MIN)

        self.assertEqual(cmd.source, "pillefyr")
        self.assertIn("skifter", cmd.reason)


class DwellTest(unittest.TestCase):
    # Bekræftelsen er slået fra her, så hver test handler om én ting.
    # ``ConfirmTest`` ovenfor dækker den anden halvdel.
    def setUp(self):
        self.g = guard(confirm_minutes=0.0)
        self.g.check(decision("varmepumpe"), LOOKUP, plan(), now=6 * MIN)

    def test_the_same_source_passes_straight_through(self):
        cmd = self.g.check(decision("varmepumpe"), LOOKUP, plan(), now=7 * MIN)

        self.assertTrue(cmd.acting)
        self.assertIn("uændret", cmd.reason)

    def test_a_switch_too_soon_is_held(self):
        # Hysteresen dæmper prisstøj; det her sætter en bund under hvor tit
        # kilden overhovedet får lov at skifte.
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
