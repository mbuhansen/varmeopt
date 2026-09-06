import unittest

from varmeopt.planner import Decision, Planner, source_now
from varmeopt.prices import Plan

PELLET = 0.706


def plan(*rates, battery_average=1.0):
    """En plan hvor batteriet er bundet, saa importprisen gaelder direkte."""
    rows = [
        {"state": "holdchrg", "import_rate": rate, "export_rate": 50} for rate in rates
    ]
    return Plan.from_predbat({"raw": {"rows": rows}}, battery_average=battery_average)


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
    """«Lad op» ud for de halvtimer opladningen ventes at ligge i.

    Planlæggeren har ingen tidsplan — den svarer «nu?» hvert minut. Men den
    venter systematisk på den billigste halvtime inden toppen, så der *er* en
    underforstået plan, og den skal kunne læses inden styringen kobles til.
    """

    # Billigt i fire halvtimer midt i vinduet, dyrt fra slot 9.
    RATES = [90, 90, 90, 90, 35, 35, 35, 35, 90, 155, 155, 155]

    def project(self, planned_kwh, target=270):
        p = planner(charge_kw=16.0)
        return p.project(
            plan(*self.RATES), cop_now=4.5, target_minutes=target,
            planned_kwh=planned_kwh,
        )

    def marked(self, rows):
        return [r.minutes for r in rows if r.charging]

    def test_only_as_many_slots_as_the_charge_takes(self):
        # 12 kWh ved 16 kW er 45 minutter: to halvtimer, ikke fire.
        self.assertEqual(len(self.marked(self.project(12.0))), 2)

    def test_and_they_are_the_cheapest_ones(self):
        # De billige halvtimer ligger paa 120-210 min.
        self.assertEqual(self.marked(self.project(12.0)), [120, 150])

    def test_a_bigger_charge_spills_into_the_next_cheapest(self):
        # 40 kWh er 2,5 time: de fire billige raekker ikke, saa den billigste
        # af resten kommer med.
        marked = self.marked(self.project(40.0))

        self.assertEqual(len(marked), 5)
        self.assertTrue({120, 150, 180, 210}.issubset(set(marked)))

    def test_nothing_is_marked_after_the_target(self):
        for minutes in self.marked(self.project(40.0)):
            self.assertLess(minutes, 270)

    def test_the_marks_disappear_as_the_store_fills(self):
        # Det er hele pointen: markeringen regnes forfra hvert minut ud fra
        # hvor meget der stadig mangler. Bliver lageret fuldt hurtigere end
        # ventet, falder maerkerne af sig selv.
        many = len(self.marked(self.project(40.0)))
        few = len(self.marked(self.project(8.0)))

        self.assertGreater(many, few)
        self.assertEqual(few, 1)

    def test_nothing_planned_marks_nothing(self):
        self.assertEqual(self.marked(self.project(None)), [])
        self.assertEqual(self.marked(self.project(0.0)), [])

    def test_without_a_target_there_is_no_window_to_fill(self):
        self.assertEqual(self.marked(self.project(40.0, target=None)), [])


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

    def decide(self, buf, dhw_kwh):
        # 0,37 kr/kWh nu mod 1,57 om aftenen, COP 4,47 som den var.
        return planner().decide(
            plan(37, 157, 157, 157),
            cop_now=4.47,
            cop_later=4.47,
            headroom_kwh=buf.headroom_kwh,
            peak_headroom_kwh=buf.peak_headroom_kwh,
            stored_kwh=buf.stored_kwh,
            hot_kwh=buf.usable_kwh(55.0),
            dhw_kwh_over=lambda hours: dhw_kwh,
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

