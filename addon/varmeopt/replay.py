"""Genafspilning: planlæggeren kørt igen på de dage logbogen har gemt.

Energy-pilots ``tune.py`` gør det for et batteri: hver nat planlægges de
sidste ugers dage forfra med det der var kendt dengang, første skridt
udføres mod det der faktisk skete, og nabo-indstillinger prøves. Batteriet er
let at simulere. Et varmelager er ikke: det er UVR'en der starter pumpen når
tanken er kold nok, og den kender vi kun fra hvad den har gjort.

Derfor tre lag, og det midterste er værnet:

1. **UVR'ens opførsel læres af logbogen.** Hvor kold tanken er når pumpen
   starter uden en opladning, hvor varm når den stopper, og hvor hurtigt
   varmepumpe og pillefyr leverer.
2. **Prøven.** Simulatoren kører de beslutninger der *faktisk* blev truffet
   - kilden og ladeflagene som de stod - og skal ramme den varme pumpe og fyr
   faktisk leverede, og lagerets forløb, inden for faste grænser. Består den
   ikke, bruges den ikke: så ville justeringen lære simulatorens fejl i stedet
   for husets. Det var den største advarsel i gennemgangen af Energy-pilot.
3. **Justeringen.** Kun når prøven er bestået. Se ``Tuner``.

Lagerets træk - huset, varmtvandet, spaen og ståtabet tilsammen - regnes ikke
af en model. Det tages af logbogen som det der *må* være gået ud: det der
kom ind, minus det lageret steg. Så er simulatoren eksakt for de beslutninger
der blev truffet, og fejlen ligger kun i hvordan den gætter på UVR'en.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable

from .charge import ChargePlan
from .planner import PLACING_RULES, Planner, choose_charge, minutes_until_hour
from .prices import Grid, Plan

log = logging.getLogger(__name__)

SLOT = 1800
MIN_COVER = 0.95
# En dag tæller kun når næsten alle halvtimer er der (sommertid giver 46/50).
MIN_SLOTS_PER_DAY = 44

# Kompressorens minutter over dagene skal ramme inden for en fjerdedel. Varmen
# alene tvinges på plads af energibalancen - en model med halv eller dobbelt
# ydelse bestod (gennemgangen den 24. november). Minutterne gør ikke.
MAX_MINUTES_ERROR = 0.25

# Prøvens grænser. Varmen fra pumpen skal ramme inden for en femtedel over
# dagene, fyrets inden for en fjerdedel eller 3 kWh pr. dag, og lagerets
# døgngennemsnit og laveste punkt må i snit ligge højst 3 kWh fra det målte.
#
# Ikke halvtime for halvtime: UVR'ens termostat giver en savtak, og et par
# minutters faseforskydning ligner en stor fejl uden at koste noget. Selv et
# hus simulatoren rammer præcist, lå 2,4 kWh fra halvtime for halvtime.
MAX_HP_ERROR = 0.20
MAX_PELLET_ERROR = 0.25
PELLET_SLACK_KWH_PER_DAY = 3.0
MAX_STORE_ERROR_KWH = 3.0
MIN_VALIDATION_DAYS = 7

# Et lager der falder under det koldeste dagen faktisk nåede, koster en
# straf. Ellers ville justeringen lære at spare komforten væk.
COLD_PENALTY_KR_PER_KWH = 2.0
COLD_SLACK_KWH = 1.0

# Hvad en start koster i varme: rør og pumpe op på temperatur. Den 18.
# oktober kostede opstarten ca. 2 kWh varme før pumpen var oppe på 56 grader.
START_HEAT_KWH = 2.0
# Et skift til pillefyret: tænding og opvarmning.
PELLET_START_KWH = 1.0


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _num(value: Any, default: float = 0.0) -> float:
    return float(value) if _finite(value) else default


# ------------------------------------------------------------------- dage


def usable_days(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Hele dage af brugbare halvtimer, ældste først."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if _usable(row):
            day = datetime.fromtimestamp(row["t"]).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(row)
    out = []
    for day in sorted(by_day):
        slots = sorted(by_day[day], key=lambda r: r["t"])
        # Sammenhængende halvtimer: simulatoren fører lageret fra den ene til
        # den næste, og et hul midt på dagen ville slette både trækket og
        # springet i lageret (gennemgangen den 24. november).
        whole = all(b["t"] - a["t"] == SLOT for a, b in zip(slots, slots[1:]))
        if len(slots) >= MIN_SLOTS_PER_DAY and whole:
            out.append(slots)
    return out


def _usable(row: dict[str, Any]) -> bool:
    seen, done = row.get("så") or {}, row.get("skete") or {}
    if (row.get("dækning") or 0) < MIN_COVER:
        return False
    # Kørte pumpen, men varmen kunne ikke måles, er trækket forkert.
    if _num(done.get("kompressor_min")) > 0 and not _finite(done.get("vp_varme_kwh")):
        return False
    if Plan.from_replay(seen.get("plan")) is None:
        return False
    if not isinstance(seen.get("plan"), dict) or not isinstance(seen.get("frem"), dict):
        return False
    return _finite(seen.get("lager_kwh")) and _finite(done.get("lager_slut_kwh"))


def draw_kwh(row: dict[str, Any]) -> float:
    """Det der gik ud af lageret i halvtimen: det der kom ind, minus stigningen."""
    done = row["skete"]
    inn = sum(
        _num(done.get(k)) for k in ("vp_varme_kwh", "solvarme_kwh", "elpatron_kwh", "pillefyr_kwh")
    )
    return inn - (done["lager_slut_kwh"] - row["så"]["lager_kwh"])


# ------------------------------------------------------------- UVR-modellen


@dataclass
class Model:
    """UVR'ens opførsel, som logbogen viser den."""

    start_kwh: float  # lageret når pumpen starter uden en opladning
    stop_kwh: float  # lageret når den stopper igen
    hp_kw: float  # varmepumpens varmeydelse
    pellet_kw: float  # pillefyrets


