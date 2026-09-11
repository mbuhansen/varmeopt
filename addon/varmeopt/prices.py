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
from dataclasses import dataclass, replace
from typing import Any

log = logging.getLogger(__name__)

# Under denne eksportpris regnes batteriets energi ikke for billigere end som
# så: der er altid den mulighed at sælge den. Samme gulv som Node-RED bruger.
EXPORT_FLOOR = 0.80

# Er der planlagt billig ladning inden for det her, kan batteriet bruges frit —
# det bliver alligevel fyldt op igen.
CHARGE_SOON_MINUTES = 120

# Rabat på den fremtidige eksportpris. Uden den ville en energi der lige
# akkurat kunne sælges, altid slå enhver anden anvendelse - og det er for
# skarpt et snit til et tal der er et gæt om fremtiden. Samme værdi som
# Node-RED bruger.
EXPORT_DISCOUNT = 0.90

# Hvor tomt batteriet er, når det er tomt. Det her er anlæggets eget tal og
# ikke Predbats reserve - se ``Plan.reserve`` for forskellen. Under det kan
# inverteren ikke levere, og så kommer den næste kilowatt-time fra nettet
# eller fra solen.
BATTERY_EMPTY_PERCENT = 5.0

# Så mange halvtimer skal planen ligge i bund, før bunden er Predbats
# reserve og ikke bare et dyk.
RESERVE_SLOTS = 2

# Predbats ladetilstande er hele procenter, så en halv procent er rigeligt
# til at afgøre om en halvtime ligger i bund.
FLOOR_TOLERANCE = 0.5

# Over den her ladetilstand er en flad kurve ikke en reserve. Ligger planen
# stille på 70 %, er det fordi solen dækker huset. En reserve er et lavt tal.
MAX_RESERVE = 25.0

# Tabene ind i og ud af batteriet. Det er de samme tre tal Predbat regner
# med - ``input_number.predbat_inverter_loss``,
# ``input_number.predbat_battery_loss`` og
# ``input_number.predbat_battery_loss_discharge`` - og add-on'en læser dem
# levende derfra. Værdierne her gælder kun når entiteterne ikke svarer.
INVERTER_LOSS = 0.05
BATTERY_LOSS = 0.04
BATTERY_LOSS_DISCHARGE = 0.04


def round_trip(
    inverter_loss: float = INVERTER_LOSS,
    charge_loss: float = BATTERY_LOSS,
    discharge_loss: float = BATTERY_LOSS_DISCHARGE,
) -> float:
    """Hvor stor en del af en købt kilowatt-time der når ud igen.

    Strømmen gennem inverteren to gange, plus batteriets eget tab hver vej.
    Med anlæggets tal - 5 % i inverteren, 4 % ind og 4 % ud - når 0,832 kWh
    ud til varmepumpen af hver kWh der blev købt.

    Det er ikke en detalje. Det er præcis grunden til at det kan betale sig
    at lade *tankene* op mens Predbat lader batteriet: varme lagret i vand
    taber en brøkdel over en aften, hvor den samme kWh gennem batteriet
    taber en sjettedel hver eneste gang.
    """
    into = (1 - inverter_loss) * (1 - charge_loss)
    out_of = (1 - inverter_loss) * (1 - discharge_loss)
    trip = into * out_of
    return trip if 0 < trip <= 1 else 0.85


BATTERY_ROUND_TRIP = round_trip()

# Kender vi ikke ladetilstanden, antages den samme værdi som Node-RED bruger.
ASSUMED_SOC = 50.0

SLOT_MINUTES = 30

# Hvad inverteren gør i en halvtime. Det er det eneste spørgsmål planens
# tilstandsord skal besvare, for det afgør hvor varmepumpens næste
# kilowatt-time kommer fra.
DISCHARGE = "discharge"   # inverteren dækker forbruget fra batteriet
LOCKED = "locked"         # afladning sat til 0, eller der lades fra nettet
EXPORT = "export"         # der eksporteres

# Predbats ordforråd oversat til hvad inverteren gør.
#
# De fem første er anlæggets egne, bekræftet af ejeren: "demand" er
# inverteren der dækker forbruget, "chrg" er ladning fra nettet, "holdchrg"
# er Predbat der sætter afladningen til 0 (solen dækker huset, resten kommer
# fra nettet), og "exp"/"frzexp" er eksport. Resten er stavemåder af de
# samme fire handlinger, sat konservativt: alt der ikke er en afladning,
# låser batteriet.
#
# Det stod før som delstrengstest - "frzchrg" blev fanget fordi den
# indeholder "chrg", og "frzexp" fordi den indeholder "exp". Det virkede, men
# det var held, og hvert ord vi ikke kendte, faldt igennem til "batteriet er
# frit". Netop den antagelse har kostet mest.
_STATES: dict[str, str] = {
    "demand": DISCHARGE,
    "chrg": LOCKED,
    "holdchrg": LOCKED,
    "exp": EXPORT,
    "frzexp": EXPORT,
    "dischrg": DISCHARGE,
    "discharge": DISCHARGE,
    "charge": LOCKED,
    "export": EXPORT,
    "frzchrg": LOCKED,
    "frzdischrg": LOCKED,
    "frzdis": LOCKED,
    "hold": LOCKED,
    "freeze": LOCKED,
    "frz": LOCKED,
}

