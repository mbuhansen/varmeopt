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

# Under så mange observationer flytter en ny måling punktet mærkbart; derover
# er punktet velbestemt og skal ikke rykke sig på en enkelt aflæsning.
_SETTLED_COUNT = 10


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

    def is_dhw(self, setpoint: float) -> bool:
        return abs(setpoint - self.dhw_setpoint) <= DHW_TOLERANCE

    # --------------------------------------------------------------- læring

    def learn(self, outdoor: float, setpoint: float) -> str:
        """Indarbejd en observation. Returnerer en status der kan logges."""
        if not _finite(outdoor) or not _finite(setpoint):
            return "ignoreret: mangler data"
        if self.is_dhw(setpoint):
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
        """Hvor mange målinger står bag forudsigelsen på det sted."""
        if not self._points:
            return 0.0
        keys = self.outdoor_temps
        nearest = min(keys, key=lambda k: abs(k - outdoor))
        return self._points[nearest].count

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
        return cls(points, dhw_setpoint=dhw_setpoint)

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, dict[str, float]]:
        return {
            str(outdoor): {"setpoint": round(p.setpoint, 3), "count": p.count}
            for outdoor, p in sorted(self._points.items())
        }

    @classmethod
    def from_raw(cls, raw: Any, dhw_setpoint: float = 56.0) -> HeatCurve:
        points: dict[int, Point] = {}
        if isinstance(raw, dict):
            for key, cell in raw.items():
                try:
                    outdoor = int(key)
                    setpoint = float(cell["setpoint"])
                    count = float(cell.get("count", 0))
                except (TypeError, ValueError, KeyError):
                    continue
                if math.isfinite(setpoint) and math.isfinite(count):
                    points[outdoor] = Point(setpoint=setpoint, count=count)
        return cls(points, dhw_setpoint=dhw_setpoint)
