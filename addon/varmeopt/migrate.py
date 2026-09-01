"""Engangsflytning af den indlærte COP-tabel ud af Node-RED.

Node-RED har 17.000+ målinger liggende i sin globale context. Vi henter dem én
gang, renser de defekte nøgler og lægger dem i add-on'ens eget lager, hvor de
er skrevet til disk og kan sikkerhedskopieres. Node-RED røres ikke.
"""

from __future__ import annotations

import logging

from .cop import CopTable
from .nodered import NodeRed
from .store import Store

log = logging.getLogger(__name__)

COP_TABLE_FILE = "cop_table.json"


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
