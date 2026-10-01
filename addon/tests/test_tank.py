import unittest

from varmeopt.tank import WH_PER_LITER_K, Buffer, Tank


def tank(name="A", liters=500.0, top=60.0, mid=45.0, bottom=30.0, outlet=None):
    return Tank(name=name, liters=liters, top=top, mid=mid, bottom=bottom, outlet=outlet)


class TankEnergyTest(unittest.TestCase):
    def test_stored_energy_over_reference(self):
        # 500 L delt på tre lag = 166,67 L pr. lag. Over 30 °C bidrager
        # top med 30 K og midt med 15 K, bunden med intet: 7500 L·K.
        t = tank()

        self.assertAlmostEqual(t.stored_kwh(30.0), 7500 * WH_PER_LITER_K / 1000, places=4)

    def test_headroom_up_to_ceiling(self):
        t = tank()

        self.assertAlmostEqual(t.headroom_kwh(60.0), 7500 * WH_PER_LITER_K / 1000, places=4)

    def test_layer_below_reference_never_counts_negative(self):
        # En bund koldere end referencen er ikke negativ energi — den er nul.
        t = tank(top=35.0, mid=30.0, bottom=20.0)

        self.assertAlmostEqual(t.stored_kwh(30.0), (500 / 3) * 5 * WH_PER_LITER_K / 1000, places=4)

    def test_missing_sensor_does_not_read_as_ice_cold(self):
        # Falder midterføleren ud, må de to andre dække tanken. Regnede vi
        # det manglende lag som 0 °C, ville estimatet styrtdykke uden at
        # tanken havde ændret sig.
        whole = tank()
        gap = tank(mid=None)

        self.assertAlmostEqual(gap.stored_kwh(30.0), whole.stored_kwh(30.0), places=4)
        self.assertEqual(gap.sensors_lost, 1)
        # Midt imellem 60 og 30 ligger 45 - præcis det der stod der.
        self.assertEqual(gap.layers, (60.0, 45.0, 30.0))

    def test_a_lost_bottom_sensor_does_not_inflate_the_store_by_half(self):
        # 500 L med 60/50/30 over reference 30 rummer 9,57 kWh. Da de to
        # målte lag dækkede hele tanken, blev det til 14,36 - halvdelen
        # mere varme end der var, netop når der var mindst grund til at tro
        # på tallet.
        whole = tank(top=60.0, mid=50.0, bottom=30.0)
        lost = tank(top=60.0, mid=50.0, bottom=None)

        truth = whole.stored_kwh(30.0)
        self.assertAlmostEqual(truth, 9.57, places=2)
        # Gradienten forlænges: 60, 50 -> 40.
        self.assertEqual(lost.layers, (60.0, 50.0, 40.0))

        # Fejlen er 1,92 kWh mod de 4,79 den gamle udgave gav. Den er ikke
        # væk: en rigtig tank har en termoklin, så bunden ligger koldere
        # end en ret linje siger, og en fremskrivning overvurderer den
        # altid lidt. Men den er mindre end det halve.
        was = (500 / 2) * (30 + 20) * 1.149 / 1000
        self.assertLess(abs(lost.stored_kwh(30.0) - truth), 0.5 * abs(was - truth))

    def test_a_lost_top_sensor_extends_the_gradient_upward(self):
        lost = tank(top=None, mid=50.0, bottom=30.0)

        self.assertEqual(lost.layers, (70.0, 50.0, 30.0))

    def test_an_inverted_profile_falls_back_on_the_nearest_layer(self):
        # Bunden varmere end midten er enten omrørt eller en føler ude af
        # kalibrering. Så er en fremskrivning værre end det nærmeste mål.
        lost = tank(top=None, mid=40.0, bottom=60.0)

        self.assertEqual(lost.layers[0], 60.0)

    def test_a_nan_is_not_a_measurement(self):
        t = tank(mid=float("nan"))

        self.assertEqual(t.sensors_lost, 1)
        self.assertEqual(t.layers, (60.0, 45.0, 30.0))

    def test_uncovered_tank_stores_nothing(self):
        t = tank(top=None, mid=None, bottom=None)

        self.assertFalse(t.covered)
        self.assertEqual(t.stored_kwh(30.0), 0.0)
        self.assertIsNone(t.mean_temp)


