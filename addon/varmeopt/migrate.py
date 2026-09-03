"""Engangsflytning af den indlærte COP-tabel ud af Node-RED.

Node-RED har 17.000+ målinger liggende i sin globale context. Vi henter dem én
gang, renser de defekte nøgler og lægger dem i add-on'ens eget lager, hvor de
er skrevet til disk og kan sikkerhedskopieres. Node-RED røres ikke.
"""

from __future__ import annotations

import logging
from typing import Any

from .cop import CopTable
from .curve import HeatCurve
from .nodered import NodeRed
from .store import Store

log = logging.getLogger(__name__)

COP_TABLE_FILE = "cop_table.json"
CURVE_FILE = "heat_curve.json"
SOLAR_FILE = "solar.json"
STANDBY_FILE = "standby.json"
COMPARE_FILE = "compare.json"
GUARD_FILE = "guard.json"


def load_solar(store: Store, geometry: Any, seed: float) -> tuple[Any, Any, str]:
    """Indlæs solvarmemodellen og det døgn der er i gang.

    Har den aldrig lært noget, startes den på en kalibrering fra en rigtig
    dag frem for på ingenting: 24. august 2026, hvor solcellerne lavede 60,9
    kWh mod solvarmens 29. Modellen retter selv tallet efter første hele døgn
    den selv har set.
    """
    from .solar import DayTracker, SolarModel

    raw = store.load(SOLAR_FILE, {}) if store.exists(SOLAR_FILE) else {}
    raw = raw if isinstance(raw, dict) else {}

    model = SolarModel.from_raw(raw.get("model"), geometry)
    tracker = DayTracker.from_raw(raw.get("day"))

    if model.scale is None:
        model.scale = seed if seed > 0 else None
        note = (
            f"solvarmemodel: startvaerdi k={seed:.3f}, endnu ingen egne doegn"
            if model.scale is not None
            else "solvarmemodel: intet at gaa ud fra endnu"
        )
    else:
        note = f"solvarmemodel: k={model.scale:.3f} efter {model.days:.0f} doegn"

    return model, tracker, note


def load_heat_curve(
    store: Store, table: CopTable, dhw_setpoint: float
) -> tuple[HeatCurve, str]:
    """Indlæs varmekurven: fra eget lager, ellers udledt af COP-tabellen.

    Kurven behøver ikke læres forfra over uger. COP-tabellen er indekseret på
    netop det setpunkt vi vil modellere, og hver celle bærer sit antal
    målinger — så den vægtede middelværdi pr. udetemperatur *er* kurven, og
    den ligger allerede i de data der blev migreret fra Node-RED.
    """
    if store.exists(CURVE_FILE):
        curve = HeatCurve.from_raw(store.load(CURVE_FILE, {}), dhw_setpoint)
        return curve, (
            f"varmekurve fra eget lager: {curve.point_count} punkter, "
            f"{curve.sample_count:.0f} målinger"
        )

    curve = HeatCurve.from_cop_table(table, dhw_setpoint)
    if not curve.point_count:
        return curve, "ingen varmekurve endnu - lærer den fra nu af"

    store.save(CURVE_FILE, curve.to_raw())
    temps = curve.outdoor_temps
    return curve, (
        f"varmekurve udledt af COP-tabellen: {curve.point_count} punkter fra "
        f"{temps[0]} til {temps[-1]} °C ude, {curve.sample_count:.0f} målinger bag"
    )


async def load_cop_table(store: Store, nodered: NodeRed) -> tuple[CopTable, str]:
    """Indlæs COP-tabellen: fra eget lager, ellers migrér fra Node-RED.

    Returnerer tabellen og en linje der kan logges og vises i web-UI'et.
    """
    if store.exists(COP_TABLE_FILE):
        table, dropped = CopTable.from_raw(store.load(COP_TABLE_FILE, {}))
        note = f"indlæst fra eget lager: {table.cell_count} celler, {table.sample_count:.0f} målinger"
        if dropped:
            note += f" ({len(dropped)} kasseret)"
        return table, note

    context = await nodered.global_context()
    raw = context.get("copTable")
    if raw is None:
        return CopTable(), "ingen tabel fundet i Node-RED - starter tom og lærer forfra"

    table, dropped = CopTable.from_raw(raw)
    store.save(COP_TABLE_FILE, table.to_raw())

    note = (
        f"migreret fra Node-RED: {table.cell_count} celler, "
        f"{table.sample_count:.0f} målinger, fremløb "
        f"{min(table.flow_temps)}-{max(table.flow_temps)} °C"
        if table.flow_temps
        else "migreret fra Node-RED: tom tabel"
    )
    if dropped:
        note += f". Kasseret {len(dropped)}: {', '.join(dropped[:5])}"
    log.info(note)
    return table, note
