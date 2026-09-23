import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from varmeopt.__main__ import Varmeopt
from varmeopt.cop import Cell, CopTable
from varmeopt.ha import HaError, State
from varmeopt.options import Options
from varmeopt.prices import BATTERY_ROUND_TRIP
from varmeopt.store import Store

FLOW = "sensor.flow"
COP = "sensor.cop"
# Udetemperaturen har sin egen entitet. Den kom før fra Node-REDs
# flow-context; den vej findes ikke mere.
OUT = "sensor.ude"


def options(**over):
    # Tag defaults fra Options selv i stedet for at ramse felterne op her.
    # Ellers vælter hele denne fil hver gang der kommer en ny indstilling.
    base = Options.load(Path("findes-ikke.json"))
    return replace(
        base,
        entity_flow_temp=FLOW,
        entity_cop_measured=COP,
        entity_outdoor_temp=OUT,
        **over,
    )


class FakeHa:
    """Nok af HomeAssistant til at cycle() kan køre uden netværk."""

    def __init__(self, states: dict[str, State]) -> None:
        self._states = states
        self.published: list[tuple[str, object]] = []
        self.attributes: dict[str, dict] = {}
        # Vejrudsigten hentes med et service-kald, ikke som en tilstand.
        self.forecast_response: dict = {}
        self.services: list[tuple[str, str]] = []
        # Når den er sat, fejler skrivningen til netop den entitet. Bruges
        # til at prøve at én fejlet udgivelse ikke tager de andre med sig.
        self.fail_on: str | None = None

    def measure(self, cop: object, last_changed: str | None) -> None:
        self._states[COP] = State(COP, str(cop), {}, last_changed)

    async def get_state(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)

    async def set_state(self, entity_id, state, attributes=None) -> None:
        if self.fail_on is not None and entity_id == self.fail_on:
            raise HaError(f"skrivning til {entity_id} fejlede (prøve)")
        self.published.append((entity_id, state))
        self.attributes[entity_id] = attributes or {}

    async def call_service(self, domain, service, data):
        self.services.append((domain, service))
        # Som Home Assistants REST-API svarer: service-svaret ligger inde i
        # en indpakning. Attrappen svarede før uden den, og så kunne den
        # ikke se at udsigten var ulæselig på det kørende anlæg.
        return {"changed_states": [], "service_response": self.forecast_response}


class CycleTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        # Uden opvarmningstid: testene her handler om hvad der læres og
        # hvornår en måling er ny, ikke om hvor længe pumpen har kørt. Den har
        # sin egen klasse, ``CopWarmupTest``.
        self.app = Varmeopt(options(cop_learn_warmup_minutes=0), Store(tmp))
        # Én belagt celle, så vi kan se præcis hvor meget en cyklus lægger til.
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        # Varmepumpens eget fremløb står her lig setpunktet, så cellen
        # ovenfor er den der læres i. Det er BT12 der er tabellens akse.
        bt12 = self.app.options.entity_hp_flow
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "flow-1"),
                bt12: State(bt12, "31.0", {}, "bt12-1"),
                COP: State(COP, "4.4", {}, "måling-1"),
                OUT: State(OUT, "17.2", {}, "ude-1"),
            }
        )

    def cycle(self, times: int = 1):
        for _ in range(times):
            asyncio.run(self.app.cycle(self.ha))

    @property
    def samples(self) -> float:
        return self.app.table.sample_count

    # -------------------------------------------------------------- dedup

    def test_a_restart_does_not_learn_the_standing_measurement_again(self):
        # Målingen på føleren er fra før genstarten og blev lært dengang.
        # Første cyklus åbner kun vinduet. Hver udrulning er en genstart, og
        # før kom den sidste måling med to gange - på det BT12 der stod efter
        # genstarten, ikke det den blev målt ved.
        self.cycle(times=3)

        self.assertEqual(self.samples, 10.0)
        self.assertIn("efter start", self.app.status["learn_note"])

    def test_same_measurement_is_only_learned_once(self):
        # Kernen: pumpen kører stabilt, sensoren står stille, vi poller videre.
        self.cycle()
        self.ha.measure(4.4, "måling-2")
        self.cycle(times=5)

        self.assertEqual(self.samples, 11.0)

    def test_new_last_changed_is_learned_again(self):
        self.cycle()
        self.ha.measure(4.6, "måling-2")
        self.cycle()
        self.ha.measure(4.5, "måling-3")
        self.cycle()

        self.assertEqual(self.samples, 12.0)

    def test_without_last_changed_we_learn_every_cycle(self):
        # Lokal afprøvning mod en attrap: uden tidsstempel kan to målinger
        # ikke kendes fra hinanden, og så lærer vi hellere for meget - fra
        # anden cyklus, for den første åbner vinduet.
        self.ha.measure(4.4, None)
        self.cycle(times=3)

        self.assertEqual(self.samples, 12.0)

    def test_stopped_pump_is_not_remembered_as_learned(self):
        # En ignoreret måling må ikke optage pladsen som "sidst lært", ellers
        # kunne den spærre for en rigtig måling bagefter.
        self.cycle()
        self.ha.measure(0, "måling-2")
        self.cycle()

        self.assertEqual(self.samples, 10.0)
        self.assertIsNone(self.app._last_learned_stamp)

    def test_implausible_measurement_is_not_remembered_as_learned(self):
        self.cycle()
        self.ha.measure(99, "måling-2")
        self.cycle()

        self.assertEqual(self.samples, 10.0)
        self.assertIsNone(self.app._last_learned_stamp)

    # --------------------------------------------------------------- BT12

    def _bt12(self, temp):
        eid = self.app.options.entity_hp_flow
        if temp is None:
            self.ha._states.pop(eid, None)
        else:
            self.ha._states[eid] = State(eid, str(temp), {}, f"bt12-{temp}")

    def test_the_heat_pump_s_own_flow_is_the_axis(self):
        # Glidende opladning: UVR'ens setpunkt står på 31, men varmepumpen
        # får sit eget setpunkt og går op. Målingen hører til det fremløb
        # pumpen faktisk leverede - middelværdien af BT12 over målingens
        # vindue - og ikke til 31.
        self.app.table = CopTable()
        self._bt12(45.0)
        self.cycle()
        self.ha.measure(4.4, "måling-2")
        self.cycle()
        for temp in (48.0, 50.0, 52.0):
            self._bt12(temp)
            self.cycle()
        self._bt12(50.0)
        self.ha.measure(4.0, "måling-3")
        self.cycle()

        self.assertEqual(self.app.table.flow_temps, [45, 50])
        self.assertEqual(self.app.table.row(31), {})

    def test_without_bt12_nothing_is_learned(self):
        # Setpunktet er ikke en reserve for BT12. Det er netop den
        # forurening der skal ud af tabellen.
        self.app.table = CopTable()
        self._bt12(None)
        self.cycle()
        self.ha.measure(4.4, "måling-2")
        self.cycle()

        self.assertEqual(self.app.table.cell_count, 0)
        self.assertIn("mangler BT12", self.app.status["learn_note"])

    def test_learning_does_not_wait_for_the_setpoint(self):
        # Aksen er BT12. At UVR'ens setpunkt ikke svarer, må ikke tie
        # læringen - og vinduet må ikke vokse videre og slå målinger sammen.
        self.app.table = CopTable()
        self._bt12(40.0)
        self.ha._states.pop(FLOW)
        self.cycle()
        self.ha.measure(4.4, "måling-2")
        self.cycle()

        self.assertEqual(self.app.table.flow_temps, [40])

    def test_a_stopped_pump_starts_a_new_window(self):
        # Står pumpen stille, er BT12 bare vandet i røret. De minutter må ikke
        # trække den næste målings fremløb ned.
        self.app.table = CopTable()
        self._bt12(50.0)
        self.cycle()
        self.ha.measure(4.4, "måling-2")
        self.cycle()
        self.ha.measure(0, "stop")
        for temp in (25.0, 25.0, 25.0):
            self._bt12(temp)
            self.cycle()
        self._bt12(50.0)
        self.ha.measure(4.0, "start")
        self.cycle()

        self.assertEqual(self.app.table.flow_temps, [50])

    # ------------------------------------------------------------- øvrigt

    def test_lookup_is_published_to_home_assistant(self):
        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_cop", published)
        self.assertIsInstance(published["sensor.varmeopt_cop"], float)

    def test_tank_is_published_when_the_sensors_answer(self):
        o = self.app.options
        for eid, temp in (
            (o.entity_tank_a_top, 60),
            (o.entity_tank_a_mid, 45),
            (o.entity_tank_a_bottom, 30),
            (o.entity_tank_a_outlet, 58),
        ):
            self.ha._states[eid] = State(eid, str(temp), {}, "tank-1")
        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_lager", published)
        self.assertGreater(published["sensor.varmeopt_lager"], 0)

    def _fill_tanks(self, b_answers: bool = True) -> None:
        o = self.app.options
        pairs = [
            (o.entity_tank_a_top, 60), (o.entity_tank_a_mid, 45),
            (o.entity_tank_a_bottom, 30), (o.entity_tank_a_outlet, 58),
        ]
        if b_answers:
            pairs += [
                (o.entity_tank_b_top, 58), (o.entity_tank_b_mid, 44),
                (o.entity_tank_b_bottom, 29), (o.entity_tank_b_outlet, 56),
            ]
        else:
            for eid in (o.entity_tank_b_top, o.entity_tank_b_mid,
                        o.entity_tank_b_bottom, o.entity_tank_b_outlet):
                self.ha._states.pop(eid, None)
        for eid, temp in pairs:
            self.ha._states[eid] = State(eid, str(temp), {}, "tank-1")

    def test_the_charge_mark_lands_on_the_row_the_block_starts_in(self):
        # Planens rækker er nummereret fra halvtimens begyndelse - række 0
        # er den halvtime vi står i - og web-siden skriver klokkeslættet
        # som halvtimens start. Blokken ligger på det samme gitter, så de
        # to skal måles fra det samme nulpunkt.
        #
        # Før blev blokken målt fra *dette sekund*: kl. 04:56 blev en blok
        # kl. 13:00 til 484 minutter, og 484 rammer rækken der hedder 12:30.
        # Mærket stod én række for tidligt.
        from varmeopt.charge import Block, slot_start

        now = 1_757_000_000.0 + 26 * 60  # et godt stykke inde i halvtimen
        base = slot_start(now)
        starts = base + 510 * 60
        self.app.charge_plan.block = Block(
            dear_from=base + 570 * 60,
            dear_until=base + 750 * 60,
            starts_at=starts,
            ends_at=starts + 15 * 60,
            kwh=2.7,
        )

        with mock.patch("time.time", return_value=now):
            window = self.app._charge_window()

        self.assertEqual(window, (510, 525))

    def test_a_slower_pump_lowers_the_minimum_draw(self):
        # Typeskiltet siger 16 kW og mindstetrækket 4,0 kWh; maskinen
        # leverer omkring 11, så de 4,0 kWh var 22 minutter og ikke de 15
        # reglen handler om. Blokken lægges i forvejen med den målte rate.
        self.cycle()
        nameplate = self.app.planner.min_charge_kwh

        for _ in range(40):
            self.app.charge_rate.observe(11.0)
        self.cycle()

        self.assertLess(self.app.planner.min_charge_kwh, nameplate)
        self.assertAlmostEqual(
            self.app.planner.min_charge_kwh,
            self.app.charge_rate.effective_kw * 15 / 60,
            places=6,
        )

    def test_a_tank_that_stops_answering_holds_its_last_reading(self):
        # Natten til den 10. september svarede tank B ikke i ét minut, og
        # lageret halverede sig fra 22,6 til 11,8 kWh. Både opladningen og
        # «lageret er fuldt» læser den sum.
        self._fill_tanks()
        self.cycle()
        whole = self.app.status["tank"].stored_kwh

        self._fill_tanks(b_answers=False)
        self.cycle()

        self.assertAlmostEqual(self.app.status["tank"].stored_kwh, whole, places=6)
        self.assertEqual(self.app._tank_held, ("B",))

    def test_a_held_reading_is_dropped_when_it_gets_old(self):
        self._fill_tanks()
        self.cycle()
        # Skru aflæsningens alder tilbage, som om der var gået en time.
        stamp, tank = self.app._tank_last["B"]
        self.app._tank_last["B"] = (stamp - 3600, tank)

        self._fill_tanks(b_answers=False)
        self.cycle()

        self.assertEqual(self.app._tank_held, ())
        self.assertFalse(self.app.status["tank"].complete)

    def test_tank_is_skipped_when_no_sensor_answers(self):
        # Standardopsætningen i denne test har ingen tankfølere i FakeHa.
        self.cycle()

        self.assertIsNone(self.app.status["tank"])
        self.assertNotIn("sensor.varmeopt_lager", dict(self.ha.published))

    def test_the_price_and_the_source_decision_are_published(self):
        # Hele kæden: Predbats plan -> marginalpris -> varmepris via COP ->
        # valg mod pillefyret. Det er den beslutning Node-RED træffer i dag,
        # regnet på den rettede COP.
        o = self.app.options
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {
                "raw": {
                    "rows": [
                        {"state": "holdchrg", "import_rate": 180, "export_rate": 60},
                        {"state": "", "import_rate": 40, "export_rate": 55},
                    ]
                }
            },
            "plan-1",
        )
        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_elpris", published)
        # Batteriet er bundet, så varmepumpen kører på nettet: 1,80 kr.
        self.assertAlmostEqual(published["sensor.varmeopt_elpris"], 1.80, places=3)

        status = self.app.status
        self.assertEqual(status["price_now"].reason, "net")
        self.assertIn("afladning", status["price_now"].detail)
        # 1,80 delt med den lærte COP mod pillefyrets 0,706.
        self.assertIsNotNone(status["heat_price"])
        self.assertIn(status["decision"].source, ("varmepumpe", "pillefyr"))
        # Tilstanden er den kilde vagten står ved, ikke planlæggerens rå
        # svar. På første cyklus er de ens; se ``HeldDecisionTest`` for
        # hvad der sker når de ikke er.
        self.assertEqual(published["sensor.varmeopt_beslutning"], status["command"].source)
        self.assertEqual(
            self.ha.attributes["sensor.varmeopt_beslutning"]["rå_kilde"],
            status["decision"].source,
        )

    def test_a_missing_predbat_plan_is_not_fatal(self):
        # Predbat kan være nede eller endnu ikke have lagt en plan. Cyklussen
        # skal køre videre - COP-læringen afhænger ikke af priser.
        self.cycle()

        self.assertNotIn("sensor.varmeopt_elpris", dict(self.ha.published))
        self.assertIsNone(self.app.status.get("price_now"))
        # Men kildevalget står stadig - det kræver ingen plan.
        self.assertEqual(self.app.status["decision"].source, "varmepumpe")

    def test_the_battery_price_is_computed_from_the_plan(self):
        o = self.app.options
        # Batteriet aflader målbart - ellers står anlægget i balance, og så
        # er det den billigste af net og batteri der gælder, ikke batteriet.
        self.ha._states[o.entity_battery_power] = State(o.entity_battery_power, "3000", {}, "b")
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {
                "raw": {
                    "rows": [
                        {"state": "", "import_rate": 300, "export_rate": 50},
                        {"state": "", "import_rate": 100, "export_rate": 50},
                    ]
                }
            },
            "plan-1",
        )
        self.cycle()

        # Ingen entitet spurgt: den billigste import der er tilbage er 1,00,
        # og der skal købes 1/0,832 for at have den kilowatt-time igen.
        self.assertAlmostEqual(
            self.app.status["price_now"].kr_per_kwh, 1.00 / BATTERY_ROUND_TRIP, places=3
        )
        self.assertEqual(self.app.status["price_now"].reason, "batteri")
        self.assertIn("genanskaffelse", self.app.status["price_now"].detail)

    def test_a_balanced_plant_runs_on_the_battery_at_the_grid_s_price(self):
        # Ingen målbar strøm nogen vej. Kilden er inverteren - det er
        # anlæggets regel - og prisen er loftet af hvad nettet tager for den
        # samme kilowatt-time.
        o = self.app.options
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {"raw": {"rows": [{"state": "", "import_rate": 40, "export_rate": 50}]}},
            "plan-1",
        )
        self.cycle()

        self.assertAlmostEqual(self.app.status["price_now"].kr_per_kwh, 0.40, places=3)
        self.assertEqual(self.app.status["price_now"].source, "batteri")
        self.assertEqual(self.app.status["price_now"].reason, "batteri")
        self.assertIn("genanskaffelse", self.app.status["price_now"].detail)

    # ------------------------------------------------- Predbats egen status

    def with_plan_and_status(self, state, status):
        """En plan hvis første række er ``state``, og Predbats status."""
        o = self.app.options
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {"raw": {"rows": [{"state": state, "import_rate": 180, "export_rate": 60}]}},
            "plan-1",
        )
        self.ha._states[o.entity_predbat_status] = State(
            o.entity_predbat_status, status, {}, "status-1"
        )

    def test_a_cycle_survives_predbat_having_a_status(self):
        # Her gik 0.38.0 ned: kontrollen mod Predbats status læste stadig
        # ``slot.discharging`` og ``slot.charging``, som var væk. Ingen test
        # kom nogensinde forbi den linje, og så faldt hver eneste cyklus på
        # anlægget - uden at en eneste test blev rød.
        self.with_plan_and_status("holdchrg", "Hold charging")

        self.cycle()

        self.assertIn("sensor.varmeopt_elpris", dict(self.ha.published))

    def test_disagreement_with_predbats_status_is_said_out_loud(self):
        # Er de uenige om indeværende halvtime, læser vi planens
        # tilstandsord forkert, og så er hver pris i horisonten et gæt.
        self.with_plan_and_status("demand", "Charging")

        with self.assertLogs("varmeopt", level="WARNING") as caught:
            self.cycle()

        self.assertTrue(
            any("tilstandsstrengene" in line for line in caught.output), caught.output
        )

    def test_predbats_words_map_to_the_same_three_outcomes(self):
        # "Discharging" indeholder "charg". Rækkefølgen i oversættelsen er
        # derfor ikke til pynt, og den er faldet forkert ud før.
        for status, mode in (
            ("Demand", "discharge"),
            ("Discharging", "discharge"),
            ("Charging", "locked"),
            ("Hold charging", "locked"),
            ("Freeze charging", "locked"),
            ("Exporting", "export"),
        ):
            with self.subTest(status=status):
                self.assertEqual(self.app._status_mode(status.lower()), mode)

    def test_a_status_we_cannot_translate_says_nothing(self):
        # En kontrol der gætter, er værre end ingen kontrol.
        self.assertIsNone(self.app._status_mode("noget helt andet"))
        self.assertIsNone(self.app._status_mode("idle"))

    # ------------------------------------------ varmepumpens egne to tal

    def with_hp(self, power=None, heat=None, cop=None):
        """Sæt varmepumpens elforbrug, varmeydelse og COP-føler."""
        o = self.app.options
        for entity, value in (
            (o.entity_hp_power, power),
            (o.entity_hp_heat, heat),
        ):
            if value is None:
                self.ha._states.pop(entity, None)
            else:
                self.ha._states[entity] = State(
                    entity, str(value), {"unit_of_measurement": "kW"}, "hp-1"
                )
        if cop is not None:
            self.ha.measure(cop, "cop-1")

    def test_the_measured_output_beats_the_derived_one(self):
        # Før blev ydelsen udledt som elforbrug gange COP - her 2 x 4,4 =
        # 8,8 kW. Når anlægget selv måler 6,0, er det 6,0 der gælder:
        # det målte slår det udledte.
        self.with_hp(power=2.0, heat=6.0, cop=4.4)

        self.cycle()

        self.assertAlmostEqual(
            self.app.status["balance"].heatpump_kw, 6.0, places=3
        )

    def test_without_the_output_sensor_it_still_derives(self):
        self.with_hp(power=2.0, heat=None, cop=4.4)

        self.cycle()

        self.assertAlmostEqual(
            self.app.status["balance"].heatpump_kw, 8.8, places=3
        )

    def test_a_measured_output_frees_the_balance_from_the_cop_sensor(self):
        # Uden COP kunne varmepumpens bidrag før ikke regnes, og så måtte
        # lagerbalancen ikke måle husets forbrug. Måles ydelsen, er den
        # spærre væk.
        o = self.app.options
        self.with_hp(power=2.0, heat=6.0)
        self.ha._states.pop(o.entity_cop_measured, None)

        self.cycle()

        self.assertTrue(self.app.status["balance"].inputs_known)
        self.assertAlmostEqual(
            self.app.status["balance"].heatpump_kw, 6.0, places=3
        )

    def test_the_plants_own_numbers_check_the_cop_sensor(self):
        # 6,0 kW varme på 2,0 kW el er COP 3,0. Melder føleren 4,4, måler
        # den noget andet end vi tror - og hele COP-tabellen er bygget på den.
        self.with_hp(power=2.0, heat=6.0, cop=4.4)

        with self.assertLogs("varmeopt", level="WARNING") as caught:
            self.cycle()

        self.assertAlmostEqual(self.app.status["hp_cop_measured"], 3.0, places=3)
        self.assertTrue(
            any("COP" in line and "føleren" in line for line in caught.output),
            caught.output,
        )

    def test_agreement_is_not_worth_a_warning(self):
        # 8,8 kW på 2,0 kW el er præcis de 4,4 føleren melder.
        self.with_hp(power=2.0, heat=8.8, cop=4.4)

        self.cycle()

        self.assertAlmostEqual(self.app.status["hp_cop_measured"], 4.4, places=3)
        self.assertFalse(self.app._warned_hp_cop)

    def test_the_calculated_house_load_gets_its_own_sensor(self):
        # Attributter kommer ikke i Home Assistants langtidsstatistik, så
        # tallet skal have sin egen sensor for at kunne tegnes en måned
        # tilbage.
        self.app.house_load.curve.learn(17.2, 3.3)

        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_husforbrug", published)
        self.assertAlmostEqual(published["sensor.varmeopt_husforbrug"], 3.3, places=2)

    def test_the_vessels_draw_is_a_number_we_can_state(self):
        # Spaen alene, beholderen alene efter hvor kold den er, og begge to.
        o = self.app.options

        self.assertIsNone(self.app._vessel_kw(False, False, 50.0))
        self.assertAlmostEqual(
            self.app._vessel_kw(False, True, 50.0), o.spa_kw, places=6
        )
        # Bunden på 40 grader er tom: fuld effekt. På 55 er den varm.
        self.assertAlmostEqual(
            self.app._vessel_kw(True, False, 40.0), o.vvb_kw_cold, places=6
        )
        self.assertAlmostEqual(
            self.app._vessel_kw(True, False, 55.0), o.vvb_kw_hot, places=6
        )
        self.assertAlmostEqual(
            self.app._vessel_kw(True, True, 55.0), o.vvb_kw_hot + o.spa_kw, places=6
        )

    def test_the_guard_binding_is_actually_written_to_disk(self):
        # ``to_raw`` blev aldrig kaldt, så guard.json opstod aldrig,
        # ``restore`` var altid en no-op, og opholdstiden overlevede ikke en
        # genstart. Testene prøvede to_raw og restore mod hinanden og
        # opdagede det ikke - samme fælde som nedbruddet i 0.38.0.
        from varmeopt.migrate import GUARD_FILE

        self.cycle()
        self.app.save()

        self.assertTrue(self.app.store.exists(GUARD_FILE))

    def test_the_charge_block_is_written_too(self):
        from varmeopt.migrate import CHARGE_FILE

        self.cycle()
        self.app.save()

        self.assertTrue(self.app.store.exists(CHARGE_FILE))

    def test_a_missing_hot_water_flag_falls_back_to_the_setpoint(self):
        # Falder udgangen ud, er det rå flag None. Uden en bagstopper bliver
        # et bad på op til 8 kW bogført som husets forbrug og lært varigt
        # ind i kurven. Varmekurven genkender setpunktet; det skal
        # husforbrugsmålingen også.
        o = self.app.options
        self.ha._states.pop(o.entity_dhw_active, None)
        self.ha._states[o.entity_flow_temp] = State(
            o.entity_flow_temp, "56.0", {}, "flow-dhw"
        )

        self.cycle()

        # Udgangen svarer ikke, men setpunktet siger varmt vand - og så er
        # det den kendsgerning både varmekurven og lagermålingen bruger.
        self.assertIn("varmt vand", self.app.status["mode"])

    # -------------------------------------------- husets forbrug uden måler

    def test_the_store_answers_when_the_flow_meter_cannot(self):
        # Flowmåleren svarer ikke i attrappen - præcis som når den ligger
        # under sin bund på anlægget. Så skal lagerets tal træde i stedet
        # hele vejen ud til sensoren, ikke bare stå i en attribut.
        self.app.house_load.curve.learn(17.2, 3.3)

        self.cycle()

        published = dict(self.ha.published)
        self.assertIn("sensor.varmeopt_behov", published)
        self.assertAlmostEqual(published["sensor.varmeopt_behov"], 3.3, places=2)
        self.assertEqual(
            dict(self.ha.attributes)["sensor.varmeopt_behov"]["kilde"], "lager"
        )

    def test_without_either_the_demand_sensor_stays_quiet(self):
        # Ingen måler og ingen kurve: så er behovet ukendt, og en sensor der
        # gættede på et tal ville være værre end en der tier.
        self.cycle()

        self.assertNotIn("sensor.varmeopt_behov", dict(self.ha.published))

    def test_the_temperatures_come_from_their_entities(self):
        self.cycle()

        self.assertEqual(self.app.status["outdoor_temp"], 17.2)
        self.assertEqual(self.app.status["flow_temp"], 31.0)

    def test_missing_temperatures_skip_the_cycle_without_raising(self):
        self.ha._states.pop(OUT)
        self.ha._states.pop(FLOW)
        self.cycle()

        self.assertIsNone(self.app.status["lookup"])
        self.assertEqual(self.samples, 10.0)
        # Uden temperaturer er der ingen COP at udgive - men styringen har
        # stadig et svar, og det er med vilje.
        self.assertNotIn("sensor.varmeopt_cop", dict(self.ha.published))
        self.assertIn("sensor.varmeopt_beslutning", dict(self.ha.published))


