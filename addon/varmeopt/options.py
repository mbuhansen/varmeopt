"""Add-on-indstillinger.

Home Assistant skriver brugerens valg til ``/data/options.json`` efter skemaet
i ``config.yaml``. Uden for add-on'en (lokal udvikling, test) falder vi tilbage
på miljøvariabler og defaults, så koden kan køres uden en HA-instans.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

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
    # Flowmaalerens bund. Under den kan den vise nul selv om der loeber
    # vand, saa en aflaesning derunder siger "hoejst saa meget" og ikke
    # "saa meget" - og behovet er dermed ukendt, ikke nul.
    "ch_flow_meter_floor": 100.0,
    # Rummet hvor tankene staar. Staatabet foelger forskellen til det her,
    # ikke til en antaget kaeldertemperatur.
    "entity_room_temp": "sensor.sadelrum_temperature",
    # De øvrige kilder ind i lageret. Solvarmen er gratis varme, og den skal
    # kunne skelnes fra den købte.
    #
    # Bemærk at solvarmeproduktionen er *modelleret*, ikke målt: en flowkurve
    # i UVR'en der følger pumpens PWM-signal og et analogt flow. Der er ingen
    # digital flowmåler, så tallet er mindre sikkert end husets forbrug.
    "entity_solar_power": "sensor.solvarme_produktion",
    "entity_element_power": "sensor.my_pv_ac_thor_9s_effekt",
    "entity_boiler_power": "sensor.nbe_boiler_49812_power_kw",
    # Predbats plan, laest direkte fra HA. Vi bruger raw.rows, den
    # strukturerede udgave - ikke HTML-tabellen, som ville vaere skroebelig.
    # Node-REDs egen beslutning, saa de to kan sammenlignes. Sensoren
    # hedder "Varme Styring" i Node-REDs ha-entity-config.
    # Home Assistants egen vejrudsigt. Med den faar hver time i planen sin
    # egen COP i stedet for at arve den vi har nu.
    "entity_weather": "weather.hjem",
    "forecast_refresh_minutes": 30,
    "entity_nodered_decision": "sensor.varme_styring",
    "entity_predbat_plan": "predbat.plan_html",
    # Predbats nuvaerende tilstand. Planens raekker bruger samme ordforraad
    # pr. halvtime, saa den her er den eneste maade at se hvad anlaeggets
    # egen Predbat faktisk skriver - uden at gaette paa dokumentationen.
    "entity_predbat_status": "predbat.status",
    # Er planen aeldre end det her, er priserne fra et andet tidspunkt.
    # Saa hellere ingen plan end en gammel: uden plan er der ingen pris,
    # og uden pris naegter vagten at styre.
    "plan_max_age_minutes": 60,
    "entity_battery_power": "sensor.hostname_scb_5313dd_battery_power",
    "entity_grid_power": "sensor.hostname_scb_5313dd_grid_power",
    # Doegntaeller for solvarmen, og Solcasts prognose for solcellerne. De to
    # kalibrerer hinanden: solfangerne og cellerne ser samme sol.
    "entity_solar_today": "sensor.solvarme_produktion_idag",
    "entity_solcast_remaining": "sensor.solcast_pv_forecast_forecast_remaining_today",
    "entity_solcast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
    # Anlaeggets geometri. Solfangerne staar stejlere end cellerne, og
    # forholdet mellem dem svinger derfor med en faktor 2,5 hen over aaret -
    # det regnes, det laeres ikke.
    "latitude": 55.4,
    "solar_thermal_tilt": 45,
    "solar_thermal_azimuth": 0,
    "pv_a_kwp": 6.4,
    "pv_a_tilt": 20,
    "pv_a_azimuth": 0,
    "pv_b_kwp": 4.0,
    "pv_b_tilt": 15,
    "pv_b_azimuth": 90,
    # Startvaerdi for skalafaktoren, kalibreret paa 24. august 2026: PV 60,9
    # kWh mod 29 kWh solvarme. Modellen retter den selv naar den har set et
    # helt doegn.
    # Nul betyder "regn startvaerdien ud af kalibreringsdagen med den
    # geometri der gaelder nu". Et fast tal her holder kun saa laenge
    # geometrien er uaendret - se solar.seed_scale.
    "solar_scale": 0,
    # UVR'en har en minimums gangtid paa varmepumpen. Er der mindre plads end
    # ét saadant traek fylder, er svaret "lad vaere" - ikke "lad lidt".
    # Kortcykling slider og koster virkningsgrad ved hver opstart.
    "hp_min_runtime_minutes": 15,
    "hp_charge_kw": 16.0,
    # At koere varmepumpen koster noget ud over stroemmen. Tallet kommer
    # fra den ukoblede v4-node i Node-RED, hvor det var en konstant - her
    # er det en indstilling, saa det kan efterproeves mod virkeligheden.
    "hp_wear_kr_per_kwh": 0.15,
    # Hvor langt frem det giver mening at gemme varme. Ud over det aeder
    # staatabet gevinsten, og prisprognosen bliver for usikker.
    "planner_horizon_hours": 12,
    # Varmtvandsbeholderen er sit eget lager ved siden af buffertankene.
    "entity_vvb_top": "sensor.node_1_input_7",
    "entity_vvb_bottom": "sensor.node_1_input_8",
    # Taender varmepumpen for brugsvandet? En kendsgerning, i modsaetning
    # til at gaette det ud af at setpunktet ligger paa 56. Sammen med
    # spaets egen kan de to ogsaa skelnes fra hinanden.
    "entity_dhw_active": "binary_sensor.node_1_output_7",
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
    # Afspaerringsventilen til tank 2 aabner naar tank 1 er over det her paa
    # topfoeleren. Indtil da er tankene *ment* at staa skaevt: solvarmen lader
    # fra bunden af tank 1, og ved kun at varme de foerste 500 L naar lageret
    # hurtigere en brugbar temperatur. Nul slaar kaskaden fra.
    "tank_cascade_temp": 55,
    # Hent nyeste kode fra master ved opstart. Slaaet fra som udgangspunkt:
    # det koerer kode fra internettet uden et menneske imellem.
    # Pillefyret. Braendvaerdi og virkningsgrad som Node-RED regner med.
    "pellet_price_per_kg": 2.88,
    "pellet_kwh_per_kg": 4.8,
    "pellet_efficiency": 0.85,
    # Hysterese paa kildevalget, saa den ikke vipper frem og tilbage paa
    # nogle oerer. Samme vaerdi som Node-RED bruger.
    "source_hysteresis": 0.05,
    # Styring. Slaaet fra: add-on'en udstiller sin beslutning og et flag,
    # og Node-RED foelger den kun naar flaget siger ja. Saa er der ét sted
    # der styrer, og det sted kan altid sige nej til os.
    "control_enabled": False,
    # Mindste tid en kilde skal staa, foer den maa skifte igen.
    "control_min_dwell_minutes": 15,
    # Efter opstart: lad tabellerne komme paa plads foer der styres.
    "control_warmup_minutes": 5,
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
    ch_flow_meter_floor: float
    entity_room_temp: str
    entity_solar_power: str
    entity_element_power: str
    entity_boiler_power: str
    entity_weather: str
    forecast_refresh_minutes: float
    entity_nodered_decision: str
    entity_predbat_plan: str
    entity_predbat_status: str
    plan_max_age_minutes: float
    entity_battery_power: str
    entity_grid_power: str
    entity_solar_today: str
    entity_solcast_remaining: str
    entity_solcast_tomorrow: str
    latitude: float
    solar_thermal_tilt: float
    solar_thermal_azimuth: float
    pv_a_kwp: float
    pv_a_tilt: float
    pv_a_azimuth: float
    pv_b_kwp: float
    pv_b_tilt: float
    pv_b_azimuth: float
    solar_scale: float
    hp_min_runtime_minutes: float
    hp_charge_kw: float
    hp_wear_kr_per_kwh: float
    planner_horizon_hours: float
    pellet_price_per_kg: float
    pellet_kwh_per_kg: float
    pellet_efficiency: float
    source_hysteresis: float

    @property
    def pellet_kwh_price(self) -> float:
        """Pillevarme i kr pr. kWh leveret varme.

        Ved en umulig konfiguration falder vi tilbage paa standardvaerdierne
        og siger det hoejt. Der stod 0,0, og gratis pillevarme vinder hver
        eneste sammenligning: anlaegget ville staa og fyre med traepiller
        doegnet rundt, tavst, fordi nogen havde skrevet et nul i et felt.
        """
        if self.pellet_kwh_per_kg <= 0 or self.pellet_efficiency <= 0:
            log.error(
                "brændværdi %.3g kWh/kg og virkningsgrad %.3g giver ingen "
                "meningsfuld pillepris - bruger standardværdierne i stedet",
                self.pellet_kwh_per_kg,
                self.pellet_efficiency,
            )
            return (
                float(_DEFAULTS["pellet_price_per_kg"])
                / float(_DEFAULTS["pellet_kwh_per_kg"])
                / float(_DEFAULTS["pellet_efficiency"])
            )
        return (self.pellet_price_per_kg / self.pellet_kwh_per_kg) / self.pellet_efficiency

    @property
    def min_charge_kwh(self) -> float:
        """Mindste opladning der er værd at starte for.

        Ét minimumstræk. Er der mindre plads end det, fylder varmepumpen det
        og slukker igen — og en start der straks efterfølges af et stop er
        slid uden udbytte.
        """
        return self.hp_charge_kw * self.hp_min_runtime_minutes / 60
    entity_vvb_top: str
    entity_vvb_bottom: str
    entity_dhw_active: str
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
    control_enabled: bool
    control_min_dwell_minutes: float
    control_warmup_minutes: float
    tank_liters: int
    tank_reference_temp: float
    tank_max_temp: float
    tank_peak_temp: float
    tank_cascade_temp: float

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

    @property
    def geometry(self) -> Any:
        """Solfangerens flade mod solcellernes, som ``solar.Geometry``."""
        from .solar import Geometry, Plane

        return Geometry(
            latitude=self.latitude,
            thermal=Plane(self.solar_thermal_tilt, self.solar_thermal_azimuth),
            pv=(
                Plane(self.pv_a_tilt, self.pv_a_azimuth, self.pv_a_kwp),
                Plane(self.pv_b_tilt, self.pv_b_azimuth, self.pv_b_kwp),
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
            control_enabled=_as_bool(values["control_enabled"]),
            dhw_setpoint=float(values["dhw_setpoint"]),
            tank_liters=int(values["tank_liters"]),
            vvb_liters=int(values["vvb_liters"]),
            tank_reference_temp=float(values["tank_reference_temp"]),
            tank_max_temp=float(values["tank_max_temp"]),
            tank_peak_temp=float(values["tank_peak_temp"]),
            tank_cascade_temp=float(values["tank_cascade_temp"]),
            # Geometri og skalafaktor er alle tal, saa de kan tages under ét.
            # Solvarmens *entiteter* hedder entity_solar_* og fanges af
            # entity-linjen nedenfor, ikke af denne.
            **{
                key: float(values[key])
                for key in _DEFAULTS
                if key == "latitude"
                or key.startswith(
                    ("solar_", "pv_", "hp_", "pellet_", "source_", "planner_")
                )
                or key in (
                    "control_min_dwell_minutes",
                    "control_warmup_minutes",
                    "forecast_refresh_minutes",
                    "plan_max_age_minutes",
                    "ch_flow_meter_floor",
                )
            },
            # Alle entity_*-felter er strenge, så de kan tages under ét i
            # stedet for at gentage den samme linje tolv gange.
            **{k: str(values[k]) for k in _DEFAULTS if k.startswith("entity_")},
        )
