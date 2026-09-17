"""Opladningen som en blok.

Den 6. september tændte og slukkede flaget fem gange på en eftermiddag, fordi
beslutningen blev taget forfra hvert minut og `need` vippede omkring nul. En
kompressor må ikke behandles sådan — og rettelsen er ikke hysterese, men at
stille spørgsmålet én gang: find de billigste halvtimer inden prisen stiger,
læg dem fast, kør dem.
"""

import unittest
from dataclasses import dataclass

from varmeopt.charge import ChargePlan, slot_start
from varmeopt.prices import Plan


@dataclass
class FakeDecision:
    planned_kwh: float | None = 12.0
    window_starts_in: int | None = 240
    window_minutes: int | None = 300
    source: str = "varmepumpe"
    dear_starts_in: int | None = None
    dear_span_minutes: int | None = None
    dear_ends_in: int | None = None


def plan(*rates):
    """En plan hvor batteriet er bundet, så importprisen gælder direkte."""
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
        # halvtime vi står i, ikke fra det her sekund. Toleransen var før
        # ét minut, og den skjulte præcis den drift.
        self.assertFalse(self.step())

        starts, ends = self.charge.slots()
        self.assertEqual(starts, slot_start(self.now) + 120 * 60)
        self.assertAlmostEqual((ends - starts) / 60, 45, delta=1)

    def test_a_pending_block_does_not_drift_between_cycles(self):
        # Blokkens start lå før på ``now + offset``, hvor ``offset`` er
        # hele halvtimer fra den halvtime vi står i. Den gled derfor ét
        # minut frem pr. cyklus og sprang 30 minutter tilbage ved hver :00 og
        # :30 - og en start der aldrig står stille, kan ikke læses.
        # Inden for den halvtime vi står i. Krydser uret en halvtime, ruller
        # Predbats plan også en halvtime frem på anlægget, og så er det
        # stadig det samme absolutte tidspunkt - men det kan attrappen her
        # ikke vise, for dens plan står stille.
        seen = set()
        for minute in range(0, 26):
            self.step(at=self.now + minute * 60)
            slots = self.charge.slots()
            if slots is not None:
                seen.add(slots[0])

        self.assertEqual(len(seen), 1)

    def test_a_block_that_exactly_fills_the_window_still_fits(self):
        # Planlæggeren kapper mængden med ``charge_kw * window / 60``, og
        # her regnes den tilbage: 16,95 kWh ved 11,3 kW er 90,000000000000014
        # minutter, som ``ceil`` gør til 91. Et vindue på 90 minutter har
        # ikke plads til 91, og så svarede den «ingen plads» - i 9,4 % af
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
        # En blok der venter, er lagt på priser vi har set efter. At der ikke
        # kan lægges en *ny* i det her minut, siger ingenting om den.
        self.step()
        waiting = self.charge.slots()
        self.assertIsNotNone(waiting)

        # Et vindue på én halvtime, som en blok på 45 min ikke kan ligge i.
        self.charge.update(
            self.now,
            FakeDecision(planned_kwh=12.0, window_starts_in=1, window_minutes=300),
            self.plan,
            16.0,
        )

        self.assertEqual(self.charge.slots(), waiting)

    def test_and_then_it_charges_without_flapping(self):
        # Kernen. Behovet vipper omkring nul minut for minut, præcis som den
        # 6. september - og flaget skal alligevel tænde én gang og blive
        # tændt blokken ud.
        #
        # Tiden går rigtigt: planen skydes frem som halvtimerne går, og
        # fristen tæller ned. Ellers ville blokken skubbe sig selv foran sig.
        #
        # Og i planens ramme: uret begynder på en halvtime, og fristen tæller
        # fra starten af den halvtime vi står i. Her talte fristen fra nu,
        # mens planen rullede på ``minute // 30`` og uret stod 3 min 20 s inde
        # i halvtimen - de to blandede rammer som blokken ikke længere tåler.
        start = slot_start(self.now)
        dear_at = 8 * 30  # de dyre halvtimer begynder her
        flags = []
        for minute in range(dear_at):
            want = 12.0 if minute % 2 == 0 else None
            slot = minute // 30
            flags.append(
                self.charge.update(
                    start + minute * 60,
                    FakeDecision(
                        planned_kwh=want,
                        window_starts_in=dear_at - slot * 30,
                        window_minutes=dear_at - slot * 30,
                    ),
                    plan(*RATES[slot:]),
                    16.0,
                )
            )

        starts = sum(1 for a, b in zip(flags, flags[1:]) if b and not a)
        self.assertEqual(starts, 1, f"flaget tændte {starts} gange")
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
        # En blok der ikke er begyndt, er ikke et løfte: bliver en anden
        # halvtime billigere, flytter den sig.
        self.step()
        first = self.charge.slots()[0]

        self.plan = plan(90, 20, 20, 90, 35, 35, 35, 35, 155, 155, 155, 155)
        self.step()

        self.assertLess(self.charge.slots()[0], first)


