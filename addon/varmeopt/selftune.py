"""Nattens justering: tre knapper, ét lille skridt ad gangen, kun når det klart betaler sig.

Energy-pilots ``tune.py`` er forlægget, med de værn to gennemgange den 24.
november bad om oveni:

* **Som standard en rapport.** Den regner hver nat og skriver hvad den *ville*
  ændre og hvorfor, men ændrer intet før ``self_tune_apply`` slås til. Den
  første udgave af simulatoren trak gulvene ned; rapporten skal holdes op mod
  regnskabet nogle uger før den får lov at handle.
* **Kun med en bestået prøve.** Simulatoren skal kunne genskabe de
  beslutninger der faktisk blev truffet - se ``replay.validate``.
* **Dage holdt ude.** De nyeste ``HOLDOUT_DAYS`` dage er ikke med når den
  bedste indstilling vælges, og forslaget må ikke være dyrere på dem.
* **Klart bedre.** Mindst ``MIN_TRAIN_DAYS`` dage, mindst ``MIN_GAIN`` af
  omkostningen, og bedre på mindst 60 % af de dage hvor de to adskiller sig.
* **Ét skridt pr. nat, inden for faste grænser**, og én knap ad gangen. Den
  *kigger* op til ``LOOK_STEPS`` skridt ud - et gulv på 2,0 kr kan være for
  højt uden at 1,9 ændrer noget - men det er skridtet den foreslår, der skal
  bevise sig, ikke værdien længere ude.
* **Brugerens tal går forud.** En indstilling uden for grænserne justeres
  ikke (0 kan betyde «slået fra»), og ændres en indstilling, glemmes den
  justering der byggede på den gamle.

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
        # Den indstilling hver justering byggede på.
        self.based_on: dict[str, float | None] = {k: None for k in BOUNDS}
        self.last_night: str | None = None
        self.validation: dict[str, Any] | None = None
        self.proposal: dict[str, Any] | None = None
        self.note = "samler - ingen nat gennemgået endnu"
        self.log: list[dict[str, Any]] = []

    def value(self, name: str, configured: float) -> float:
        tuned = self.values.get(name)
        if tuned is not None and self.based_on.get(name) != configured:
            # Brugeren har ændret indstillingen siden: den gælder.
            self.values[name] = None
            self.based_on[name] = None
            return configured
        return tuned if tuned is not None else configured

    def knobs(self, configured: Knobs) -> Knobs:
        return Knobs(**{k: self.value(k, v) for k, v in asdict(configured).items()})

    def nightly(
        self,
        rows: list[dict[str, Any]],
        night: str,
        configured: Knobs,
        fixed: Fixed,
        apply: bool = False,
    ) -> bool:
        """Én nats gennemgang. Returnerer om en knap blev flyttet."""
        if night == self.last_night:
            return False
        self.last_night = night
        self.proposal = None
        days = usable_days(rows)
        model = learn_model(days)
        check = validate(days, model)
        self.validation = check.to_raw()
        if not check.ok:
            self.note = f"regner ikke: {check.why}"
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
        for name, (step, low, high) in BOUNDS.items():
            if not low <= getattr(current, name) <= high:
                continue
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
        best, best_total = current, sum(base)
        for knobs in candidates[1:]:
            total = sum(costs(knobs, train))
            if total < best_total - 1e-9:
                best, best_total = knobs, total
        if best == current:
            self.note = "den nuværende indstilling er den billigste - intet at foreslå"
            return False
        changed = next(k for k in BOUNDS if getattr(best, k) != getattr(current, k))
        step = BOUNDS[changed][0]
        direction = 1 if getattr(best, changed) > getattr(current, changed) else -1
        new_value = _clip(changed, getattr(current, changed) + direction * step)
        new = Knobs(**{**asdict(current), changed: new_value})
        # Det skridt der foreslås, skal selv bevise sig. Her blev det bedste
        # målt og det næste sat, og en kørsel gik fire nætter over værdier der
        # var dyrere end udgangspunktet.
        new_costs = costs(new, train)
        gain = sum(base) - sum(new_costs)
        scale = max(sum(abs(x) for x in base), 1e-6)
        differ = [(a, b) for a, b in zip(base, new_costs) if abs(a - b) > 1e-6]
        wins = sum(1 for a, b in differ if b < a)
        if gain / scale < MIN_GAIN or not differ or wins < MIN_WIN_SHARE * len(differ):
            self.note = (
                f"{changed} {new_value} var {gain:.2f} kr billigere end nu, men ikke "
                f"klart nok ({wins} af {len(differ)} dage) - intet at foreslå"
            )
            return False
        held_now, held_new = sum(costs(current, holdout)), sum(costs(new, holdout))
        if held_new > held_now + 1e-6:
            self.note = (
                f"{changed} {new_value} var bedre på de gamle dage, men "
                f"{held_new - held_now:.2f} kr dyrere på de {HOLDOUT_DAYS} nyeste"
            )
            return False
        entry = {
            "nat": night, "knap": changed,
            "fra": getattr(current, changed), "til": new_value,
            "mål": getattr(best, changed),
            "sparet_kr": round(gain, 2), "procent": round(100 * gain / scale, 1),
            "dage": len(train), "vundet": f"{wins}/{len(differ)}",
            "nyeste_dage_kr": round(held_now - held_new, 2),
            "anvendt": apply,
        }
        self.proposal = entry
        self.log = (self.log + [entry])[-KEEP_LOG:]
        if not apply:
            self.note = (
                f"foreslår {changed} {entry['fra']} -> {entry['til']} (mod {entry['mål']}): "
                f"{entry['sparet_kr']} kr billigere over {len(train)} dage - "
                "ikke anvendt (rapport)"
            )
            log.info("selvjustering: %s", self.note)
            return False
        self.values[changed] = new_value
        self.based_on[changed] = getattr(configured, changed)
        self.note = (
            f"flyttede {changed} fra {entry['fra']} til {entry['til']} (mod {entry['mål']}) - "
            f"{entry['sparet_kr']} kr billigere over {len(train)} dage"
        )
        log.info("selvjustering: %s", self.note)
        return True

    def to_raw(self) -> dict[str, Any]:
        return {
            "values": self.values, "based_on": self.based_on,
            "last_night": self.last_night, "validation": self.validation,
            "proposal": self.proposal, "note": self.note, "log": self.log,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> Tuner:
        out = cls()
        if not isinstance(raw, dict):
            return out
        values = raw.get("values")
        based = raw.get("based_on") if isinstance(raw.get("based_on"), dict) else {}
        if isinstance(values, dict):
            for name in BOUNDS:
                v, b = values.get(name), based.get(name)
                # Uden det den byggede på kan en justering ikke holdes op mod
                # brugerens indstilling, og så glemmes den.
                if _finite(v) and _finite(b):
                    out.values[name] = _clip(name, float(v))
                    out.based_on[name] = float(b)
        if isinstance(raw.get("last_night"), str):
            out.last_night = raw["last_night"]
        if isinstance(raw.get("validation"), dict):
            out.validation = raw["validation"]
        if isinstance(raw.get("proposal"), dict):
            out.proposal = raw["proposal"]
        if isinstance(raw.get("note"), str):
            out.note = raw["note"]
        if isinstance(raw.get("log"), list):
            out.log = [e for e in raw["log"] if isinstance(e, dict)][-KEEP_LOG:]
        return out
