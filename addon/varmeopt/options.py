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
    # Centralvarmens fremløb ud mod huset. Den sidder *efter* tankene og måler
    # altså forbrugssiden, ikke hvad varmepumpen laver — og den er derfor ikke
    # en akse COP kan indekseres på.
    #
    # Afvigelsen fra setpunktet er heller ikke en diagnose så længe
    # vejrkompenseringsventilen efter tankene er sat ud af spil: så *er*
    # fremløbet tanktoppen, og forskellen er ventilens manglende blanding,
    # ikke et anlæg der ikke kan følge med.
    "entity_flow_measured": "sensor.node_1_dl_bus_1",
    # Varmepumpens egne følere. BT12 er kondensatorafgangen — den fysisk
    # rigtige temperatur for COP, målt før hydraulikken blander noget — og
    # BT12 minus BT3 er løftet over kondensatoren, altså hvor hårdt den kører.
    "entity_hp_flow": "sensor.nibe_eb101_ep14_bt12_condensor_out",
    "entity_hp_return": "sensor.nibe_eb101_ep14_bt3_return_temp",
    # Varmepumpens elforbrug. Ganget med den målte COP giver det dens
    # varmeydelse, uafhængigt af hvad tankene i øvrigt får fra solen.
    "entity_hp_power": "sensor.node_1_input_15",
    # Husets forbrug: retur og flow hører til fremløbet ovenfor, alle tre
    # efter tankene.
    "entity_ch_return": "sensor.node_1_dl_bus_2",
    "entity_ch_flow_rate": "sensor.node_1_dl_bus_3",
    # De øvrige kilder ind i lageret. Solvarmen er gratis varme, og den skal
    # kunne skelnes fra den købte.
    #
    # Bemærk at solvarmeproduktionen er *modelleret*, ikke målt: en flowkurve
    # i UVR'en der følger pumpens PWM-signal og et analogt flow. Der er ingen
    # digital flowmåler, så tallet er mindre sikkert end husets forbrug.
    "entity_solar_power": "sensor.solvarme_produktion",
    "entity_element_power": "sensor.my_pv_ac_thor_9s_effekt",
    "entity_boiler_power": "sensor.nbe_boiler_49812_power_kw",
    # Varmtvandsbeholderen er sit eget lager ved siden af buffertankene.
    "entity_vvb_top": "sensor.node_1_input_7",
    "entity_vvb_bottom": "sensor.node_1_input_8",
    # Spabadet kalder med samme setpunkt som brugsvandet, så dets tilstand
    # forklarer hvorfor varmekurven pludselig springer til 56 °C.
    "entity_spa_temp": "sensor.tub_temperature",
    "entity_spa_target": "sensor.target_tub_temp",
    "entity_spa_heater": "binary_sensor.heater",
    # Beholderens rumfang kendes ikke. Nul betyder "regn ikke energi på den" —
    # to temperaturer er mere ærligt end en kWh-værdi bygget på et gæt.
    "vvb_liters": 0,
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
    # Solvarmen og ACthors elpatroner kan begge naa 90 grader, langt over
    # varmepumpens raekkevidde. Det er anlaeggets fysiske top, ikke
    # varmepumpens.
    "tank_peak_temp": 90,
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
    entity_hp_power: str
    entity_ch_return: str
    entity_ch_flow_rate: str
    entity_solar_power: str
    entity_element_power: str
    entity_boiler_power: str
    entity_vvb_top: str
    entity_vvb_bottom: str
    entity_spa_temp: str
    entity_spa_target: str
    entity_spa_heater: str
    vvb_liters: int
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
    tank_peak_temp: float

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
            vvb_liters=int(values["vvb_liters"]),
            tank_reference_temp=float(values["tank_reference_temp"]),
            tank_max_temp=float(values["tank_max_temp"]),
            tank_peak_temp=float(values["tank_peak_temp"]),
            # Alle entity_*-felter er strenge, så de kan tages under ét i
            # stedet for at gentage den samme linje tolv gange.
            **{k: str(values[k]) for k in _DEFAULTS if k.startswith("entity_")},
        )
