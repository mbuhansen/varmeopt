"""Solvarmeudbytte forudsagt af Solcasts PV-prognose.

Solfangerne og solcellerne ser den samme sol, men ikke fra samme vinkel: fire
paneler i syd med 45° hældning mod 6,4 kW syd/20° plus 4 kW vest/15°. Den
forskel kan **regnes, ikke læres.** Tilbage står ét enkelt tal: en skalafaktor
der dækker kollektorareal, virkningsgrad og Solcasts egen skævhed. Årstidsformen
kommer gratis fra matematikken, og modellen skal derfor kun lære én værdi i
stedet for tolv — dage i stedet for et år.

**Diffus stråling hører med.** Solcast forudsiger ikke solhøjden, men hvad
panelerne producerer, og på 55,4° nord kommer over halvdelen af den energi fra
den diffuse himmel — om vinteren fire femtedele. Diffus stråling rammer ikke
fra en retning, så den ser kun hvor stor en del af himlen fladen vender mod:
udsynsfaktoren (1+cos β)/2, som er 0,85 for 45° og 0,97 for 20°. Næsten ens.

Regnes kun den direkte stråling, svinger forholdet mellem de to flader med en
faktor 2,5 over året. Med den diffuse regnet med er svingningen 1,47, og
december falder fra 2,27 til 1,31. Forskellen er ikke akademisk: den gamle
udgave lovede planlæggeren 74 % mere solvarme end anlægget kan levere, netop
i den måned hvor et forkert løfte er dyrest.

Der er én fælde i læringen. Solfangerens udbytte er begrænset af
tanktemperaturen, ikke kun af solen: er lageret fyldt helt op, stagnerer
kollektoren, uanset vejret. Lærer man af sådan en dag, lærer man «solvarmen er
dårlig», når sandheden er «der var ikke plads». Derfor læres kun fra dage hvor
lageret havde plads hele vejen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Læringen er symmetrisk. Det var den ikke før: en god dag blev troet med
# alfa 0,5 og en dårlig med 0,05, for at en dag med fyldt lager ikke skulle
# trække skalafaktoren ned.
#
# Men mætning håndteres allerede af ``store_was_full``, som kasserer sådan en
# dag helt. Asymmetrien var altså den samme rettelse en gang til — og en
# skæv EMA er ikke en filtrering, den er en systematisk fejl: hvert udsving
# opad blev troet ti gange så meget som det tilsvarende nedad, så selv med
# symmetrisk støj omkring den sande værdi lagde estimatet sig for højt — 12 %
# ved lille dag-til-dag-spredning, 24 % ved realistisk, 37 % ved stor.
#
# Begrundelsen dengang var to augustdage hvor den ene var reguleret. Den dag
# skulle have været filtreret fra, ikke udglattet.
_ALPHA = 0.15

_STEPS_PER_DAY = 288  # 5-minutters skridt

# Diffusandelen af den globale stråling på disse breddegrader, glattet over
# året: omkring 0,52 midt om sommeren og 0,85 ved vintersolhverv. Den behøver
# ikke være præcis — den bestemmer kun *formen* på årstidsvariationen, og
# resten samler skalafaktoren op.
_DIFFUSE_MEAN = 0.685
_DIFFUSE_SWING = 0.165

# Hvor meget jorden kaster tilbage. Sne ville give mere, men det er få dage.
_GROUND_ALBEDO = 0.2

# Skalafaktoren maales mod geometrien, saa den betyder kun noget saa laenge
# geometrien er den samme. Version 2 tog diffus straaling med; et tal lært
# under version 1 er malt med en anden malestok og kastes vaek.
MODEL_VERSION = 2


def diffuse_fraction(day_of_year: int) -> float:
    """Hvor stor en del af strålingen der kommer fra himlen frem for solskiven."""
    phase = 2 * math.pi * (day_of_year - 172) / 365
    return _DIFFUSE_MEAN - _DIFFUSE_SWING * math.cos(phase)


@dataclass(frozen=True)
class Plane:
    """En flade: hældning, orientering og hvor meget den vejer."""

    tilt: float
    azimuth: float  # 0 = syd, positiv mod vest
    weight: float = 1.0


def daily_irradiance(day_of_year: int, latitude: float, plane: Plane) -> float:
    """Dagens samlede indstråling på fladen, i vilkårlige enheder.

    Tre bidrag: solskiven gennem indfaldsvinklen, himlen gennem udsynsfaktoren
    og jorden gennem det den kaster tilbage. Atmosfærens dæmpning er udeladt —
    vi sammenligner to flader samme sted samme dag, så det fælles går ud.

    Den globale stråling sættes proportional med solhøjden. Det er groft, men
    det er den *relative* fordeling mellem direkte og diffus over døgnet der
    betyder noget her, ikke niveauet, og niveauet kommer fra Solcast.
    """
    dec = math.radians(23.45 * math.sin(math.radians(360 * (284 + day_of_year) / 365)))
    lat = math.radians(latitude)
    tilt = math.radians(plane.tilt)
    azi = math.radians(plane.azimuth)

    sky_view = (1 + math.cos(tilt)) / 2
    ground_view = (1 - math.cos(tilt)) / 2
    kd = diffuse_fraction(day_of_year)
    hours = 24 / _STEPS_PER_DAY

    total = 0.0
    for step in range(_STEPS_PER_DAY):
        omega = math.radians(15 * (step * 24 / _STEPS_PER_DAY - 12))

        sin_elev = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(omega)
        # Lige over horisonten bliver den direkte stråling numerisk ustabil
        # (divisionen med solhøjden), og bidraget er alligevel forsvindende.
        if sin_elev <= 0.02:
            continue

        global_h = sin_elev
        diffuse_h = kd * global_h
        beam_normal = (global_h - diffuse_h) / sin_elev

        cos_theta = (
            math.sin(dec) * math.sin(lat) * math.cos(tilt)
            - math.sin(dec) * math.cos(lat) * math.sin(tilt) * math.cos(azi)
            + math.cos(dec) * math.cos(lat) * math.cos(tilt) * math.cos(omega)
            + math.cos(dec) * math.sin(lat) * math.sin(tilt) * math.cos(azi) * math.cos(omega)
            + math.cos(dec) * math.sin(tilt) * math.sin(azi) * math.sin(omega)
        )

        total += (
            beam_normal * max(0.0, cos_theta)
            + diffuse_h * sky_view
            + global_h * _GROUND_ALBEDO * ground_view
        ) * hours
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
        pv = sum(daily_irradiance(day_of_year, self.latitude, p) * p.weight for p in self.pv)
        pv /= pv_weight
        if pv <= 0:
            return None
        return daily_irradiance(day_of_year, self.latitude, self.thermal) / pv


# Prognosen for et døgn skal fanges før solen står op, ellers er "resten af
# dagen" ikke hele dagen. Starter add-on'en klokken to om eftermiddagen, må
# den dag ikke bruges til læring.
MORNING_HOUR = 5


@dataclass
class DayTracker:
    """Holder styr på et døgn ad gangen, så en dag kan læres når den er slut.

    Solvarmens dagstæller nulstilles ved midnat, og Solcasts "resten af dagen"
    er kun hele dagen hvis man spørger inden solopgang. Begge dele gør at et
    døgn først kan gøres op *efter* det er forbi, med tal man huskede undervejs.
    """

    date: str | None = None
    forecast_kwh: float | None = None
    forecast_hour: int | None = None
    thermal_kwh: float | None = None
    saturated: bool = False

    def observe(
        self,
        date: str,
        hour: int,
        forecast_remaining: float | None,
        thermal_today: float | None,
        store_full: bool = False,
    ) -> tuple[float, float, str, bool] | None:
        """Returnerer (solvarme, prognose, dato, maettet) når et døgn er slut.

        ``store_full`` skal aflæses *undervejs*, ikke ved døgnskiftet — ved
        midnat er tankene kølet af, og en dag hvor solen stod og bankede mod
        et fuldt lager ville se helt normal ud.
        """
        finished = None

        if self.date != date:
            if (
                self.date is not None
                and self.thermal_kwh is not None
                and self.forecast_kwh is not None
                and self.forecast_hour is not None
                and self.forecast_hour <= MORNING_HOUR
            ):
                finished = (self.thermal_kwh, self.forecast_kwh, self.date, self.saturated)

            self.date = date
            self.forecast_kwh = forecast_remaining
            self.forecast_hour = hour
            self.thermal_kwh = None
            self.saturated = False

        if thermal_today is not None:
            self.thermal_kwh = thermal_today
        if store_full:
            self.saturated = True

        return finished

    def to_raw(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "forecast_kwh": self.forecast_kwh,
            "forecast_hour": self.forecast_hour,
            "thermal_kwh": self.thermal_kwh,
            "saturated": self.saturated,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> DayTracker:
        if not isinstance(raw, dict):
            return cls()
        tracker = cls()
        date = raw.get("date")
        tracker.date = str(date) if isinstance(date, str) else None
        tracker.saturated = bool(raw.get("saturated", False))
        for field in ("forecast_kwh", "forecast_hour", "thermal_kwh"):
            value = raw.get(field)
            try:
                setattr(tracker, field, float(value) if value is not None else None)
            except (TypeError, ValueError):
                setattr(tracker, field, None)
        if tracker.forecast_hour is not None:
            tracker.forecast_hour = int(tracker.forecast_hour)
        return tracker


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
        self.scale = self.scale * (1 - _ALPHA) + observed * _ALPHA
        return f"skalafaktor {self.scale:.3f} (dag {self.days:.0f}, i dag {observed:.3f})"

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        return {"model": MODEL_VERSION, "scale": self.scale, "days": self.days}

    @classmethod
    def from_raw(cls, raw: Any, geometry: Geometry) -> SolarModel:
        """Læs det lærte tilbage — men kun hvis det blev lært af samme model.

        Skalafaktoren er defineret som udbytte divideret med *den her*
        geometris forudsigelse. Ændrer geometrien sig, betyder det gemte tal
        ikke længere det samme, og at føre det videre ville være at blande to
        målestokke. Det koster ét døgn at lære forfra. Det er billigere end at
        forudsige forkert i ubestemt tid.
        """
        scale = days = None
        if isinstance(raw, dict) and raw.get("model") == MODEL_VERSION:
            try:
                value = raw.get("scale")
                scale = float(value) if value is not None else None
                days = float(raw.get("days", 0))
            except (TypeError, ValueError):
                scale = days = None
        if scale is not None and not math.isfinite(scale):
            scale = None
        return cls(geometry, scale=scale, days=days or 0.0)
