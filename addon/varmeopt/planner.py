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
import time
from dataclasses import replace
from dataclasses import dataclass
from typing import Any

from .prices import SUN

# Hvor langt frem det giver mening at gemme varme.
#
# Her stod 12 timer, med ståtabet som begrundelse. Det holdt ikke: Predbats
# plan rækker 35 timer, og kl. 01:53 den 10. september kunne planlæggeren
# ikke se eksporten kl. 19:23 til 3,38 kr/kWh. Værre var det at
# ``_dear_window`` scannede til den samme kant, så det dyre stræk blev
# *afkortet* og voksede efterhånden som horisonten skred frem — det er
# derfor «der bruges X kWh mens det er dyrt» svingede mellem 2,0 og 8,9 kWh
# fra minut til minut.
#
# Et døgn dækker altid næste aften, uanset hvornår på dagen der spørges.
# Ståtabet over den lagring er stadig ikke trukket fra marginen — det er
# målt (``standby.loss_kw_at``), men bevidst ikke koblet på endnu — så
# gevinsten på et langt træk er en anelse for optimistisk. Det er kendt.
DEFAULT_HORIZON_MINUTES = 24 * 60

SLOT_MINUTES = 30

# Saa lang en billig pause maa der vaere midt i et dyrt vindue, foer det
# taeller som to vinduer. Huset traekker videre af lageret i pausen, saa en
# enkelt halvtime deler ikke en aften i to.
MAX_GAP_IN_WINDOW = 60


@dataclass(frozen=True)
class Decision:
    """Svaret. ``reason`` er en del af svaret, ikke en note ved siden af."""

    source: str  # "varmepumpe" | "pillefyr"
    heat_price: float | None
    pellet_price: float
    charge: bool = False
    charge_kwh: float | None = None
    # Hvad der skal lades i alt, ogsaa naar svaret er "vent". ``charge_kwh``
    # er hvad der lades *nu*; det her er hensigten, og det er den planen
    # tegner "lad op" efter.
    planned_kwh: float | None = None
    # Hvor mange minutter der er til det *bliver* dyrt - ikke til det er
    # dyrest. Det er den frist en opladning skal vaere faerdig inden.
    window_starts_in: int | None = None
    # Hvem satte den frist: prisen eller uret. De to betyder ikke det samme
    # paa en skaerm. "Stroemmen bliver dyr kl. 17" er en paastand om
    # priserne, og den er forkert naar det er badevandet der har sat
    # tidspunktet.
    deadline_on_the_clock: bool = False
    # Det dyre straek der lades op imod: hvornaar det begynder, og hvor
    # laenge det varer. Det er ikke det samme som ``window_minutes``, som er
    # den *dyreste* halvtime - og forskellen er hele grunden til at flaget
    # flimrede. En blok hoerer til et straek, ikke til en halvtime, og
    # ``charge.py`` bruger de to tal til at kende straekket igen naeste minut.
    dear_starts_in: int | None = None
    dear_span_minutes: int | None = None
    saving_kr: float | None = None
    window_minutes: int | None = None
    reason: str = ""
    # Svaret i ét kort udsagn, sat af den gren der ved hvad der skete.
    # ``reason`` er hele historien; det her er den linje der kan staa paa en
    # skaerm uden at nogen skal laese sig frem til pointen.
    charge_state: str = ""
    # Hvad opladningen er *til*: hvor meget lageret mangler til hver af de to,
    # talt ved hver sin temperatur. Varmt vand og spa kan kun tage af den del
    # af lageret der er varm nok til dem; rumvarmen kan tage af det hele.
    # ``None`` betyder "ikke regnet ud", nul betyder "der er nok".
    dhw_short_kwh: float | None = None
    space_short_kwh: float | None = None
    # Og hvad regnestykket bestod af: hvad de to ventes at bruge i det dyre
    # vindue, og hvad lageret har til hver af dem.
    dhw_need_kwh: float | None = None
    dhw_have_kwh: float | None = None
    space_need_kwh: float | None = None
    space_have_kwh: float | None = None

    @property
    def charging_note(self) -> str:
        if self.charge and self.charge_kwh is not None:
            return f"lad {self.charge_kwh:.1f} kWh"
        return "lad ikke op"


@dataclass(frozen=True)
class Shortfall:
    """Hvad lageret mangler, delt på de to der skal bruge det.

    De to tal kan ikke lægges sammen før de er talt ved hver sin temperatur —
    se ``Planner._shortfall``. ``told`` er sætningen til begrundelsen, og
    ``driver`` det led der hænges på «lad 8,2 kWh …».
    """

    # Hvad de to ventes at bruge i det dyre vindue.
    dhw_need: float = 0.0
    space_need: float = 0.0
    # Hvad lageret har til hver af dem, talt ved hver sin temperatur.
    dhw_have: float = 0.0
    space_have: float = 0.0
    # Og hvad der saa mangler. Ikke bare differensen: varmtvandets mangel
    # maales over brugstemperaturen, men det der skal lades, er energi *ind
    # i* lageret - se ``Planner._shortfall``.
    dhw_kwh: float = 0.0
    space_kwh: float = 0.0
    told: str = ""
    driver: str = ""

    @property
    def total(self) -> float:
        return self.dhw_kwh + self.space_kwh


