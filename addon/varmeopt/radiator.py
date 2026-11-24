"""Radiatorernes ventiler som mål for fremløbet - en skyggemåling.

Radiatorerne får lagerets vand direkte. Vejrkompenseringsventilen efter
tankene er sat ud af spil, så fremløbet ud mod huset (``entity_flow_measured``)
*er* tank A's afgang - de to følger hinanden inden for et par tiendedele.
Varmekurven bestemmer derfor ikke radiatorernes fremløb direkte. Den bestemmer
hvor varmt varmepumpen lader lageret, og det er det vand radiatorerne får,
når det er varmepumpen der har ladet.

Termostaterne (Danfoss Ally, med setpunkter fra RoomMind) åbner deres ventil
så meget rummet kræver. Står den mest åbne ventil i huset langt fra fuldt
åben, var vandet varmere end huset havde brug for, og varmepumpen kunne have
ladet koldere - med en bedre COP.

**Målingen styrer intet.** Hver cyklus læses ventilerne og rummene, og
logbogen gemmer halvtimens middel. Hver nat regnes på de sidste 21 dage:

    åbning = a + b · fremløb + c · ude

med mindste kvadrater over de halvtimer hvor ventilerne siger noget (se
``_usable``). Lageret svinger fra ~45 grader når varmepumpen har ladet, til
60-66 når pillefyret eller solvarmen har (brugerens oplysning 24. november),
så der er spredning nok i fremløbet til at se hvor meget ventilerne lukker
pr. grad - uden at nogen skal flytte kurven. Varmepumpens eget område ligger
i den nederste ende af det målte, så det nødvendige fremløb regnes inden for
data og ikke ud i det blå - så længe luften er nogle få grader.

Med ``b`` kendt kan det fremløb regnes, hvor den mest åbne ventil ville stå
på ``mål`` procent, og holdes op mod det varmepumpen leverer efter kurven.
Forskellen er luften i kurven. Kronerne er varmepumpens strøm til rumvarme
de sidste syv dage gange den COP der kunne være vundet, til den elpris der
gjaldt i halvtimen.

**Skævheden går den forsigtige vej.** Ventilerne reagerer langsomt på et
skift i fremløbet, så en halvtimes middel fanger kun en del af svaret, og
``b`` bliver for lille. Et for lille ``b`` gør det nødvendige fremløb højere
end det er, og luften mindre. Tallet er altså snarere for lavt end for højt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

WINDOW_DAYS = 21
# Kronerne regnes over en uge - det er den man kan holde op mod sin elregning.
PRICE_DAYS = 7
MIN_COVER = 0.95
# Under det står ventilerne i bund: intet rum kalder på varme, og åbningen
# siger intet om fremløbet. Ally melder 1 for en lukket ventil.
MIN_OPENING = 3.0
# Over det er ventilen i loftet: rummet ville have åbnet mere hvis det kunne,
# og sammenhængen med fremløbet er klippet af.
MAX_OPENING = 95.0
# Et rum der ligger mere end så meget under sit mål, får ikke nok - dets
# ventil er på vej op, ikke i ligevægt. RoomMinds morgenhævning er typisk.
MAX_SHORTFALL = 0.5
# To døgns halvtimer, og fremløbet skal have svinget: ellers kan ``b`` ikke
# skilles fra ``a``.
MIN_SLOTS = 96
MIN_SPREAD = 2.0
# Lukker ventilerne mindre end det pr. grad, er der ingen sammenhæng at
# regne på - og en division med et tal tæt på nul giver vilde fremløb.
MIN_SLOPE = 0.5
# Luften klemmes: længere ned end det er en ekstrapolation langt uden for det
# målte, og under ``FLOOR_SETPOINT`` køber et lavere fremløb næsten ingen COP
# på anlægget her (maskinen takter i den lave ende).
MAX_LUFT = 8.0
FLOOR_SETPOINT = 30.0
SLOT = 1800
# Udetemperaturer luften vises ved på siden.
SHOWN_OUTDOOR = (-5.0, 0.0, 5.0, 10.0)


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


# ------------------------------------------------------------------ rummene


@dataclass(frozen=True)
class Room:
    """Ét rum: termostaten der kender mål og temperatur, og dets ventiler."""

    climate: str
    valves: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.climate.split(".", 1)[-1].removeprefix("roommind_")


def parse_rooms(text: str) -> tuple[Room, ...]:
    """``climate.x=sensor.a,sensor.b; climate.y=sensor.c`` som rum.

    Et stykke uden lighedstegn eller uden ventiler springes over. Det er en
    indstilling, ikke en måling, så en tastefejl skal ikke vælte opstarten.
    """
    rooms = []
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        climate, valves = part.split("=", 1)
        climate = climate.strip()
        found = tuple(v.strip() for v in valves.split(",") if v.strip())
        if climate and found:
            rooms.append(Room(climate, found))
    return tuple(rooms)


@dataclass
class RoomState:
    name: str
    openings: list[float | None]
    current: float | None = None
    target: float | None = None
    # Hvorfor rummet ikke tæller lige nu - et åbent vindue eller en manuel
    # overstyring. Ventilen følger så noget andet end husets behov.
    excluded: str | None = None

    @property
    def shortfall(self) -> float | None:
        if _finite(self.current) and _finite(self.target):
            return self.target - self.current
        return None


def room_state(name: str, attributes: dict[str, Any] | None, openings: list[float | None]) -> RoomState:
    """Et rum ud fra termostatens attributter og ventilernes åbning."""
    attrs = attributes or {}
    excluded = None
    if attrs.get("window_open") is True:
        excluded = "vindue åbent"
    elif attrs.get("override_until") not in (None, ""):
        excluded = "overstyret"
    current = attrs.get("current_temperature")
    target = attrs.get("temperature")
    return RoomState(
        name=name,
        openings=openings,
        current=float(current) if _finite(current) else None,
        target=float(target) if _finite(target) else None,
        excluded=excluded,
    )


@dataclass
class Snapshot:
    """Ventilerne i ét minut."""

    top: float | None = None
    top_room: str | None = None
    second: float | None = None
    # Største underskud blandt rummene: mål minus målt.
    shortfall: float | None = None
    rooms: int = 0
    excluded: list[str] = field(default_factory=list)

    def to_raw(self) -> dict[str, Any]:
        return {
            "ventil_max": self.top, "ventil_rum": self.top_room,
            "ventil_anden": self.second, "rum_mangler": _round(self.shortfall, 2),
            "rum": self.rooms, "udeladt": self.excluded,
        }


def snapshot(rooms: Iterable[RoomState]) -> Snapshot:
    """Den mest åbne ventil, den næstmest åbne, og det største underskud.

    Den mest åbne er den der sætter grænsen: sænkes fremløbet, er det den der
    først når loftet. Den næste står med for at kunne se om ét rum alene
    holder kurven oppe.
    """
    out = Snapshot()
    valves: list[tuple[float, str]] = []
    shortfalls = []
    for room in rooms:
        if room.excluded:
            out.excluded.append(f"{room.name}: {room.excluded}")
            continue
        known = [o for o in room.openings if _finite(o)]
        if known:
            out.rooms += 1
            valves.extend((float(o), room.name) for o in known)
        if room.shortfall is not None:
            shortfalls.append(room.shortfall)
    valves.sort(key=lambda v: v[0], reverse=True)
    if valves:
        out.top, out.top_room = valves[0]
    if len(valves) > 1:
        out.second = valves[1][0]
    if shortfalls:
        out.shortfall = max(shortfalls)
    return out


# ---------------------------------------------------------------- regningen


def _round(value: Any, digits: int = 2) -> Any:
    return round(float(value), digits) if _finite(value) else None


def _usable(row: dict[str, Any]) -> tuple[float, float, float] | None:
    """(åbning, fremløb, ude) for en halvtime der kan regnes på, ellers None."""
    if (row.get("dækning") or 0) < MIN_COVER:
        return None
    skete = row.get("skete") or {}
    opening = skete.get("ventil_max")
    supply = skete.get("frem_hus")
    outdoor = skete.get("ude_middel")
    shortfall = skete.get("rum_mangler")
    if not (_finite(opening) and _finite(supply) and _finite(outdoor)):
        return None
    if not MIN_OPENING < opening < MAX_OPENING:
        return None
    # Ukendt underskud tæller som et underskud: uden termostaten ved vi ikke
    # om ventilen var i ligevægt.
    if not _finite(shortfall) or shortfall > MAX_SHORTFALL:
        return None
    return float(opening), float(supply), float(outdoor)


def _solve3(m: list[list[float]], v: list[float]) -> list[float] | None:
    """Løs 3x3 ved Gauss-elimination med delvis pivotering."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-9:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, 3):
            f = a[r][col] / a[col][col]
            for k in range(col, 4):
                a[r][k] -= f * a[col][k]
    x = [0.0, 0.0, 0.0]
    for r in (2, 1, 0):
        x[r] = (a[r][3] - sum(a[r][k] * x[k] for k in range(r + 1, 3))) / a[r][r]
    return x