class TankShapeTest(unittest.TestCase):
    def test_spread_is_top_minus_bottom(self):
        self.assertEqual(tank(top=58.0, bottom=31.0).spread, 27.0)

    def test_spread_needs_both_ends(self):
        self.assertIsNone(tank(bottom=None).spread)

    def test_outlet_wins_over_top_for_deliverable(self):
        # Toppen er hvad der står i tanken; afgangsrøret er hvad der faktisk
        # kommer ud. Det sidste er det der afgør om brugsvandet bliver varmt.
        self.assertEqual(tank(top=60.0, outlet=54.0).deliverable, 54.0)

    def test_top_stands_in_when_no_outlet_sensor(self):
        self.assertEqual(tank(top=60.0, outlet=None).deliverable, 60.0)


class BufferTest(unittest.TestCase):
    def setUp(self):
        self.buffer = Buffer(
            tanks=(
                tank("A", top=60.0, mid=45.0, bottom=30.0, outlet=58.0),
                tank("B", top=50.0, mid=40.0, bottom=30.0, outlet=48.0),
            ),
            reference=30.0,
            ceiling=60.0,
        )

    def test_stored_is_the_sum_of_measured_tanks(self):
        a, b = self.buffer.tanks

        self.assertAlmostEqual(
            self.buffer.stored_kwh, a.stored_kwh(30.0) + b.stored_kwh(30.0), places=6
        )

    def test_charge_percent_sits_between_reference_and_ceiling(self):
        pct = self.buffer.charge_percent

        self.assertIsNotNone(pct)
        self.assertGreater(pct, 0)
        self.assertLess(pct, 100)

    def test_full_buffer_is_a_hundred_percent(self):
        full = Buffer(
            tanks=(tank("A", top=60.0, mid=60.0, bottom=60.0),),
            reference=30.0,
            ceiling=60.0,
        )

        self.assertAlmostEqual(full.charge_percent, 100.0, places=6)
        self.assertAlmostEqual(full.headroom_kwh, 0.0, places=6)

    def test_room_remains_above_what_the_heat_pump_can_reach(self):
        # Solvarme og ACthor kan begge presse tankene til 90 °C. Er de allerede
        # over varmepumpens loft, er der nul plads *til varmepumpen* — men de
        # to andre har stadig et sted at gøre af varmen.
        hot = Buffer(
            tanks=(tank("A", top=65.0, mid=65.0, bottom=65.0),),
            reference=30.0,
            ceiling=60.0,
            peak_ceiling=90.0,
        )

        self.assertEqual(hot.headroom_kwh, 0.0)
        self.assertGreater(hot.peak_headroom_kwh, 0.0)
        self.assertTrue(hot.above_heatpump_ceiling)
        # ... men solfangeren har stadig 30 K at give af. Det er forskellen
        # på de to lofter, og den afgør om en soldag kan læres af.
        self.assertFalse(hot.at_peak_ceiling)

    def test_a_cool_buffer_is_not_above_the_heat_pump_ceiling(self):
        self.assertFalse(self.buffer.above_heatpump_ceiling)
        self.assertGreater(self.buffer.peak_headroom_kwh, self.buffer.headroom_kwh)

    def test_an_unmeasured_buffer_is_not_declared_full(self):
        blind = Buffer(
            tanks=(tank("A", top=None, mid=None, bottom=None),),
            reference=30.0,
            ceiling=60.0,
        )

        self.assertFalse(blind.above_heatpump_ceiling)

    def test_imbalance_is_the_gap_between_tank_means(self):
        # A har middel 45, B har middel 40.
        self.assertAlmostEqual(self.buffer.imbalance, 5.0, places=6)

    def test_imbalance_needs_two_measured_tanks(self):
        lonely = Buffer(tanks=(tank("A"),), reference=30.0, ceiling=60.0)

        self.assertIsNone(lonely.imbalance)

    def test_deliverable_is_the_warmest_outlet(self):
        self.assertEqual(self.buffer.deliverable, 58.0)
        self.assertTrue(self.buffer.can_deliver(56.0))
        self.assertFalse(self.buffer.can_deliver(59.0))

    def test_sensor_count_reports_coverage(self):
        self.assertEqual(self.buffer.sensor_count, 6)

    def test_buffer_without_any_reading_is_not_covered(self):
        blind = Buffer(
            tanks=(tank("A", top=None, mid=None, bottom=None),),
            reference=30.0,
            ceiling=60.0,
        )

        self.assertFalse(blind.covered)
        self.assertEqual(blind.sensor_count, 0)
        self.assertIsNone(blind.charge_percent)



