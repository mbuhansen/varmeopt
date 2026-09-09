import time
import unittest

from varmeopt.planner import Decision, Planner, minutes_until_hour, source_now
from varmeopt.prices import Grid, Plan

PELLET = 0.706


def plan(*rates):
    """En plan hvor batteriet er bundet, saa importprisen gaelder direkte."""
    rows = [
        {"state": "holdchrg", "import_rate": rate, "export_rate": 50} for rate in rates
    ]
    return Plan.from_predbat({"raw": {"rows": rows}})


def planner(**over):
    values = dict(
        pellet_price=PELLET,
        hysteresis=0.05,
        wear_kr_per_kwh=0.15,
        min_charge_kwh=4.0,
        charge_kw=16.0,
    )
    values.update(over)
    return Planner(**values)


class SourceTest(unittest.TestCase):
    def test_the_heat_pump_wins_when_it_is_cheaper(self):
        source, why = source_now(0.30, PELLET, 0.05)

        self.assertEqual(source, "varmepumpe")
        self.assertIn("<", why)

    def test_the_boiler_wins_when_the_pump_is_dearer(self):
        source, _ = source_now(1.20, PELLET, 0.05)

        self.assertEqual(source, "pillefyr")

    def test_a_close_race_goes_to_the_heat_pump(self):
        source, why = source_now(PELLET + 0.02, PELLET, 0.05)

        self.assertEqual(source, "varmepumpe")
        self.assertIn("tæt", why)

    def test_without_a_cop_we_assume_the_heat_pump(self):
        source, why = source_now(None, PELLET, 0.05)

        self.assertEqual(source, "varmepumpe")
        self.assertIn("ingen COP", why)


class CheapestHeatTest(unittest.TestCase):
    def test_the_boiler_caps_the_price(self):
        # Uanset hvor dyr stroemmen bliver, betaler man aldrig mere end pille.
        self.assertAlmostEqual(planner().cheapest_heat(20.0, 4.0), PELLET, places=9)

    def test_the_pump_wins_when_it_is_cheaper(self):
        # 1,20/4 = 0,30 i stroem, plus 0,15 i slitage. Varmepumpevarme
        # koster mere end elprisen alene siger.
        self.assertAlmostEqual(planner().cheapest_heat(1.20, 4.0), 0.45, places=9)

    def test_wear_belongs_to_the_pump_and_not_to_the_boiler(self):
        # Pillefyret baerer ikke tallet - der er braendslet og
        # virkningsgraden hele historien.
        self.assertAlmostEqual(planner().cheapest_heat(20.0, 4.0), PELLET, places=9)

    def test_wear_can_decide_the_source(self):
        # 2,50/4 = 0,625 i ren stroem: klart under pillefyrets 0,706, og saa
        # havde varmepumpen vundet. Med slitagen er varmen 0,775, og saa er
        # pillefyret billigst. Det er hele pointen i at flytte tallet - de
        # to regnestykker gav foer to forskellige svar.
        p = planner()

        self.assertLess(p.heat_price(2.50, 4.0) - 0.15, PELLET - p.hysteresis)
        self.assertGreater(p.heat_price(2.50, 4.0), PELLET + p.hysteresis)

        self.assertEqual(p.decide(plan(250), cop_now=4.0).source, "pillefyr")

    def test_no_cop_falls_back_to_the_boiler(self):
        self.assertAlmostEqual(planner().cheapest_heat(1.20, None), PELLET, places=9)


