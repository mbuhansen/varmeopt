"""Døgnets forbrug ud af lageret, delt på varme, varmt vand og spa.

Forbrugssiden viste hidtil kun øjebliksbilleder: hvad huset trækker lige nu,
og hvad forbrugskurven har lært. Begge dele svarer på «hvor hårdt kører det»,
og ingen af dem på det spørgsmål man faktisk står med: *hvor blev varmen af i
dag, og er det meget?* Til det skal der tælles sammen, og der skal være noget
at holde dagen op imod.

**Det der tælles, er varmen ud af lageret** — ikke den varmepumpen lavede. De
to er ikke det samme: pumpen kan fylde tankene om formiddagen til en aften
hvor den står stille, og et regnskab på produktionen ville lægge hele aftenens
bad om formiddagen. Ud af lageret kommer varmen derimod når den bruges.

De tre tal kommer hvert fra sin kilde, og det er med vilje:

* ``varme`` er ``HouseLoad.kw_at`` — husets eget træk, målt på lagerets
  energiændring eller af flowmåleren, med beholderens og spaens træk allerede
  trukket fra.
* ``vvb`` og ``spa`` er anlæggets eget skøn over de to, det samme som
  ``_vessel_kw`` bruger til at holde dem ude af husets tal. Det er et skøn og
  ikke en måling — der sidder ingen energimåler på nogen af dem — men det er
  *det samme* skøn i begge ender, så de tre tal kan lægges sammen uden at der
  mangler eller tælles dobbelt.

Derfor er summen af de tre lagerets samlede træk, og det er den egenskab hele
siden hviler på.

**Huller tælles ikke.** Står add-on'en stille i et kvarter, ved vi ikke hvad
der skete imens, og et gæt ville lægge sig oven i dagens tal hvor ingen kan se
det igen. Et spring over ``MAX_GAP_SECONDS`` springes over — samme regel som
``VesselProfile.observe`` bruger, og af samme grund.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Et hul i målingen betyder at vi ikke ved hvad der skete imens. Fem minutter
# er ti cyklusser - rigeligt til en langsom cyklus, for lidt til en genstart.
MAX_GAP_SECONDS = 300.0

# Hvor tæt kurvens punkter ligger. Et kvarter er 96 punkter i døgnet og 384
# over de fire, og det er både nok til at se formen og lidt nok til at ligge i
# en JSON-fil der skrives hvert femte minut.
SAMPLE_MINUTES = 15

MINUTES_PER_DAY = 24 * 60

# I dag og de tre foregående. Fire døgn er nok til at se om i dag ligner de
# andre, og kort nok til at vejret ikke nåede at blive en anden årstid.
KEEP_DAYS = 4

CATEGORIES = ("varme", "vvb", "spa")


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _local(at: float) -> datetime | None:
    """Tidspunktet i lokal tid. Et ur der ikke giver mening, giver ingenting."""
    try:
        return datetime.fromtimestamp(at).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


@dataclass
class Day:
    """Ét døgns forbrug, og formen på vejen derhen.

    ``samples`` er kurven: kumuleret forbrug ved hvert kvarter i døgnet, så
    siden kan tegne den uden at gemme hvert minut. Punkterne er (minut i
    døgnet, varme, vvb, spa), alle tre *kumuleret siden midnat*.
    """

    date: str
    varme: float = 0.0
    vvb: float = 0.0
    spa: float = 0.0
    samples: list[tuple[int, float, float, float]] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.varme + self.vvb + self.spa

    def part(self, name: str) -> float:
        return float(getattr(self, name, 0.0))


class Usage:
    """Forbruget døgn for døgn. Nyeste sidst."""

    def __init__(self, days: list[Day] | None = None, keep: int = KEEP_DAYS) -> None:
        self._days: list[Day] = list(days or [])
        self._keep = max(1, keep)
        self._last: float | None = None

    # ------------------------------------------------------------- opslag

    @property
    def days(self) -> list[Day]:
        """Ældste først, nyeste sidst."""
        return list(self._days)

    @property
    def today(self) -> Day | None:
        return self._days[-1] if self._days else None

    @property
    def yesterday(self) -> Day | None:
        """Døgnet før det nyeste — kun når det *er* dagen før.

        Har add-on'en stået stille et døgn, er den næstsidste dag i listen
        ikke i går, og så er svaret ingenting. Et tal med en forkert dato på
        er værre end intet tal: det bliver sammenlignet alligevel.
        """
        if len(self._days) < 2:
            return None
        before = self._days[-2]
        return before if _day_after(before.date) == self._days[-1].date else None

    # -------------------------------------------------------------- måling

    def observe(
        self,
        now: float,
        house_kw: float | None,
        vvb_kw: float | None,
        spa_kw: float | None,
    ) -> None:
        """Ét skridt. De tre kW er træk *ud af lageret*, hver for sig.

        ``None`` er ikke nul. Kan husets forbrug ikke måles, står dagens
        varmetal stille i de minutter — det er ærligere end at lægge et gæt
        til et tal nogen senere læser som en måling.
        """
        stamp = _local(now)
        if stamp is None:
            return

        gap = now - self._last if self._last is not None else 0.0
        self._last = now

        day = self._day_for(stamp.strftime("%Y-%m-%d"))
        if gap <= 0 or gap > MAX_GAP_SECONDS:
            # Hullet tæller ikke, men dagen er skiftet og skal stadig findes -
            # derfor efter ``_day_for`` og ikke før.
            return

        hours = gap / 3600
        for name, kw in (("varme", house_kw), ("vvb", vvb_kw), ("spa", spa_kw)):
            if _finite(kw) and kw > 0:
                setattr(day, name, day.part(name) + kw * hours)

        self._sample(day, stamp.hour * 60 + stamp.minute)

    def _day_for(self, date: str) -> Day:
        if self._days and self._days[-1].date == date:
            return self._days[-1]
        # Dagen der slutter, får et sidste punkt ved midnat. Uden det holder
        # kurven op ved 23:45, og savtakken når aldrig sin egen top.
        if self._days:
            self._close(self._days[-1])
        self._days.append(Day(date=date))
        del self._days[: -self._keep]
        return self._days[-1]

    @staticmethod
    def _close(day: Day) -> None:
        if not day.samples or day.samples[-1][0] < MINUTES_PER_DAY:
            day.samples.append((MINUTES_PER_DAY, day.varme, day.vvb, day.spa))

    def _sample(self, day: Day, minute: int) -> None:
        bucket = minute // SAMPLE_MINUTES * SAMPLE_MINUTES
        if day.samples and day.samples[-1][0] >= bucket:
            return
        day.samples.append((bucket, day.varme, day.vvb, day.spa))

    # --------------------------------------------------------------- lager

    def to_raw(self) -> dict[str, Any]:
        return {
            "days": [
                {
                    "date": day.date,
                    "varme": round(day.varme, 4),
                    "vvb": round(day.vvb, 4),
                    "spa": round(day.spa, 4),
                    "samples": [
                        [m, round(v, 3), round(b, 3), round(s, 3)]
                        for m, v, b, s in day.samples
                    ],
                }
                for day in self._days
            ]
        }

    @classmethod
    def from_raw(cls, raw: Any, keep: int = KEEP_DAYS) -> Usage:
        days: list[Day] = []
        rows = raw.get("days") if isinstance(raw, dict) else None
        for row in rows or []:
            if not isinstance(row, dict) or not isinstance(row.get("date"), str):
                continue
            samples = []
            for point in row.get("samples") or []:
                if isinstance(point, (list, tuple)) and len(point) == 4:
                    try:
                        samples.append(
                            (
                                int(point[0]),
                                float(point[1]),
                                float(point[2]),
                                float(point[3]),
                            )
                        )
                    except (TypeError, ValueError):
                        continue
            days.append(
                Day(
                    date=row["date"],
                    varme=_number(row.get("varme")),
                    vvb=_number(row.get("vvb")),
                    spa=_number(row.get("spa")),
                    samples=samples,
                )
            )
        days.sort(key=lambda d: d.date)
        return cls(days[-max(1, keep) :], keep=keep)


def _number(value: Any) -> float:
    return float(value) if _finite(value) else 0.0


def _day_after(date: str) -> str | None:
    """Datoen efter, som tekst. Bruges kun til at genkende «i går»."""
    try:
        from datetime import timedelta

        return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
    except ValueError:
        return None