@dataclass(frozen=True)
class Projection:
    """Én halvtime, set forfra. Til at vise, ikke til at handle på.

    Forskellen er vigtig. Styringen spørger planlæggeren igen hvert minut og
    handler kun på svaret for *nu* — et fastlåst skema ville forældes i samme
    øjeblik en pris flyttede sig. Men uden en fremskrivning kan man ikke se
    *hvorfor* den svarer som den gør, og så er den umulig at stole på.
    """

    minutes: int
    # Marginalprisen: den af raapriserne der faktisk gaelder i timen.
    # Raapriserne foelger med, saa man kan se hvor den kommer fra i stedet
    # for at skulle regne det ud af begrundelsen.
    electricity: float
    reason: str
    # Hvor stroemmen kommer fra i den halvtime: "net", "batteri" eller
    # "eksport". Den staar her som sit eget felt, saa skaermen ikke skal
    # udlede en kilde ved at klippe begrundelsen ved et kolon.
    power: str = "net"
    import_price: float | None = None
    export_price: float | None = None
    heat_price: float | None = None
    # Predbats egen raekke: hvad planen siger, og ved hvilken ladetilstand.
    # Uden de to kan man ikke se *hvorfor* kilden er som den er - at der fx
    # staar hold charge ved 37 % - uden at gaa over i Predbats egen tabel.
    state: str = ""
    soc_percent: float | None = None
    source: str = "varmepumpe"
    # Regner planlaeggeren med at lade op i den her halvtime? Det er en
    # hensigt og ikke et loefte: den regnes forfra hvert minut, og bliver
    # lageret fuldt hurtigere end ventet, forsvinder maerkerne af sig selv.
    charging: bool = False
    # Hvorfor *den kilde* - ikke hvor prisen kommer fra. Det er den
    # forklaring der hoerer hjemme paa en raekke hvor noget aendrer sig.
    note: str = ""
    target: bool = False

    @property
    def now(self) -> bool:
        return self.minutes == 0


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _on_sun(price: Any, grid: Any) -> bool:
    """Er det solen der leverer lige nu?

    To veje til det samme svar, og de daekker hver sit tilfaelde. Prisen
    siger det naar batteriet er ude af spillet og maaleren ser panelerne
    baere huset. Maaleren siger det ogsaa naar batteriet staar frit - der er
    prisen batteriets, men stroemmen i ledningen er stadig solens. Begge
    taeller, for spoergsmaalet er hvor stroemmen kommer fra.
    """
    if price is not None and getattr(price, "source", None) == SUN:
        return True
    return bool(getattr(grid, "solar_covering", False))


def minutes_until_hour(hour: float | None, now: float) -> int | None:
    """Minutter til naeste gang klokken slaar ``hour`` — i lokal tid.

    Lokal tid, ikke UTC: fristen er et klokkeslaet paa en vaeg, og den skal
    flytte sig med sommertiden ligesom badevandet goer.

    Et klokkeslaet uden for doegnet slaar fristen fra - det er saadan
    indstillingen siges fra. Er tidspunktet passeret i dag, gaelder fristen i
    morgen, og saa ligger den laengere ude end planlaegningshorisonten: den
    binder kun den del af doegnet hvor den er foran os.
    """
    if hour is None or not _finite(hour) or not 0 <= hour < 24:
        return None
    local = time.localtime(now)
    minutes_now = local.tm_hour * 60 + local.tm_min
    ahead = (int(round(hour * 60)) - minutes_now) % (24 * 60)
    # Staar vi praecis paa fristen, er den her doegns frist forbi.
    return ahead or 24 * 60