class DecideTest(unittest.TestCase):
    def test_no_plan_still_gives_a_source(self):
        # Predbat kan vaere nede. Styringen skal stadig kunne vaelge.
        decision = planner().decide(plan=None, cop_now=4.0)

        self.assertEqual(decision.source, "varmepumpe")
        self.assertFalse(decision.charge)

    def test_flat_prices_give_nothing_to_gain(self):
        decision = planner().decide(plan(100, 100, 100), cop_now=4.0, headroom_kwh=20)

        self.assertFalse(decision.charge)
        self.assertIn("intet at hente", decision.reason)

    def test_a_margin_inside_the_noise_is_not_worth_moving_heat_for(self):
        # 1,00 -> 1,16 kr/kWh stroem ved COP 4 er 0,04 kr/kWh varme. Det er
        # under hysteresen: de to halvtimer er ikke til at skelne med de tal
        # vi har, og saa saettes 20 kWh ikke i bevaegelse paa forskellen.
        decision = planner().decide(plan(100, 116), cop_now=4.0, headroom_kwh=20)

        self.assertFalse(decision.charge)
        self.assertIn("for tæt", decision.reason)

    def test_a_margin_above_the_noise_still_charges(self):
        # 0,06 kr/kWh varme er over snittet, og saa lades der.
        decision = planner().decide(plan(100, 125), cop_now=4.0, headroom_kwh=20)

        self.assertTrue(decision.charge)

    def test_a_dearer_hour_ahead_is_worth_charging_for(self):
        # 0,40 kr nu mod 2,40 senere ved COP 4: 0,10 mod 0,60 pr. kWh varme.
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=20)

        self.assertTrue(decision.charge)
        self.assertEqual(decision.window_minutes, 30)
        self.assertGreater(decision.saving_kr, 0)

    def test_the_boiler_caps_what_is_worth_avoiding(self):
        # To absurde elpriser senere. Begge ligger over pillevarmen, saa begge
        # klemmes til den - og saa er der praecis lige meget at spare.
        høj = planner().decide(plan(40, 4000), cop_now=4.0, headroom_kwh=20)
        højere = planner().decide(plan(40, 8000), cop_now=4.0, headroom_kwh=20)

        self.assertAlmostEqual(høj.saving_kr, højere.saving_kr, places=9)

    def test_below_the_cap_a_dearer_hour_is_worth_more(self):
        # Under loftet slaar prisen stadig igennem. Loftet ligger nu ved
        # 0,706 - 0,15 = 0,556 kr/kWh varme, altsaa 2,22 kr/kWh stroem ved
        # COP 4; begge raekker her er under.
        mild = planner().decide(plan(40, 180), cop_now=4.0, headroom_kwh=20)
        værre = planner().decide(plan(40, 210), cop_now=4.0, headroom_kwh=20)

        self.assertGreater(værre.saving_kr, mild.saving_kr)

    def test_moving_pump_heat_in_time_costs_no_extra_wear(self):
        # 0,40 -> 0,80 kr/kWh stroem ved COP 4. Slitagen er den samme om
        # pumpen koerer nu eller om en halv time - den samme kWh gaar
        # igennem den samme maskine - saa de 0,10 kr er en aegte gevinst.
        #
        # Foer blev slitagen trukket fra her *og* talt i varmeprisen, og saa
        # blev det til -0,05 og ingen opladning.
        decision = planner(wear_kr_per_kwh=0.15).decide(
            plan(40, 80), cop_now=4.0, headroom_kwh=20
        )

        self.assertTrue(decision.charge)

    def test_but_displacing_pellet_heat_does_pay_the_wear(self):
        # Her er den senere varme pillefyrets, og saa staar slitagen
        # tilbage i marginen: 0,706 - (0,40/4 + 0,15) = 0,456, ikke 0,606.
        decision = planner(wear_kr_per_kwh=0.15).decide(
            plan(40, 300), cop_now=4.0, cop_later=4.0, headroom_kwh=20, demand_kw=None
        )

        self.assertTrue(decision.charge)
        self.assertAlmostEqual(decision.saving_kr / decision.charge_kwh, 0.456, places=6)

    def test_solar_gets_its_share_first(self):
        # 20 kWh plads, men solen venter med 18. Saa er der 2 tilbage, og det
        # er under minimumstraekket.
        decision = planner().decide(
            plan(40, 240), cop_now=4.0, headroom_kwh=20, solar_expected_kwh=18.0
        )

        self.assertFalse(decision.charge)
        self.assertIn("under minimumstrækket", decision.reason)

    def test_a_full_store_cannot_be_charged(self):
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=0.0)

        self.assertFalse(decision.charge)

    def test_the_charge_rate_limits_a_short_window(self):
        # 16 kW i en halv time er 8 kWh, uanset at der er 40 kWh plads.
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=40)

        self.assertAlmostEqual(decision.charge_kwh, 8.0, places=9)

    def test_a_worse_cop_later_makes_charging_more_attractive(self):
        # Samme priser, men COP falder til aften: saa er der mere at hente.
        same = planner().decide(plan(40, 100), cop_now=4.0, cop_later=4.0, headroom_kwh=20)
        worse = planner().decide(plan(40, 100), cop_now=4.0, cop_later=2.5, headroom_kwh=20)

        self.assertTrue(worse.charge)
        self.assertGreater(worse.saving_kr, same.saving_kr)

    def test_the_reason_says_what_was_decided(self):
        decision = planner().decide(plan(40, 240), cop_now=4.0, headroom_kwh=20)

        self.assertIn("lad", decision.reason)
        self.assertIn("spar", decision.reason)
        self.assertIn("kWh", decision.charging_note)