def learn_model(days: list[list[dict[str, Any]]]) -> Model | None:
    starts, stops, hp_rates, pellet_rates = [], [], [], []
    for day in days:
        prev_running = None
        for row in day:
            done, seen = row["skete"], row["så"]
            minutes = _num(done.get("kompressor_min"), -1)
            charging = _num(done.get("lad_op_min")) + _num(done.get("lad_op_lav_min")) > 0
            running = minutes > 1
            if prev_running is False and running and not charging:
                starts.append(seen["lager_kwh"])
            if prev_running and not running and not charging:
                stops.append(seen["lager_kwh"])
            if minutes >= 10:
                heat = _num(done.get("vp_varme_kwh"))
                if heat > 0:
                    hp_rates.append(heat / (minutes / 60))
            pellet = _num(done.get("pillefyr_kwh"))
            if pellet > 0.5:
                pellet_rates.append(pellet * 2)
            prev_running = running if minutes >= 0 else None
    if len(starts) < 5 or len(hp_rates) < 5:
        return None
    start = statistics.median(starts)
    stop = statistics.median(stops) if len(stops) >= 3 else start + 3.0
    return Model(
        start_kwh=start,
        stop_kwh=max(stop, start + 1.0),
        hp_kw=statistics.median(hp_rates),
        pellet_kw=statistics.median(pellet_rates) if len(pellet_rates) >= 3 else 10.0,
    )


# -------------------------------------------------------------- simulatoren


@dataclass
class Knobs:
    source_hysteresis: float
    charge_min_saving_kr: float
    low_charge_min_saving_kr: float


@dataclass
class Fixed:
    """Det justeringen aldrig rører - fra add-on'ens indstillinger."""

    wear_kr_per_kwh: float
    min_runtime_minutes: float
    horizon_minutes: int
    dhw_temp: float
    store_full_by_hour: float
    low_max_temp: float = 53.0
    min_dwell_minutes: float = 15.0


@dataclass
class Result:
    cost_kr: float = 0.0
    hp_heat_kwh: float = 0.0
    hp_minutes: int = 0
    starts: int = 0
    end_short_kwh: float = 0.0
    pellet_kwh: float = 0.0
    store_error_kwh: float = 0.0
    cold_kwh: float = 0.0
    trace: list[float] = field(default_factory=list)


