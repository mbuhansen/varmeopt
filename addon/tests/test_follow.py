"""Følger anlægget ladeflaget - starter kompressoren når der bliver sagt lad op?"""

import unittest

from varmeopt.follow import AGAINST_PELLET, NOT_STARTED, FollowWatch

T0 = 1_764_000_000.0


class FollowWatchTest(unittest.TestCase):
    def run_minutes(self, watch, start, minutes, **kw):
        lines = []
        for m in range(minutes):
            lines += watch.observe(T0 + (start + m) * 60, **kw)
        return lines

    def charge(self, running):
        return dict(ordered=True, acting=True, running=running, source="varmepumpe")

    def test_pump_never_starts_warns_once_after_grace(self):
        watch = FollowWatch(grace_minutes=20)
        lines = self.run_minutes(watch, 0, 30, **self.charge(False))
        self.assertEqual(len(lines), 1)
        self.assertIn("varmepumpen kører", lines[0])
        self.assertEqual(watch.active()[0][0], "advarsel")

    def test_start_before_grace_is_just_a_delay(self):
        watch = FollowWatch(grace_minutes=20)
        lines = self.run_minutes(watch, 0, 6, **self.charge(False))
        lines += self.run_minutes(watch, 6, 5, **self.charge(True))
        self.assertEqual(lines, [])
        self.assertEqual(watch.active(), [])
        self.assertEqual(watch.events, [])
        self.assertEqual(watch.delays, [6.0])
        self.assertIn("typisk 6 min", watch.note)

    def test_late_start_closes_event_with_delay(self):
        watch = FollowWatch(grace_minutes=20)
        self.run_minutes(watch, 0, 25, **self.charge(False))
        self.run_minutes(watch, 25, 2, **self.charge(True))
        self.assertEqual(watch.active(), [])
        self.assertEqual(len(watch.events), 1)
        self.assertEqual(watch.events[0].kind, NOT_STARTED)
        self.assertIn("startede efter 25 min", watch.events[0].outcome)
        self.assertEqual(watch.delays, [25.0])

    def test_order_withdrawn_before_start(self):
        watch = FollowWatch(grace_minutes=20)
        self.run_minutes(watch, 0, 25, **self.charge(False))
        self.run_minutes(
            watch, 25, 1, ordered=False, acting=True, running=False, source="varmepumpe"
        )
        self.assertIn("faldt bort", watch.events[0].outcome)
        self.assertEqual(watch.delays, [])

    def test_not_acting_is_no_order(self):
        watch = FollowWatch(grace_minutes=20)
        lines = self.run_minutes(
            watch, 0, 60, ordered=True, acting=False, running=False, source="varmepumpe"
        )
        self.assertEqual(lines, [])
        self.assertEqual(watch.active(), [])

    def test_unknown_power_is_not_judged(self):
        watch = FollowWatch(grace_minutes=20)
        lines = self.run_minutes(watch, 0, 60, **self.charge(None))
        self.assertEqual(lines, [])
        self.assertEqual(watch.active(), [])

    def test_gap_resets_the_clock(self):
        watch = FollowWatch(grace_minutes=20)
        self.run_minutes(watch, 0, 15, **self.charge(False))
        # Add-on'en var nede i en time; femten minutter mere er ikke tyve.
        lines = self.run_minutes(watch, 75, 15, **self.charge(False))
        self.assertEqual(lines, [])
        self.assertEqual(watch.active(), [])

    def test_the_uvr_overlap_after_a_switch_to_pellet_is_not_a_remark(self):
        # UVR'en lader pumpen køre op til 20 min efter skiftet, mens fyret
        # varmer op - med vilje. Det skal ikke stå på forsiden.
        watch = FollowWatch(grace_minutes=20)
        self.run_minutes(
            watch, 0, 25, ordered=False, acting=True, running=True, source="pillefyr"
        )
        self.assertEqual(watch.active(), [])
        self.run_minutes(
            watch, 25, 1, ordered=False, acting=True, running=False, source="pillefyr"
        )
        self.assertEqual(watch.events, [])

    def test_pump_running_on_pellet_is_a_remark(self):
        watch = FollowWatch(grace_minutes=20)
        lines = self.run_minutes(
            watch, 0, 35, ordered=False, acting=True, running=True, source="pillefyr"
        )
        self.assertEqual(lines, [])  # kun en bemærkning, ikke en advarsel i loggen
        self.assertEqual(watch.active()[0][0], "bemærkning")
        self.run_minutes(
            watch, 35, 1, ordered=False, acting=True, running=False, source="pillefyr"
        )
        self.assertEqual(watch.events[-1].kind, AGAINST_PELLET)
        self.assertEqual(watch.events[-1].outcome, "pumpen stoppede")

    def test_a_long_stretch_without_power_is_a_gap(self):
        # To timer uden effektmåling, så starter pumpen. Det er ikke en
        # ventetid på 121 min.
        watch = FollowWatch(grace_minutes=20)
        self.run_minutes(watch, 0, 1, **self.charge(False))
        self.run_minutes(watch, 1, 120, **self.charge(None))
        self.run_minutes(watch, 121, 2, **self.charge(True))
        self.assertEqual(watch.delays, [])
        self.assertEqual(watch.active(), [])

    def test_a_stop_mid_charge_is_not_a_failed_start(self):
        # Pumpen starter, kører 10 min og stopper på sin egen termostat mens
        # flaget står. Det er ikke «startede ikke», og genstarten er ikke en
        # ventetid.
        watch = FollowWatch(grace_minutes=20)
        self.run_minutes(watch, 0, 3, **self.charge(False))
        self.run_minutes(watch, 3, 10, **self.charge(True))
        lines = self.run_minutes(watch, 13, 30, **self.charge(False))
        self.run_minutes(watch, 43, 2, **self.charge(True))
        self.assertEqual(lines, [])
        self.assertEqual(watch.delays, [3.0])

    def test_the_low_flag_is_named_and_timed_on_its_own(self):
        watch = FollowWatch(grace_minutes=20)
        low = dict(ordered=True, acting=True, source="varmepumpe", low=True)
        lines = self.run_minutes(watch, 0, 25, running=False, **low)
        self.assertIn("lad op lav", lines[0])
        self.assertIn("Lad op lav", watch.active()[0][1])
        self.run_minutes(watch, 25, 1, running=True, **low)
        self.assertEqual(watch.delays_low, [25.0])
        self.assertEqual(watch.delays, [])

    def test_round_trip(self):
        watch = FollowWatch(grace_minutes=20)
        self.run_minutes(watch, 0, 25, **self.charge(False))
        self.run_minutes(watch, 25, 1, **self.charge(True))
        back = FollowWatch.from_raw(watch.to_raw())
        self.assertEqual(back.delays, watch.delays)
        self.assertEqual([e.outcome for e in back.events], [e.outcome for e in watch.events])
        self.assertEqual(FollowWatch.from_raw({"events": [{"x": 1}], "delays": ["a"]}).events, [])


class SelfcheckSectionTest(unittest.TestCase):
    def test_a_warning_lands_on_the_front_page(self):
        from varmeopt.web import _selfcheck_section

        watch = FollowWatch(grace_minutes=20)
        for m in range(25):
            watch.observe(T0 + m * 60, ordered=True, acting=True, running=False,
                          source="varmepumpe")
        html = _selfcheck_section(
            {"follow": watch.active(), "follow_note": watch.note}, None
        )
        self.assertIn("Selvkontrol", html)
        self.assertIn('class="warn"', html)
        self.assertIn("Node-RED-flowet", html)

    def test_all_well_is_one_line(self):
        from varmeopt.web import _selfcheck_section

        html = _selfcheck_section({"follow_note": "ordren følges"}, None)
        self.assertNotIn('class="warn"', html)
        self.assertIn("ordren følges", html)


if __name__ == "__main__":
    unittest.main()
