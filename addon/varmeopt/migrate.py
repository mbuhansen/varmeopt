"""Indlæsning af det add-on'en har lært, fra sit eget lager.

COP-tabellen blev i sin tid flyttet hertil fra Node-RED. Den flytning er sket,
og lageret er nu den eneste kilde: filerne ligger på disk, kan sikkerhedskopieres,
og ingen af dem hentes andetsteds fra.
"""

from __future__ import annotations

import logging
from typing import Any

from .cop import CopTable
from .curve import CURVE_VERSION, HeatCurve
from .store import Store

log = logging.getLogger(__name__)

# Setpunkt-tabellen: sytten tusind målinger lært på UVR'ens setpunkt. Den
# læres ikke mere og skrives aldrig igen - den er fald for BT12-tabellen.
COP_TABLE_FILE = "cop_table.json"
# Tabellen på varmepumpens eget fremløb, BT12. Den læres fra 14. september.
COP_TABLE_BT12_FILE = "cop_table_bt12.json"
CURVE_FILE = "heat_curve.json"
SOLAR_FILE = "solar.json"
STANDBY_FILE = "standby.json"
HOUSE_LOAD_FILE = "house_load.json"
CAPACITY_FILE = "capacity.json"
CHARGE_FILE = "charge.json"
GUARD_FILE = "guard.json"


def load_solar(store: Store, geometry: Any, seed: float) -> tuple[Any, Any, str]:
    """Indlæs solvarmemodellen og det døgn der er i gang.

    Har den aldrig lært noget, startes den på en kalibrering fra en rigtig
    dag frem for på ingenting: 24. august 2026, hvor solcellerne lavede 60,9
    kWh mod solvarmens 29. Modellen retter selv tallet efter første hele døgn
    den selv har set.

    ``seed`` på nul betyder «regn den ud af kalibreringsdagen med den
    geometri der gælder nu». Det er det rigtige valg: et nedskrevet tal
    holder kun så længe geometrien er uændret, og den ændrede sig i 0.19.0.
    """
    from .solar import DayTracker, SolarModel, seed_scale

    raw = store.load(SOLAR_FILE, {}) if store.exists(SOLAR_FILE) else {}
    raw = raw if isinstance(raw, dict) else {}

    model = SolarModel.from_raw(raw.get("model"), geometry)
    tracker = DayTracker.from_raw(raw.get("day"))

    if model.scale is None:
        derived = seed_scale(geometry)
        if seed > 0:
            model.scale = seed
            source = "fra konfigurationen"
            if derived is not None and abs(seed - derived) > 0.02:
                source += (
                    f" - men kalibreringsdagen giver {derived:.3f} med den "
                    f"geometri der gælder nu"
                )
        else:
            model.scale = derived
            source = "regnet af kalibreringsdagen"
        note = (
            f"solvarmemodel: startværdi k={model.scale:.3f} {source}, "
            "endnu ingen egne døgn"
            if model.scale is not None
            else "solvarmemodel: intet at gå ud fra endnu"
        )
    else:
        note = f"solvarmemodel: k={model.scale:.3f} efter {model.days:.0f} døgn"

    return model, tracker, note


def load_heat_curve(
    store: Store, table: CopTable, dhw_setpoint: float
) -> tuple[HeatCurve, str]:
    """Indlæs varmekurven: fra eget lager, ellers udledt af COP-tabellen.

    Kurven behøver ikke læres forfra over uger. COP-tabellen er indekseret på
    netop det setpunkt vi vil modellere, og hver celle bærer sit antal
    målinger — så den vægtede middelværdi pr. udetemperatur *er* kurven, og
    den ligger allerede i de data tabellen bærer.
    """
    saved = store.load(CURVE_FILE, {}) if store.exists(CURVE_FILE) else None
    if isinstance(saved, dict) and saved.get("version") != CURVE_VERSION:
        # En kurve lært under version 1 er malet med en anden målestok: et
        # negativt varmtvandsflag slog værditjekket fra, så spaens 56 °C blev
        # lært som vejrkurve. Den kastes væk og udledes forfra af COP-tabellen,
        # som aldrig har haft fejlen.
        log.warning(
            "varmekurven er fra en ældre udgave med varmtvand lært ind - "
            "udleder den forfra af COP-tabellen"
        )
        saved = None

    if saved is not None:
        curve = HeatCurve.from_raw(saved, dhw_setpoint)
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


def load_cop_table(store: Store) -> tuple[CopTable, str]:
    """Indlæs BT12-tabellen med setpunkt-tabellen som fald.

    Tabellen var lært på UVR'ens setpunkt. Under en glidende opladning styres
    varmepumpens eget setpunkt, og pumpen kunne gå mod 60 °C, mens UVR'en
    viste 33 - så blev lave COP'er lært ind under et fremløb pumpen aldrig
    kørte ved. Nu læres der på BT12, i sin egen fil, og den gamle tabel
    svarer hvor den nye endnu ikke ved nok. Den gamle fil skrives aldrig
    igen, så de sytten tusind målinger kan ikke gå tabt af at aksen skiftede.

    Returnerer den nye tabel - med den gamle i ``fallback`` - og en linje der
    kan logges og vises i web-UI'et.
    """
    old: CopTable | None = None
    notes: list[str] = []
    if store.exists(COP_TABLE_FILE):
        old, dropped = CopTable.from_raw(store.load(COP_TABLE_FILE, {}))
        note = (
            f"setpunkt-tabel som fald: {old.cell_count} celler, "
            f"{old.sample_count:.0f} målinger"
        )
        if dropped:
            note += f" ({len(dropped)} kasseret)"
        notes.append(note)

    if store.exists(COP_TABLE_BT12_FILE):
        table, dropped = CopTable.from_raw(store.load(COP_TABLE_BT12_FILE, {}))
        table.fallback = old
        note = f"BT12-tabel: {table.cell_count} celler, {table.sample_count:.0f} målinger"
        if dropped:
            note += f" ({len(dropped)} kasseret)"
    else:
        table = CopTable(fallback=old)
        note = "BT12-tabel: tom - lærer forfra på varmepumpens eget fremløb"
    notes.insert(0, note)
    return table, " · ".join(notes)