# Bemærk hvad der *ikke* står der. Predbat har flere ord - "ecoo" (Eco, no
# discharge) og "idle" blandt dem - men de forekommer ikke på det her
# anlæg, og hvad de præcis betyder, ville være et gæt. Et gæt i den
# tabel er værre end ingenting: det ville se ud som viden. De låser derfor
# som ethvert andet ukendt ord, og halvtimen siger det på skærmen.

# Kilder en kilowatt-time kan komme fra. Ordet står på skærmen, så
# hvorfor-kolonnen kan skrive kilden i stedet for at klippe en tekst ved
# kolon og håbe at det første ord var en kilde.
NET = "net"
BATTERY = "batteri"
SUN = "sol"

# Ikke en kilde, men en begrundelse - som "eksport". Strømmen kommer fra
# batteriet, og den er dyr fordi den skal købes fra nettet igen når planen
# når sin bund.
BOUGHT_BACK = "købes tilbage"


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
    def word(self) -> str:
        """Tilstanden som ét renset ord — det er sådan den slås op."""
        return self.state.strip().lower()

    @property
    def mode(self) -> str:
        """Hvad inverteren gør i den halvtime.

        En tom tilstand er «ingenting planlagt», og så kører anlægget som det
        plejer: inverteren dækker forbruget. Et ord vi ikke kender, låser
        batteriet — så koster halvtimen importprisen i stedet for at blive
        læst som et frit batteri. Det er den forsigtige vej og den rigtige: en
        for høj pris koster en opladning vi kunne have taget, en for lav
        tømmer batteriet på en løgn.
        """
        if not self.word:
            return DISCHARGE
        return _STATES.get(self.word, LOCKED)

    @property
    def understood(self) -> bool:
        """Kunne vi overhovedet tyde hvad Predbat har planlagt her?"""
        return not self.word or self.word in _STATES

    @property
    def refills(self) -> bool:
        """Bliver batteriet *fyldt op* i den halvtime?

        Ikke det samme som at være bundet. «Hold charge» låser afladningen og
        holder ladetilstanden, men den hæver den ikke — batteriet får ikke
        mere i sig, og en halvtime der venter på at blive toppet op, venter
        forgæves på den. Kun en rigtig ladning tæller.
        """
        return self.word in ("chrg", "charge")

    @property
    def locked(self) -> bool:
        """Batteriet er bundet — enten lades det, eller afladning er slået fra."""
        return self.mode == LOCKED

    @property
    def exporting(self) -> bool:
        return self.mode == EXPORT

    @property
    def frozen(self) -> bool:
        """Fastholder Predbat ladetilstanden i den halvtime?

        Forskellen mellem «exp» og «frzexp» er hvor det der sælges, kommer
        fra: en almindelig eksport tømmer batteriet ud på nettet, mens en
        frossen eksport holder ladetilstanden og sælger solen. Det afgør
        hvem der må undvære, hvis varmepumpen tager en kilowatt-time.
        """
        return self.word.startswith("frz") or "freeze" in self.word


@dataclass(frozen=True)
class Price:
    """En marginalpris med to begrundelser, en kort og en lang.

    ``reason`` er det ene ord der skal stå på skærmen: hvor kilowatt-timen
    kommer fra, eller — når det er derfor den er dyr — at den kan sælges.
    Ordforrådet er med vilje lille, for en plan man skal læse en forklaring
    for at forstå, bliver ikke læst:

    ``net`` · ``net, lader op`` · ``batteri`` · ``sol`` · ``eksport`` ·
    ``købes tilbage``

    ``detail`` er regnestykket bag: hvilken gren der svarede, og med hvilket
    tal. Den står i fejlsøgningsfilen og ingen andre steder. Uden den kan man
    ikke bagefter se forskel på "0,26 kr fordi batteriet lades billigt om en
    time" og "0,26 kr fordi vi smider strøm ud til den pris netop nu" — og de
    to fører til helt forskellige beslutninger en time senere.
    """

    kr_per_kwh: float
    reason: str
    # Hvor kilowatt-timen kommer fra: ``NET``, ``BATTERY`` eller ``SUN``.
    # Begrundelsen forklarer *hvorfor* den koster det den koster; kilden er
    # hvor strømmen fysisk kommer fra, og de to er ikke det samme. En
    # halvtime hvor der eksporteres, henter strømmen i batteriet og koster
    # den mistede indtægt - kilde ``BATTERY``, begrundelse "eksport". Det
    # samme gælder "købes tilbage": strømmen kommer fra batteriet, og den
    # er dyr fordi den skal købes fra nettet når planen når bunden.
    source: str = NET
    # Regnestykket bag. Kun til fejlsøgningsfilen.
    detail: str = ""