class InterruptionTest(unittest.TestCase):
    """Kun to ting må bryde en igangværende blok."""

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
        # ``headroom`` er en sum over otte termometre. Ét udsving må ikke
        # afslutte en opladning - og brænde strækket med, så der ikke kan
        # lægges en ny.
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
        # Ikke fuldt igen - tælleren nulstilles.
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
        # Vagten holder varmepumpen i femten minutter; planlæggerens rå
        # svar vipper på nogle øre. Det er vagtens svar der gælder.
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

    def test_the_minimum_runtime_counts_from_when_the_block_began(self):
        # En blok der lægges kl. :20, starter kl. :20 - men dens start står
        # på halvtimen, så «ung» blev regnet fra :00. Et minut senere var den
        # tyve minutter gammel, pillefyret vandt, og kompressoren stoppede
        # efter én minut med strækket brugt op.
        late = slot_start(self.now) + 20 * 60
        self.assertTrue(
            self.charge.update(
                late, FakeDecision(window_starts_in=90, window_minutes=90),
                self.plan, 16.0,
            )
        )

        self.assertTrue(
            self.charge.update(
                late + 60,
                FakeDecision(source="pillefyr"),
                self.plan,
                16.0,
                min_runtime_minutes=15,
            )
        )

    def test_a_block_that_began_survives_a_restart_with_its_start(self):
        late = slot_start(self.now) + 20 * 60
        self.charge.update(
            late, FakeDecision(window_starts_in=90, window_minutes=90), self.plan, 16.0
        )

        back = ChargePlan.from_raw(self.charge.to_raw())

        self.assertTrue(
            back.update(
                late + 60, FakeDecision(source="pillefyr"), self.plan, 16.0,
                min_runtime_minutes=15,
            )
        )

    def test_but_a_full_store_goes_before_the_minimum_runtime(self):
        # Der er ingen varme at levere ind i et fuldt lager, så der er heller
        # ikke noget at beskytte.
        self.start()
        self.full_at(60, min_runtime_minutes=15)

        self.assertFalse(self.full_at(60 + 180, min_runtime_minutes=15))

    def test_nothing_else_does(self):
        # Behovet forsvinder midt i blokken. Den kører alligevel færdig.
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
        """Beslutningen som den ser ud det minut - fristen tæller ned."""
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
    """Ét dyrt stræk giver én opladning - også når toppen vandrer.

    Reproduktionen af den 9. september. Flaget tændte og slukkede otte gange,
    fordi spærren kendte strækket på dets *dyreste* halvtime. Pilleloftet
    gør alle dyre halvtimer lige dyre, den tidligste vinder, og når den
    bliver til «nu», arver den næste titlen. Strækket selv rykkede sig ikke
    en tomme.
    """

    def setUp(self):
        self.now = 1_757_000_000.0
        # Ti billige halvtimer, så dyrt resten af vejen.
        self.plan = plan(*([35] * 10 + [155] * 14))
        self.charge = ChargePlan()

    def at(self, minute, top):
        """Strækket står stille; ``window_minutes`` vandrer."""
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

        # Toppen vandrer en halvtime ad gangen gennem strækket, præcis som
        # den gjorde den 9. september. Strækket er det samme, så der må
        # ikke lægges en ny blok.
        for minute, top in ((60, 330), (90, 360), (120, 390), (180, 450)):
            self.assertFalse(
                self.charge.update(
                    self.now + minute * 60, self.at(minute, top), self.plan, 16.0
                )
            )
            self.assertIn("allerede ladet op", self.charge.note)
            self.assertIsNone(self.charge.slots())

    def test_a_new_stretch_may_be_charged_for(self):
        # Modtesten, og den er lige så vigtig: to dyre stræk med billige
        # timer imellem - en dyr morgen og en dyr aften - skal give to
        # blokke, én inden hver. Ellers er spærren bare blevet til
        # «én om dagen».
        self.run_block()

        # Aftenens stræk: begynder 100 min efter det første er forbi.
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


