"""Radiatorernes ventiler og rum i logbogen - data til et forsøg i hånden.

Radiatorerne får lagerets vand direkte. Vejrkompenseringsventilen efter
tankene er sat ud af spil, så fremløbet ud mod huset (``entity_flow_measured``)
*er* tank A's afgang. Varmekurven bestemmer derfor ikke radiatorernes fremløb
direkte; den bestemmer hvor varmt varmepumpen lader lageret. Høj temperatur
fra tankene (60-66 grader) er pillefyr eller solvarme, varmepumpen giver ~45.

Spørgsmålet er om kurven kunne ligge lavere uden at rummene bliver kolde.

**Hvorfor her ikke regnes et tal.** Den første udgave fittede den mest åbne
Ally-ventil mod fremløb og udetemperatur hver nat og regnede luften i kurven
ud. Gennemgangen den 24. november, med en tørkørsel på seks døgns data fra
anlægget, viste at forudsætningen ikke holder:

* RoomMind tænder og slukker termostaterne - Ally får rummets komfort eller
  5 grader, op til 23 gange i døgnet - så ventilen står sjældent i ligevægt.
* Ally åbner ikke mere når rummet bliver koldt (Magnus værelse står på ~30 %
  med 0 og med 1 K underskud), så en ventil under 75 % er ikke en reserve.
* Fremløbet følger klokken - pillefyr og solvarme om eftermiddagen og
  aftenen - så vandets temperatur blandes med døgnets rytme. Dag alene gav
  39 grader nødvendigt fremløb, nat alene 30.

Svaret skal i stedet komme af et forsøg: kurven sænkes i hånden et par
grader i en uge, og logbogen viser om rummene holdt deres mål. Det her modul
sørger for at logbogen har det der skal til: den mest åbne ventil, husets
fremløb og hvert rums største underskud pr. halvtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _round(value: Any, digits: int = 2) -> Any:
    return round(float(value), digits) if _finite(value) else None


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
    # Hvorfor rummet ikke tæller lige nu - slukket, åbent vindue eller en
    # manuel overstyring. Ventilen og målet følger så noget andet end husets
    # behov.
    excluded: str | None = None

    @property
    def shortfall(self) -> float | None:
        if _finite(self.current) and _finite(self.target):
            return self.target - self.current
        return None


def room_state(
    name: str,
    state: str | None,
    attributes: dict[str, Any] | None,
    openings: list[float | None],
) -> RoomState:
    """Et rum ud fra termostatens tilstand og attributter og ventilernes åbning.

    En slukket termostat beholder sit mål: ``climate.roommind_gang`` står på
    off med 21,5. Talte den med, kunne et slukket rum der falder til 17 grader
    stå som et underskud på flere grader i hver eneste halvtime.
    """
    attrs = attributes or {}
    excluded = None
    if state == "off":
        excluded = "slukket"
    elif attrs.get("window_open") is True:
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
    """Ventilerne og rummene i ét minut."""

    top: float | None = None
    top_room: str | None = None
    second: float | None = None
    # Største underskud blandt rummene, mål minus målt - og pr. rum, så et
    # forsøg kan se *hvilket* rum der ikke holdt.
    shortfall: float | None = None
    shortfalls: dict[str, float] = field(default_factory=dict)
    rooms: int = 0
    excluded: list[str] = field(default_factory=list)

    def to_raw(self) -> dict[str, Any]:
        return {
            "ventil_max": self.top, "ventil_rum": self.top_room,
            "ventil_anden": self.second, "rum_mangler": _round(self.shortfall, 2),
            "rum": self.rooms, "udeladt": self.excluded,
        }


def snapshot(rooms: Iterable[RoomState]) -> Snapshot:
    """Den mest åbne ventil, den næstmest åbne, og rummenes underskud."""
    out = Snapshot()
    valves: list[tuple[float, str]] = []
    for room in rooms:
        if room.excluded:
            out.excluded.append(f"{room.name}: {room.excluded}")
            continue
        known = [o for o in room.openings if _finite(o)]
        if known:
            out.rooms += 1
            valves.extend((float(o), room.name) for o in known)
        if room.shortfall is not None:
            out.shortfalls[room.name] = room.shortfall
    valves.sort(key=lambda v: v[0], reverse=True)
    if valves:
        out.top, out.top_room = valves[0]
    if len(valves) > 1:
        out.second = valves[1][0]
    if out.shortfalls:
        out.shortfall = max(out.shortfalls.values())
    return out
