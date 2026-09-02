"""Vagten: må beslutningen overhovedet handles på lige nu?

Planlæggeren svarer altid. Det er dens opgave. Men et svar er ikke det samme
som et svar man tør styre efter, og forskellen er det her modul.

Vagten siger aldrig hvad der skal gøres — kun om nogen bør gøre det. Siger den
nej, står beslutningen der stadig, og Node-RED bruger sin egen logik. Det er
med vilje: en styring der falder tilbage til noget der virker, er bedre end en
der insisterer på at have ret.

**Om at give slip.** Add-on'en skriver ikke til UVR'en. Den udstiller sin
beslutning og et flag der siger om den skal følges, og Node-RED gør resten.
Dermed er der kun ét sted der styrer, og det sted kan altid sige nej til os.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    """Hvad Node-RED skal gøre — og om den overhovedet skal lytte."""

    source: str | None
    acting: bool
    reason: str

    @property
    def note(self) -> str:
        return f"{self.source or '—'} ({self.reason})"


class Guard:
    """Holder styr på om og hvornår vi tør styre."""

    def __init__(
        self,
        enabled: bool = False,
        min_dwell_minutes: float = 15.0,
        warmup_minutes: float = 5.0,
    ) -> None:
        self.enabled = enabled
        # Anlægget må ikke vippe frem og tilbage. Hysteresen i planlæggeren
        # daemper prisstøj; det her sætter en bund under hvor tit kilden
        # overhovedet får lov at skifte.
        self.min_dwell_minutes = min_dwell_minutes
        # Efter en opstart er tabellerne lige indlæst og der er kørt én
        # cyklus. At kaste anlægget om i det øjeblik ville være at handle på
        # den mindst oplyste tilstand vi nogensinde har.
        self.warmup_minutes = warmup_minutes

        self.started: float | None = None
        self.committed: str | None = None
        self.committed_at: float | None = None

    # ------------------------------------------------------------------ tid

    def start(self, now: float) -> None:
        self.started = now

    def _minutes_since(self, then: float | None, now: float) -> float | None:
        return None if then is None else (now - then) / 60

    # -------------------------------------------------------------- vurdering

    def check(
        self, decision: Any, lookup: Any, plan: Any, now: float
    ) -> Command:
        """Afgør om beslutningen må følges. ``now`` er sekunder, monotont."""
        if self.started is None:
            self.started = now

        if not self.enabled:
            return Command(decision.source if decision else None, False, "styring slået fra")

        if decision is None or decision.source is None:
            return Command(None, False, "ingen beslutning")

        # Uden en COP er varmeprisen et gæt, og så er valget det også.
        if lookup is None or decision.heat_price is None:
            return Command(decision.source, False, "ingen COP — styrer ikke")

        warm = self._minutes_since(self.started, now) or 0.0
        if warm < self.warmup_minutes:
            left = self.warmup_minutes - warm
            return Command(decision.source, False, f"varmer op, {left:.0f} min endnu")

        # Uden en plan kan vi stadig vælge kilde — planlæggeren er bygget til
        # det. Men det skal siges, så det ikke ligner mere end det er.
        planless = "" if plan is not None and len(plan) else " (uden plan)"

        if self.committed is None:
            return self._commit(decision.source, now, f"overtager{planless}")

        if decision.source == self.committed:
            return Command(decision.source, True, f"uændret{planless}")

        held = self._minutes_since(self.committed_at, now) or 0.0
        if held < self.min_dwell_minutes:
            left = self.min_dwell_minutes - held
            return Command(
                self.committed,
                True,
                f"holder {self.committed} i {left:.0f} min endnu",
            )

        return self._commit(decision.source, now, f"skifter fra {self.committed}{planless}")

    def _commit(self, source: str, now: float, reason: str) -> Command:
        self.committed = source
        self.committed_at = now
        return Command(source, True, reason)

    def release(self) -> None:
        """Giv slip, så næste overtagelse begynder forfra."""
        self.committed = None
        self.committed_at = None