class SensorGuardTest(unittest.TestCase):
    """Hvilket tal der kan bruges til at opdage en død føler."""

    def test_the_layer_count_does_not_notice_one_dead_sensor(self):
        # ``layers`` interpolerer det manglende lag og giver stadig tre, så
        # ``sensor_count`` står stille. Det er rigtigt til energiregnskabet
        # og ubrugeligt som vagt.
        whole = Buffer((Tank("A", 500.0, 60.0, 50.0, 40.0),), 30.0, 60.0)
        dead = Buffer((Tank("A", 500.0, 60.0, 50.0, None),), 30.0, 60.0)

        self.assertEqual(whole.sensor_count, dead.sensor_count)

    def test_but_the_lost_count_does(self):
        whole = Buffer((Tank("A", 500.0, 60.0, 50.0, 40.0),), 30.0, 60.0)
        dead = Buffer((Tank("A", 500.0, 60.0, 50.0, None),), 30.0, 60.0)

        self.assertEqual(whole.sensors_lost, 0)
        self.assertEqual(dead.sensors_lost, 1)

    def test_a_whole_silent_tank_counts_as_three_lost(self):
        # Den vigtigste af dem alle, og den der manglede. En tank uden ét
        # eneste svar er ikke med i ``measured``, så summen over de *målte*
        # tanke gav nul mistede følere - mens lageret halverede sig. Natten
        # til den 10. september svarede tank B ikke i ét minut: 22,6 -> 11,8
        # kWh, og vagten så ingenting.
        whole = Buffer(
            (Tank("A", 500.0, 60.0, 50.0, 40.0), Tank("B", 500.0, 58.0, 48.0, 38.0)),
            30.0,
            60.0,
        )
        silent = Buffer(
            (Tank("A", 500.0, 60.0, 50.0, 40.0), Tank("B", 500.0, None, None, None)),
            30.0,
            60.0,
        )

        self.assertEqual(whole.sensors_lost, 0)
        self.assertEqual(silent.sensors_lost, 3)
        # Og energien halverer sig, så vagten har noget at reagere på.
        self.assertLess(silent.heat_kwh, whole.heat_kwh * 0.6)

    def test_and_the_energy_really_does_jump(self):
        # Det er derfor det betyder noget: når føleren falder ud, hopper
        # energien - og en hældning over vinduet ville læse det som et
        # forbrug på flere kW og lære det ind i kurven.
        whole = Buffer((Tank("A", 500.0, 60.0, 50.0, 30.0),), 30.0, 60.0)
        dead = Buffer((Tank("A", 500.0, 60.0, 50.0, None),), 30.0, 60.0)

        self.assertGreater(abs(dead.heat_kwh - whole.heat_kwh), 1.5)


if __name__ == "__main__":
    unittest.main()


