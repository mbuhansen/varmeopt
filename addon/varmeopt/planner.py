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
from dataclasses import replace
from dataclasses import dataclass
from typing import Any

# Hvor langt frem det giver mening at gemme varme. Ud over det æder ståtabet
# gevinsten, og prisprognosen bliver for usikker til at handle på.
DEFAULT_HORIZON_MINUTES = 12 * 60

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
    saving_kr: float | None = None
    window_minutes: int | None = None
    reason: str = ""

    @property
    def charging_note(self) -> str:
        if self.charge and self.charge_kwh is not None:
            return f"lad {self.charge_kwh:.1f} kWh"
        return "lad ikke op"


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
        headroom_kwh: float | None = None,
        peak_headroom_kwh: float | None = None,
        stored_kwh: float | None = None,
        hot_kwh: float | None = None,
        dhw_kwh_over: Any = None,
        dhw_input_for: Any = None,
        solar_expected_kwh: float | None = None,
        grid: Any = None,
        demand_kw: float | None = None,
    ) -> Decision:
        """Hele svaret: kilde nu, og om der skal lades ud over behovet.

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

        # Spoergsmaal 2: findes der en senere time hvor varmen bliver dyrere?
        best_gap = 0.0
        best_when = None
        for minutes in range(SLOT_MINUTES, self.horizon_minutes + 1, SLOT_MINUTES):
            price = plan.marginal(minutes)
            if price is None:
                break
            cop = self._cop_for(minutes, cop_now, cop_later)
            gap = self.cheapest_heat(price.kr_per_kwh, cop) - vp_now
            if gap > best_gap:
                best_gap, best_when = gap, minutes

        # Slitagen er allerede inde i begge led gennem ``heat_price``, saa
        # den maa ikke traekkes fra igen. Skal varmen alligevel laves af
        # varmepumpen, gaar den ud af sig selv - det er den samme slitage
        # om pumpen koerer nu eller om tre timer. Skal den ellers laves af
        # pillefyret, staar den tilbage i marginen, hvor den hoerer hjemme.
        margin = best_gap
        if best_when is None or margin <= 0:
            return _with(decision, reason=f"{why}; intet at hente ved at gemme")

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
        if margin <= self.hysteresis:
            return _with(
                decision,
                window_minutes=best_when,
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
        window = min(best_when, self.horizon_minutes)
        room = min(room, self.charge_kw * window / 60)

        if room < self.min_charge_kwh:
            return _with(
                decision,
                reason=(
                    f"{why}; {margin:.2f} kr/kWh at hente om {best_when} min, "
                    f"men kun {room:.1f} kWh plads — under minimumstrækket"
                ),
            )

        # Gevinsten gaelder kun den varme der faktisk bliver fortraengt mens
        # prisen er hoej - ikke hele lagerpladsen. Her stod ``margin * room``,
        # og det overdrev 2-3 gange: 24 kWh lagerplads mod en dyr halvtime
        # hvor huset bruger 3 kW er 1,5 kWh fortraengt varme, ikke 24.
        displaced = self._displaced_kwh(plan, best_when, vp_now, cop_now, cop_later, demand_kw)
        # Varmt vand og spa over det samme spaend. Doegnprofilen ved hvornaar
        # de koerer; her spoerges den bare om de timer der er dyre.
        dhw_kwh = None
        starts, span = self._dear_window(plan, vp_now, cop_now, cop_later)
        if dhw_kwh_over is not None and span > 0:
            # Profilen skal laeses over *vinduet*, ikke fra nu. Lades der kl.
            # 11 mod en eksport kl. 18-20, er det de to timers varmtvand der
            # skal daekkes - ikke de naeste to timers.
            dhw_kwh = dhw_kwh_over(starts, span / 60)

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
        if displaced is not None:
            need, told, driver = self._shortfall(
                displaced, stored_kwh, hot_kwh, dhw_kwh, dhw_input_for
            )
            if need <= 0:
                return _with(
                    decision,
                    window_minutes=best_when,
                    reason=f"{why}; {told} — intet at lade op til",
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
        cheaper = self._cheaper_moment_before(plan, best_when, vp_now, cop_now, cop_later)
        if cheaper is not None:
            when, price = cheaper
            return _with(
                decision,
                planned_kwh=want,
                window_minutes=best_when,
                window_starts_in=starts or best_when,
                reason=(
                    f"{why}; venter - om {when} min koster varmen {price:.2f} "
                    f"mod {vp_now:.2f} nu, og der er stadig tid inden toppen "
                    f"om {best_when} min"
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
            charge=True,
            charge_kwh=want,
            planned_kwh=want,
            window_starts_in=starts or best_when,
            saving_kr=saving,
            window_minutes=best_when,
            reason=(
                f"{why}; lad {want:.1f} kWh{driver} nu og spar {saving:.2f} kr "
                f"mod om {best_when} min{shortfall}"
            ),
        )

    # ------------------------------------------------------- hjaelp til valget

    def _cheaper_moment_before(
        self, plan: Any, best_when: int, vp_now: float, cop_now: Any, cop_later: Any
    ) -> tuple[int, float] | None:
        """Ligger der en billigere halvtime mellem nu og toppen?

        Den skal ogsaa vaere til at naa: der skal vaere tid nok tilbage til at
        lade mindstetraekket inden prisen stiger. Ellers er en billigere
        halvtime uden vaerdi - man naar ikke at bruge den.
        """
        best: tuple[int, float] | None = None
        for minutes in range(SLOT_MINUTES, best_when, SLOT_MINUTES):
            price = plan.marginal(minutes)
            if price is None:
                break
            heat = self.heat_price(price.kr_per_kwh, self._cop_for(minutes, cop_now, cop_later))
            if heat is None or heat >= vp_now - self.hysteresis:
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
    ) -> tuple[float, str, str]:
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
        return dhw_short + space_short, told, driver

    def _displaced_kwh(
        self,
        plan: Any,
        best_when: int,
        vp_now: float,
        cop_now: Any,
        cop_later: Any,
        demand_kw: float | None,
    ) -> float | None:
        """Hvor meget varme der faktisk bliver hentet fra lageret i det dyre.

        Uden et behov at regne med kan spoergsmaalet ikke besvares, og saa
        siger vi det i stedet for at gaette.
        """
        if not _finite(demand_kw) or demand_kw <= 0:
            return None
        _, span = self._dear_window(plan, vp_now, cop_now, cop_later)
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
