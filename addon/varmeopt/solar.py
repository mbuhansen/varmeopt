"""Solvarmeudbytte forudsagt af Solcasts PV-prognose.

Solfangerne og solcellerne ser den samme sol, men ikke fra samme vinkel: fire
paneler i syd med 45° hældning mod 6,4 kW syd/20° plus 4 kW vest/15°. Den
forskel er ikke lille. Regnet på indfaldsvinklen over året svinger forholdet
mellem de to flader med en faktor 2,5 — fra 0,90 i juni til 2,25 i december,
fordi 45° fanger den lave vintersol langt bedre end 20° gør.

En fast omregningsfaktor ville derfor være groft forkert det halve af året.
Men **geometrien kan regnes, ikke læres.** Tilbage står ét enkelt tal: en
skalafaktor der dækker kollektorareal, virkningsgrad og Solcasts egen skævhed.
Årstidsformen kommer gratis fra matematikken, og modellen skal derfor kun lære
én værdi i stedet for tolv — dage i stedet for et år.

Der er én fælde i læringen. Solfangerens udbytte er begrænset af
tanktemperaturen, ikke kun af solen: er lageret fuldt, stagnerer kollektoren og
laver ingenting, uanset vejret. Lærer man af sådan en dag, lærer man «solvarmen
er dårlig», når sandheden er «der var ikke plads». Derfor læres kun fra dage
hvor lageret havde plads hele vejen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Læringen er med vilje skæv, og det er den vigtigste beslutning i modulet.
#
# En dag hvor lageret var fyldt, får kollektoren til at holde igen, og
# målingen bliver for lav. En dag kan derimod aldrig komme til at *yde mere*
# end solen gav. Fejlen er altså ensidig: et højt udbytte er ægte information,
# et lavt kan lige så godt være en fuld tank som en grå himmel.
#
# Derfor tror vi hurtigt på en god dag og kun langsomt på en dårlig. Det gør
# en saturationsdetektor overflødig — asymmetrien håndterer det selv. Målt på
# to rigtige dage i august, hvor den ene var reguleret: symmetrisk læring gav
# 0,397, asymmetrisk gav 0,422, og sandheden fra den frie dag var 0,428.
_ALPHA_UP = 0.5
_ALPHA_DOWN = 0.05

_STEPS_PER_DAY = 288  # 5-minutters skridt


@dataclass(frozen=True)
class Plane:
    """En flade: hældning, orientering og hvor meget den vejer."""

    tilt: float
    azimuth: float  # 0 = syd, positiv mod vest
    weight: float = 1.0


def daily_incidence(day_of_year: int, latitude: float, plane: Plane) -> float:
    """Summen af cos(indfaldsvinkel) over dagens lyse timer.

    Diffus stråling og atmosfære er udeladt med vilje. Vi sammenligner to
    flader samme sted samme dag, så det fælles går ud, og tilbage står præcis
    det hældning og orientering betyder.
    """
    declination = 23.45 * math.sin(math.radians(360 * (284 + day_of_year) / 365))
    dec = math.radians(declination)
    lat = math.radians(latitude)
    tilt = math.radians(plane.tilt)
    azi = math.radians(plane.azimuth)

    total = 0.0
    for step in range(_STEPS_PER_DAY):
        omega = math.radians(15 * (step * 24 / _STEPS_PER_DAY - 12))

        elevation = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(omega)
        if elevation <= 0:
            continue

        cos_theta = (
            math.sin(dec) * math.sin(lat) * math.cos(tilt)
            - math.sin(dec) * math.cos(lat) * math.sin(tilt) * math.cos(azi)
            + math.cos(dec) * math.cos(lat) * math.cos(tilt) * math.cos(omega)
            + math.cos(dec) * math.sin(lat) * math.sin(tilt) * math.cos(azi) * math.cos(omega)
            + math.cos(dec) * math.sin(tilt) * math.sin(azi) * math.sin(omega)
        )
        if cos_theta > 0:
            total += cos_theta * (24 / _STEPS_PER_DAY)
    return total


@dataclass(frozen=True)
class Geometry:
    """Solfangerens flade mod solcellernes, på et givet sted."""

    latitude: float
    thermal: Plane
    pv: tuple[Plane, ...]

    def ratio(self, day_of_year: int) -> float | None:
        """Hvor meget mere sol solfangeren ser end solcellerne, den dag.

        Over 1 betyder at solfangeren er bedst stillet — det er den om
        vinteren, hvor dens 45° møder den lave sol nær vinkelret.
        """
        pv_weight = sum(p.weight for p in self.pv)
        if pv_weight <= 0:
            return None
        pv = sum(daily_incidence(day_of_year, self.latitude, p) * p.weight for p in self.pv)
        pv /= pv_weight
        if pv <= 0:
            return None
        return daily_incidence(day_of_year, self.latitude, self.thermal) / pv


class SolarModel:
    """Skalafaktoren mellem forudsagt PV og faktisk solvarme."""

    def __init__(self, geometry: Geometry, scale: float | None = None, days: float = 0.0) -> None:
        self.geometry = geometry
        self.scale = scale
        self.days = days
        self._cache: dict[int, float | None] = {}

    @property
    def known(self) -> bool:
        return self.scale is not None and self.days > 0

    def geometric_ratio(self, day_of_year: int) -> float | None:
        # Formen ændrer sig kun fra dag til dag, så den regnes én gang.
        if day_of_year not in self._cache:
            self._cache[day_of_year] = self.geometry.ratio(day_of_year)
        return self._cache[day_of_year]

    def expected_kwh(self, pv_forecast_kwh: float | None, day_of_year: int) -> float | None:
        """Forventet solvarme af en PV-prognose. None indtil der er lært."""
        if self.scale is None or pv_forecast_kwh is None or pv_forecast_kwh < 0:
            return None
        ratio = self.geometric_ratio(day_of_year)
        if ratio is None:
            return None
        return pv_forecast_kwh * ratio * self.scale

    def learn(
        self,
        thermal_kwh: float,
        pv_forecast_kwh: float,
        day_of_year: int,
        store_was_full: bool = False,
    ) -> str:
        """Indarbejd et fuldt døgn. Returnerer en status der kan logges."""
        if store_was_full:
            return "ignoreret: lageret var fuldt - solen fik ikke lov"
        if pv_forecast_kwh is None or pv_forecast_kwh <= 0.5:
            return "ignoreret: for lidt sol til at sige noget"
        if thermal_kwh is None or thermal_kwh < 0:
            return "ignoreret: ugyldigt solvarmeudbytte"

        ratio = self.geometric_ratio(day_of_year)
        if ratio is None or ratio <= 0:
            return "ignoreret: kan ikke regne geometrien"

        observed = thermal_kwh / (pv_forecast_kwh * ratio)
        if self.scale is None:
            self.scale = observed
            self.days = 1.0
            return f"foerste dag: skalafaktor {observed:.3f}"

        self.days += 1
        rising = observed > self.scale
        alpha = _ALPHA_UP if rising else _ALPHA_DOWN
        self.scale = self.scale * (1 - alpha) + observed * alpha
        retning = "op" if rising else "ned"
        return (
            f"skalafaktor {self.scale:.3f} ({retning}, dag {self.days:.0f}, "
            f"i dag {observed:.3f})"
        )

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        return {"scale": self.scale, "days": self.days}

    @classmethod
    def from_raw(cls, raw: Any, geometry: Geometry) -> SolarModel:
        scale = days = None
        if isinstance(raw, dict):
            try:
                value = raw.get("scale")
                scale = float(value) if value is not None else None
                days = float(raw.get("days", 0))
            except (TypeError, ValueError):
                scale = days = None
        if scale is not None and not math.isfinite(scale):
            scale = None
        return cls(geometry, scale=scale, days=days or 0.0)
