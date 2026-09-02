"""Varmeopt — hovedløkke.

Fase 0: add-on'en overtager COP-læringen og gør den efterprøvelig. Den læser
de samme temperaturer som Node-RED regner på, lærer i sin egen tabel, slår op
med den rettede interpolation og udstiller resultatet. **Der styres intet.**
Node-RED bliver ved med at træffe alle beslutninger indtil fase 4.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Any

import aiohttp

from . import VERSION, selfupdate
from .cop import CopTable
from .curve import HeatCurve
from .demand import Balance, Load
from .ha import HaError, HomeAssistant, State
from .migrate import (
    COP_TABLE_FILE,
    CURVE_FILE,
    SOLAR_FILE,
    load_cop_table,
    load_heat_curve,
    load_solar,
)
from .nodered import NodeRed
from .options import Options
from .prices import Grid, Plan
from .solar import DayTracker, SolarModel
from .store import Store
from .tank import Buffer, Tank
from .web import WebUI

log = logging.getLogger("varmeopt")

SENSOR = "sensor.varmeopt_cop"
SENSOR_TANK = "sensor.varmeopt_lager"
SENSOR_DEMAND = "sensor.varmeopt_behov"
SENSOR_PRICE = "sensor.varmeopt_elpris"

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
        self.status: dict[str, Any] = {"note": "starter", "lookup": None}
        self._dirty = False
        self._last_save = 0.0
        self._last_learned_stamp: str | None = None

    # ------------------------------------------------------------------ cyklus

    async def cycle(self, ha: HomeAssistant | None, nodered: NodeRed) -> None:
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

        # Flow-contexten hentes hver cyklus. Udetemperaturen kommer fra MQTT
        # direkte ind i Node-RED og findes ikke som HA-entitet, og batteriets
        # gennemsnitspris er regnet af Node-RED — begge dele skal vi bruge.
        context = await nodered.flow_context()
        if outdoor_temp is None:
            outdoor_temp = _as_number(context.get("udeTemp"))
        if flow_temp is None:
            flow_temp = _as_number(context.get("flowTemp"))

        buffer = await self._read_tank(ha)
        balance = await self._read_balance(ha, measured_cop)
        vessels = await self._read_vessels(ha)
        solar = await self._read_solar(ha, buffer)

        # Kalder varmtvandsbeholderen eller spabadet, overstyres varmekurven
        # med et fast setpunkt. Den slags målinger siger intet om kurven, og
        # de skal heller ikke forveksles med varmedrift senere.
        mode = curve_note = None
        if flow_temp is not None:
            mode = "varmt vand / spa" if self.curve.is_dhw(flow_temp) else "varme"
            if outdoor_temp is not None:
                curve_note = self.curve.learn(outdoor_temp, flow_temp)
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

        prices = await self._read_prices(ha, context, lookup)

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
            flow_measured=flow_measured,
            hp_flow=hp_flow,
            hp_return=hp_return,
            # Løftet over kondensatoren. Et løft nær nul betyder at pumpen
            # ikke laver noget, uanset hvad COP-føleren måtte påstå.
            hp_lift=_difference(hp_flow, hp_return),
            mode=mode,
            curve_note=curve_note,
            predicted_setpoint=(
                self.curve.predict(outdoor_temp) if outdoor_temp is not None else None
            ),
            learn_note=learn_note,
            last_run=datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
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
                await self._publish(ha, lookup)
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
            await self._publish_tank(ha, buffer)

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
            await self._publish_demand(ha, balance, buffer)

        if prices.get("price_now") is not None and ha is not None:
            price = prices["price_now"]
            heat = prices.get("heat_price")
            log.info(
                "el %.2f kr/kWh (%s) | varme %s mod pille %.2f -> %s",
                price.kr_per_kwh,
                price.reason,
                f"{heat:.2f}" if heat is not None else "-",
                prices["pellet_price"],
                prices.get("decision") or "-",
            )
            await self._publish_price(ha, prices)

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
        tanks = [
            Tank(
                name=name,
                liters=share,
                top=await self._number(ha, top),
                mid=await self._number(ha, mid),
                bottom=await self._number(ha, bottom),
                outlet=await self._number(ha, outlet),
            )
            for name, top, mid, bottom, outlet in self.options.tanks
        ]
        buffer = Buffer(
            tuple(tanks),
            self.options.tank_reference_temp,
            self.options.tank_max_temp,
            self.options.tank_peak_temp,
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

    # ---------------------------------------------------------------- pris

    async def _read_prices(
        self, ha: HomeAssistant | None, context: dict[str, Any], lookup: Any
    ) -> dict[str, Any]:
        """Marginalprisen nu og fremad, og hvad varmen dermed koster.

        Det er her add-on'en for foerste gang regner den *samme* beslutning som
        Node-RED - men paa den rettede COP. Den styrer stadig intet; forskellen
        mellem de to svar er praecis det fase 1 skal vurderes paa.
        """
        if ha is None:
            return {}

        state = await self._state(ha, self.options.entity_predbat_plan)
        if state is None:
            return {}

        battery_average = _as_number(context.get("battery_avg_price")) or 0.0
        plan = Plan.from_predbat(state.attributes, battery_average=battery_average)
        if not len(plan):
            log.warning(
                "kunne ikke laese Predbats plan fra %s", self.options.entity_predbat_plan
            )
            return {}

        grid = Grid(
            battery_power=await self._number(ha, self.options.entity_battery_power) or 0.0,
            grid_power=await self._number(ha, self.options.entity_grid_power) or 0.0,
        )
        now = plan.marginal(0, grid=grid)
        if now is None:
            return {}

        pellet = self.options.pellet_kwh_price
        heat_price = decision = None
        if lookup is not None and lookup.cop > 0:
            heat_price = now.kr_per_kwh / lookup.cop
            gap = heat_price - pellet
            hysteresis = self.options.source_hysteresis
            # Ved uafgjort vinder varmepumpen, som Node-RED ogsaa goer.
            decision = "pillefyr" if gap > hysteresis else "varmepumpe"

        return {
            "plan": plan,
            "price_now": now,
            "grid": grid,
            "heat_price": heat_price,
            "pellet_price": pellet,
            "decision": decision,
        }

    async def _publish_price(self, ha: HomeAssistant, prices: dict[str, Any]) -> None:
        price = prices["price_now"]
        plan: Plan = prices["plan"]

        attributes: dict[str, Any] = {
            "friendly_name": "Varmeopt elpris",
            "unit_of_measurement": "kr/kWh",
            "state_class": "measurement",
            "icon": "mdi:cash-clock",
            "begrundelse": price.reason,
            "vp_varmepris": _round(prices.get("heat_price"), 3),
            "pille_varmepris": round(prices["pellet_price"], 3),
            "beslutning": prices.get("decision"),
            "horisont_timer": round(plan.horizon_minutes / 60, 1),
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
        full_now = buffer.above_heatpump_ceiling if buffer is not None else False
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
        self, ha: HomeAssistant | None, measured_cop: float | None
    ) -> Balance | None:
        """Hvad huset trækker ud, og hvad de fire kilder lader ind."""
        if ha is None:
            return None

        load = Load(
            flow=await self._number(ha, self.options.entity_flow_measured),
            ret=await self._number(ha, self.options.entity_ch_return),
            litres_per_hour=await self._number(ha, self.options.entity_ch_flow_rate),
        )

        # Varmepumpens ydelse regnes af dens elforbrug og den målte COP.
        # Tanktemperaturen kan ikke bruges: solvarmen lader de samme tanke, og
        # så ville solen blive krediteret varmepumpen.
        hp_power = await self._power_kw(ha, self.options.entity_hp_power)
        heatpump_kw = (
            hp_power * measured_cop
            if hp_power is not None and measured_cop is not None and measured_cop > 0
            else None
        )

        return Balance(
            load=load,
            solar_kw=await self._power_kw(ha, self.options.entity_solar_power),
            element_kw=await self._power_kw(ha, self.options.entity_element_power),
            boiler_kw=await self._power_kw(ha, self.options.entity_boiler_power),
            heatpump_kw=heatpump_kw,
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
        nodered = NodeRed(session, options.nodered_url)

        try:
            ha: HomeAssistant | None = HomeAssistant(session)
        except HaError as exc:
            # Uden HA kan vi stadig læse Node-RED og lære videre — vi kan bare
            # ikke udstille noget. Det gør lokal afprøvning mulig.
            log.warning("kører uden Home Assistant: %s", exc)
            ha = None

        app.table, note = await load_cop_table(store, nodered)
        app.status["note"] = note
        log.info(note)

        app.curve, curve_note = load_heat_curve(store, app.table, options.dhw_setpoint)
        log.info(curve_note)

        app.solar, app.solar_day, solar_note = load_solar(
            store, options.geometry, options.solar_scale
        )
        log.info(solar_note)

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
                    await app.cycle(ha, nodered)
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


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


if __name__ == "__main__":
    if sys.platform == "win32":
        # Add-on'en kører på Linux, men lokal afprøvning på Windows kræver
        # SelectorEventLoop for at aiohttps DNS-resolver kan starte.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
