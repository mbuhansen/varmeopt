"""Udetemperaturen time for time, fra Home Assistants egen vejrudsigt.

Indtil nu har planlæggeren regnet COP fremad på den temperatur der er *nu*.
Over nogle timer flytter prisen sig langt mere end COP'en, så rangordenen
mellem timerne holdt — men de absolutte varmepriser længst ude var et gæt, og
netop for brugsvand var gættet skævt: aftenen er koldere end middagen, og det
var hele argumentet for at lade op i forvejen.

Med udsigten bliver kæden komplet, og den bruger alt hvad der er bygget:

    forudsagt temperatur  →  varmekurven  →  setpunkt  →  COP-tabellen  →  COP

Vejrudsigten kan ikke læses som en tilstand. Siden Home Assistant 2023.7
ligger den bag ``weather.get_forecasts``, som svarer på selve kaldet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Forecast:
    """Punkter af (minutter frem, grader). Tom hvis udsigten ikke kunne læses."""

    points: tuple[tuple[float, float], ...] = ()

    def __len__(self) -> int:
        return len(self.points)

    @property
    def horizon_minutes(self) -> float:
        return self.points[-1][0] if self.points else 0.0

    # ------------------------------------------------------------------ indlæs

    @classmethod
    def from_response(cls, response: Any, entity_id: str, now: datetime) -> Forecast:
        """Læs svaret fra ``weather.get_forecasts``.

        Formen er ``{"weather.x": {"forecast": [{"datetime": ..., "temperature":
        ...}]}}``. Punkter der ligger bag os springes over — en udsigt der
        begynder i går siger intet om i aften.
        """
        block = response.get(entity_id) if isinstance(response, dict) else None
        if not isinstance(block, dict):
            # Nogle udgaver svarer uden at gentage entitets-id'et.
            block = response if isinstance(response, dict) else {}
        rows = block.get("forecast")
        if not isinstance(rows, list):
            return cls()

        points: list[tuple[float, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            stamp = _parse_time(row.get("datetime"))
            temp = _number(row.get("temperature"))
            if stamp is None or temp is None:
                continue
            minutes = (stamp - now).total_seconds() / 60
            if minutes < -60:
                continue
            points.append((minutes, temp))

        # Sorter paa den *rigtige* tid, ikke paa den afkortede. Foer laa
        # klampningen foer sorteringen, saa alle fortidige punkter fik
        # minuttal 0,0 og blev raekkefoelgebestemt af deres temperatur.
        # Harmloest ved timeoploesning, hvor der hoejst er ét; forkert ved
        # kvarter, hvor fire punkter byttede plads efter hvor varmt der var.
        points.sort(key=lambda point: point[0])
        points = [(max(0.0, minutes), temp) for minutes, temp in points]
        return cls(tuple(points))

    # ------------------------------------------------------------------ opslag

    def temperature_at(self, minutes_ahead: float) -> float | None:
        """Temperaturen så mange minutter frem, interpoleret mellem punkterne.

        Uden for udsigtens spænd klemmes der fast på nærmeste punkt. At
        forlænge en temperaturkurve lineært ud i det blå ville finde på tal
        som ingen har lovet os.
        """
        if not self.points or not _finite(minutes_ahead):
            return None

        if minutes_ahead <= self.points[0][0]:
            return self.points[0][1]
        if minutes_ahead >= self.points[-1][0]:
            return self.points[-1][1]

        for (m0, t0), (m1, t1) in zip(self.points, self.points[1:]):
            if m0 <= minutes_ahead <= m1:
                if m1 == m0:
                    return t0
                ratio = (minutes_ahead - m0) / (m1 - m0)
                return t0 + (t1 - t0) * ratio
        return None

    def to_raw(self) -> list[dict[str, float]]:
        return [{"minutter": round(m), "grader": t} for m, t in self.points]


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # HA melder ISO 8601 med tidszone; ældre udgaver bruger "Z".
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
