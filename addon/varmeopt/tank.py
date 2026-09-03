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
#
# **Men UVR'en regner med 1,163** — altså uden tæthedskorrektionen. Prøven
# mod anlægget bekræftede at flow og ΔT læses rigtigt; den kunne ikke skelne
# de to konstanter, for forskellen *er* de 0,01 kW der lå i afvigelsen.
# Vælger man den anden, flytter alle lagertal 1,2 % systematisk. Talet her er
# det fysisk rigtige; det er ikke det samme som anlæggets eget.
WH_PER_LITER_K = 1.149


def _finite(value: float | None) -> bool:
    """NaN er ikke en måling. HA leverer den som en helt almindelig værdi."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


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
    def measured(self) -> tuple[tuple[int, float], ...]:
        """De lag vi faktisk har målt, med deres dybde. 0 er toppen."""
        return tuple(
            (depth, t)
            for depth, t in enumerate((self.top, self.mid, self.bottom))
            if _finite(t)
        )

    @property
    def layers(self) -> tuple[float, ...]:
        """Alle tre lag, øverst først — et manglende yderlag følger gradienten.

        Her stod tidligere kun de målte lag, og de dækkede så hele volumenet
        ligeligt. Det er rigtigt når *midterste* føler falder ud: gennemsnittet
        af top og bund er stadig et fair bud på tanken. Men falder en yderføler
        ud, kommer begge de resterende fra samme ende, og at brede dem ud over
        hele tanken flytter energien groft.

        500 L med 60/50/30 °C over en reference på 30 rummer 9,57 kWh. Dør
        bundføleren, blev de 60 og 50 til hele tanken: 14,36 kWh. Halvdelen
        mere varme end der er, netop når der er mindst grund til at tro på
        tallet.

        I stedet forlænges den lagdeling vi kan se. Med 60 og 50 målt er
        faldet 10 K pr. lag, og bunden bliver 40 — ikke sandheden, men i den
        rigtige retning og i den rigtige størrelsesorden.
        """
        known = self.measured
        if len(known) != 2:
            return tuple(t for _, t in known)

        (d1, t1), (d2, t2) = known
        slope = (t2 - t1) / (d2 - d1)
        missing = ({0, 1, 2} - {d1, d2}).pop()
        filled = t1 + slope * (missing - d1)

        # En tank er varmest foroven. Går forlængelsen den anden vej, er
        # lagdelingen enten vendt om eller en føler er ude af kalibrering, og
        # så er det tætteste målte lag et bedre bud end en fremskrivning.
        nearest = t1 if abs(missing - d1) <= abs(missing - d2) else t2
        if missing == 0:
            filled = max(filled, max(t1, t2))
        elif missing == 2:
            filled = min(filled, min(t1, t2))
        if not 0.0 <= filled <= 100.0:
            filled = nearest

        out = {d1: t1, d2: t2, missing: filled}
        return (out[0], out[1], out[2])

    @property
    def covered(self) -> bool:
        return bool(self.measured)

    @property
    def sensors_lost(self) -> int:
        """Hvor mange af de tre dybdefølere der mangler."""
        return 3 - len(self.measured)

    @property
    def _liters_per_layer(self) -> float:
        """Tankens volumen fordelt på de lag vi regner med.

        En føler der er faldet ud må ikke tælle med som 0 °C — det ville se ud
        som om en tredjedel af tanken var iskold.
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
    # Anlægget lader tankene i rækkefølge, ikke parallelt: en afspærringsventil
    # på tank to åbner først når tank ét er over den her temperatur i toppen.
    # Det er med vilje — solvarmen lader fra bunden af tank ét, og ved kun at
    # varme de første 500 L når lageret hurtigere en brugbar temperatur.
    #
    # Konsekvensen for modellen er ikke energien, som er summen uanset, men
    # hvordan en *ubalance* skal læses: så længe tank ét er under grænsen, er
    # forskellen designet, ikke en fejl. Nul slår kaskaden fra.
    cascade_temp: float = 0.0

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
    def at_peak_ceiling(self) -> bool:
        """Er der fysisk ikke mere plads — heller ikke til gratis varme?

        Det er det spørgsmål solfangeren stiller. Den stagnerer først når
        tankene er helt oppe ved de 90 °C, ikke når de er kommet forbi
        varmepumpens rækkevidde ved 60. Forskellen er 30 K, og de 30 K er
        præcis de bedste soltimer på den bedste soldag.
        """
        return self.covered and self.peak_headroom_kwh <= 0.01

    @property
    def charge_percent(self) -> float | None:
        """Hvor fuldt lageret er i det bånd varmepumpen arbejder i.

        Nævneren er båndets fulde rummelighed, og tælleren tæller kun med
        indtil loftet. Der stod ``stored / (stored + headroom)``, og de to
        tællere måler ikke det samme: energi *over* loftet talte med foroven
        men gav ingen rummelighed forneden. En tank på 90/70/40 med reference
        30 og loft 60 blev til 84,6 % fyldt, hvor det ærlige svar er 78.
        """
        span = self.ceiling - self.reference
        if span <= 0:
            return None
        capacity = 0.0
        usable = 0.0
        for tank in self.measured:
            per = tank.liters / len(tank.layers) if tank.layers else 0.0
            for t in tank.layers:
                capacity += per * span
                usable += per * max(0.0, min(t, self.ceiling) - self.reference)
        return 100 * usable / capacity if capacity > 0 else None

    @property
    def sensors_lost(self) -> int:
        """Hvor mange dybdefølere der mangler på tværs af lageret."""
        return sum(t.sensors_lost for t in self.measured)

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

        Tallet er en kendsgerning; om det er et problem, afgør
        ``imbalance_is_by_design``. På et anlæg med parallelle tanke betyder
        en stor forskel skævt flow. På et anlæg der lader i rækkefølge
        betyder den bare at første tank ikke er fyldt endnu.
        """
        means = [t.mean_temp for t in self.measured if t.mean_temp is not None]
        return max(means) - min(means) if len(means) > 1 else None

    @property
    def cascade_filling(self) -> bool:
        """Er første tank stadig under den temperatur der åbner for de næste?"""
        if self.cascade_temp <= 0 or not self.tanks:
            return False
        first = self.tanks[0]
        return first.top is not None and first.top < self.cascade_temp

    @property
    def imbalance_is_by_design(self) -> bool:
        """Skal forskellen mellem tankene læses som en fejl eller som drift?

        På et anlæg der lader i rækkefølge er tankene *ment* at stå skævt —
        ikke bare mens første tank fyldes, men også et stykke efter ventilen
        har åbnet, mens den anden henter ind. Uden det her ville et anlæg der
        virker som det skal, stå med en permanent advarsel, og en advarsel
        der altid lyser er en advarsel man holder op med at læse.

        Grænsen går ved at *første* tank er ladet så langt varmepumpen kan
        tage den. Er den det, og står den anden stadig langt bagud, er
        rækkefølgen kørt til ende uden at have rettet forskellen op — og så
        er det flowet.

        Det er ikke en tidsmåling, og det er med vilje: en tidsmåling ville
        kræve historik, og den forskel den skulle afgøre er ikke stor nok til
        at bære den kompleksitet.
        """
        if self.cascade_temp <= 0 or not self.tanks:
            return False
        first = self.tanks[0]
        return first.top is None or first.top < self.ceiling

    def can_deliver(self, required: float) -> bool:
        """Kan mindst én tank levere den temperatur lige nu?"""
        temps = [t.deliverable for t in self.tanks if t.deliverable is not None]
        return any(t >= required for t in temps)

    @property
    def deliverable(self) -> float | None:
        """Den varmeste afgang vi har — det bedste lageret kan levere nu."""
        temps = [t.deliverable for t in self.tanks if t.deliverable is not None]
        return max(temps) if temps else None
