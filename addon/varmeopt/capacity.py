"""Hvor hurtigt varmepumpen faktisk fylder lageret.

Typeskiltet siger 16 kW, og det gør papiret også. Men maskinen bestemmer selv
hvor hårdt den kører: i praksis omkring 12 kW, højst 14. Den forskel er ikke
kosmetisk, for raten står fire steder i planlægningen og trækker alle samme
vej:

* hvor meget der kan nå at blive ladet inden prisen stiger,
* om der stadig er tid til at vente på en billigere halvtime,
* hvor mange halvtimer «lad op» skal stå ud for,
* og hvor lidt et minimumstræk er.

Sættes raten for højt, tror planlæggeren at den har bedre tid end den har.
Den venter for længe, starter for sent og når for lidt — og fejlen viser sig
netop de dage hvor det gælder, hvor et billigt vindue skal udnyttes inden en
dyr aften.

Derfor måles den. Siden varmepumpens egen ydelse blev læst
(``sensor.node_1_analog_logging_24``), er raten en aflæsning frem for et
skøn, og den behøver ikke gættes af et typeskilt.

**Kun de hårde minutter tæller.** Pumpen kører også 2-3 kW rumvarme i lange
stræk, og et gennemsnit over alt ville trække raten langt under det den kan.
Det der skal måles, er hvad den leverer *når den lader* — så kun minutter over
en andel af typeskiltets tal regnes med.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Under denne andel af typeskiltets ydelse er pumpen ikke i gang med at lade
# lageret op - saa er det rumvarme ved dellast, og den siger intet om hvor
# hurtigt tankene kan fyldes.
CHARGE_SHARE = 0.4

# Under saa mange maalinger flytter en ny aflaesning raten maerkbart; derover
# er den kendt. Samme form som resten af det der laeres i projektet.
_SETTLED_COUNT = 30


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass
class ChargeRate:
    """Varmepumpens målte ydelse, når den lader lageret op."""

    # Typeskiltets tal. Bruges som bund for hvad der taeller som en opladning,
    # og som svar indtil der er maalt noget.
    nameplate_kw: float = 16.0
    kw: float | None = None
    count: float = 0.0
    peak_kw: float | None = None

    def observe(self, output_kw: float | None) -> None:
        """Ét skridt. ``output_kw`` er varmepumpens målte varmeydelse."""
        if not _finite(output_kw):
            return
        if output_kw < self.nameplate_kw * CHARGE_SHARE:
            return

        self.count += 1
        alpha = 0.2 if self.count < _SETTLED_COUNT else 0.05
        self.kw = output_kw if self.kw is None else self.kw * (1 - alpha) + output_kw * alpha
        self.peak_kw = output_kw if self.peak_kw is None else max(self.peak_kw, output_kw)

    @property
    def effective_kw(self) -> float:
        """Den rate planlægningen skal regne med.

        Den målte, når der er målt noget. Ellers typeskiltets — og så er
        svaret for optimistisk, men det er det eneste vi har indtil pumpen
        har kørt en opladning.
        """
        return self.kw if self.kw is not None else self.nameplate_kw

    @property
    def note(self) -> str:
        if self.kw is None:
            return f"typeskilt {self.nameplate_kw:.0f} kW — ikke målt endnu"
        peak = f", højst {self.peak_kw:.1f}" if self.peak_kw is not None else ""
        return f"målt {self.kw:.1f} kW{peak} over {self.count:.0f} minutter"

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        return {
            "kw": None if self.kw is None else round(self.kw, 3),
            "peak_kw": None if self.peak_kw is None else round(self.peak_kw, 3),
            "count": self.count,
        }

    @classmethod
    def from_raw(cls, raw: Any, nameplate_kw: float = 16.0) -> ChargeRate:
        rate = cls(nameplate_kw=nameplate_kw)
        if not isinstance(raw, dict):
            return rate
        try:
            kw = raw.get("kw")
            peak = raw.get("peak_kw")
            rate.kw = None if kw is None else float(kw)
            rate.peak_kw = None if peak is None else float(peak)
            rate.count = float(raw.get("count", 0))
        except (TypeError, ValueError):
            return cls(nameplate_kw=nameplate_kw)
        if rate.kw is not None and (not math.isfinite(rate.kw) or rate.kw <= 0):
            rate.kw = None
        return rate