@dataclass(frozen=True)
class Grid:
    """Det anlægget måler lige nu. Kendes kun for indeværende halvtime.

    **Ingen af målingerne afgør en pris.** Det er værd at sige lige ud, for
    to af dem gjorde, og det var fejlen bag de fjorten kildeskift den
    10. september: retningen på én stikprøve fik lov at slå en stabil,
    planbaseret pris ihjel hvert minut. Priserne kommer fra planen; målingerne
    er til balancen i debug-filen og til at begrundelsen kan sige at solen
    dækker huset når den gør.

    Fortegnene er anlæggets:

    * ``grid_power`` — **negativ når der sælges til nettet, positiv når der
      købes.** Rent balancetal.
    * ``battery_power`` — positiv når batteriet aflader. Bruges kun af
      ``solar_covering``.
    * ``inverter_ac`` — vekselstrøm ud af inverteren, altså sol plus batteri.
      **Negativ når batteriet lades.** Rent balancetal.
    * ``pv_power`` — jævnstrøm ind fra panelerne, aldrig under nul.
    * ``discharge_floor`` — ikke en måling, men den grænse Predbat lige nu
      har skrevet til inverteren: ladetilstanden der må aflades ned til.
      Under hold charge er det den, ikke reserven, der binder. Den kommer fra
      planen, ikke fra en måler, og den afgør derfor gerne en pris.
    """

    battery_power: float = 0.0
    grid_power: float = 0.0
    pv_power: float = 0.0
    inverter_ac: float = 0.0
    discharge_floor: float | None = None

    @property
    def battery_discharging(self) -> bool:
        return self.battery_power > 500

    @property
    def importing(self) -> bool:
        return self.grid_power > 200

    @property
    def solar_covering(self) -> bool:
        """Dækker solcellerne huset lige nu?

        Målt, ikke udledt: panelerne leverer, batteriet aflader ikke, og der
        købes ikke.

        Det gør ikke solen til kilden for den *næste* kilowatt-time. Er der
        intet overskud, kommer den fra batteriet eller nettet; er der
        overskud, går det til eksport, og så er prisen den mistede indtægt.
        Men det hører med i begrundelsen, for det er det man ser på anlægget.
        """
        return (
            self.pv_power > 200
            and not self.battery_discharging
            and not self.importing
        )


