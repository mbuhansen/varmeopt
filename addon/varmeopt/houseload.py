"""Husets varmeforbrug, læst af tankene når flowmåleren ikke kan svare.

Flowmåleren på centralvarmen har en bund: under omkring 100 l/h kan den vise
nul selv om der løber vand. Derfor er behovet *ukendt* derunder og ikke nul
— se ``demand.Load.kw`` — og når det er ukendt, falder alt det der hænger på
det, ud på én gang: nettobalancen, hvor længe lageret rækker, og det led i
opladningen der regner på hvor meget varme der faktisk bliver fortrængt.

Men der står et andet måleinstrument i kælderen. Tankene *er* et kalorimeter:

    huset [kWh] = ∫ kilder ind − (E_slut − E_start) − ståtab
    E           = Σ_lag  liter_pr_lag × T × 1,149 Wh/(L·K)

**Støjbudgettet.** Følerne kvantiserer i 0,1 °C, og middelværdien over de seks
dybdefølere støjer omkring 0,04 K — samme regnestykke som ståtabsmålingen
bygger på. På 1000 L er det 46 Wh. Et forbrug på 2,5 kW trækker til gengæld
1,25 kWh ud på en halv time, og det er 1,1 K — syvogtyve gange støjen. Vinduet
er derfor en halv time, og hældningen findes ved mindste kvadraters fit hen
over alle tredive aflæsninger frem for som forskellen mellem to endepunkter;
det tredeler støjen igen.

**Hvad der forurener en måling.** Varmtvandsbeholderen og spaen tapper de
samme buffertanke som huset. En lagerbalance kan ikke se forskel på et bad og
en radiator — den ser kun energi der forlader tankene — så de minutter må
kasseres, ellers bliver et brusebad til husets varmeforbrug. Det samme gælder
et vindue hvor varmepumpen kører uden en COP at regne ydelsen af, hvor antallet
af følere skifter undervejs, eller hvor der er hul i aflæsningerne.

**Bad og spa kan modelleres frem for at kasseres.** Varmtvandsbeholderen og
spaen tapper de samme tanke, og de to kan ikke skelnes fra huset i en
energibalance. Kasseres de vinduer, er målingen tavs fem timer om dagen, for
spaen kører 12-17 hver eneste dag. Kendes vessel-trækket omtrent — spaen på
omkring 3,5 kW, beholderen mellem 3 og 8 alt efter hvor kold den er — kan det
trækkes fra i stedet, og så bliver der målt videre. Men et modelleret vindue
er ikke en måling: det tæller med i det tal der vises nu, og aldrig i kurven.
Kurven skal blive ved med kun at kende rene vinduer.

**To skævheder, som er kendte og ikke skjulte.** Ståtabet til rummet tilskrives
huset indtil ``standby`` har målt det; det er 0,1-0,2 kW for meget ved et
forbrug på 2-3 kW. Og solvarmen er modelleret, ikke målt, så en skæv flowkurve
går direkte ind i tallet. Derfor lærer kurven mod udetemperaturen kun af
vinduer uden sol, mens den rullende måling gerne må køre hele døgnet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Vinduet der regnes over. En halv time er langt nok til at faldet er
# halvtreds gange følerstøjen, og kort nok til at et tal fra det stadig
# beskriver huset som det er nu.
WINDOW_MINUTES = 30.0

# Kortere end det er faldet for lille til at kunne skelnes, og et vindue med
# færre aflæsninger end det har vi ikke tillid til uanset længden.
MIN_MINUTES = 15.0
MIN_SAMPLES = 10

# Går der længere mellem to aflæsninger, er der et hul i integralet af
# kilderne, og så ved vi ikke hvad der løb ind imens. Cyklussen er 60
# sekunder, så fem minutter er rigelig plads til en langsom runde.
MAX_GAP_SECONDS = 300.0

# Hvor gammelt et målt tal må være, før det ikke længere beskriver nu.
MAX_AGE_MINUTES = 15.0

# Stiger energien mere end kilderne kan forklare, gik der noget ind vi ikke
# så. Så er det ikke en måling af husets forbrug. Grænsen er sat over
# støjen, så et lille minus stadig bare bliver til nul.
UNEXPLAINED_KW = 0.5

# Under så mange observationer flytter en ny måling punktet mærkbart;
# derover er punktet velbestemt. Samme grænse som varmekurven bruger.
_SETTLED_COUNT = 10

# Inden for så mange grader regnes en nabocelle som evidens på stedet.
NEAR_ENOUGH_K = 2.0

# Hvor længe målinger gemmes til grafen, og hvor tit der gemmes et punkt.
# Én pr. vindue over fjorten dage er 672 punkter - nok til at se et døgns
# form og en uges vejr, og lille nok til en fil der skrives hvert femte
# minut.
HISTORY_DAYS = 14.0

# Så meget af en time skal være set, før den tæller med i døgnprofilen.
# En genstart midt i timen må ikke tælle som om beholderen stod stille
# resten af den.
MIN_HOUR_SECONDS = 1800.0

# Under så mange døgn bag en time flytter en ny dag den mærkbart; derover
# er vanen kendt og skal ikke rykke sig på én aften.
_SETTLED_DAYS = 5.0


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _slope(points: list[tuple[float, float]]) -> float | None:
    """Hældningen af en ret linje gennem punkterne, mindste kvadraters fit.

    Endepunktsdifferensen bruger to aflæsninger og arver derfor støjen fra
    begge. Hældningen bruger dem alle tredive, og det er hele grunden til at
    en halv time rækker.
    """
    n = len(points)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return sxy / sxx


@dataclass(frozen=True)
class Point:
    """Ét punkt på forbrugskurven: hvad huset trak ved den udetemperatur."""

    kw: float
    count: float


class LoadCurve:
    """Indlært sammenhæng mellem udetemperatur og husets varmeforbrug.

    Samme form som varmekurven i ``curve.py``: ét punkt pr. hele grad, med
    antallet af målinger bag sig, og en ny måling der flytter punktet mindre
    jo bedre bestemt det er.

    Én ting er anderledes, og den har fysik bag sig. Varmekurven klemmer fast
    på det yderste punkt uden for det målte spænd, fordi UVR'ens kurve har en
    reel bund. Husets tab har ikke en bund — det er proportionalt med
    forskellen mellem inde og ude — så her forlænges den nærmeste linje i
    stedet. Med det forbehold at forbruget ikke må *stige* med
    udetemperaturen: peger de to nærmeste punkter den vej, er det støj, og så
    holdes tallet fladt.
    """

    def __init__(self, points: dict[int, Point] | None = None) -> None:
        self._points: dict[int, Point] = dict(points or {})

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

    # --------------------------------------------------------------- læring

    def learn(self, outdoor: float, kw: float) -> str:
        if not _finite(outdoor) or not _finite(kw) or kw < 0:
            return "ignoreret: mangler data"

        key = round(outdoor)
        old = self._points.get(key)
        if old is None:
            self._points[key] = Point(kw=float(kw), count=1.0)
            return f"nyt punkt U{key} = {kw:.2f} kW"

        count = old.count + 1
        alpha = 0.15 if count < _SETTLED_COUNT else 0.05
        blended = old.kw * (1 - alpha) + kw * alpha
        self._points[key] = Point(kw=blended, count=count)
        return f"U{key} = {blended:.2f} kW (n={count:.0f})"

    # ----------------------------------------------------------- forudsigelse

    def predict(self, outdoor: float) -> float | None:
        """Hvad huset trækker ved den udetemperatur."""
        if not _finite(outdoor) or not self._points:
            return None

        keys = self.outdoor_temps
        if len(keys) == 1:
            return self._points[keys[0]].kw

        if outdoor < keys[0]:
            return self._extend(keys[0], keys[1], outdoor)
        if outdoor > keys[-1]:
            return self._extend(keys[-1], keys[-2], outdoor)

        for low, high in zip(keys, keys[1:]):
            if low <= outdoor <= high:
                a, b = self._points[low].kw, self._points[high].kw
                if high == low:
                    return a
                return a + (b - a) * (outdoor - low) / (high - low)
        return None

    def _extend(self, near: int, other: int, outdoor: float) -> float:
        """Forlæng linjen gennem de to yderste punkter — men aldrig opad."""
        a, b = self._points[near].kw, self._points[other].kw
        span = near - other
        if span == 0:
            return max(0.0, a)
        slope = (a - b) / span
        # Forbruget falder med udetemperaturen. Peger de to punkter den anden
        # vej, er det støj i belægningen og ikke et hus der bruger mere
        # varme når det bliver varmere.
        if slope > 0:
            return max(0.0, a)
        return max(0.0, a + slope * (outdoor - near))

    def confidence(self, outdoor: float) -> float:
        """Hvor meget evidens der står bag forudsigelsen *på det sted*."""
        if not self._points:
            return 0.0
        keys = self.outdoor_temps
        nearest = min(keys, key=lambda k: abs(k - outdoor))
        distance = abs(nearest - outdoor)
        return self._points[nearest].count / (1 + max(0.0, distance - NEAR_ENOUGH_K))

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, dict[str, float]]:
        return {
            str(outdoor): {"kw": round(p.kw, 4), "count": p.count}
            for outdoor, p in sorted(self._points.items())
        }

    @classmethod
    def from_raw(cls, raw: Any) -> LoadCurve:
        points: dict[int, Point] = {}
        if isinstance(raw, dict):
            for key, cell in raw.items():
                try:
                    outdoor = int(key)
                    kw = float(cell["kw"])
                    count = float(cell.get("count", 0))
                except (TypeError, ValueError, KeyError):
                    continue
                if math.isfinite(kw) and math.isfinite(count) and kw >= 0:
                    points[outdoor] = Point(kw=kw, count=count)
        return cls(points)


@dataclass(frozen=True)
class VesselHour:
    """Én time af døgnet: hvor tit beholderen og spaen kører, og hvor hårdt."""

    duty: float = 0.0
    kw: float = 0.0
    count: float = 0.0

    @property
    def kwh(self) -> float:
        """Hvad de tager ud af tankene på en typisk sådan time."""
        return self.duty * self.kw


class VesselProfile:
    """Døgnprofil for varmt vand og spa — hvornår de tapper lageret.

    Hidtil blev ``dhw_active`` og ``spa_heating`` kun brugt i øjeblikket: til
    at holde deres setpunkter ude af varmekurven, til at kassere et
    målevindue, og til visningen. Intet blev gemt, og derfor kunne
    planlæggeren ikke svare på det spørgsmål der afgør en opladning: *hvor
    meget varmt vand kommer der i de dyre timer?*

    Formen skal passe til anlægget. Spaen kører 12-17 efter en tidsplan i
    UVR'en, og varmtvandet har sine egne vinduer, så ét gennemsnit over
    døgnet ville sige «en fjerdedel af tiden» og ramme forkert i begge ender.
    Én celle pr. time rammer skemaet.

    Timen samles op mens den går og lægges først ind når den er forbi. Ellers
    ville cellen følge *denne* time frem for at være et gennemsnit over dage,
    og en enkelt lang spa-tur ville se ud som en vane.
    """

    def __init__(self, hours: dict[int, VesselHour] | None = None) -> None:
        self._hours: dict[int, VesselHour] = dict(hours or {})
        self._hour: int | None = None
        self._last: float | None = None
        self._seconds = 0.0
        self._on_seconds = 0.0
        self._kwh = 0.0

    @property
    def hours(self) -> dict[int, VesselHour]:
        return dict(self._hours)

    @property
    def known_hours(self) -> int:
        return len(self._hours)

    def hour(self, of_day: int) -> VesselHour | None:
        return self._hours.get(of_day % 24)

    # -------------------------------------------------------------- læring

    def observe(self, now: float, on: bool, kw: float | None) -> None:
        """Ét skridt. ``on`` er om nogen af de to kører, ``kw`` deres træk."""
        hour = _hour_of_day(now)
        if hour is None:
            return

        gap = now - self._last if self._last is not None else 0.0
        self._last = now
        if self._hour is not None and hour != self._hour:
            self._commit()
        self._hour = hour
        if gap <= 0 or gap > MAX_GAP_SECONDS:
            # Et hul betyder at vi ikke ved hvad der skete imens. Timen tæller
            # kun den tid vi faktisk har set.
            return

        self._seconds += gap
        if on:
            self._on_seconds += gap
            self._kwh += (kw or 0.0) * gap / 3600

    def _commit(self) -> None:
        hour, seconds = self._hour, self._seconds
        on_seconds, kwh = self._on_seconds, self._kwh
        self._seconds = self._on_seconds = self._kwh = 0.0
        if hour is None or seconds < MIN_HOUR_SECONDS:
            # En halv time er ikke en time. En genstart midt i timen må ikke
            # tælle som om vesslerne stod stille resten af den.
            return

        duty = on_seconds / seconds
        kw = kwh / (on_seconds / 3600) if on_seconds > 0 else None
        old = self._hours.get(hour)
        if old is None:
            self._hours[hour] = VesselHour(duty=duty, kw=kw or 0.0, count=1.0)
            return

        count = old.count + 1
        alpha = 0.3 if count < _SETTLED_DAYS else 0.15
        self._hours[hour] = VesselHour(
            duty=old.duty * (1 - alpha) + duty * alpha,
            # Effekten læres kun af timer hvor de faktisk kørte. Ellers ville
            # en stille nat trække den mod nul, og så ville en time med
            # halv drift se ud som en med fuld drift ved halv effekt.
            kw=old.kw if kw is None else old.kw * (1 - alpha) + kw * alpha,
            count=count,
        )

    # ---------------------------------------------------------- opslag frem

    def kwh_between(self, now: float, hours: float) -> float | None:
        """Hvad beholderen og spaen tager ud af lageret i de næste ``hours``.

        Summen af profilen hen over vinduet, med de skæve ender regnet med.
        ``None`` når der ikke er lært nok til at svare — og så skal den der
        spørger, sige det i stedet for at regne på et nul.
        """
        if not self._hours or hours <= 0:
            return None
        start = _hour_of_day(now)
        if start is None:
            return None

        offset = (now % 3600) / 3600
        total = 0.0
        left = hours
        index = start
        # Første time er kun delvis tilbage.
        share = min(left, 1.0 - offset)
        while left > 0:
            cell = self._hours.get(index % 24)
            if cell is not None:
                total += cell.kwh * share
            left -= share
            index += 1
            share = min(left, 1.0)
        return total

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, dict[str, float]]:
        return {
            str(hour): {
                "duty": round(cell.duty, 4),
                "kw": round(cell.kw, 3),
                "count": cell.count,
            }
            for hour, cell in sorted(self._hours.items())
        }

    @classmethod
    def from_raw(cls, raw: Any) -> VesselProfile:
        hours: dict[int, VesselHour] = {}
        if isinstance(raw, dict):
            for key, cell in raw.items():
                try:
                    hour = int(key)
                    duty = float(cell["duty"])
                    kw = float(cell["kw"])
                    count = float(cell.get("count", 0))
                except (TypeError, ValueError, KeyError):
                    continue
                if 0 <= hour < 24 and 0 <= duty <= 1 and kw >= 0:
                    hours[hour] = VesselHour(duty=duty, kw=kw, count=count)
        return cls(hours)


def _hour_of_day(at: float) -> int | None:
    """Timen i lokal tid. Et ur der ikke giver mening, giver ingen time."""
    try:
        return datetime.fromtimestamp(at).astimezone().hour
    except (OSError, OverflowError, ValueError):
        return None


@dataclass
class _Sample:
    at: float
    energy_kwh: float
    inflow_kwh: float


@dataclass
class HouseLoad:
    """Husets forbrug målt på lagerets energiændring."""

    curve: LoadCurve = field(default_factory=LoadCurve)
    # Hvornår beholderen og spaen tapper lageret. Ikke en del af husets
    # forbrug - tværtimod det der skal trakkes fra for at finde det - men
    # det er her flagene, effekten og tiden mødes, og derfor bor profilen
    # her frem for i hovedløkken.
    vessels: VesselProfile = field(default_factory=VesselProfile)
    kw: float | None = None
    measured_at: float | None = None
    note: str = "venter på første vindue"
    # Hvor langt målingen ligger fra flowmåleren, når de begge svarer.
    # Målet skal kunne sige selv hvor godt det rammer, før nogen stoler på
    # det - og det er også sådan man opdager at måleren driver.
    error_sum: float = 0.0
    error_n: float = 0.0
    # (tidspunkt, kW, udetemperatur, modelleret) pr. vindue. Til grafen - den
    # rullende måling selv lever kun i hukommelsen.
    history: list[tuple[float, float, float | None, bool]] = field(default_factory=list)
    _samples: list[_Sample] = field(default_factory=list)
    _modelled: bool = False
    _sensors: int | None = None
    _last_learned_at: float | None = None

    # -------------------------------------------------------------- måling

    def observe(
        self,
        now: float,
        heat_kwh: float | None,
        sources: dict[str, float] | None,
        inputs_known: bool = True,
        dhw: bool | None = None,
        spa: bool | None = None,
        sensors: int | None = None,
        outdoor: float | None = None,
        meter_kw: float | None = None,
        standby_kw: float | None = None,
        vessel_kw: float | None = None,
    ) -> str:
        """Ét skridt. Returnerer en status der kan vises og logges."""
        # Døgnprofilen først, og altid. Den skal netop lære af de minutter
        # hvor der bliver badet - det er dem der bliver kasseret nedenfor, og
        # de er dermed de eneste der ellers aldrig blev husket.
        self.vessels.observe(now, bool(dhw or spa), vessel_kw)

        if (dhw or spa) and not _finite(vessel_kw):
            # Bad og spa tapper de samme tanke som huset, og en lagerbalance
            # kan ikke se forskel. Uden et bud på hvor meget de tager,
            # begynder vinduet forfra.
            return self._drop("bad eller spa tapper tankene")
        if not inputs_known:
            return self._drop("varmepumpen kører uden en COP at regne på")
        if not _finite(heat_kwh):
            return self._drop("mangler tankmåling")
        if sensors is not None and self._sensors is not None and sensors != self._sensors:
            # Et lag der falder ud eller kommer til, skifter energigrundlaget
            # midt i en måling: forskellen ville være følerens og ikke
            # husets.
            #
            # Det nye antal skal med over i samme åndedrag. Uden det blev
            # det gamle stående, og hver eneste aflæsning derefter blev
            # kasseret mod et tal anlægget ikke længere havde - målingen
            # kom aldrig i gang igen efter en død føler.
            self._sensors = sensors
            return self._drop("antallet af følere skiftede")
        self._sensors = sensors

        inflow = 0.0
        if self._samples:
            gap = now - self._samples[-1].at
            if gap <= 0 or gap > MAX_GAP_SECONDS:
                return self._drop("hul i aflæsningerne")
            input_kw = sum(v for v in (sources or {}).values() if _finite(v))
            # Det bad eller den spa der kører, tæller som et træk ved siden
            # af husets - altså som en negativ tilførsel. Tallet er et
            # skøn, og derfor bliver vinduet mærket.
            if dhw or spa:
                input_kw -= vessel_kw or 0.0
                self._modelled = True
            inflow = self._samples[-1].inflow_kwh + input_kw * gap / 3600

        self._samples.append(_Sample(now, float(heat_kwh), inflow))
        cutoff = now - WINDOW_MINUTES * 60
        self._samples = [s for s in self._samples if s.at >= cutoff]

        return self._measure(now, outdoor, meter_kw, standby_kw)

    def _drop(self, why: str) -> str:
        self._samples = []
        self._modelled = False
        self.note = f"venter — {why}"
        return self.note

    def _measure(
        self,
        now: float,
        outdoor: float | None,
        meter_kw: float | None,
        standby_kw: float | None,
    ) -> str:
        first, last = self._samples[0], self._samples[-1]
        hours = (last.at - first.at) / 3600
        if len(self._samples) < MIN_SAMPLES or hours * 60 < MIN_MINUTES:
            self.note = (
                f"måler — {hours * 60:.0f} min af {MIN_MINUTES:.0f}, "
                f"{len(self._samples)} aflæsninger"
            )
            return self.note

        origin = first.at
        change = _slope([((s.at - origin) / 3600, s.energy_kwh) for s in self._samples])
        if change is None:
            self.note = "venter — kunne ikke regne en hældning"
            return self.note

        # Kilderne ind, minus det lageret voksede med. Bliver tankene koldere,
        # er ``change`` negativ, og de to lægges dermed sammen.
        drawn = (last.inflow_kwh - first.inflow_kwh) / hours - change
        if drawn < -UNEXPLAINED_KW:
            return self._drop(
                f"lageret voksede {-drawn:.1f} kW mere end kilderne forklarer"
            )

        if _finite(standby_kw):
            drawn -= standby_kw
        drawn = max(0.0, drawn)

        self.kw = drawn
        self.measured_at = now
        if _finite(meter_kw) and not self._modelled:
            self.error_sum += drawn - meter_kw
            self.error_n += 1
        self._maybe_learn(now, outdoor)

        notes = []
        if self._modelled:
            notes.append("bad/spa trukket fra efter skøn")
        if not _finite(standby_kw):
            notes.append("ståtab ikke trukket fra")
        tail = f" ({', '.join(notes)})" if notes else ""
        self.note = f"målt {drawn:.2f} kW over {hours * 60:.0f} min{tail}"
        return self.note

    def _maybe_learn(self, now: float, outdoor: float | None) -> None:
        """Læg målingen ind i kurven — men kun ét uafhængigt vindue ad gangen.

        Den rullende måling regnes hvert minut, og hvert af de tal beskriver
        stort set den samme halve time. Lærte kurven af dem alle, ville én
        aften tælle tredive gange og se ud som tredive aftener.
        """
        if self.kw is None:
            return
        if self._last_learned_at is not None:
            if now - self._last_learned_at < WINDOW_MINUTES * 60:
                return
        self._last_learned_at = now

        # Grafen skal vise alt der er målt, også de modellerede vinduer -
        # de er mærket, så de kan tegnes for sig.
        self.history.append((now, self.kw, outdoor, self._modelled))
        cutoff = now - HISTORY_DAYS * 86400
        self.history = [h for h in self.history if h[0] >= cutoff]

        # Kurven derimod kender kun rene vinduer. Et skøn på spaens træk
        # må gerne bære det tal der vises nu; det må ikke bygge modellen.
        if _finite(outdoor) and not self._modelled:
            self.curve.learn(outdoor, self.kw)

    # -------------------------------------------------------------- resultat

    def kw_at(self, now: float, outdoor: float | None = None) -> float | None:
        """Bedste bud på husets forbrug: målingen, ellers kurven.

        Målingen tier i de vinduer der bliver kasseret — et bad, en genstart —
        og så er kurven det eneste tilbage. Rækkefølgen er den samme som for
        kilder i det hele taget: det målte slår det modellerede.
        """
        if self.kw is not None and self.measured_at is not None:
            if now - self.measured_at <= MAX_AGE_MINUTES * 60:
                return self.kw
        return self.curve.predict(outdoor) if outdoor is not None else None

    @property
    def bias_kw(self) -> float | None:
        """Hvor meget målingen i gennemsnit ligger over flowmålerens tal."""
        if self.error_n <= 0:
            return None
        return self.error_sum / self.error_n

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        # Det igangværende vindue gemmes med vilje ikke: en genstart betyder
        # et hul i aflæsningerne, og så er det ærligere at begynde forfra
        # end at regne hen over hullet. Samme valg som ståtabsmålingen.
        return {
            "curve": self.curve.to_raw(),
            "vessels": self.vessels.to_raw(),
            "error_sum": round(self.error_sum, 4),
            "error_n": self.error_n,
            "history": [
                [round(at, 1), round(kw, 3), outdoor, modelled]
                for at, kw, outdoor, modelled in self.history
            ],
        }

    @classmethod
    def from_raw(cls, raw: Any) -> HouseLoad:
        model = cls()
        if isinstance(raw, dict):
            model.curve = LoadCurve.from_raw(raw.get("curve"))
            model.vessels = VesselProfile.from_raw(raw.get("vessels"))
            try:
                model.error_sum = float(raw.get("error_sum", 0.0))
                model.error_n = float(raw.get("error_n", 0.0))
            except (TypeError, ValueError):
                model.error_sum, model.error_n = 0.0, 0.0
            for item in raw.get("history") or []:
                try:
                    at, kw = float(item[0]), float(item[1])
                    outdoor = None if item[2] is None else float(item[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if math.isfinite(at) and math.isfinite(kw):
                    modelled = bool(item[3]) if len(item) > 3 else False
                    model.history.append((at, kw, outdoor, modelled))
        if model.curve.point_count:
            model.note = f"{model.curve.point_count} punkt(er) på kurven"
        return model