class RoomToTest(unittest.TestCase):
    """Pladsen måles op til den temperatur blokken lader ved."""

    def setUp(self):
        # Lag der ligger mellem de to temperaturer: 58 er over begge, 54
        # ligger imellem, 40 er under begge.
        self.buffer = Buffer((Tank("A", 500.0, 58.0, 54.0, 40.0),), 30.0, 60.0)

    def test_a_warmer_charge_temperature_leaves_more_room(self):
        # Med 53 ses der ingen plads over det lag der står på 54; med 56
        # er der to kelvin. Det var forskellen mellem ``dhw_setpoint`` - som
        # står på 53 på anlægget - og den rigtige ladetemperatur.
        self.assertGreater(self.buffer.room_to(56.0), self.buffer.room_to(53.0))

    def test_the_ceiling_itself_is_untouched(self):
        # ``headroom_kwh`` regner stadig op til loftet på 60, for det er
        # også det ``energy_to_reach`` og ``charge_percent`` bygger på.
        self.assertAlmostEqual(
            self.buffer.headroom_kwh, self.buffer.room_to(60.0), places=9
        )


class CompleteTest(unittest.TestCase):
    """Forskellen på «der er noget at vise» og «der er noget at handle på»."""

    def test_one_answering_tank_is_covered_but_not_complete(self):
        half = Buffer(
            (Tank("A", 500.0, 60.0, 50.0, 40.0), Tank("B", 500.0, None, None, None)),
            30.0,
            60.0,
        )

        self.assertTrue(half.covered)
        self.assertFalse(half.complete)
        self.assertEqual(half.silent, ("B",))

    def test_both_answering_is_complete(self):
        whole = Buffer(
            (Tank("A", 500.0, 60.0, 50.0, 40.0), Tank("B", 500.0, 58.0, 48.0, 38.0)),
            30.0,
            60.0,
        )

        self.assertTrue(whole.complete)
        self.assertEqual(whole.silent, ())

    def test_a_tank_missing_one_sensor_is_still_complete(self):
        # Ét dødt lag er ikke en tavs tank - gradientreglen dækker det, og
        # den skal ikke overtrumfes af en gammel aflæsning.
        gap = Buffer(
            (Tank("A", 500.0, 60.0, None, 40.0), Tank("B", 500.0, 58.0, 48.0, 38.0)),
            30.0,
            60.0,
        )

        self.assertTrue(gap.complete)
        self.assertEqual(gap.sensors_lost, 1)


class ChargePercentTest(unittest.TestCase):
    def test_energy_above_the_ceiling_does_not_inflate_the_percentage(self):
        # 90/70/40 med reference 30 og loft 60. Der stod stored/(stored +
        # headroom), og de to tællere målte ikke det samme: energi over
        # loftet talte med foroven men gav ingen rummelighed forneden.
        b = Buffer(tanks=(tank(top=90.0, mid=70.0, bottom=40.0),),
                   reference=30.0, ceiling=60.0)

        self.assertAlmostEqual(b.charge_percent, 77.8, places=1)

    def test_a_store_at_the_ceiling_is_full_and_no_more(self):
        b = Buffer(tanks=(tank(top=60.0, mid=60.0, bottom=60.0),),
                   reference=30.0, ceiling=60.0)

        self.assertAlmostEqual(b.charge_percent, 100.0, places=6)

    def test_lost_sensors_are_counted_across_the_store(self):
        b = Buffer(tanks=(tank(mid=None), tank(name="B", top=None, mid=None)),
                   reference=30.0, ceiling=60.0)

        self.assertEqual(b.sensors_lost, 3)