class LockedPricesSensorTest(unittest.TestCase):
    """Aflæsningen af «er morgendagens priser endelige?».

    Sensoren er brugerens egen,
    ``binary_sensor.stromligning_energifyn_tomorrow_available_vat``, og den
    står «Available» når Nord Pool har offentliggjort i morgen - typisk
    omkring kl. 13. Indtil da er resten af horisonten en prognose.

    Det farlige svar er ikke et forkert antal minutter; det er at svare
    «intet er låst» på et anlæg der bare mangler entiteten. Så ville
    planlæggeren holde op med at lade op. Derfor er ``None`` - «regn alt for
    låst» - svaret på alt hvad vi ikke forstår.
    """

    LÅS = "binary_sensor.priser_i_morgen"

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(entity_prices_locked=self.LÅS), Store(tmp))
        self.ha = FakeHa({})

    def read(self, value=None):
        if value is not None:
            self.ha._states[self.LÅS] = State(self.LÅS, value, {}, None)
        return asyncio.run(self.app._locked_minutes(self.ha))

    def test_no_entity_configured_means_everything_is_locked(self):
        app = Varmeopt(options(entity_prices_locked=""), self.app.store)

        self.assertIsNone(asyncio.run(app._locked_minutes(self.ha)))

    def test_a_missing_entity_means_everything_is_locked(self):
        # Entiteten findes ikke. Add-on'en må ikke reagere ved at holde op
        # med at lade op.
        self.assertIsNone(self.read())

    def test_an_unavailable_sensor_means_everything_is_locked(self):
        for word in ("unknown", "unavailable", "none", ""):
            with self.subTest(word=word):
                self.assertIsNone(self.read(word))

    def test_tomorrow_available_reaches_past_midnight(self):
        i_dag = self.read("off")
        i_morgen = self.read("Available")

        self.assertIsNotNone(i_dag)
        self.assertIsNotNone(i_morgen)
        # Nøjagtig et døgn længere, uanset hvornår på dagen testen kører.
        self.assertEqual(i_morgen - i_dag, 24 * 60)

    def test_the_vocabulary_is_read_both_ways(self):
        # Sensoren siger «Available», Home Assistants egne binary_sensors
        # siger «on». Begge skal forstås, og store bogstaver må ikke tælle.
        låst = self.read("off")
        for word in ("available", "AVAILABLE", "on", "true", "yes"):
            with self.subTest(word=word):
                self.assertEqual(self.read(word) - låst, 24 * 60)

    def test_it_never_answers_with_a_past(self):
        # Minutterne tæller fra halvtimens start og frem til midnat. De kan
        # ikke være negative, uanset hvornår på døgnet der spørges.
        self.assertGreater(self.read("off"), 0)