# Billigt nu og de næste to timer, dyrt fra slot 4. Blokken kan altså lægges
# med det samme, og der er slæk mellem dens ende og fristen.
DELIVERY_RATES = (35, 35, 35, 35, 155, 155, 155, 155)


class DeliveryTest(unittest.TestCase):
    """Blokken slutter på den mængde den blev lagt for, ikke på uret alene.

    Den 17. september kl. 12 blev der lagt en blok på 20,8 kWh over 119
    minutter, regnet af en *målt* ladehastighed på 10,5 kW. Men add-on'en var
    lige genstartet på en ny udgave, og de første 25 minutter stod pumpen
    stille: 0,07 kW el, 0,0 kW varme, «ignoreret: pumpen står stille». Uret
    talte dem med alligevel, så blokken ville slutte 13:57 med omkring 16 af
    de 20,8 kWh i lageret - og ``_finish`` markerede så hele strækket som
    klaret frem til kl. 07:30 næste morgen.

    Der var to timers slæk: blokken sluttede 13:57, og det dyre stræk
    begyndte først kl. 16. Dem bruger den nu.
    """

    def setUp(self):
        self.now = slot_start(1_757_000_000.0)
        self.plan = plan(*DELIVERY_RATES)
        self.charge = ChargePlan()
        # Fristen er et *tidspunkt*. Planlæggeren giver den som minutter
        # forude, regnet fra halvtimens start, og tallet tæller ned hver
        # cyklus - så gør det her også. Uden nedtællingen skubbes fristen
        # foran sig selv, og så kan ingen test nå den.
        self.target = self.now + 120 * 60
        self.step(0, heat_kw=0.0)
        self.block = self.charge.block
        self.length = int(round((self.block.ends_at - self.block.starts_at) / 60))
        self.until = int(round((self.block.deadline - self.block.starts_at) / 60))
        # Den hastighed der leverer mængden på blokkens egen tid.
        self.rate = self.block.kwh / (self.length / 60)

    def decision_at(self, at):
        left = max(1, int(round((self.target - slot_start(at)) / 60)))
        return FakeDecision(
            planned_kwh=12.0,
            window_starts_in=left,
            window_minutes=left,
            dear_starts_in=left,
            dear_span_minutes=120,
        )

    def step(self, minute, heat_kw=None, full=False):
        at = self.now + minute * 60
        return self.charge.update(
            at,
            self.decision_at(at),
            self.plan,
            16.0,
            full=full,
            heat_kw=heat_kw,
        )

    def run_minutes(self, first, last, heat_kw, full=False):
        """Kør cyklusser ét minut ad gangen, som add-on'en selv gør."""
        for minute in range(first, last + 1):
            self.step(minute, heat_kw=heat_kw, full=full)

    def run_until_stopped(self, first, last, heat_kw, full=False):
        """Kør til blokken slutter, og giv noten fra det minut.

        Noten skal læses *når* den sættes. Cyklussen efter en afsluttet blok
        skriver «allerede ladet op mod det her dyre stræk» oven i den - helt
        korrekt, men så er grunden til at den sluttede, væk.
        """
        for minute in range(first, last + 1):
            self.step(minute, heat_kw=heat_kw, full=full)
            if not self.charge.charging and self.charge.block is None:
                return self.charge.note
        return None

    def test_the_block_starts_now_and_has_room_to_grow(self):
        # Forudsætningen for resten. Uden slæk mellem enden og fristen siger
        # de næste tests ingenting.
        self.assertEqual(self.block.starts_at, self.now)
        self.assertGreater(self.block.deadline, self.block.ends_at)

    def test_a_block_that_delivered_its_kwh_still_ends_on_time(self):
        # Modtesten først. Leverer pumpen rigeligt, må forlængelsen ikke røre
        # noget - ellers er den bare blevet til «kør altid til fristen».
        self.run_minutes(1, self.length, heat_kw=self.rate * 1.5)

        self.assertFalse(self.charge.charging)
        self.assertIn("kørt", self.charge.note)
        self.assertIsNone(self.charge.block)

    def test_a_block_the_pump_slept_through_runs_on(self):
        sleep = 20
        self.run_minutes(1, sleep, heat_kw=0.0)
        self.run_minutes(sleep + 1, self.length, heat_kw=self.rate)

        self.assertTrue(self.charge.charging, "blokken skal køre videre")
        self.assertGreater(self.charge.block.ends_at, self.block.ends_at)
        # Og den strækker sig ikke længere end den tid der gik tabt.
        self.assertLessEqual(
            self.charge.block.ends_at, self.block.ends_at + (sleep + 1) * 60
        )

    def test_and_it_stops_once_the_kwh_are_in(self):
        sleep = 20
        self.run_minutes(1, sleep, heat_kw=0.0)
        note = self.run_until_stopped(
            sleep + 1, self.length + sleep + 5, heat_kw=self.rate
        )

        self.assertFalse(self.charge.charging)
        self.assertIn("kørt", note or "")

    def test_the_deadline_is_a_hard_ceiling(self):
        # En blok må aldrig løbe ind i det dyre stræk - det er hele dens
        # formål. Leverer pumpen ingenting, stopper den ved fristen.
        note = self.run_until_stopped(1, self.until + 2, heat_kw=0.0)

        self.assertFalse(self.charge.charging)
        self.assertIn("nåede ikke i lageret", note or "")

    def test_without_a_heat_reading_the_clock_still_rules(self):
        # Har anlægget ingen varmeydelsesføler, ved vi ikke hvad der gik ind.
        # Så kører blokken på uret som den altid har gjort - der findes ikke
        # en dårligere grund til at holde kompressoren i gang end at man ikke
        # kan måle.
        self.run_minutes(1, self.length, heat_kw=None)

        self.assertFalse(self.charge.charging)
        self.assertIn("kørt", self.charge.note)

    def test_a_full_store_still_beats_a_missing_kwh(self):
        # Lageret kan ikke tage imod, og så er der ingen grund til at holde
        # kompressoren i gang efter en mængde der aldrig kommer ind.
        self.run_minutes(1, self.length, heat_kw=0.0)
        self.assertTrue(self.charge.charging, "forlænget, så der er noget at afbryde")

        note = self.run_until_stopped(
            self.length + 1, self.length + 8, heat_kw=0.0, full=True
        )

        self.assertFalse(self.charge.charging)
        self.assertIn("fuldt", note or "")