@dataclass
class Fit:
    a: float
    b: float
    c: float
    n: int
    spread: float
    r2: float

    def needed_supply(self, outdoor: float, target: float) -> float:
        """Fremløbet hvor den mest åbne ventil står på ``target`` procent."""
        return (target - self.a - self.c * outdoor) / self.b


def fit(points: list[tuple[float, float, float]]) -> tuple[Fit | None, str]:
    """``åbning = a + b·fremløb + c·ude`` - eller hvorfor ikke."""
    n = len(points)
    if n < MIN_SLOTS:
        return None, f"samler: {n} brugbare halvtimer, skal bruge {MIN_SLOTS}"
    mean_s = sum(p[1] for p in points) / n
    spread = math.sqrt(sum((p[1] - mean_s) ** 2 for p in points) / n)
    if spread < MIN_SPREAD:
        return None, (
            f"fremløbet har kun svinget {spread:.1f} K - for lidt til at se "
            "hvad ventilerne gør pr. grad"
        )
    xs = [(1.0, s, u) for _, s, u in points]
    ys = [o for o, _, _ in points]
    m = [[sum(x[i] * x[j] for x in xs) for j in range(3)] for i in range(3)]
    v = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(3)]
    sol = _solve3(m, v)
    if sol is None:
        return None, "kan ikke skille fremløb og udetemperatur ad"
    a, b, c = sol
    mean_y = sum(ys) / n
    total = sum((y - mean_y) ** 2 for y in ys)
    resid = sum((y - (a + b * s + c * u)) ** 2 for y, (_, s, u) in zip(ys, xs))
    r2 = 1 - resid / total if total > 0 else 0.0
    result = Fit(a, b, c, n, spread, r2)
    if b > -MIN_SLOPE:
        return result, (
            f"ventilerne lukker kun {-b:.2f} %-point pr. grad - ingen "
            "sammenhæng at regne på"
        )
    return result, "ok"


