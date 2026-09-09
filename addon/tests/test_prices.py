import unittest

from varmeopt.prices import (
    BATTERY,
    BATTERY_ROUND_TRIP,
    DISCHARGE,
    EXPORT,
    EXPORT_FLOOR,
    LOCKED,
    NET,
    SUN,
    Grid,
    Plan,
    Slot,
)


def row(state="", import_rate=200, export_rate=80, soc=50):
    return {
        "state": state,
        "import_rate": import_rate,
        "export_rate": export_rate,
        "soc_percent": soc,
    }


def plan(*rows, cheapest=None):
    """Planen.

    ``cheapest`` laegger en billig halvtime bagest, saa batteriets
    genanskaffelsespris bliver et kendt tal: den billigste import der er
    tilbage, ganget op med tabet hele vejen rundt. Uden den ligger alle
    raekker paa 2,00, og saa er der ingenting at hente ved at gemme energi -
    loftet «aldrig dyrere end at koebe den nu» svarer for dem alle.
    """
    rows = list(rows)
    if cheapest is not None:
        rows.append(row(import_rate=cheapest * 100))
    return Plan.from_predbat({"raw": {"rows": rows}})


class ParseTest(unittest.TestCase):
    def test_rates_come_in_oere_and_are_converted(self):
        p = plan(row(import_rate=250, export_rate=95))

        self.assertAlmostEqual(p.slots[0].import_price, 2.50, places=9)
        self.assertAlmostEqual(p.slots[0].export_price, 0.95, places=9)

    def test_slots_are_half_hours_from_now(self):
        p = plan(row(), row(), row())

        self.assertEqual([s.minutes_ahead for s in p.slots], [0, 30, 60])
        self.assertEqual(p.horizon_minutes, 90)

    def test_missing_rates_survive_as_none(self):
        p = plan({"state": "chrg"})

        self.assertIsNone(p.slots[0].import_price)
        self.assertEqual(p.slots[0].state, "chrg")

    def test_garbage_gives_an_empty_plan(self):
        for junk in (None, "ikke en plan", {}, {"raw": "noget"}, {"raw": {"rows": "aeh"}}):
            self.assertEqual(len(Plan.from_predbat(junk)), 0)

class SlotStateTest(unittest.TestCase):
    def test_the_five_states_the_plant_actually_sends(self):
        # Anlaeggets egne fem, som ejeren har bekraeftet dem.
        self.assertEqual(Slot(0, "Demand", None, None, None).mode, DISCHARGE)
        self.assertEqual(Slot(0, "Chrg", None, None, None).mode, LOCKED)
        self.assertEqual(Slot(0, "HoldChrg", None, None, None).mode, LOCKED)
        self.assertEqual(Slot(0, "Exp", None, None, None).mode, EXPORT)
        self.assertEqual(Slot(0, "FrzExp", None, None, None).mode, EXPORT)

    def test_nothing_planned_means_the_inverter_carries_the_house(self):
        self.assertEqual(Slot(0, "", None, None, None).mode, DISCHARGE)
        self.assertTrue(Slot(0, "", None, None, None).understood)

    def test_a_word_we_do_not_know_locks_the_battery(self):
        # Foer faldt den igennem til "batteriet er frit" - den billigste og
        # farligste af de tre muligheder.
        slot = Slot(0, "SuperEcoTurbo", None, None, None)

        self.assertEqual(slot.mode, LOCKED)
        self.assertFalse(slot.understood)

    def test_hold_charge_does_not_refill_the_battery(self):
        # Den laaser afladningen, men den haever ikke ladetilstanden. Kun en
        # rigtig ladning tæller som en paafyldning.
        self.assertTrue(Slot(0, "chrg", None, None, None).refills)
        self.assertFalse(Slot(0, "holdchrg", None, None, None).refills)
        self.assertFalse(Slot(0, "frzchrg", None, None, None).refills)