class FallbackLockTest(unittest.TestCase):
    """Den 15. september: et relativt stræk må ikke låse hele horisonten.

    Kl. 11:56 blev en blok på 5,6 kWh lagt mod et stræk fra kl. 12:30 til
    horisontens kant - «dyrere end nu» set fra ladevinduet, og natten på
    batteri er også dyrere. Da den var kørt, stod der «allerede ladet op» til
    næste formiddag, mens beslutningen bad om 8-23 kWh.
    """

    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(*([35] * 6 + [155] * 42))
        self.charge = ChargePlan()

    def at(self, minute, ends_in):
        return FakeDecision(
            planned_kwh=5.6,
            window_starts_in=30,
            window_minutes=420 - minute,
            dear_starts_in=30,
            dear_span_minutes=1410,
            dear_ends_in=ends_in,
        )

    def run_block(self):
        # Toppen slutter 480 minutter frem.
        for minute in (0, 15, 22, 30):
            self.charge.update(
                self.now + minute * 60, self.at(minute, 480 - minute), self.plan, 16.0
            )
        self.assertIsNone(self.charge.slots())

    def test_the_same_peak_is_still_covered(self):
        self.run_block()

        self.assertFalse(
            self.charge.update(self.now + 60 * 60, self.at(60, 420), self.plan, 16.0)
        )
        self.assertIn("allerede ladet op", self.charge.note)

    def test_once_the_peak_is_over_a_new_block_may_be_laid(self):
        # Ni timer senere er eksporten forbi. Det relative stræk rækker stadig
        # langt ind i næste dag, men det var toppen der blev ladet op imod.
        self.run_block()

        self.charge.update(self.now + 9 * 3600, self.at(0, 480), self.plan, 16.0)

        self.assertNotIn("allerede ladet op", self.charge.note)
        self.assertIsNotNone(self.charge.slots())


