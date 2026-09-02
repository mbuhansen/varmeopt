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
from .ha import HaError, HomeAssistant, State
from .migrate import COP_TABLE_FILE, load_cop_table
from .nodered import NodeRed
from .options import Options
from .store import Store
from .tank import Buffer, Tank
from .web import WebUI

log = logging.getLogger("varmeopt")

SENSOR = "sensor.varmeopt_cop"
SENSOR_TANK = "sensor.varmeopt_lager"

# Tabellen gemmes højst så ofte, selv om der læres hvert minut. En skrivning
# pr. minut ville slide unødigt på lagringen uden at redde mere.
SAVE_INTERVAL_SECONDS = 300


class Varmeopt:
    def __init__(self, options: Options, store: Store) -> None:
        self.options = options
        self.store = store
        self.table = CopTable()
        self.status: dict[str, Any] = {"note": "starter", "lookup": None}
        self._dirty = False
        self._last_save = 0.0
        self._last_learned_stamp: str | None = None

    # ------------------------------------------------------------------ cyklus

    async def cycle(self, ha: HomeAssistant | None, nodered: NodeRed) -> None:
        flow_temp = outdoor_temp = measured_cop = measured_stamp = None

        if ha is not None:
            flow_temp = await self._number(ha, self.options.entity_flow_temp)
            outdoor_temp = await self._number(ha, self.options.entity_outdoor_temp)
            measured = await self._state(ha, self.options.entity_cop_measured)
            if measured is not None:
                measured_cop = measured.as_float()
                measured_stamp = measured.last_changed

        # Udetemperaturen kommer i dag fra MQTT direkte ind i Node-REDs flow-
        # context og findes ikke som HA-entitet. Indtil den gør, læser vi den
        # samme værdi som Node-RED selv regner på.
        if flow_temp is None or outdoor_temp is None:
            context = await nodered.flow_context()
            if outdoor_temp is None:
                outdoor_temp = _as_number(context.get("udeTemp"))
            if flow_temp is None:
                flow_temp = _as_number(context.get("flowTemp"))

        buffer = await self._read_tank(ha)

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

        self.status.update(
            flow_temp=flow_temp,
            outdoor_temp=outdoor_temp,
            measured_cop=measured_cop,
            lookup=lookup,
            tank=buffer,
            learn_note=learn_note,
            last_run=datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
        )

        if lookup is not None:
            log.info(
                "COP %.2f (%s: %s) | fremloeb %.1f, ude %.1f | laering: %s",
                lookup.cop,
                lookup.source,
                lookup.detail,
                flow_temp,
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
                "fremloeb": self.status.get("flow_temp"),
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
            self._dirty = False
            log.debug("COP-tabel gemt: %d celler", self.table.cell_count)
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
