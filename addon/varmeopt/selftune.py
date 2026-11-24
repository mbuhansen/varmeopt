"""Nattens justering: tre knapper, ét lille skridt ad gangen, kun når det klart betaler sig.

Energy-pilots ``tune.py`` er forlægget, med de værn gennemgangen den 24.
november bad om oveni:

* **Kun med en bestået prøve.** Simulatoren skal kunne genskabe de
  beslutninger der faktisk blev truffet - se ``replay.validate``. Ellers
  justeres der ikke, og siden siger hvorfor.
* **Dage holdt ude.** De nyeste ``HOLDOUT_DAYS`` dage er ikke med når den
  bedste indstilling vælges. Den må ikke være dyrere på dem end den nuværende.
  Energy-pilot testede på de samme dage som den justerede på.
* **Klart bedre.** Mindst ``MIN_TRAIN_DAYS`` dage, mindst ``MIN_GAIN`` af
  omkostningen, og bedre på mindst 60 % af de dage hvor de to adskiller sig.
* **Ét skridt pr. nat, inden for faste grænser**, og én knap ad gangen. Men
  den *kigger* op til ``LOOK_STEPS`` skridt ud: et gulv på 2,0 kr kan være
  for højt uden at 1,9 ændrer noget, og så ville et enkelt skridt aldrig
  finde det. Er en værdi længere ude klart bedre, flyttes der ét skridt i den
  retning.

Knapperne er de tre der afgør *om* en opladning eller et kildeskift kan
betale sig. Mindstekørselstid, antal blokke pr. stræk, bekræftelser,
temperaturer og slitage rører den aldrig - det er brugerens regler og priser,
ikke noget der skal optimeres.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict
from typing import Any

from .replay import Fixed, Knobs, PlannerPolicy, learn_model, simulate, usable_days, validate

log = logging.getLogger(__name__)

# (skridt, mindst, højst) for hver knap.
BOUNDS = {
    "source_hysteresis": (0.01, 0.03, 0.10),
    "charge_min_saving_kr": (0.1, 0.5, 2.0),
    "low_charge_min_saving_kr": (0.1, 0.3, 2.0),
}
HOLDOUT_DAYS = 3
MIN_TRAIN_DAYS = 5
MIN_GAIN = 0.005
MIN_WIN_SHARE = 0.6
KEEP_LOG = 20
LOOK_STEPS = 5


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _clip(name: str, value: float) -> float:
    step, low, high = BOUNDS[name]
    return round(min(high, max(low, value)), 3)


class Tuner:
    def __init__(self) -> None:
        # Justeringerne. ``None`` betyder indstillingen fra add-on'en.
        self.values: dict[str, float | None] = {k: None for k in BOUNDS}
        self.last_night: str | None = None
        self.validation: dict[str, Any] | None = None
        self.note = "samler - ingen nat gennemgået endnu"
        self.log: list[dict[str, Any]] = []

    def value(self, name: str, configured: float) -> float:
        tuned = self.values.get(name)
        return tuned if tuned is not None else configured

    def knobs(self, configured: Knobs) -> Knobs:
        return Knobs(**{k: self.value(k, v) for k, v in asdict(configured).items()})

    def nightly(
        self, rows: list[dict[str, Any]], night: str, configured: Knobs, fixed: Fixed
    ) -> bool:
        """Én nats justering. Returnerer om en knap blev flyttet."""
        if night == self.last_night:
            return False
        self.last_night = night
        days = usable_days(rows)
        model = learn_model(days)
        check = validate(days, model)
        self.validation = check.to_raw()
        if not check.ok:
            self.note = f"justerer ikke: {check.why}"
            return False
        assert model is not None
        if len(days) < MIN_TRAIN_DAYS + HOLDOUT_DAYS:
            self.note = (
                f"prøven bestået, men {len(days)} dage er for få - "
                f"kræver {MIN_TRAIN_DAYS + HOLDOUT_DAYS}"
            )
            return False
        train, holdout = days[:-HOLDOUT_DAYS], days[-HOLDOUT_DAYS:]
        current = self.knobs(configured)
        candidates = [current]
        for name, (step, _low, _high) in BOUNDS.items():
            for sign in (1, -1):
                for n in range(1, LOOK_STEPS + 1):
                    moved = _clip(name, getattr(current, name) + sign * n * step)
                    if moved != getattr(current, name):
                        candidates.append(Knobs(**{**asdict(current), name: moved}))
        candidates = list({tuple(asdict(k).values()): k for k in candidates}.values())

        def costs(knobs: Knobs, which: list[list[dict[str, Any]]]) -> list[float]:
            return [
                simulate(day, model, planner_policy=PlannerPolicy(knobs, fixed, model)).cost_kr
                for day in which
            ]

        base = costs(current, train)
        best, best_costs = current, base
        for knobs in candidates[1:]:
            c = costs(knobs, train)
            if sum(c) < sum(best_costs) - 1e-9:
                best, best_costs = knobs, c
        if best == current:
            self.note = "den nuværende indstilling er den billigste - intet flyttet"
            return False
        gain = sum(base) - sum(best_costs)
        scale = max(sum(abs(x) for x in base), 1e-6)
        differ = [(a, b) for a, b in zip(base, best_costs) if abs(a - b) > 1e-6]
        wins = sum(1 for a, b in differ if b < a)
        if gain / scale < MIN_GAIN or not differ or wins < MIN_WIN_SHARE * len(differ):
            self.note = (
                f"en anden indstilling var {gain:.2f} kr billigere, men ikke klart nok "
                f"({wins} af {len(differ)} dage)"
            )
            return False
        held_now, held_best = sum(costs(current, holdout)), sum(costs(best, holdout))
        if held_best > held_now + 1e-6:
            self.note = (
                f"bedre på de gamle dage, men {held_best - held_now:.2f} kr dyrere "
                f"på de {HOLDOUT_DAYS} nyeste - intet flyttet"
            )
            return False
        changed = next(k for k in BOUNDS if getattr(best, k) != getattr(current, k))
        # Ét skridt i retning af det bedste - ikke helt derud.
        step = BOUNDS[changed][0]
        direction = 1 if getattr(best, changed) > getattr(current, changed) else -1
        new = _clip(changed, getattr(current, changed) + direction * step)
        self.values[changed] = new
        entry = {
            "nat": night, "knap": changed,
            "fra": getattr(current, changed), "til": new,
            "mål": getattr(best, changed),
            "sparet_kr": round(gain, 2), "procent": round(100 * gain / scale, 1),
            "dage": len(train), "vundet": f"{wins}/{len(differ)}",
        }
        self.log = (self.log + [entry])[-KEEP_LOG:]
        self.note = (
            f"flyttede {changed} fra {entry['fra']} til {entry['til']} (mod {entry['mål']}) - "
            f"{entry['sparet_kr']} kr billigere over {len(train)} dage"
        )
        log.info("selvjustering: %s", self.note)
        return True

    def to_raw(self) -> dict[str, Any]:
        return {
            "values": self.values, "last_night": self.last_night,
            "validation": self.validation, "note": self.note, "log": self.log,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> Tuner:
        out = cls()
        if not isinstance(raw, dict):
            return out
        values = raw.get("values")
        if isinstance(values, dict):
            for name in BOUNDS:
                v = values.get(name)
                out.values[name] = _clip(name, float(v)) if _finite(v) else None
        if isinstance(raw.get("last_night"), str):
            out.last_night = raw["last_night"]
        if isinstance(raw.get("validation"), dict):
            out.validation = raw["validation"]
        if isinstance(raw.get("note"), str):
            out.note = raw["note"]
        if isinstance(raw.get("log"), list):
            out.log = [e for e in raw["log"] if isinstance(e, dict)][-KEEP_LOG:]
        return out