class MarginalTest(unittest.TestCase):
    def test_planned_export_costs_the_lost_income(self):
        p = plan(row(state="exp", export_rate=140))

        price = p.marginal(0)

        self.assertAlmostEqual(price.kr_per_kwh, 1.40, places=9)
        self.assertIn("eksport", price.reason)

    def test_a_locked_battery_means_the_pump_runs_on_the_grid(self):
        # "hold charge": Predbat saetter afladningen til 0, og resten af
        # husets forbrug - varmepumpen med - kommer fra nettet.
        p = plan(row(state="holdchrg", import_rate=180))

        price = p.marginal(0)

        self.assertAlmostEqual(price.kr_per_kwh, 1.80, places=9)
        self.assertEqual(price.source, NET)
        self.assertIn("afladning", price.reason)

    def test_hold_charge_above_the_floor_is_still_the_battery(self):
        # Predbat skriver et gulv til inverteren - "her maa der aflades ned
        # til". Staar holdet ti point under ladetilstanden, er de ti point
        # rigtig energi, og den naeste kilowatt-time kommer derfra.
        p = plan(row(state="holdchrg", soc=40, import_rate=180))

        price = p.marginal(0, grid=Grid(discharge_floor=30.0))

        self.assertEqual(price.source, BATTERY)
        self.assertIn("hold charge ned til 30 %", price.reason)

    def test_hold_charge_at_the_floor_is_the_grid(self):
        # Samme hold, men ladetilstanden ligger paa gulvet. Saa er der ikke
        # mere at aflade, og huset koeber.
        p = plan(row(state="holdchrg", soc=30, import_rate=180))

        price = p.marginal(0, grid=Grid(discharge_floor=30.0))

        self.assertEqual(price.source, NET)
        self.assertAlmostEqual(price.kr_per_kwh, 1.80, places=9)

    def test_a_real_charge_stays_on_the_grid_however_full_it_is(self):
        # Mens der lades fra nettet, aflader inverteren ikke - uanset at
        # ladetilstanden ligger langt over gulvet.
        p = plan(row(state="chrg", soc=90, import_rate=180))

        price = p.marginal(0, grid=Grid(discharge_floor=30.0))

        self.assertEqual(price.source, NET)
        self.assertIn("lades", price.reason)

    def test_the_floor_only_speaks_for_the_half_hour_we_are_in(self):
        # Gulvet er hvad der er skrevet til inverteren *nu*. En halvtime
        # frem har kun planens ord, og der er hold charge stadig et hold.
        p = plan(
            row(soc=40),
            row(state="holdchrg", soc=40, import_rate=180),
        )

        price = p.marginal(30, grid=Grid(discharge_floor=30.0))

        self.assertEqual(price.source, NET)

    def test_an_unknown_state_costs_the_grid_and_says_so(self):
        p = plan(row(state="Ecoo", import_rate=180))

        price = p.marginal(0)

        self.assertAlmostEqual(price.kr_per_kwh, 1.80, places=9)
        self.assertEqual(price.source, NET)
        self.assertIn("ukendt", price.reason)

    def test_a_free_battery_costs_what_it_takes_to_replace(self):
        p = plan(row(), row(), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        # Ikke hvad energien kostede engang. Den billigste import der er
        # tilbage er 1,00, og der skal koebes 1/0,832 for at have den igen.
        self.assertAlmostEqual(price.kr_per_kwh, 1.00 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.reason)

    def test_energy_is_valued_against_a_coming_export(self):
        # Eksport om en time til 1,60 er mere vaerd end de 1,20 det koster
        # at skaffe energien igen.
        #
        # Det er en vaerdisaettelse, ikke en beslutning: om energien faktisk
        # bliver gemt, afgoeres af hvad den ellers skulle bruges til.
        p = plan(row(), row(), row(state="exp", export_rate=160), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.60 * 0.90, places=9)
        self.assertIn("værdisat mod eksport", price.reason)

    def test_a_cheap_charge_soon_frees_the_battery(self):
        p = plan(row(), row(state="chrg", import_rate=40))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        # Den kendte paafyldning, ikke bare den billigste i horisonten:
        # bruger vi en kWh nu og fylder den paa om en halv time, koster den
        # hvad *den* paafyldning koster - og der skal koebes 1/0,832 for at
        # faa 1 igen.
        self.assertAlmostEqual(
            price.kr_per_kwh, 0.40 / BATTERY_ROUND_TRIP, places=9
        )
        self.assertIn("lades om", price.reason)

    def test_a_planned_discharge_is_not_read_as_a_charge(self):
        # "dischrg" indeholder "chrg". Uden afladningstesten foerst blev hver
        # eneste planlagte afladning laest som en opladning - halvtimen blev
        # prissat som om batteriet var bundet.
        p = plan(row(state="dischrg", import_rate=300))

        self.assertEqual(p.slots[0].mode, DISCHARGE)
        self.assertFalse(p.slots[0].refills)
        self.assertFalse(p.slots[0].locked)

    def test_an_almost_empty_battery_is_priced_as_grid(self):
        # Uanset hvad de sidste par procent kostede engang, kan de ikke
        # levere den naeste kWh til en varmepumpe paa 16 kW.
        p = plan(row(soc=4, import_rate=300))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 3.00, places=9)
        self.assertIn("tom", price.reason)

    def test_the_reserve_is_not_a_floor_under_the_heat_pump(self):
        # Predbats reserve paa 14 % er 5 kWh gemt til uplanlagt forbrug - og
        # en varmepumpe der starter, *er* uplanlagt forbrug. Anlaegget kan
        # aflade til 5 %. Reserven siger kun at Predbat ikke vil *saelge*
        # den energi, ikke at den ikke maa bruges.
        p = plan(
            row(soc=14, import_rate=170),
            row(soc=14, import_rate=160),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(p.reserve, 14)
        self.assertEqual(price.source, BATTERY)

    def test_an_empty_battery_is_the_grid(self):
        # Under anlaeggets eget nulpunkt kan inverteren ikke levere.
        p = plan(row(soc=4, import_rate=170))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(price.source, NET)
        self.assertAlmostEqual(price.kr_per_kwh, 1.70, places=9)
        self.assertIn("tomt", price.reason)

    def test_a_battery_on_the_reserve_still_answers(self):
        # Batteriet ligger paa Predbats reserve, og der lades halvanden time
        # senere. Energien over anlaeggets nulpunkt er der stadig, saa den
        # naeste kilowatt-time er batteriets - og den koster hvad det koster
        # at fylde den paa igen.
        p = plan(
            row(soc=16, import_rate=109),
            row(soc=16, import_rate=109),
            row(soc=15, import_rate=95),
            row(state="chrg", soc=30, import_rate=85),
            row(soc=15),
            row(soc=15),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(p.reserve, 15)
        self.assertEqual(price.source, BATTERY)
        self.assertAlmostEqual(
            price.kr_per_kwh, 0.85 / BATTERY_ROUND_TRIP, places=9
        )
        self.assertIn("lades om", price.reason)

    def test_a_plain_export_sells_the_battery(self):
        # "exp" toemmer batteriet ud paa nettet. Tager varmepumpen en
        # kilowatt-time, er det batteriets - prisen er den mistede indtaegt.
        p = plan(row(state="exp", export_rate=140))

        price = p.marginal(0)

        self.assertEqual(price.source, BATTERY)
        self.assertAlmostEqual(price.kr_per_kwh, 1.40, places=9)

    def test_a_frozen_export_sells_the_sun(self):
        # "frzexp" holder ladetilstanden og saelger solen. Saa er det solens
        # kilowatt-time der bliver brugt, ikke batteriets.
        p = plan(row(state="frzexp", export_rate=140))

        price = p.marginal(0)

        self.assertEqual(price.source, SUN)
        self.assertAlmostEqual(price.kr_per_kwh, 1.40, places=9)

    def test_an_empty_battery_under_a_covering_sun_is_the_sun(self):
        # Batteriet er ude af spillet, men panelerne baerer huset og der
        # koebes ikke. Saa er det solen der leverer den naeste kilowatt-time.
        p = plan(row(soc=4, import_rate=170))

        price = p.marginal(0, grid=Grid(pv_power=4000))

        self.assertEqual(price.source, SUN)

    def test_a_flat_plan_high_up_is_not_a_bottom(self):
        # Staar ladetilstanden stille paa 70 %, er det solen der daekker
        # huset. Det er ikke et tomt batteri, og energien koster hvad den
        # koster at skaffe igen.
        p = plan(row(soc=70), row(soc=70), row(soc=70), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIsNone(p.reserve)
        self.assertAlmostEqual(price.kr_per_kwh, 1.00 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.reason)

    def test_a_frozen_export_on_the_floor_still_marks_the_reserve(self):
        # Predbat saelger ikke under reserven, saa en frossen eksport dernede
        # ligger der netop fordi det *er* bunden. Kravet er kun at bunden
        # ogsaa ses ét sted hvor batteriet maatte aflade.
        p = plan(
            row(soc=30),
            row(state="frzexp", soc=12),
            row(state="frzexp", soc=12),
            row(soc=12),
            row(soc=20),
        )

        self.assertEqual(p.reserve, 12)

    def test_a_hold_alone_does_not_name_a_reserve(self):
        # Staar ladetilstanden stille fordi Predbat holder batteriet, er det
        # ikke fordi der ikke er noget i det. Uden en afladning dernede er
        # der ingen bund at laese.
        p = plan(
            row(soc=30),
            row(state="holdchrg", soc=12),
            row(state="holdchrg", soc=12),
        )

        self.assertIsNone(p.reserve)

    def test_a_single_dip_is_not_a_bottom(self):
        # Et dyk til 20 % er ikke en bund - batteriet kommer op igen af sig
        # selv, og der er noget at tage af hele vejen.
        p = plan(row(soc=30), row(soc=20), row(soc=30), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIsNone(p.reserve)
        self.assertAlmostEqual(price.kr_per_kwh, 1.00 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.reason)

    def test_a_battery_that_runs_dry_costs_what_it_takes_to_buy_back(self):
        # Batteriet daekker huset nu, men planen viser det i bund laenge foer
        # det lades igen. Saa er den kWh vi bruger nu, praecis den kWh vi
        # koeber til 1,73 naar batteriet staar tomt - ikke de 1,00 den
        # kostede engang. Gennemsnittet er sunk cost.
        p = plan(
            row(soc=37, import_rate=190),
            row(soc=20, import_rate=180),
            row(soc=4, import_rate=173),
            row(soc=4, import_rate=170),
            row(soc=4, import_rate=150),
            row(soc=5, import_rate=140),
            row(state="chrg", soc=40, import_rate=85),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.73, places=9)
        self.assertIn("købes tilbage om 60 min", price.reason)

    def test_energy_the_plan_sells_before_it_runs_dry_costs_the_export(self):
        # Batteriet naar bunden inden det lades - men det er en planlagt
        # eksport der toemmer det. Saa er den kWh vi bruger nu, ikke en der
        # skal koebes tilbage til importprisen i bunden; det er en der ikke
        # bliver solgt, og prisen er den mistede indtaegt.
        p = plan(
            row(soc=37, import_rate=190),
            row(soc=30, import_rate=180),
            row(state="exp", soc=24, export_rate=115),
            row(soc=4, import_rate=185),
            row(soc=4, import_rate=173),
            row(state="chrg", soc=40, import_rate=85),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        # Prisen er den mistede indtaegt. Hvilken af de to eksportgrene der
        # svarer, er underordnet - tallet er det samme, og det er tallet der
        # afgoer valget.
        self.assertAlmostEqual(price.kr_per_kwh, 1.15 * 0.90, places=9)
        self.assertIn("eksport", price.reason)

    def test_without_a_sale_first_it_is_still_bought_back(self):
        # Samme plan uden eksporten: saa er bunden en bund, og den kWh vi
        # bruger nu, koeber vi fra nettet naar den mangler.
        p = plan(
            row(soc=37, import_rate=190),
            row(soc=30, import_rate=180),
            row(soc=24, import_rate=180),
            row(soc=4, import_rate=185),
            row(soc=4, import_rate=173),
            row(state="chrg", soc=40, import_rate=85),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.85, places=9)
        self.assertIn("købes tilbage", price.reason)

    def test_a_charge_before_the_bottom_leaves_the_replacement_alone(self):
        # Fyldes batteriet inden det loeber toert, er energien ikke
        # disponeret - saa skal den ikke koebes tilbage i bunden, og prisen
        # er den almindelige genanskaffelse. Ladningen ligger 150 min ude og
        # er dermed heller ikke "snart", saa det er den billigste import i
        # horisonten der svarer: 0,85.
        p = plan(
            row(soc=40), row(soc=38), row(soc=36), row(soc=34), row(soc=32),
            row(state="chrg", soc=60, import_rate=85),
            row(soc=14), row(soc=14),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 0.85 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.reason)

    def test_the_grid_wins_when_the_inverter_is_already_maxed(self):
        # Baade "batteriet aflader" og "vi importerer" kan vaere sande paa
        # en gang: saa staar inverteren paa sit loft, og ekstra forbrug kan
        # kun komme fra nettet. 12 kW inverter mod 16 kW varmepumpe.
        p = plan(row(import_rate=300))

        price = p.marginal(0, grid=Grid(battery_power=3000, grid_power=4000))

        self.assertAlmostEqual(price.kr_per_kwh, 3.00, places=9)
        self.assertIn("import", price.reason)

    def test_export_valuation_never_makes_energy_cheaper(self):
        # I baandet snit < eksport < snit/0,90 vendte grenen sit formaal paa
        # hovedet: snit 1,00 og eksport 1,05 gav 0,945.
        p = plan(row(), row(state="exp", export_rate=105))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertGreaterEqual(price.kr_per_kwh, 1.0)

    def test_a_battery_on_its_reserve_is_priced_at_its_replacement(self):
        # Under bunden bliver energien aldrig solgt - Predbat eksporterer
        # ikke derunder - saa en kommende eksport er ikke et alternativ til
        # at bruge den. Bunden er planens egen reserve.
        p = plan(
            row(soc=15),
            row(soc=15),
            row(state="exp", export_rate=160, soc=15),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(p.reserve, 15)
        self.assertAlmostEqual(price.kr_per_kwh, 1.00 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.reason)

    def test_the_best_paid_export_decides_not_the_first(self):
        # Den marginale kilowatt-time bliver solgt i den bedste halvtime der
        # er tilbage. En tidlig, daarligt betalt eksport siger ingenting om
        # hvad energien er vaerd.
        p = plan(
            row(soc=60),
            row(state="exp", export_rate=90, soc=55),
            row(state="exp", export_rate=210, soc=45),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 2.10 * 0.90, places=9)
        self.assertIn("om 60 min", price.reason)

    def test_an_export_far_out_still_counts(self):
        # Der stod foer en graense paa tre timer. Planen kender salget tolv
        # timer i forvejen, og om det ligger to eller elleve timer ude, er
        # energien lige meget lovet vaek.
        p = plan(
            row(soc=60),
            *[row(soc=55) for _ in range(15)],
            row(state="exp", export_rate=210, soc=45),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 2.10 * 0.90, places=9)
        self.assertIn("om 480 min", price.reason)

    def test_a_charge_before_the_sale_takes_the_export_out_of_play(self):
        # Fyldes batteriet foer salget, er det ikke *den her* kilowatt-time
        # der bliver solgt bagefter.
        p = plan(
            row(soc=60),
            row(state="chrg", import_rate=100, soc=90),
            row(state="exp", export_rate=210, soc=45),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("lades om", price.reason)

    def test_a_full_battery_can_afford_to_be_valued_against_export(self):
        p = plan(
            row(soc=75),
            row(),
            row(state="exp", export_rate=160),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.60 * 0.90, places=9)
        self.assertIn("SOC 75 %", price.reason)

    def test_an_unknown_soc_is_assumed_to_be_enough(self):
        p = plan(
            {"state": "", "import_rate": 200, "export_rate": 80},
            row(),
            row(state="exp", export_rate=160),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("værdisat mod eksport", price.reason)

    def test_an_export_that_pays_less_than_the_battery_is_not_worth_saving_for(self):
        p = plan(row(), row(state="exp", export_rate=60), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("genanskaffelse", price.reason)

    def test_physical_export_beats_the_plan(self):
        # Planen siger ingenting, men maaleren siger at der gaar stroem ud.
        p = plan(row(export_rate=120))

        price = p.marginal(0, grid=Grid(grid_power=-4000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.20, places=9)
        self.assertIn("eksport", price.reason)

    def test_physical_import_is_priced_at_the_grid(self):
        p = plan(row(import_rate=210))

        price = p.marginal(0, grid=Grid(grid_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 2.10, places=9)
        self.assertIn("import", price.reason)

    def test_no_measurable_flow_still_means_the_battery(self):
        # Maaleren ser hverken import, eksport eller en afladning vaerd at
        # naevne. Foer gav det grenen "balanceret", som ikke var en kilde;
        # anlaeggets regel er at inverteren daekker forbruget, saa kilden er
        # batteriet. Prisen er stadig loftet af hvad nettet tager.
        p = plan(row(import_rate=60))

        price = p.marginal(0, grid=Grid())

        self.assertAlmostEqual(price.kr_per_kwh, 0.60, places=9)
        self.assertEqual(price.source, BATTERY)
        self.assertIn("genanskaffelse", price.reason)

    def test_the_sun_is_named_when_it_carries_the_house(self):
        p = plan(row(import_rate=200))

        price = p.marginal(0, grid=Grid(pv_power=4000))

        self.assertEqual(price.source, BATTERY)
        self.assertIn("solen daekker huset", price.reason)

    def test_beyond_the_horizon_there_is_no_price(self):
        self.assertIsNone(plan(row()).marginal(600))


class FutureTest(unittest.TestCase):
    """Det nye: en pris for en halvtime vi endnu ikke er naaet til."""

    def test_a_future_slot_is_priced_from_the_plan_alone(self):
        p = plan(row(), row(state="exp", export_rate=150), row(state="chrg", import_rate=30))

        self.assertIn("eksport", p.marginal(30).reason)
        self.assertEqual(p.marginal(60).source, NET)

    def test_the_physical_reading_only_applies_to_the_slot_we_are_in(self):
        # Grid gaelder nu. En halvtime frem maa planen staa alene.
        p = plan(row(import_rate=200), row(import_rate=50))

        now = p.marginal(0, grid=Grid(grid_power=4000))
        later = p.marginal(30, grid=Grid(grid_power=4000))

        self.assertAlmostEqual(now.kr_per_kwh, 2.00, places=9)
        self.assertNotEqual(later.reason, "net: import")


class WindowTest(unittest.TestCase):
    def test_finds_the_cheapest_stretch(self):
        p = plan(
            row(state="holdchrg", import_rate=300),
            row(state="holdchrg", import_rate=100),
            row(state="holdchrg", import_rate=90),
            row(state="holdchrg", import_rate=400),
        )

        start, average = p.cheapest_window(60)

        self.assertEqual(start, 30)
        self.assertAlmostEqual(average, (1.00 + 0.90) / 2, places=9)

    def test_a_deadline_rules_out_later_windows(self):
        p = plan(
            row(state="holdchrg", import_rate=300),
            row(state="holdchrg", import_rate=280),
            row(state="holdchrg", import_rate=10),
        )

        start, _ = p.cheapest_window(30, before_minutes=60)

        self.assertEqual(start, 30)

    def test_a_window_longer_than_the_horizon_has_no_answer(self):
        p = plan(row(state="holdchrg"), row(state="holdchrg"))

        self.assertIsNone(p.cheapest_window(300))

    def test_a_deadline_that_leaves_no_room_has_no_answer(self):
        p = plan(row(state="holdchrg"), row(state="holdchrg"))

        self.assertIsNone(p.cheapest_window(60, before_minutes=30))


if __name__ == "__main__":
    unittest.main()


class VocabularyTest(unittest.TestCase):
    """Ordforraadet er nu efterproevet mod anlaegget.

    Fire debug-udtraek fra 3.-4. september 2026 indeholder praecis fem ord:
    Demand, Chrg, HoldChrg, Exp og FrzExp. Ejeren har bekraeftet hvad de
    betyder. Resten herunder er stavemaader af de samme handlinger.
    """

    def test_the_states_predbat_is_known_to_write_are_understood(self):
        for state in ("Chrg", "Dischrg", "FrzChrg", "FrzDischrg", "HoldChrg",
                      "Exp", "FrzExp", "Hold", "Demand", ""):
            with self.subTest(state=state):
                p = plan(row(state=state))
                self.assertTrue(p.slots[0].understood, state)

    def test_words_the_plant_never_sends_are_not_guessed_at(self):
        # "Idle" og "ecoo" findes i Predbat, men ikke paa det her anlaeg, og
        # hvad de praecis goer ved inverteren ville vaere et gaet. Et gaet i
        # tabellen ville se ud som viden. De laaser i stedet og siger det.
        for state in ("Idle", "ecoo"):
            with self.subTest(state=state):
                p = plan(row(state=state, import_rate=180))

                self.assertFalse(p.slots[0].understood)
                self.assertEqual(p.slots[0].mode, LOCKED)
                self.assertIn("ukendt", p.marginal(0).reason)

    def test_a_state_we_cannot_read_is_flagged_not_swallowed(self):
        # Kan vi ikke tyde den, laases halvtimen til importprisen - og saa
        # skal det staa baade i loggen og i begrundelsen.
        p = plan(row(state="Turboladning"))

        self.assertFalse(p.slots[0].understood)

    def test_reading_a_plan_with_an_unknown_state_warns_once(self):
        with self.assertLogs("varmeopt.prices", level="WARNING") as caught:
            plan(row(state="Turboladning"), row(state="Turboladning"))

        self.assertEqual(len(caught.records), 1)
        self.assertIn("turboladning", caught.output[0].lower())


class ReplacementCostTest(unittest.TestCase):
    """Batteriets energi koster hvad det koster at laegge den tilbage."""

    def test_it_is_the_cheapest_import_ahead_grossed_up_by_the_losses(self):
        # Ikke hvad energien kostede engang - hvad den koster at skaffe igen.
        p = plan(row(import_rate=300), row(import_rate=100))

        self.assertAlmostEqual(p.replacement_cost(0), 1.00 / BATTERY_ROUND_TRIP, places=9)

    def test_it_only_looks_forward(self):
        # Den billige halvtime ligger bag os set fra den sidste raekke, og en
        # pris vi ikke kan naa, kan ingen kilowatt-time laegges tilbage til.
        p = plan(row(import_rate=100), row(import_rate=300))

        self.assertAlmostEqual(p.replacement_cost(1), 3.00 / BATTERY_ROUND_TRIP, places=9)

    def test_the_export_floor_still_holds_the_bottom(self):
        # Selv gratis energi er mindst det vaerd man kan saelge den for.
        p = plan(row(import_rate=0))

        self.assertAlmostEqual(p.replacement_cost(), EXPORT_FLOOR, places=9)

    def test_a_plan_without_prices_falls_to_the_floor(self):
        self.assertAlmostEqual(
            Plan.from_predbat(None).replacement_cost(), EXPORT_FLOOR, places=9
        )

    def test_it_makes_charging_the_tanks_during_a_battery_charge_pay(self):
        # Predbat lader batteriet til 1,00. Koerer varmepumpen paa nettet i
        # den samme halvtime, koster stroemmen 1,00 - gaar den samme energi
        # gennem batteriet foerst, koster den 1,00/0,832.
        during = plan(row(state="chrg", import_rate=100))
        later = plan(row(import_rate=300), row(state="chrg", import_rate=100))

        now = during.marginal(0, grid=Grid(grid_power=5000))
        via_battery = later.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(now.kr_per_kwh, 1.00, places=9)
        self.assertGreater(via_battery.kr_per_kwh, now.kr_per_kwh)
        self.assertAlmostEqual(
            via_battery.kr_per_kwh / now.kr_per_kwh, 1 / BATTERY_ROUND_TRIP, places=6
        )

