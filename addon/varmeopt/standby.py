"""Ståtabet målt, ikke gættet.

Lageret taber varme til rummet hele tiden. Hvor meget, ved vi ikke — og det
tal er det ene led der kan vende fortegnet på en for-opladning, fordi varme
der lades ind klokken fire og bruges klokken ni, har fem timer til at sive ud.

**Hvorfor det ikke bare kan aflæses.** Tabet er en effekt på et par hundrede
watt mod fire varmekilder der leverer titusinder. Det forsvinder i støjen så
længe noget som helst kører. Og huset trækker vand ud gennem en flowmåler der
kan vise nul ved reelle strømme op mod 100 l/h — så «flowmåleren siger nul» er
ikke et bevis for at der ikke trækkes noget.

Derfor er målingen noget man **starter med vilje**, når cirkulationspumpen er
slukket og man selv ved at der hverken går noget ind eller ud. Så er lagerets
temperaturfald over natten hele historien.

**Ét vindue er nok.** Tabet er proportionalt med temperaturforskellen til
rummet — det er varmeledning gennem isolering — så en enkelt måling giver
koefficienten direkte:

    UA [W/K] = tabt effekt [W] / (tanktemperatur − rumtemperatur) [K]

Derfor gemmes hvert vindue som et punkt, og koefficienten er middelværdien af
punkternes UA, vægtet med vinduets længde. Flere nætter ved forskellig
tanktemperatur gør den bedre, men den første nat giver et brugbart tal.

Regnestykket for et døgn: 1000 L taber ved 0,15-0,25 kW mellem 1,0 og 1,7 K
på otte timer. Med seks følere er støjen på middeltemperaturen omkring
0,04 K, så faldet er 25-40 gange støjen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .tank import WH_PER_LITER_K

# Under det her leverer en kilde ikke noget der betyder noget for et tab på
# et par hundrede watt. Over det er vinduet forurenet.
QUIET_KW = 0.05

# Kortere end det er faldet for lille til at kunne skelnes fra følerstøj.
MIN_HOURS = 2.0

# Tanken skal være varmere end rummet, ellers er der intet tab at måle og
# divisionen giver noget meningsløst.
MIN_DELTA_K = 5.0


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass(frozen=True)
class Window:
    """Et gennemført måleforløb."""

    date: str
    hours: float
    delta_k: float
    loss_kw: float

    @property
    def ua_w_per_k(self) -> float:
        """Tabskoefficienten: watt pr. kelvin forskel til rummet."""
        return self.loss_kw * 1000 / self.delta_k

    def to_raw(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "hours": round(self.hours, 3),
            "delta_k": round(self.delta_k, 3),
            "loss_kw": round(self.loss_kw, 4),
        }

    @classmethod
    def from_raw(cls, raw: Any) -> Window | None:
        if not isinstance(raw, dict):
            return None
        try:
            window = cls(
                date=str(raw["date"]),
                hours=float(raw["hours"]),
                delta_k=float(raw["delta_k"]),
                loss_kw=float(raw["loss_kw"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if window.hours <= 0 or window.delta_k <= 0:
            return None
        return window


@dataclass
class StandbyTest:
    """Måler ståtabet i et vindue brugeren selv åbner."""

    armed: bool = False
    started_at: float | None = None
    start_temp: float | None = None
    liters: float = 0.0
    # Middelværdien af (tank − rum) hen over vinduet, ikke bare ved starten:
    # tanken køler, og forskellen med den.
    _delta_sum: float = 0.0
    _delta_n: int = 0
    last_temp: float | None = None
    last_at: float | None = None
    note: str = "ikke i gang"
    windows: list[Window] = field(default_factory=list)

    # ------------------------------------------------------------- betjening

    def arm(self, now: float) -> str:
        """Begynd at måle. Vinduet starter først når der er tal at starte på."""
        self.armed = True
        self._reset()
        self.note = "klar — venter på første aflæsning"
        return self.note

    def disarm(self, now: float) -> str:
        """Afslut. Er vinduet langt nok, gemmes det som et punkt."""
        window = self._close()
        self.armed = False
        self._reset()
        if window is None:
            self.note = "afbrudt — for kort eller for lidt at måle på"
            return self.note
        self.windows.append(window)
        self.note = (
            f"målt {window.loss_kw * 1000:.0f} W over {window.hours:.1f} t "
            f"ved {window.delta_k:.1f} K — {window.ua_w_per_k:.1f} W/K"
        )
        return self.note

    def _reset(self) -> None:
        self.started_at = None
        self.start_temp = None
        self._delta_sum = 0.0
        self._delta_n = 0
        self.last_temp = None
        self.last_at = None

    # -------------------------------------------------------------- måling

    def observe(
        self,
        now: float,
        mean_temp: float | None,
        room_temp: float | None,
        liters: float,
        sources: dict[str, float] | None = None,
    ) -> str:
        """Ét skridt. Returnerer en status der kan vises og logges."""
        if not self.armed:
            return self.note

        busy = [
            name
            for name, kw in (sources or {}).items()
            if _finite(kw) and kw > QUIET_KW
        ]
        if busy:
            # Kører en kilde, er alt målt indtil nu ubrugeligt: vi kan ikke
            # skille et tab på 200 W fra en tilførsel på 8 kW. Vinduet
            # begynder forfra i stedet for at gemme et forurenet tal.
            self._reset()
            self.note = f"venter — {' + '.join(busy)} kører"
            return self.note

        if not _finite(mean_temp) or not _finite(room_temp) or liters <= 0:
            self.note = "venter — mangler tank- eller rumtemperatur"
            return self.note

        delta = mean_temp - room_temp
        if delta < MIN_DELTA_K:
            self._reset()
            self.note = f"venter — kun {delta:.1f} K over rummet"
            return self.note

        if self.started_at is None:
            self.started_at = now
            self.start_temp = mean_temp
            self.liters = liters

        self._delta_sum += delta
        self._delta_n += 1
        self.last_temp = mean_temp
        self.last_at = now

        hours = (now - self.started_at) / 3600
        drop = (self.start_temp or mean_temp) - mean_temp
        if hours < MIN_HOURS:
            self.note = (
                f"måler — {hours:.1f} t, faldet {drop:.2f} K "
                f"(mindst {MIN_HOURS:.0f} t)"
            )
        else:
            watts = drop * liters * WH_PER_LITER_K / hours
            self.note = f"måler — {hours:.1f} t, {watts:.0f} W indtil videre"
        return self.note

    def _close(self) -> Window | None:
        if (
            self.started_at is None
            or self.last_at is None
            or self.start_temp is None
            or self.last_temp is None
            or self._delta_n == 0
        ):
            return None

        hours = (self.last_at - self.started_at) / 3600
        if hours < MIN_HOURS:
            return None

        drop = self.start_temp - self.last_temp
        if drop <= 0:
            # Tanken blev varmere. Så gik der noget ind vi ikke så, og det
            # er ikke en måling af et tab.
            return None

        loss_kw = drop * self.liters * WH_PER_LITER_K / hours / 1000
        delta = self._delta_sum / self._delta_n
        from datetime import datetime

        return Window(
            date=datetime.now().astimezone().strftime("%Y-%m-%d"),
            hours=hours,
            delta_k=delta,
            loss_kw=loss_kw,
        )

    # -------------------------------------------------------------- resultat

    @property
    def ua_w_per_k(self) -> float | None:
        """Tabskoefficienten af alle målinger, vægtet med vinduernes længde."""
        if not self.windows:
            return None
        weight = sum(w.hours for w in self.windows)
        if weight <= 0:
            return None
        return sum(w.ua_w_per_k * w.hours for w in self.windows) / weight

    def loss_kw_at(self, mean_temp: float | None, room_temp: float | None) -> float | None:
        """Det aktuelle ståtab, skaleret til den forskel der er nu."""
        ua = self.ua_w_per_k
        if ua is None or not _finite(mean_temp) or not _finite(room_temp):
            return None
        return max(0.0, ua * (mean_temp - room_temp) / 1000)

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        # Selve vinduet gemmes med vilje ikke: en genstart midt i en måling
        # betyder at der gik et par minutter uden aflæsninger, og så er det
        # ærligere at begynde forfra end at regne hen over hullet.
        return {"windows": [w.to_raw() for w in self.windows]}

    @classmethod
    def from_raw(cls, raw: Any) -> StandbyTest:
        test = cls()
        if isinstance(raw, dict):
            for item in raw.get("windows") or []:
                window = Window.from_raw(item)
                if window is not None:
                    test.windows.append(window)
        if test.windows:
            test.note = f"{len(test.windows)} måling(er) gemt"
        return test