class ChargingSlotsTest(unittest.TestCase):
    """«Lad op» ud for blokkens egne halvtimer.

    Her stod et gaet: de N billigste halvtimer inden toppen, rekonstrueret af
    maengden og pumpens ydelse. Nu laegger ``charge.py`` blokken, og tabellen
    tegner den - saa det den viser, er den plan der faktisk koeres.
    """

    RATES = [90, 90, 90, 90, 35, 35, 35, 35, 90, 155, 155, 155]

    def marked(self, window):
        rows = planner().project(
            plan(*self.RATES), cop_now=4.5, target_minutes=270, charge_window=window
        )
        return [r.minutes for r in rows if r.charging]

    def test_the_blocks_own_half_hours_are_marked(self):
        # Blokken ligger 120-210 minutter frem: tre halvtimer.
        self.assertEqual(self.marked((120, 210)), [120, 150, 180])

    def test_a_block_that_starts_now_marks_the_row_we_stand_in(self):
        self.assertIn(0, self.marked((0, 45)))

    def test_no_block_marks_nothing(self):
        self.assertEqual(self.marked(None), [])
        self.assertEqual(self.marked((120, 120)), [])


class WaitingStillHasAnIntentTest(unittest.TestCase):
    def test_a_waiting_decision_still_says_how_much(self):
        # Foer stod vent-grenen foer maengden blev regnet, og saa var
        # hensigten ukendt mens den ventede - saa planen kunne ikke tegne
        # "lad op" paa netop de halvtimer den ventede paa.
        d = planner().decide(
            plan(90, 30, 30, 300), cop_now=4.0, headroom_kwh=20, stored_kwh=0.0
        )

        self.assertFalse(d.charge)
        self.assertIn("venter", d.reason)
        self.assertIsNotNone(d.planned_kwh)
        self.assertGreater(d.planned_kwh, 0)


class ExportWindowTest(unittest.TestCase):
    """Hele det dyre vindue skal daekkes, ikke kun toppen.

    Formaalet med at lade op i forvejen er at holde varmepumpen ude af
    eksportvinduet: koerer den mens der saelges til 1,57 kr/kWh, er det tabt
    indtjening. Og koden har ingen «koer ikke»-udgang - kildevalget kan kun
    vaelge mellem varmepumpe og pillefyr, og ved 1,57 vinder varmepumpen. Den
    eneste vej udenom er et lager der raekker hele vinduet igennem.
    """

    # Billigt nu, 1,20 i to halvtimer, saa 1,57 i fire. Det dyre begynder
    # altsaa 60 minutter frem og varer tre timer.
    RATES = (37, 37, 120, 120, 157, 157, 157, 157, 37)

    def window(self):
        p = planner()
        pl = plan(*self.RATES)
        vp_now = p.heat_price(0.37, 4.5)
        return p._dear_window(pl, vp_now, 4.5, None)

    def test_it_begins_where_it_gets_dear_not_where_it_is_dearest(self):
        # Her stod ``best_when`` - den dyreste halvtime - som startpunkt, og
        # saa blev spaendet to timer i stedet for tre. Lageret blev ladet til
        # to, toemt fra den foerste dyre time, og loeb toert midt i den
        # dyreste eksport. Saa starter UVR'en pumpen selv.
        starts, span = self.window()

        self.assertEqual(starts, 60)
        self.assertEqual(span, 180)

    def test_a_single_cheap_half_hour_does_not_split_the_window(self):
        # Huset traekker videre af lageret i den billige halvtime, saa
        # vinduet er ét vindue.
        p = planner()
        pl = plan(37, 157, 157, 37, 157, 157, 37)
        vp_now = p.heat_price(0.37, 4.5)

        starts, span = p._dear_window(pl, vp_now, 4.5, None)

        self.assertEqual(starts, 30)
        self.assertEqual(span, 150)

    def test_the_amount_is_sized_to_the_whole_window(self):
        # Tre timers vindue ved 2 kW husforbrug er 6 kWh fortraengt varme -
        # ikke de 4 to timer ville give.
        d = planner().decide(
            plan(*self.RATES),
            cop_now=4.5,
            cop_later=4.5,
            headroom_kwh=30.0,
            stored_kwh=0.0,
            demand_kw=2.0,
        )

        self.assertTrue(d.charge, d.reason)
        self.assertGreaterEqual(d.charge_kwh, 6.0)


