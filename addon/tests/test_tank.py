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
        # Midt imellem 60 og 30 ligger 45 - praecis det der stod der.
        self.assertEqual(gap.layers, (60.0, 45.0, 30.0))

    def test_a_lost_bottom_sensor_does_not_inflate_the_store_by_half(self):
        # 500 L med 60/50/30 over reference 30 rummer 9,57 kWh. Da de to
        # maalte lag daekkede hele tanken, blev det til 14,36 - halvdelen
        # mere varme end der var, netop naar der var mindst grund til at tro
        # paa tallet.
        whole = tank(top=60.0, mid=50.0, bottom=30.0)
        lost = tank(top=60.0, mid=50.0, bottom=None)

        truth = whole.stored_kwh(30.0)
        self.assertAlmostEqual(truth, 9.57, places=2)
        # Gradienten forlaenges: 60, 50 -> 40.
        self.assertEqual(lost.layers, (60.0, 50.0, 40.0))

        # Fejlen er 1,92 kWh mod de 4,79 den gamle udgave gav. Den er ikke
        # vaek: en rigtig tank har en termoklin, saa bunden ligger koldere
        # end en ret linje siger, og en fremskrivning overvurderer den
        # altid lidt. Men den er mindre end det halve.
        was = (500 / 2) * (30 + 20) * 1.149 / 1000
        self.assertLess(abs(lost.stored_kwh(30.0) - truth), 0.5 * abs(was - truth))

    def test_a_lost_top_sensor_extends_the_gradient_upward(self):
        lost = tank(top=None, mid=50.0, bottom=30.0)

        self.assertEqual(lost.layers, (70.0, 50.0, 30.0))

    def test_an_inverted_profile_falls_back_on_the_nearest_layer(self):
        # Bunden varmere end midten er enten omroert eller en foeler ude af
        # kalibrering. Saa er en fremskrivning vaerre end det naermeste maal.
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
        # paa de to lofter, og den afgoer om en soldag kan laeres af.
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


if __name__ == "__main__":
    unittest.main()


class ChargePercentTest(unittest.TestCase):
    def test_energy_above_the_ceiling_does_not_inflate_the_percentage(self):
        # 90/70/40 med reference 30 og loft 60. Der stod stored/(stored +
        # headroom), og de to taellere maalte ikke det samme: energi over
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
    """Anlaegget lader tankene i raekkefoelge, ikke parallelt.

    Afspaerringsventilen til tank 2 aabner foerst naar tank 1 er over 55 paa
    topfoeleren. Det er med vilje: solvarmen lader fra bunden af tank 1, saa
    ved kun at varme de foerste 500 L naar lageret hurtigere en brugbar
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
        # Anlaeggets egne tal 3. september: A 55,2/44,2/32,3, B 41,0/35,3/28,8.
        # Ventilen er lige aabnet ved 55, og tank 2 er ved at hente ind. At
        # kalde det en flowfejl ville vaere lige saa forkert som at kalde
        # opfyldningen af tank 1 en fejl.
        b = self.store(55.2, 41.0)

        self.assertGreater(b.imbalance, 5.0)
        self.assertFalse(b.cascade_filling)
        self.assertTrue(b.imbalance_is_by_design)

    def test_but_not_once_the_first_tank_is_as_full_as_the_pump_can_make_it(self):
        # Raekkefoelgen er koert til ende uden at have rettet forskellen op.
        # Saa er det flowet.
        b = self.store(60.5, 41.0)

        self.assertGreater(b.imbalance, 5.0)
        self.assertFalse(b.imbalance_is_by_design)

    def test_without_a_cascade_every_gap_is_a_flow_problem(self):
        b = self.store(50.0, 41.0, cascade=0.0)

        self.assertFalse(b.imbalance_is_by_design)

    def test_the_store_still_delivers_from_the_warm_tank(self):
        # Kaskaden aendrer ikke hvad lageret kan levere - det er den
        # varmeste afgang, ikke gennemsnittet.
        b = self.store(55.2, 41.0)

        self.assertAlmostEqual(b.deliverable, 55.2, places=6)
        self.assertTrue(b.can_deliver(54.0))

    def test_the_energy_is_the_sum_regardless(self):
        # Kaskaden er en raekkefoelge, ikke en opdeling: begge tanke lades,
        # bare ikke samtidig. Energien og pladsen er summen som foer.
        b = self.store(55.2, 41.0)

        self.assertAlmostEqual(
            b.stored_kwh,
            sum(t.stored_kwh(30.0) for t in b.tanks),
            places=9,
        )

