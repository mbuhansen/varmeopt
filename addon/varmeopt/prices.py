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

# Kan planens egen bund ikke laeses, er det her graensen for hvornaar
# batteriet ikke laengere er en reel kilde til den naeste kWh. Staar bunden
# at laese i planen, bruges den i stedet - den er anlaeggets eget tal, og
# den her konstant er kun et skoen.
MIN_SOC_FOR_BATTERY = 12.0

# Saa mange halvtimer skal planen ligge i bund, foer bunden er en reserve og
# ikke bare et dyk. Et dyk er ikke en bund - der er stadig noget at tage af.
#
# Bunden er ikke det punkt hvor batteriet er tomt. Anlaegget her er foerst
# tomt ved 5 %, men Predbat har faaet besked paa at gemme 5 kWh til
# uplanlagt forbrug, og derfor lander planens gulv omkring 12-15 %. Den
# forskel aendrer ikke regnestykket: under reserven aflader inverteren ikke,
# saa den naeste kilowatt-time kommer fra nettet, uanset at der staar energi
# tilbage i batteriet.
RESERVE_SLOTS = 2

# Predbats ladetilstande er hele procenter, saa en halv procent er rigeligt
# til at afgoere om en halvtime ligger i bund.
FLOOR_TOLERANCE = 0.5

# Hvor mange procentpoint over reserven der skal vaere, foer batteriet reelt
# kan levere den naeste kilowatt-time. Varmepumpen traekker 4 kW, saa en
# halvtimes drift er 2 kWh - en stor bid af batteriet. Ligger ladetilstanden
# faa point over reserven, er der ikke noget at hente, og det aendrer hverken
# en planlagt ladning senere eller et gennemsnit fra i gaar paa.
USABLE_ABOVE_RESERVE = 5.0

# Over den her ladetilstand er en flad kurve ikke en bund. Ligger planen
# stille paa 70 %, er det fordi solen daekker huset - ikke fordi batteriet er
# tomt. En reserve er altid et lavt tal.
MAX_RESERVE = 25.0

# Tillaeg paa genanskaffelsesprisen: tab ved at koere en kWh ind og ud af
# batteriet igen. Anlaeggets inverter taber omkring 15 % hele vejen rundt,
# saa 1 kWh koebt fra nettet giver 0,85 kWh tilbage til varmepumpen - og en
# kWh taget fra batteriet nu koster derfor 1/0,85 gange hvad paafyldningen
# koster. Node-RED brugte 1,10, hvilket var et skoen; det her er anlaeggets.
BATTERY_ROUND_TRIP = 0.85
CHARGE_LOSS_MARKUP = 1 / BATTERY_ROUND_TRIP

# Kender vi ikke ladetilstanden, antages den samme vaerdi som Node-RED bruger.
ASSUMED_SOC = 50.0

SLOT_MINUTES = 30

# Hvad inverteren goer i en halvtime. Det er det eneste spoergsmaal planens
# tilstandsord skal besvare, for det afgoer hvor varmepumpens naeste
# kilowatt-time kommer fra.
DISCHARGE = "discharge"   # inverteren daekker forbruget fra batteriet
LOCKED = "locked"         # afladning sat til 0, eller der lades fra nettet
EXPORT = "export"         # der eksporteres

# Predbats ordforraad oversat til hvad inverteren goer.
#
# De fem foerste er anlaeggets egne, bekraeftet af ejeren: "demand" er
# inverteren der daekker forbruget, "chrg" er ladning fra nettet, "holdchrg"
# er Predbat der saetter afladningen til 0 (solen daekker huset, resten kommer
# fra nettet), og "exp"/"frzexp" er eksport. Resten er stavemaader af de
# samme fire handlinger, sat konservativt: alt der ikke er en afladning,
# laaser batteriet.
#
# Det stod foer som delstrengstest - "frzchrg" blev fanget fordi den
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

# Bemaerk hvad der *ikke* staar der. Predbat har flere ord - "ecoo" (Eco, no
# discharge) og "idle" blandt dem - men de forekommer ikke paa det her
# anlaeg, og hvad de praecis betyder, ville vaere et gaet. Et gaet i den
# tabel er værre end ingenting: det ville se ud som viden. De laaser derfor
# som ethvert andet ukendt ord, og halvtimen siger det paa skaermen.

# Kilder en kilowatt-time kan komme fra. Ordet staar paa skaermen, saa
# hvorfor-kolonnen kan skrive kilden i stedet for at klippe en tekst ved
# kolon og haabe at det foerste ord var en kilde.
NET = "net"
BATTERY = "batteri"
EXPORTED = "eksport"


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