class SolarRoomTest(unittest.TestCase):
    """Solen og varmepumpen konkurrerer kun om pladsen under 60 grader."""

    def test_solar_that_fits_above_the_ceiling_blocks_nothing(self):
        # Solfangeren kan presse til 90 hvor pumpen stopper ved 60, saa der er
        # 20 kWh plads derover. Den forventede sol kan ligge der, og en billig
        # formiddag behoever derfor ikke staa tom.
        d = planner().decide(
            plan(40, 240),
            cop_now=4.0,
            headroom_kwh=20,
            peak_headroom_kwh=40,
            solar_expected_kwh=18.0,
            stored_kwh=0.0,
        )

        self.assertTrue(d.charge, d.reason)

    def test_solar_that_does_not_fit_still_gets_its_share_first(self):
        # Er der kun 2 kWh plads over pumpens loft, kan solen ikke laegges
        # derover, og saa skal den have sin plads under det.
        d = planner().decide(
            plan(40, 240),
            cop_now=4.0,
            headroom_kwh=20,
            peak_headroom_kwh=22,
            solar_expected_kwh=19.0,
            stored_kwh=0.0,
        )

        self.assertFalse(d.charge)
        self.assertIn("under minimumstrækket", d.reason)


class SavingTest(unittest.TestCase):
    def test_the_saving_follows_what_is_actually_charged(self):
        # Behovet er stoerre end pladsen. Saa kan gevinsten kun gaelde det der
        # kommer i tanken - her stod behovet, og det lovede en besparelse paa
        # varme der aldrig blev lavet.
        d = planner().decide(
            plan(40, 240, 240, 240),
            cop_now=4.0,
            headroom_kwh=6.0,
            demand_kw=10.0,
            stored_kwh=0.0,
        )

        self.assertTrue(d.charge, d.reason)
        margin = 0.706 - (0.40 / 4.0 + 0.15)
        self.assertAlmostEqual(d.saving_kr, margin * d.charge_kwh, places=6)