class CopWarmupTest(unittest.TestCase):
    """COP'en kan først regnes med ti minutter efter en start.

    De første minutter fejer fremløbet fra ~41 til ~57 grader, og hver måling
    lander i en ny celle med en COP der ikke har sat sig. Natten til den 20.
    september kl. 21:10: ny celle F53 = 1,92, mens BT12 sprang 43,9 -> 53,3.
    """

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-cop-"))
        self.app = Varmeopt(options(cop_learn_warmup_minutes=10), Store(tmp))
        self.app.table = CopTable()
        self.bt12 = self.app.options.entity_hp_flow
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "flow"),
                COP: State(COP, "0", {}, "stille"),
                OUT: State(OUT, "16.0", {}, "ude"),
            }
        )
        self.t0 = 1_758_400_000.0

    def at(self, minutes, cop, stamp, bt12):
        self.ha._states[COP] = State(COP, str(cop), {}, stamp)
        self.ha._states[self.bt12] = State(self.bt12, str(bt12), {}, f"b{minutes}")
        with mock.patch("time.time", return_value=self.t0 + minutes * 60):
            asyncio.run(self.app.cycle(self.ha))

    def test_the_first_ten_minutes_are_not_learned(self):
        # Pumpen står stille, og så starter den kl. 1. Hver tredje minut en ny
        # måling, mens fremløbet kravler op som den gjorde den nat.
        self.at(0, 0, "stille", 30.0)
        for minute, cop, bt12 in ((1, 3.4, 42.5), (4, 1.92, 53.3), (7, 4.1, 56.0)):
            self.at(minute, cop, f"m{minute}", bt12)

        self.assertEqual(self.app.table.cell_count, 0, "ingen passagepunkter")
        self.assertIn("har kørt", self.app.status["learn_note"])

    def test_a_steady_measurement_after_warmup_is_learned(self):
        self.at(0, 0, "stille", 30.0)
        minute = 1
        while minute <= 25:
            self.at(minute, 4.3, f"m{minute}", 57.0)
            minute += 3

        self.assertEqual(self.app.table.flow_temps, [57])


