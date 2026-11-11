"""Varmepumpens forventede strøm - den last Predbat skal planlægge efter.

Predbat planlægger batteriet efter det forbrug den forventer. Indtil den
11. november lærte dens ML det af husets måler plus pillefyrets varme
omregnet til strøm (``pelletload``): varmen talte altid som strøm, uanset
hvem der lavede den. Så holdt Predbat 7 kWh tilbage til aftenen - solgte
ned til 37 % i stedet for 20 % kl. 17 til 3,29 - og bagefter valgte
varmeopt pillefyr, fordi strømmen nu kun var værd hvad den ville koste at
købe kl. 22. Reservationen blev aldrig brugt til varme.

Her siger varmeopt i stedet hvad varmepumpen faktisk kommer til at bruge.
Predbat trækker varmepumpens målte strøm ud af sin ML-historik
(``iboost_energy_today``) og lægger den her oveni sin prognose
(``load_forecast``). Begge dele er efterprøvet i Predbats kode, se README.

**Formatet** er PredHeats: en liste af ``{"last_updated", "energy"}``, hvor
energien er kumuleret. Predbat bruger kun tilvæksten, så kurven starter ved
0 i dette øjeblik.

**Rundgangen.** Varmeopts valg bygger på Predbats priser, og Predbats priser
bygger på den last varmeopt melder. Uden dæmpning skubber de til hinanden
hvert femte minut. En halvtime skifter derfor først kilde i prognosen, når
varmeprisen er krydset helt over hysteresebåndet om pillefyrets pris *og*
to Predbat-beregninger i træk har sagt det samme - samme regel som
``control_confirm_plans`` for styringen.

**Fejl er skæve.** Mangler varmepumpen i Predbats prognose, sælger Predbat
batteriet tomt, og så kører pumpen på genkøbt strøm. Står den der for
meget, holder Predbat lidt for meget tilbage, og det koster næsten intet.
Derfor er tilbagefaldet *varmepumpe overalt*, ikke nul: bag projektionen,
uden plan, når styringen er slået fra (så kører Node-RED pumpen), og når
add-on'en stopper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

SLOT_MINUTES = 30
SLOT_SECONDS = SLOT_MINUTES * 60

# Predbat planlægger 48 timer og en halv frem, og bag sidste punkt sætter
# den tilvæksten til 0. Halvtreds timer rækker hen over det.
HORIZON_SLOTS = 100

# Pumpens elektronik i ro. Den står med i UVR'ens dagsforbrug, og den bliver
# derfor trukket ud af Predbats historik sammen med resten - så den skal
# også være her, ellers mangler den i Predbats plan.
STANDBY_KW = 0.07

# Kendes COP'en slet ikke, regnes der med den her. Det er et skøn, men et
# forsigtigt et: hellere for meget strøm i Predbats plan end for lidt.
FALLBACK_COP = 3.5

VP = "varmepumpe"
PELLET = "pillefyr"

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
class _Held:
    """Den kilde en halvtime står med i prognosen, og et skifte der venter."""

    source: str
    pending: str | None = None
    seen: set[Any] = field(default_factory=set)


class SourceMemory:
    """Kilden pr. halvtime på væguret - på tværs af cyklusser.

    Projektionen husker intet fra den ene cyklus til den næste; dens
    hysterese går fra række til række. Prognosen skal kunne holde fast i
    hvad den sagde sidst, ellers er der ingen dæmpning at tale om.
    """

    def __init__(self, band: float = 0.05, confirm: int = 2) -> None:
        self.band = max(0.0, band)
        self.confirm = max(1, int(confirm))
        self._held: dict[float, _Held] = {}
        self._cycle = 0

    def update(
        self,
        now_slot: float,
        rows: list[tuple[float, str, float | None]],
        pellet_price: float,
        plan_stamp: Any = None,
    ) -> dict[float, str]:
        """Læg projektionens svar ind, og giv kilden pr. halvtime tilbage.

        ``rows`` er ``(halvtimens start, kilde, varmepris)``. Uden et
        stempel fra Predbat tæller hver cyklus som en ny beregning.
        """
        self._cycle += 1
        stamp = plan_stamp if plan_stamp is not None else ("cyklus", self._cycle)
        for at in [at for at in self._held if at < now_slot]:
            del self._held[at]

        for at, source, heat in rows:
            held = self._held.get(at)
            if held is None:
                self._held[at] = _Held(source)
                continue
            if source == held.source or not self._crossed(source, heat, pellet_price):
                held.pending, held.seen = None, set()
                continue
            if held.pending != source:
                held.pending, held.seen = source, set()
            held.seen.add(stamp)
            if len(held.seen) >= self.confirm:
                self._held[at] = _Held(source)
        return {at: held.source for at, held in self._held.items()}

    def _crossed(self, source: str, heat: float | None, pellet: float) -> bool:
        """Er varmeprisen helt over på den anden side af båndet?"""
        if not _finite(heat):
            return False
        if source == VP:
            return heat < pellet - self.band
        return heat > pellet + self.band


@dataclass(frozen=True)
class Inputs:
    """Det prognosen regnes af. Alt er opslag på planens minutter."""

    now: float
    sources: dict[float, str]
    demand_kw_at: Callable[[int], float | None]
    cop_at: Callable[[int], float | None]
    charge_cop_at: Callable[[int], float | None]
    vessel_kwh: Callable[[float, float], float | None]
    acting: bool
    fallback_demand_kw: float | None = None
    cop_now: float | None = None
    hp_power_kw: float | None = None
    charge_window: tuple[int, int] | None = None
    charge_kw: float | None = None


@dataclass(frozen=True)
class Forecast:
    """Strømmen pr. halvtime, og det Predbat skal have."""

    start: float
    slots: list[tuple[float, str, float]]
    reason: str

    def external(self) -> list[dict[str, Any]]:
        """PredHeats format: kumuleret kWh fra nu, ét punkt pr. halvtime."""
        points = [{"last_updated": _stamp(self.start), "energy": 0.0}]
        total = 0.0
        for at, _, kwh in self.slots:
            total += kwh
            points.append({"last_updated": _stamp(at + SLOT_SECONDS), "energy": round(total, 3)})
        return points

    def kwh_within(self, hours: float) -> float:
        until = self.start + hours * 3600
        return sum(kwh for at, _, kwh in self.slots if at < until)


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime(TIME_FORMAT)


def build(inputs: Inputs) -> Forecast:
    """Varmepumpens strøm pr. halvtime, 50 timer frem.

    Pr. halvtime: på varmepumpe husets varmebehov ÷ COP plus det
    beholderen og spaen tager ud af lageret ÷ COP'en ved ladetemperatur; i
    en opladningsblok blokkens egen ladeeffekt ÷ lade-COP; på pillefyr kun
    standby. Den halvtime vi står i, regnes kun fra nu, og med det målte
    elforbrug når det kendes.
    """
    base = math.floor(inputs.now / SLOT_SECONDS) * SLOT_SECONDS
    elapsed = (inputs.now - base) / SLOT_SECONDS
    slots: list[tuple[float, str, float]] = []
    reason = "projektionen" if inputs.acting else "tilbagefald: styringen er ikke koblet til"
    for index in range(HORIZON_SLOTS):
        at = base + index * SLOT_SECONDS
        minutes = index * SLOT_MINUTES
        source = inputs.sources.get(at, VP) if inputs.acting else VP
        kwh = _slot_kwh(inputs, at, minutes, source)
        if index == 0:
            remaining = max(0.0, 1.0 - elapsed)
            if _finite(inputs.hp_power_kw):
                kwh = max(inputs.hp_power_kw, STANDBY_KW) * SLOT_MINUTES / 60
            kwh *= remaining
        slots.append((at, source, kwh))
    return Forecast(start=inputs.now, slots=slots, reason=reason)


def fallback(inputs: Inputs) -> Forecast:
    """Varmepumpe overalt - når varmeopt ikke står inde for andet."""
    forecast = build(replace(inputs, acting=False, charge_window=None, hp_power_kw=None))
    return Forecast(forecast.start, forecast.slots, "tilbagefald: add-on'en er stoppet")


def _slot_kwh(inputs: Inputs, at: float, minutes: int, source: str) -> float:
    hours = SLOT_MINUTES / 60
    standby = STANDBY_KW * hours
    cop = _first(inputs.cop_at(minutes) if minutes else None, inputs.cop_now, FALLBACK_COP)
    charge_cop = _first(inputs.charge_cop_at(minutes), cop)

    window = inputs.charge_window
    if window is not None and window[0] <= minutes < window[1] and _finite(inputs.charge_kw):
        return max(inputs.charge_kw / charge_cop * hours, standby)

    if source != VP:
        return standby

    demand = _first(inputs.demand_kw_at(minutes), inputs.fallback_demand_kw, 0.0)
    vessel = inputs.vessel_kwh(at, hours)
    kwh = max(0.0, demand) / cop * hours
    if _finite(vessel) and vessel > 0:
        kwh += vessel / charge_cop
    return max(kwh, standby)


def _first(*values: Any) -> float:
    """Den første brugbare værdi. COP'er skal være positive."""
    for value in values:
        if _finite(value) and value > 0:
            return float(value)
    return float(values[-1])