class SixthOfSeptemberTest(unittest.TestCase):
    """Dagen hvor den ikke ladede op, og brugeren maatte goere det selv.

    Stroemmen var billig fra 10 til 17 og dyr om aftenen - saa dyr at der blev
    eksporteret til 1,57 kr/kWh. Varmen kostede 0,23 kr/kWh om formiddagen mod
    0,50 om aftenen. Planlaeggeren sagde «der bruges 1,3 kWh mens det er dyrt,
    og lageret har 13,3 - intet at lade op til».

    De 13,3 kWh var varme over 30 grader. Der stod nul over 50, og aftenen er
    delvis varmt vand.
    """

    def buffer(self, a, b):
        from varmeopt.tank import Buffer, Tank

        return Buffer(
            (Tank("A", 500.0, *a), Tank("B", 500.0, *b)),
            reference=30.0,
            ceiling=60.0,
            peak_ceiling=90.0,
        )

    def decide(self, buf, dhw_kwh, rates=(37, 37, 37, 37, 37, 37, 157, 157, 157)):
        # 0,37 kr/kWh formiddagen igennem mod 1,57 om aftenen, COP 4,47 som
        # den var. Der er timer at lade i inden prisen stiger - det var der
        # ogsaa den 6. september.
        return planner().decide(
            plan(*rates),
            cop_now=4.47,
            cop_later=4.47,
            headroom_kwh=buf.headroom_kwh,
            peak_headroom_kwh=buf.peak_headroom_kwh,
            stored_kwh=buf.stored_kwh,
            hot_kwh=buf.usable_kwh(55.0),
            dhw_kwh_over=lambda start_min, hours: dhw_kwh,
            dhw_input_for=lambda kwh: buf.energy_to_reach(kwh, 55.0),
            demand_kw=0.37,
        )

    def test_the_morning_tanks_could_not_make_hot_water(self):
        # Det er hele sagen: 13,3 kWh der kan varme et gulv, og ingenting der
        # kan lave et bad.
        morning = self.buffer((45.0, 45.0, 43.0), (47.0, 39.0, 31.0))

        self.assertAlmostEqual(morning.stored_kwh, 13.4, delta=0.2)
        self.assertEqual(morning.usable_kwh(55.0), 0.0)

    def test_and_so_it_charges(self):
        morning = self.buffer((45.0, 45.0, 43.0), (47.0, 39.0, 31.0))

        d = self.decide(morning, dhw_kwh=6.0)

        self.assertTrue(d.charge, d.reason)
        self.assertIn("til varmt vand", d.reason)
        # Og maengden skal vaere den der faktisk giver 6 kWh over 55 grader.
        # 6 kWh ind haever lageret fem grader og efterlader nul deroppe; det
        # rigtige tal er hele pladsen.
        self.assertGreater(d.charge_kwh, 15.0)

    def test_it_says_so_when_it_cannot_cover_the_window(self):
        # Bliver det dyrt om en halv time, er der kun tid til 8 kWh. Saa
        # daekker opladningen ikke vinduet, og det skal staa der frem for at
        # maengden bare bliver kappet.
        morning = self.buffer((45.0, 45.0, 43.0), (47.0, 39.0, 31.0))

        d = self.decide(morning, dhw_kwh=6.0, rates=(37, 157, 157, 157))

        self.assertTrue(d.charge, d.reason)
        self.assertIn("daekker ikke vinduet", d.reason)

    def test_after_the_charge_the_tanks_are_simply_full(self):
        # Brugerens egen opladning kl. 15 gav 33,7 kWh i lageret, hvoraf
        # 5,0 er varme nok til beholderen - og saa er der ikke mere plads.
        afternoon = self.buffer((62.0, 63.0, 62.0), (57.0, 56.0, 56.0))

        self.assertAlmostEqual(afternoon.stored_kwh, 33.7, delta=0.2)
        self.assertAlmostEqual(afternoon.usable_kwh(55.0), 5.0, delta=0.2)

        d = self.decide(afternoon, dhw_kwh=4.0)

        self.assertFalse(d.charge, d.reason)

    def test_a_hot_tank_with_room_left_still_says_no(self):
        # Tank A er varm nok til aftenens bad, tank B er kold og har masser af
        # plads. Der er altsaa plads at lade i - og alligevel ingen grund,
        # fordi behovet er daekket. Det er selve testen af opdelingen.
        covered = self.buffer((60.0, 60.0, 60.0), (35.0, 33.0, 31.0))

        self.assertGreater(covered.headroom_kwh, 10.0)
        self.assertAlmostEqual(covered.usable_kwh(55.0), 2.9, delta=0.2)

        d = self.decide(covered, dhw_kwh=2.0)

        self.assertFalse(d.charge, d.reason)
        self.assertIn("intet at lade op til", d.reason)

    def test_without_a_profile_it_falls_back_to_the_house_alone(self):
        # Er doegnprofilen ikke laert endnu, er nul det eneste aerlige tal for
        # varmtvandet - og saa opfoerer den sig som foer.
        morning = self.buffer((45.0, 45.0, 43.0), (47.0, 39.0, 31.0))

        d = self.decide(morning, dhw_kwh=None)

        self.assertFalse(d.charge)


class DecisionShapeTest(unittest.TestCase):
    def test_a_plain_decision_reads_sensibly(self):
        decision = Decision(source="pillefyr", heat_price=1.0, pellet_price=PELLET)

        self.assertEqual(decision.charging_note, "lad ikke op")


if __name__ == "__main__":
    unittest.main()


