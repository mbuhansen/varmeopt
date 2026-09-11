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

    ``cheapest`` lægger en billig halvtime bagest, så batteriets
    genanskaffelsespris bliver et kendt tal: den billigste import der er
    tilbage, ganget op med tabet hele vejen rundt. Uden den ligger alle
    rækker på 2,00, og så er der ingenting at hente ved at gemme energi -
    loftet «aldrig dyrere end at købe den nu» svarer for dem alle.
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
        # Anlæggets egne fem, som ejeren har bekræftet dem.
        self.assertEqual(Slot(0, "Demand", None, None, None).mode, DISCHARGE)
        self.assertEqual(Slot(0, "Chrg", None, None, None).mode, LOCKED)
        self.assertEqual(Slot(0, "HoldChrg", None, None, None).mode, LOCKED)
        self.assertEqual(Slot(0, "Exp", None, None, None).mode, EXPORT)
        self.assertEqual(Slot(0, "FrzExp", None, None, None).mode, EXPORT)

    def test_nothing_planned_means_the_inverter_carries_the_house(self):
        self.assertEqual(Slot(0, "", None, None, None).mode, DISCHARGE)
        self.assertTrue(Slot(0, "", None, None, None).understood)

    def test_a_word_we_do_not_know_locks_the_battery(self):
        # Før faldt den igennem til "batteriet er frit" - den billigste og
        # farligste af de tre muligheder.
        slot = Slot(0, "SuperEcoTurbo", None, None, None)

        self.assertEqual(slot.mode, LOCKED)
        self.assertFalse(slot.understood)

    def test_hold_charge_does_not_refill_the_battery(self):
        # Den låser afladningen, men den hæver ikke ladetilstanden. Kun en
        # rigtig ladning tæller som en påfyldning.
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
        # "hold charge": Predbat sætter afladningen til 0, og resten af
        # husets forbrug - varmepumpen med - kommer fra nettet.
        p = plan(row(state="holdchrg", import_rate=180))

        price = p.marginal(0)

        self.assertAlmostEqual(price.kr_per_kwh, 1.80, places=9)
        self.assertEqual(price.source, NET)
        self.assertIn("afladning", price.detail)

    def test_hold_charge_above_the_floor_is_still_the_battery(self):
        # Predbat skriver et gulv til inverteren - "her må der aflades ned
        # til". Står holdet ti point under ladetilstanden, er de ti point
        # rigtig energi, og den næste kilowatt-time kommer derfra.
        p = plan(row(state="holdchrg", soc=40, import_rate=180))

        price = p.marginal(0, grid=Grid(discharge_floor=30.0))

        self.assertEqual(price.source, BATTERY)
        self.assertIn("hold charge ned til 30 %", price.detail)

    def test_hold_charge_at_the_floor_is_the_grid(self):
        # Samme hold, men ladetilstanden ligger på gulvet. Så er der ikke
        # mere at aflade, og huset køber.
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
        self.assertEqual(price.reason, "net, lader op")

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
        # tilbage er 1,00, og der skal købes 1/0,832 for at have den igen.
        self.assertAlmostEqual(price.kr_per_kwh, 1.00 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.detail)

    def test_energy_is_valued_against_a_coming_export(self):
        # Eksport om en time til 1,60 er mere værd end de 1,20 det koster
        # at skaffe energien igen.
        #
        # Det er en værdisættelse, ikke en beslutning: om energien faktisk
        # bliver gemt, afgøres af hvad den ellers skulle bruges til.
        p = plan(row(), row(), row(state="exp", export_rate=160), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.60 * 0.90, places=9)
        self.assertIn("værdisat mod eksport", price.detail)

    def test_a_cheap_charge_soon_frees_the_battery(self):
        p = plan(row(), row(state="chrg", import_rate=40))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        # Den kendte påfyldning, ikke bare den billigste i horisonten:
        # bruger vi en kWh nu og fylder den på om en halv time, koster den
        # hvad *den* påfyldning koster - og der skal købes 1/0,832 for at
        # få 1 igen.
        self.assertAlmostEqual(
            price.kr_per_kwh, 0.40 / BATTERY_ROUND_TRIP, places=9
        )
        self.assertIn("lades om", price.detail)

    def test_a_planned_discharge_is_not_read_as_a_charge(self):
        # "dischrg" indeholder "chrg". Uden afladningstesten først blev hver
        # eneste planlagte afladning læst som en opladning - halvtimen blev
        # prissat som om batteriet var bundet.
        p = plan(row(state="dischrg", import_rate=300))

        self.assertEqual(p.slots[0].mode, DISCHARGE)
        self.assertFalse(p.slots[0].refills)
        self.assertFalse(p.slots[0].locked)

    def test_an_almost_empty_battery_is_priced_as_grid(self):
        # Uanset hvad de sidste par procent kostede engang, kan de ikke
        # levere den næste kWh til en varmepumpe på 16 kW.
        p = plan(row(soc=4, import_rate=300))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 3.00, places=9)
        self.assertIn("batteriet er tomt", price.detail)

    def test_the_reserve_is_not_a_floor_under_the_heat_pump(self):
        # Predbats reserve på 14 % er 5 kWh gemt til uplanlagt forbrug - og
        # en varmepumpe der starter, *er* uplanlagt forbrug. Anlægget kan
        # aflade til 5 %. Reserven siger kun at Predbat ikke vil *sælge*
        # den energi, ikke at den ikke må bruges.
        p = plan(
            row(soc=14, import_rate=170),
            row(soc=14, import_rate=160),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(p.reserve, 14)
        self.assertEqual(price.source, BATTERY)

    def test_an_empty_battery_is_the_grid(self):
        # Under anlæggets eget nulpunkt kan inverteren ikke levere.
        p = plan(row(soc=4, import_rate=170))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(price.source, NET)
        self.assertAlmostEqual(price.kr_per_kwh, 1.70, places=9)
        self.assertIn("batteriet er tomt", price.detail)

    def test_a_battery_on_the_reserve_still_answers(self):
        # Batteriet ligger på Predbats reserve, og der lades halvanden time
        # senere. Energien over anlæggets nulpunkt er der stadig, så den
        # næste kilowatt-time er batteriets - og den koster hvad det koster
        # at fylde den på igen.
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
        self.assertIn("lades om", price.detail)

    def test_a_plain_export_sells_the_battery(self):
        # "exp" tømmer batteriet ud på nettet. Tager varmepumpen en
        # kilowatt-time, er det batteriets - prisen er den mistede indtægt.
        p = plan(row(state="exp", export_rate=140))

        price = p.marginal(0)

        self.assertEqual(price.source, BATTERY)
        self.assertAlmostEqual(price.kr_per_kwh, 1.40, places=9)

    def test_a_frozen_export_sells_the_sun(self):
        # "frzexp" holder ladetilstanden og sælger solen. Så er det solens
        # kilowatt-time der bliver brugt, ikke batteriets.
        p = plan(row(state="frzexp", export_rate=140))

        price = p.marginal(0)

        self.assertEqual(price.source, SUN)
        self.assertAlmostEqual(price.kr_per_kwh, 1.40, places=9)

    def test_an_empty_battery_under_a_covering_sun_is_the_sun(self):
        # Batteriet er ude af spillet, men panelerne bærer huset og der
        # købes ikke. Så er det solen der leverer den næste kilowatt-time.
        p = plan(row(soc=4, import_rate=170))

        price = p.marginal(0, grid=Grid(pv_power=4000))

        self.assertEqual(price.source, SUN)

    def test_a_flat_plan_high_up_is_not_a_bottom(self):
        # Står ladetilstanden stille på 70 %, er det solen der dækker
        # huset. Det er ikke et tomt batteri, og energien koster hvad den
        # koster at skaffe igen.
        p = plan(row(soc=70), row(soc=70), row(soc=70), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIsNone(p.reserve)
        self.assertAlmostEqual(price.kr_per_kwh, 1.00 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.detail)

    def test_a_frozen_export_on_the_floor_still_marks_the_reserve(self):
        # Predbat sælger ikke under reserven, så en frossen eksport dernede
        # ligger der netop fordi det *er* bunden. Kravet er kun at bunden
        # også ses ét sted hvor batteriet måtte aflade.
        p = plan(
            row(soc=30),
            row(state="frzexp", soc=12),
            row(state="frzexp", soc=12),
            row(soc=12),
            row(soc=20),
        )

        self.assertEqual(p.reserve, 12)

    def test_a_hold_alone_does_not_name_a_reserve(self):
        # Står ladetilstanden stille fordi Predbat holder batteriet, er det
        # ikke fordi der ikke er noget i det. Uden en afladning dernede er
        # der ingen bund at læse.
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
        self.assertIn("genanskaffelse", price.detail)

    def test_a_battery_that_runs_dry_costs_what_it_takes_to_buy_back(self):
        # Batteriet dækker huset nu, men planen viser det i bund længe før
        # det lades igen. Så er den kWh vi bruger nu, præcis den kWh vi
        # køber til 1,73 når batteriet står tomt - ikke de 1,00 den
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
        self.assertIn("købes tilbage om 60 min", price.detail)

    def test_energy_the_plan_sells_before_it_runs_dry_costs_the_export(self):
        # Batteriet når bunden inden det lades - men det er en planlagt
        # eksport der tømmer det. Så er den kWh vi bruger nu, ikke en der
        # skal købes tilbage til importprisen i bunden; det er en der ikke
        # bliver solgt, og prisen er den mistede indtægt.
        p = plan(
            row(soc=37, import_rate=190),
            row(soc=30, import_rate=180),
            row(state="exp", soc=24, export_rate=115),
            row(soc=4, import_rate=185),
            row(soc=4, import_rate=173),
            row(state="chrg", soc=40, import_rate=85),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        # Prisen er den mistede indtægt. Hvilken af de to eksportgrene der
        # svarer, er underordnet - tallet er det samme, og det er tallet der
        # afgør valget.
        self.assertAlmostEqual(price.kr_per_kwh, 1.15 * 0.90, places=9)
        self.assertIn("eksport", price.reason)

    def test_without_a_sale_first_it_is_still_bought_back(self):
        # Samme plan uden eksporten: så er bunden en bund, og den kWh vi
        # bruger nu, køber vi fra nettet når den mangler.
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
        self.assertIn("købes tilbage", price.detail)

    def test_a_sale_on_the_other_side_of_the_bottom_is_not_an_alternative(self):
        # Natten til den 9. september lå batteriet på 32 % kl. 03:20, og
        # planen kørte det ned til reserven på 9 % fire timer senere. Solen
        # fyldte det op igen, og om aftenen solgte Predbat til 1,31. Uden
        # bunden som grænse fandt værdisættelsen *det* salg atten timer
        # ude og prissatte hele natten til 1,18 - og så så en opladning
        # kl. 03:20 billig ud på energi der var brugt længe inden. Den
        # kilowatt-time nattens varmepumpe tager, køber huset tilbage fra
        # nettet når planen rammer bunden.
        p = plan(
            row(soc=32, import_rate=159),
            row(soc=20, import_rate=158),
            row(soc=9, import_rate=223),
            row(soc=9, import_rate=222),
            row(soc=45, import_rate=100),
            row(state="exp", soc=83, export_rate=131),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(p.reserve, 9)
        self.assertAlmostEqual(price.kr_per_kwh, 2.23, places=9)
        self.assertIn("købes tilbage om 60 min", price.detail)
        # Kilden er stadig batteriets: Predbat står på demand, og
        # inverteren leverer. Det er *prisen* der kommer fra nettet, og den
        # oplysning hører til i begrundelsen - ikke i kilden.
        self.assertEqual(price.source, BATTERY)
        self.assertEqual(price.reason, "købes tilbage")

    def test_a_bottom_after_the_sale_does_not_block_it(self):
        # Bunden spærrer kun for det der ligger bagved den. Kommer salget
        # først, er energien lovet væk dertil, og prisen er den mistede
        # indtægt - ikke importprisen i en bund der ligger endnu senere.
        p = plan(
            row(soc=60),
            row(state="exp", soc=40, export_rate=210),
            row(soc=9, import_rate=250),
            row(soc=9, import_rate=250),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertEqual(price.source, BATTERY)
        self.assertAlmostEqual(price.kr_per_kwh, 2.10 * 0.90, places=9)

    def test_a_charge_before_the_bottom_leaves_the_replacement_alone(self):
        # Fyldes batteriet inden det løber tørt, er energien ikke
        # disponeret - så skal den ikke købes tilbage i bunden, og prisen
        # er den almindelige genanskaffelse. Ladningen ligger 150 min ude og
        # er dermed heller ikke "snart", så det er den billigste import i
        # horisonten der svarer: 0,85.
        p = plan(
            row(soc=40), row(soc=38), row(soc=36), row(soc=34), row(soc=32),
            row(state="chrg", soc=60, import_rate=85),
            row(soc=14), row(soc=14),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 0.85 / BATTERY_ROUND_TRIP, places=9)
        self.assertIn("genanskaffelse", price.detail)

    def test_a_maxed_inverter_is_not_a_reason_to_price_at_the_grid(self):
        # Her stod det modsatte: importerede måleren mens batteriet
        # afladede, stod inverteren på sit loft, og prisen var nettets.
        # Begrundelsen var "12 kW inverter mod 16 kW varmepumpe" - men de
        # 16 kW er ``hp_charge_kw``, tankenes ladeeffekt i *varme*.
        # Elforbruget er varmen delt med COP; anlægget måler selv 6,23 kW
        # varme for 1,79 kW el. Pumpen når aldrig inverterens loft, og
        # tilbage målte tærsklen kun husets vippen omkring nul.
        p = plan(row(import_rate=300))

        price = p.marginal(0, grid=Grid(battery_power=3000, grid_power=4000))

        self.assertEqual(price.kr_per_kwh, p.marginal(0).kr_per_kwh)
        self.assertIn("genanskaffelse", price.detail)

    def test_export_valuation_never_makes_energy_cheaper(self):
        # I båndet snit < eksport < snit/0,90 vendte grenen sit formål på
        # hovedet: snit 1,00 og eksport 1,05 gav 0,945.
        p = plan(row(), row(state="exp", export_rate=105))

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertGreaterEqual(price.kr_per_kwh, 1.0)

    def test_a_battery_on_its_reserve_is_priced_at_its_replacement(self):
        # Under bunden bliver energien aldrig solgt - Predbat eksporterer
        # ikke derunder - så en kommende eksport er ikke et alternativ til
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
        self.assertIn("genanskaffelse", price.detail)

    def test_the_best_paid_export_decides_not_the_first(self):
        # Den marginale kilowatt-time bliver solgt i den bedste halvtime der
        # er tilbage. En tidlig, dårligt betalt eksport siger ingenting om
        # hvad energien er værd.
        p = plan(
            row(soc=60),
            row(state="exp", export_rate=90, soc=55),
            row(state="exp", export_rate=210, soc=45),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 2.10 * 0.90, places=9)
        self.assertIn("om 60 min", price.detail)

    def test_an_export_far_out_still_counts(self):
        # Der stod før en grænse på tre timer. Planen kender salget tolv
        # timer i forvejen, og om det ligger to eller elleve timer ude, er
        # energien lige meget lovet væk.
        p = plan(
            row(soc=60),
            *[row(soc=55) for _ in range(15)],
            row(state="exp", export_rate=210, soc=45),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 2.10 * 0.90, places=9)
        self.assertIn("om 480 min", price.detail)

    def test_a_charge_before_the_sale_takes_the_export_out_of_play(self):
        # Fyldes batteriet før salget, er det ikke *den her* kilowatt-time
        # der bliver solgt bagefter.
        p = plan(
            row(soc=60),
            row(state="chrg", import_rate=100, soc=90),
            row(state="exp", export_rate=210, soc=45),
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("lades om", price.detail)

    def test_a_full_battery_can_afford_to_be_valued_against_export(self):
        p = plan(
            row(soc=75),
            row(),
            row(state="exp", export_rate=160),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(price.kr_per_kwh, 1.60 * 0.90, places=9)
        self.assertIn("SOC 75 %", price.detail)

    def test_an_unknown_soc_is_assumed_to_be_enough(self):
        p = plan(
            {"state": "", "import_rate": 200, "export_rate": 80},
            row(),
            row(state="exp", export_rate=160),
            cheapest=1.00,
        )

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("værdisat mod eksport", price.detail)

    def test_an_export_that_pays_less_than_the_battery_is_not_worth_saving_for(self):
        p = plan(row(), row(state="exp", export_rate=60), cheapest=1.00)

        price = p.marginal(0, grid=Grid(battery_power=3000))

        self.assertIn("genanskaffelse", price.detail)

    def test_physical_export_does_not_beat_the_plan(self):
        # Her stod "physical_export beats the plan": sagde måleren at der
        # gik strøm ud, svarede eksportgrenen med den *aktuelle* halvtimes
        # rå tarif. Den 10. september lå den på 1,31 mens batterigrenen
        # med rette værdisatte energien til 3,04 mod aftenens top - så
        # hvert minut med lidt overskud halverede prisen og vendte
        # beslutningen. Planen siger om der sælges; måleren gætter.
        p = plan(row(export_rate=120))

        price = p.marginal(0, grid=Grid(grid_power=-4000))

        self.assertEqual(price.kr_per_kwh, p.marginal(0).kr_per_kwh)
        self.assertNotIn("eksport", price.reason)

    def test_physical_import_is_not_priced_at_the_grid(self):
        p = plan(row(import_rate=210))

        price = p.marginal(0, grid=Grid(grid_power=3000))

        self.assertEqual(price.kr_per_kwh, p.marginal(0).kr_per_kwh)
        self.assertNotIn("import", price.detail)

    def test_the_direction_of_the_meter_changes_nothing(self):
        """Den ene test der holder fejlen fra den 10. september ude.

        Samme halvtime, fem forskellige strømretninger - og ``None``, for
        ``cheapest_window`` prissætter uden måling overhovedet. Et eneste
        tal må ikke afhænge af hvilken vej tråden gik i det øjeblik.
        """
        p = plan(row(import_rate=210, export_rate=120), row(state="exp", export_rate=380))
        expected = p.marginal(0)

        for power in (-9000, -4000, -300, 0, 300, 4000, 9000):
            with self.subTest(grid_power=power):
                price = p.marginal(0, grid=Grid(grid_power=power))
                self.assertAlmostEqual(price.kr_per_kwh, expected.kr_per_kwh, places=9)
                self.assertEqual(price.reason, expected.reason)
                self.assertEqual(price.source, expected.source)

    def test_no_measurable_flow_still_means_the_battery(self):
        # Måleren ser hverken import, eksport eller en afladning værd at
        # nævne. Før gav det grenen "balanceret", som ikke var en kilde;
        # anlæggets regel er at inverteren dækker forbruget, så kilden er
        # batteriet. Prisen er stadig loftet af hvad nettet tager.
        p = plan(row(import_rate=60))

        price = p.marginal(0, grid=Grid())

        self.assertAlmostEqual(price.kr_per_kwh, 0.60, places=9)
        self.assertEqual(price.source, BATTERY)
        self.assertIn("genanskaffelse", price.detail)

    def test_the_sun_is_named_when_it_carries_the_house(self):
        p = plan(row(import_rate=200))

        price = p.marginal(0, grid=Grid(pv_power=4000))

        self.assertEqual(price.source, BATTERY)
        self.assertIn("solen dækker huset", price.detail)

    def test_beyond_the_horizon_there_is_no_price(self):
        self.assertIsNone(plan(row()).marginal(600))


class FutureTest(unittest.TestCase):
    """Det nye: en pris for en halvtime vi endnu ikke er nået til."""

    def test_a_future_slot_is_priced_from_the_plan_alone(self):
        p = plan(row(), row(state="exp", export_rate=150), row(state="chrg", import_rate=30))

        self.assertIn("eksport", p.marginal(30).reason)
        self.assertEqual(p.marginal(60).source, NET)

    def test_a_measurement_never_reaches_a_future_slot(self):
        # Målingen beskriver kun den halvtime vi står i, og den afgør
        # ingen pris længere overhovedet. Både nu og senere skal derfor
        # svare præcis som uden måling.
        p = plan(row(state="holdchrg", import_rate=200), row(import_rate=50))
        grid = Grid(grid_power=4000, pv_power=3000)

        self.assertAlmostEqual(p.marginal(0, grid=grid).kr_per_kwh, 2.00, places=9)
        self.assertEqual(
            p.marginal(30, grid=grid).kr_per_kwh, p.marginal(30).kr_per_kwh
        )


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


class ReasonVocabularyTest(unittest.TestCase):
    """Begrundelsen er ét ord, og der er kun seks af dem.

    En plan man skal læse en forklaring for at forstå, bliver ikke læst.
    Regnestykket bag står i ``detail`` og går til fejlsøgningsfilen.

    To af de seks er ikke kilder men grunde: "eksport" og "købes tilbage".
    Begge står på en række hvor strømmen kommer fra batteriet, og begge
    siger hvorfor den energi ikke er gratis at bruge.
    """

    WORDS = {
        "net",
        "net, lader op",
        "batteri",
        "sol",
        "eksport",
        "købes tilbage",
    }

    def test_every_reason_is_one_of_the_words(self):
        plans = [
            plan(row()),
            plan(row(import_rate=300), row(import_rate=100)),
            plan(row(state="chrg", import_rate=40, soc=90)),
            plan(row(state="holdchrg", soc=40, import_rate=180)),
            plan(row(state="exp", export_rate=160)),
            plan(row(state="frzexp", export_rate=160)),
            plan(row(soc=4, import_rate=300)),
            plan(row(soc=60), row(state="exp", export_rate=210, soc=45)),
            plan(row(soc=37), row(soc=20), row(soc=4), row(soc=4)),
        ]
        grids = [
            None,
            Grid(),
            Grid(battery_power=3000),
            Grid(grid_power=9000),
            Grid(grid_power=-4000),
            Grid(pv_power=4000),
        ]
        seen = set()
        for p in plans:
            for grid in grids:
                price = p.marginal(0, grid=grid)
                if price is None:
                    continue
                with self.subTest(reason=price.reason):
                    self.assertIn(price.reason, self.WORDS)
                seen.add(price.reason)

        # Og de bruges faktisk - ellers prøver den ovenstående ingenting.
        self.assertGreaterEqual(len(seen), 4)

    def test_an_unknown_predbat_state_says_so(self):
        # Den ene undtagelse fra de fem ord. En tilstand vi ikke kender, kan
        # være prissat forkert, og det skal stå der.
        price = plan(row(state="Turboladning", import_rate=180)).marginal(0)

        self.assertIn("ukendt", price.reason)
        self.assertIn("turboladning", price.detail)

    def test_the_detail_survives_for_the_debug_file(self):
        price = plan(row(import_rate=300), row(import_rate=100)).marginal(
            0, grid=Grid(battery_power=3000)
        )

        self.assertEqual(price.reason, "batteri")
        self.assertIn("genanskaffelse", price.detail)


if __name__ == "__main__":
    unittest.main()


class VocabularyTest(unittest.TestCase):
    """Ordforrådet er nu efterprøvet mod anlægget.

    Fire debug-udtræk fra 3.-4. september 2026 indeholder præcis fem ord:
    Demand, Chrg, HoldChrg, Exp og FrzExp. Ejeren har bekræftet hvad de
    betyder. Resten herunder er stavemåder af de samme handlinger.
    """

    def test_the_states_predbat_is_known_to_write_are_understood(self):
        for state in ("Chrg", "Dischrg", "FrzChrg", "FrzDischrg", "HoldChrg",
                      "Exp", "FrzExp", "Hold", "Demand", ""):
            with self.subTest(state=state):
                p = plan(row(state=state))
                self.assertTrue(p.slots[0].understood, state)

    def test_words_the_plant_never_sends_are_not_guessed_at(self):
        # "Idle" og "ecoo" findes i Predbat, men ikke på det her anlæg, og
        # hvad de præcis gør ved inverteren ville være et gæt. Et gæt i
        # tabellen ville se ud som viden. De låser i stedet og siger det.
        for state in ("Idle", "ecoo"):
            with self.subTest(state=state):
                p = plan(row(state=state, import_rate=180))

                self.assertFalse(p.slots[0].understood)
                self.assertEqual(p.slots[0].mode, LOCKED)
                self.assertIn("ukendt", p.marginal(0).reason)

    def test_a_state_we_cannot_read_is_flagged_not_swallowed(self):
        # Kan vi ikke tyde den, låses halvtimen til importprisen - og så
        # skal det stå både i loggen og i begrundelsen.
        p = plan(row(state="Turboladning"))

        self.assertFalse(p.slots[0].understood)

    def test_reading_a_plan_with_an_unknown_state_warns_once(self):
        with self.assertLogs("varmeopt.prices", level="WARNING") as caught:
            plan(row(state="Turboladning"), row(state="Turboladning"))

        self.assertEqual(len(caught.records), 1)
        self.assertIn("turboladning", caught.output[0].lower())


class ReplacementCostTest(unittest.TestCase):
    """Batteriets energi koster hvad det koster at lægge den tilbage."""

    def test_it_is_the_cheapest_import_ahead_grossed_up_by_the_losses(self):
        # Ikke hvad energien kostede engang - hvad den koster at skaffe igen.
        p = plan(row(import_rate=300), row(import_rate=100))

        self.assertAlmostEqual(p.replacement_cost(0), 1.00 / BATTERY_ROUND_TRIP, places=9)

    def test_it_only_looks_forward(self):
        # Den billige halvtime ligger bag os set fra den sidste række, og en
        # pris vi ikke kan nå, kan ingen kilowatt-time lægges tilbage til.
        p = plan(row(import_rate=100), row(import_rate=300))

        self.assertAlmostEqual(p.replacement_cost(1), 3.00 / BATTERY_ROUND_TRIP, places=9)

    def test_the_export_floor_still_holds_the_bottom(self):
        # Selv gratis energi er mindst det værd man kan sælge den for.
        p = plan(row(import_rate=0))

        self.assertAlmostEqual(p.replacement_cost(), EXPORT_FLOOR, places=9)

    def test_a_plan_without_prices_falls_to_the_floor(self):
        self.assertAlmostEqual(
            Plan.from_predbat(None).replacement_cost(), EXPORT_FLOOR, places=9
        )

    def test_it_makes_charging_the_tanks_during_a_battery_charge_pay(self):
        # Predbat lader batteriet til 1,00. Kører varmepumpen på nettet i
        # den samme halvtime, koster strømmen 1,00 - går den samme energi
        # gennem batteriet først, koster den 1,00/0,832.
        during = plan(row(state="chrg", import_rate=100))
        later = plan(row(import_rate=300), row(state="chrg", import_rate=100))

        now = during.marginal(0, grid=Grid(grid_power=5000))
        via_battery = later.marginal(0, grid=Grid(battery_power=3000))

        self.assertAlmostEqual(now.kr_per_kwh, 1.00, places=9)
        self.assertGreater(via_battery.kr_per_kwh, now.kr_per_kwh)
        self.assertAlmostEqual(
            via_battery.kr_per_kwh / now.kr_per_kwh, 1 / BATTERY_ROUND_TRIP, places=6
        )

