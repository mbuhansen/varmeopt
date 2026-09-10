"""Opladningen som en blok.

Den 6. september tændte og slukkede flaget fem gange på en eftermiddag, fordi
beslutningen blev taget forfra hvert minut og `need` vippede omkring nul. En
kompressor må ikke behandles sådan — og rettelsen er ikke hysterese, men at
stille spørgsmålet én gang: find de billigste halvtimer inden prisen stiger,
læg dem fast, kør dem.
"""

import unittest
from dataclasses import dataclass

from varmeopt.charge import ChargePlan, _slot_start
from varmeopt.prices import Plan


@dataclass
class FakeDecision:
    planned_kwh: float | None = 12.0
    window_starts_in: int | None = 240
    window_minutes: int | None = 300
    source: str = "varmepumpe"
    dear_starts_in: int | None = None
    dear_span_minutes: int | None = None


def plan(*rates):
    """En plan hvor batteriet er bundet, saa importprisen gaelder direkte."""
    rows = [
        {"state": "holdchrg", "import_rate": rate, "export_rate": 50} for rate in rates
    ]
    return Plan.from_predbat({"raw": {"rows": rows}})


# Dyrt nu, billigt i slot 4-7, dyrt igen fra slot 8.
RATES = (90, 90, 90, 90, 35, 35, 35, 35, 155, 155, 155, 155)


class BlockTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(*RATES)
        self.charge = ChargePlan()

    def step(self, at=None, decision=None, rate=16.0, full=False):
        return self.charge.update(
            at if at is not None else self.now,
            decision if decision is not None else FakeDecision(),
            self.plan,
            rate,
            full=full,
        )

    def test_it_waits_for_the_cheapest_window(self):
        # De billige halvtimer begynder 120 minutter frem - regnet fra den
        # halvtime vi staar i, ikke fra det her sekund. Toleransen var foer
        # ét minut, og den skjulte praecis den drift.
        self.assertFalse(self.step())

        starts, ends = self.charge.slots()
        self.assertEqual(starts, _slot_start(self.now) + 120 * 60)
        self.assertAlmostEqual((ends - starts) / 60, 45, delta=1)

    def test_a_pending_block_does_not_drift_between_cycles(self):
        # Blokkens start laa foer paa ``now + offset``, hvor ``offset`` er
        # hele halvtimer fra den halvtime vi staar i. Den gled derfor ét
        # minut frem pr. cyklus og sprang 30 minutter tilbage ved hver :00 og
        # :30 - og en start der aldrig staar stille, kan ikke laeses.
        # Inden for den halvtime vi staar i. Krydser uret en halvtime, ruller
        # Predbats plan ogsaa en halvtime frem paa anlaegget, og saa er det
        # stadig det samme absolutte tidspunkt - men det kan attrappen her
        # ikke vise, for dens plan staar stille.
        seen = set()
        for minute in range(0, 26):
            self.step(at=self.now + minute * 60)
            slots = self.charge.slots()
            if slots is not None:
                seen.add(slots[0])

        self.assertEqual(len(seen), 1)

    def test_a_block_that_exactly_fills_the_window_still_fits(self):
        # Planlaeggeren kapper maengden med ``charge_kw * window / 60``, og
        # her regnes den tilbage: 16,95 kWh ved 11,3 kW er 90,000000000000014
        # minutter, som ``ceil`` goer til 91. Et vindue paa 90 minutter har
        # ikke plads til 91, og saa svarede den «ingen plads» - i 9,4 % af
        # alle cyklusser.
        want = 11.3 * 90 / 60

        self.charge.update(
            self.now,
            FakeDecision(planned_kwh=want, window_starts_in=90, window_minutes=90),
            self.plan,
            11.3,
        )

        self.assertIsNotNone(self.charge.slots())
        self.assertNotIn("ingen plads", self.charge.note)

    def test_no_room_this_minute_does_not_delete_a_waiting_block(self):
        # En blok der venter, er lagt paa priser vi har set efter. At der ikke
        # kan laegges en *ny* i det her minut, siger ingenting om den.
        self.step()
        waiting = self.charge.slots()
        self.assertIsNotNone(waiting)

        # Et vindue paa én halvtime, som en blok paa 45 min ikke kan ligge i.
        self.charge.update(
            self.now,
            FakeDecision(planned_kwh=12.0, window_starts_in=1, window_minutes=300),
            self.plan,
            16.0,
        )

        self.assertEqual(self.charge.slots(), waiting)

    def test_and_then_it_charges_without_flapping(self):
        # Kernen. Behovet vipper omkring nul minut for minut, praecis som den
        # 6. september - og flaget skal alligevel taende én gang og blive
        # taendt blokken ud.
        #
        # Tiden gaar rigtigt: planen skydes frem som halvtimerne gaar, og
        # fristen taeller ned. Ellers ville blokken skubbe sig selv foran sig.
        dear_at = 8 * 30  # de dyre halvtimer begynder her
        flags = []
        for minute in range(dear_at):
            want = 12.0 if minute % 2 == 0 else None
            slot = minute // 30
            flags.append(
                self.charge.update(
                    self.now + minute * 60,
                    FakeDecision(
                        planned_kwh=want,
                        window_starts_in=dear_at - minute,
                        window_minutes=dear_at - minute,
                    ),
                    plan(*RATES[slot:]),
                    16.0,
                )
            )

        starts = sum(1 for a, b in zip(flags, flags[1:]) if b and not a)
        self.assertEqual(starts, 1, f"flaget taendte {starts} gange")
        self.assertEqual(sum(flags), 45, "blokken skal vare 45 minutter")

    def test_a_slower_pump_gets_a_longer_block(self):
        # 12 kWh er 45 minutter ved 16 kW og 60 ved 12.
        self.step(rate=16.0)
        fast = self.charge.slots()

        self.charge = ChargePlan()
        self.step(rate=12.0)
        slow = self.charge.slots()

        self.assertAlmostEqual((fast[1] - fast[0]) / 60, 45, delta=1)
        self.assertAlmostEqual((slow[1] - slow[0]) / 60, 60, delta=1)

    def test_it_moves_while_it_waits(self):
        # En blok der ikke er begyndt, er ikke et loefte: bliver en anden
        # halvtime billigere, flytter den sig.
        self.step()
        first = self.charge.slots()[0]

        self.plan = plan(90, 20, 20, 90, 35, 35, 35, 35, 155, 155, 155, 155)
        self.step()

        self.assertLess(self.charge.slots()[0], first)


