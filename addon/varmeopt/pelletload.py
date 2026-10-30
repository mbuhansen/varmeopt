"""Pillefyrets varme, regnet om til den strøm varmepumpen ville have brugt.

Predbat lærer husets forbrug af elmåleren. En dag hvor varmeopt vælger
pillefyret, ligner huset derfor et hus der brugte mindre strøm - 29. oktober
2026 omkring 7 kWh - og næste dags plan lader batteriet for lidt op. Når
varmepumpen så kører igen, slipper batteriet op i de dyre timer.

Svaret er ikke at styre Predbat, men at rette det den lærer af. Denne tæller
står stille når pillefyret står stille, og tæller op mens det brænder, med
den strøm pumpen ville have brugt på samme varme. Den lægges *ved siden af*
husets måler i Predbats ``load_today``, og så ser historikken ens ud uanset
hvilken kilde der kørte.

**Mængden regnes fra kg piller** (brugerens valg 30. oktober). Fyrets
``power_kw`` er ingen måling, kun fyrets egen beregning ud fra belastningen;
sneglen vejer. Kilden er fyrets døgntal, der nulstilles ved midnat.

**COP'en er rumvarmens opslag nu** - det pumpen ville have kørt med. Dækker
fyret varmt vand, ville pumpen have kørt ved 56 grader med lavere COP, og
tallet bliver lidt for lavt. Det er accepteret.

**Huller og fald tælles ikke.** Står add-on'en stille, eller falder døgntallet
(midnat, eller en føler der hikker), bliver den nye værdi udgangspunkt uden at
noget lægges til. Det koster højst det fyret nåede i ét minut efter midnat; et
fald talt som forbrug kunne derimod lægge en hel dags piller til to gange.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Samme regel som ``usage``: et hul betyder at vi ikke ved hvornår pillerne
# blev brændt, og en klump på én cyklus ville give Predbat et spring.
MAX_GAP_SECONDS = 300.0

# Fyret bruger højst omkring 2,8 kg i timen. Mere end det på én cyklus er en
# føler der har sagt noget forkert, ikke et forbrug.
MAX_KG_STEP = 3.0

# Effekten er det der blev talt over det seneste kvarter. Døgntallet kommer i
# spring på omkring fem minutter, og uden et vindue ville effekten skifte
# mellem nul og det tredobbelte.
POWER_WINDOW_SECONDS = 900.0

# I dag og i går.
KEEP_DAYS = 2


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _date(at: float) -> str | None:
    try:
        return datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


@dataclass
class PelletDay:
    date: str
    kg: float = 0.0
    kwh: float = 0.0


class PelletLoad:
    """Tælleren. ``total_kwh`` falder aldrig - det er den Predbat læser."""

    def __init__(
        self,
        total_kwh: float = 0.0,
        days: list[PelletDay] | None = None,
        cop: float | None = None,
    ) -> None:
        self.total_kwh = max(0.0, total_kwh)
        self._days: list[PelletDay] = list(days or [])
        # Sidste gode COP. Den overlever en genstart, så et minut uden
        # temperaturdata ikke taber en pilleperiode.
        self.cop = cop
        # Kun i hukommelsen: efter en genstart ved vi ikke hvad der skete
        # imens, og første aflæsning er udgangspunkt.
        self._last_kg: float | None = None
        self._last_at: float | None = None
        self._window: deque[tuple[float, float]] = deque()

    # ------------------------------------------------------------- opslag

    @property
    def today(self) -> PelletDay | None:
        return self._days[-1] if self._days else None

    @property
    def yesterday(self) -> PelletDay | None:
        """Døgnet før det nyeste - kun når det *er* dagen før."""
        if len(self._days) < 2:
            return None
        before = self._days[-2]
        return before if _day_after(before.date) == self._days[-1].date else None

    def power_w(self, now: float) -> float:
        """Strømmen pumpen ville have brugt, over det seneste kvarter."""
        self._trim(now)
        kwh = sum(k for _, k in self._window)
        return kwh / (POWER_WINDOW_SECONDS / 3600) * 1000

    # -------------------------------------------------------------- måling

    def observe(
        self,
        now: float,
        kg_today: float | None,
        cop: float | None,
        kwh_per_kg: float,
        efficiency: float,
    ) -> float:
        """Ét skridt. Giver de kWh der blev lagt til."""
        date = _date(now)
        if date is None:
            return 0.0
        day = self._day_for(date)

        if _finite(cop) and cop >= 1.0:
            self.cop = float(cop)

        if not _finite(kg_today) or kg_today < 0:
            # Ved ikke. Udgangspunktet bliver stående til føleren svarer.
            return 0.0

        gap = now - self._last_at if self._last_at is not None else None
        last = self._last_kg
        self._last_kg = float(kg_today)
        self._last_at = now
        if last is None or gap is None or gap <= 0 or gap > MAX_GAP_SECONDS:
            return 0.0

        step = kg_today - last
        if step <= 0:
            # Uændret, eller faldet: midnat eller en hikke. Ny værdi er
            # udgangspunkt - se modulets docstring.
            return 0.0
        if step > MAX_KG_STEP:
            log.warning(
                "pillefyr: %.2f kg på én cyklus (%.2f -> %.2f) ignoreret",
                step,
                last,
                kg_today,
            )
            return 0.0

        day.kg += step
        if self.cop is None:
            return 0.0
        kwh = step * kwh_per_kg * efficiency / self.cop
        day.kwh += kwh
        self.total_kwh += kwh
        self._window.append((now, kwh))
        self._trim(now)
        return kwh

    def _trim(self, now: float) -> None:
        while self._window and now - self._window[0][0] > POWER_WINDOW_SECONDS:
            self._window.popleft()

    def _day_for(self, date: str) -> PelletDay:
        if self._days and self._days[-1].date == date:
            return self._days[-1]
        self._days.append(PelletDay(date=date))
        del self._days[:-KEEP_DAYS]
        return self._days[-1]

    # --------------------------------------------------------------- lager

    def to_raw(self) -> dict[str, Any]:
        return {
            # Uafrundet: udgivet med tre decimaler og gemt med fire kunne
            # en genstart få den udgivne tæller til at falde en tusindedel.
            "total_kwh": self.total_kwh,
            "cop": self.cop,
            "days": [
                {"date": d.date, "kg": round(d.kg, 3), "kwh": round(d.kwh, 4)}
                for d in self._days
            ],
        }

    @classmethod
    def from_raw(cls, raw: Any) -> PelletLoad:
        if not isinstance(raw, dict):
            return cls()
        days = []
        for row in raw.get("days") or []:
            if isinstance(row, dict) and isinstance(row.get("date"), str):
                days.append(
                    PelletDay(
                        date=row["date"],
                        kg=_number(row.get("kg")),
                        kwh=_number(row.get("kwh")),
                    )
                )
        days.sort(key=lambda d: d.date)
        cop = raw.get("cop")
        return cls(
            total_kwh=_number(raw.get("total_kwh")),
            days=days[-KEEP_DAYS:],
            cop=float(cop) if _finite(cop) and cop >= 1.0 else None,
        )


def _number(value: Any) -> float:
    return float(value) if _finite(value) and value >= 0 else 0.0


def _day_after(date: str) -> str | None:
    try:
        return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
    except ValueError:
        return None
