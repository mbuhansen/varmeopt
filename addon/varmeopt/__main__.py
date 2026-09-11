"""Varmeopt — hovedløkke.

Add-on'en er den der regner: den læser sine målinger fra Home Assistant, lærer
i sin egen COP-tabel, slår op med den rettede interpolation og træffer valget.
Node-RED står tilbage som den hånd der rører anlægget — den følger
beslutningen når flaget siger ja — men den regner ikke med, og der læses
ingenting fra den.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from . import VERSION, selfupdate
from .capacity import ChargeRate
from .charge import ChargePlan, slot_start
from .cop import CopTable
from .curve import HeatCurve
from .demand import Balance, Load
from .forecast import Forecast
from .guard import Guard
from .houseload import MAX_AGE_MINUTES, HouseLoad
from .ha import HaError, HomeAssistant, State
from .journal import install as install_journal
from .migrate import (
    COP_TABLE_FILE,
    CURVE_FILE,
    CAPACITY_FILE,
    CHARGE_FILE,
    GUARD_FILE,
    HOUSE_LOAD_FILE,
    SOLAR_FILE,
    STANDBY_FILE,
    load_cop_table,
    load_heat_curve,
    load_solar,
)
from .options import Options
from .planner import Planner, minutes_until_hour
from .prices import (
    BATTERY_LOSS,
    BATTERY_LOSS_DISCHARGE,
    DISCHARGE,
    EXPORT,
    INVERTER_LOSS,
    LOCKED,
    Grid,
    Plan,
    round_trip,
)
from .solar import DayTracker, SolarModel
from .store import Store
from .standby import StandbyTest
from .tank import Buffer, Tank
from .web import WebUI

log = logging.getLogger("varmeopt")

SENSOR = "sensor.varmeopt_cop"
SENSOR_TANK = "sensor.varmeopt_lager"
SENSOR_DEMAND = "sensor.varmeopt_behov"
SENSOR_PRICE = "sensor.varmeopt_elpris"
SENSOR_DECISION = "sensor.varmeopt_beslutning"
# Husets forbrug som lageret maaler det. Det staar ogsaa som attribut paa
# behovssensoren, men kun *nogle gange* som dens vaerdi - naar flowmaaleren
# tier. En attribut kommer ikke i Home Assistants langtidsstatistik, og saa
# kan tallet ikke tegnes en maaned tilbage. Derfor sin egen sensor.
SENSOR_HOUSE = "sensor.varmeopt_husforbrug"
# Opladningen som sit eget flag. Den staar ogsaa som attribut paa
# beslutningen, men et flag man kan spoerge direkte om, er lettere at koble
# videre end en attribut man skal grave ud - og en styring der er let at
# laese rigtigt, bliver oftere laest rigtigt.
SENSOR_CHARGE = "binary_sensor.varmeopt_lad_op"

def _clock_ahead(minutes: float) -> str:
    """Saa mange minutter frem som et klokkeslaet paa vaeggen.

    Planlaeggeren faar den ind udefra i stedet for at kende uret selv, saa
    den bliver ved med at vaere til at proeve af uden en systemklokke.
    """
    return "kl. " + (
        datetime.now().astimezone() + timedelta(minutes=minutes)
    ).strftime("%H:%M")


# Saa laenge en tavs tank maa svare med sin sidste gode aflaesning. Lageret
# flytter sig ikke langt paa en halv time - pumpen kan laegge 11 kW i, huset
# tager 1-3 - saa et par kelvin er det vaerste der kan ske, og det ligger
# inden for stoejen paa «tre foelere repraesenterer en tank». Derudover er
# tallet en fiktion, og saa er «ved ikke» det aerlige svar.
TANK_HOLD_SECONDS = 30 * 60

# Tabellen gemmes højst så ofte, selv om der læres hvert minut. En skrivning
# pr. minut ville slide unødigt på lagringen uden at redde mere.
SAVE_INTERVAL_SECONDS = 300


class Varmeopt:
    def __init__(self, options: Options, store: Store) -> None:
        self.options = options
        self.store = store
        self.table = CopTable()
        self.curve = HeatCurve(dhw_setpoint=options.dhw_setpoint)
        self.solar = SolarModel(options.geometry)
        self.solar_day = DayTracker()
        self.forecast = Forecast()
        self._forecast_at: float | None = None
        self.guard = Guard(
            enabled=options.control_enabled,
            min_dwell_minutes=options.control_min_dwell_minutes,
            warmup_minutes=options.control_warmup_minutes,
            confirm_minutes=options.control_confirm_minutes,
        )
        self.planner = Planner(
            pellet_price=options.pellet_kwh_price,
            hysteresis=options.source_hysteresis,
            wear_kr_per_kwh=options.hp_wear_kr_per_kwh,
            min_charge_kwh=options.min_charge_kwh,
            charge_kw=options.hp_charge_kw,
            horizon_minutes=int(options.planner_horizon_hours * 60),
            dhw_temp=options.dhw_usable_temp,
            # Begrundelserne skriver klokkeslaet i stedet for minutter.
            # «kl. 13:26» kan laeses; «om 510 min» skal regnes.
            clock=_clock_ahead,
        )
        self.status: dict[str, Any] = {"note": "starter", "lookup": None}
        # Staatabsmaalingen. Den maaler kun naar brugeren selv har aabnet et
        # vindue - se standby.py for hvorfor den ikke bare kan aflaese det.
        self.standby = StandbyTest()
        # Husets forbrug laest af lageret, som bagstopper naar
        # flowmaaleren ligger under sin bund - se houseload.py.
        self.house_load = HouseLoad()
        # Typeskiltet siger 16 kW; maskinen bestemmer selv og lander omkring
        # 12. Raten maales derfor frem for at gaettes - se capacity.py.
        self.charge_rate = ChargeRate(nameplate_kw=options.hp_charge_kw)
        # Opladningen som en blok: planlagt én gang, koert én gang. Se
        # charge.py for hvorfor det ikke er en beslutning pr. minut.
        self.charge_plan = ChargePlan()
        # Sidste gode aflaesning pr. tank, saa et enkelt minuts tavshed ikke
        # halverer lageret. Kun i hukommelsen: efter en genstart er svaret
        # «ved ikke», og det er det rigtige svar.
        self._tank_last: dict[str, tuple[float, Tank]] = {}
        self._tank_held: tuple[str, ...] = ()
        self._dirty = False
        # Sig det én gang pr. ny uenighed, ikke hvert minut.
        self._last_status_warning: str | None = None
        self._warned_limit_unit = False
        self._warned_horizon = False
        self._warned_losses = False
        self._warned_hp_cop = False
        self._hp_cop: float | None = None
        self._last_save = 0.0
        self._last_learned_stamp: str | None = None

    # ------------------------------------------------------------------ cyklus

    async def cycle(self, ha: HomeAssistant | None) -> None:
        flow_temp = outdoor_temp = measured_cop = measured_stamp = None
        flow_measured = hp_flow = hp_return = None

        if ha is not None:
            flow_temp = await self._number(ha, self.options.entity_flow_temp)
            flow_measured = await self._number(ha, self.options.entity_flow_measured)
            hp_flow = await self._number(ha, self.options.entity_hp_flow)
            hp_return = await self._number(ha, self.options.entity_hp_return)
            outdoor_temp = await self._number(ha, self.options.entity_outdoor_temp)
            measured = await self._state(ha, self.options.entity_cop_measured)
            if measured is not None:
                measured_cop = measured.as_float()
                measured_stamp = measured.last_changed

        buffer = await self._read_tank(ha)
        # Bagstopperen er forrige cyklus' maaling. Den er hoejst et minut
        # gammel mod et vindue paa en halv time, og raekkefoelgen kan ikke
        # vendes: maalingen har brug for den balance vi er ved at bygge.
        balance = await self._read_balance(
            ha, measured_cop, self.house_load.kw_at(time.time(), outdoor_temp)
        )
        vessels = await self._read_vessels(ha)
        solar = await self._read_solar(ha, buffer)

        # Kalder varmtvandsbeholderen eller spabadet, overstyres varmekurven
        # med et fast setpunkt, og de maalinger hoerer ikke til i kurven.
        # Udgangene siger det som en kendsgerning; setpunktet ville kun
        # vaere et gaet.
        room_temp = (
            await self._number(ha, self.options.entity_room_temp) if ha else None
        )
        dhw_active = await self._binary(ha, self.options.entity_dhw_active) if ha else None
        mode, is_dhw = _mode(
            dhw_active,
            vessels.get("spa_heating"),
            flow_temp,
            self.curve,
            outdoor_temp,
        )

        # Udgangen naar den svarer, setpunktet naar den ikke goer.
        dhw_fact = dhw_active if dhw_active is not None else is_dhw

        # Husets forbrug laest af lagerets energiaendring. Den koerer efter
        # brugsvandsflaget, for et bad tapper de samme tanke som huset, og en
        # energibalance kan ikke se forskel paa de to.
        load_note = self.house_load.observe(
            time.time(),
            buffer.heat_kwh if buffer is not None else None,
            balance.sources if balance is not None else None,
            inputs_known=balance.inputs_known if balance is not None else False,
            # Samme kendsgerning som varmekurven bruger. Falder
            # varmtvandsudgangen ud, er det raa flag None, vinduet kasseres
            # ikke, og et bad paa op til 8 kW bogfoeres som husets forbrug -
            # og laeres varigt ind i forbrugskurven. ``is_dhw`` genkender
            # ogsaa setpunktet, saa der er noget at falde tilbage paa.
            dhw=dhw_fact,
            spa=vessels.get("spa_heating"),
            # De *maalte* foelere, ikke lagene. ``sensor_count`` taeller
            # ``len(layers)``, og ``layers`` interpolerer det manglende lag og
            # giver stadig tre - saa én doed foeler aendrede ikke tallet, og
            # vagten mod at maale hen over et foelerskift kunne aldrig
            # udloeses. Naar foeleren kommer igen, springer ``heat_kwh``
            # naesten to kWh, og hældningen over vinduet bliver til flere kW
            # husforbrug der laeres permanent ind i kurven.
            sensors=buffer.sensors_lost if buffer is not None else None,
            outdoor=outdoor_temp,
            meter_kw=balance.load.kw if balance is not None and balance.load.trustworthy else None,
            standby_kw=self.standby.loss_kw_at(
                buffer.mean_temp if buffer is not None else None, room_temp
            ),
            vessel_kw=self._vessel_kw(
                dhw_fact, vessels.get("spa_heating"), vessels.get("vvb_bottom")
            ),
        )
        if self.house_load.measured_at is not None:
            self._dirty = True

        # Hvor hurtigt pumpen faktisk fylder lageret. Planlaeggeren regner
        # baade tid og maengde ud fra den, saa et typeskilt der lyver en
        # tredjedel, faar den til at starte for sent.
        self.charge_rate.observe(balance.heatpump_kw if balance is not None else None)
        self.planner.charge_kw = self.charge_rate.effective_kw
        # Og mindstetraekket med. Det er ét minimumstraek - de minutter
        # pumpen skal koere for ikke at kortcykle - og det er kun det samme
        # tal som typeskiltets naar pumpen leverer typeskiltets kW. Den
        # leverer omkring 11, saa de 4,0 kWh fra options svarede til 22
        # minutter og ikke til de 15 reglen handler om. Blokken laegges i
        # forvejen med den maalte rate; nu regner begge ender med den samme.
        self.planner.min_charge_kwh = (
            self.charge_rate.effective_kw * self.options.hp_min_runtime_minutes / 60
        )

        curve_note = None
        if flow_temp is not None and outdoor_temp is not None:
            curve_note = self.curve.learn(outdoor_temp, flow_temp, is_dhw)
            if not curve_note.startswith("ignoreret"):
                self._dirty = True

        learn_note = "—"
        if flow_temp is not None and outdoor_temp is not None:
            if measured_cop is not None:
                learn_note = self._learn(
                    flow_temp, outdoor_temp, measured_cop, measured_stamp
                )
            lookup = self.table.lookup(flow_temp, outdoor_temp)
        else:
            lookup = None
            learn_note = "ignoreret: mangler temperaturdata"

        await self._refresh_forecast(ha)
        prices = await self._read_prices(ha, lookup)

        # Planlaeggeren binder pris, COP, lager og sol sammen. Den svarer
        # ogsaa uden en plan - saa er det bare kildevalget.
        # Lageret maa kun *handles* paa naar alle tankene svarer. Svarer
        # kun den ene, er summen ikke en ringere maaling - den er forkert, og
        # en halveret plads er praecis det der lagde en blok der ikke skulle
        # laegges. Vises maa den gerne; det er en anden ting.
        store = buffer if buffer is not None and buffer.complete else None
        decision = self.planner.decide(
            plan=prices.get("plan"),
            cop_now=lookup.cop if lookup is not None else None,
            cop_later=self._cop_at,
            charge_cop_at=self._charge_cop_at,
            # Pladsen maales op til den temperatur blokken lader ved, ikke op
            # til varmepumpens loft. De sidste grader op til 60 hoerer til
            # solvarmen og ACthor, og en blok kan ikke fylde dem.
            headroom_kwh=(
                store.room_to(self.options.hp_charge_temp)
                if store is not None
                else None
            ),
            peak_headroom_kwh=store.peak_headroom_kwh if store is not None else None,
            stored_kwh=store.stored_kwh if store is not None else None,
            # Den del af lageret der er varm nok til at lade beholderen. Uden
            # den blev 13 kWh ved 45 grader talt med mod en aften der delvis
            # er varmt vand - og lageret kunne ikke lave et eneste bad.
            hot_kwh=(
                store.usable_kwh(self.options.dhw_usable_temp)
                if store is not None
                else None
            ),
            # Varmtvandet i det dyre vindue, ikke i de naeste timer: profilen
            # laeses fra vinduets begyndelse.
            dhw_kwh_over=lambda start_min, hours: (
                self.house_load.vessels.kwh_between(
                    time.time() + start_min * 60, hours
                )
            ),
            # Og hvad det koster at faa den varme til at *staa* der. Lagerets
            # fysik hoerer hjemme i tank.py, ikke i planlaeggeren.
            dhw_input_for=(
                (lambda kwh: store.energy_to_reach(kwh, self.options.dhw_usable_temp))
                if store is not None
                else None
            ),
            solar_expected_kwh=solar.get("solar_expected"),
            grid=prices.get("grid"),
            demand_kw=balance.load.kw if balance is not None else None,
            demand_kw_at=self._demand_at,
            # Fristen paa uret. Den regnes her og ikke i planlaeggeren:
            # planlaeggeren faar minutter, ikke et klokkeslaet, saa den kan
            # proeves af uden at nogen skal stille en systemklokke.
            deadline_minutes=minutes_until_hour(
                self.options.store_full_by_hour, time.time()
            ),
        )
        # Vagten siger ikke hvad der skal goeres - kun om nogen boer goere
        # det. Siger den nej, staar beslutningen der stadig, men flaget
        # siger nej, og Node-RED bruger sin egen logik.
        # Vaegurstid, ikke monoton - kun den giver mening paa tvaers af en
        # genstart, og opholdstiden skal fortsaette hvor den slap.
        #
        # Vagten spoerges *foer* blokken. Blokken skal kende den kilde vagten
        # staar ved, ikke planlaeggerens raa svar: ladeflaget var det eneste
        # udgang i huset uden hviletid, saa ét minuts udsving i COP eller pris
        # kunne afslutte en opladning som vagten samtidig holdt paa
        # varmepumpen. Vagten laeser kun ``source`` og ``heat_price``, aldrig
        # ``charge``, saa den kan trygt gaa foerst.
        command = self.guard.check(
            decision, lookup, prices.get("plan"), time.time()
        )

        # Opladningen er en blok, ikke en beslutning pr. minut. Den siger
        # ja eller nej for hele sit forloeb, og beslutningens flag rettes ind
        # efter den, saa flaget, attributterne og planen siger det samme.
        charging = self.charge_plan.update(
            time.time(),
            decision,
            prices.get("plan"),
            self.charge_rate.effective_kw,
            # Og «fuldt» maa heller ikke afgoeres paa et halvt lager: det
            # afslutter en koerende blok. Samme loft som pladsen ovenfor -
            # ellers ville planlaeggeren sige «ingen plads» mens blokken kunne
            # koere videre mod et loft den ikke kan naa.
            full=(
                store is not None
                and store.room_to(self.options.hp_charge_temp) <= 0.01
            ),
            source=command.source,
            min_runtime_minutes=self.options.hp_min_runtime_minutes,
        )
        decision = replace(decision, charge=charging)
        projection = self.planner.project(
            prices.get("plan"),
            lookup.cop if lookup is not None else None,
            cop_later=self._cop_at,
            target_minutes=decision.window_minutes,
            grid=prices.get("grid"),
            charge_window=self._charge_window(),
        )

        self.status.update(
            flow_temp=flow_temp,
            outdoor_temp=outdoor_temp,
            measured_cop=measured_cop,
            lookup=lookup,
            tank=buffer,
            balance=balance,
            **vessels,
            **solar,
            **prices,
            decision=decision,
            command=command,
            forecast=self.forecast,
            projection=projection,
            flow_measured=flow_measured,
            hp_flow=hp_flow,
            hp_return=hp_return,
            # Løftet over kondensatoren. Et løft nær nul betyder at pumpen
            # ikke laver noget, uanset hvad COP-føleren måtte påstå.
            hp_lift=_difference(hp_flow, hp_return),
            mode=mode,
            dhw_active=dhw_active,
            room_temp=room_temp,
            standby=self.standby.observe(
                time.time(),
                buffer.mean_temp if buffer is not None else None,
                room_temp,
                sum(t.liters for t in buffer.measured) if buffer is not None else 0.0,
                balance.sources if balance is not None else None,
            ),
            standby_ua=self.standby.ua_w_per_k,
            standby_loss_kw=self.standby.loss_kw_at(
                buffer.mean_temp if buffer is not None else None, room_temp
            ),
            hp_power_kw=balance.hp_power_kw if balance is not None else None,
            hp_heat_kw=balance.heatpump_kw if balance is not None else None,
            # Varmeydelse delt med elforbrug - anlaeggets egen COP, regnet af
            # to maalinger i stedet for laest af en foeler.
            hp_cop_measured=self._hp_cop,
            house_load=load_note,
            house_load_kw=self.house_load.kw,
            house_load_curve_kw=(
                self.house_load.curve.predict(outdoor_temp)
                if outdoor_temp is not None
                else None
            ),
            charge_plan=self.charge_plan.note,
            charge_rate=self.charge_rate.note,
            charge_rate_kw=self.charge_rate.effective_kw,
            house_load_bias=self.house_load.bias_kw,
            house_load_points=self.house_load.curve.point_count,
            vessel_hours=self.house_load.vessels.known_hours,
            vessel_kwh_today=self.house_load.vessels.kwh_between(time.time(), 24.0),
            curve_note=curve_note,
            predicted_setpoint=(
                self.curve.predict(outdoor_temp) if outdoor_temp is not None else None
            ),
            learn_note=learn_note,
            last_run=datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
        )

        if ha is not None:
            # Flaget foerst. Det er den ene skrivning der er
            # sikkerhedskritisk, og foer laa den sidst - efter fem andre
            # der hver kunne afbryde cyklussen foer den blev naaet.
            #
            # Loggen skriver den *udgivne* kilde, for det er den entiteten
            # staar paa. Den raa kommer i parentes naar vagten holder noget
            # andet, saa linjen viser baade hvad planlaeggeren mente og hvad
            # der faktisk stod.
            published = command.source or decision.source
            raw = "" if published == decision.source else f" (raa {decision.source})"
            log.info(
                "beslutning: %s%s | %s | styring: %s",
                published,
                raw,
                decision.reason,
                command.note,
            )
            await self._safely("beslutning", self._publish_decision(ha, decision, command))
            await self._safely(
                "opladningsflag", self._publish_charge(ha, decision, command)
            )

        if lookup is not None:
            log.info(
                "COP %.2f (%s: %s) | %s: setpunkt %.1f, maalt %s, ude %.1f | laering: %s",
                lookup.cop,
                lookup.source,
                lookup.detail,
                mode or "?",
                flow_temp,
                f"{flow_measured:.1f}" if flow_measured is not None else "-",
                outdoor_temp,
                learn_note,
            )
            if ha is not None:
                await self._safely("COP", self._publish(ha, lookup))
        else:
            log.warning("springer cyklus over: %s", learn_note)

        if buffer is not None and ha is not None:
            log.info(
                "lager %.1f kWh (%.0f %% fuldt), plads til %.1f kWh | %s",
                buffer.stored_kwh,
                buffer.charge_percent or 0.0,
                buffer.headroom_kwh,
                _tank_summary(buffer),
            )
            await self._safely("lager", self._publish_tank(ha, buffer))

        if balance is not None and ha is not None:
            load_kw = balance.load.kw
            if load_kw is not None or balance.sources:
                horizon = ""
                if buffer is not None:
                    left = balance.hours_left(buffer.stored_kwh)
                    full = balance.hours_to_full(buffer.headroom_kwh)
                    if left is not None:
                        horizon = f" | raekker {left:.1f} t"
                    elif full is not None:
                        horizon = f" | fuld om {full:.1f} t"
                net = balance.net_kw
                log.info(
                    "behov %s | ind %.2f kW (%s) | netto %s%s",
                    f"{load_kw:.2f} kW" if load_kw is not None else "-",
                    balance.input_kw,
                    ", ".join(f"{k} {v:.2f}" for k, v in balance.sources.items())
                    or "ingen kilder",
                    f"{net:+.2f} kW" if net is not None else "-",
                    horizon,
                )
            await self._safely("behov", self._publish_demand(ha, balance, buffer))
            await self._safely("husforbrug", self._publish_house_load(ha))

        if prices.get("price_now") is not None and ha is not None:
            price = prices["price_now"]
            heat = prices.get("heat_price")
            log.info(
                "el %.2f kr/kWh (%s) | varme %s mod pille %.2f",
                price.kr_per_kwh,
                price.reason,
                f"{heat:.2f}" if heat is not None else "-",
                prices["pellet_price"],
            )
            await self._safely(
                "elpris", self._publish_price(ha, prices, decision, command)
            )

        self._maybe_save()

    async def _publish(self, ha: HomeAssistant, lookup: Any) -> None:
        await ha.set_state(
            SENSOR,
            round(lookup.cop, 2),
            {
                "friendly_name": "Varmeopt COP",
                "unit_of_measurement": "",
                "state_class": "measurement",
                "icon": "mdi:heat-pump",
                "kilde": lookup.source,
                "metode": lookup.detail,
                "laert_cop": lookup.learned_cop,
                "laert_antal": round(lookup.learned_count, 1),
                # "fremloeb" hed det, men det er UVR'ens setpunkt, ikke en
                # måling. Nu står begge, så de ikke kan forveksles.
                "setpunkt": self.status.get("flow_temp"),
                "freml_maalt": self.status.get("flow_measured"),
                "afvigelse": _round(_difference(
                    self.status.get("flow_measured"), self.status.get("flow_temp")
                ), 1),
                "tilstand": self.status.get("mode"),
                "vp_frem_bt12": self.status.get("hp_flow"),
                "vp_retur_bt3": self.status.get("hp_return"),
                "vp_loeft": _round(self.status.get("hp_lift"), 1),
                "setpunkt_forudsagt": _round(self.status.get("predicted_setpoint"), 1),
                "ude": self.status.get("outdoor_temp"),
                "maalt_cop": self.status.get("measured_cop"),
                "celler": self.table.cell_count,
                "maalinger": round(self.table.sample_count),
            },
        )

    # -------------------------------------------------------------- varmelager

    async def _read_tank(self, ha: HomeAssistant | None) -> Buffer | None:
        """Læs de otte tankfølere. None hvis ingen af dem svarer."""
        if ha is None:
            return None

        share = self.options.tank_liters / max(1, len(self.options.tanks))
        now = time.time()
        tanks: list[Tank] = []
        held: list[str] = []
        for name, top, mid, bottom, outlet in self.options.tanks:
            tank = Tank(
                name=name,
                liters=share,
                top=await self._number(ha, top),
                mid=await self._number(ha, mid),
                bottom=await self._number(ha, bottom),
                outlet=await self._number(ha, outlet),
            )
            if tank.covered:
                self._tank_last[name] = (now, tank)
            else:
                # En tank uden ét eneste svar er ikke en koldere tank, den er
                # en ukendt tank - og uden det her halverer summen sig i
                # tavshed. Natten til den 10. september skete det i ét minut:
                # 22,6 -> 11,8 kWh, og baade opladningen og «lageret er
                # fuldt» laeser den sum.
                #
                # Mangler den kun *nogle* foelere, holdes den ikke. Det er
                # praecis det tilfaelde gradientreglen i tank.py er skrevet
                # til, og en gammel aflaesning ville overtroeve den.
                cached = self._tank_last.get(name)
                if cached is not None and now - cached[0] <= TANK_HOLD_SECONDS:
                    tank = cached[1]
                    held.append(name)
            tanks.append(tank)
        self._tank_held = tuple(held)
        buffer = Buffer(
            tuple(tanks),
            self.options.tank_reference_temp,
            self.options.tank_max_temp,
            self.options.tank_peak_temp,
            self.options.tank_cascade_temp,
        )
        return buffer if buffer.covered else None



    async def _publish_tank(self, ha: HomeAssistant, buffer: Buffer) -> None:
        attributes: dict[str, Any] = {
            "friendly_name": "Varmeopt lager",
            "unit_of_measurement": "kWh",
            "state_class": "measurement",
            "icon": "mdi:water-boiler",
            "plads_kwh": round(buffer.headroom_kwh, 2),
            "fyldning_pct": _round(buffer.charge_percent, 1),
            "middel_temp": _round(buffer.mean_temp, 1),
            "leverer_op_til": _round(buffer.deliverable, 1),
            "ubalance_k": _round(buffer.imbalance, 1),
            "foelere": buffer.sensor_count,
            "reference_temp": buffer.reference,
            "loft_temp": buffer.ceiling,
            "plads_i_alt_kwh": round(buffer.peak_headroom_kwh, 2),
            "over_vp_loft": buffer.above_heatpump_ceiling,
            # En manglende dybdefoeler goer lagerenergien til et skoen. Det
            # skal kunne ses, ikke bare regnes videre paa.
            "foelere_mangler": buffer.sensors_lost,
            # Og en helt tavs tank goer den til noget vaerre end et skoen.
            # Saa laenge den holdes paa sin sidste gode aflaesning, staar
            # tallet der stadig - men det er ikke maalt lige nu, og
            # opladningen roerer det ikke.
            "lager_komplet": buffer.complete,
            "lager_holdt": ", ".join(self._tank_held) or "—",
            # Rummet tankene staar i. Staatabet foelger forskellen til det
            # her, ikke til en antaget kaeldertemperatur - og de to tal
            # sammen er raamaterialet til at maale tabet naar der en nat
            # hverken tilfoeres eller traekkes noget.
            "rum_temp": _round(self.status.get("room_temp"), 1),
            "over_rummet_k": _round(
                None
                if self.status.get("room_temp") is None or buffer.mean_temp is None
                else buffer.mean_temp - self.status["room_temp"],
                1,
            ),
            # Hvor meget af varmepumpens baand solen selv tager i dag, og hvad
            # der saa er tilbage at lade uden at fortraenge gratis varme.
            "forventet_solvarme_kwh": _round(self.status.get("solar_expected"), 1),
            "vp_maa_lade_kwh": _round(self.status.get("solar_may_charge"), 1),
            "solvarme_i_dag_kwh": self.status.get("solar_today"),
            "solar_k": _round(self.status.get("solar_scale"), 3),
            # Beholderne ved siden af: de deler varmekilder med tankene, men
            # ikke energi, og de må derfor ikke lægges sammen med dem.
            "vvb_top": self.status.get("vvb_top"),
            "vvb_bund": self.status.get("vvb_bottom"),
            "spa_temp": self.status.get("spa_temp"),
            "spa_maal": self.status.get("spa_target"),
            "spa_varmer": self.status.get("spa_heating"),
            "varmtvand_koerer": self.status.get("dhw_active"),
        }
        for tank in buffer.measured:
            key = tank.name.lower()
            attributes[f"tank_{key}_top"] = tank.top
            attributes[f"tank_{key}_midt"] = tank.mid
            attributes[f"tank_{key}_bund"] = tank.bottom
            attributes[f"tank_{key}_afgang"] = tank.outlet
            attributes[f"tank_{key}_lagdeling"] = _round(tank.spread, 1)

        await ha.set_state(SENSOR_TANK, round(buffer.stored_kwh, 2), attributes)

    def _learn(
        self,
        flow_temp: float,
        outdoor_temp: float,
        measured_cop: float,
        stamp: str | None,
    ) -> str:
        """Indarbejd en måling, men kun én gang pr. måling.

        Node-RED lærer hændelsesdrevet — dens ``Cop learning``-node fyrer når
        sensoren skifter. Vi poller i stedet, og uden det her ville en
        stillestående aflæsning blive lært om igen hver eneste cyklus.
        ``count`` ville så tælle minutter i stedet for målinger, og det er
        præcis det tal der afgør hvor meget en lært celle vejer mod TA-kurven.
        Et døgn i ét driftspunkt ville dermed drukne den migrerede historik.

        Uden ``last_changed`` (lokal afprøvning mod en attrap) kan vi ikke
        kende to målinger fra hinanden, og så lærer vi hellere for meget end
        for lidt.
        """
        if stamp is not None and stamp == self._last_learned_stamp:
            return "ignoreret: uændret måling, allerede lært"

        note = self.table.learn(flow_temp, outdoor_temp, measured_cop)
        if not note.startswith("ignoreret"):
            self._last_learned_stamp = stamp
            self._dirty = True
        return note

    @staticmethod
    async def _state(ha: HomeAssistant, entity_id: str) -> State | None:
        if not entity_id:
            return None
        try:
            return await ha.get_state(entity_id)
        except HaError as exc:
            log.warning("%s: %s", entity_id, exc)
            return None

    @classmethod
    async def _number(cls, ha: HomeAssistant, entity_id: str) -> float | None:
        state = await cls._state(ha, entity_id)
        return state.as_float() if state else None

    @classmethod
    async def _power_kw(cls, ha: HomeAssistant, entity_id: str) -> float | None:
        """Effekt i kW, uanset om føleren melder watt eller kilowatt.

        Enheden læses af entitetens egen attribut frem for at antages: ACthor
        melder watt, solvarmen kilowatt, og en faktor tusind det forkerte sted
        ville se ud som et anlæg der yder vanvittigt.
        """
        state = await cls._state(ha, entity_id)
        if state is None:
            return None
        value = state.as_float()
        if value is None:
            return None
        unit = str(state.attributes.get("unit_of_measurement", "")).strip().lower()
        return value / 1000 if unit in ("w", "watt") else value

    async def _round_trip(self, ha: HomeAssistant) -> float:
        """Hvor stor en del af en koebt kWh der naar ud af batteriet igen.

        Tabene laeses af Predbats egne indstillinger i stedet for at skrives
        af. Saa er der ét sted de staar, og aendrer man dem dér, foelger
        genanskaffelsesprisen med. Svarer entiteterne ikke, gaelder
        ``prices.py``s standardvaerdier - det er et par procent, ikke en
        anden beslutning, saa det maa ikke standse en cyklus.
        """
        losses: list[float] = []
        for entity, fallback in (
            (self.options.entity_inverter_loss, INVERTER_LOSS),
            (self.options.entity_battery_loss, BATTERY_LOSS),
            (self.options.entity_battery_loss_discharge, BATTERY_LOSS_DISCHARGE),
        ):
            value = await self._number(ha, entity) if entity else None
            # Et tab er en broekdel, ikke en procent. Melder entiteten 4 i
            # stedet for 0,04, ville rundturen blive negativ - og et
            # batteri der leverer mere end det faar, er ikke en pris vi
            # skal regne videre paa.
            if value is None or not 0 <= value < 1:
                if not self._warned_losses:
                    log.warning(
                        "kunne ikke laese Predbats tab fra %s - regner med "
                        "add-on'ens egne tal",
                        entity or "(ikke sat)",
                    )
                    self._warned_losses = True
                value = fallback
            losses.append(value)
        return round_trip(*losses)

    async def _charge_limit(self, ha: HomeAssistant) -> float | None:
        """Predbats graense, som en ladetilstand i procent.

        Den skal kunne sammenlignes med planens ``soc_percent``, og de to er
        kun sammenlignelige hvis begge er procent. Melder entiteten kWh, kan
        vi ikke regne om uden batteriets stoerrelse, og saa er det rigtige
        svar ingenting - sagt hoejt én gang, ikke gaettet hver cyklus.
        """
        state = await self._state(ha, self.options.entity_predbat_charge_limit)
        if state is None:
            return None
        value = state.as_float()
        if value is None:
            return None
        unit = str(state.attributes.get("unit_of_measurement", "")).strip().lower()
        if unit not in ("", "%", "percent"):
            if not self._warned_limit_unit:
                log.warning(
                    "%s melder %r og ikke procent - graensen bruges ikke",
                    self.options.entity_predbat_charge_limit,
                    unit,
                )
                self._warned_limit_unit = True
            return None
        return value

    @classmethod
    async def _binary(cls, ha: HomeAssistant, entity_id: str) -> bool | None:
        state = await cls._state(ha, entity_id)
        return None if state is None else state.state.strip().lower() == "on"

    async def _read_vessels(self, ha: HomeAssistant | None) -> dict[str, Any]:
        """Varmtvandsbeholder og spa — de to lagre ved siden af buffertankene.

        Begge kalder med samme setpunkt som brugsvandet, så deres tilstand
        forklarer hvorfor varmekurven pludselig springer til 56 °C.
        """
        if ha is None:
            return {}
        return {
            "vvb_top": await self._number(ha, self.options.entity_vvb_top),
            "vvb_bottom": await self._number(ha, self.options.entity_vvb_bottom),
            "spa_temp": await self._number(ha, self.options.entity_spa_temp),
            "spa_target": await self._number(ha, self.options.entity_spa_target),
            "spa_heating": await self._binary(ha, self.options.entity_spa_heater),
        }

    # ------------------------------------------------------------ vejrudsigt

    async def _refresh_forecast(self, ha: HomeAssistant | None) -> None:
        """Hent udsigten, men ikke hvert minut — den ændrer sig i timer."""
        if ha is None or not self.options.entity_weather:
            return
        now = asyncio.get_running_loop().time()
        if (
            self._forecast_at is not None
            and now - self._forecast_at < self.options.forecast_refresh_minutes * 60
        ):
            return

        self._forecast_at = now
        try:
            response = await ha.call_service(
                "weather",
                "get_forecasts",
                {"entity_id": self.options.entity_weather, "type": "hourly"},
            )
        except HaError as exc:
            log.warning("kunne ikke hente vejrudsigten: %s", exc)
            return

        forecast = Forecast.from_response(
            response, self.options.entity_weather, datetime.now(timezone.utc)
        )
        if len(forecast):
            self.forecast = forecast
            log.info(
                "vejrudsigt: %d timer frem, %.1f til %.1f grader",
                forecast.horizon_minutes / 60,
                min(t for _, t in forecast.points),
                max(t for _, t in forecast.points),
            )
        else:
            # Sig hvad der kom tilbage. Stod der bare "kunne ikke laeses",
            # var det umuligt at se om entiteten var forkert, eller om svaret
            # havde en anden form end den vi pakker ud.
            keys = sorted(response) if isinstance(response, dict) else type(response).__name__
            log.warning(
                "vejrudsigten fra %s kunne ikke laeses - svaret indeholdt %s",
                self.options.entity_weather,
                keys,
            )

    def _charge_cop_at(self, minutes: int) -> float | None:
        """COP'en ved ladetemperaturen om saa mange minutter.

        Samme kaede som ``_cop_at``, men uden varmekurven: setpunktet er
        givet. En blok koerer ``hp_charge_temp`` - 56 grader - og det er
        derfor varmen bagefter ogsaa kan lave et bad.

        Ikke ``dhw_setpoint``. Den er det setpunkt beholderen *kalder* med, og
        paa anlaegget her staar den paa 53. De to stod som ét i et doegn, og
        saa blev COP'en slaaet op tre grader for lavt og pladsen maalt til en
        temperatur lavere end den blokken naar.
        """
        temp = self.forecast.temperature_at(minutes)
        if temp is None:
            return None
        return self.table.lookup(self.options.hp_charge_temp, temp).cop

    def _demand_at(self, minutes: int) -> float | None:
        """Hvad huset ventes at traekke om saa mange minutter.

        Samme kaede som ``_cop_at``, men den korte ende af den: forudsagt
        temperatur gennem den indlaerte forbrugskurve. Udsigten klemmer fast
        paa yderpunkterne i stedet for at svare ingenting, saa naar der
        overhovedet er en udsigt, er der ogsaa et svar - og kurven svarer kun
        ``None`` foer den har laert sit foerste punkt.

        Den maalte vaerdi staar med vilje ikke her. Den hoerer til nuet, og
        det her er en udsigt - se ``Planner._displaced_kwh``.
        """
        temp = self.forecast.temperature_at(minutes)
        if temp is None:
            return None
        return self.house_load.curve.predict(temp)

    def _cop_at(self, minutes: int) -> float | None:
        """COP om saa mange minutter, hele vejen gennem kaeden.

        Forudsagt temperatur -> varmekurven giver setpunktet -> COP-tabellen
        giver virkningsgraden. Uden udsigt er der intet svar, og planlaeggeren
        falder tilbage paa den COP vi har nu.
        """
        temp = self.forecast.temperature_at(minutes)
        if temp is None:
            return None
        setpoint = self.curve.predict(temp)
        if setpoint is None:
            return None
        return self.table.lookup(setpoint, temp).cop

    # ---------------------------------------------------------------- pris

    async def _read_prices(
        self, ha: HomeAssistant | None, lookup: Any
    ) -> dict[str, Any]:
        """Marginalprisen nu og fremad, og hvad varmen dermed koster."""
        if ha is None:
            return {}

        state = await self._state(ha, self.options.entity_predbat_plan)
        if state is None:
            return {}

        # En foraeldet plan er farligere end ingen plan: priserne ser
        # gyldige ud, saa vagten gaar igennem alle porte paa tal fra et
        # andet tidspunkt.
        age = state.age_seconds()
        if age is not None and age > self.options.plan_max_age_minutes * 60:
            log.warning(
                "Predbats plan er %.0f min gammel - regner uden den", age / 60
            )
            return {}

        plan = Plan.from_predbat(
            state.attributes,
            trip=await self._round_trip(ha),
            empty_percent=self.options.battery_empty_percent,
        )
        if not len(plan):
            log.warning(
                "kunne ikke laese Predbats plan fra %s", self.options.entity_predbat_plan
            )
            return {}

        # Raekker Predbats plan laengere end vi kigger, ser vi ikke enden paa
        # det dyre. Saa bliver straekket afkortet, behovet for lille, og
        # blokken for kort - og intet siger det. Sig det én gang.
        if plan.horizon_minutes > self.planner.horizon_minutes and not self._warned_horizon:
            self._warned_horizon = True
            log.warning(
                "Predbats plan raekker %.0f timer, men horisonten er %.0f - "
                "det dyre straek bliver afkortet, og opladningen for lille. "
                "Saet planner_horizon_hours op i add-on'ens indstillinger.",
                plan.horizon_minutes / 60,
                self.planner.horizon_minutes / 60,
            )

        grid = Grid(
            battery_power=await self._number(ha, self.options.entity_battery_power) or 0.0,
            grid_power=await self._number(ha, self.options.entity_grid_power) or 0.0,
            pv_power=await self._number(ha, self.options.entity_pv_power) or 0.0,
            inverter_ac=await self._number(ha, self.options.entity_inverter_ac) or 0.0,
            discharge_floor=await self._charge_limit(ha),
        )
        now = plan.marginal(0, grid=grid)
        if now is None:
            return {}

        # Predbats egen tilstand lige nu. Planens raekker bruger samme
        # ordforraad pr. halvtime, saa den her er den eneste maade at se hvad
        # *dette* anlaegs Predbat faktisk skriver - i stedet for at gaette paa
        # dokumentationen. Siger de to noget forskelligt om den halvtime vi
        # staar i, er det os der laeser planen forkert.
        status = await self._state(ha, self.options.entity_predbat_status)
        if status is not None and plan.slots:
            self._check_predbat_status(status.state, plan.slots[0])

        return {
            "plan": plan,
            "price_now": now,
            "grid": grid,
            "predbat_status": status.state if status is not None else None,
            "heat_price": self.planner.heat_price(
                now.kr_per_kwh, lookup.cop if lookup is not None else None
            ),
            "pellet_price": self.options.pellet_kwh_price,
        }

    @staticmethod
    def _status_mode(text: str) -> str | None:
        """Predbats statustekst oversat til de samme tre udfald som planen.

        Statussen er en sætning — «Hold charging» — hvor planens celle er ét
        ord: «holdchrg». Derfor delstrenge her, hvor planen har en tabel.
        Rækkefølgen er ikke til pynt: «Discharging» indeholder «charg».

        Et ord vi ikke tør oversætte, giver ``None``, og så siges der
        ingenting. En kontrol der gætter, er værre end ingen kontrol.
        """
        if "dischrg" in text or "discharg" in text:
            return DISCHARGE
        if "exp" in text:
            return EXPORT
        if "chrg" in text or "charg" in text or "hold" in text:
            return LOCKED
        if "freeze" in text or "frz" in text:
            return LOCKED
        if "demand" in text:
            return DISCHARGE
        return None

    def _check_predbat_status(self, status: str, slot: Any) -> None:
        """Siger Predbat og vores laesning af planen det samme om nu?

        Kun en kontrol - der styres ikke efter den. Men er de uenige om
        indevaerende halvtime, laeser vi planens tilstandsstrenge forkert, og
        saa er hver eneste pris i horisonten et gaet. Det skal staa i loggen,
        ikke opdages en vinter senere.
        """
        text = (status or "").strip().lower()
        if not text or text in ("unknown", "unavailable"):
            return
        theirs = self._status_mode(text)
        if theirs is None or theirs == slot.mode:
            return
        if text != self._last_status_warning:
            log.warning(
                "Predbat siger %r, men planens foerste raekke %r laeses som "
                "%s - tjek tilstandsstrengene",
                status,
                slot.state,
                slot.mode,
            )
            self._last_status_warning = text

    async def _publish_price(
        self,
        ha: HomeAssistant,
        prices: dict[str, Any],
        decision: Any = None,
        command: Any = None,
    ) -> None:
        price = prices["price_now"]
        plan: Plan = prices["plan"]

        attributes: dict[str, Any] = {
            "friendly_name": "Varmeopt elpris",
            "unit_of_measurement": "kr/kWh",
            "state_class": "measurement",
            "icon": "mdi:cash-clock",
            "begrundelse": price.reason,
            # Samme hold-note som paa beslutningen. Den her sensor udgiver
            # de samme to varmepriser, saa modsigelsen mellem tilstand og
            # tal ville ellers staa uforklaret to steder i stedet for ét.
            "kilde_grund": self._held_reason(decision, command)
            if decision is not None
            else None,
            "vp_varmepris": _round(prices.get("heat_price"), 3),
            "pille_varmepris": round(prices["pellet_price"], 3),
            "horisont_timer": round(plan.horizon_minutes / 60, 1),
            "predbat_status": prices.get("predbat_status"),
        }

        for hours in (2, 4, 6):
            ahead = plan.marginal(hours * 60)
            if ahead is not None:
                attributes[f"om_{hours}t"] = round(ahead.kr_per_kwh, 3)
                attributes[f"om_{hours}t_hvorfor"] = ahead.reason

        window = plan.cheapest_window(int(self.options.hp_min_runtime_minutes))
        if window is not None:
            start, average = window
            attributes["billigste_vindue_om_min"] = start
            attributes["billigste_vindue_pris"] = round(average, 3)

        await ha.set_state(SENSOR_PRICE, round(price.kr_per_kwh, 3), attributes)

    async def _safely(self, what: str, coro: Any) -> None:
        """Kør en udgivelse, men lad den ikke vælte de andre.

        Før lå de seks skrivninger i én kæde, så en enkelt ``HaError`` i den
        første afbrød resten — inklusive beslutningsflaget, som er den ene der
        er sikkerhedskritisk.
        """
        try:
            await coro
        except HaError as exc:
            log.warning("kunne ikke udgive %s: %s", what, exc)

    async def release_control(self, ha: HomeAssistant) -> None:
        """Saet begge flag falske, saa Node-RED tager over igen.

        Kaldes ved nedlukning. De to skrives hver for sig og med hver sin
        fejlhaandtering: laa de i samme forsoeg, ville en fejl paa det
        foerste betyde at det andet aldrig blev sluppet — og saa ville et
        stop efterlade praecis den frosne kommando det hele er til for at
        undgaa.

        Opladningen slippes foerst, for den er den farligste at efterlade
        taendt. En frossen kilde ville bare fortsaette som den koerte; et
        frossent "lad op" ville blive ved med at fylde tankene efter vi er
        holdt op med at kunne se paa dem.
        """
        self.guard.release()

        await self._release_one(
            ha,
            SENSOR_CHARGE,
            "off",
            {
                "friendly_name": "Varmeopt lad op",
                "icon": "mdi:battery-charging-high",
                "styrer": False,
                "begrundelse": "add-on'en er stoppet",
            },
        )
        # Den kilde entiteten stod paa, ikke planlaeggerens raa svar - ellers
        # ville et stop selv vaere et tilstandsskifte i HA's historik.
        # ``release()`` ovenfor har ryddet vagtens binding, men kommandoen fra
        # sidste cyklus staar stadig i status og baerer kilden. Nøglen mangler
        # helt hvis vi stopper foer foerste cyklus er faerdig.
        decision = self.status.get("decision")
        command = self.status.get("command")
        last = command.source if command is not None else None
        if last is None and decision is not None:
            last = decision.source
        await self._release_one(
            ha,
            SENSOR_DECISION,
            last if last is not None else "ukendt",
            {
                "friendly_name": "Varmeopt beslutning",
                "icon": "mdi:scale-balance",
                "styrer": False,
                "styr_til": None,
                "styring_grund": "add-on'en er stoppet",
                "begrundelse": "add-on'en er stoppet",
            },
        )

    async def _release_one(
        self, ha: HomeAssistant, entity: str, state: Any, attributes: dict[str, Any]
    ) -> None:
        """Slip ét flag. Fejler det, er der ikke mere vi kan goere end at raabe."""
        try:
            await ha.set_state(entity, state, attributes)
            log.info("gav slip paa %s", entity)
        except HaError as exc:
            log.error(
                "KUNNE IKKE give slip paa %s: %s - den kan staa med styrer=true",
                entity,
                exc,
            )

    async def _publish_charge(
        self, ha: HomeAssistant, decision: Any, command: Any
    ) -> None:
        """Opladningen som et flag, ikke som en attribut.

        Tilstanden er ``on``/``off`` som ethvert andet binary_sensor, saa den
        kan spoerges direkte i stedet for at skulle graves ud af
        beslutningens attributter.

        **Samme regel som paa beslutningen:** tilstanden er hvad
        planlaeggeren *vil*, og ``styrer`` siger om det maa foelges. De to er
        med vilje adskilt — flaget skal kunne ses ogsaa mens styringen er
        slaaet fra, ellers kan man ikke vurdere planen inden man kobler den
        til. Foelg det kun naar ``styrer`` er sand, praecis som med kilden.
        """
        await ha.set_state(
            SENSOR_CHARGE,
            "on" if decision.charge else "off",
            {
                "friendly_name": "Varmeopt lad op",
                "icon": "mdi:battery-charging-high",
                # Samme port som paa beslutningen. Uden den ville flaget se
                # ud som en ordre selv naar ingen har lov at give den.
                "styrer": command.acting,
                "begrundelse": decision.reason,
                "lad_kwh": _round(decision.charge_kwh, 1),
                "besparelse_kr": _round(decision.saving_kr, 2),
                "vindue_min": decision.window_minutes,
                # Blokken: hvornaar den ligger, og hvor meget den er sat til.
                # Opladningen er planlagt én gang og koeres én gang - se
                # charge.py - saa det her er et skema og ikke et oejebliksbud.
                "plan": self.charge_plan.note,
                "starter_om_min": _round(self._charge_minutes()[0], 0),
                "slutter_om_min": _round(self._charge_minutes()[1], 0),
            },
        )

    @staticmethod
    def _held_reason(decision: Any, command: Any) -> str:
        """Begrundelsen, med vagtens hold sat bagpaa naar den holder.

        Uden det her ville sensoren staa paa ``pillefyr`` med teksten
        "VP 0.55 < pille 0.71" ved siden af, og det er vaerre end at vippe:
        tallene i attributterne beskriver stadig det raa svar, saa der skal
        staa hvorfor tilstanden er en anden.
        """
        reason = decision.reason
        if command is None or command.source in (None, decision.source):
            return reason
        return f"{reason} — {command.reason}"

    async def _publish_decision(
        self, ha: HomeAssistant, decision: Any, command: Any
    ) -> None:
        await ha.set_state(
            SENSOR_DECISION,
            # Den kilde vagten staar ved - ikke planlaeggerens raa svar.
            #
            # Det er entitetens *tilstand*, og den er det Node-RED haenger
            # sin ``server-state-changed`` paa. Stod den paa det raa svar,
            # ville en enkelt cyklus med stoej vaere et tilstandsskifte i
            # HA's historik: den 10. september blev det til fjorten paa seks
            # timer. Det raa svar staar i ``raa_kilde``, saa man stadig kan
            # se hvad planlaeggeren ville have sagt.
            command.source if command.source is not None else decision.source,
            {
                "friendly_name": "Varmeopt beslutning",
                "icon": "mdi:scale-balance",
                "begrundelse": self._held_reason(decision, command),
                "raa_kilde": decision.source,
                # Node-RED skal kun foelge os naar "styrer" er sand.
                # Ellers bruger den sin egen logik, og det er meningen.
                "styrer": command.acting,
                "styr_til": command.source,
                "styring_grund": command.reason,
                "vp_varmepris": _round(decision.heat_price, 3),
                "pille_varmepris": round(decision.pellet_price, 3),
                "lad_op": decision.charge,
                "lad_kwh": _round(decision.charge_kwh, 1),
                "besparelse_kr": _round(decision.saving_kr, 2),
                "vindue_min": decision.window_minutes,
            },
        )

    # ------------------------------------------------------------- solvarme

    async def _read_solar(
        self, ha: HomeAssistant | None, buffer: Buffer | None
    ) -> dict[str, Any]:
        """Følg døgnet, lær af det når det er slut, og forudsig resten af i dag."""
        if ha is None:
            return {}

        remaining = await self._number(ha, self.options.entity_solcast_remaining)
        tomorrow = await self._number(ha, self.options.entity_solcast_tomorrow)
        today = await self._number(ha, self.options.entity_solar_today)

        now = datetime.now().astimezone()
        # Maetningen skal ses undervejs. Ved midnat er tankene koelet af, og
        # en dag hvor solen bankede mod et fuldt lager ville se normal ud.
        #
        # Og den skal maales mod det *fysiske* loft. Stod der
        # above_heatpump_ceiling, ville hver eneste god soldag blive kasseret,
        # for solen presser rutinemaessigt tankene forbi varmepumpens 60 °C -
        # og det er netop de dage der baerer information.
        full_now = buffer.at_peak_ceiling if buffer is not None else False
        finished = self.solar_day.observe(
            now.strftime("%Y-%m-%d"), now.hour, remaining, today, store_full=full_now
        )

        note = None
        if finished is not None:
            thermal, forecast, date, saturated = finished
            day_of_year = datetime.strptime(date, "%Y-%m-%d").timetuple().tm_yday
            note = self.solar.learn(thermal, forecast, day_of_year, store_was_full=saturated)
            log.info("solvarme, doegnet %s: %s", date, note)
            self._dirty = True

        day_of_year = now.timetuple().tm_yday
        expected = self.solar.expected_kwh(remaining, day_of_year)
        expected_tomorrow = self.solar.expected_kwh(tomorrow, day_of_year + 1)

        may_charge = worth_starting = None
        if expected is not None and buffer is not None:
            # Det varmepumpen kan lade uden at tage plads fra solen.
            may_charge = max(0.0, buffer.headroom_kwh - expected)
            # Men er der mindre plads end ét minimumstraek fylder, er svaret
            # "lad vaere". En start der straks foelges af et stop er slid
            # uden udbytte.
            worth_starting = may_charge >= self.options.min_charge_kwh

        return {
            "solar_today": today,
            "solar_pv_remaining": remaining,
            "solar_pv_tomorrow": tomorrow,
            "solar_expected": expected,
            "solar_expected_tomorrow": expected_tomorrow,
            "solar_may_charge": may_charge,
            "solar_worth_starting": worth_starting,
            "solar_min_charge": self.options.min_charge_kwh,
            "solar_scale": self.solar.scale,
            "solar_days": self.solar.days,
            "solar_note": note,
        }

    # -------------------------------------------------------------- balance

    async def _read_balance(
        self,
        ha: HomeAssistant | None,
        measured_cop: float | None,
        fallback_kw: float | None = None,
    ) -> Balance | None:
        """Hvad huset trækker ud, og hvad de fire kilder lader ind."""
        if ha is None:
            return None

        load = Load(
            flow=await self._number(ha, self.options.entity_flow_measured),
            ret=await self._number(ha, self.options.entity_ch_return),
            litres_per_hour=await self._number(ha, self.options.entity_ch_flow_rate),
            meter_floor=self.options.ch_flow_meter_floor,
            fallback_kw=fallback_kw,
        )

        # Varmepumpens ydelse måles nu direkte. Før blev den udledt af
        # elforbrug gange målt COP — det var rigtigt regnet, men det bandt
        # hele energiregnskabet til COP-føleren, og tanktemperaturen kunne
        # ikke træde i stedet: solvarmen lader de samme tanke, så solen ville
        # blive krediteret varmepumpen.
        hp_power = await self._power_kw(ha, self.options.entity_hp_power)
        hp_heat = await self._power_kw(ha, self.options.entity_hp_heat)
        derived = (
            hp_power * measured_cop
            if hp_power is not None and measured_cop is not None and measured_cop > 0
            else None
        )
        heatpump_kw = hp_heat if hp_heat is not None else derived
        self._hp_cop = self._check_hp_cop(hp_power, hp_heat, measured_cop)

        return Balance(
            load=load,
            hp_power_kw=hp_power,
            solar_kw=await self._power_kw(ha, self.options.entity_solar_power),
            element_kw=await self._power_kw(ha, self.options.entity_element_power),
            boiler_kw=await self._power_kw(ha, self.options.entity_boiler_power),
            heatpump_kw=heatpump_kw,
            # Koerer pumpen uden at vi kan sige hvad den laver, mangler der en
            # kilde i summen, og en energibalance bygget paa den ville
            # tilskrive huset varmen. Maales ydelsen direkte, sker det ikke
            # laengere fordi COP-foeleren tier.
            inputs_known=not (
                hp_power is not None and hp_power > 0.05 and heatpump_kw is None
            ),
        )

    def _check_hp_cop(
        self,
        power_kw: float | None,
        heat_kw: float | None,
        measured_cop: float | None,
    ) -> float | None:
        """Anlæggets to tal mod dets egen COP-føler.

        ``varmeydelse / elforbrug`` *er* COP'en. Er den påfaldende langt fra
        det føleren melder, måler føleren noget andet end vi tror — og hele
        COP-tabellen er bygget på den. Det skal siges højt én gang pr. ny
        uenighed, ikke opdages en vinter senere.
        """
        if not power_kw or power_kw <= 0.2 or heat_kw is None or heat_kw <= 0:
            return None
        implied = heat_kw / power_kw
        if measured_cop is None or measured_cop <= 0:
            return implied
        off = abs(implied - measured_cop)
        if off <= max(0.5, 0.15 * measured_cop):
            self._warned_hp_cop = False
            return implied
        if not self._warned_hp_cop:
            log.warning(
                "varmepumpens egne tal giver COP %.2f (%.2f kW varme / %.2f kW el), "
                "men %s melder %.2f - tabellen er bygget paa foeleren",
                implied,
                heat_kw,
                power_kw,
                self.options.entity_cop_measured,
                measured_cop,
            )
            self._warned_hp_cop = True
        return implied

    def _charge_minutes(self) -> tuple[float | None, float | None]:
        """Blokkens start og slut i minutter frem, til attributterne."""
        window = self._charge_window()
        return (None, None) if window is None else (window[0], window[1])

    def _charge_window(self) -> tuple[int, int] | None:
        """Blokkens start og slut som minutter frem, til plan-tabellen.

        Regnet fra **halvtimens begyndelse**, ikke fra dette sekund. Planens
        raekker er nummereret sadan: raekke 0 er den halvtime vi staar i, og
        web-siden skriver klokkeslaettet som halvtimens start. Blokken ligger
        ogsaa paa det gitter, saa de to skal maales fra det samme nulpunkt.

        Her stod ``starts - now``, og det var rigtigt saa laenge blokkens
        start selv laa paa ``now + offset``. Da starten blev lagt paa
        halvtimen, kom de to ud af trit med hvor langt vi er inde i
        halvtimen: kl. 04:56 blev en blok kl. 13:00 til 484 minutter, og 484
        rammer raekken der hedder 12:30. Maerket stod én raekke for tidligt.
        """
        slots = self.charge_plan.slots()
        if slots is None:
            return None
        base = slot_start(time.time())
        starts, ends = slots
        return int((starts - base) / 60), int((ends - base) / 60)

    def _vessel_kw(
        self, dhw: bool | None, spa: bool | None, vvb_bottom: float | None
    ) -> float | None:
        """Hvad beholderen og spaen trækker ud af tankene lige nu.

        Et skøn, ikke en måling — men uden det ville målingen af husets
        forbrug være tavs de fem timer om dagen hvor spaen kører. Beholderen
        tager mest når den er koldest, så den interpoleres mellem de to
        yderpunkter over det spænd den faktisk bevæger sig i.
        """
        total = 0.0
        if spa:
            total += self.options.spa_kw
        if dhw:
            cold, hot = self.options.vvb_kw_cold, self.options.vvb_kw_hot
            if vvb_bottom is None:
                total += (cold + hot) / 2
            else:
                # 40 °C er en toemt beholder, 55 en fuldt opvarmet. Uden for
                # spaendet klemmes der fast paa yderpunktet.
                share = min(1.0, max(0.0, (vvb_bottom - 40.0) / 15.0))
                total += cold + (hot - cold) * share
        return total if total > 0 else None

    async def _publish_house_load(self, ha: HomeAssistant) -> None:
        """Husets forbrug som lageret måler det — som sit eget tal i HA."""
        now = time.time()
        outdoor = self.status.get("outdoor_temp")
        value = self.house_load.kw_at(now, outdoor)
        if value is None:
            return

        fresh = (
            self.house_load.measured_at is not None
            and now - self.house_load.measured_at <= MAX_AGE_MINUTES * 60
        )
        await ha.set_state(
            SENSOR_HOUSE,
            round(value, 2),
            {
                "friendly_name": "Varmeopt husforbrug",
                "unit_of_measurement": "kW",
                "device_class": "power",
                "state_class": "measurement",
                "icon": "mdi:home-thermometer",
                "kilde": "maalt paa lageret" if fresh else "forbrugskurven",
                "maalt_kw": _round(self.house_load.kw, 2),
                "kurve_kw": _round(
                    self.house_load.curve.predict(outdoor)
                    if outdoor is not None
                    else None,
                    2,
                ),
                "kurvepunkter": self.house_load.curve.point_count,
                # Doegnprofilen for beholderen og spaen. Den er ikke husets
                # forbrug - den er det der skal traekkes fra for at finde det -
                # men den afgoer hvor meget der skal lades op til aftenen.
                "varmtvand_timer_laert": self.house_load.vessels.known_hours,
                "varmtvand_kwh_i_doegn": _round(
                    self.house_load.vessels.kwh_between(now, 24.0), 1
                ),
                "afvigelse_mod_maaler_kw": _round(self.house_load.bias_kw, 2),
                "note": self.house_load.note,
            },
        )

    async def _publish_demand(
        self, ha: HomeAssistant, balance: Balance, buffer: Buffer | None
    ) -> None:
        if balance.load.kw is None:
            return

        stored = buffer.stored_kwh if buffer is not None else None
        headroom = buffer.headroom_kwh if buffer is not None else None

        attributes: dict[str, Any] = {
            "friendly_name": "Varmeopt varmebehov",
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "icon": "mdi:radiator",
            "frem": balance.load.flow,
            "retur": balance.load.ret,
            "delta_t": _round(balance.load.delta, 1),
            "flow_lh": balance.load.litres_per_hour,
            "cirkulerer": balance.load.circulating,
            # Hvor tallet kom fra. Flowmaaleren maaler huset direkte; lageret
            # regner sig frem til det gennem fire kilder og et energiregnskab,
            # og det staar der saa.
            "kilde": balance.load.source,
            "lager_kw": _round(self.house_load.kw, 2),
            "lager_alder_min": _round(
                (time.time() - self.house_load.measured_at) / 60
                if self.house_load.measured_at is not None
                else None,
                0,
            ),
            "lager_afvigelse_kw": _round(self.house_load.bias_kw, 2),
            "lager_note": self.house_load.note,
            "ind_kw": round(balance.input_kw, 2),
            "gratis_kw": round(balance.free_kw, 2),
            "netto_kw": _round(balance.net_kw, 2),
            "timer_tilbage": _round(balance.hours_left(stored), 1),
            "timer_til_fuld": _round(balance.hours_to_full(headroom), 1),
        }
        for name, kilowatt in balance.sources.items():
            attributes[f"kilde_{name}"] = round(kilowatt, 2)

        await ha.set_state(SENSOR_DEMAND, round(balance.load.kw, 2), attributes)

    # ------------------------------------------------------------------- lager

    def _maybe_save(self, force: bool = False) -> None:
        if not self._dirty:
            return
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_save < SAVE_INTERVAL_SECONDS:
            return
        self.save()
        self._last_save = now

    def save(self) -> None:
        try:
            self.store.save(COP_TABLE_FILE, self.table.to_raw())
            self.store.save(CURVE_FILE, self.curve.to_raw())
            self.store.save(
                SOLAR_FILE,
                {"model": self.solar.to_raw(), "day": self.solar_day.to_raw()},
            )
            self.store.save(STANDBY_FILE, self.standby.to_raw())
            self.store.save(HOUSE_LOAD_FILE, self.house_load.to_raw())
            self.store.save(CAPACITY_FILE, self.charge_rate.to_raw())
            self.store.save(CHARGE_FILE, self.charge_plan.to_raw())
            # Vagtens binding. Den blev aldrig gemt, saa opholdstiden
            # overlevede ikke en genstart og loglinjen "vagten genoptager
            # binding" kunne aldrig udloeses.
            self.store.save(GUARD_FILE, self.guard.to_raw())
            self._dirty = False
            log.debug(
                "gemt: %d COP-celler, %d kurvepunkter",
                self.table.cell_count,
                self.curve.point_count,
            )
        except OSError as exc:
            log.error("kunne ikke gemme COP-tabellen: %s", exc)


async def run() -> None:
    options = Options.load()
    logging.basicConfig(
        level=getattr(logging, options.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Efter basicConfig, ikke før: rod-loggerens niveau filtrerer records
    # inden håndtagene ser dem, og så ville journalen kun fange advarsler.
    journal = install_journal()
    log.info("varmeopt %s starter", VERSION)

    store = Store()
    app = Varmeopt(options, store)

    if selfupdate.boot_failed():
        # Sidste opstart naaede aldrig frem. Den hentede kode faar ikke
        # lov at proeve igen.
        log.error("forrige opstart fejlede - ruller den hentede kode tilbage")
        selfupdate.rollback()
        selfupdate.clear_boot()

    async with aiohttp.ClientSession() as session:
        await _self_update_on_start(session, options)

        try:
            ha: HomeAssistant | None = HomeAssistant(session)
        except HaError as exc:
            # Uden HA er der ingen maalinger at laere af og intet at udstille.
            # Add-on'en koerer videre, saa den kan proeves lokalt.
            log.warning("kører uden Home Assistant: %s", exc)
            ha = None

        if not options.entity_outdoor_temp:
            # Uden udetemperatur kan hverken varmekurven eller COP-tabellen
            # slaa op, og hver cyklus springer over. Det skal staa i loggen
            # ved opstart og ikke opdages som en tavs raekke advarsler.
            log.error(
                "entity_outdoor_temp er ikke sat - uden udetemperatur kan der "
                "hverken laeres eller slaas op, og hver cyklus springes over"
            )

        app.table, note = load_cop_table(store)
        app.status["note"] = note
        log.info(note)

        app.curve, curve_note = load_heat_curve(store, app.table, options.dhw_setpoint)
        log.info(curve_note)

        # Azimut har to gaengse konventioner, og de er 180 grader fra
        # hinanden: kompassets med nord som 0, og den soltekniske med syd
        # som 0, som den her bruger. Solcast bruger den anden, saa tal
        # kopieret derfra peger stik modsat. Derfor staar retningerne i ord.
        geometry = options.geometry
        log.info(
            "solgeometri: solfangere %.0f grader mod %s, solceller %s",
            geometry.thermal.tilt,
            geometry.thermal.compass_name,
            ", ".join(
                f"{p.weight:.1f} kWp {p.tilt:.0f} grader mod {p.compass_name}"
                for p in geometry.pv
            ),
        )
        app.solar, app.solar_day, solar_note = load_solar(
            store, geometry, options.solar_scale
        )
        log.info(solar_note)

        app.guard.restore(store.load(GUARD_FILE, {}))
        app.standby = StandbyTest.from_raw(store.load(STANDBY_FILE, {}))
        app.house_load = HouseLoad.from_raw(store.load(HOUSE_LOAD_FILE, {}))
        app.charge_rate = ChargeRate.from_raw(
            store.load(CAPACITY_FILE, {}), options.hp_charge_kw
        )
        app.charge_plan = ChargePlan.from_raw(store.load(CHARGE_FILE, {}))
        log.info("ladehastighed: %s", app.charge_rate.note)
        if app.house_load.curve.point_count:
            log.info(
                "forbrugskurve fra eget lager: %d punkter, %.0f maalinger",
                app.house_load.curve.point_count,
                app.house_load.curve.sample_count,
            )
        if app.guard.committed:
            log.info(
                "vagten genoptager binding: %s (hviletiden fortsaetter hvor "
                "den slap - den gaelder beslutningen, ogsaa naar styringen "
                "er slaaet fra)",
                app.guard.committed,
            )

        loop = asyncio.get_running_loop()

        async def update() -> str:
            revision = await selfupdate.download(session)
            if revision is None:
                return "Kunne ikke hente koden. Se loggen for hvorfor."
            # Svar foerst, genstart bagefter - ellers dor forbindelsen
            # midt i, og brugeren ser en fejl i stedet for en kvittering.
            selfupdate.mark_boot()
            loop.call_later(1.0, selfupdate.restart)
            return f"Hentet {revision.short} - {revision.message}. Genstarter ..."

        web = WebUI(
            lambda: app.status,
            lambda: app.table,
            check=lambda: selfupdate.latest(session),
            update=update,
            curve=lambda: app.curve,
            journal=journal,
            options=options,
            standby=lambda: app.standby,
            on_standby=lambda arm: _toggle_standby(app, arm),
            house_load=lambda: app.house_load,
        )
        await web.start()
        log.info("web-UI lytter paa port %d (ingress)", web.port)
        # Naaede vi hertil, virker koden. Maerket kan ryddes.
        selfupdate.clear_boot()

        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stopping.set)

        try:
            while not stopping.is_set():
                try:
                    await app.cycle(ha)
                except Exception:
                    # En enkelt dårlig cyklus må aldrig vælte add-on'en.
                    log.exception("cyklus fejlede")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        stopping.wait(), timeout=options.cycle_seconds
                    )
        finally:
            log.info("stopper, gemmer COP-tabellen")
            app.save()
            # Giv slip foer vi doer. En HA-tilstand forsvinder ikke af sig
            # selv, saa uden det her ville Node-RED foelge en frossen
            # kommando indtil nogen opdagede det.
            if ha is not None:
                await app.release_control(ha)
            await web.stop()


async def _self_update_on_start(
    session: aiohttp.ClientSession, options: Options
) -> None:
    """Hent nyeste master ved opstart, hvis brugeren har bedt om det."""
    if not options.auto_update:
        return
    revision = await selfupdate.latest(session)
    if revision is None or not revision.sha:
        return
    if revision.sha == selfupdate.current():
        log.info("koden er nyeste paa master (%s)", revision.short)
        return
    log.info("ny kode paa master: %s - %s", revision.short, revision.message)
    if await selfupdate.download(session) is not None:
        selfupdate.mark_boot()
        selfupdate.restart()


def _mode(
    dhw: bool | None,
    spa: bool | None,
    setpoint: float | None,
    curve: HeatCurve,
    outdoor: float | None = None,
) -> tuple[str | None, bool | None]:
    """Hvad varmepumpen laver, og om det hører til i varmekurven.

    Udgangene fra anlægget er kendsgerninger, og de slår setpunktet: står
    varmtvandsudgangen tændt, *er* det varmt vand, uanset hvad setpunktet
    tilfældigvis viser. Kun når ingen af dem svarer, falder vi tilbage på at
    genkende varmtvandssetpunktet på tallet — og så siger tilstanden selv at
    den er gættet.

    Returnerer også om målingen skal holdes ude af kurven — men kun som et
    *ja*. Et nej herfra er ikke en kendsgerning på samme måde: udgangen kan
    stå på nul mens spaen varmer, og så er setpunktet stadig ikke husets.
    Derfor er svaret ``False`` og ikke ``None`` kun en oplysning om at
    flagene svarede; ``HeatCurve.learn`` afgør resten selv.
    """
    if dhw or spa:
        names = [n for n, on in (("varmt vand", dhw), ("spa", spa)) if on]
        return " + ".join(names), True
    if setpoint is None:
        return ("varme", False) if dhw is not None or spa is not None else (None, None)
    # Ordet paa skaermen skal sige det samme som kurven gjorde ved sig selv.
    if curve.is_dhw(setpoint, outdoor):
        return "varmt vand / spa (gættet)", True
    return "varme", False


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _difference(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _tank_summary(buffer: Buffer) -> str:
    """Kompakt linje til loggen: "A 58/44/31° afg 57  B 55/43/30° afg 54"."""
    parts = []
    for tank in buffer.measured:
        temps = "/".join(f"{t:.0f}" for t in tank.layers)
        outlet = f" afg {tank.outlet:.0f}" if tank.outlet is not None else ""
        parts.append(f"{tank.name} {temps}°{outlet}")
    return "  ".join(parts)


if __name__ == "__main__":
    if sys.platform == "win32":
        # Add-on'en kører på Linux, men lokal afprøvning på Windows kræver
        # SelectorEventLoop for at aiohttps DNS-resolver kan starte.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


def _toggle_standby(app: Any, arm: bool) -> str:
    """Aabn eller luk et staatabsvindue, og gem resultatet med det samme.

    Gemmes der ikke her, ville en maaling der lige er afsluttet kunne gaa
    tabt ved en genstart inden naeste automatiske gemning - og den maaling
    kostede en nat uden cirkulation.
    """
    now = time.time()
    note = app.standby.arm(now) if arm else app.standby.disarm(now)
    log.info("staatabsmaaling: %s", note)
    if not arm:
        app._dirty = True
        app.save()
    return note