class InterruptionTest(unittest.TestCase):
    """Kun to ting maa bryde en igangvaerende blok."""

    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(35, 35, 35, 155, 155, 155)
        self.charge = ChargePlan()

    def start(self):
        self.assertTrue(
            self.charge.update(
                self.now, FakeDecision(window_starts_in=90, window_minutes=90),
                self.plan, 16.0,
            )
        )

    def full_at(self, seconds, **over):
        return self.charge.update(
            self.now + seconds, FakeDecision(), self.plan, 16.0, full=True, **over
        )

    def test_one_full_reading_does_not_stop_a_running_block(self):
        # ``headroom`` er en sum over otte termometre. Ét udsving maa ikke
        # afslutte en opladning - og braende straekket med, saa der ikke kan
        # laegges en ny.
        self.start()

        self.assertTrue(self.full_at(300))
        self.assertIsNotNone(self.charge.slots())

    def test_three_minutes_of_a_full_store_does(self):
        self.start()
        self.full_at(300)

        self.assertFalse(self.full_at(300 + 180))
        self.assertIn("fuldt", self.charge.note)

    def test_a_flicker_of_full_starts_the_patience_over(self):
        self.start()
        self.full_at(300)
        # Ikke fuldt igen - taelleren nulstilles.
        self.charge.update(self.now + 360, FakeDecision(), self.plan, 16.0)

        self.assertTrue(self.full_at(300 + 180))
        self.assertIsNotNone(self.charge.slots())

    def test_the_pellet_boiler_winning_stops_it(self):
        self.start()

        self.assertFalse(
            self.charge.update(
                self.now + 300, FakeDecision(source="pillefyr"), self.plan, 16.0
            )
        )
        self.assertIn("pillefyret", self.charge.note)

    def test_the_guarded_source_is_what_counts(self):
        # Vagten holder varmepumpen i femten minutter; planlaeggerens raa
        # svar vipper paa nogle oere. Det er vagtens svar der gaelder.
        self.start()

        self.assertTrue(
            self.charge.update(
                self.now + 300,
                FakeDecision(source="pillefyr"),
                self.plan,
                16.0,
                source="varmepumpe",
            )
        )

    def test_a_block_is_not_stopped_inside_its_minimum_runtime(self):
        # Kortcykling slider. En blok der lige er startet, afsluttes ikke
        # fordi pillefyret vandt ét minut.
        self.start()

        self.assertTrue(
            self.charge.update(
                self.now + 300,
                FakeDecision(source="pillefyr"),
                self.plan,
                16.0,
                min_runtime_minutes=15,
            )
        )

    def test_but_a_full_store_goes_before_the_minimum_runtime(self):
        # Der er ingen varme at levere ind i et fuldt lager, saa der er heller
        # ikke noget at beskytte.
        self.start()
        self.full_at(60, min_runtime_minutes=15)

        self.assertFalse(self.full_at(60 + 180, min_runtime_minutes=15))

    def test_nothing_else_does(self):
        # Behovet forsvinder midt i blokken. Den koerer alligevel faerdig.
        self.start()

        self.assertTrue(
            self.charge.update(
                self.now + 300, FakeDecision(planned_kwh=None), self.plan, 16.0
            )
        )


