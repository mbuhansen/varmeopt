"""Varmelageret: to 500 L tanke i parallel, otte følere.

Seks følere sidder i dybden, tre i hver tank. Det er dem der siger *hvor meget*
varme der er tilbage, og hvor meget plads der er til at lade op — en enkelt
temperatur kan ikke svare på det, for en tank med 60 °C i toppen og 30 °C i
bunden rummer noget helt andet end en der er 60 °C hele vejen ned.

De sidste to sidder på afgangsrørene i toppen og måler noget andet: om tanken
kan levere lige *nu*. Brugsvand og spa kræver omkring 56 °C ved afgangen,
radiatorkredsen kun godt 31 °C. Lagdeling og leveringsevne er to forskellige
spørgsmål, og de to slags følere svarer på hver sit.

Der styres intet herfra. Modulet regner tal, som fase 1 kan vurderes på.
"""

from __future__ import annotations

from dataclasses import dataclass

# Vand ved ca. 50 °C: 4,186 kJ/(kg·K) ÷ 3600 = 1,163 Wh/(kg·K), tæthed
# 0,988 kg/L. Over hele driftsbåndet 30-70 °C varierer produktet under 1,5 %,
# hvilket er langt under usikkerheden på at tre følere repræsenterer en tank.
WH_PER_LITER_K = 1.149


@dataclass(frozen=True)
class Tank:
    """Én tank med tre dybdefølere og en føler på afgangsrøret."""

    name: str
    liters: float
    top: float | None
    mid: float | None
    bottom: float | None
    outlet: float | None = None

    @property
    def layers(self) -> tuple[float, ...]:
        """De lagtemperaturer vi faktisk har, øverst først."""
        return tuple(t for t in (self.top, self.mid, self.bottom) if t is not None)

    @property
    def covered(self) -> bool:
        return bool(self.layers)

    @property
    def _liters_per_layer(self) -> float:
        """Tankens volumen fordelt på de lag vi har målt.

        En føler der er faldet ud må ikke tælle med som 0 °C — det ville se ud
        som om en tredjedel af tanken var iskold. I stedet lader vi de
        resterende lag dække hele volumenet: estimatet bliver grovere, men det
        peger ikke pludselig helt galt.
        """
        return self.liters / len(self.layers) if self.layers else 0.0

    def stored_kwh(self, reference: float) -> float:
        """Energi over referencetemperaturen — den varme der reelt kan bruges."""
        per = self._liters_per_layer
        wh = sum(per * max(0.0, t - reference) for t in self.layers)
        return wh * WH_PER_LITER_K / 1000

    def headroom_kwh(self, ceiling: float) -> float:
        """Hvor meget mere der kan lagres, før loftet er nået."""
        per = self._liters_per_layer
        wh = sum(per * max(0.0, ceiling - t) for t in self.layers)
        return wh * WH_PER_LITER_K / 1000

    @property
    def mean_temp(self) -> float | None:
        return sum(self.layers) / len(self.layers) if self.layers else None

    @property
    def spread(self) -> float | None:
        """Lagdelingen: top minus bund.

        Et stort spænd betyder skarp lagdeling — toppen kan levere varmt vand,
        mens bunden stadig tager imod. Et spænd nær nul betyder enten en fuldt
        opladet tank eller en gennemblandet én, og de to skal ikke forveksles:
        se på middeltemperaturen for at skelne.
        """
        if self.top is None or self.bottom is None:
            return None
        return self.top - self.bottom

    @property
    def deliverable(self) -> float | None:
        """Den temperatur tanken kan levere nu — afgangsrøret, ellers toppen."""
        return self.outlet if self.outlet is not None else self.top


@dataclass(frozen=True)
class Buffer:
    """Hele lageret: alle tanke set under ét."""

    tanks: tuple[Tank, ...]
    reference: float
    ceiling: float
    # Solvarmen og ACthors elpatroner kan begge presse tankene helt op til
    # 90 °C, langt over hvad varmepumpen kan levere. De to lofter svarer på
    # hver sit spørgsmål: hvor meget *varmepumpen* kan nå at tilføre, og hvor
    # meget der overhovedet er plads til. Det første styrer en blokplan; det
    # andet siger om der stadig er et sted at gøre af gratis eller overskydende
    # varme.
    peak_ceiling: float = 90.0

    @property
    def measured(self) -> tuple[Tank, ...]:
        return tuple(t for t in self.tanks if t.covered)

    @property
    def covered(self) -> bool:
        return bool(self.measured)

    @property
    def sensor_count(self) -> int:
        return sum(len(t.layers) for t in self.tanks)

    @property
    def stored_kwh(self) -> float:
        return sum(t.stored_kwh(self.reference) for t in self.measured)

    @property
    def headroom_kwh(self) -> float:
        """Hvor meget varmepumpen kan nå at tilføre, før den løber tør for løft."""
        return sum(t.headroom_kwh(self.ceiling) for t in self.measured)

    @property
    def peak_headroom_kwh(self) -> float:
        """Hvor meget der fysisk er plads til — det solvarme og ACthor kan nå."""
        return sum(t.headroom_kwh(self.peak_ceiling) for t in self.measured)

    @property
    def above_heatpump_ceiling(self) -> bool:
        """Er lageret allerede varmere end varmepumpen kan levere?

        Sker det, har solvarmen eller elpatronerne fyldt tankene forbi
        varmepumpens rækkevidde, og en blokopladning ville ikke bare være
        unødvendig — den ville være umulig.
        """
        return self.covered and self.headroom_kwh <= 0.01

    @property
    def charge_percent(self) -> float | None:
        """Hvor fuldt lageret er, mellem reference og loft."""
        total = self.stored_kwh + self.headroom_kwh
        return 100 * self.stored_kwh / total if total > 0 else None

    @property
    def mean_temp(self) -> float | None:
        """Volumenvægtet middeltemperatur over de målte tanke."""
        tanks = self.measured
        if not tanks:
            return None
        liters = sum(t.liters for t in tanks)
        if liters <= 0:
            return None
        return sum(t.mean_temp * t.liters for t in tanks) / liters  # type: ignore[operator]

    @property
    def imbalance(self) -> float | None:
        """Største forskel i middeltemperatur mellem to tanke.

        To parallelle tanke bør lagdele ens. Gør de det ikke, er det ikke
        varmen der er skæv, men flowet — skæv fordeling eller en ventil der
        ikke gør sit arbejde. Tallet er gratis at føre med, og det er den
        slags fejl man ellers først opdager om vinteren.
        """
        means = [t.mean_temp for t in self.measured if t.mean_temp is not None]
        return max(means) - min(means) if len(means) > 1 else None

    def can_deliver(self, required: float) -> bool:
        """Kan mindst én tank levere den temperatur lige nu?"""
        temps = [t.deliverable for t in self.tanks if t.deliverable is not None]
        return any(t >= required for t in temps)

    @property
    def deliverable(self) -> float | None:
        """Den varmeste afgang vi har — det bedste lageret kan levere nu."""
        temps = [t.deliverable for t in self.tanks if t.deliverable is not None]
        return max(temps) if temps else None