class CascadeTest(unittest.TestCase):
    """Anlægget lader tankene i rækkefølge, ikke parallelt.

    Afspærringsventilen til tank 2 åbner først når tank 1 er over 55 på
    topføleren. Det er med vilje: solvarmen lader fra bunden af tank 1, så
    ved kun at varme de første 500 L når lageret hurtigere en brugbar
    temperatur.
    """

    def store(self, a_top, b_top, cascade=55.0):
        return Buffer(
            tanks=(tank(top=a_top, mid=a_top - 11, bottom=a_top - 23),
                   tank(name="B", top=b_top, mid=b_top - 6, bottom=b_top - 12)),
            reference=30.0, ceiling=60.0, cascade_temp=cascade,
        )

    def test_a_gap_while_the_first_tank_fills_is_by_design(self):
        b = self.store(50.0, 38.0)

        self.assertGreater(b.imbalance, 5.0)
        self.assertTrue(b.cascade_filling)
        self.assertTrue(b.imbalance_is_by_design)

    def test_and_still_is_just_after_the_valve_opens(self):
        # Anlæggets egne tal 3. september: A 55,2/44,2/32,3, B 41,0/35,3/28,8.
        # Ventilen er lige åbnet ved 55, og tank 2 er ved at hente ind. At
        # kalde det en flowfejl ville være lige så forkert som at kalde
        # opfyldningen af tank 1 en fejl.
        b = self.store(55.2, 41.0)

        self.assertGreater(b.imbalance, 5.0)
        self.assertFalse(b.cascade_filling)
        self.assertTrue(b.imbalance_is_by_design)

    def test_but_not_once_the_first_tank_is_as_full_as_the_pump_can_make_it(self):
        # Rækkefølgen er kørt til ende uden at have rettet forskellen op.
        # Så er det flowet.
        b = self.store(60.5, 41.0)

        self.assertGreater(b.imbalance, 5.0)
        self.assertFalse(b.imbalance_is_by_design)

    def test_without_a_cascade_every_gap_is_a_flow_problem(self):
        b = self.store(50.0, 41.0, cascade=0.0)

        self.assertFalse(b.imbalance_is_by_design)

    def test_the_store_still_delivers_from_the_warm_tank(self):
        # Kaskaden ændrer ikke hvad lageret kan levere - det er den
        # varmeste afgang, ikke gennemsnittet.
        b = self.store(55.2, 41.0)

        self.assertAlmostEqual(b.deliverable, 55.2, places=6)
        self.assertTrue(b.can_deliver(54.0))

    def test_the_energy_is_the_sum_regardless(self):
        # Kaskaden er en rækkefølge, ikke en opdeling: begge tanke lades,
        # bare ikke samtidig. Energien og pladsen er summen som før.
        b = self.store(55.2, 41.0)

        self.assertAlmostEqual(
            b.stored_kwh,
            sum(t.stored_kwh(30.0) for t in b.tanks),
            places=9,
        )



class WholeTankChargeTest(unittest.TestCase):
    """Den 1. oktober: UVR'en løfter hele tanken, før toppen kommer over 55.

    Planen bad om 5,7 kWh for 1,82 kWh over 55 grader, regnet lag for lag
    oppefra. Men UVR'en lader med setpunktet toppen + 2 grader og bygger
    temperaturen op over hele tank A - dagen før endte en blok på 5,4 kWh med
    nul over 55.
    """

    def setUp(self):
        self.buffer = Buffer(
            (Tank("A", 500, 44.7, 45.0, 41.1), Tank("B", 500, 40.6, 34.0, 31.3)),
            reference=30.0,
            ceiling=60.0,
        )

    def test_the_whole_first_tank_is_lifted(self):
        per = 500 / 3 * WH_PER_LITER_K / 1000
        lift = per * ((55 - 44.7) + (55 - 45.0) + (55 - 41.1))

        self.assertAlmostEqual(
            self.buffer.energy_to_reach(1.82, 55.0), lift + 1.82, places=2
        )

    def test_the_second_tank_only_when_the_first_cannot_carry_it(self):
        # Tank A kan bære 5 K over 55 i alle tre lag. Mere end det, og B må med.
        per = 500 / 3 * WH_PER_LITER_K / 1000
        a_band = 3 * per * 5

        small = self.buffer.energy_to_reach(a_band - 0.1, 55.0)
        large = self.buffer.energy_to_reach(a_band + 0.1, 55.0)

        self.assertGreater(large - small, per * (55 - 31.3), "B's løft er med")

    def test_nothing_when_it_is_already_there(self):
        hot = Buffer((Tank("A", 500, 58.0, 57.0, 56.0),), reference=30.0, ceiling=60.0)

        self.assertEqual(hot.energy_to_reach(1.0, 55.0), 0.0)