@dataclass(frozen=True)
class Price:
    """En marginalpris med begrundelsen. Begrundelsen er ikke pynt.

    Uden den kan man ikke se forskel på "0,26 kr fordi batteriet er billigt" og
    "0,26 kr fordi vi lige nu smider strøm ud til den pris" — og de to fører
    til helt forskellige beslutninger en time senere.
    """

    kr_per_kwh: float
    reason: str
    # Hvor kilowatt-timen kommer fra: ``NET``, ``BATTERY`` eller ``EXPORTED``.
    # Begrundelsen forklarer *hvorfor*; kilden er hvad den er, og skaermen
    # skal ikke skulle udlede den ved at klippe teksten ved et kolon.
    source: str = NET


@dataclass(frozen=True)
class Grid:
    """Den fysiske strømretning lige nu. Kendes kun for indeværende halvtime.

    Fortegnene er anlæggets, og de står her fordi de ellers kun ville leve
    inde i tærskeltallene nedenfor:

    * ``grid_power`` — **negativ når der sælges til nettet, positiv når der
      købes.**
    * ``battery_power`` — positiv når batteriet aflader.
    * ``inverter_ac`` — vekselstrøm ud af inverteren, altså sol plus batteri.
      **Negativ når batteriet lades.**
    * ``pv_power`` — jævnstrøm ind fra panelerne, aldrig under nul.
    * ``discharge_floor`` — ikke en måling, men den grænse Predbat lige nu
      har skrevet til inverteren: ladetilstanden der må aflades ned til.
      Under hold charge er det den, ikke reserven, der binder.

    De to sidste afgør ingen pris. De er der for at balancen kan efterprøves
    i debug-filen, og for at begrundelsen kan sige at solen dækker huset når
    den gør.
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
    def exporting(self) -> bool:
        return self.grid_power < -200

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
        battery_average: float = 0.0,
        export_floor: float = EXPORT_FLOOR,
    ) -> None:
        self.slots = slots
        # Batteriets gennemsnitspris, men aldrig under eksportgulvet: der er
        # altid muligheden for at sælge i stedet for at bruge.
        # Batteriets snitpris er hvad energien kostede *ved elmåleren*, pr.
        # kWh der landede i batteriet — Node-REDs node vejer importprisen med
        # den SOC-stigning den gav, og regner hverken lade- eller afladetab
        # med. Skal den kWh ud til varmepumpen igen, koster den derfor
        # 1/virkningsgraden af det tal.
        #
        # Det er ikke en detalje. Det er præcis grunden til at det kan betale
        # sig at lade *tankene* op mens Predbat lader batteriet: varme lagret
        # i vand taber en brøkdel over en aften, hvor den samme kWh gennem
        # batteriet taber 15 % hver eneste gang.
        self.battery_average = max(
            battery_average / BATTERY_ROUND_TRIP, export_floor
        )
        self.export_floor = export_floor
        # Predbats reserve staar ikke i planen, men den kan laeses af den.
        self.reserve = self._find_reserve()

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
            "reserve": self.reserve,
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
        """Den ladetilstand planen falder ned til og bliver liggende på.

        Predbats reserve står ikke i planen, men den kan læses af den: er
        batteriet i bund, bliver ladetilstanden stående på det samme tal
        halvtime efter halvtime, mens huset kører videre på nettet. Det er
        forskellen mellem «her er der ikke mere at tage af» og «her er det
        billigst at tage det fra», og den forskel afgør hvad den næste kWh
        koster.

        Tre krav, og de holder tre andre ting ude: bunden skal ligge lavt (en
        flad kurve i 70 % er solen der dækker, ikke et tomt batteri), den
        skal holde i mere end én halvtime (et dyk er ikke en bund), og en af
        halvtimerne skal være en hvor batteriet *måtte* aflade. Står
        ladetilstanden stille fordi Predbat holder batteriet, er det ikke
        fordi der ikke er noget i det.

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

    def _at_bottom(self, slot: Slot, floor: float | None = None) -> bool:
        """Er batteriet i bund i den halvtime — kan det levere den næste kWh?

        Ikke «er det tomt», men «er der noget at tage af». Bunden er
        Predbats reserve, ikke et fladt batteri: under den aflader
        inverteren ikke, og så kommer den næste kilowatt-time fra nettet,
        uanset hvor meget der fysisk står tilbage. Og de sidste par
        procentpoint over reserven er ikke en kilde til en varmepumpe: se
        ``USABLE_ABOVE_RESERVE``.

        Bunden kan komme to steder fra, og den højeste gælder: planens egen
        reserve, og det gulv Predbat lige nu har skrevet til inverteren.
        Kendes ingen af dem, står vores eget gulv tilbage.
        """
        if slot.soc_percent is None:
            return False
        bottom = self.reserve
        if floor is not None:
            bottom = floor if bottom is None else max(bottom, floor)
        if bottom is not None:
            return slot.soc_percent <= bottom + USABLE_ABOVE_RESERVE
        return slot.soc_percent <= MIN_SOC_FOR_BATTERY

    def _may_still_discharge(self, slot: Slot, floor: float | None) -> bool:
        """Er «bundet» alligevel ikke helt bundet?

        Hold charge låser ikke batteriet fast — Predbat skriver et gulv til
        inverteren, og over det gulv leverer batteriet stadig. Sættes holdet
        ti point under ladetilstanden, er de ti point batteriets energi, og
        den næste kilowatt-time kommer derfra og ikke fra nettet.

        Gælder kun hold, ikke en rigtig ladning: mens der lades fra nettet,
        aflader inverteren ikke uanset hvor fyldt batteriet er. Og kun for
        den halvtime vi står i — gulvet er hvad der er skrevet til
        inverteren *nu*, ikke et løfte om klokken 18.
        """
        if floor is None or slot.refills or slot.soc_percent is None:
            return False
        return slot.soc_percent > floor + USABLE_ABOVE_RESERVE

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
        # Gulvet Predbat har skrevet til inverteren. Som maalingerne gaelder
        # det kun den halvtime vi staar i.
        floor = grid.discharge_floor if grid is not None else None

        # 1. Eksporterer vi — planlagt eller fysisk — er prisen den indtægt vi
        #    giver afkald på.
        if physical_export or slot.exporting:
            if slot.export_price is not None:
                return Price(
                    slot.export_price, "eksport: mistet indtjening", EXPORTED
                )

        # 2. Batteriet er bundet. Varmepumpen koerer paa nettet.
        #
        #    Det er "hold charge": Predbat saetter afladningen til 0, solen
        #    daekker huset saa langt den raekker, og resten kommer fra nettet.
        #    Det er ogsaa "chrg", hvor der oven i koebet lades fra nettet.
        #    Men "hold charge" er ikke det samme som "ingen strøm": Predbat
        #    skriver et gulv til inverteren, og ligger ladetilstanden over
        #    det, leverer batteriet stadig. Saa falder vi igennem til
        #    batterigrenen - de point ned til gulvet er rigtig energi.
        if slot.locked and not self._may_still_discharge(slot, floor):
            if slot.import_price is not None:
                why = (
                    "net: batteriet lades"
                    if slot.refills
                    else "net: afladning er slaaet fra"
                )
                if not slot.understood:
                    # En tilstand vi ikke kender, laases - men saa skal det
                    # ogsaa staa der, i stedet for at se ud som en beslutning
                    # Predbat har truffet.
                    why = f"net: ukendt tilstand «{slot.state}»"
                return Price(slot.import_price, why, NET)

        # 3. Koeber vi allerede fra nettet, kommer den naeste kWh derfra.
        #
        #    Det her stod foer efter batterigrenen, og det var forkert naar
        #    begge var sande. Baade "batteriet aflader" og "vi importerer"
        #    kan gaelde samtidig, og saa betyder det at inverteren staar paa
        #    sit loft: batteriet giver alt hvad det kan, og *ekstra* forbrug
        #    kan kun komme fra nettet.
        if physical_import and slot.import_price is not None:
            return Price(slot.import_price, "net: import", NET)

        # 3b. Er batteriet i bund, kommer den naeste kWh fra nettet - og det
        #     er ligegyldigt hvad der er planlagt senere.
        #
        #     Den her stod inde i batterigrenen, *efter* "lades snart", og
        #     saa vandt den planlagte ladning over den tomme tank: fra 11:18
        #     til 12:48 blev stroemmen prissat til 1,00 fordi batteriet ville
        #     blive fyldt kl. 13:18 - mens batteriet laa paa 16 % og huset
        #     koebte hver eneste kilowatt-time fra nettet til 1,09. Loeftet om
        #     billig ladning om to timer goer ikke energien billig nu; den er
        #     der ikke.
        #
        #     Den skal ogsaa ligge foer batterigrenen nedenfor: det er ikke
        #     energiens vaerdi der er spoergsmaalet, naar der ikke er nogen
        #     energi at tage af.
        if self._at_bottom(slot, floor) and slot.import_price is not None:
            # Reserven og et tomt batteri er ikke det samme, og det skal
            # kunne ses paa skaermen: det ene er en indstilling, det andet
            # er fysik.
            note = (
                f"net: batteriet er paa reserven ({slot.soc_percent:.0f} %)"
                if self.reserve is not None
                else f"net: batteriet er naesten tomt ({slot.soc_percent:.0f} %)"
            )
            return Price(slot.import_price, note, NET)

        # 4. Tilbage er der kun én mulighed: inverteren maa aflade, og der er
        #    noget over reserven. Saa kommer den naeste kilowatt-time fra
        #    batteriet - ogsaa hvis batteriet lige nu staar stille, fordi
        #    solen daekker huset praecis. Det er anlaeggets egen regel: som
        #    udgangspunkt leverer inverteren, og nettet kommer foerst ind naar
        #    der ikke er mere at tage af, eller naar Predbat har laast.
        #
        #    Her stod foer en gren mere - "balanceret" - som slog til naar
        #    maaleren hverken saa import, eksport eller en afladning over
        #    500 W. Den svarede med den laveste af importprisen og batteriets
        #    gennemsnit, og det tal svarer ikke til nogen kilde: kl. 08:18 den
        #    4. september leverede batteriet 389 W, og halvtimen blev prissat
        #    til 0,97 kr mens batteriet laa paa reserven og huset koebte til
        #    1,85. Spoergsmaalet er ikke hvor mange watt der tilfaeldigvis
        #    loeber i det sekund, men om inverteren maa aflade og om der er
        #    noget tilbage.
        price = self._battery_price(slot)
        if price is None:
            return None
        if slot.locked and floor is not None:
            price = Price(
                price.kr_per_kwh,
                f"{price.reason} (hold charge ned til {floor:.0f} %)",
                price.source,
            )
        if grid is not None and grid.solar_covering:
            price = Price(
                price.kr_per_kwh,
                f"{price.reason} (solen daekker huset)",
                price.source,
            )
        return price

    def _battery_price(self, slot: Slot) -> Price | None:
        """Hvad batteriets energi er værd, når det står frit."""
        after = slot.index + 1
        next_export = self._next_where(lambda s: s.exporting, after)
        next_charge = self._next_where(lambda s: s.refills, after)

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
                    BATTERY,
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
                    BATTERY,
                )

        # Loeber batteriet toert inden det lades op igen, er dets energi fuldt
        # disponeret: den kWh vi bruger nu, er praecis den kWh der mangler i
        # den halvtime hvor batteriet staar i bund, og den koeber vi fra
        # nettet til den halvtimes importpris.
        #
        # Gennemsnittet nedenfor er hvad energien kostede engang. Det tal maa
        # kun bruges naar batteriet bliver fyldt igen inden det skal bruges -
        # ellers betales den samme kWh to gange, og den billigste af de to
        # priser bogfoeres. Det er den samme genanskaffelsestanke som i
        # ladegrenen ovenfor; forskellen er kun hvor energien kommer tilbage
        # fra, og her er svaret nettet.
        empty = self._next_where(self._at_bottom, slot.index + 1)
        if empty is not None and next_charge is not None and next_charge.index <= empty.index:
            empty = None
        if empty is not None:
            # Men *hvordan* bliver det tomt? Toemmes batteriet undervejs af
            # en planlagt eksport, er den kilowatt-time vi bruger nu, ikke en
            # der skal koebes tilbage - den er en der ikke bliver solgt. Saa
            # er prisen den mistede indtaegt, ikke importprisen i bunden.
            #
            # Uden det her blev aftenen den 3. september prissat til 1,85 -
            # importprisen fredag kl. 08:02, hvor planen ligger i bund - selv
            # om det der faktisk sker inden, er en eksport kl. 07:32 til
            # 1,15. Batteriet loeb ikke toert; det blev solgt.
            sold = (
                next_export
                if next_export is not None and next_export.index <= empty.index
                else None
            )
            if sold is not None and sold.export_price is not None:
                minutes = sold.minutes_ahead - slot.minutes_ahead
                return Price(
                    sold.export_price * EXPORT_DISCOUNT,
                    f"batteri: sælges ellers om {minutes} min",
                    BATTERY,
                )
            if empty.import_price is not None:
                minutes = empty.minutes_ahead - slot.minutes_ahead
                return Price(
                    empty.import_price,
                    f"batteri: købes tilbage om {minutes} min",
                    BATTERY,
                )

        # Aldrig dyrere end at koebe den samme kilowatt-time fra nettet i
        # den samme halvtime. Paastaar gennemsnittet andet, er gennemsnittet
        # forældet - importprisen er det tal vi faktisk kender.
        #
        # Loftet er det eneste der overlever fra den gamle "balanceret"-gren.
        # Den tog ``min(importpris, gennemsnit)`` og kaldte resultatet et navn
        # der ikke var en kilde; her staar kilden rigtigt, og loftet bliver.
        if slot.import_price is not None:
            return Price(
                min(self.battery_average, slot.import_price),
                "batteri: frit",
                BATTERY,
            )
        return Price(self.battery_average, "batteri: frit", BATTERY)

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
