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
from dataclasses import dataclass
from typing import Any

# Hvor langt frem det giver mening at gemme varme. Ud over det æder ståtabet
# gevinsten, og prisprognosen bliver for usikker til at handle på.
DEFAULT_HORIZON_MINUTES = 12 * 60

SLOT_MINUTES = 30


@dataclass(frozen=True)
class Decision:
    """Svaret. ``reason`` er en del af svaret, ikke en note ved siden af."""

    source: str  # "varmepumpe" | "pillefyr"
    heat_price: float | None
    pellet_price: float
    charge: bool = False
    charge_kwh: float | None = None
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
        stored_kwh: float | None = None,
        solar_expected_kwh: float | None = None,
        grid: Any = None,
        demand_kw: float | None = None,
    ) -> Decision:
        """Hele svaret: kilde nu, og om der skal lades ud over behovet.

        ``grid`` er den fysiske strømretning. Den gælder kun indeværende
        halvtime, og den *skal* med: uden den falder prissætningen af nu-timen
        tilbage på batteriets gennemsnit, og beslutningen ville så bruge en
        anden pris end den sensoren viser.
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
        # regnet af: batteriets snitpris, en COP fra en tabel og Predbats
        # plan for i morgen tidlig.
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

        # Spoergsmaal 2b: er *nu* overhovedet det rigtige tidspunkt?
        #
        # Her stod intet, og det var en dyr tavshed. Loekken ovenfor finder
        # den dyreste time forude, men spurgte aldrig om der laa en billigere
        # halvtime imellem. Med priserne 1,00 -> 0,30 -> 0,30 -> 3,00 lader
        # den 24 kWh nu til 1,00 i stedet for at vente et kvarter paa 0,30 -
        # 4,20 kr smidt vaek paa ét traek, og lageret er fuldt naar den
        # billige time kommer.
        cheaper = self._cheaper_moment_before(plan, best_when, vp_now, cop_now, cop_later)
        if cheaper is not None:
            when, price = cheaper
            return _with(
                decision,
                window_minutes=best_when,
                reason=(
                    f"{why}; venter - om {when} min koster varmen {price:.2f} "
                    f"mod {vp_now:.2f} nu, og der er stadig tid inden toppen "
                    f"om {best_when} min"
                ),
            )

        # Spoergsmaal 3: hvor meget maa der lades?
        room = headroom_kwh if _finite(headroom_kwh) else 0.0
        if _finite(solar_expected_kwh):
            # Solen faar sit foerst - dens varme er gratis.
            room = max(0.0, room - solar_expected_kwh)
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

        # Og kun den del af den varme der ikke allerede staar i tankene. Den
        # varme er lavet og betalt, og den bliver brugt foerst. Skal der 11
        # kWh gennem huset mens stroemmen er dyr, og staar der 13 i lageret,
        # er der ingenting at lade op til - saa flytter en opladning kun
        # varme man alligevel havde, og betaler staatab for det.
        #
        # Foer blev der ladet op til hele lagerpladsen, og gevinsten blev
        # regnet paa den fortraengte varme uden at spoerge hvor den skulle
        # komme fra. Et fuldt lager og et tomt lager gav samme svar.
        need = displaced
        if displaced is not None:
            stored = stored_kwh if _finite(stored_kwh) else 0.0
            need = max(0.0, displaced - stored)
            if need <= 0:
                return _with(
                    decision,
                    window_minutes=best_when,
                    reason=(
                        f"{why}; der bruges {displaced:.1f} kWh mens det er "
                        f"dyrt, og lageret har {stored:.1f} — intet at lade "
                        "op til"
                    ),
                )

        # Der lades det der skal bruges - ikke hele lagerpladsen. Mindre end
        # mindstetraekket kan pumpen ikke levere, saa der rundes op til det;
        # gevinsten gaelder stadig kun den varme der faktisk fortraenges.
        want = room if need is None else min(room, max(need, self.min_charge_kwh))
        saving = margin * (want if need is None else need)

        return _with(
            decision,
            charge=True,
            charge_kwh=want,
            saving_kr=saving,
            window_minutes=best_when,
            reason=(
                f"{why}; lad {want:.1f} kWh nu og spar {saving:.2f} kr "
                f"mod om {best_when} min"
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

        threshold = vp_now
        minutes = best_when
        span = 0
        while minutes <= self.horizon_minutes:
            price = plan.marginal(minutes)
            if price is None:
                break
            heat = self.cheapest_heat(
                price.kr_per_kwh, self._cop_for(minutes, cop_now, cop_later)
            )
            if heat <= threshold:
                break
            span += SLOT_MINUTES
            minutes += SLOT_MINUTES
        return demand_kw * span / 60


    # ------------------------------------------------------------ fremskrivning

    def project(
        self,
        plan: Any,
        cop_now: float | None,
        cop_later: Any = None,
        target_minutes: int | None = None,
        grid: Any = None,
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
        return rows


def _with(decision: Decision, **changes: Any) -> Decision:
    from dataclasses import replace

    return replace(decision, **changes)