class HotWaterTest(unittest.TestCase):
    """Den 1. oktober: et lag på 55-58 kan give beholderen varme ned til 44.

    Anlæggets ejer: «når tank A er 55-58 grader, så er der jo 500 liter der
    kan afkøles ned til ca. 44 grader, som retur er på VVB'en».
    """

    def setUp(self):
        self.per = 500 / 3 * WH_PER_LITER_K / 1000

    def test_a_hot_tank_counts_down_to_the_return(self):
        hot = Buffer((Tank("A", 500, 56.0, 55.0, 55.0),), reference=30.0, ceiling=60.0)

        self.assertAlmostEqual(
            hot.hot_water_kwh(55.0, 44.0), self.per * (12 + 11 + 11), places=3
        )
        # Det gamle tal talte kun det over grænsen.
        self.assertLess(hot.usable_kwh(55.0), 1.0)

    def test_layers_below_the_supply_give_nothing(self):
        cold = Buffer((Tank("A", 500, 54.0, 50.0, 45.0),), reference=30.0, ceiling=60.0)

        self.assertEqual(cold.hot_water_kwh(55.0, 44.0), 0.0)

    def test_lifting_top_and_mid_is_enough_for_the_evening(self):
        # Tankene den 1. oktober kl. 13:05, og 1,82 kWh varmt vand kl. 19-20.
        # Top og midt op til 55; bunden må blive hvor returen kommer ind.
        buffer = Buffer(
            (Tank("A", 500, 44.7, 45.0, 41.1), Tank("B", 500, 40.6, 34.0, 31.3)),
            reference=30.0,
            ceiling=60.0,
        )
        lift = self.per * ((55 - 44.7) + (55 - 45.0))

        self.assertAlmostEqual(
            buffer.energy_to_reach(1.82, 55.0, 44.0), lift, places=3
        )

    def test_top_and_mid_at_the_supply_count_the_whole_tank_to_the_return(self):
        # Ejerens valg: står top og midt på 55, er det 500 L × (middel − 44).
        warm = Buffer((Tank("A", 500, 56.0, 55.0, 47.0),), reference=30.0, ceiling=60.0)
        mean = (56.0 + 55.0 + 47.0) / 3

        self.assertAlmostEqual(
            warm.hot_water_kwh(55.0, 44.0),
            500 * WH_PER_LITER_K * (mean - 44.0) / 1000,
            places=3,
        )


class ThinHotLayerTest(unittest.TestCase):
    """Den 1. oktober kl. 14:42: blokken stoppede på A 55,7/46/47.

    Toppen alene talte 2,2 kWh ned til returen, og aftenen skulle bruge 1,82.
    Men et varmt lag over et skillelag er ikke 167 liter badevand: toppen
    falder under 53, når beholderen trækker, og pumpen starter.
    """

    def setUp(self):
        self.buffer = Buffer(
            (Tank("A", 500, 55.7, 46.0, 47.0), Tank("B", 500, 40.0, 34.0, 31.0)),
            reference=30.0,
            ceiling=60.0,
        )
        self.per = 500 / 3 * WH_PER_LITER_K / 1000

    def test_a_thin_hot_layer_counts_only_above_the_supply(self):
        self.assertAlmostEqual(
            self.buffer.hot_water_kwh(55.0, 44.0), self.per * 0.7, places=3
        )

    def test_and_so_the_middle_must_be_lifted(self):
        lift = self.per * (55 - 46.0)

        self.assertAlmostEqual(
            self.buffer.energy_to_reach(1.82, 55.0, 44.0), lift, places=3
        )
