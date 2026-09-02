"""Add-on-indstillinger.

Home Assistant skriver brugerens valg til ``/data/options.json`` efter skemaet
i ``config.yaml``. Uden for add-on'en (lokal udvikling, test) falder vi tilbage
på miljøvariabler og defaults, så koden kan køres uden en HA-instans.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OPTIONS_PATH = Path("/data/options.json")

_DEFAULTS: dict[str, object] = {
    "log_level": "info",
    "cycle_seconds": 60,
    "nodered_url": "http://192.168.1.159:1880",
    # UVR'ens beregnede setpunkt, ikke en måling: det er kurven anlægget
    # styrer efter, og den akse COP-tabellen er indekseret på.
    "entity_flow_temp": "sensor.node_1_analog_logging_13",
    # Det faktisk målte fremløb på centralvarmen. Forskellen til setpunktet
    # siger om anlægget kan følge med.
    "entity_flow_measured": "sensor.node_1_dl_bus_1",
    # Varmepumpens egne følere. BT12 er kondensatorafgangen — den fysisk
    # rigtige temperatur for COP, målt før hydraulikken blander noget — og
    # BT12 minus BT3 er løftet over kondensatoren, altså hvor hårdt den kører.
    "entity_hp_flow": "sensor.nibe_eb101_ep14_bt12_condensor_out",
    "entity_hp_return": "sensor.nibe_eb101_ep14_bt3_return_temp",
    "entity_cop_measured": "sensor.node_1_analog_logging_12",
    "entity_outdoor_temp": "",
    # Kalder varmtvandsbeholderen eller spabadet, overstyres varmekurven med
    # dette setpunkt. De målinger hører ikke til i kurven.
    "dhw_setpoint": 56,
    # Tre dybdefølere pr. tank plus én på hvert afgangsrør. Rækkefølgen top /
    # midt / bund bærer betydning: lagdelingen kan ikke regnes uden at vide
    # hvilken føler der sidder hvor.
    "entity_tank_a_top": "sensor.node_1_input_4",
    "entity_tank_a_mid": "sensor.node_1_input_5",
    "entity_tank_a_bottom": "sensor.node_1_input_6",
    "entity_tank_a_outlet": "sensor.node_1_input_9",
    "entity_tank_b_top": "sensor.my_pv_ac_thor_9s_temperature_1",
    "entity_tank_b_mid": "sensor.my_pv_ac_thor_9s_temperature_2",
    "entity_tank_b_bottom": "sensor.my_pv_ac_thor_9s_temperature_3",
    "entity_tank_b_outlet": "sensor.node_1_input_10",
    "tank_liters": 1000,
    # Under referencen er varmen ikke til nogen nytte — radiatorkredsen kører
    # på godt 31 °C fremløb. Loftet er hvad varmepumpen realistisk når.
    "tank_reference_temp": 30,
    "tank_max_temp": 60,
    # Hent nyeste kode fra master ved opstart. Slaaet fra som udgangspunkt:
    # det koerer kode fra internettet uden et menneske imellem.
    "auto_update": False,
}


def _as_bool(value: object) -> bool:
    """HA giver en rigtig bool; en miljoevariabel giver strengen "false"."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja")


@dataclass(frozen=True)
class Options:
    log_level: str
    cycle_seconds: int
    nodered_url: str
    entity_flow_temp: str
    entity_flow_measured: str
    entity_hp_flow: str
    entity_hp_return: str
    entity_cop_measured: str
    entity_outdoor_temp: str
    dhw_setpoint: float
    entity_tank_a_top: str
    entity_tank_a_mid: str
    entity_tank_a_bottom: str
    entity_tank_a_outlet: str
    entity_tank_b_top: str
    entity_tank_b_mid: str
    entity_tank_b_bottom: str
    entity_tank_b_outlet: str
    auto_update: bool
    tank_liters: int
    tank_reference_temp: float
    tank_max_temp: float

    @property
    def tanks(self) -> tuple[tuple[str, str, str, str, str], ...]:
        """Pr. tank: navn, top, midt, bund, afgang."""
        return (
            (
                "A",
                self.entity_tank_a_top,
                self.entity_tank_a_mid,
                self.entity_tank_a_bottom,
                self.entity_tank_a_outlet,
            ),
            (
                "B",
                self.entity_tank_b_top,
                self.entity_tank_b_mid,
                self.entity_tank_b_bottom,
                self.entity_tank_b_outlet,
            ),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Options:
        values = dict(_DEFAULTS)

        source = path or DEFAULT_OPTIONS_PATH
        if source.exists():
            try:
                values.update(json.loads(source.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                # En ulæselig optionsfil må ikke forhindre add-on'en i at
                # starte — så kører den bare på defaults og siger det i loggen.
                pass

        for key in _DEFAULTS:
            env = os.environ.get("VARMEOPT_" + key.upper())
            if env is not None:
                values[key] = env

        return cls(
            log_level=str(values["log_level"]),
            cycle_seconds=int(values["cycle_seconds"]),
            nodered_url=str(values["nodered_url"]).rstrip("/"),
            auto_update=_as_bool(values["auto_update"]),
            dhw_setpoint=float(values["dhw_setpoint"]),
            tank_liters=int(values["tank_liters"]),
            tank_reference_temp=float(values["tank_reference_temp"]),
            tank_max_temp=float(values["tank_max_temp"]),
            # Alle entity_*-felter er strenge, så de kan tages under ét i
            # stedet for at gentage den samme linje tolv gange.
            **{k: str(values[k]) for k in _DEFAULTS if k.startswith("entity_")},
        )
