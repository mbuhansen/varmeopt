"""Hvad en kilowatt-time strøm reelt koster — nu og i hver halvtime fremad.

Spotprisen er ikke svaret. Strøm fra nettet, strøm fra et batteri der alligevel
lades billigt om to timer, og strøm man kunne have solgt til eksportpris er
tre forskellige tal i det samme minut. Det er derfor Node-REDs
``INTELLIGENT VARMESTYRING 2D`` har syv prisgrene i stedet for ét opslag, og
den logik er rigtig — den kan bare kun svare på *nu*.

Her er den generaliseret til en vilkårlig halvtime i Predbats plan. Det er
forskellen mellem at kunne vælge kilde og at kunne lægge en blok: uden en pris
for kl. 18 kan man ikke afgøre om det betaler sig at lade op kl. 12.

**Om fortegn og retning.** For *nu* kender vi den fysiske strømretning på
nettet, og den slår planen: planen siger hvad der burde ske, måleren siger hvad
der sker. For fremtidige halvtimer har vi kun planen, og så er batteriets
tilstand det bedste vi har.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Under denne eksportpris regnes batteriets energi ikke for billigere end som
# så: der er altid den mulighed at sælge den. Samme gulv som Node-RED bruger.
EXPORT_FLOOR = 0.80

# Er der planlagt billig ladning inden for det her, kan batteriet bruges frit —
# det bliver alligevel fyldt op igen.
CHARGE_SOON_MINUTES = 120

# Er der planlagt eksport inden for det her, vaerdisaettes batteriets energi
# mod den eksport i stedet for mod sit eget gennemsnit.
EXPORT_SOON_MINUTES = 180

# Rabat paa den fremtidige eksportpris. Uden den ville en energi der lige
# akkurat kunne saelges, altid slaa enhver anden anvendelse - og det er for
# skarpt et snit til et tal der er et gaet om fremtiden. Samme vaerdi som
# Node-RED bruger.
EXPORT_DISCOUNT = 0.90

# Under denne ladetilstand vaerdisaettes batteriet ikke mod en kommende
# eksport. Er der ikke energi nok til baade at varme og saelge, er eksporten
# ikke et reelt alternativ, og saa er batteriets egen pris den rigtige.
# Node-RED har samme graense; den faldt paa gulvet ved portningen.
MIN_SOC_FOR_EXPORT = 40.0

# Under denne ladetilstand er batteriet ikke en reel kilde til den naeste
# kWh. Predbats reserve ligger typisk omkring 5-10 %, saa 12 % er lige over
# det punkt hvor der reelt ikke er noget at tage af.
MIN_SOC_FOR_BATTERY = 12.0

# Tillaeg paa genanskaffelsesprisen: tab ved at koere en kWh ind og ud af
# batteriet igen. Samme tal som Node-RED bruger.
CHARGE_LOSS_MARKUP = 1.10

# Kender vi ikke ladetilstanden, antages den samme vaerdi som Node-RED bruger.
ASSUMED_SOC = 50.0

SLOT_MINUTES = 30

# Predbats ordforraad for hvad batteriet laver i en halvtime, som det staar i
# planens raekker. Listen er ikke en filtrering - den er en kontrol, saa en
# tilstand vi ikke kender bliver sagt hoejt i loggen i stedet for stiltiende
# at blive laest som "batteriet er frit".
_KNOWN_STATES = (
    "chrg",     # ogsaa dischrg, frzchrg, holdchrg
    "charge",
    "exp",      # ogsaa frzexp
    "export",
    "hold",
    "freeze",
    "frz",
    "idle",
    "demand",
    "ecoo",     # Predbats "Eco (no discharge)"
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class Slot:
    """En halv time i Predbats plan. Index 0 er den vi står i."""

    index: int
    state: str
    import_price: float | None
    export_price: float | None
    soc_percent: float | None

    @property
    def minutes_ahead(self) -> int:
        return self.index * SLOT_MINUTES

    @property
    def discharging(self) -> bool:
        return "dischrg" in self.state or "discharge" in self.state

    @property
    def understood(self) -> bool:
        """Kunne vi overhovedet tyde hvad Predbat har planlagt her?

        En tom tilstand er «ingenting planlagt» og er helt i orden. Alt andet
        vi ikke genkender, er ordforråd vi ikke kender — og så prissætter vi
        halvtimen som om batteriet var frit, hvilket det måske ikke er.
        """
        return not self.state.strip() or any(
            word in self.state for word in _KNOWN_STATES
        )

    @property
    def charging(self) -> bool:
        """Bemærk rækkefølgen: «dischrg» indeholder «chrg».

        Uden afladningstesten først blev hver eneste planlagte afladning læst
        som en opladning. Det gik to steder galt på én gang: halvtimen blev
        prissat til importprisen, som om batteriet var bundet, og
        ``no_charge_first`` troede at batteriet ville blive fyldt inden en
        kommende eksport — netop når det modsatte var planlagt.
        """
        if self.discharging:
            return False
        return "chrg" in self.state or "charge" in self.state

    @property
    def locked(self) -> bool:
        """Batteriet er bundet — enten lades det, eller det holdes."""
        return self.charging or "hold" in self.state

    @property
    def exporting(self) -> bool:
        return "exp" in self.state


@dataclass(frozen=True)
class Price:
    """En marginalpris med begrundelsen. Begrundelsen er ikke pynt.

    Uden den kan man ikke se forskel på "0,26 kr fordi batteriet er billigt" og
    "0,26 kr fordi vi lige nu smider strøm ud til den pris" — og de to fører
    til helt forskellige beslutninger en time senere.
    """

    kr_per_kwh: float
    reason: str


@dataclass(frozen=True)
class Grid:
    """Den fysiske strømretning lige nu. Kendes kun for indeværende halvtime."""

    battery_power: float = 0.0
    grid_power: float = 0.0

    @property
    def battery_discharging(self) -> bool:
        return self.battery_power > 500

    @property
    def importing(self) -> bool:
        return self.grid_power > 200

    @property
    def exporting(self) -> bool:
        return self.grid_power < -200


class Plan:
    """Predbats plan, læst som en række marginalpriser."""

    def __init__(
        self,
        slots: tuple[Slot, ...],
        battery_average: float = 0.0,
        export_floor: float = EXPORT_FLOOR,
    ) -> None:
        self.slots = slots
        # Batteriets gennemsnitspris, men aldrig under eksportgulvet: der er
        # altid muligheden for at sælge i stedet for at bruge.
        self.battery_average = max(battery_average, export_floor)
        self.export_floor = export_floor

    def __len__(self) -> int:
        return len(self.slots)

    @property
    def horizon_minutes(self) -> int:
        return len(self.slots) * SLOT_MINUTES

    # ----------------------------------------------------------------- indlæs

    @classmethod
    def from_predbat(
        cls, attributes: Any, battery_average: float = 0.0, export_floor: float = EXPORT_FLOOR
    ) -> Plan:
        """Læs ``predbat.plan_html``'s ``raw.rows``.

        Priserne kommer i øre og skal divideres med 100 — samme antagelse som
        Node-RED gør, og den er efterprøvet mod det kørende anlæg.
        """
        rows: Any = []
        if isinstance(attributes, dict):
            raw = attributes.get("raw")
            if isinstance(raw, dict):
                rows = raw.get("rows") or []

        slots = []
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            import_rate = _number(row.get("import_rate"))
            export_rate = _number(row.get("export_rate"))
            slots.append(
                Slot(
                    index=index,
                    state=str(row.get("state") or "").lower(),
                    import_price=import_rate / 100 if import_rate is not None else None,
                    export_price=export_rate / 100 if export_rate is not None else None,
                    soc_percent=_number(row.get("soc_percent")),
                )
            )
        plan = cls(tuple(slots), battery_average, export_floor)
        unknown = sorted({s.state for s in plan.slots if not s.understood})
        if unknown:
            # Sig det én gang pr. plan, ikke én gang pr. halvtime.
            log.warning(
                "ukendte Predbat-tilstande i planen: %s - de prissættes som "
                "et frit batteri, hvilket de måske ikke er",
                ", ".join(repr(u) for u in unknown),
            )
        return plan

    # ------------------------------------------------------------------ opslag

    def to_raw(self) -> dict[str, Any]:
        """Planen som almindelige tal — til debug-filen."""
        return {
            "battery_average": self.battery_average,
            "export_floor": self.export_floor,
            "horizon_minutes": self.horizon_minutes,
            "slots": [
                {
                    "minutes": s.minutes_ahead,
                    "state": s.state,
                    "import": s.import_price,
                    "export": s.export_price,
                    "soc": s.soc_percent,
                }
                for s in self.slots
            ],
        }

    def at(self, minutes_ahead: int = 0) -> Slot | None:
        index = max(0, minutes_ahead) // SLOT_MINUTES
        return self.slots[index] if index < len(self.slots) else None

    def _next_where(self, predicate: Any, after: int = 0) -> Slot | None:
        for slot in self.slots[after:]:
            if predicate(slot):
                return slot
        return None

    # ---------------------------------------------------------- marginalpris

    def marginal(self, minutes_ahead: int = 0, grid: Grid | None = None) -> Price | None:
        """Hvad en ekstra kilowatt-time koster i den halvtime.

        ``grid`` gælder kun indeværende halvtime: den fysiske måling slår
        planen, fordi planen siger hvad der burde ske, og måleren hvad der sker.
        """
        slot = self.at(minutes_ahead)
        if slot is None:
            return None

        # Målingen beskriver kun den halvtime vi står i. Lod vi den gælde
        # fremad, ville "der går strøm ud lige nu" blive til en påstand om
        # klokken 18 — og hele pointen med at læse planen ville være tabt.
        if slot.index > 0:
            grid = None

        physical_export = grid is not None and grid.exporting
        physical_import = grid is not None and grid.importing
        battery_free = grid is not None and grid.battery_discharging

        # 1. Eksporterer vi — planlagt eller fysisk — er prisen den indtægt vi
        #    giver afkald på.
        if physical_export or slot.exporting:
            if slot.export_price is not None:
                return Price(slot.export_price, "eksport: mistet indtjening")

        # 2. Batteriet er bundet. Varmepumpen koerer paa nettet.
        if slot.locked:
            if slot.import_price is not None:
                return Price(slot.import_price, "net: batteriet er bundet")

        # 3. Koeber vi allerede fra nettet, kommer den naeste kWh derfra.
        #
        #    Det her stod foer efter batterigrenen, og det var forkert naar
        #    begge var sande. Baade "batteriet aflader" og "vi importerer"
        #    kan gaelde samtidig, og saa betyder det at inverteren staar paa
        #    sit loft: batteriet giver alt hvad det kan, og *ekstra* forbrug
        #    kan kun komme fra nettet. Med 12 kW inverter mod en varmepumpe
        #    paa 16 kW er det ikke et hjoerne, det er en almindelig tirsdag.
        if physical_import and slot.import_price is not None:
            return Price(slot.import_price, "net: import")

        # 4. Batteriet er frit. Hvad er dets energi vaerd?
        if battery_free or (grid is None and not slot.locked):
            price = self._battery_price(slot)
            if price is not None:
                return price

        # 5. Hverken det ene eller det andet - solen daekker. Den billigste af
        #    de to muligheder gaelder.
        if slot.import_price is not None:
            return Price(
                min(slot.import_price, self.battery_average), "balanceret"
            )
        return None

    def _battery_price(self, slot: Slot) -> Price | None:
        """Hvad batteriets energi er værd, når det står frit."""
        after = slot.index + 1
        next_export = self._next_where(lambda s: s.exporting, after)
        next_charge = self._next_where(lambda s: s.charging, after)

        # Venter der eksport snart, og bliver batteriet ikke fyldt inden, er
        # energien mere vaerd end sit gennemsnit: den kan saelges.
        #
        # Bemaerk at det er en *vaerdisaettelse*, ikke en beslutning. Om
        # energien faktisk bliver gemt, afgoeres af hvad den saa bruges til:
        # kan varmepumpen lave varme til under pillefyrets pris af den, er
        # det bedre at bruge den end at saelge den, og saa bliver den brugt.
        if next_export is not None and next_export.export_price is not None:
            soc = slot.soc_percent if slot.soc_percent is not None else ASSUMED_SOC
            soon = next_export.minutes_ahead - slot.minutes_ahead <= EXPORT_SOON_MINUTES
            no_charge_first = next_charge is None or next_charge.index > next_export.index
            # Sammenligningen skal ske paa det tal der faktisk returneres.
            # Stod den paa den urabatterede pris, vendte grenen sit formaal
            # paa hovedet i baandet snit < eksport < snit/0,90: snit 1,00 og
            # eksport 1,05 gav 0,945 - energien blev *billigere* af at have
            # et salg i vente.
            worth_it = next_export.export_price * EXPORT_DISCOUNT > self.battery_average
            # Er batteriet lavt, raekker energien ikke til baade at varme og
            # saelge. Saa er eksporten ikke et reelt alternativ, og batteriets
            # egen pris er den rigtige.
            enough = soc > MIN_SOC_FOR_EXPORT
            if soon and no_charge_first and worth_it and enough:
                minutes = next_export.minutes_ahead - slot.minutes_ahead
                return Price(
                    next_export.export_price * EXPORT_DISCOUNT,
                    f"batteri: værdisat mod eksport om {minutes} min "
                    f"(SOC {soc:.0f} %)",
                )

        # Fyldes batteriet billigt snart, kan det bruges frit - det bliver
        # alligevel toppet op igen.
        if next_charge is not None and next_charge.import_price is not None:
            soon = next_charge.minutes_ahead - slot.minutes_ahead <= CHARGE_SOON_MINUTES
            if soon:
                # Genanskaffelsesprisen, ikke gennemsnittet. Bruger vi en kWh
                # nu og fylder den paa igen om en time, koster den hvad
                # paafyldningen koster - hvad den energi der ligger der i
                # forvejen kostede engang, er sunk cost.
                #
                # Der stod ``max(gennemsnit, ...)``, og da Predbat netop
                # vaelger de billige timer til ladning, vandt gennemsnittet
                # naesten altid. Grenen var i praksis doed.
                return Price(
                    next_charge.import_price * CHARGE_LOSS_MARKUP,
                    f"batteri: lades om {next_charge.minutes_ahead - slot.minutes_ahead} min",
                )

        # Et naesten tomt batteri kan ikke levere den naeste kWh, uanset hvad
        # den energi der er tilbage, kostede. Saa kommer den fra nettet.
        # ``soc_percent`` blev foer kun brugt i eksportgrenen, og et tomt
        # batteri blev prissat praecis som et fuldt.
        if slot.soc_percent is not None and slot.soc_percent <= MIN_SOC_FOR_BATTERY:
            if slot.import_price is not None:
                return Price(
                    slot.import_price,
                    f"net: batteriet er naesten tomt ({slot.soc_percent:.0f} %)",
                )

        return Price(self.battery_average, "batteri: frit")

    # -------------------------------------------------------------- planlaeg

    def cheapest_window(
        self, duration_minutes: int, before_minutes: int | None = None
    ) -> tuple[int, float] | None:
        """Find det billigste sammenhængende vindue.

        Returnerer (minutter frem til start, gennemsnitspris). Det er dette
        opslag en blokplan er bygget på: «hvornår ligger de billigste 45
        minutter mellem nu og klokken 18?»
        """
        needed = max(1, math.ceil(duration_minutes / SLOT_MINUTES))
        limit = len(self.slots)
        if before_minutes is not None:
            limit = min(limit, max(0, before_minutes) // SLOT_MINUTES)
        if needed > limit:
            return None

        best: tuple[int, float] | None = None
        for start in range(limit - needed + 1):
            prices = [self.marginal(s * SLOT_MINUTES) for s in range(start, start + needed)]
            if any(p is None for p in prices):
                continue
            average = sum(p.kr_per_kwh for p in prices) / needed  # type: ignore[union-attr]
            if best is None or average < best[1]:
                best = (start * SLOT_MINUTES, average)
        return best