class HalfHourFrameTest(unittest.TestCase):
    """Fristen tæller fra halvtimens start, og blokken må ikke løbe forbi den."""

    def setUp(self):
        # 16:17 - sytten minutter inde i halvtimen.
        self.base = slot_start(1_757_000_000.0)
        self.now = self.base + 17 * 60
        self.plan = plan(40, 40, 300, 300)

    def test_a_block_does_not_run_into_the_dear_half_hour(self):
        # 16 kWh er en time ved 16 kW. Starter den kl. 16:17 og kører en hel
        # time, slutter den 17:17 - sytten minutter inde i det dyre.
        charge = ChargePlan()
        running = charge.update(
            self.now,
            FakeDecision(planned_kwh=16.0, window_starts_in=60, window_minutes=60),
            self.plan,
            16.0,
        )

        self.assertTrue(running)
        self.assertEqual(charge.slots(), (self.base, self.base + 60 * 60))
        self.assertAlmostEqual(charge.block.kwh, 16.0 * 43 / 60, places=6)

    def test_a_block_that_fits_is_left_alone(self):
        charge = ChargePlan()
        charge.update(
            self.now,
            FakeDecision(planned_kwh=8.0, window_starts_in=60, window_minutes=60),
            self.plan,
            16.0,
        )

        self.assertEqual(charge.slots(), (self.base, self.now + 30 * 60))
        self.assertAlmostEqual(charge.block.kwh, 8.0, places=6)


class ManualTest(unittest.TestCase):
    """Opladningen startet med knappen på plan-siden."""

    def setUp(self):
        self.now = 1_757_000_000.0
        self.plan = plan(155, 155, 155, 155)
        self.charge = ChargePlan()

    def step(self, minute, **over):
        values = dict(decision=FakeDecision(planned_kwh=None), plan=self.plan, rate_kw=12.0)
        values.update(over)
        return self.charge.update(self.now + minute * 60, **values)

    def test_it_charges_right_away_for_its_minutes(self):
        self.charge.start_manual(self.now, 90, 12.0)

        self.assertTrue(self.step(0))
        self.assertTrue(self.step(89))
        self.assertFalse(self.step(90))
        self.assertIsNone(self.charge.slots())

    def test_the_boiler_winning_does_not_stop_it(self):
        # Knappen er en ordre. Pillefyret kan være billigere - det er derfor
        # der er en knap.
        self.charge.start_manual(self.now, 90, 12.0)

        self.assertTrue(self.step(30, source="pillefyr", min_runtime_minutes=15))

    def test_a_full_store_does(self):
        self.charge.start_manual(self.now, 90, 12.0)
        self.step(10, full=True)

        self.assertFalse(self.step(14, full=True))
        self.assertIn("fuldt", self.charge.note)

    def test_it_does_not_burn_a_stretch(self):
        # Planlæggeren må gerne lægge sin egen blok bagefter.
        self.charge.start_manual(self.now, 30, 12.0)
        self.step(31)

        self.assertIsNone(self.charge.done_until)

    def test_it_can_be_stopped_and_leaves_nothing_behind(self):
        self.charge.start_manual(self.now, 90, 12.0)

        note = self.charge.stop(self.now + 600)

        self.assertIn("stoppet", note)
        self.assertFalse(self.charge.running(self.now + 601))
        self.assertIsNone(self.charge.done_until)

    def test_stopping_an_automatic_block_counts_it_as_done(self):
        # Ellers ville planlæggeren lægge den igen i næste minut.
        charge = ChargePlan()
        charge.update(
            self.now, FakeDecision(window_starts_in=90, window_minutes=90),
            plan(35, 35, 35, 155), 16.0,
        )
        self.assertTrue(charge.running(self.now))

        charge.stop(self.now + 60)

        self.assertIsNotNone(charge.done_until)

    def test_it_does_not_take_over_a_running_automatic_block(self):
        charge = ChargePlan()
        charge.update(
            self.now, FakeDecision(window_starts_in=90, window_minutes=90),
            plan(35, 35, 35, 155), 16.0,
        )

        note = charge.start_manual(self.now + 60, 90, 12.0)

        self.assertIn("kører allerede", note)
        self.assertFalse(charge.manual)

    def test_it_survives_a_restart(self):
        self.charge.start_manual(self.now, 90, 12.0)

        back = ChargePlan.from_raw(self.charge.to_raw())

        self.assertTrue(back.manual)
        self.assertTrue(
            back.update(self.now + 1800, FakeDecision(source="pillefyr"), self.plan, 12.0)
        )


class StorageTest(unittest.TestCase):
    def test_a_running_block_survives_a_restart(self):
        # En genstart midt i en opladning må ikke starte kompressoren forfra
        # på den anden side.
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