class Plan:
    """Predbats plan, læst som en række marginalpriser."""

    def __init__(
        self,
        slots: tuple[Slot, ...],
        trip: float = BATTERY_ROUND_TRIP,
        export_floor: float = EXPORT_FLOOR,
        empty_percent: float = BATTERY_EMPTY_PERCENT,
    ) -> None:
        self.slots = slots
        # Hvornår batteriet er tomt. Anlæggets tal, ikke Predbats reserve.
        self.empty_percent = empty_percent
        self.round_trip = trip if 0 < trip <= 1 else BATTERY_ROUND_TRIP
        self.export_floor = export_floor
        # Predbats reserve står ikke i planen, men den kan læses af den.
        self.reserve = self._find_reserve()
        # Billigste importpris fra hver halvtime og resten af horisonten ud.
        # Regnet én gang bagfra; genanskaffelsesprisen slår op i den.
        self._cheapest_ahead = self._suffix_min_import()

    def _suffix_min_import(self) -> tuple[float | None, ...]:
        cheapest: list[float | None] = [None] * len(self.slots)
        best: float | None = None
        for index in range(len(self.slots) - 1, -1, -1):
            price = self.slots[index].import_price
            if price is not None and (best is None or price < best):
                best = price
            cheapest[index] = best
        return tuple(cheapest)

    def replacement_cost(self, index: int = 0) -> float:
        """Hvad en kilowatt-time fra batteriet koster at lægge tilbage.

        Ikke hvad energien kostede engang. Det tal — gennemsnitsprisen — er
        sunk cost, og det var kilden til de fleste forkerte valg vi har set:
        en kWh solen lagde i batteriet gratis, er ikke gratis at *bruge*, for
        den skal skaffes igen.

        Prisen er derfor den billigste import der er tilbage i horisonten,
        ganget op med tabet hele vejen rundt: bruger vi en kWh nu, skal der
        købes 1/0,832 for at have den igen. Det er den samme tanke som
        grenene «lades om 40 min» og «købes tilbage om 90 min» bygger på —
        her er den bare uden en bestemt begivenhed at hænge den op på.

        Aldrig under eksportgulvet: der er altid den mulighed at sælge.

        Salgssiden ligger med vilje ikke her. At energien er mere værd fordi
        den kan eksporteres, gælder kun hvis den stadig er der når salget
        kommer, og over reserven — de betingelser hører hjemme i
        ``_battery_price``, hvor de kan stilles.
        """
        cheapest = self._cheapest_ahead[index] if 0 <= index < len(self._cheapest_ahead) else None
        if cheapest is None:
            return self.export_floor
        return max(cheapest / self.round_trip, self.export_floor)

    def __len__(self) -> int:
        return len(self.slots)

    @property
    def horizon_minutes(self) -> int:
        return len(self.slots) * SLOT_MINUTES

    # ----------------------------------------------------------------- indlæs

    @classmethod
    def from_predbat(
        cls,
        attributes: Any,
        trip: float = BATTERY_ROUND_TRIP,
        export_floor: float = EXPORT_FLOOR,
        empty_percent: float = BATTERY_EMPTY_PERCENT,
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
        plan = cls(tuple(slots), trip, export_floor, empty_percent)
        unknown = sorted({s.state for s in plan.slots if not s.understood})
        if unknown:
            # Sig det én gang pr. plan, ikke én gang pr. halvtime.
            log.warning(
                "ukendte Predbat-tilstande i planen: %s - batteriet låses "
                "konservativt, og prisen kan derfor være for høj",
                ", ".join(repr(u) for u in unknown),
            )
        return plan

    # ------------------------------------------------------------------ opslag

    def to_raw(self) -> dict[str, Any]:
        """Planen som almindelige tal — til debug-filen."""
        return {
            "replacement_cost": self.replacement_cost(),
            "round_trip": self.round_trip,
            "export_floor": self.export_floor,
            "reserve": self.reserve,
            "empty_percent": self.empty_percent,
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

    # -------------------------------------------------------------- reserven

    def _find_reserve(self) -> float | None:
        """Predbats reserve: den ladetilstand planen falder ned til og bliver
        liggende på.

        **Den er ikke en bund for varmepumpen.** Anlægget er først tomt ved
        5 %, og reserven på 12-15 % er 5 kWh Predbat holder tilbage til
        uplanlagt forbrug — og en varmepumpe der starter, *er* uplanlagt
        forbrug. Den energi må bruges; Predbat vil bare ikke sælge den.

        Derfor er reserven et loft over hvad der kan **eksporteres**, ikke et
        gulv under hvad der kan bruges, og det er kun dér den bruges: en
        kommende eksport er ikke et alternativ til den energi der ligger
        under reserven, for den bliver aldrig solgt.

        Tre krav, og de holder tre andre ting ude: bunden skal ligge lavt (en
        flad kurve i 70 % er solen der dækker), den skal holde i mere end én
        halvtime (et dyk er ikke en bund), og en af halvtimerne skal være en
        hvor batteriet måtte aflade. Står ladetilstanden stille fordi Predbat
        holder batteriet, er det et hold og ikke en reserve.

        En eksport tæller derimod med, når den ligger dernede sammen med en
        afladning: Predbat sælger ikke under reserven, så en frossen eksport
        på bunden ligger der netop fordi det *er* bunden. Kravet er kun at
        bunden også ses ét sted hvor batteriet havde lov at levere — ellers
        kunne et hold alene udnævne en reserve.
        """
        levels = [s.soc_percent for s in self.slots if s.soc_percent is not None]
        if not levels:
            return None
        floor = min(levels)
        if floor > MAX_RESERVE:
            return None
        resting = [
            s
            for s in self.slots
            if s.soc_percent is not None
            and s.soc_percent <= floor + FLOOR_TOLERANCE
            and s.mode != LOCKED
        ]
        if len(resting) < RESERVE_SLOTS:
            return None
        return floor if any(s.mode == DISCHARGE for s in resting) else None

    def _at_bottom(self, slot: Slot) -> bool:
        """Er batteriet tomt i den halvtime?

        Tomt, ikke «på reserven». Anlægget kan aflade til 5 %, og de 12-15 %
        Predbat planlægger efter, er penge på bogen og ikke en mur — se
        ``_find_reserve``. Under det punkt kan inverteren ikke levere, og så
        kommer den næste kilowatt-time fra nettet eller fra solen.
        """
        if slot.soc_percent is None:
            return False
        return slot.soc_percent <= self.empty_percent

    def _depleted(self, slot: Slot) -> bool:
        """Er planen nået ned til sit gulv i den halvtime?

        Gulvet er Predbats reserve når den kan læses af planen, ellers
        anlæggets eget nulpunkt. Forskellen på den her og ``_at_bottom`` er
        hvem der spørger: ``_at_bottom`` spørger om inverteren kan levere
        *nu*, og der er 5 % svaret. Den her spørger om planen har mere at
        give af, og der er reserven svaret - Predbat planlægger ikke at gå
        under den, så når kurven rammer den, er der ikke mere tilbage af den
        energi der ligger i batteriet i dag.
        """
        if slot.soc_percent is None:
            return False
        floor = self.reserve if self.reserve is not None else self.empty_percent
        return slot.soc_percent <= floor + FLOOR_TOLERANCE

    def _runs_dry(self, slot: Slot) -> Slot | None:
        """Hvornår planen bruger den energi der ligger i batteriet nu.

        Energi kan ikke krydse en bund. Falder ladetilstanden ned til gulvet
        inden batteriet fyldes igen, er den kilowatt-time der ligger der nu,
        allerede lovet til huset før da - og hvad der sker på den anden side
        af bunden, er en anden energi.

        Ligger vi *allerede* på gulvet, løber der ikke noget tørt forude:
        så er der ingen nedstigning at datere, og prisen hører til de andre
        grene. Det er også det der holder reserven fra at blive en mur -
        se ``_find_reserve``.
        """
        if slot.soc_percent is None or self._depleted(slot):
            return None
        return self._next_where(self._depleted, slot.index + 1)

    def _may_still_discharge(self, slot: Slot, floor: float | None) -> bool:
        """Er «bundet» alligevel ikke helt bundet?

        Hold charge låser ikke batteriet fast — Predbat skriver et gulv til
        inverteren, og over det gulv leverer batteriet stadig. Sættes holdet
        ti point under ladetilstanden, er de ti point batteriets energi, og
        den næste kilowatt-time kommer derfra og ikke fra nettet.

        Gælder kun hold, ikke en rigtig ladning: mens der lades fra nettet,
        aflader inverteren ikke uanset hvor fyldt batteriet er. Og kun for
        den halvtime vi står i — gulvet er hvad der er skrevet til
        inverteren *nu*, ikke et løfte om klokken 18. Kender vi ikke gulvet
        for en halvtime længere fremme, er nettet det forsigtige svar.
        """
        if floor is None or slot.refills or slot.soc_percent is None:
            return False
        return slot.soc_percent > floor

    def _grid_or_sun(self, grid: Grid | None) -> str:
        """Nettet — eller solen, når vi kan måle at det er den der bærer.

        Er batteriet ude af spillet, kommer den næste kilowatt-time fra
        nettet eller fra solcellerne, og de to kan ikke skelnes af planen
        alene. Måler vi at panelerne bærer huset uden at der købes, er det
        solen; ellers er nettet det ærlige svar. For en halvtime længere
        fremme er der ingen måling, og så er det nettet.
        """
        return SUN if grid is not None and grid.solar_covering else NET

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

        # Gulvet Predbat har skrevet til inverteren. Som målingerne gælder
        # det kun den halvtime vi står i.
        floor = grid.discharge_floor if grid is not None else None

        # 1. Sælger planen i den halvtime, er prisen den indtægt vi giver
        #    afkald på. Kilden er derimod ikke «eksport», for eksport er
        #    ikke et sted strøm kommer fra: en almindelig eksport tømmer
        #    batteriet ud på nettet, så den kilowatt-time varmepumpen tager,
        #    er batteriets. En frossen eksport holder ladetilstanden og sælger
        #    solen, og så er det solens.
        #
        #    Her stod før ``physical_export or slot.exporting``, så måleren
        #    kunne udløse grenen på egen hånd. Det var forkert: måleren
        #    kender kun det aktuelle øjeblik, og grenen svarer med den
        #    *aktuelle* halvtimes rå tarif. Den 10. september lå den på 1,31
        #    mens batterigrenen med rette værdisatte energien til 3,04 mod
        #    aftenens top - så hvert minut hvor huset tilfældigvis sendte
        #    strøm ud, faldt prisen til under det halve og beslutningen vippede.
        if slot.exporting:
            if slot.export_price is not None:
                sold = SUN if slot.frozen else BATTERY
                return Price(
                    slot.export_price,
                    "eksport",
                    sold,
                    detail="der sælges i den halvtime - prisen er den "
                    "indtægt vi giver afkald på",
                )

        # 2. Batteriet er bundet. Varmepumpen kører på nettet.
        #
        #    Det er "hold charge": Predbat sætter afladningen til 0, solen
        #    dækker huset så langt den rækker, og resten kommer fra nettet.
        #    Det er også "chrg", hvor der oven i købet lades fra nettet.
        #    Men "hold charge" er ikke det samme som "ingen strøm": Predbat
        #    skriver et gulv til inverteren, og ligger ladetilstanden over
        #    det, leverer batteriet stadig. Så falder vi igennem til
        #    batterigrenen - de point ned til gulvet er rigtig energi.
        if slot.locked and not self._may_still_discharge(slot, floor):
            if slot.import_price is not None:
                # Lades der fra nettet, kommer ekstra forbrug også derfra -
                # solen går jo i batteriet. Er afladningen bare slået fra,
                # kan solen dække huset så langt den rækker.
                source = NET if slot.refills else self._grid_or_sun(grid)
                why = "net, lader op" if slot.refills else source
                detail = (
                    "batteriet lades fra nettet"
                    if slot.refills
                    else "afladning er slået fra"
                )
                if not slot.understood:
                    # En tilstand vi ikke kender, låses - men så skal det
                    # også stå der, i stedet for at se ud som en beslutning
                    # Predbat har truffet.
                    why = f"{source}, ukendt tilstand"
                    detail = f"ukendt Predbat-tilstand «{slot.state}» - låst"
                return Price(slot.import_price, why, source, detail=detail)

        # Her lå en gren mere: "ser måleren import, står inverteren på
        # sit loft, og så kan ekstra forbrug kun komme fra nettet". Den er
        # fjernet, og begrundelsen skal stå her, så ingen indfører den igen
        # på samme præmis.
        #
        # Præmissen var en enhedsfejl. Den blev skrevet som "12 kW inverter
        # mod 16 kW varmepumpe", men de 16 kW er ``hp_charge_kw`` - tankenes
        # ladeeffekt i *varme*. Pumpens elforbrug er varmen delt med COP:
        # anlæggets egen måling er 6,23 kW varme for 1,79 kW el, så selv
        # ved fuld ydelse trækker den under 5 kW. En 12 kW inverter når
        # aldrig sit loft af den, og der er ingen halvtime hvor batteriet
        # aflader alt hvad det kan *og* varmepumpen er grunden til at der
        # købes.
        #
        # Tilbage målte ``grid_power > 200`` bare husets almindelige vippen
        # omkring nul, og den vippen slog en stabil, planbaseret pris ihjel
        # med en 60-sekunders stikprøve. Målingerne afgør herefter ingen
        # pris; det gør planen.

        # 3b. Er batteriet i bund, kommer den næste kWh fra nettet - og det
        #     er ligegyldigt hvad der er planlagt senere.
        #
        #     Den her stod inde i batterigrenen, *efter* "lades snart", og
        #     så vandt den planlagte ladning over den tomme tank: fra 11:18
        #     til 12:48 blev strømmen prissat til 1,00 fordi batteriet ville
        #     blive fyldt kl. 13:18 - mens batteriet lå på 16 % og huset
        #     købte hver eneste kilowatt-time fra nettet til 1,09. Løftet om
        #     billig ladning om to timer gør ikke energien billig nu; den er
        #     der ikke.
        #
        #     Den skal også ligge før batterigrenen nedenfor: det er ikke
        #     energiens værdi der er spørgsmålet, når der ikke er nogen
        #     energi at tage af.
        if self._at_bottom(slot) and slot.import_price is not None:
            source = self._grid_or_sun(grid)
            return Price(
                slot.import_price,
                source,
                source,
                detail=f"batteriet er tomt ({slot.soc_percent:.0f} %)",
            )

        # 4. Tilbage er der kun én mulighed: inverteren må aflade, og der er
        #    noget over reserven. Så kommer den næste kilowatt-time fra
        #    batteriet - også hvis batteriet lige nu står stille, fordi
        #    solen dækker huset præcis. Det er anlæggets egen regel: som
        #    udgangspunkt leverer inverteren, og nettet kommer først ind når
        #    der ikke er mere at tage af, eller når Predbat har låst.
        #
        #    Her stod før en gren mere - "balanceret" - som slog til når
        #    måleren hverken så import, eksport eller en afladning over
        #    500 W. Den svarede med den laveste af importprisen og batteriets
        #    gennemsnit, og det tal svarer ikke til nogen kilde: kl. 08:18 den
        #    4. september leverede batteriet 389 W, og halvtimen blev prissat
        #    til 0,97 kr mens batteriet lå på reserven og huset købte til
        #    1,85. Spørgsmålet er ikke hvor mange watt der tilfældigvis
        #    løber i det sekund, men om inverteren må aflade og om der er
        #    noget tilbage.
        price = self._battery_price(slot)
        if price is None:
            return None
        # De to her hører til regnestykket, ikke til skærmen: de ændrer
        # hverken prisen eller hvor strømmen kommer fra.
        if slot.locked and floor is not None:
            price = replace(
                price,
                detail=f"{price.detail} (hold charge ned til {floor:.0f} %)",
            )
        if grid is not None and grid.solar_covering:
            price = replace(price, detail=f"{price.detail} (solen dækker huset)")
        return price

    def _best_export(
        self,
        after: int,
        next_charge: Slot | None,
        runs_dry: Slot | None = None,
    ) -> Slot | None:
        """Den bedst betalte eksport der er tilbage, før batteriet lades op.

        Ikke den første. Den marginale kilowatt-time bliver solgt i den bedste
        halvtime der er tilbage, så det er den pris der er alternativet til at
        bruge den — og en tidlig, dårligt betalt eksport siger ikke noget om
        hvad energien er værd.

        Grænsen er opladningen, ikke uret: fyldes batteriet inden, er det ikke
        *den her* kilowatt-time der bliver solgt bagefter. Der stod før en
        grænse på tre timer, som kom fra den første portering og aldrig havde
        nogen begrundelse — planen kender salget tolv timer i forvejen, og om
        det ligger to eller elleve timer ude, er energien lige meget lovet væk.

        Bunden er den anden grænse, og den manglede. Natten til den 9.
        september lå batteriet på 32 % kl. 03:20, og planen kørte det ned til
        reserven på 9 % kl. 07:20 — men grenen her fandt et salg kl. 21:20 til
        1,31, atten timer og en bund senere, og prissatte hele døgnet til
        1,18. Det salg er solens energi, ikke nattens: den kilowatt-time der
        lå der kl. 03:20, var brugt længe før. Et salg på den anden side af
        bunden er ikke et alternativ til at bruge energien nu.
        """
        # Prisen følger med som sit eget tal. Den står i ``best.export_price``
        # og kan ikke være ``None`` når ``best`` er sat - men den invariant
        # ligger i et ``continue`` en omgang tidligere, og hverken en
        # typetjekker eller en læser kan se den derfra.
        best: Slot | None = None
        best_price: float | None = None
        for candidate in self.slots[after:]:
            if next_charge is not None and candidate.index >= next_charge.index:
                break
            if runs_dry is not None and candidate.index > runs_dry.index:
                break
            price = candidate.export_price
            if not candidate.exporting or price is None:
                continue
            if best_price is None or price > best_price:
                best, best_price = candidate, price
        return best

    def _battery_price(self, slot: Slot) -> Price | None:
        """Hvad batteriets energi er værd, når det står frit."""
        after = slot.index + 1
        next_export = self._next_where(lambda s: s.exporting, after)
        next_charge = self._next_where(lambda s: s.refills, after)
        runs_dry = self._runs_dry(slot)
        best_export = self._best_export(after, next_charge, runs_dry)

        # Bliver energien solgt inden batteriet lades op igen, er den lovet
        # væk: den kilowatt-time vi bruger nu, er en der ikke bliver solgt.
        #
        # Bemærk at det er en *værdisættelse*, ikke en beslutning. Om
        # energien faktisk bliver gemt, afgøres af hvad den så bruges til:
        # kan varmepumpen lave varme til under pillefyrets pris af den, er
        # det bedre at bruge den end at sælge den, og så bliver den brugt.
        if best_export is not None and best_export.export_price is not None:
            soc = slot.soc_percent if slot.soc_percent is not None else ASSUMED_SOC
            # Sammenligningen skal ske på det tal der faktisk returneres.
            # Stod den på den urabatterede pris, vendte grenen sit formål
            # på hovedet i båndet snit < eksport < snit/0,90: snit 1,00 og
            # eksport 1,05 gav 0,945 - energien blev *billigere* af at have
            # et salg i vente.
            worth_it = (
                best_export.export_price * EXPORT_DISCOUNT
                > self.replacement_cost(slot.index)
            )
            # Under bunden bliver energien aldrig solgt, og så er eksporten
            # ikke et alternativ til at bruge den. Bunden er Predbats reserve
            # når den kan læses af planen - Predbat eksporterer ikke
            # derunder - ellers anlæggets eget nulpunkt.
            #
            # Her stod ``MIN_SOC_FOR_EXPORT = 40`` som skøn, og de 40 % slog
            # netop værdisættelsen fra når batteriet var lavt: så blev
            # energien *billigere* af at være knap.
            floor_for_sale = (
                self.reserve if self.reserve is not None else self.empty_percent
            )
            if worth_it and soc > floor_for_sale:
                minutes = best_export.minutes_ahead - slot.minutes_ahead
                return Price(
                    best_export.export_price * EXPORT_DISCOUNT,
                    "eksport",
                    BATTERY,
                    detail=f"værdisat mod eksport om {minutes} min "
                    f"(SOC {soc:.0f} %)",
                )

        # Fyldes batteriet billigt snart, kan det bruges frit - det bliver
        # alligevel toppet op igen.
        if next_charge is not None and next_charge.import_price is not None:
            soon = next_charge.minutes_ahead - slot.minutes_ahead <= CHARGE_SOON_MINUTES
            if soon:
                # Den kendte påfyldning, ikke bare den billigste i
                # horisonten: sker den om en halv time, er det *den* pris den
                # kWh vi bruger nu, bliver lagt tilbage til.
                return Price(
                    next_charge.import_price / self.round_trip,
                    BATTERY,
                    BATTERY,
                    detail="lades om "
                    f"{next_charge.minutes_ahead - slot.minutes_ahead} min "
                    f"til {next_charge.import_price:.2f}",
                )

        # Løber batteriet tørt inden det lades op igen, er dets energi fuldt
        # disponeret: den kWh vi bruger nu, er præcis den kWh der mangler i
        # den halvtime hvor planen når sit gulv, og den køber vi fra nettet
        # til den halvtimes importpris.
        #
        # Gulvet er planens eget - reserven - og ikke anlæggets nulpunkt på
        # 5 %. Det er ikke en mur under varmepumpen, som det ville være hvis
        # den stod i ``_at_bottom``: den siger stadig at inverteren gerne må
        # aflade ned til 5 %. Den siger kun hvornår planen ikke har mere at
        # give af, og det er dér den manglende kilowatt-time bliver købt.
        #
        # Gennemsnittet nedenfor er hvad energien kostede engang. Det tal må
        # kun bruges når batteriet bliver fyldt igen inden det skal bruges -
        # ellers betales den samme kWh to gange, og den billigste af de to
        # priser bogføres. Det er den samme genanskaffelsestanke som i
        # ladegrenen ovenfor; forskellen er kun hvor energien kommer tilbage
        # fra, og her er svaret nettet.
        empty = runs_dry
        if empty is not None and next_charge is not None and next_charge.index <= empty.index:
            empty = None
        if empty is not None:
            # Men *hvordan* bliver det tomt? Tømmes batteriet undervejs af
            # en planlagt eksport, er den kilowatt-time vi bruger nu, ikke en
            # der skal købes tilbage - den er en der ikke bliver solgt. Så
            # er prisen den mistede indtægt, ikke importprisen i bunden.
            #
            # Uden det her blev aftenen den 3. september prissat til 1,85 -
            # importprisen fredag kl. 08:02, hvor planen ligger i bund - selv
            # om det der faktisk sker inden, er en eksport kl. 07:32 til
            # 1,15. Batteriet løb ikke tørt; det blev solgt.
            sold = (
                next_export
                if next_export is not None and next_export.index <= empty.index
                else None
            )
            if sold is not None and sold.export_price is not None:
                minutes = sold.minutes_ahead - slot.minutes_ahead
                return Price(
                    sold.export_price * EXPORT_DISCOUNT,
                    "eksport",
                    BATTERY,
                    detail=f"sælges ellers om {minutes} min",
                )
            if empty.import_price is not None:
                minutes = empty.minutes_ahead - slot.minutes_ahead
                # Kilden er batteriets, begrundelsen er nettets, og de to
                # skal ikke slås sammen til ét ord.
                #
                # Her stod NET i begge felter en dag, og på skærmen var det
                # forkert: Predbat står på demand, ladetilstanden er 30 %,
                # og inverteren leverer. Det *er* batteriet den næste
                # kilowatt-time kommer fra. At den så koster importprisen i
                # bunden, er en anden oplysning, og det er præcis derfor de
                # to felter findes - prisen siger hvad den koster, kilden
                # hvor den kommer fra, og begrundelsen hvorfor de to ikke
                # følges ad.
                return Price(
                    empty.import_price,
                    BOUGHT_BACK,
                    BATTERY,
                    detail=f"købes tilbage om {minutes} min "
                    f"til {empty.import_price:.2f}",
                )

        # Ingen bestemt begivenhed at hænge prisen op på. Så er det
        # genanskaffelsen i al almindelighed: den billigste import der er
        # tilbage, ganget op med tabet hele vejen rundt.
        #
        # Aldrig dyrere end at købe den samme kilowatt-time fra nettet i den
        # samme halvtime. Er den billigste fremtidige import dyrere end
        # prisen nu, giver batteriet ingenting - og så er loftet svaret.
        # Det er det eneste der overlever fra den gamle "balanceret"-gren.
        value = self.replacement_cost(slot.index)
        if slot.import_price is not None and slot.import_price < value:
            return Price(
                slot.import_price,
                BATTERY,
                BATTERY,
                detail="genanskaffelsen er dyrere end at købe den nu - "
                f"loftet svarer ({slot.import_price:.2f})",
            )
        return Price(
            value,
            BATTERY,
            BATTERY,
            detail=f"genanskaffelse: billigste import forude / {self.round_trip:.3f}",
        )

    # -------------------------------------------------------------- planlæg

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
