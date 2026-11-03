"""Vejrudsigten oversat til det BT1 kommer til at vise.

Varmekurven, COP-tabellen og husets forbrugskurve er alle lært på
``sensor.nibe_bt1_average``. Udsigten er vejrtjenestens tal, og de to er ikke
den samme temperatur. Den 31. oktober til 3. november lå BT1 0,3-0,9 grader
under ``weather.hjem`` hver gang de to kunne sammenlignes, og den 3. november
kl. 12 sagde udsigten 9,9 mod 8,3 målt. Uden en korrektion slog planen COP op
1,6 grader for varmt de første timer, og varmepumpen så billigere ud end den
var.

Der er to slags afvigelse, og de opfører sig forskelligt:

* **Den faste.** Følerens placering, vejrtjenestens net - det der gør BT1
  koldere end udsigten dag efter dag. Den læres langsomt og gælder hele
  horisonten ud.
* **Den her og nu.** Skyer der ikke kom, en front der er forsinket. Den er
  ofte større, men holder kun nogle timer, så den fades ud over
  ``FADE_MINUTES``.

    korrektion(t) = fast + (nu - fast) * fade(t)

Tallene i ``FADE_MINUTES`` og ``HALF_LIFE_MINUTES`` er skøn, ikke målinger.
Derfor efterprøves korrektionen: hver halve time noteres hvad udsigten sagde
1, 3 og 6 timer frem, med og uden korrektion, og når tiden kommer, måles
begge mod BT1. Hjælper korrektionen ikke, står det i tallene.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Over så lang tid går afvigelsen her og nu over i den faste.
FADE_MINUTES = 6 * 60

# Den faste afvigelses hukommelse. To døgn: længe nok til at et døgns
# afvigende vejr ikke flytter den meget, kort nok til at følge en ny årstid.
HALF_LIFE_MINUTES = 2 * 24 * 60

# Et hul i målingerne - en genstart, en føler der faldt ud - tæller højst som
# så mange minutter. Ellers ville den første måling efter et døgns pause
# flytte den faste afvigelse en tredjedel på én gang.
MAX_STEP_MINUTES = 60.0

# Hvor langt frem korrektionen efterprøves, og hvor tit der noteres.
CHECK_HORIZONS = (60, 180, 360)
CHECK_EVERY_MINUTES = 30.0

# Efterprøvningens gennemsnit glemmer langsomt, så det følger årstiden.
_CHECK_SETTLED = 200


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass
class _Check:
    due: float
    horizon: int
    raw: float
    corrected: float


@dataclass
class OutdoorBias:
    """BT1 minus udsigten: den faste del, og den der gælder lige nu."""

    fixed: float | None = None
    count: float = 0.0
    # Kun i hukommelsen. Efter en genstart er den første cyklus' måling
    # det rigtige svar, ikke en der er timer gammel.
    now: float | None = None
    last_at: float | None = None
    # Pr. horisont: (middel absolut fejl uden, middel absolut fejl med, antal).
    errors: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    _pending: list[_Check] = field(default_factory=list)
    _last_check: float | None = None

    def observe(
        self, wall: float, measured: float | None, forecast_now: float | None
    ) -> None:
        """Ét skridt med BT1 og hvad udsigten siger for netop nu."""
        if _finite(measured):
            self._settle(wall, measured)
        if not _finite(measured) or not _finite(forecast_now):
            # Uden den ene af de to er der ingen afvigelse her og nu, og
            # så gælder kun den faste.
            self.now = None
            return

        offset = measured - forecast_now
        step = 1.0
        if self.last_at is not None:
            step = min(max((wall - self.last_at) / 60, 0.0), MAX_STEP_MINUTES)
        decay = 1 - 0.5 ** (step / HALF_LIFE_MINUTES)
        # De første døgn et almindeligt gennemsnit af alt hvad der er målt.
        # Ellers ville den første måling stå som den faste afvigelse i dagevis.
        alpha = max(1 / (self.count + 1), decay)
        self.fixed = offset if self.fixed is None else self.fixed + (offset - self.fixed) * alpha
        self.count += 1
        self.now = offset
        self.last_at = wall

    def correction(self, minutes_ahead: float) -> float:
        """Det der skal lægges til udsigten så mange minutter frem."""
        fixed = self.fixed if self.fixed is not None else 0.0
        if self.now is None:
            return fixed
        weight = min(max(1 - minutes_ahead / FADE_MINUTES, 0.0), 1.0)
        return fixed + (self.now - fixed) * weight

    # ----------------------------------------------------------- efterprøvning

    def note_forecast(self, wall: float, forecast_at: Any) -> None:
        """Notér udsigten frem, med og uden korrektion, til efterprøvning.

        ``forecast_at`` giver udsigtens rå tal så mange minutter fra nu.
        """
        if self.now is None:
            return
        if self._last_check is not None and wall - self._last_check < CHECK_EVERY_MINUTES * 60:
            return
        self._last_check = wall
        for horizon in CHECK_HORIZONS:
            raw = forecast_at(horizon)
            if not _finite(raw):
                continue
            self._pending.append(
                _Check(wall + horizon * 60, horizon, raw, raw + self.correction(horizon))
            )

    def _settle(self, wall: float, measured: float) -> None:
        keep = []
        for check in self._pending:
            if check.due > wall:
                keep.append(check)
                continue
            raw_err, corr_err, n = self.errors.get(check.horizon, (0.0, 0.0, 0.0))
            n += 1
            alpha = max(1 / n, 1 / _CHECK_SETTLED)
            raw_err += (abs(check.raw - measured) - raw_err) * alpha
            corr_err += (abs(check.corrected - measured) - corr_err) * alpha
            self.errors[check.horizon] = (raw_err, corr_err, n)
        self._pending = keep

    @property
    def note(self) -> str:
        if self.fixed is None:
            return "ingen korrektion endnu"
        now = f"nu {self.now:+.1f}, " if self.now is not None else ""
        text = f"{now}fast {self.fixed:+.1f} °C ({self.count:.0f} målinger)"
        checks = [
            f"{h // 60} t: {raw:.1f} → {corr:.1f}"
            for h, (raw, corr, n) in sorted(self.errors.items())
            if n
        ]
        if checks:
            text += " | fejl uden → med: " + ", ".join(checks)
        return text

    def to_status(self) -> dict[str, Any]:
        return {
            "nu": self.now,
            "fast": self.fixed,
            "målinger": self.count,
            "efterprøvning": {
                f"{h // 60} t": {"uden": raw, "med": corr, "antal": n}
                for h, (raw, corr, n) in sorted(self.errors.items())
            },
        }

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        return {
            "fixed": None if self.fixed is None else round(self.fixed, 4),
            "count": self.count,
            "errors": {
                str(h): [round(raw, 4), round(corr, 4), n]
                for h, (raw, corr, n) in self.errors.items()
            },
        }

    @classmethod
    def from_raw(cls, raw: Any) -> OutdoorBias:
        bias = cls()
        if not isinstance(raw, dict):
            return bias
        fixed = raw.get("fixed")
        count = raw.get("count", 0)
        if _finite(fixed) and _finite(count) and count >= 0:
            bias.fixed = float(fixed)
            bias.count = float(count)
        errors = raw.get("errors")
        if isinstance(errors, dict):
            for key, value in errors.items():
                try:
                    horizon = int(key)
                    raw_err, corr_err, n = (float(v) for v in value)
                except (TypeError, ValueError):
                    continue
                if all(math.isfinite(v) for v in (raw_err, corr_err, n)):
                    bias.errors[horizon] = (raw_err, corr_err, n)
        return bias
