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
    "entity_flow_temp": "sensor.node_1_analog_logging_13",
    "entity_cop_measured": "sensor.node_1_analog_logging_12",
    "entity_outdoor_temp": "",
}


@dataclass(frozen=True)
class Options:
    log_level: str
    cycle_seconds: int
    nodered_url: str
    entity_flow_temp: str
    entity_cop_measured: str
    entity_outdoor_temp: str

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
            entity_flow_temp=str(values["entity_flow_temp"]),
            entity_cop_measured=str(values["entity_cop_measured"]),
            entity_outdoor_temp=str(values["entity_outdoor_temp"]),
        )