class WaitForTheCheapestTest(unittest.TestCase):
    """Prisen falder foer den stiger. Saa er nu ikke tidspunktet."""

    def setUp(self):
        # 1,00 -> 0,30 -> 0,30 -> 3,00 kr/kWh. COP 3 hele vejen.
        self.plan = plan(100, 30, 30, 300)
        self.planner = planner()

    def test_it_waits_for_the_cheap_slot_instead_of_charging_now(self):
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0
        )

        self.assertFalse(d.charge)
        self.assertIn("venter", d.reason)

    def test_and_charges_once_the_cheap_slot_is_the_one_it_stands_in(self):
        # Samme raekke set et kvarter senere: nu *er* 0,30 den billigste.
        d = self.planner.decide(
            plan(30, 30, 300), cop_now=3.0, cop_later=3.0, headroom_kwh=24.0
        )

        self.assertTrue(d.charge)

    def test_a_cheaper_slot_too_late_to_use_is_not_worth_waiting_for(self):
        # Naar den billige halvtime foerst kommer lige inden toppen, er der
        # ikke tid til mindstetraekket, og saa er den uden vaerdi.
        d = planner(charge_kw=2.0).decide(
            plan(100, 30, 300), cop_now=3.0, cop_later=3.0, headroom_kwh=24.0
        )

        self.assertNotIn("venter", d.reason)


class SavingIsWhatGetsDisplacedTest(unittest.TestCase):
    """Gevinsten gaelder den fortraengte varme, ikke hele lagerpladsen."""

    def setUp(self):
        # Een dyr halvtime forude. Huset bruger 3 kW.
        self.plan = plan(30, 300, 30)
        self.planner = planner()

    def test_the_saving_counts_only_the_dear_half_hour(self):
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0, demand_kw=3.0
        )

        self.assertTrue(d.charge)
        # 3 kW i en halv time er 1,5 kWh fortraengt - ikke de 24 der er plads
        # til. Marginen er den samme; det er gangefaktoren der var forkert.
        self.assertLess(d.saving_kr, d.charge_kwh * 0.35)

    def test_two_dear_half_hours_displace_twice_as_much(self):
        one = self.planner.decide(
            plan(30, 300, 30), cop_now=3.0, cop_later=3.0,
            headroom_kwh=24.0, demand_kw=3.0,
        )
        two = self.planner.decide(
            plan(30, 300, 300, 30), cop_now=3.0, cop_later=3.0,
            headroom_kwh=24.0, demand_kw=3.0,
        )

        self.assertAlmostEqual(two.saving_kr, 2 * one.saving_kr, places=6)

    def test_a_store_that_covers_the_dear_hours_is_not_charged(self):
        # 3 kW gennem to dyre halvtimer er 3 kWh varme, og lageret har 5. Den
        # varme er lavet og betalt, og den bliver brugt foerst - der er
        # ingenting at lade op til.
        d = self.planner.decide(
            plan(30, 300, 300, 30), cop_now=3.0, cop_later=3.0,
            headroom_kwh=24.0, stored_kwh=5.0, demand_kw=3.0,
        )

        self.assertFalse(d.charge)
        self.assertIn("intet at lade op til", d.reason)

    def test_only_what_the_store_is_short_of_is_charged(self):
        # Samme to halvtimer, men lageret har kun 1 kWh: der mangler 2. Der
        # lades mindstetraekket paa 4, ikke de 8 der er plads til - og
        # gevinsten gaelder de 2, ikke de 4.
        d = self.planner.decide(
            plan(30, 300, 300, 30), cop_now=3.0, cop_later=3.0,
            headroom_kwh=24.0, stored_kwh=1.0, demand_kw=3.0,
        )

        self.assertTrue(d.charge)
        self.assertAlmostEqual(d.charge_kwh, 4.0, places=9)
        self.assertLess(d.saving_kr, 0.456 * 2 + 1e-9)

    def test_without_a_demand_it_says_so_by_not_pretending(self):
        # Uden et behov kan spoergsmaalet ikke besvares. Saa staar det gamle
        # tal - men det er nu det eneste tilfaelde, ikke reglen.
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0, demand_kw=None
        )

        self.assertTrue(d.charge)
        self.assertGreater(d.saving_kr, 0.0)



