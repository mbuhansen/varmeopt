"""COP-tabel: læring, opslag og 2D-interpolation.

Tabellen er indlært på det kørende anlæg og indekseret som
``table[fremløbstemperatur][udetemperatur] -> Cell``, begge i hele grader.

Den udgave tabellen kom fra havde en fejl her: alle interpolerede opslag
returnerede ``count: 0``, hvorfor både ``count >= 5``- og ``count > 0``-grenene
fejlede og opslaget faldt tilbage på TA-kurven. Kun eksakte celletræf blev
reelt brugt. Denne udgave fører et *effektivt* målingsantal med gennem
interpolationen, så en interpoleret værdi vejer efter hvor godt de celler den
kom fra er belagt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

# Antal målinger før en lært celle står helt på egne ben. Under det blandes
# den med faldet - setpunkt-tabellen - eller TA-kurven, i forhold til hvor
# godt den er belagt.
FULL_TRUST_COUNT = 5.0

# Ekstrapolation ud over tabellens kant får højst denne vægt, uanset hvor
# mange målinger nabocellen har. Vi tror ikke på tal vi aldrig har målt.
EXTRAPOLATION_COUNT_CAP = 2.0

# Henfaldslængden, i grader, for evidensen uden for en rækkes kant. Uden en
# glidning tog skridtet fra en rækkes sidste celle til en hundrededel uden
# for den alle målingerne på én gang.
EXTRAPOLATION_FADE_K = 0.5

# Inden for så mange grader fremløb regnes en naborrække som evidens om
# det samme driftspunkt - svagere, jo længere væk den ligger.
REINFORCE_K = 5

# Hvor stor en del af Carnot en måling højst må være for at tros. Anlæggets
# højeste af 17.204 målinger ligger på 70,9 %, så 75 % accepterer alt
# virkeligt med luft og afviser stadig hvad en fejlbehæftet føler finder på.
CARNOT_FRACTION = 0.75

# Absolut loft uanset løft. Ved mildt vejr er Carnot-grænsen så høj at den
# ikke længere begrænser noget; den højest målte på anlægget er 5,88.
ABSOLUTE_MAX_COP = 7.0

# Under 1 leverer maskinen mindre varme end den bruger strøm.
ABSOLUTE_MIN_COP = 1.0

# Fremløbstemperaturer uden for dette spænd læres ikke.
MIN_FLOW_TEMP = 20
MAX_FLOW_TEMP = 65

# TA-kurvernes fabriksværdier, brugt indtil der er lært nok.
_TA_CURVES: dict[int, dict[int, float]] = {
    35: {-15: 3.0, -10: 3.3, -5: 3.8, 0: 4.0, 5: 4.4, 10: 4.9, 15: 5.4, 25: 5.4},
    45: {-15: 2.4, -10: 2.8, -5: 3.1, 0: 3.9, 5: 4.2, 10: 4.2, 15: 4.5, 20: 4.5, 25: 4.5},
    55: {-15: 2.1, -10: 2.2, -5: 2.5, 0: 3.1, 5: 3.3, 10: 3.5, 15: 3.8, 25: 3.8},
    60: {-15: 2.1, -10: 2.2, -5: 2.5, 0: 2.6, 5: 3.3, 10: 3.3, 15: 3.5, 20: 3.5, 25: 3.5},
}


@dataclass(frozen=True)
class Cell:
    cop: float
    count: float


@dataclass(frozen=True)
class Lookup:
    """Resultatet af et COP-opslag, med hele begrundelsen."""

    cop: float
    source: str  # "exact" | "interp" | "blend" | "curve" | "fallback"
    detail: str
    learned_cop: float | None = None
    learned_count: float = 0.0

    @property
    def is_learned(self) -> bool:
        return self.source in ("exact", "interp")


def _carnot_ceiling(flow: float, outdoor: float) -> float:
    """Det termodynamiske loft, ganget med en generøs virkningsgrad.

    Den gamle udgave havde faste bånd, og de var forkerte. Loftet på 4,0 for
    delta-T mellem 40 og 55 K lå på **medianen** af netop det bånd der rummer
    halvdelen af anlæggets drift: 5.562 af 8.758 målinger lå over det. De
    tungeste var brugsvandet — F56/U8 med 740 målinger på COP 4,03.

    Værre end at kassere dem var at trunkeringen kun ramte den ene ende. En
    celle med sand fordeling N(4,22; 0,30) og loft 4,0 konvergerer mod 3,83 —
    9 % for lavt — og `count` vokser fire gange for langsomt. Ved break-even
    COP 3,12 er 9 % pessimisme direkte i den beslutning der skal træffes.

    Carnot er den rigtige form: loftet skal falde med temperaturløftet, og det
    gør det af sig selv. Målt på anlæggets 17.204 målinger ligger den højeste
    på 70,9 % af Carnot, så 75 % accepterer alt virkeligt med luft — og
    afviser stadig alt hvad en fejlbehæftet føler kan finde på.
    """
    lift = max(1.0, flow - outdoor)
    return min(ABSOLUTE_MAX_COP, CARNOT_FRACTION * (flow + 273.15) / lift)


def plausible_cop_range(flow: float, outdoor: float) -> tuple[float, float]:
    """Hvad der overhovedet kan være en ægte måling ved dette temperaturløft.

    Loftet er termodynamisk — se ``_carnot_ceiling``. Gulvet er fladt: under
    COP 1 leverer maskinen mindre varme end den bruger strøm, og det er
    afrimning eller en fejl, ikke et driftspunkt.

    Det gamle gulv steg til 2,0 ved lille løft og kasserede dermed ægte
    afrimnings- og dellastmålinger. En maskine der modulerer ned, *har* lave
    COP'er, og de hører med i gennemsnittet.
    """
    return ABSOLUTE_MIN_COP, _carnot_ceiling(flow, outdoor)


def _interp_1d(x: float, points: dict[int, float]) -> float:
    keys = sorted(points)
    if x <= keys[0]:
        return points[keys[0]]
    if x >= keys[-1]:
        return points[keys[-1]]
    for lo, hi in zip(keys, keys[1:]):
        if lo <= x <= hi:
            span = hi - lo
            if span == 0:
                return points[lo]
            return points[lo] + (points[hi] - points[lo]) * (x - lo) / span
    return points[keys[-1]]


def ta_curve_cop(flow: float, outdoor: float) -> float:
    """Fabrikskurvernes COP. Bruges når der ikke er lært nok."""
    v = {t: _interp_1d(outdoor, c) for t, c in _TA_CURVES.items()}
    if flow <= 35:
        return v[35]
    if flow >= 60:
        return v[60]
    if flow <= 45:
        return v[35] + (v[45] - v[35]) * (flow - 35) / 10
    if flow <= 55:
        return v[45] + (v[55] - v[45]) * (flow - 45) / 10
    return v[55] + (v[60] - v[55]) * (flow - 55) / 5


def _blend_count(c1: float, w1: float, c2: float, w2: float) -> float:
    """Hvor meget evidens der står bag et vægtet gennemsnit af to celler.

    Et vægtet gennemsnit har variansen ``w1²σ²/n1 + w2²σ²/n2``, så det
    tilsvarende antal målinger er ``1/(w1²/n1 + w2²/n2)``. Kvadratet er ikke
    en detalje: der stod ``w/n``, og forskellen er hvor hurtigt et lille
    islæt af en tynd celle æder troværdigheden.

    Blander man 99 % af en celle med 100 målinger med 1 % af en med én, gav
    den gamle form 50 — halveret af en hundrededel. Den rigtige giver 100.
    Ved 90/10 mod n=100 og n=5 var det 34,5 mod 99.

    Loftet er ``max(n1, n2)``. Rent variansmæssigt kan to uafhængige skøn
    tilsammen bære mere end hver for sig, men her er de skøn over *hvert
    sit* driftspunkt, og interpolationen har derfor også en bias som
    variansregningen ikke ser. Vi påstår aldrig at vide mere om et punkt
    imellem end vi ved om det bedst målte endepunkt.
    """
    # En side uden vægt bidrager intet - heller ikke sin mangel på evidens.
    terms = [(c, w) for c, w in ((c1, w1), (c2, w2)) if w > 0.0]
    if not terms or any(c <= 0 for c, _ in terms):
        return 0.0
    variance_based = 1.0 / sum(w * w / c for c, w in terms)
    return min(max(c for c, _ in terms), variance_based)


class CopTable:
    """Indlært COP som funktion af fremløbs- og udetemperatur."""

    def __init__(
        self,
        table: dict[int, dict[int, Cell]] | None = None,
        fallback: CopTable | None = None,
    ) -> None:
        self._table: dict[int, dict[int, Cell]] = table or {}
        # Den tabel der svarer, hvor den her ikke ved nok. Uden et fald er
        # det fabrikskurven. Faldet læres ikke og gemmes ikke med tabellen -
        # det er setpunkt-tabellen, som skal ligge urørt, mens tabellen på
        # varmepumpens eget fremløb, BT12, fyldes op.
        self.fallback = fallback

    # ------------------------------------------------------------- indlæsning

    @classmethod
    def from_raw(cls, raw: Any) -> tuple[CopTable, list[str]]:
        """Læs en rå tabel og fortæl hvad der blev kasseret.

        Node-RED-tabellen indeholder en ``"NaN"``-fremløbsrække, fordi
        ``Math.round(undefined)`` giver ``NaN`` når fremløbstemperaturen
        mangler. Den slags skal ikke med videre.
        """
        table: dict[int, dict[int, Cell]] = {}
        dropped: list[str] = []
        if not isinstance(raw, dict):
            return cls(), ["roden er ikke et objekt"]

        for flow_key, row in raw.items():
            flow = _as_int(flow_key)
            if flow is None:
                dropped.append(f"fremløb {flow_key!r}")
                continue
            if not isinstance(row, dict):
                dropped.append(f"fremløb {flow}: rækken er ikke et objekt")
                continue
            for out_key, cell in row.items():
                outdoor = _as_int(out_key)
                if outdoor is None:
                    dropped.append(f"F{flow} ude {out_key!r}")
                    continue
                parsed = _as_cell(cell)
                if parsed is None:
                    dropped.append(f"F{flow}/U{outdoor}: {cell!r}")
                    continue
                table.setdefault(flow, {})[outdoor] = parsed

        return cls(table), dropped

    def to_raw(self) -> dict[str, dict[str, dict[str, float]]]:
        return {
            str(flow): {
                str(out): {"cop": c.cop, "count": c.count}
                for out, c in sorted(row.items())
            }
            for flow, row in sorted(self._table.items())
        }

    # -------------------------------------------------------------- statistik

    @property
    def cell_count(self) -> int:
        return sum(len(row) for row in self._table.values())

    @property
    def sample_count(self) -> float:
        return sum(c.count for row in self._table.values() for c in row.values())

    @property
    def flow_temps(self) -> list[int]:
        return sorted(self._table)

    def row(self, flow: int) -> dict[int, Cell]:
        return dict(self._table.get(flow, {}))

    # ----------------------------------------------------------------- læring

    def learn(self, flow: float, outdoor: float, cop: float) -> str:
        """Indarbejd en måling. Returnerer en status der kan logges."""
        if not _finite(cop) or cop == 0:
            return "ignoreret: pumpen står stille"
        if not _finite(flow) or not _finite(outdoor):
            return "ignoreret: mangler temperaturdata"

        f, u = round(flow), round(outdoor)
        if not MIN_FLOW_TEMP <= f <= MAX_FLOW_TEMP:
            return f"ignoreret: fremløb {f} °C uden for {MIN_FLOW_TEMP}-{MAX_FLOW_TEMP}"

        lo, hi = plausible_cop_range(f, u)
        if not lo <= cop <= hi:
            return f"ignoreret: COP {cop:.2f} uden for {lo:.1f}-{hi:.1f} ved delta-T {f - u} K"

        row = self._table.setdefault(f, {})
        old = row.get(u)
        if old is None:
            row[u] = Cell(cop=float(cop), count=1.0)
            return f"ny celle F{f}/U{u} = {cop:.2f}"

        count = old.count + 1
        alpha = 0.15 if count < 10 else 0.05
        row[u] = Cell(cop=old.cop * (1 - alpha) + cop * alpha, count=count)
        return f"F{f}/U{u} = {row[u].cop:.2f} (n={count:.0f})"

    # ----------------------------------------------------------------- opslag

    def lookup(self, flow: float, outdoor: float) -> Lookup:
        learned = self._learned_at(flow, outdoor)

        if learned is not None and learned[1] >= FULL_TRUST_COUNT:
            cop, count, detail = learned
            source = "exact" if detail == "eksakt" else "interp"
            return Lookup(cop, source, detail, learned_cop=cop, learned_count=count)

        # Det der blandes med, når tabellen selv er tynd: faldet hvis der er
        # et, ellers fabrikskurven. Setpunkt-tabellen er et langt bedre gæt
        # end kurven - den er sytten tusind målinger på det her anlæg - men
        # den skal vige i samme takt som den nye tabel får sine egne.
        if self.fallback is not None:
            base = self.fallback.lookup(flow, outdoor)
            if learned is None:
                return replace(
                    base, source="fallback", detail=f"setpunkt-tabel: {base.detail}"
                )
            under = base.cop
        else:
            under = ta_curve_cop(flow, outdoor)
            if learned is None:
                return Lookup(cop=under, source="curve", detail="TA-kurve")

        cop, count, detail = learned
        weight = count / FULL_TRUST_COUNT
        blended = under * (1 - weight) + cop * weight
        return Lookup(
            cop=blended,
            source="blend",
            detail=f"{detail}, {weight * 100:.0f} % lært",
            learned_cop=cop,
            learned_count=count,
        )

    def _learned_at(
        self, flow: float, outdoor: float
    ) -> tuple[float, float, str] | None:
        rows = self.flow_temps
        if not rows:
            return None

        f_low = max((f for f in rows if f <= flow), default=None)
        f_high = min((f for f in rows if f >= flow), default=None)

        if f_low is not None and f_low == f_high:
            got = self._interp_row(self._table[f_low], outdoor)
            if got is None:
                return None
            cop, count, how = got
            count = _row_evidence(count)
            plain = how if how == "eksakt" else f"F{f_low}, {how}"
            return self._reinforce(flow, outdoor, cop, count, how, plain, {f_low: 1.0})

        # Uden for tabellens fremløbsspænd: brug nærmeste række, men lad den
        # kun tælle som svag evidens.
        if f_high is None and f_low is not None:
            return self._edge_row(f_low, flow, outdoor)
        if f_low is None and f_high is not None:
            return self._edge_row(f_high, flow, outdoor)
        if f_low is None or f_high is None:
            return None

        low = self._interp_row(self._table[f_low], outdoor)
        high = self._interp_row(self._table[f_high], outdoor)
        if low is None and high is None:
            return None
        if low is None:
            assert high is not None
            return high[0], min(high[1], EXTRAPOLATION_COUNT_CAP), f"kun F{f_high}"
        if high is None:
            return low[0], min(low[1], EXTRAPOLATION_COUNT_CAP), f"kun F{f_low}"

        span = f_high - f_low
        w_high = (flow - f_low) / span
        w_low = 1.0 - w_high
        # En række vejer kun i det omfang den ved noget her. Ligger
        # udetemperaturen langt uden for rækkens celler, er dens evidens
        # glidet ud mod nul - og så satte den før hele blandingen til nul,
        # også med en vægt på en titusindedel: fra F56,0 til F56,0001 gik
        # COP'en fra 4,03 til faldets 3,42, fordi F57 kun var målt i frost.
        #
        # Så hver side vejer ``w * min(1, n)``, og den evidens der er tilbage,
        # ganges med den masse der er tilbage. Rigtige celler - en hel måling
        # eller mere - interpoleres præcis som altid. En side uden evidens
        # overlader sin vægt til lånet, hvor den anden række står som nabo;
        # det er det samme regnestykke som når fremløbet rammer rækken.
        mass_low = w_low * min(1.0, low[1])
        mass_high = w_high * min(1.0, high[1])
        mass = mass_low + mass_high
        if mass > 0.0:
            cop = (low[0] * mass_low + high[0] * mass_high) / mass
            count = mass * _blend_count(
                low[1], mass_low / mass, high[1], mass_high / mass
            )
        else:
            cop = low[0] * w_low + high[0] * w_high
            count = 0.0
        how = f"interp F{f_low}-{f_high}"
        return self._reinforce(
            flow, outdoor, cop, count, how, how, {f_low: w_low, f_high: w_high}
        )

    def _reinforce(
        self,
        flow: float,
        outdoor: float,
        cop: float,
        count: float,
        how: str,
        plain: str,
        weights: dict[int, float],
    ) -> tuple[float, float, str]:
        """Lån evidens fra naborækkerne når opslaget selv er tyndt.

        Rammer fremløbet en række der findes, brugtes før kun den — også når
        den kun havde et par målinger ved denne udetemperatur, og også når en
        række fire grader væk havde snesevis. Resten blev hentet i
        fabrikskurven i stedet, som ikke ved noget om dette anlæg.

        **Alt her glider.** Lånet blev før slået til og fra: af en tærskel
        (kun under fem målinger), af et stop (når vægten nåede fem) og af en
        hård grænse (fem grader væk). Hver af de tre er et spring, og kl. 22
        den 13. september gik COP'en 3,47 -> 4,03 -> 3,37 på få minutter,
        mens setpunktet krøb en tiendedel og udetemperaturen vippede mellem
        13,0 og 13,1. Beslutningen vippede med. Nu:

        * Lånet fylder kun op til de fem målinger opslaget mangler, og det
          skaleres ned i stedet for at stoppe. Når opslaget selv når fem,
          er der intet at låne, og vejen derhen er lineær. En fade på
          ``1 - count/5`` alene var ikke nok: naborækkerne kan have halvtreds
          målingers vægt, og så gik COP'en næsten lodret det sidste stykke.
        * Alle rækker inden for rækkevidden tæller, og hver af dem fader ud
          med afstanden, så en række ved kanten bidrager nul og ikke n/6.
        * En række der selv indgår i interpolationen, låner kun den del af
          sig selv den ikke allerede bidrager med, ``1 - w``. Uden det fik
          F33 fuld vægt som nabo ved fremløb 34,0 og ingen ved 33,99.

        Naboerne vejer med deres egen evidens delt med afstanden, så den
        rigtige række vejer stadig tungest pr. måling.
        """
        missing = FULL_TRUST_COUNT - count
        if missing <= 0.0:
            return cop, count, plain

        borrowed: list[tuple[int, float, float]] = []
        for other in sorted(self.flow_temps, key=lambda f: abs(f - flow)):
            distance = abs(other - flow)
            reach = 1.0 - distance / REINFORCE_K
            own = weights.get(other, 0.0)
            if reach <= 0.0 or own >= 1.0:
                continue
            got = self._interp_row(self._table[other], outdoor)
            if got is None:
                continue
            share = got[1] / (1 + distance) * reach * (1.0 - own)
            if share > 0.0:
                borrowed.append((other, got[0], share))

        offered = sum(share for _, _, share in borrowed)
        if offered <= 0.0:
            return cop, count, plain
        scale = min(1.0, missing / offered)
        total = cop * count + sum(c * share * scale for _, c, share in borrowed)
        weight = count + offered * scale
        used = "+".join(f"F{other}" for other, _, _ in borrowed)
        return total / weight, weight, f"{how} styrket af {used}"

    def _edge_row(
        self, row_flow: int, flow: float, outdoor: float
    ) -> tuple[float, float, str] | None:
        """Nærmeste række, når fremløbet ligger uden for tabellen.

        Evidensen tages ikke fra rækken i ét hug ved kanten. Den glider ned
        til loftet over ``EXTRAPOLATION_FADE_K`` grader, og naborækkerne
        låner med som inde i tabellen - ellers sprang opslaget på skridtet
        fra rækken selv til en hundrededel uden for den.
        """
        got = self._interp_row(self._table[row_flow], outdoor)
        if got is None:
            return None
        cop, count, _ = got
        count = _row_evidence(_faded_cap(count, abs(flow - row_flow)))
        how = f"nærmeste F{row_flow}"
        return self._reinforce(flow, outdoor, cop, count, how, how, {row_flow: 1.0})

    @staticmethod
    def _interp_row(
        row: dict[int, Cell], outdoor: float
    ) -> tuple[float, float, str] | None:
        """Interpolér over udetemperatur inden for én fremløbsrække."""
        if not row:
            return None
        keys = sorted(row)

        if outdoor in row:
            cell = row[int(outdoor)]
            return cell.cop, cell.count, "eksakt"

        # Uden for rækkens udetemperaturer er evidensen svag - men den
        # glider ned til loftet i stedet for at falde i ét skridt. Fra 13,0
        # til 12,99 gik F33 fra atten målinger til to.
        if outdoor <= keys[0]:
            cell = row[keys[0]]
            return (
                cell.cop,
                _faded_cap(cell.count, keys[0] - outdoor),
                f"U<={keys[0]}",
            )
        if outdoor >= keys[-1]:
            cell = row[keys[-1]]
            return (
                cell.cop,
                _faded_cap(cell.count, outdoor - keys[-1]),
                f"U>={keys[-1]}",
            )

        for lo, hi in zip(keys, keys[1:]):
            if lo <= outdoor <= hi:
                span = hi - lo
                w_hi = (outdoor - lo) / span
                w_lo = 1.0 - w_hi
                cop = row[lo].cop * w_lo + row[hi].cop * w_hi
                count = _blend_count(row[lo].count, w_lo, row[hi].count, w_hi)
                return cop, count, f"interp U{lo}-{hi}"
        return None


def _row_evidence(count: float) -> float:
    """Én rækkes evidens, regnet som interpolationen regner den.

    Under én måling vejer rækken ``n * n`` - den samme masse gange den samme
    evidens som i ``_learned_at``. Ellers gav rækken selv et andet tal end
    interpolationen en titusindedel ved siden af.
    """
    return count * min(1.0, count)


def _faded_cap(count: float, beyond: float) -> float:
    """Evidens uden for en række: ned mod loftet, og derfra ud til ingenting.

    Den henfalder eksponentielt mod ``EXTRAPOLATION_COUNT_CAP``. Loftet gjaldt
    før uanset afstand, så en række 14 grader væk stadig vejede to målinger -
    og med BT12-tabellen fik én måling ved F45 en femtedel af opslaget ved
    setpunkt 31, hvor setpunkt-tabellen havde snesevis. Nu fader den ud over
    samme rækkevidde som naborækkerne låner inden for.

    Eksponentielt og ikke lineært: lineært fra 52 målinger til 2 over en grad
    tog zonen under fem målinger - hvor lånet og fabrikskurven tager over -
    ned på seks hundrededele af en grad, og COP'en faldt 0,33 der. Et henfald
    gør den zone lige bred, uanset hvor mange målinger rækken havde.
    """
    beyond = max(0.0, beyond)
    if count > EXTRAPOLATION_COUNT_CAP:
        decay = math.exp(-beyond / EXTRAPOLATION_FADE_K)
        count = EXTRAPOLATION_COUNT_CAP + (count - EXTRAPOLATION_COUNT_CAP) * decay
    return count * max(0.0, 1.0 - beyond / REINFORCE_K)


# ------------------------------------------------------------------- hjælpere


def _finite(x: Any) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return x == x and x not in (float("inf"), float("-inf"))


def _as_int(key: Any) -> int | None:
    """Tolerér heltal som tal eller streng, men afvis NaN og alt andet."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key
    if isinstance(key, float):
        return int(key) if key == key and key == int(key) else None
    if isinstance(key, str):
        try:
            return int(key.strip())
        except ValueError:
            return None
    return None


def _as_cell(value: Any) -> Cell | None:
    if not isinstance(value, dict):
        return None
    cop = value.get("cop")
    count = value.get("count", 0)
    # Bemærk: eksplicit None-tjek og ikke sandhedsværdi. Node-RED-udgaven
    # brugte ``?.cop`` og ville have kasseret en legitim nulværdi.
    if cop is None or not _finite(cop) or cop <= 0:
        return None
    if not _finite(count) or count < 0:
        return None
    return Cell(cop=float(cop), count=float(count))
