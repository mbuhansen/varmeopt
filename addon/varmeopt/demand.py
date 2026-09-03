"""Effektbalancen omkring lageret: hvad der går ind, og hvad huset trækker ud.

Lagerets fyldning alene kan man ikke handle på. 11,6 kWh i tankene er to timer
eller tyve — det afhænger af forbruget, og uden det tal er «lad op nu» et gæt.
Centralvarmens frem og retur sidder efter tankene med en flowmåler imellem, og
sammen giver de husets varmebehov direkte i kilowatt.

På indgangssiden er der fire kilder: varmepumpe, solvarme, elpatroner og
pillefyr. De skal holdes adskilt, for varme fra solen er gratis, og varme fra
varmepumpen er det ikke. Uden den opdeling kan man ikke skelne «varmepumpen
kørte godt» fra «solen skinnede» — og en blokplan der lader tankene op med
varmepumpen lige før solen står op, fortrænger den gratis varme med købt.

Formlen er efterprøvet mod anlæggets eget display: 130 l/h ved 18,2 K giver
2,72 kW her, hvor UVR'en skriver 2,73.

De fire kilder er ikke lige sikkert målt, og det skal man vide inden man
undrer sig over at regnskabet ikke går op:

- **Husets forbrug** har en rigtig flowmåler, men den har en bund: under
  omkring 100 l/h kan den vise nul selv om der løber vand. Et nul betyder
  altså ikke «intet forbrug» — det betyder «højst 100 l/h», og det er ikke
  det samme. Derfor er behovet *ukendt* under den grænse, ikke nul.
- **Solvarmen** har ikke. Produktionen er en flowkurve lagt ind i UVR'en, som
  følger pumpens PWM-signal og et analogt flow — der er ingen digital
  flowmåler. Værdien er altså modelleret, ikke målt, og en skævhed i den
  kurve slår direkte igennem i balancen.
- **Varmepumpen** regnes af elforbrug gange målt COP.
- **Pillefyr og elpatron** melder selv deres effekt.

Går regnskabet ikke op mod tankenergiens ændring, er solvarmen derfor den
første mistænkte — ikke den sidste. Til gengæld kan netop den forskel en dag
bruges til at kalibrere kurven: over en solrig time med varmepumpen slukket
*er* tankenes energistigning plus forbruget lig solvarmen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .tank import WH_PER_LITER_K


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def thermal_kw(litres_per_hour: float | None, delta_k: float | None) -> float | None:
    """Effekt af en vandstrøm med et givet temperaturfald."""
    if not _finite(litres_per_hour) or not _finite(delta_k):
        return None
    return litres_per_hour * delta_k * WH_PER_LITER_K / 1000


@dataclass(frozen=True)
class Load:
    """Husets forbrug, målt efter tankene."""

    flow: float | None = None
    ret: float | None = None
    litres_per_hour: float | None = None
    # Målerens bund. Under den kan den vise nul selv om der løber vand, så
    # aflæsningen siger «højst så meget» og ikke «så meget».
    meter_floor: float = 100.0

    @property
    def delta(self) -> float | None:
        if not _finite(self.flow) or not _finite(self.ret):
            return None
        return self.flow - self.ret

    @property
    def kw(self) -> float | None:
        """Varmebehovet nu, eller None når måleren ikke kan svare.

        Et negativt fald ville betyde at returen er varmere end fremløbet —
        det sker ved stilstand og småfejl på følerne, og det er ikke et
        forbrug. Så er svaret nul, ikke et negativt behov.

        Under målerens bund er svaret *ukendt*, ikke nul og ikke et lille
        tal. Det gælder begge fejl: en måler der hænger på 5 l/h gav før
        0,06 kW, som ser ud som et rigtigt forbrug — 101 timers restlevetid
        på lageret, og en planlægger der roligt lod være med at gøre noget.
        Og et nul fra en måler der først tæller fra 100 l/h er ikke et nul,
        det er «højst 100 l/h», hvilket ved 15 K er op mod 1,7 kW.
        """
        if not self.trustworthy:
            return None
        power = thermal_kw(self.litres_per_hour, self.delta)
        return None if power is None else max(0.0, power)

    @property
    def trustworthy(self) -> bool:
        """Er flowaflæsningen over målerens bund?

        Grænsen er målerens egenskab, ikke anlæggets. Denne måler kan vise
        nul ved reelle strømme op mod 100 l/h, så alt derunder — nul
        inklusive — er en aflæsning vi ikke kan regne på.
        """
        return _finite(self.litres_per_hour) and self.litres_per_hour >= self.meter_floor

    @property
    def circulating(self) -> bool:
        """Løber der vand vi kan måle? Bevaret navn; se ``trustworthy``."""
        return self.trustworthy


@dataclass(frozen=True)
class Balance:
    """Hvad der lades ind i lageret, mod hvad huset tager ud."""

    load: Load
    solar_kw: float | None = None
    element_kw: float | None = None
    heatpump_kw: float | None = None
    boiler_kw: float | None = None

    @property
    def sources(self) -> dict[str, float]:
        """Kun de kilder der faktisk måler noget lige nu."""
        named = {
            "varmepumpe": self.heatpump_kw,
            "solvarme": self.solar_kw,
            "elpatron": self.element_kw,
            "pillefyr": self.boiler_kw,
        }
        return {k: v for k, v in named.items() if _finite(v) and v > 0.05}

    @property
    def input_kw(self) -> float:
        return sum(self.sources.values())

    @property
    def free_kw(self) -> float:
        """Den del af tilførslen der ikke koster noget: solvarmen."""
        return self.sources.get("solvarme", 0.0)

    @property
    def net_kw(self) -> float | None:
        """Positiv: lageret fyldes. Negativ: det tømmes."""
        if self.load.kw is None:
            return None
        return self.input_kw - self.load.kw

    def hours_left(self, stored_kwh: float | None) -> float | None:
        """Hvor længe rækker lageret, hvis det bliver ved som nu?"""
        net = self.net_kw
        if net is None or net >= -0.05 or not _finite(stored_kwh):
            return None
        return stored_kwh / -net

    def hours_to_full(self, headroom_kwh: float | None) -> float | None:
        """Hvor længe til lageret er fuldt, hvis det bliver ved som nu?"""
        net = self.net_kw
        if net is None or net <= 0.05 or not _finite(headroom_kwh):
            return None
        return headroom_kwh / net
