"""Regnskabet: kunne opladningerne svare sig, og hvad blev der sparet?

Planlæggeren lover en besparelse når den lægger en blok - «lad 5,2 kWh nu og
spar 1,23 kr». Det er et skøn på priser og COP'er der endnu ikke er sket. Det
her er bagefter-regnestykket, og det bygger kun på det der faktisk skete:

* **Hvad opladningen kostede.** Varmepumpens målte elforbrug, minut for
  minut, ganget med marginalprisen i det minut - den samme pris kildevalget
  bruger, så en kilowatt-time fra batteriet koster det batteriet er værd og
  ikke nettariffen. Oveni slitagen pr. kWh varme, som ``heat_price`` også
  regner med.

* **Hvad den samme varme ellers ville have kostet.** Uden opladningen var
  varmen blevet lavet i det dyre stræk blokken blev lagt imod - af den
  billigste af varmepumpen ved rumvarmens setpunkt og pillefyret. Den pris
  *måles* mens strækket står på, minut for minut. Før strækket er forbi, er
  posten ikke afregnet, og så står den som foreløbig.

Besparelsen er forskellen, ganget med den varme opladningen leverede.

**Hvad der ikke er trukket fra.** Ståtabet: varme der er lavet kl. 14 og
bruges kl. 19, har stået og tabt sig. Og regnestykket antager at hele den
ladede varme blev brugt i strækket. Bruges den først bagefter, har den stadig
fortrængt noget - men til en anden pris. Begge dele gør tallet lidt for pænt,
og det skal stå på siden.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any

# Længste spring mellem to cyklusser der tælles med. Samme grænse som
# blokkens egen tælling i ``charge.py``: har add-on'en været nede, ved vi ikke
# hvad pumpen lavede imens.
MAX_GAP_SECONDS = 300.0

# Så mange opladninger huskes. Ca. to om dagen giver et par måneder.
MAX_ENTRIES = 200


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass
class Entry:
    """Én opladning, fra den startede til strækket den var til, er forbi."""

    started: float
    ended: float | None = None
    manual: bool = False
    top_up: bool = False
    # Det dyre stræk varmen skulle fortrænge. ``None`` når det ikke kendes -
    # en manuel opladning uden et stræk i planen - og så kan der ikke regnes
    # en besparelse, kun en pris.
    stretch_from: float | None = None
    stretch_until: float | None = None
    # Hvad planlæggeren lovede, da blokken blev lagt.
    expected_kr: float | None = None
    heat_kwh: float = 0.0
    el_kwh: float = 0.0
    el_kr: float = 0.0
    wear_kr: float = 0.0
    # Den målte alternative varmepris i strækket, som kr/kWh · sekunder.
    alt_weighted: float = 0.0
    alt_seconds: float = 0.0

    @property
    def paid_per_kwh(self) -> float | None:
        if self.heat_kwh <= 0:
            return None
        return (self.el_kr + self.wear_kr) / self.heat_kwh

    @property
    def alt_per_kwh(self) -> float | None:
        if self.alt_seconds <= 0:
            return None
        return self.alt_weighted / self.alt_seconds

    def settled(self, now: float) -> bool:
        """Er strækket forbi, og har vi set det?"""
        return (
            self.stretch_until is not None
            and now >= self.stretch_until
            and self.alt_seconds > 0
        )

    @property
    def saving_kr(self) -> float | None:
        paid, alt = self.paid_per_kwh, self.alt_per_kwh
        if paid is None or alt is None:
            return None
        return self.heat_kwh * (alt - paid)

    @property
    def cop(self) -> float | None:
        if self.el_kwh <= 0:
            return None
        return self.heat_kwh / self.el_kwh

    def to_raw(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_raw(cls, raw: Any) -> Entry | None:
        if not isinstance(raw, dict) or not _finite(raw.get("started")):
            return None
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in raw.items() if k in known})
        except TypeError:
            return None


class Ledger:
    """Opladningerne, én post hver, og den løbende tælling."""

    def __init__(self, entries: list[Entry] | None = None) -> None:
        self.entries: list[Entry] = entries or []
        self._last_seen: float | None = None

    @property
    def open(self) -> Entry | None:
        """Den opladning der kører lige nu, hvis nogen."""
        if self.entries and self.entries[-1].ended is None:
            return self.entries[-1]
        return None

    def observe(
        self,
        now: float,
        block: Any,
        stretch: tuple[float, float] | None,
        expected_kr: float | None,
        price_kr: float | None,
        el_kw: float | None,
        heat_kw: float | None,
        hp_heat_price: float | None,
        pellet_price: float | None,
        wear: float,
    ) -> None:
        """Ét skridt.

        ``block`` er den blok der kører lige nu, eller ``None``. ``stretch``
        og ``expected_kr`` er fra beslutningen, og bruges kun når en ny
        opladning åbnes. ``hp_heat_price`` er hvad en kWh varme fra
        varmepumpen koster lige nu, ved rumvarmens setpunkt.
        """
        seen, self._last_seen = self._last_seen, now
        gap = None if seen is None else now - seen
        if gap is not None and (gap <= 0 or gap > MAX_GAP_SECONDS):
            gap = None

        current = self.open
        began = getattr(block, "began", None) if block is not None else None
        if current is not None and (began is None or abs(current.started - began) > 1):
            current.ended = now
            current = None
        if current is None and began is not None:
            current = self._start(block, began, stretch, expected_kr)
            # Minuttet inden blokken begyndte, hører ikke til den.
            gap_for_block = None
        else:
            gap_for_block = gap

        if current is not None and gap_for_block is not None:
            hours = gap_for_block / 3600
            if _finite(el_kw) and el_kw > 0:
                current.el_kwh += el_kw * hours
                if _finite(price_kr):
                    current.el_kr += el_kw * hours * price_kr
            if _finite(heat_kw) and heat_kw > 0:
                current.heat_kwh += heat_kw * hours
                current.wear_kr += heat_kw * hours * wear

        # Den alternative pris tælles for hver post hvis stræk står på nu.
        if gap is not None:
            alt = self._alternative(hp_heat_price, pellet_price)
            if alt is not None:
                for entry in self.entries:
                    if (
                        entry.stretch_from is not None
                        and entry.stretch_until is not None
                        and entry.stretch_from <= now < entry.stretch_until
                    ):
                        entry.alt_weighted += alt * gap
                        entry.alt_seconds += gap

    @staticmethod
    def _alternative(hp: float | None, pellet: float | None) -> float | None:
        """Den billigste varme der kunne være lavet på stedet."""
        options = [p for p in (hp, pellet) if _finite(p) and p > 0]
        return min(options) if options else None

    def _start(
        self,
        block: Any,
        began: float,
        stretch: tuple[float, float] | None,
        expected_kr: float | None,
    ) -> Entry:
        manual = bool(getattr(block, "manual", False))
        # En automatisk blok kender sit eget stræk. En manuel har kun
        # knappens minutter, så dér lånes planens stræk, hvis den har et.
        if not manual:
            stretch = (block.dear_from, block.dear_until)
        entry = Entry(
            started=began,
            manual=manual,
            top_up=bool(getattr(block, "top_up", False)),
            stretch_from=None if stretch is None else float(stretch[0]),
            stretch_until=None if stretch is None else float(stretch[1]),
            expected_kr=(
                float(expected_kr) if not manual and _finite(expected_kr) else None
            ),
        )
        self.entries.append(entry)
        del self.entries[:-MAX_ENTRIES]
        return entry

    def totals(self, now: float, since: float | None = None) -> dict[str, float]:
        """De afregnede posters sum - kun dem hvis stræk er forbi."""
        settled = [
            e
            for e in self.entries
            if e.settled(now)
            and e.saving_kr is not None
            and (since is None or e.started >= since)
        ]
        return {
            "count": float(len(settled)),
            "heat_kwh": sum(e.heat_kwh for e in settled),
            "paid_kr": sum(e.el_kr + e.wear_kr for e in settled),
            "alt_kr": sum(e.heat_kwh * (e.alt_per_kwh or 0.0) for e in settled),
            "saving_kr": sum(e.saving_kr or 0.0 for e in settled),
            "expected_kr": sum(e.expected_kr or 0.0 for e in settled),
        }

    def to_raw(self) -> dict[str, Any]:
        return {"entries": [e.to_raw() for e in self.entries]}

    @classmethod
    def from_raw(cls, raw: Any) -> Ledger:
        if not isinstance(raw, dict):
            return cls()
        items = raw.get("entries")
        entries = [
            e for e in (Entry.from_raw(r) for r in items or []) if e is not None
        ] if isinstance(items, list) else []
        return cls(entries[-MAX_ENTRIES:])