def _ahead(values: Any, minutes: float) -> float | None:
    if not isinstance(values, list):
        return None
    index = int(max(0, minutes) // 30)
    return values[index] if index < len(values) and _finite(values[index]) else None


def simulate(
    day: list[dict[str, Any]],
    model: Model,
    policy: Callable[..., tuple[str, bool, bool]] | None = None,
    planner_policy: Any = None,
    wear_kr_per_kwh: float = 0.0,
) -> Result:
    """Én dag. ``policy`` er de loggede beslutninger (prøven);
    ``planner_policy`` er planlæggeren og blokken (justeringen).

    Gennemgangen den 24. november fandt fem ting der alle fik lavere gulve og
    mindre hysterese til at se billigere ud end de er. De er med nu:

    * **Solvarme og elpatron** lægges til som de faktisk kom. Trækket er regnet
      af dem, så uden dem betalte simulatoren for varme ACthor gav gratis.
    * **En start koster** ``START_HEAT_KWH`` varme - rør og pumpe skal op på
      temperatur. Det er hele begrundelsen for gulvet på 1 kr.
    * **Et skift til pillefyret koster** ``PELLET_START_KWH``.
    * **Lageret ved midnat tæller.** Ender simulatoren lavere end det målte, er
      forskellen varme der skal købes i morgen, til dagens sidste varmepris.
      Ellers så det billigere ud at lade mindre op.
    * **Nettets måling** sendes med, så prisen er den beslutningen så.
    """
    out = Result()
    store = day[0]["så"]["lager_kwh"]
    floor = min(r["så"]["lager_kwh"] for r in day) - COLD_SLACK_KWH
    # UVR'en står som den faktisk stod da dagen begyndte.
    uvr_on = _num(day[0]["skete"].get("kompressor_min")) >= 15
    hp_was = pellet_was = uvr_on
    wear = planner_wear(planner_policy) if planner_policy is not None else wear_kr_per_kwh
    last_heat_price = None
    for row in day:
        seen, done = row["så"], row["skete"]
        plan = Plan.from_replay(seen["plan"])
        grid = _grid(seen)
        price = plan.marginal(0, grid=grid) if plan is not None else None
        el = price.kr_per_kwh if price is not None else None
        cop = _ahead((seen.get("frem") or {}).get("cop"), 0) or 4.0
        charge_cop = _ahead((seen.get("frem") or {}).get("lade_cop"), 0) or cop
        pellet_price = _num(seen.get("pille_varmepris"), 0.75)
        if el is not None:
            last_heat_price = min(el / max(cop, 1.0) + wear, pellet_price)
        draw = draw_kwh(row) / 30
        free = (_num(done.get("solvarme_kwh")) + _num(done.get("elpatron_kwh"))) / 30
        offset = store - seen["lager_kwh"]
        if planner_policy is not None:
            decide = planner_policy.slot(row, store, offset, plan, grid)
        for minute in range(30):
            now = row["t"] + minute * 60
            if planner_policy is not None:
                source, high, low = planner_policy.minute(now, decide, plan, store, row, grid)
            else:
                source, high, low = policy(row, minute)
            hp = pellet = 0.0
            charging_cop = None
            if high or low:
                room = (
                    _num(seen.get("lager_plads_kwh"))
                    if high
                    else _num(seen.get("lager_plads_lav_kwh"))
                ) - (store - seen["lager_kwh"])
                hp = max(0.0, min(model.hp_kw / 60, room))
                charging_cop = charge_cop if high else cop
            else:
                # UVR'en: start når lageret er koldt nok, stop når det er varmt.
                if store <= model.start_kwh:
                    uvr_on = True
                elif store >= model.stop_kwh:
                    uvr_on = False
                if uvr_on:
                    if source == "pillefyr":
                        pellet = model.pellet_kw / 60
                    else:
                        hp = model.hp_kw / 60
            if planner_policy is not None:
                planner_policy.delivered(hp * 60 if (high or low) else 0.0)
            store += hp + pellet + free - draw
            use_cop = max(charging_cop or cop, 1.0)
            if hp and not hp_was:
                out.starts += 1
                out.cost_kr += START_HEAT_KWH * (
                    (el if el is not None else 0.0) / use_cop + wear
                )
            if pellet and not pellet_was:
                out.cost_kr += PELLET_START_KWH * pellet_price
            hp_was, pellet_was = hp > 0, pellet > 0
            if hp:
                out.cost_kr += hp / use_cop * (el if el is not None else 0.0)
                out.cost_kr += hp * wear
                out.hp_heat_kwh += hp
                out.hp_minutes += 1
            if pellet:
                out.cost_kr += pellet * pellet_price
                out.pellet_kwh += pellet
            if store < floor:
                out.cold_kwh += (floor - store) / 30
        out.trace.append(store)
    out.cost_kr += out.cold_kwh * COLD_PENALTY_KR_PER_KWH
    real = [r["skete"]["lager_slut_kwh"] for r in day]
    if out.trace:
        # Lageret ved midnat: det simulatoren mangler, købes i morgen.
        out.end_short_kwh = real[-1] - out.trace[-1]
        if last_heat_price is not None:
            out.cost_kr += out.end_short_kwh * last_heat_price
        mean_error = abs(sum(out.trace) / len(out.trace) - sum(real) / len(real))
        low_error = abs(min(out.trace) - min(real))
        high_error = abs(max(out.trace) - max(real))
        out.store_error_kwh = max(mean_error, low_error, high_error)
    return out


def _grid(seen: dict[str, Any]) -> Grid | None:
    net = seen.get("net")
    if not isinstance(net, dict):
        return None
    try:
        return Grid(**net)
    except TypeError:
        return None


def planner_wear(policy: Any) -> float:
    return policy.fixed.wear_kr_per_kwh if policy is not None else 0.0


def logged_policy(row: dict[str, Any], minute: int) -> tuple[str, bool, bool]:
    """De beslutninger der faktisk blev truffet - til prøven."""
    done = row["skete"]
    sources = done.get("kilde_min") or {}
    source = max(sources, key=lambda k: sources[k]) if sources else "varmepumpe"
    high = minute < _num(done.get("lad_op_min"))
    low = not high and minute < _num(done.get("lad_op_min")) + _num(done.get("lad_op_lav_min"))
    return source, high, low


class PlannerPolicy:
    """Planlæggeren og blokken, kørt med en bestemt indstilling."""

    def __init__(self, knobs: Knobs, fixed: Fixed, model: Model) -> None:
        self.knobs, self.fixed, self.model = knobs, fixed, model
        self.charge = ChargePlan()
        self._heat = 0.0
        # Simulatorens egen kilde og hvornår den sidst skiftede - vagtens
        # hviletid. Her stod den loggede kilde, og så var et kildeskift gratis
        # og øjeblikkeligt: en lav hysterese så altid billigere ud.
        self.source: str | None = None
        self._source_since: float | None = None

    def delivered(self, kw: float) -> None:
        self._heat = kw

    def _planner(self, pellet_price: float) -> Planner:
        return Planner(
            pellet_price=pellet_price,
            hysteresis=self.knobs.source_hysteresis,
            wear_kr_per_kwh=self.fixed.wear_kr_per_kwh,
            min_charge_kwh=self.model.hp_kw * self.fixed.min_runtime_minutes / 60,
            charge_kw=self.model.hp_kw,
            horizon_minutes=self.fixed.horizon_minutes,
            dhw_temp=self.fixed.dhw_temp,
            min_saving_kr=self.knobs.charge_min_saving_kr,
            min_runtime_minutes=self.fixed.min_runtime_minutes,
        )

    def slot(
        self,
        row: dict[str, Any],
        store: float,
        offset: float,
        plan: Plan | None,
        grid: Grid | None = None,
    ) -> Any:
        seen = row["så"]
        if plan is None:
            return None
        frem = seen.get("frem") or {}
        planner = self._planner(_num(seen.get("pille_varmepris"), 0.75))
        vvb = frem.get("vvb_kwh")

        def dhw_over(start: float, hours: float) -> float | None:
            if not isinstance(vvb, list):
                return None
            first, n = int(start // 30), int(round(hours * 2))
            return sum(_num(v) for v in vvb[max(0, first):max(0, first) + n])

        if self.source is None:
            self.source = seen.get("vagt_kilde") or "varmepumpe"
            self._source_since = row["t"]
        common = dict(
            plan=plan,
            cop_now=_ahead(frem.get("cop"), 0),
            cop_later=lambda m: _ahead(frem.get("cop"), m),
            peak_headroom_kwh=max(0.0, _num(seen.get("lager_top_plads_kwh")) - offset),
            stored_kwh=max(0.0, _num(seen.get("lager_lagret_kwh")) + offset),
            solar_expected_kwh=seen.get("sol_forventet_kwh"),
            grid=grid,
            demand_kw=seen.get("behov_kw"),
            demand_kw_at=lambda m: _ahead(frem.get("behov_kw"), m),
            deadline_minutes=minutes_until_hour(self.fixed.store_full_by_hour, row["t"]),
            elapsed_minutes=0.0,
            locked_minutes=seen.get("låst_min"),
            current_source=self.source,
        )
        hot = seen.get("lager_varmt_kwh")
        hot = max(0.0, _num(hot) + min(0.0, offset)) if _finite(hot) else None
        high = planner.decide(
            **common,
            charge_cop_at=lambda m: _ahead(frem.get("lade_cop"), m),
            headroom_kwh=max(0.0, _num(seen.get("lager_plads_kwh")) - offset),
            hot_kwh=hot,
            dhw_kwh_over=dhw_over,
        )
        low = None
        low_temp = seen.get("lav_temp")
        if _finite(low_temp) and low_temp <= self.fixed.low_max_temp and _finite(
            seen.get("lager_plads_lav_kwh")
        ):
            low = planner.decide(
                **common,
                charge_cop_at=lambda m: _ahead(frem.get("cop"), m),
                headroom_kwh=max(0.0, seen["lager_plads_lav_kwh"] - offset),
                hot_kwh=hot,
                dhw_kwh_over=dhw_over,
                space_only=True,
                min_saving_kr=self.knobs.low_charge_min_saving_kr,
            )
            low = replace(low, low=True, charge_temp=low_temp)
        now = row["t"]
        return choose_charge(
            high,
            low,
            high_open=self.charge.may_place(now, high),
            low_open=low is not None and self.charge.may_place(now, low),
        )

    def minute(
        self,
        now: float,
        decision: Any,
        plan: Plan | None,
        store: float,
        row: dict[str, Any],
        grid: Grid | None = None,
    ) -> tuple[str, bool, bool]:
        if decision is None:
            return self.source or "varmepumpe", False, False
        # Vagtens hviletid: kilden skifter først når den har stået i
        # ``min_dwell_minutes``.
        if decision.source != self.source and (
            self._source_since is None
            or now - self._source_since >= self.fixed.min_dwell_minutes * 60
        ):
            self.source, self._source_since = decision.source, now
        charging = self.charge.update(
            now,
            decision,
            plan,
            self.model.hp_kw,
            full=False,
            source=self.source,
            min_runtime_minutes=self.fixed.min_runtime_minutes,
            heat_kw=self._heat,
            grid=grid if now - row["t"] < SLOT else None,
            plan_stamp=f"r{int(row['t'])}",
            confirm_plans=1,
            target_met=None,
        )
        block = self.charge.block if charging else None
        low = bool(block is not None and block.low)
        return self.source or decision.source, charging and not low, low


# ------------------------------------------------------------------- prøven


@dataclass
class Validation:
    ok: bool
    why: str
    days: int = 0
    hp_error: float | None = None
    pellet_error_kwh: float | None = None
    store_error_kwh: float | None = None
    minutes_error: float | None = None

    def to_raw(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "why": self.why, "days": self.days,
            "minutes_error": None if self.minutes_error is None else round(self.minutes_error, 3),
            "hp_error": None if self.hp_error is None else round(self.hp_error, 3),
            "pellet_error_kwh": None if self.pellet_error_kwh is None else round(self.pellet_error_kwh, 2),
            "store_error_kwh": None if self.store_error_kwh is None else round(self.store_error_kwh, 2),
        }


def validate(days: list[list[dict[str, Any]]], model: Model | None) -> Validation:
    if len(days) < MIN_VALIDATION_DAYS:
        return Validation(False, f"{len(days)} hele dage i logbogen - prøven kræver {MIN_VALIDATION_DAYS}", len(days))
    if model is None:
        return Validation(False, "for få starter til at lære UVR'ens opførsel", len(days))
    hp_sim = hp_real = pellet_sim = pellet_real = store_err = 0.0
    minutes_sim = minutes_real = 0.0
    for day in days:
        result = simulate(day, model, policy=logged_policy)
        hp_sim += result.hp_heat_kwh
        pellet_sim += result.pellet_kwh
        store_err += result.store_error_kwh
        minutes_sim += result.hp_minutes
        for row in day:
            hp_real += _num(row["skete"].get("vp_varme_kwh"))
            pellet_real += _num(row["skete"].get("pillefyr_kwh"))
            minutes_real += _num(row["skete"].get("kompressor_min"))
    minutes_error = abs(minutes_sim - minutes_real) / max(minutes_real, 1.0)
    hp_error = abs(hp_sim - hp_real) / max(hp_real, 1.0)
    pellet_error = abs(pellet_sim - pellet_real)
    store_error = store_err / len(days)
    v = Validation(False, "", len(days), hp_error, pellet_error, store_error)
    v.minutes_error = minutes_error
    if hp_error > MAX_HP_ERROR:
        v.why = f"simulatoren rammer varmepumpens varme {hp_error:.0%} ved siden af"
    elif minutes_error > MAX_MINUTES_ERROR:
        v.why = f"simulatoren rammer kompressorens driftstid {minutes_error:.0%} ved siden af"
    elif pellet_error > max(MAX_PELLET_ERROR * pellet_real, PELLET_SLACK_KWH_PER_DAY * len(days)):
        v.why = f"simulatoren rammer pillefyrets varme {pellet_error:.1f} kWh ved siden af"
    elif store_error > MAX_STORE_ERROR_KWH:
        v.why = f"lagerets døgnforløb ligger i snit {store_error:.1f} kWh fra det målte"
    else:
        v.ok, v.why = True, "bestået"
    return v
