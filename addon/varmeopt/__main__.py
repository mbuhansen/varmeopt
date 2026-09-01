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

from .cop import CopTable
from .ha import HaError, HomeAssistant
from .migrate import COP_TABLE_FILE, load_cop_table
from .nodered import NodeRed
from .options import Options
from .store import Store
from .web import WebUI

log = logging.getLogger("varmeopt")

SENSOR = "sensor.varmeopt_cop"

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

    # ------------------------------------------------------------------ cyklus

    async def cycle(self, ha: HomeAssistant | None, nodered: NodeRed) -> None:
        flow_temp = outdoor_temp = measured_cop = None

        if ha is not None:
            flow_temp = await self._number(ha, self.options.entity_flow_temp)
            outdoor_temp = await self._number(ha, self.options.entity_outdoor_temp)
            measured_cop = await self._number(ha, self.options.entity_cop_measured)

        # Udetemperaturen kommer i dag fra MQTT direkte ind i Node-REDs flow-
        # context og findes ikke som HA-entitet. Indtil den gør, læser vi den
        # samme værdi som Node-RED selv regner på.
        if flow_temp is None or outdoor_temp is None:
            context = await nodered.flow_context()
            if outdoor_temp is None:
                outdoor_temp = _as_number(context.get("udeTemp"))
            if flow_temp is None:
                flow_temp = _as_number(context.get("flowTemp"))

        learn_note = "—"
        if flow_temp is not None and outdoor_temp is not None:
            if measured_cop is not None:
                learn_note = self.table.learn(flow_temp, outdoor_temp, measured_cop)
                if not learn_note.startswith("ignoreret"):
                    self._dirty = True
            lookup = self.table.lookup(flow_temp, outdoor_temp)
        else:
            lookup = None
            learn_note = "ignoreret: mangler temperaturdata"

        self.status.update(
            flow_temp=flow_temp,
            outdoor_temp=outdoor_temp,
            measured_cop=measured_cop,
            lookup=lookup,
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

    @staticmethod
    async def _number(ha: HomeAssistant, entity_id: str) -> float | None:
        if not entity_id:
            return None
        try:
            state = await ha.get_state(entity_id)
        except HaError as exc:
            log.warning("%s: %s", entity_id, exc)
            return None
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

    store = Store()
    app = Varmeopt(options, store)

    async with aiohttp.ClientSession() as session:
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

        web = WebUI(lambda: app.status, lambda: app.table)
        await web.start()
        log.info("web-UI lytter paa port %d (ingress)", web.port)

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
