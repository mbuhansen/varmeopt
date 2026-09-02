"""Engangsflytning af den indlærte COP-tabel ud af Node-RED.

Node-RED har 17.000+ målinger liggende i sin globale context. Vi henter dem én
gang, renser de defekte nøgler og lægger dem i add-on'ens eget lager, hvor de
er skrevet til disk og kan sikkerhedskopieres. Node-RED røres ikke.
"""

from __future__ import annotations

import logging

from .cop import CopTable
from .curve import HeatCurve
from .nodered import NodeRed
from .store import Store

log = logging.getLogger(__name__)

COP_TABLE_FILE = "cop_table.json"
CURVE_FILE = "heat_curve.json"


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