class OnceTest(unittest.TestCase):
    """Ét billigt vindue giver én opladning."""

    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(35, 35, 155, 155, 155, 155)
        self.charge = ChargePlan()
        self.decision = FakeDecision(planned_kwh=8.0, window_starts_in=60, window_minutes=60)

    def at(self, minute):
        """Beslutningen som den ser ud det minut - fristen taeller ned."""
        return FakeDecision(
            planned_kwh=8.0,
            window_starts_in=max(1, 60 - minute),
            window_minutes=max(1, 60 - minute),
        )

    def run_block(self):
        for minute in (0, 30, 45, 60):
            self.charge.update(
                self.now + minute * 60, self.at(minute), self.plan, 16.0
            )

    def test_it_does_not_charge_twice_against_the_same_top(self):
        self.run_block()

        self.assertFalse(
            self.charge.update(self.now + 61 * 60, self.at(61), self.plan, 16.0)
        )
        self.assertIn("allerede ladet op", self.charge.note)
        self.assertIsNone(self.charge.slots())

    def test_but_a_new_top_gets_its_own_block(self):
        self.run_block()

        # Fire timer senere er der en ny pristop forude.
        later = self.now + 4 * 3600
        self.charge.update(later, self.at(0), self.plan, 16.0)

        self.assertIsNotNone(self.charge.slots())


class OncePerStretchTest(unittest.TestCase):
    """Ét dyrt straek giver én opladning - ogsaa naar toppen vandrer.

    Reproduktionen af den 9. september. Flaget taendte og slukkede otte gange,
    fordi spaerren kendte straekket paa dets *dyreste* halvtime. Pilleloftet
    goer alle dyre halvtimer lige dyre, den tidligste vinder, og naar den
    bliver til «nu», arver den naeste titlen. Straekket selv rykkede sig ikke
    en tomme.
    """

    def setUp(self):
        self.now = 1_757_000_000.0
        # Ti billige halvtimer, saa dyrt resten af vejen.
        self.plan = plan(*([35] * 10 + [155] * 14))
        self.charge = ChargePlan()

    def at(self, minute, top):
        """Straekket staar stille; ``window_minutes`` vandrer."""
        return FakeDecision(
            planned_kwh=8.0,
            window_starts_in=max(1, 300 - minute),
            # Den dyreste halvtime - den der flyttede sig hver halve time.
            window_minutes=max(1, top - minute),
            dear_starts_in=max(1, 300 - minute),
            dear_span_minutes=240,
        )

    def run_block(self):
        for minute in (0, 15, 30, 31):
            self.charge.update(
                self.now + minute * 60, self.at(minute, 300), self.plan, 16.0
            )

    def test_a_wandering_dearest_half_hour_does_not_open_a_new_block(self):
        self.run_block()
        self.assertIsNone(self.charge.slots())

        # Toppen vandrer en halvtime ad gangen gennem straekket, praecis som
        # den gjorde den 9. september. Straekket er det samme, saa der maa
        # ikke laegges en ny blok.
        for minute, top in ((60, 330), (90, 360), (120, 390), (180, 450)):
            self.assertFalse(
                self.charge.update(
                    self.now + minute * 60, self.at(minute, top), self.plan, 16.0
                )
            )
            self.assertIn("allerede ladet op", self.charge.note)
            self.assertIsNone(self.charge.slots())

    def test_a_new_stretch_may_be_charged_for(self):
        # Modtesten, og den er lige saa vigtig: to dyre straek med billige
        # timer imellem - en dyr morgen og en dyr aften - skal give to
        # blokke, én inden hver. Ellers er spaerren bare blevet til
        # «én om dagen».
        self.run_block()

        # Aftenens straek: begynder 100 min efter det foerste er forbi.
        later = FakeDecision(
            planned_kwh=8.0,
            window_starts_in=100,
            window_minutes=100,
            dear_starts_in=100,
            dear_span_minutes=120,
        )
        self.charge.update(self.now + 600 * 60, later, plan(*([35] * 4 + [155] * 8)), 16.0)

        self.assertIsNotNone(self.charge.slots())
        self.assertNotIn("allerede ladet op", self.charge.note)


class StorageTest(unittest.TestCase):
    def test_a_running_block_survives_a_restart(self):
        # En genstart midt i en opladning maa ikke starte kompressoren forfra
        # paa den anden side.
        now = 1_757_000_000.0
        charge = ChargePlan()
        charge.update(
            now, FakeDecision(window_starts_in=90, window_minutes=90), plan(35, 35, 155, 155), 16.0
        )

        back = ChargePlan.from_raw(charge.to_raw())

        self.assertEqual(back.slots(), charge.slots())

    def test_garbage_gives_an_empty_plan(self):
        for junk in (None, "aeh", {}, {"block": "nej"}, {"block": {"kwh": -1}}):
            self.assertIsNone(ChargePlan.from_raw(junk).slots())


if __name__ == "__main__":
    unittest.main()