class RadiatorReport:
    """Nattens regnestykke over logbogen - som rapport, ikke som styring."""

    def __init__(self) -> None:
        self.fit: Fit | None = None
        self.fit_note = "samler - ingen nat gennemgået endnu"
        self.last_night: str | None = None
        # Halvtimerne kronerne regnes på: (strøm til rumvarme, ude, elpris).
        # Samlet i tråden, prissat i hovedtråden hvor COP-tabellen må læses.
        self._recent: list[tuple[float, float, float | None]] = []
        self.luft_k: float | None = None
        self.saving_kwh: float | None = None
        self.saving_kr: float | None = None
        self.vp_el_kwh: float | None = None
        self.at_outdoor: list[dict[str, Any]] = []
        self.top_rooms: dict[str, int] = {}
        self.log: list[dict[str, Any]] = []

    @property
    def usable(self) -> bool:
        return self.fit is not None and self.fit.b <= -MIN_SLOPE

    def nightly(self, rows: list[dict[str, Any]], night: str) -> bool:
        """Tilpasningen og halvtimerne til kronerne. Kører i nattens tråd."""
        if night == self.last_night:
            return False
        self.last_night = night
        stamps = [int(r["t"]) for r in rows if _finite(r.get("t"))]
        if not stamps:
            return False
        newest = max(stamps)
        cut = newest - WINDOW_DAYS * 86400
        recent_cut = newest - PRICE_DAYS * 86400
        points = []
        recent = []
        rooms: dict[str, int] = {}
        for row in rows:
            t = row.get("t")
            if not _finite(t) or t < cut:
                continue
            point = _usable(row)
            if point is not None:
                points.append(point)
                room = (row.get("skete") or {}).get("ventil_rum")
                if isinstance(room, str):
                    rooms[room] = rooms.get(room, 0) + 1
            if t < recent_cut or (row.get("dækning") or 0) < MIN_COVER:
                continue
            skete = row.get("skete") or {}
            el = skete.get("vp_el_rum_kwh")
            outdoor = skete.get("ude_middel")
            if _finite(el) and el > 0 and _finite(outdoor):
                price = (row.get("så") or {}).get("el_kr")
                recent.append((float(el), float(outdoor), float(price) if _finite(price) else None))
        found, note = fit(points)
        # Alt sættes til sidst, så hovedtråden ikke ser en ny tilpasning med
        # gamle halvtimer.
        self.top_rooms = dict(sorted(rooms.items(), key=lambda kv: -kv[1]))
        self._recent = recent
        self.fit, self.fit_note = found, note
        return True

    def price(
        self,
        setpoint_at: Callable[[float], float | None],
        cop_at: Callable[[float, float], float | None],
        offset: float,
        target: float,
    ) -> None:
        """Luften og kronerne. Kører i hovedtråden: COP-tabellen læses her."""
        self.luft_k = self.saving_kwh = self.saving_kr = self.vp_el_kwh = None
        self.at_outdoor = []
        if not self.usable:
            self._remember()
            return
        assert self.fit is not None
        for outdoor in SHOWN_OUTDOOR:
            setpoint = setpoint_at(outdoor)
            if setpoint is None:
                continue
            needed = self.fit.needed_supply(outdoor, target)
            self.at_outdoor.append({
                "ude": outdoor,
                "setpunkt": _round(setpoint, 1),
                "leverer": _round(setpoint + offset, 1),
                "nok": _round(needed, 1),
                "luft": _round(setpoint + offset - needed, 1),
            })
        el_sum = saved = kr = luft_weighted = 0.0
        priced = False
        for el, outdoor, price in self._recent:
            setpoint = setpoint_at(outdoor)
            if setpoint is None:
                continue
            luft = self._luft(setpoint, offset, outdoor, target)
            cop_now = cop_at(setpoint, outdoor)
            cop_new = cop_at(setpoint - luft, outdoor)
            if not (_finite(cop_now) and _finite(cop_new)) or cop_new <= 0:
                continue
            el_sum += el
            luft_weighted += luft * el
            # Samme varme, ny COP: strømmen skaleres med forholdet.
            gain = el * max(0.0, 1 - cop_now / cop_new)
            saved += gain
            if price is not None:
                kr += gain * price
                priced = True
        if el_sum > 0:
            self.vp_el_kwh = round(el_sum, 1)
            self.luft_k = round(luft_weighted / el_sum, 1)
            self.saving_kwh = round(saved, 1)
            self.saving_kr = round(kr, 1) if priced else None
        self._remember()

    def _luft(self, setpoint: float, offset: float, outdoor: float, target: float) -> float:
        assert self.fit is not None
        needed = self.fit.needed_supply(outdoor, target)
        luft = setpoint + offset - needed
        return max(0.0, min(MAX_LUFT, luft, setpoint - FLOOR_SETPOINT))

    def _remember(self) -> None:
        if self.last_night is None:
            return
        entry = {
            "nat": self.last_night,
            "halvtimer": self.fit.n if self.fit else 0,
            "b": _round(self.fit.b, 2) if self.fit else None,
            "r2": _round(self.fit.r2, 2) if self.fit else None,
            "luft_k": self.luft_k,
            "sparet_kr_uge": self.saving_kr,
        }
        self.log = [e for e in self.log if e.get("nat") != self.last_night]
        self.log = (self.log + [entry])[-30:]

    @property
    def note(self) -> str:
        if not self.usable:
            return self.fit_note
        assert self.fit is not None
        head = (
            f"ventilerne lukker {-self.fit.b:.1f} %-point pr. grad "
            f"({self.fit.n} halvtimer, R² {self.fit.r2:.2f})"
        )
        if self.luft_k is None:
            return head + " - ingen rumvarme fra varmepumpen den sidste uge"
        money = (
            f", ca. {self.saving_kr:.1f} kr" if self.saving_kr is not None else ""
        )
        return (
            f"{head}. Kurven kunne have ligget {self.luft_k:.1f} K lavere: "
            f"{self.saving_kwh:.1f} kWh af {self.vp_el_kwh:.0f} kWh på en uge{money}"
        )

    def to_raw(self) -> dict[str, Any]:
        return {
            "last_night": self.last_night,
            "fit": (
                {
                    "a": round(self.fit.a, 3), "b": round(self.fit.b, 3),
                    "c": round(self.fit.c, 3), "n": self.fit.n,
                    "spread": round(self.fit.spread, 2), "r2": round(self.fit.r2, 3),
                }
                if self.fit is not None
                else None
            ),
            "fit_note": self.fit_note,
            "luft_k": self.luft_k,
            "saving_kwh": self.saving_kwh,
            "saving_kr": self.saving_kr,
            "vp_el_kwh": self.vp_el_kwh,
            "at_outdoor": self.at_outdoor,
            "top_rooms": self.top_rooms,
            "recent": [list(r) for r in self._recent],
            "log": self.log,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> RadiatorReport:
        out = cls()
        if not isinstance(raw, dict):
            return out
        f = raw.get("fit")
        if isinstance(f, dict) and all(_finite(f.get(k)) for k in ("a", "b", "c", "spread", "r2")):
            n = f.get("n")
            out.fit = Fit(
                float(f["a"]), float(f["b"]), float(f["c"]),
                n if isinstance(n, int) else 0, float(f["spread"]), float(f["r2"]),
            )
        if isinstance(raw.get("fit_note"), str):
            out.fit_note = raw["fit_note"]
        if isinstance(raw.get("last_night"), str):
            out.last_night = raw["last_night"]
        for key in ("luft_k", "saving_kwh", "saving_kr", "vp_el_kwh"):
            if _finite(raw.get(key)):
                setattr(out, key, float(raw[key]))
        if isinstance(raw.get("at_outdoor"), list):
            out.at_outdoor = [e for e in raw["at_outdoor"] if isinstance(e, dict)]
        if isinstance(raw.get("top_rooms"), dict):
            out.top_rooms = {
                k: v for k, v in raw["top_rooms"].items()
                if isinstance(k, str) and isinstance(v, int)
            }
        if isinstance(raw.get("recent"), list):
            out._recent = [
                (float(r[0]), float(r[1]), float(r[2]) if _finite(r[2]) else None)
                for r in raw["recent"]
                if isinstance(r, list) and len(r) == 3 and _finite(r[0]) and _finite(r[1])
            ]
        if isinstance(raw.get("log"), list):
            out.log = [e for e in raw["log"] if isinstance(e, dict)][-30:]
        return out