def source_now(
    heat_price: float | None, pellet_price: float, hysteresis: float
) -> tuple[str, str]:
    """Spørgsmål 1: hvilken kilde er billigst til behovet nu?

    Ved uafgjort vinder varmepumpen, som Node-RED også gør. Hysteresen er der
    for at valget ikke vipper frem og tilbage på nogle ører.
    """
    if heat_price is None:
        return "varmepumpe", "ingen COP — antager varmepumpe"
    # Slitagen staar med i tallet, men ikke i teksten. At varmepumpevarme
    # koster det den koster, er en egenskab ved prisen - ikke en oplysning der
    # hoerer hjemme i hver eneste linje.
    if heat_price > pellet_price + hysteresis:
        return "pillefyr", f"VP {heat_price:.2f} > pille {pellet_price:.2f}"
    if heat_price < pellet_price - hysteresis:
        return "varmepumpe", f"VP {heat_price:.2f} < pille {pellet_price:.2f}"
    return "varmepumpe", "tæt løb — varmepumpen foretrækkes"


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
        dhw_temp: float = 55.0,
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
        # Kun til begrundelsen: hvilken temperatur lageret blev talt ved.
        self.dhw_temp = dhw_temp

    # ------------------------------------------------------------------ pris

    def _cop_for(self, minutes: int, cop_now: float | None, cop_later: Any) -> float | None:
        """COP i en given time.

        ``cop_later`` maa gerne vaere et opslag frem for et tal. Med en
        vejrudsigt faar hver time sin egen: forudsagt temperatur gennem
        varmekurven giver setpunktet, og setpunktet giver COP'en. Uden
        udsigt falder vi tilbage paa den vi har nu.
        """
        if callable(cop_later):
            value = cop_later(minutes)
            return value if value is not None else cop_now
        return cop_later if cop_later is not None else cop_now

    def heat_price(self, electricity_price: float | None, cop: float | None) -> float | None:
        """Hvad en kWh varme fra varmepumpen koster — strøm plus slitage.

        Slitagen hører med her og ikke kun i opladningen. At køre
        varmepumpen koster noget ud over strømmen, og den omkostning følger
        varmen: laver den en kWh, er den kWh dyrere end elprisen alene siger.
        Pillefyret bærer ikke tallet — der er brændslet og virkningsgraden
        hele historien.

        Før stod slitagen kun i opladningen, hvor den blev trukket fra
        marginen. Så blev den samme omkostning talt i det ene regnestykke og
        ignoreret i det andet, og kildevalget kunne vælge varmepumpen på en
        pris der ikke fandtes.

        Til gengæld skal den *ikke* trækkes fra marginen længere: står den i
        begge led, går den ud af sig selv når varmen alligevel skulle laves
        af varmepumpen — og det skal den, for slitagen er den samme uanset
        hvornår på aftenen pumpen kører.
        """
        if not _finite(electricity_price) or not _finite(cop) or cop <= 0:
            return None
        return electricity_price / cop + self.wear

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
        cop_later: Any = None,
        charge_cop_at: Any = None,
        headroom_kwh: float | None = None,
        peak_headroom_kwh: float | None = None,
        stored_kwh: float | None = None,
        hot_kwh: float | None = None,
        dhw_kwh_over: Any = None,
        dhw_input_for: Any = None,
        solar_expected_kwh: float | None = None,
        grid: Any = None,
        demand_kw: float | None = None,
        demand_kw_at: Any = None,
        deadline_minutes: float | None = None,
    ) -> Decision:
        """Hele svaret: kilde nu, og om der skal lades ud over behovet.

        ``deadline_minutes`` er fristen paa uret: minutter til lageret skal
        vaere fyldt. Den er ikke et prisargument og skal ikke udledes af et -
        se ``_frist``.

        ``demand_kw`` er husets forbrug *nu*, maalt. ``demand_kw_at`` er et
        opslag: hvad huset ventes at traekke om saa mange minutter. Se
        ``_displaced_kwh`` for hvorfor de to ikke er det samme spoergsmaal.

        ``charge_cop_at`` er COP'en ved *ladetemperaturen*. En blok koerer
        56 grader fremloeb - derfor kan varmen bagefter ogsaa bruges til bad -
        og det er en anden virkningsgrad end den rumvarmen koeres paa. Se
        ``_charge_cop``.

        ``grid`` er den fysiske strømretning. Den gælder kun indeværende
        halvtime, og den *skal* med: uden den falder prissætningen af nu-timen
        tilbage på batteriets genanskaffelsespris, og beslutningen ville så
        bruge en anden pris end den sensoren viser.
        """
        price_now = plan.marginal(0, grid=grid) if plan is not None else None
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

        # Hvad det koster at fylde *lageret* nu. Det er et andet tal end
        # ``vp_now``, som er hvad den varme huset vil have nu, koster.
        vp_charge = self._charge_price(0, now, cop_now, cop_later, charge_cop_at)

        # Spoergsmaal 2: findes der en senere time hvor varmen bliver dyrere?
        #
        # Nu-benet er opladningens egen pris, ikke rumvarmens. Marginen er
        # forskellen mellem at *fylde lageret nu* og at lave varmen naar den
        # skal bruges - to forskellige temperaturer, to forskellige COP'er.
        best_gap = 0.0
        best_when = None
        for minutes in range(SLOT_MINUTES, self.horizon_minutes + 1, SLOT_MINUTES):
            price = plan.marginal(minutes)
            if price is None:
                break
            cop = self._cop_for(minutes, cop_now, cop_later)
            gap = self.cheapest_heat(price.kr_per_kwh, cop) - vp_charge
            if gap > best_gap:
                best_gap, best_when = gap, minutes

        # Slitagen er allerede inde i begge led gennem ``heat_price``, saa
        # den maa ikke traekkes fra igen. Skal varmen alligevel laves af
        # varmepumpen, gaar den ud af sig selv - det er den samme slitage
        # om pumpen koerer nu eller om tre timer. Skal den ellers laves af
        # pillefyret, staar den tilbage i marginen, hvor den hoerer hjemme.
        margin = best_gap

        # Straekket der skal daekkes, og fristen det skal vaere klart inden.
        # Begge dele hoerer til her, hvor de kan regnes én gang og bruges af
        # alle spoergsmaalene nedenfor: om det kan vente, hvor meget der er
        # tid til, og hvor meget der skal bruges.
        #
        # Det regnes *foer* den foerste udgang, og det er med vilje. Staar vi
        # inde i et dyrt straek, er der ingen dyrere time forude, og saa gaar
        # turen ud ad den her doer - men det er praecis dér ``charge.py``
        # skal kunne se hvilket straek vi er inde i. Uden det ville spaerren
        # miste hukommelsen paa de eneste cyklusser den findes for.
        starts, span = self._dear_stretch(plan, vp_now, cop_now, cop_later)
        stretch: dict[str, Any] = (
            {"dear_starts_in": starts, "dear_span_minutes": span}
            if span > 0
            else {}
        )

        if best_when is None or margin <= 0:
            return _with(
                decision,
                **stretch,
                charge_state="ingen dyrere timer forude at gemme varme til",
                reason=f"{why}; intet at hente ved at gemme",
            )

        priced = starts or best_when
        frist = self._frist(priced, deadline_minutes)
        on_the_clock = frist != priced

        # Spoergsmaal 2a: er forskellen stor nok til at handle paa?
        #
        # Her stod intet, og saa var enhver positiv forskel nok. En margin
        # paa 0,04 kr/kWh mod en halvtime ti timer ude satte 13 kWh i
        # bevaegelse - og de 0,04 er mindre end usikkerheden paa de tal de er
        # regnet af: batteriets genanskaffelsespris, en COP fra en tabel og
        # Predbats plan for i morgen tidlig.
        #
        # Snittet er det samme som kildevalget bruger. Under det kan tallene
        # ikke skelne de to muligheder, og en plan der handler paa stoej,
        # handler hele tiden - hver aften faar man flyttet en lagerfuld varme
        # rundt for at hente en forskel der ikke er der.
        #
        # Og den gaelder ogsaa naar uret har sat fristen. Her stod
        # ``and not on_the_clock``, saa en frist kunne tvinge en opladning
        # igennem paa en margin ingen kunne skelne fra stoej. Det blev
        # skrevet den 9. september, hvor hele doegnet laa paa 1,18 kr og der
        # kun var 0,03 at hente - men den flade dag var selv en fejl: prisen
        # var forkert, og horisonten paa 12 timer kunne ikke se aftenen.
        #
        # Begge dele er rettet, og saa staar valget rent: fristen siger
        # *hvornaar* en opladning skal vaere faerdig, ikke *at* der skal
        # lades. Kan det ikke svare sig, lades der ikke - heller ikke selv om
        # klokken naermer sig sytten. Konsekvensen skal staa her: paa et
        # virkelig fladt doegn bliver lageret ikke fyldt paa forhaand, og saa
        # starter UVR'en selv pumpen ved det setpunkt fremloebet kraever. Det
        # er billigere end at flytte en lagerfuld varme for tre oere.
        if margin <= self.hysteresis:
            return _with(
                decision,
                **stretch,
                window_minutes=best_when,
                charge_state=(
                    f"forskellen er for lille - kun {margin:.2f} kr/kWh at hente"
                ),
                reason=(
                    f"{why}; kun {margin:.2f} kr/kWh at hente om {best_when} "
                    "min — for tæt til at flytte varme på"
                ),
            )

        # Spoergsmaal 3: hvor meget maa der lades?
        room = headroom_kwh if _finite(headroom_kwh) else 0.0
        if _finite(solar_expected_kwh):
            # Solen faar sit foerst - dens varme er gratis. Men de to
            # konkurrerer kun om pladsen under varmepumpens loft: solfangeren
            # kan presse videre til 90 grader, hvor pumpen stopper ved 60, og
            # den plads kan en opladning ikke tage fra den.
            #
            # Her stod hele den forventede solvarme, og det kostede en billig
            # formiddag hver gang eftermiddagen tegnede til sol. Den 6.
            # september steg lageret 11,5 -> 25 kWh paa sol alene, og der var
            # plads til baade den og en opladning hele dagen.
            above = 0.0
            if _finite(peak_headroom_kwh) and _finite(headroom_kwh):
                above = max(0.0, peak_headroom_kwh - headroom_kwh)
            room = max(0.0, room - max(0.0, solar_expected_kwh - above))
        window = min(frist, self.horizon_minutes)
        room = min(room, self.charge_kw * window / 60)

        if room < self.min_charge_kwh:
            return _with(
                decision,
                **stretch,
                window_minutes=best_when,
                charge_state=f"der er kun {room:.1f} kWh plads i lageret",
                reason=(
                    f"{why}; {margin:.2f} kr/kWh at hente om {best_when} min, "
                    f"men kun {room:.1f} kWh plads — under minimumstrækket"
                ),
            )

        # Gevinsten gaelder kun den varme der faktisk bliver fortraengt mens
        # prisen er hoej - ikke hele lagerpladsen. Her stod ``margin * room``,
        # og det overdrev 2-3 gange: 24 kWh lagerplads mod en dyr halvtime
        # hvor huset bruger 3 kW er 1,5 kWh fortraengt varme, ikke 24.
        displaced = self._displaced_kwh(span, demand_kw, demand_kw_at, starts)
        # Varmt vand og spa over det samme spaend. Doegnprofilen ved hvornaar
        # de koerer; her spoerges den bare om de timer der er dyre.
        dhw_kwh = None
        if dhw_kwh_over is not None and span > 0:
            # Profilen skal laeses over *vinduet*, ikke fra nu. Lades der kl.
            # 11 mod en eksport kl. 18-20, er det de to timers varmtvand der
            # skal daekkes - ikke de naeste to timers.
            #
            # Har uret sat fristen, laeses den fra fristen og frem: der bades
            # kl. 19, uanset om den halvtime tilfaeldigvis er den dyreste, og
            # et lager der foerst skal kunne lave badevand fra kl. 21, kan
            # ikke lave det bad. Rumvarmen taeller stadig kun i det dyre -
            # den kan laves billigt lige inden, og den venter gerne.
            first = frist if on_the_clock and frist < starts else starts
            dhw_kwh = dhw_kwh_over(first, (starts + span - first) / 60)

        # Og kun den del af den varme der ikke allerede staar i tankene. Den
        # varme er lavet og betalt, og den bliver brugt foerst.
        #
        # Men den maa taelles ved den temperatur den skal bruges ved, og det
        # er her det gik galt den 6. september. Tankene stod paa 45/45/43 og
        # 47/39/31 grader: 13,3 kWh over de 30 radiatorkredsen koerer paa, og
        # *nul* over 50. Koden lagde de 13,3 op mod en aften der delvis er
        # varmt vand og sagde "intet at lade op til" - mens lageret ikke
        # kunne lave et eneste bad. Stroemmen kostede 0,37 om formiddagen og
        # 1,57 om aftenen, hvor den kunne vaere solgt.
        need = displaced
        driver = ""
        short = None
        if displaced is not None:
            short = self._shortfall(
                displaced, stored_kwh, hot_kwh, dhw_kwh, dhw_input_for
            )
            need, driver = short.total, short.driver
            if need <= 0:
                return _with(
                    decision,
                    **stretch,
                    window_minutes=best_when,
                    charge_state="lageret rækker - der er ikke noget at lade op til",
                    dhw_short_kwh=short.dhw_kwh,
                    space_short_kwh=short.space_kwh,
                    dhw_need_kwh=short.dhw_need,
                    dhw_have_kwh=short.dhw_have,
                    space_need_kwh=short.space_need,
                    space_have_kwh=short.space_have,
                    reason=f"{why}; {short.told} — intet at lade op til",
                )

        # Der lades det der skal bruges - ikke hele lagerpladsen. Mindre end
        # mindstetraekket kan pumpen ikke levere, saa der rundes op til det;
        # gevinsten gaelder stadig kun den varme der faktisk fortraenges.
        want = room if need is None else min(room, max(need, self.min_charge_kwh))

        # Spoergsmaal 3b: er *nu* overhovedet det rigtige tidspunkt?
        #
        # Her stod intet, og det var en dyr tavshed. Loekken ovenfor finder
        # den dyreste time forude, men spurgte aldrig om der laa en billigere
        # halvtime imellem. Med priserne 1,00 -> 0,30 -> 0,30 -> 3,00 lader
        # den 24 kWh nu til 1,00 i stedet for at vente et kvarter paa 0,30 -
        # 4,20 kr smidt vaek paa ét traek, og lageret er fuldt naar den
        # billige time kommer.
        #
        # Spoergsmaalet stod foer *foer* maengden blev regnet, og saa var
        # hensigten ukendt mens den ventede. Planen kunne derfor ikke tegne
        # "lad op" paa de halvtimer den ventede paa - og det er netop dem man
        # vil se, inden styringen kobles til.
        cheaper = self._cheaper_moment_before(
            plan, frist, vp_charge, cop_now, cop_later, charge_cop_at
        )
        # Kommer stroemmen fra solen lige nu, ventes der ikke. Den billigere
        # halvtime forude er en anden slags stroem end den der staar paa
        # taget i det her oejeblik, og lageret skal alligevel vaere fyldt
        # inden fristen: er det solen der leverer, skal tankene bare fyldes.
        # Det er kun naar der lades fra nettet at timen skal vaere den
        # billigste.
        if _on_sun(price_now, grid):
            cheaper = None
        if cheaper is not None:
            when, price = cheaper
            return _with(
                decision,
                **stretch,
                planned_kwh=want,
                window_minutes=best_when,
                window_starts_in=frist,
                deadline_on_the_clock=on_the_clock,
                charge_state=(
                    f"venter - om {when} min er strømmen billigere, og der er "
                    "stadig tid inden det bliver dyrt"
                ),
                dhw_short_kwh=None if short is None else short.dhw_kwh,
                space_short_kwh=None if short is None else short.space_kwh,
                dhw_need_kwh=None if short is None else short.dhw_need,
                dhw_have_kwh=None if short is None else short.dhw_have,
                space_need_kwh=None if short is None else short.space_need,
                space_have_kwh=None if short is None else short.space_have,
                reason=(
                    f"{why}; venter - om {when} min koster varmen {price:.2f} "
                    f"mod {vp_now:.2f} nu, og der er stadig tid inden "
                    + (
                        f"lageret skal vaere fyldt om {frist} min"
                        if on_the_clock
                        else f"toppen om {best_when} min"
                    )
                ),
            )
        # Gevinsten gaelder det der faktisk bliver ladet. Her stod ``need``,
        # og naar pladsen var mindre end behovet, lovede den en besparelse paa
        # varme der aldrig kom i tanken: "lad 5,9 kWh nu og spar 13,02 kr" er
        # 2,2 kr/kWh, hvor marginen hoejst kan vaere forskellen op til
        # pillefyret.
        saving = margin * (want if need is None else min(want, need))

        # Raekker det ikke hele vejen, skal det staa der. Her blev maengden
        # kappet i stilhed af pladsen eller af tiden inden prisen stiger, og
        # saa saa en halv loesning ud som en hel: lageret loeber toert midt i
        # det dyre vindue, og UVR'en starter varmepumpen selv - praecis det
        # opladningen var sat i verden for at undgaa.
        shortfall = ""
        if need is not None and want + 0.05 < need:
            shortfall = f" — daekker ikke vinduet, mangler {need - want:.1f} kWh"

        return _with(
            decision,
            **stretch,
            charge=True,
            charge_kwh=want,
            planned_kwh=want,
            window_starts_in=frist,
            deadline_on_the_clock=on_the_clock,
            saving_kr=saving,
            window_minutes=best_when,
            charge_state=f"lader {want:.1f} kWh op nu",
            dhw_short_kwh=None if short is None else short.dhw_kwh,
            space_short_kwh=None if short is None else short.space_kwh,
            dhw_need_kwh=None if short is None else short.dhw_need,
            dhw_have_kwh=None if short is None else short.dhw_have,
            space_need_kwh=None if short is None else short.space_need,
            space_have_kwh=None if short is None else short.space_have,
            reason=(
                f"{why}; lad {want:.1f} kWh{driver} nu — lageret skal vaere "
                f"fyldt om {frist} min{shortfall}"
                if on_the_clock
                else (
                    f"{why}; lad {want:.1f} kWh{driver} nu og spar "
                    f"{saving:.2f} kr mod om {best_when} min{shortfall}"
                )
            ),
        )

    # ------------------------------------------------------- hjaelp til valget

    def _charge_cop(
        self, minutes: int, cop_now: Any, cop_later: Any, charge_cop_at: Any
    ) -> float | None:
        """COP'en ved ladetemperaturen, om saa mange minutter.

        En blok koerer 56 grader fremloeb, og det er derfor varmen bagefter
        ogsaa kan bruges til bad. Men ``_cop_at`` gaar udetemperatur ->
        varmekurven -> setpunkt, altsaa **rumvarmens** setpunkt - 38,4 grader
        den 10. september. Hele opladningens oekonomi blev regnet paa en
        virkningsgrad anlaegget ikke koerer med naar det lader op.

        I mildt vejr er forskellen faa procent: ved 13 grader ude staar
        tabellen paa 4,20 ved 56 og 4,82 ved 38. Om vinteren, hvor kurven
        kalder paa 30-35, bliver spaendet stort - og saa lover regnestykket en
        billigere opladning end der findes.

        Svarer opslaget ikke, falder vi tilbage paa rumvarmens COP. Det er
        det gamle svar, og det er bedre end ingenting.
        """
        if charge_cop_at is not None:
            value = charge_cop_at(minutes) if callable(charge_cop_at) else charge_cop_at
            if _finite(value) and value > 0:
                return value
        return self._cop_for(minutes, cop_now, cop_later)

    def _charge_price(
        self,
        minutes: int,
        electricity: float | None,
        cop_now: Any,
        cop_later: Any,
        charge_cop_at: Any,
    ) -> float:
        """Hvad det koster at laegge en kWh i lageret i den halvtime."""
        price = self.heat_price(
            electricity, self._charge_cop(minutes, cop_now, cop_later, charge_cop_at)
        )
        if price is not None:
            return price
        fallback = self.heat_price(electricity, self._cop_for(minutes, cop_now, cop_later))
        return fallback if fallback is not None else 0.0

    def _cheaper_moment_before(
        self,
        plan: Any,
        best_when: int,
        vp_charge: float,
        cop_now: Any,
        cop_later: Any,
        charge_cop_at: Any = None,
    ) -> tuple[int, float] | None:
        """Ligger der en billigere halvtime mellem nu og toppen?

        Begge led er opladningens egen pris. Spoergsmaalet er ikke om varmen
        bliver billigere, men om det bliver billigere at *fylde lageret*, og
        det sker ved samme temperatur nu og om en time.

        Den skal ogsaa vaere til at naa: der skal vaere tid nok tilbage til at
        lade mindstetraekket inden prisen stiger. Ellers er en billigere
        halvtime uden vaerdi - man naar ikke at bruge den.
        """
        best: tuple[int, float] | None = None
        for minutes in range(SLOT_MINUTES, best_when, SLOT_MINUTES):
            price = plan.marginal(minutes)
            if price is None:
                break
            heat = self.heat_price(
                price.kr_per_kwh,
                self._charge_cop(minutes, cop_now, cop_later, charge_cop_at),
            )
            if heat is None or heat >= vp_charge - self.hysteresis:
                continue
            if self.charge_kw * (best_when - minutes) / 60 < self.min_charge_kwh:
                continue
            if best is None or heat < best[1]:
                best = (minutes, heat)
        return best

    def _shortfall(
        self,
        displaced: float,
        stored_kwh: float | None,
        hot_kwh: float | None,
        dhw_kwh: float | None,
        dhw_input_for: Any = None,
    ) -> Shortfall:
        """Hvor meget lageret mangler — talt ved hver sin temperatur.

        To spor, og de deler det samme vand. Varmt vand og spa kan kun tages
        fra den del af lageret der er varm nok til dem; rumvarmen kan tages
        fra det hele. Derfor får varmtvandet sit først, og gulvet får resten:
        varme over 55 grader kan begge dele, varme over 30 kan kun det ene.

        ``displaced`` er husets eget forbrug i det dyre vindue, målt på
        flowmåleren efter tankene. ``dhw_kwh`` er hvad beholderen og spaen
        tager i det samme vindue, læst af døgnprofilen. De to lægges ikke
        sammen ukritisk — de skal dækkes af hver sin del af lageret.
        """
        warm = stored_kwh if _finite(stored_kwh) else 0.0
        hot = hot_kwh if _finite(hot_kwh) else 0.0
        # Uden en profil ved vi ikke hvor meget varmt vand der kommer, og saa
        # er nul det eneste aerlige - men saa siger begrundelsen det ogsaa.
        dhw = dhw_kwh if _finite(dhw_kwh) else 0.0

        # Varmtvandets underskud maales over 55 grader, men det der skal
        # lades, er energi *ind i* lageret - og de to er ikke det samme tal.
        # Vil man have 6 kWh staaende over 55 i et lager paa 45, skal man
        # baade betale loeftet fra 45 til 55 og de 6 kWh ovenpaa. Her stod
        # forskellen, og opladningen blev derfor systematisk for lille
        # praecis naar varmt vand var det der drev den.
        missing = max(0.0, dhw - hot)
        if missing <= 0:
            dhw_short = 0.0
        elif dhw_input_for is not None:
            dhw_short = max(missing, dhw_input_for(dhw))
        else:
            dhw_short = missing
        # Den varme del taeller med i den lune: bruges den til bad, er den
        # ikke ogsaa til raadighed for gulvet.
        space_have = max(0.0, warm - min(dhw, hot))
        space_short = max(0.0, displaced - space_have)

        if dhw_short > 0 and space_short > 0:
            told = (
                f"varmt vand mangler {dhw_short:.1f} kWh over "
                f"{self.dhw_temp:.0f}°, rumvarmen {space_short:.1f}"
            )
            driver = " til varmt vand og rumvarme"
        elif dhw_short > 0:
            told = (
                f"der skal {dhw:.1f} kWh varmt vand, og lageret har {hot:.1f} "
                f"over {self.dhw_temp:.0f}°"
            )
            driver = " til varmt vand"
        else:
            told = (
                f"der bruges {displaced:.1f} kWh mens det er dyrt, og lageret "
                f"har {space_have:.1f}"
            )
            driver = " til rumvarme" if space_short > 0 else ""
        return Shortfall(
            dhw_need=dhw,
            space_need=displaced,
            dhw_have=hot,
            space_have=space_have,
            dhw_kwh=dhw_short,
            space_kwh=space_short,
            told=told,
            driver=driver,
        )

    def _frist(self, priced: int, deadline_minutes: float | None) -> int:
        """Hvornaar skal opladningen vaere faerdig?

        To ting kan saette fristen, og den strammeste vinder.

        Prisen saetter den ene: der hvor det *bliver* dyrt. Uret saetter den
        anden, og den kender prisen ikke. Lageret skal vaere fyldt kl. 17,
        fordi der bades om aftenen og resten af doegnet koeres paa
        restvarmen - og om vinteren er tankene alligevel toemt naar natten,
        og dermed den billige stroem, kommer. Den frist kan ikke udledes af
        en prisraekke, og den skal derfor staa som en indstilling.

        Her stod ``best_when`` - den *dyreste* halvtime - i baade
        tidsregnestykket og ventegrenen, og det var for loest. Med et dyrt
        vindue fra 17 til 22, dyrest kl. 20:30, ventede planen gerne paa en
        billigere halvtime kl. 18: den ligger jo foer den dyreste. Saa stod
        lageret tomt fra sytten, midt i badetiden, og UVR'en startede
        varmepumpen selv.
        """
        # ``None`` foerst og for sig: ``_finite`` svarer rigtigt paa den, men
        # den er en almindelig funktion, saa hverken en typetjekker eller en
        # laeser faar noget at vide om hvad der staar tilbage bagefter.
        if deadline_minutes is None or not _finite(deadline_minutes):
            return priced
        return int(deadline_minutes) if 0 < deadline_minutes < priced else priced

    def _displaced_kwh(
        self,
        span: int,
        demand_kw: float | None,
        demand_kw_at: Any = None,
        starts: int = 0,
    ) -> float | None:
        """Hvor meget varme der faktisk bliver hentet fra lageret i det dyre.

        Regnes over straekkets egne halvtimer, ikke af ét minut ganget op.

        Her stod ``demand_kw * span / 60``, hvor ``demand_kw`` er
        flowmaalerens aflaesning i det sekund cyklussen koerte. Den blev ganget
        op over et vindue paa flere timer, og resultatet svingede derefter: i
        loggen natten til den 10. september stod «der bruges X kWh mens det er
        dyrt» skiftevis paa 2,0 og 8,9 kWh, mens den indlaerte vejrkurve laa
        roligt paa 1,34 kW.

        **Og her slaar det maalte *ikke* det modellerede.** Den regel gaelder
        i ``houseload.kw_at`` og er rigtig dér, for spoergsmaalet er hvad
        huset traekker *nu*. Men en flowmaaleraflaesning er en maaling af
        nuet, og nuet er ikke en udsigt. Til et vindue der ligger timer ude,
        er kurven ved den forudsagte temperatur det bedste svar, og maaleren
        er kun bagstopperen.

        Svarer opslaget ikke for en enkelt halvtime, udelades den af
        middelvaerdien. Den maa ikke goere hele svaret ukendt: ``None``
        betyder «kan ikke besvares», og saa bliver ``want`` til hele
        lagerpladsen.
        """
        if demand_kw_at is not None and span > 0:
            known = []
            for minutes in range(starts, starts + span, SLOT_MINUTES):
                value = demand_kw_at(minutes)
                if _finite(value) and value > 0:
                    known.append(value)
            if known:
                return sum(known) / len(known) * span / 60
        if demand_kw is None or not _finite(demand_kw) or demand_kw <= 0:
            return None
        return demand_kw * span / 60

    def _dear_window(
        self,
        plan: Any,
        vp_now: float,
        cop_now: Any,
        cop_later: Any,
    ) -> tuple[int, int]:
        """Hvornår bliver det dyrt, og hvor længe bliver det ved?

        Returnerer (minutter frem til det bliver dyrt, vinduets længde).

        Her stod ``best_when`` som startpunkt, altså den *dyreste* halvtime —
        og det er ikke der det bliver dyrt, det er der det er dyrest. Med
        eksport til 1,20 kl. 17-18 og 1,57 kl. 18-20 begyndte spændet kl. 18
        og blev to timer i stedet for tre. Lageret blev ladet til to timer,
        tømt fra sytten, og løb tørt omkring nitten — midt i den dyreste
        eksport. Så starter UVR'en varmepumpen selv, og hele øvelsen er
        spildt: strømmen sælges til 1,57 samtidig med at den bruges.

        En enkelt billig halvtime midt i et vindue afslutter det heller ikke.
        Huset trækker jo videre af lageret i den, og et eksportvindue delt af
        en halv time blev ellers halveret.
        """
        first = last = None
        gap = 0
        for minutes in range(SLOT_MINUTES, self.horizon_minutes + 1, SLOT_MINUTES):
            price = plan.marginal(minutes)
            if price is None:
                break
            heat = self.cheapest_heat(
                price.kr_per_kwh, self._cop_for(minutes, cop_now, cop_later)
            )
            if heat > vp_now:
                if first is None:
                    first = minutes
                last = minutes
                gap = 0
            elif first is not None:
                gap += SLOT_MINUTES
                if gap > MAX_GAP_IN_WINDOW:
                    break
        if first is None or last is None:
            return 0, 0
        return first, last - first + SLOT_MINUTES

    def _dear_period(
        self,
        plan: Any,
        cop_now: Any,
        cop_later: Any,
    ) -> tuple[int, int]:
        """Det foerste straek hvor varmepumpen taber til pillefyret.

        Returnerer (minutter frem til det begynder, straekkets laengde).

        Forskellen fra ``_dear_window`` er hvem der bestemmer hvad «dyrt» er.
        Der er det *dyrere end lige nu*, og det goer straekket til en egenskab
        ved **hvornaar man spoerger** i stedet for ved priserne. Spoerger man
        kl. 04 fra nattens bund, er svaret ét straek fra kl. 05 til midnat —
        tyve timer — fordi intet undervejs er billigere end natten. Alt hvad
        man haenger paa det straek, arver den egenskab: maengden, fristen, og
        enhver spaerre der skal huske «det her straek har vi ladet op til».

        Her er graensen anlaeggets egen og staar stille: over pillefyrets pris
        taber varmepumpen, og saa er det pillefyret der laver varmen. Det er
        ogsaa praecis den varme lageret kan fortraenge — hver kWh det baerer
        derinde, sparer en kWh pillevarme. En eksport til 3,38 loefter varmen
        til 0,94 og er dermed dyr; Predbats ladevindue til 0,96 er det ikke.

        Uden hysteresen ville et straek kunne begynde og slutte paa nogle
        oerer, saa den traekkes fra - samme snit som kildevalget bruger.
        Hultolerancen er den samme som i ``_dear_window``: huset traekker
        videre af lageret i en enkelt billig halvtime.
        """
        threshold = self.pellet_price - self.hysteresis
        first = last = None
        gap = 0
        for minutes in range(SLOT_MINUTES, self.horizon_minutes + 1, SLOT_MINUTES):
            price = plan.marginal(minutes)
            if price is None:
                break
            # Den *uloftede* pris. ``cheapest_heat`` klemmer ved pillefyret,
            # og saa ville sammenligningen vaere sand for hver eneste time.
            heat = self.heat_price(
                price.kr_per_kwh, self._cop_for(minutes, cop_now, cop_later)
            )
            if heat is not None and heat >= threshold:
                if first is None:
                    first = minutes
                last = minutes
                gap = 0
            elif first is not None:
                gap += SLOT_MINUTES
                if gap > MAX_GAP_IN_WINDOW:
                    break
        if first is None or last is None:
            return 0, 0
        return first, last - first + SLOT_MINUTES

    def _dear_stretch(
        self,
        plan: Any,
        vp_now: float,
        cop_now: Any,
        cop_later: Any,
    ) -> tuple[int, int]:
        """Straekket der lades op imod. Absolut naar der findes et.

        Findes der timer hvor pumpen taber til pillefyret, er *de* timer
        straekket. Findes der ingen, falder vi tilbage paa den relative:
        hvad der er dyrere end nu.

        Tilbagefaldet er ufarligt netop dér. Pilleloftets uafgjorte - som er
        grunden til at den dyreste halvtime vandrer - opstaar kun naar en
        halvtime rammer loftet, og paa et doegn uden absolut straek goer ingen
        af dem det. Saa er den dyreste halvtime entydig, og en spaerre der
        haenger paa den, staar stille. Hver tilstand har sin egen stabile
        noegle, af hver sin grund.
        """
        starts, span = self._dear_period(plan, cop_now, cop_later)
        if span > 0:
            return starts, span
        return self._dear_window(plan, vp_now, cop_now, cop_later)


    # ------------------------------------------------------------ fremskrivning

    def project(
        self,
        plan: Any,
        cop_now: float | None,
        cop_later: Any = None,
        target_minutes: int | None = None,
        grid: Any = None,
        charge_window: tuple[int, int] | None = None,
    ) -> list[Projection]:
        """Halvtime for halvtime: pris, varmepris og hvilken kilde der vinder.

        COP fremad regnes på den nuværende udetemperatur, fordi vi ikke har en
        vejrudsigt. Over nogle timer flytter prisen sig langt mere end COP'en,
        så rangordenen mellem timerne holder — men de absolutte varmepriser
        længst ude skal læses med det forbehold.
        """
        if plan is None:
            return []

        rows: list[Projection] = []
        for minutes in range(0, self.horizon_minutes + 1, SLOT_MINUTES):
            # Maaleren gaelder kun nu-timen; marginal() ser selv bort fra den
            # for alt andet, men vi sender den kun hvor den hoerer hjemme.
            price = plan.marginal(minutes, grid=grid if minutes == 0 else None)
            if price is None:
                break
            slot = plan.at(minutes)
            cop = cop_now if minutes == 0 else self._cop_for(minutes, cop_now, cop_later)
            heat = self.heat_price(price.kr_per_kwh, cop)
            source, note = source_now(heat, self.pellet_price, self.hysteresis)
            rows.append(
                Projection(
                    minutes=minutes,
                    electricity=price.kr_per_kwh,
                    reason=price.reason,
                    power=price.source,
                    import_price=slot.import_price if slot else None,
                    export_price=slot.export_price if slot else None,
                    heat_price=heat,
                    state=slot.state if slot else "",
                    soc_percent=slot.soc_percent if slot else None,
                    source=source,
                    note=note,
                    target=target_minutes is not None and minutes == target_minutes,
                )
            )
        return self._mark_charging(rows, charge_window)

    def _mark_charging(
        self,
        rows: list[Projection],
        charge_window: tuple[int, int] | None,
    ) -> list[Projection]:
        """Sæt «lad op» på blokkens egne halvtimer.

        Her stod et gæt: de N billigste halvtimer inden toppen, rekonstrueret
        af mængden og pumpens ydelse. Nu findes den rigtige liste — blokken
        er lagt, og den her tegner den. Samme tekst, men det er ikke længere
        en hensigt der genskabes, det er den plan der faktisk køres.
        """
        if not rows or charge_window is None:
            return rows
        starts, ends = charge_window
        if ends <= starts:
            return rows
        return [
            replace(row, charging=True)
            if starts - SLOT_MINUTES < row.minutes < ends
            else row
            for row in rows
        ]


def _with(decision: Decision, **changes: Any) -> Decision:
    from dataclasses import replace

    return replace(decision, **changes)