class ForecastTest(unittest.TestCase):
    """Vejrudsigten: hver time i planen får sin egen COP."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone

        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table = CopTable(
            {44: {5: Cell(3.9, 300.0)}, 32: {15: Cell(4.6, 300.0)}}
        )
        from varmeopt.curve import HeatCurve, Point

        self.app.curve = HeatCurve({5: Point(44.0, 500.0), 15: Point(32.0, 500.0)})
        self.ha = FakeHa(
            {FLOW: State(FLOW, "32.0", {}, "f"), OUT: State(OUT, "15.0", {}, "u")}
        )
        now = datetime.now(timezone.utc)
        self.ha.forecast_response = {
            self.app.options.entity_weather: {
                "forecast": [
                    {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": t}
                    for h, t in ((0, 15.0), (6, 5.0))
                ]
            }
        }

    def test_the_forecast_is_fetched_once_and_then_cached(self):
        asyncio.run(self.app.cycle(self.ha))
        asyncio.run(self.app.cycle(self.ha))

        # Udsigten ændrer sig i timer, ikke i minutter.
        self.assertEqual(self.ha.services, [("weather", "get_forecasts")])
        self.assertGreater(len(self.app.forecast), 0)

    def test_a_colder_evening_gives_a_lower_cop_six_hours_out(self):
        asyncio.run(self.app.cycle(self.ha))

        # 15 grader nu -> setpunkt 32 -> COP 4,6.
        # 5 grader om seks timer -> setpunkt 44 -> COP 3,9.
        self.assertAlmostEqual(self.app._cop_at(0), 4.6, places=1)
        self.assertAlmostEqual(self.app._cop_at(360), 3.9, places=1)

    def test_without_a_forecast_there_is_no_answer(self):
        self.ha.forecast_response = {}
        asyncio.run(self.app.cycle(self.ha))

        # Planlæggeren falder så tilbage på den COP vi har nu.
        self.assertIsNone(self.app._cop_at(360))




class TenthOfSeptemberTest(unittest.TestCase):
    """Hele hændelsen, spillet af igen.

    Den 10. september lå Predbats plan stille: aftenens eksport var 3,38,
    og batterigrenen værdisatte dengang energien til 3,04 - langt over
    pillefyrets 0,71. Beslutningen skulle have stået på pillefyr fra 14:31
    til 21:00. Den skiftede fjorten gange.

    Årsagen var to grene der måtte overtrumfe planen på én stikprøve af
    elmåleren. Huset lå og vippede omkring nul, så hvert minut hvor
    ``grid_power`` krydsede ±200 W, faldt prisen til importprisen og
    beslutningen vendte.

    Planen herunder er syntetisk - to rækker - og ikke dagens rigtige plan.
    """

    ROWS = [
        {"state": "demand", "import_rate": 180, "export_rate": 60, "soc_percent": 50},
        {"state": "exp", "import_rate": 180, "export_rate": 380, "soc_percent": 50},
    ]

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(
            options(control_warmup_minutes=0, control_confirm_minutes=0),
            Store(tmp),
        )
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        o = self.app.options
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "flow-1"),
                COP: State(COP, "4.4", {}, "måling-1"),
                OUT: State(OUT, "17.2", {}, "ude-1"),
                o.entity_predbat_plan: State(
                    o.entity_predbat_plan, "ok", {"raw": {"rows": self.ROWS}}, "plan-1"
                ),
            }
        )

    def _meter(self, watts):
        o = self.app.options
        self.ha._states[o.entity_grid_power] = State(
            o.entity_grid_power, str(watts), {}, f"måler-{watts}"
        )

    def test_the_meter_wobbling_across_zero_changes_nothing(self):
        prices, states = set(), []
        # Husets vippen: ud, ind, ud, ind - fyrre gange, som loggen viser.
        for cycle in range(40):
            self._meter(-800 if cycle % 2 else 600)
            asyncio.run(self.app.cycle(self.ha))
            prices.add(round(self.app.status["price_now"].kr_per_kwh, 6))
            states.append(dict(self.ha.published)["sensor.varmeopt_beslutning"])

        # Én pris i alle fyrre cyklusser, ikke to der skiftes om at vinde.
        self.assertEqual(len(prices), 1, f"prisen vippede: {sorted(prices)}")
        # Og 3,42 er 0,90 x aftenens 3,80 - Predbats egen salgsmulighed,
        # ikke den aktuelle halvtimes rå tarif på 0,60.
        self.assertAlmostEqual(prices.pop(), 3.42, places=6)
        self.assertEqual(set(states), {"pillefyr"})

    def test_and_without_the_wobble_it_is_the_same_answer(self):
        # Kontrolprøven: uden måling overhovedet skal svaret være det
        # samme. Ellers vandt måleren bare konsekvent i stedet for
        # vekslende, og det ville være lige så galt.
        asyncio.run(self.app.cycle(self.ha))

        self.assertAlmostEqual(self.app.status["price_now"].kr_per_kwh, 3.42, places=6)
        self.assertEqual(self.app.status["decision"].source, "pillefyr")


class HeldDecisionTest(unittest.TestCase):
    """Entiteten må ikke vippe, også når styringen er slået fra.

    Det her er den test der ville have fanget fejlen fra den 10. september.
    Resten af suiten kørte én cyklus fra en frisk ``Guard``, og der er den
    holdte kilde altid lig den rå - så alt var grønt mens entiteten
    skiftede fjorten gange på seks timer.
    """

    DEAR = {"state": "holdchrg", "import_rate": 900, "export_rate": 50}
    CHEAP = {"state": "holdchrg", "import_rate": 5, "export_rate": 1}

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        # Bekræftelsen slås fra her, så testen handler om hviletiden og om
        # hvad der bliver *udgivet*. ``tests.test_guard.ConfirmTest`` dækker
        # bekræftelsen for sig.
        self.app = Varmeopt(
            options(control_warmup_minutes=0, control_confirm_minutes=0),
            Store(tmp),
        )
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "flow-1"),
                COP: State(COP, "4.4", {}, "måling-1"),
                OUT: State(OUT, "17.2", {}, "ude-1"),
            }
        )
        self._plan(self.CHEAP)

    def _plan(self, first):
        o = self.app.options
        self.ha._states[o.entity_predbat_plan] = State(
            o.entity_predbat_plan,
            "ok",
            {"raw": {"rows": [first, {"state": "holdchrg", "import_rate": 60,
                                      "export_rate": 55}]}},
            f"plan-{first['import_rate']}",
        )

    def cycle(self):
        asyncio.run(self.app.cycle(self.ha))

    def test_the_state_is_the_held_source_not_the_raw_one(self):
        self.cycle()
        first = dict(self.ha.published)["sensor.varmeopt_beslutning"]
        self.assertEqual(first, "varmepumpe")

        # Prisen springer, så planlæggeren vender. Hviletiden er ikke
        # udløbet, så entiteten skal blive stående.
        self._plan(self.DEAR)
        self.cycle()

        attrs = self.ha.attributes["sensor.varmeopt_beslutning"]
        self.assertEqual(self.app.status["decision"].source, "pillefyr")
        self.assertEqual(dict(self.ha.published)["sensor.varmeopt_beslutning"], "varmepumpe")
        self.assertEqual(attrs["rå_kilde"], "pillefyr")
        self.assertIn("holder", attrs["begrundelse"])

    def test_the_price_sensor_explains_it_too(self):
        # Begge sensorer udgiver de samme to varmepriser. Uden hold-noten
        # ville modsigelsen mellem tilstand og tal stå uforklaret her.
        self.cycle()
        self._plan(self.DEAR)
        self.cycle()

        self.assertIn("holder", self.ha.attributes["sensor.varmeopt_elpris"]["kilde_grund"])

    def test_a_real_change_gets_through_once_the_dwell_has_passed(self):
        self.cycle()
        self._plan(self.DEAR)
        self.cycle()
        # Skru uret tilbage på bindingen i stedet for at vente et kvarter.
        self.app.guard.committed_at -= 16 * 60
        self.cycle()

        self.assertEqual(dict(self.ha.published)["sensor.varmeopt_beslutning"], "pillefyr")
        self.assertNotIn("holder", self.ha.attributes["sensor.varmeopt_beslutning"]["begrundelse"])

    def test_releasing_publishes_the_held_source(self):
        self.cycle()
        self._plan(self.DEAR)
        self.cycle()

        asyncio.run(self.app.release_control(self.ha))

        # Et stop må ikke selv være et tilstandsskifte i HA's historik.
        self.assertEqual(dict(self.ha.published)["sensor.varmeopt_beslutning"], "varmepumpe")


class ControlTest(unittest.TestCase):
    """Styringen: add-on'en udstiller et flag, Node-RED følger det."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.app.table = CopTable({31: {17: Cell(cop=4.5, count=10.0)}})
        o = self.app.options
        self.ha = FakeHa(
            {
                FLOW: State(FLOW, "31.0", {}, "flow-1"),
                COP: State(COP, "4.4", {}, "måling-1"),
                OUT: State(OUT, "17.2", {}, "ude-1"),
                # Uden en plan er der ingen pris, og uden en pris ingen
                # varmepris - så nægter vagten med rette at styre.
                o.entity_predbat_plan: State(
                    o.entity_predbat_plan,
                    "ok",
                    {"raw": {"rows": [{"state": "holdchrg", "import_rate": 40,
                                       "export_rate": 50}]}},
                    "plan-1",
                ),
            }
        )

    def cycle(self):
        asyncio.run(self.app.cycle(self.ha))

    def test_control_is_off_by_default(self):
        self.cycle()

        command = self.app.status["command"]
        self.assertFalse(command.acting)
        self.assertIn("slået fra", command.reason)

    def test_the_decision_is_still_published_when_not_controlling(self):
        # Vagten siger ikke hvad der skal gøres - kun om nogen bør gøre det.
        self.cycle()

        self.assertIn("sensor.varmeopt_beslutning", dict(self.ha.published))
        self.assertIsNotNone(self.app.status["decision"].source)

    def test_control_holds_off_until_warmed_up(self):
        self.app.guard.enabled = True
        self.cycle()

        command = self.app.status["command"]
        self.assertFalse(command.acting)
        self.assertIn("varmer op", command.reason)

    def test_control_takes_over_once_warm(self):
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        self.cycle()

        command = self.app.status["command"]
        self.assertTrue(command.acting)
        self.assertEqual(command.source, self.app.status["decision"].source)

    def test_no_price_means_no_control_even_when_enabled(self):
        # Uden Predbats plan er der ingen varmepris. At handle på en
        # antagelse er ikke styring, det er et gæt.
        self.app.guard.enabled = True
        self.app.guard.warmup_minutes = 0.0
        self.ha._states.pop(self.app.options.entity_predbat_plan)
        self.cycle()

        command = self.app.status["command"]
        self.assertFalse(command.acting)
        self.assertIn("ingen COP", command.reason)




