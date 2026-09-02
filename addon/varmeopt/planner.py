"""Beslutningen: hvilken kilde nu, og skal der lades ud over behovet.

Det er fristende at lade en planlægger lægge et helt døgns skema. Den fristelse
er værd at modstå. Et skema er svært at gennemskue når det er forkert, det
forældes i samme øjeblik en pris eller en vejrudsigt flytter sig, og en styring
skal alligevel kun bruge det første skridt. Så den svarer i stedet på tre
spørgsmål hver cyklus:

1. Hvilken kilde er billigst til behovet lige nu?
2. Er der en senere time hvor varmen bliver dyrere — nok til at det betaler sig
   at lave den nu og gemme den?
3. Hvor meget må der så lades, når solen har fået sit, og lageret har plads?

Spørgsmål 1 kan besvares uden nogen plan overhovedet, og det er med vilje: er
Predbat nede eller planen forældet, falder styringen tilbage på det, i stedet
for at stå uden svar.

**Om fremtidige COP.** Vi kender ikke vejrudsigten, så COP fremad regnes på den
nuværende udetemperatur. Over nogle timer flytter den sig nogle få grader og
COP nogle procent, mens prisen kan fordoble sig — så prisen dominerer, og
tilnærmelsen holder. ``outdoor_later`` findes for den dag en vejrudsigt kobles
på; indtil da er den lig med nu.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Hvor langt frem det giver mening at gemme varme. Ud over det æder ståtabet
# gevinsten, og prisprognosen bliver for usikker til at handle på.
DEFAULT_HORIZON_MINUTES = 12 * 60

SLOT_MINUTES = 30


@dataclass(frozen=True)
class Decision:
    """Svaret. ``reason`` er en del af svaret, ikke en note ved siden af."""

    source: str  # "varmepumpe" | "pillefyr"
    heat_price: float | None
    pellet_price: float
    charge: bool = False
    charge_kwh: float | None = None
    saving_kr: float | None = None
    window_minutes: int | None = None
    reason: str = ""

    @property
    def charging_note(self) -> str:
        if self.charge and self.charge_kwh is not None:
            return f"lad {self.charge_kwh:.1f} kWh"
        return "lad ikke op"


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def source_now(
    heat_price: float | None, pellet_price: float, hysteresis: float
) -> tuple[str, str]:
    """Spørgsmål 1: hvilken kilde er billigst til behovet nu?

    Ved uafgjort vinder varmepumpen, som Node-RED også gør. Hysteresen er der
    for at valget ikke vipper frem og tilbage på nogle ører.
    """
    if heat_price is None:
        return "varmepumpe", "ingen COP - antager varmepumpe"
    if heat_price > pellet_price + hysteresis:
        return "pillefyr", f"VP {heat_price:.2f} > pille {pellet_price:.2f}"
    if heat_price < pellet_price - hysteresis:
        return "varmepumpe", f"VP {heat_price:.2f} < pille {pellet_price:.2f}"
    return "varmepumpe", "taet loeb - varmepumpen foretraekkes"


class Planner:
    """Binder pris, COP, lager og sol sammen til ét svar."""

    def __init__(
        self,
        pellet_price: float,
        hysteresis: float = 0.05,
        wear_kr_per_kwh: float = 0.15,
        min_charge_kwh: float = 4.0,
        charge_kw: float = 16.0,
        horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    ) -> None:
        self.pellet_price = pellet_price
        self.hysteresis = hysteresis
        # At koere varmepumpen koster noget ud over stroemmen. Tallet kommer
        # fra den ukoblede v4-node i Node-RED, hvor det var en konstant - her
        # er det en indstilling, saa det kan efterproeves mod virkeligheden.
        self.wear = wear_kr_per_kwh
        self.min_charge_kwh = min_charge_kwh
        self.charge_kw = charge_kw
        self.horizon_minutes = horizon_minutes

    # ------------------------------------------------------------------ pris

    def heat_price(self, electricity_price: float | None, cop: float | None) -> float | None:
        if not _finite(electricity_price) or not _finite(cop) or cop <= 0:
            return None
        return electricity_price / cop

    def cheapest_heat(self, electricity_price: float | None, cop: float | None) -> float:
        """Den billigste varme man kan lave i en given time.

        Pillefyret sætter loftet: uanset hvor dyr strømmen bliver, behøver man
        aldrig betale mere end pillevarmen koster. Det er derfor en plan aldrig
        skal jagte ekstreme priser — der findes en anden udvej.
        """
        vp = self.heat_price(electricity_price, cop)
        return self.pellet_price if vp is None else min(vp, self.pellet_price)

    # -------------------------------------------------------------- beslutning

    def decide(
        self,
        plan: Any,
        cop_now: float | None,
        cop_later: float | None = None,
        headroom_kwh: float | None = None,
        solar_expected_kwh: float | None = None,
    ) -> Decision:
        """Hele svaret: kilde nu, og om der skal lades ud over behovet."""
        price_now = plan.marginal(0) if plan is not None else None
        now = price_now.kr_per_kwh if price_now is not None else None

        vp_now = self.heat_price(now, cop_now)
        source, why = source_now(vp_now, self.pellet_price, self.hysteresis)

        decision = Decision(
            source=source,
            heat_price=vp_now,
            pellet_price=self.pellet_price,
            reason=why,
        )
        if plan is None or vp_now is None:
            return decision

        # Spoergsmaal 2: findes der en senere time hvor varmen bliver dyrere?
        later_cop = cop_later if cop_later is not None else cop_now
        best_gap = 0.0
        best_when = None
        for minutes in range(SLOT_MINUTES, self.horizon_minutes + 1, SLOT_MINUTES):
            price = plan.marginal(minutes)
            if price is None:
                break
            gap = self.cheapest_heat(price.kr_per_kwh, later_cop) - vp_now
            if gap > best_gap:
                best_gap, best_when = gap, minutes

        # Slitagen skal daekkes foer det er en gevinst. Ellers er man bare
        # begyndt at slide paa varmepumpen for at flytte oerer.
        margin = best_gap - self.wear
        if best_when is None or margin <= 0:
            return _with(decision, reason=f"{why}; intet at hente ved at gemme")

        # Spoergsmaal 3: hvor meget maa der lades?
        room = headroom_kwh if _finite(headroom_kwh) else 0.0
        if _finite(solar_expected_kwh):
            # Solen faar sit foerst - dens varme er gratis.
            room = max(0.0, room - solar_expected_kwh)
        window = min(best_when, self.horizon_minutes)
        room = min(room, self.charge_kw * window / 60)

        if room < self.min_charge_kwh:
            return _with(
                decision,
                reason=(
                    f"{why}; {margin:.2f} kr/kWh at hente om {best_when} min, "
                    f"men kun {room:.1f} kWh plads - under minimumstraekket"
                ),
            )

        return _with(
            decision,
            charge=True,
            charge_kwh=room,
            saving_kr=margin * room,
            window_minutes=best_when,
            reason=(
                f"{why}; lad {room:.1f} kWh nu og spar {margin * room:.2f} kr "
                f"mod om {best_when} min"
            ),
        )


def _with(decision: Decision, **changes: Any) -> Decision:
    from dataclasses import replace

    return replace(decision, **changes)
