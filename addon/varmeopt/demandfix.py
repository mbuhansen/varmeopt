"""Behovsudsigten rettet efter hvor meget den rammer ved siden af.

Planlæggeren dimensionerer en opladning efter hvad huset ventes at trække i
det dyre stræk - forbrugskurven slået op ved den forudsagte udetemperatur.
Rammer den systematisk for lavt, bliver blokken for lille, lageret løber
tørt inden strækket er forbi, og pumpen må køre i det dyre alligevel. Den
24. november var det præcis det der skete kl. 6.

Logbogen gemmer udsigten som den så ud *dengang* - for hver halvtime frem i
horisonten - og bagefter hvad huset faktisk trak. Hver nat holdes de op mod
hinanden: udsigten lavet 1-12 timer før, for det er den en opladning lægges
på, mod det målte træk i den halvtime.

**En faktor, ikke en margin.** Energy-pilot lægger 70 %-percentilen af fejlen
oveni i de dyre timer. Det gør hver opladning større, også når udsigten rammer
rigtigt i gennemsnit. En faktor retter kun en skævhed: rammer udsigten 15 %
for lavt, ganges den med 1,15; rammer den rigtigt, er faktoren 1.

**Værnene**, i Energy-pilots ånd:

* Ingen virkning før ``MIN_DAYS`` dage med nok sammenlignelige halvtimer.
  Indtil da læres faktoren, men udsigten bruges urørt.
* Både den lærte og den *brugte* faktor flytter en femtedel af vejen pr.
  nat, og aldrig uden for ``MIN_FACTOR``-``MAX_FACTOR``. Den brugte glider
  også når faktoren bliver aktiv eller holder op med at være det - her
  sprang den før fra 1,0 til den lærte værdi den nat den blev aktiv.
* Halvtimer add-on'en kun så en del af, eller hvor husets træk ikke kunne
  måles, er ikke med.

**Den læres ikke oven på sig selv.** Logbogens udsigt er allerede ganget med
den faktor der gjaldt dengang, og den står på linjen. Den deles ud igen, så
det er den rå kurves skævhed der måles - ellers ville en faktor der var
rigtig, blive ved med at vokse.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

MIN_DAYS = 14
# Vinduet er længere end kravet: første og sidste dag i det er altid halve,
# og et par dårlige dage må ikke holde faktoren på 1 for altid.
WINDOW_DAYS = 21
STEP = 0.2
MIN_FACTOR, MAX_FACTOR = 0.8, 1.3
# Udsigten lavet så langt frem: en halv time er nuet, tolv timer er hvor langt
# et dyrt stræk typisk ligger fra den opladning der lægges imod det.
MIN_AHEAD, MAX_AHEAD = 2, 24
MIN_COVER = 0.95
# Så mange *målte halvtimer* skal en dag have for at tælle - ikke par. Hver
# målt halvtime holdes op mod op til 23 udsigter, så 24 par kunne før nås med
# to målinger i døgnet (gennemgangen den 24. november).
MIN_SLOTS_PER_DAY = 24
SLOT = 1800


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


class DemandFactor:
    def __init__(self) -> None:
        self.learned = 1.0
        # Den faktor udsigten faktisk ganges med. Glider mod ``learned`` når
        # den er aktiv, mod 1 når den ikke er.
        self.applied = 1.0
        self.days = 0
        self.last_ratio: float | None = None
        self.last_night: str | None = None
        self.log: list[dict[str, Any]] = []

    @property
    def active(self) -> bool:
        return self.days >= MIN_DAYS

    @property
    def factor(self) -> float:
        """Den faktor udsigten ganges med nu - 1 indtil der er data nok."""
        return self.applied

    def nightly(self, rows: list[dict[str, Any]], night: str) -> bool:
        """Én nats gennemgang. Returnerer om faktoren blev regnet."""
        if night == self.last_night:
            return False
        self.last_night = night
        actual = {}
        for row in rows:
            skete = row.get("skete") or {}
            # Husets forbrug som kurven er lært af. Flowmåleren alene falder
            # ud under sin bund - netop de stille halvtimer - og trak faktoren
            # opad; den måler også en lidt anden størrelse end kurven.
            kwh = skete.get("hus_kwh")
            if (row.get("dækning") or 0) >= MIN_COVER and _finite(kwh) and kwh >= 0:
                actual[int(row["t"])] = kwh * 2  # kWh på en halv time -> kW
        newest = max(actual) if actual else None
        if newest is None:
            return False
        cut = newest - WINDOW_DAYS * 86400
        real = raw = 0.0
        per_day: dict[str, set[int]] = {}
        for row in rows:
            t = int(row["t"])
            if t < cut or (row.get("dækning") or 0) < MIN_COVER:
                continue
            seen = row.get("så") or {}
            ahead = (seen.get("frem") or {}).get("behov_kw")
            used = seen.get("behov_faktor")
            used = used if _finite(used) and used > 0 else 1.0
            if not isinstance(ahead, list):
                continue
            for k in range(MIN_AHEAD, min(MAX_AHEAD + 1, len(ahead))):
                forecast = ahead[k]
                target = t + k * SLOT
                if not _finite(forecast) or forecast <= 0.1 or target not in actual:
                    continue
                real += actual[target]
                raw += forecast / used
                day = datetime.fromtimestamp(target).strftime("%Y-%m-%d")
                per_day.setdefault(day, set()).add(target)
        days = sum(1 for slots in per_day.values() if len(slots) >= MIN_SLOTS_PER_DAY)
        if raw <= 0:
            self.days = days
            self._glide()
            return False
        ratio = real / raw
        self.last_ratio = round(ratio, 3)
        target = min(MAX_FACTOR, max(MIN_FACTOR, ratio))
        old = self.learned
        # Den lærte først, så dagene: hovedtråden læser ``factor`` mens den her
        # kører, og den må ikke se et nyt antal dage med en gammel faktor.
        self.learned = round(min(MAX_FACTOR, max(MIN_FACTOR, old + STEP * (target - old))), 3)
        self.days = days
        self._glide()
        self.log = (self.log + [{
            "nat": night, "forhold": self.last_ratio, "faktor": self.learned,
            "dage": self.days, "virker": self.active,
        }])[-30:]
        return True

    def _glide(self) -> None:
        """Den brugte faktor en femtedel mod målet - den lærte, eller 1."""
        goal = self.learned if self.active else 1.0
        self.applied = round(self.applied + STEP * (goal - self.applied), 3)

    @property
    def note(self) -> str:
        if self.last_ratio is None:
            return "samler - ingen nat gennemgået endnu"
        if not self.active:
            return (
                f"lærer: udsigten rammer {self.last_ratio:.2f} gange det målte, "
                f"faktor {self.learned:.2f} - virker efter {MIN_DAYS} dage "
                f"({self.days} indtil nu)"
            )
        return (
            f"virker: behovet ganges med {self.applied:.2f} på vej mod "
            f"{self.learned:.2f} ({self.days} dage, sidst målt {self.last_ratio:.2f})"
        )

    def to_raw(self) -> dict[str, Any]:
        return {
            "learned": self.learned, "applied": self.applied, "days": self.days,
            "last_ratio": self.last_ratio,
            "last_night": self.last_night, "log": self.log,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> DemandFactor:
        out = cls()
        if not isinstance(raw, dict):
            return out
        if _finite(raw.get("learned")):
            out.learned = min(MAX_FACTOR, max(MIN_FACTOR, float(raw["learned"])))
        if _finite(raw.get("applied")):
            out.applied = min(MAX_FACTOR, max(MIN_FACTOR, float(raw["applied"])))
        if isinstance(raw.get("days"), int) and raw["days"] >= 0:
            out.days = raw["days"]
        if _finite(raw.get("last_ratio")):
            out.last_ratio = float(raw["last_ratio"])
        if isinstance(raw.get("last_night"), str):
            out.last_night = raw["last_night"]
        if isinstance(raw.get("log"), list):
            out.log = [e for e in raw["log"] if isinstance(e, dict)][-30:]
        return out