class Bt12TableFileTest(unittest.TestCase):
    """Setpunkt-tabellen skal ligge urørt, mens BT12-tabellen fyldes."""

    def test_the_setpoint_table_is_never_written_again(self):
        from varmeopt.migrate import COP_TABLE_BT12_FILE, COP_TABLE_FILE, load_cop_table

        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        store = Store(tmp)
        store.save(COP_TABLE_FILE, {"31": {"17": {"cop": 4.5, "count": 10.0}}})
        before = store.path(COP_TABLE_FILE).read_bytes()

        app = Varmeopt(options(), store)
        app.table, _ = load_cop_table(store)
        app.table.learn(45, 17, 4.0)
        app._dirty = True
        app.save()

        self.assertEqual(store.path(COP_TABLE_FILE).read_bytes(), before)
        self.assertEqual(store.load(COP_TABLE_BT12_FILE), {"45": {"17": {"cop": 4.0, "count": 1.0}}})
        # Og den gamle svarer stadig, hvor den nye intet ved.
        self.assertAlmostEqual(app.table.lookup(31, 17).cop, 4.5)


if __name__ == "__main__":
    unittest.main()


class UsageSensorTest(unittest.TestCase):
    """Døgnets forbrug ud af lageret, som entiteter i Home Assistant.

    De skal kunne tegnes en måned tilbage, og det kan en attribut ikke —
    derfor tre sensorer og en sum, og derfor ``total_increasing``: tallet
    tæller op gennem døgnet og falder til nul ved midnat.
    """

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.app = Varmeopt(options(), Store(tmp))
        self.ha = FakeHa({})

    def publish(self):
        asyncio.run(self.app._publish_usage(self.ha))
        return dict(self.ha.published)

    def test_nothing_counted_publishes_nothing(self):
        # Ellers ville der stå fire nuller i historikken fra den dag
        # add-on'en blev installeret, og de er ikke målinger.
        self.assertEqual(self.publish(), {})

    def test_the_three_and_the_sum_are_published(self):
        from datetime import datetime, timedelta, timezone

        tz = timezone(timedelta(hours=2))
        at = datetime(2026, 9, 16, 8, 0, tzinfo=tz).timestamp()
        for step in range(61):
            self.app.usage.observe(at + step * 60, 2.0, 6.0, 3.0)

        published = self.publish()

        self.assertAlmostEqual(published["sensor.varmeopt_forbrug_varme"], 2.0, places=1)
        self.assertAlmostEqual(published["sensor.varmeopt_forbrug_vvb"], 6.0, places=1)
        self.assertAlmostEqual(published["sensor.varmeopt_forbrug_spa"], 3.0, places=1)
        self.assertAlmostEqual(published["sensor.varmeopt_forbrug_i_alt"], 11.0, places=1)

    def test_they_are_energy_that_resets_at_midnight(self):
        from datetime import datetime, timedelta, timezone

        tz = timezone(timedelta(hours=2))
        at = datetime(2026, 9, 16, 8, 0, tzinfo=tz).timestamp()
        for step in range(11):
            self.app.usage.observe(at + step * 60, 2.0, None, None)
        self.publish()

        for entity in (
            "sensor.varmeopt_forbrug_varme",
            "sensor.varmeopt_forbrug_i_alt",
        ):
            with self.subTest(entity=entity):
                attributes = self.ha.attributes[entity]
                self.assertEqual(attributes["device_class"], "energy")
                self.assertEqual(attributes["state_class"], "total_increasing")
                self.assertEqual(attributes["unit_of_measurement"], "kWh")

    def test_yesterday_rides_along_as_an_attribute(self):
        from datetime import datetime, timedelta, timezone

        tz = timezone(timedelta(hours=2))
        for day, kw in ((15, 3.0), (16, 2.0)):
            at = datetime(2026, 9, day, 8, 0, tzinfo=tz).timestamp()
            for step in range(61):
                self.app.usage.observe(at + step * 60, kw, None, None)
        self.publish()

        i_alt = self.ha.attributes["sensor.varmeopt_forbrug_i_alt"]

        self.assertEqual(i_alt["dato"], "2026-09-16")
        self.assertAlmostEqual(i_alt["i_går_kwh"], 3.0, places=1)

    def test_a_failed_write_does_not_take_the_cycle_with_it(self):
        from datetime import datetime, timedelta, timezone

        tz = timezone(timedelta(hours=2))
        at = datetime(2026, 9, 16, 8, 0, tzinfo=tz).timestamp()
        for step in range(11):
            self.app.usage.observe(at + step * 60, 2.0, None, None)
        self.ha.fail_on = "sensor.varmeopt_forbrug_vvb"

        with self.assertRaises(HaError):
            self.publish()

        # Kaldet ligger bag ``_safely`` i cyklussen; her prøves kun at det
        # er en HaError der slipper ud, så den kan fanges det rigtige sted.
        self.assertIn("sensor.varmeopt_forbrug_varme", dict(self.ha.published))
