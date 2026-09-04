"""UVR'ens varmekurve: udetemperatur ind, fremløbssetpunkt ud.

``sensor.node_1_analog_logging_13`` er ikke en måling — det er det setpunkt
UVR'en regner sig frem til ud fra udetemperaturen. Det kan ses direkte i de
17.167 indlærte COP-målinger: setpunktet falder glat og monotont fra 49 °C ved
−5 °C ude til 24 °C ved +24 °C, og lægger sig så fladt. En målt temperatur
ville støje; det her er en kurve med en nedre klemme.

Og det er præcis derfor den er værd at modellere. Fordi setpunktet er en
funktion af udetemperaturen, kan vejrudsigten regnes om til morgendagens
fremløb — og dermed til morgendagens COP. Det er byggestenen under
blokplanlægning: uden den kan man kun konstatere hvad COP *er*, ikke gætte
kvalificeret på hvad den *bliver*.

Varmtvand og spa kører på et fast setpunkt uafhængigt af vejret, og de
målinger hører ikke til i kurven. De kendes på at ligge på den faste værdi.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Setpunkter inden for dette af varmtvandsværdien regnes som brugsvand eller
# spa og læres ikke ind i varmekurven.
DHW_TOLERANCE = 0.5

# Hvor meget over kurven et setpunkt skal ligge, før det ikke længere kan være
# husets.
#
# Varmtvandet har ikke ét setpunkt. UVR'en kører det efter tidsplaner, og
# uden for planen varmer den op ved en lavere temperatur — 42, 44 eller 56
# alt efter hvornår på døgnet. De to lave tal ligger midt i vinterens rigtige
# fremløb (kurven er 44,7 ved +4 °C ude), så de kan ikke kendes på værdien
# alene. Men de kan kendes på *stedet*: 44 ved 19 °C ude er brugsvand, for
# huset beder om 27 der.
#
# Skævheden er altid opad. Brugsvand og spa varmer hedere end huset har brug
# for — ellers var der ingen grund til at gøre det — så en måling der ligger
# højt over kurven, er den mistænkte, og en der ligger lavt, er bare vejret.
# Derfor er testen ensidig, og derfor kan den ikke låse kurven fast: en
# rigtig justering nedad læres med det samme, og en opad inden for marginen.
DHW_MARGIN_K = 5.0

# Under så meget evidens på stedet kan kurven ikke afvise noget. Et punkt der
# selv er et gæt, skal ikke have vetoret over nye målinger.
VETO_COUNT = 5.0

# Under så mange observationer flytter en ny måling punktet mærkbart; derover
# er punktet velbestemt og skal ikke rykke sig på en enkelt aflæsning.
_SETTLED_COUNT = 10

# Kurvens format. Version 1 blev lært med en fejl: et negativt
# varmtvandsflag slog værditjekket fra, så 56 °C blev lært som om det var
# vejrkurven hver gang udgangen stod på nul mens spaen varmede. Skaden voksede
# til 16,6 K ved 19 °C ude. En kurve fra dengang kastes væk og udledes forfra
# af COP-tabellen, som er ren.
CURVE_VERSION = 2


@dataclass(frozen=True)
class Point:
    setpoint: float
    count: float


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class HeatCurve:
    """Indlært sammenhæng mellem udetemperatur og fremløbssetpunkt."""

    def __init__(self, points: dict[int, Point] | None = None, dhw_setpoint: float = 56.0) -> None:
        self._points: dict[int, Point] = dict(points or {})
        self.dhw_setpoint = dhw_setpoint

    # ------------------------------------------------------------------ form

    @property
    def outdoor_temps(self) -> list[int]:
        return sorted(self._points)

    @property
    def point_count(self) -> int:
        return len(self._points)

    @property
    def sample_count(self) -> float:
        return sum(p.count for p in self._points.values())

    def point(self, outdoor: int) -> Point | None:
        return self._points.get(outdoor)

    def is_dhw(self, setpoint: float, outdoor: float | None = None) -> bool:
        """Er det her setpunkt varmtvand eller spa frem for vejret?

        To spørgsmål, og det andet er det der virker om sommeren:

        1. Ligger det på varmtvandsværdien (56 °C)? Huset beder aldrig om så
           meget — kurven topper ved 54 i frost.
        2. Ligger det langt over hvad kurven selv siger på stedet? Så er det
           ikke vejret, uanset hvilket tal det er. Det fanger de lave
           varmtvandssetpunkter (42 og 44), som ikke kan kendes på værdien,
           fordi de er husets rigtige fremløb midt om vinteren.
        """
        if not _finite(setpoint):
            return False
        if abs(setpoint - self.dhw_setpoint) <= DHW_TOLERANCE:
            return True
        if outdoor is None or self.confidence(outdoor) < VETO_COUNT:
            return False
        predicted = self.predict(outdoor)
        return predicted is not None and setpoint > predicted + DHW_MARGIN_K

    # --------------------------------------------------------------- læring

    def learn(
        self, outdoor: float, setpoint: float, dhw: bool | None = None
    ) -> str:
        """Indarbejd en observation. Returnerer en status der kan logges.

        ``dhw`` er en kendsgerning fra anlægget, når den findes: står
        varmtvandsudgangen tændt, hører målingen ikke til i kurven, uanset
        hvad setpunktet tilfældigvis står på.

        Men et flag der siger *nej*, er ikke den samme slags kendsgerning.
        Her stod ``if self.is_dhw(setpoint) if dhw is None else dhw``, og den
        lod et negativt flag slå værditjekket fra: stod varmtvandsudgangen på
        nul mens spaen varmede, blev 56 °C lært som om det var vejrkurven.
        Det kostede 16,6 K ved 19 °C ude, hvor kurven kom til at stå på 44 i
        stedet for 27. Flaget må kun *tilføje* mistanke, aldrig fjerne den.
        """
        if not _finite(outdoor) or not _finite(setpoint):
            return "ignoreret: mangler data"
        if dhw or self.is_dhw(setpoint, outdoor):
            return f"ignoreret: varmtvand ({setpoint:.0f} °C)"

        key = round(outdoor)
        old = self._points.get(key)
        if old is None:
            self._points[key] = Point(setpoint=float(setpoint), count=1.0)
            return f"nyt punkt U{key} = {setpoint:.1f} °C"

        count = old.count + 1
        alpha = 0.15 if count < _SETTLED_COUNT else 0.05
        blended = old.setpoint * (1 - alpha) + setpoint * alpha
        self._points[key] = Point(setpoint=blended, count=count)
        return f"U{key} = {blended:.1f} °C (n={count:.0f})"

    # ----------------------------------------------------------- forudsigelse

    def predict(self, outdoor: float) -> float | None:
        """Hvilket fremløb vil UVR'en bede om ved den udetemperatur?

        Uden for det målte spænd klemmes der fast på nærmeste punkt frem for
        at forlænge kurven. Kurven har en reel bund — under en vis
        udetemperatur beder UVR'en ikke om mindre — og en lineær forlængelse
        ville finde på tal anlægget aldrig har vist os.
        """
        if not _finite(outdoor) or not self._points:
            return None

        keys = self.outdoor_temps
        if outdoor <= keys[0]:
            return self._points[keys[0]].setpoint
        if outdoor >= keys[-1]:
            return self._points[keys[-1]].setpoint

        for low, high in zip(keys, keys[1:]):
            if low <= outdoor <= high:
                a, b = self._points[low].setpoint, self._points[high].setpoint
                if high == low:
                    return a
                ratio = (outdoor - low) / (high - low)
                return a + (b - a) * ratio
        return None

    def confidence(self, outdoor: float) -> float:
        """Hvor meget evidens der står bag forudsigelsen *på det sted*.

        Afstanden tæller med. Før returnerede −20 °C de 70 målinger der står
        ved −10, som om der var målt dernede — der er bare ikke nogen målinger
        inden for 10 K. Nu falder vægten med afstanden ud over de par grader
        hvor naboceller er reelle naboer.
        """
        if not self._points:
            return 0.0
        keys = self.outdoor_temps
        nearest = min(keys, key=lambda k: abs(k - outdoor))
        distance = abs(nearest - outdoor)
        return self._points[nearest].count / (1 + max(0.0, distance - NEAR_ENOUGH_K))

    # --------------------------------------------------------------- bootstrap

    @classmethod
    def from_cop_table(cls, table: Any, dhw_setpoint: float = 56.0) -> HeatCurve:
        """Læs kurven ud af den allerede indlærte COP-tabel.

        COP-tabellen er indekseret på netop det setpunkt vi vil modellere, og
        hver celle bærer sit antal målinger. Den vægtede middelværdi pr.
        udetemperatur *er* kurven — så den behøver ikke læres forfra over
        uger, den findes allerede i de data der blev migreret.
        """
        weighted: dict[int, list[float]] = {}
        for flow in table.flow_temps:
            if abs(flow - dhw_setpoint) <= DHW_TOLERANCE:
                continue
            for outdoor, cell in table.row(flow).items():
                acc = weighted.setdefault(outdoor, [0.0, 0.0])
                acc[0] += flow * cell.count
                acc[1] += cell.count

        points = {
            outdoor: Point(setpoint=total / count, count=count)
            for outdoor, (total, count) in weighted.items()
            if count > 0
        }
        return cls(_enforce_monotone(points), dhw_setpoint=dhw_setpoint)

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        return {
            "version": CURVE_VERSION,
            "points": {
                str(outdoor): {"setpoint": round(p.setpoint, 3), "count": p.count}
                for outdoor, p in sorted(self._points.items())
            },
        }

    @classmethod
    def from_raw(cls, raw: Any, dhw_setpoint: float = 56.0) -> HeatCurve:
        points: dict[int, Point] = {}
        if isinstance(raw, dict):
            # Version 1 var punkterne selv; version 2 lagde dem i "points" og
            # skrev et versionsnummer ved siden af.
            cells = raw.get("points") if isinstance(raw.get("points"), dict) else raw
            for key, cell in cells.items():
                try:
                    outdoor = int(key)
                    setpoint = float(cell["setpoint"])
                    count = float(cell.get("count", 0))
                except (TypeError, ValueError, KeyError):
                    continue
                if math.isfinite(setpoint) and math.isfinite(count):
                    points[outdoor] = Point(setpoint=setpoint, count=count)
        return cls(points, dhw_setpoint=dhw_setpoint)


# Inden for saa mange grader regnes en nabocelle som evidens paa stedet.
NEAR_ENOUGH_K = 2.0


def _enforce_monotone(points: dict[int, Point]) -> dict[int, Point]:
    """Gør kurven ikke-stigende i udetemperatur.

    En varmekurve kan ikke andet: bliver det varmere ude, skal fremløbet ned.
    Men kurven her er et vægtet gennemsnit pr. udetemperatur af de setpunkter
    der tilfældigvis er målt, og belægningen er ikke ens fra grad til grad. Så
    kom U9 til at give 38,8 mod U10's 40,8 — en varmere prognose gav et
    *højere* setpunkt og dermed lavere COP, 2 K den forkerte vej, netop i
    efterårets beslutningsbånd hvor valget mellem kilderne er tættest.

    Rettelsen er vægtet isotonisk regression (pool adjacent violators): den
    nærmeste ikke-stigende kurve i mindste kvadraters forstand, hvor hvert
    punkt vejer med sit antal målinger. Et enkelt tyndt punkt kan altså ikke
    trække en velbelagt nabo med sig.
    """
    keys = sorted(points)
    if len(keys) < 2:
        return points

    # Hver blok er (sum af vægtet setpunkt, sum af vægte, antal punkter).
    blocks: list[list[float]] = []
    for key in keys:
        point = points[key]
        weight = max(point.count, 1e-9)
        blocks.append([point.setpoint * weight, weight, 1])
        # Stiger den nye blok over den forrige, brydes monotonien, og de to
        # slås sammen til deres faelles gennemsnit. Det kan bryde monotonien
        # bagud igen, saa der pooles indtil kaeden er faldende.
        while len(blocks) > 1 and blocks[-1][0] / blocks[-1][1] > blocks[-2][0] / blocks[-2][1]:
            merged = blocks.pop()
            blocks[-1][0] += merged[0]
            blocks[-1][1] += merged[1]
            blocks[-1][2] += merged[2]

    smoothed: dict[int, Point] = {}
    index = 0
    for total, weight, span in blocks:
        value = total / weight
        for key in keys[index : index + int(span)]:
            smoothed[key] = Point(setpoint=value, count=points[key].count)
        index += int(span)
    return smoothed