class DeadlineTest(unittest.TestCase):
    """Fristen paa uret: lageret skal vaere fyldt kl. 17.

    Prisen kender ikke badetiden. Den ser kun at aftenen er dyrere end nu,
    og den slutning kommer for sent hvis den billigste halvtime ligger lige
    inden - eller lige efter - det tidspunkt tankene skal vaere fulde.
    """

    def setUp(self):
        # 1,00 nu og de naeste to halvtimer, saa 0,30 kl. 90 min, og
        # 3,00 kl. 120. COP 3: varmen koster 0,48 nu og 0,25 i den billige.
        self.plan = plan(100, 100, 100, 30, 300)
        self.planner = planner()

    def test_without_a_deadline_it_waits_for_the_cheap_half_hour(self):
        # Den billige halvtime ligger foer toppen, og der er tid nok. Saa
        # venter den - og det er rigtigt, naar uret ikke siger andet.
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0
        )

        self.assertFalse(d.charge)
        self.assertIn("venter", d.reason)
        self.assertEqual(d.window_starts_in, 120)

    def test_the_clock_can_be_stricter_than_the_price(self):
        # Skal lageret vaere fyldt om 90 minutter, er den billige halvtime
        # der ligger *paa* fristen uden vaerdi: varmen naar ikke i tankene
        # inden der bades. Saa lades der nu.
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0,
            deadline_minutes=90,
        )

        self.assertTrue(d.charge)
        self.assertEqual(d.window_starts_in, 90)

    def test_a_deadline_beyond_the_price_changes_nothing(self):
        # Ligger fristen laengere ude end det tidspunkt hvor det bliver
        # dyrt, er det stadig prisen der binder.
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0,
            deadline_minutes=600,
        )

        self.assertFalse(d.charge)
        self.assertEqual(d.window_starts_in, 120)

    def test_the_sun_does_not_wait_for_a_cheaper_hour(self):
        # Er det solen der baerer huset, er der ikke en billigere time at
        # vente paa - tankene skal bare vaere fulde inden fristen. Det er
        # kun naar der lades fra nettet at timen skal vaere den billigste.
        d = self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0,
            grid=Grid(pv_power=3000.0),
        )

        self.assertTrue(d.charge)


    def test_the_bath_is_counted_from_the_deadline_and_not_from_the_peak(self):
        # Badet ligger kl. 19 uanset hvornaar stroemmen er dyrest. Har uret
        # sat fristen, skal doegnprofilen derfor laeses fra fristen og frem -
        # ellers skal lageret kun kunne lave badevand fra det tidspunkt
        # prisen tilfaeldigvis topper, og saa staar man med kolde tanke kl. 19.
        asked = []

        def profile(start_min, hours):
            asked.append((start_min, hours))
            return 5.0

        self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0,
            stored_kwh=0.0, hot_kwh=0.0, demand_kw=3.0,
            dhw_kwh_over=profile, deadline_minutes=90,
        )

        # Vinduet er dyrt fra 120; fristen er 90. Profilen skal laeses fra 90.
        self.assertEqual(asked[0][0], 90)

    def test_without_a_deadline_the_bath_is_counted_over_the_dear_window(self):
        asked = []

        def profile(start_min, hours):
            asked.append((start_min, hours))
            return 5.0

        self.planner.decide(
            self.plan, cop_now=3.0, cop_later=3.0, headroom_kwh=24.0,
            stored_kwh=0.0, hot_kwh=0.0, demand_kw=3.0,
            dhw_kwh_over=profile,
        )

        self.assertEqual(asked[0][0], 120)


class DeadlineOnTheClockTest(unittest.TestCase):
    """``minutes_until_hour`` - fristen som minutter, i lokal tid."""

    def test_a_time_later_today_is_the_hours_between(self):
        now = time.time()
        local = time.localtime(now)
        ahead = minutes_until_hour((local.tm_hour + 2) % 24, now)

        self.assertGreater(ahead, 60)
        self.assertLessEqual(ahead, 120)

    def test_a_time_already_passed_is_tomorrows(self):
        # Fristen binder kun den del af doegnet hvor den er foran os. Er den
        # passeret, ligger den laengere ude end nogen horisont.
        now = time.time()
        local = time.localtime(now)
        ahead = minutes_until_hour((local.tm_hour - 1) % 24, now)

        self.assertGreater(ahead, 22 * 60)

    def test_a_time_outside_the_day_turns_the_deadline_off(self):
        now = time.time()

        self.assertIsNone(minutes_until_hour(-1, now))
        self.assertIsNone(minutes_until_hour(24, now))
        self.assertIsNone(minutes_until_hour(None, now))
