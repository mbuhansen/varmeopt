"""Fører regnskab over hvor tit add-on'en og Node-RED er uenige.

Begge regner nu det samme valg hvert minut — varmepumpe mod pillefyr — men på
hver sin COP. Node-RED bruger fabrikkens TA-kurve overalt hvor opslaget ikke
rammer en celle eksakt; add-on'en bruger den rettede interpolation. Hvor de to
peger forskelligt, er det den forskel der gør det.

Uden et regnskab kan man kun *se* uenighederne, ikke tælle dem. Og det er
tællingen der afgør om det næste skridt er værd at tage: står der to kroner om
måneden på spil, er det ikke værd at lade add-on'en styre. Står der to hundrede,
er det.

**Om ordet «på spil».** Beløbet er ikke en bevist besparelse. Det er hvad
*vores egne tal* siger der er forskel på de to valg, og de tal er netop det
der er til debat. Så det er indsatsen i væddemålet, ikke gevinsten — og det er
med vilje at det hedder det.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Node-RED skriver med versaler; vi med minuskler. Og "VP" og "PILLE" duer
# lige saa godt som de lange navne, hvis nogen skulle finde paa at forkorte.
_ALIASES = {
    "varmepumpe": "varmepumpe",
    "vp": "varmepumpe",
    "heatpump": "varmepumpe",
    "pillefyr": "pillefyr",
    "pille": "pillefyr",
    "traepiller": "pillefyr",
}


def normalise(value: Any) -> str | None:
    """Gør Node-REDs svar sammenligneligt med vores."""
    if not isinstance(value, str):
        return None
    return _ALIASES.get(value.strip().lower())


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass
class Accuracy:
    """Hvem rammer den målte COP bedst — os eller Node-RED?

    Det her er den ikke-cirkulære prøve. At tælle uenigheder siger kun hvor
    tit de to peger forskelligt, og «på spil» er regnet med vores egen COP,
    som er præcis det der er til debat.

    Men anlægget måler selv sin COP hvert par minutter. Den måling er dommer:
    begge udgaver kan slå op i det samme (fremløb, ude) og få hver sit tal, og
    så kan man se hvis der lå nærmest. Det kræver ingen enighed om noget som
    helst, og det kan afgøres af data alene.
    """

    samples: float = 0.0
    ours_closer: float = 0.0
    ours_error: float = 0.0
    theirs_error: float = 0.0

    def observe(self, measured: float, ours: float | None, theirs: float | None) -> None:
        if not _finite(measured) or not _finite(ours) or not _finite(theirs):
            return
        mine = abs(ours - measured)
        yours = abs(theirs - measured)
        self.samples += 1
        self.ours_error += mine
        self.theirs_error += yours
        if mine < yours:
            self.ours_closer += 1

    @property
    def ours_mean_error(self) -> float | None:
        return self.ours_error / self.samples if self.samples else None

    @property
    def theirs_mean_error(self) -> float | None:
        return self.theirs_error / self.samples if self.samples else None

    @property
    def ours_closer_percent(self) -> float | None:
        return 100 * self.ours_closer / self.samples if self.samples else None

    @property
    def improvement_percent(self) -> float | None:
        """Hvor meget mindre vores fejl er. Negativt vil sige at vi er værre."""
        if not self.samples or self.theirs_error <= 0:
            return None
        return 100 * (1 - self.ours_error / self.theirs_error)

    def to_raw(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "ours_closer": self.ours_closer,
            "ours_error": round(self.ours_error, 4),
            "theirs_error": round(self.theirs_error, 4),
        }

    @classmethod
    def from_raw(cls, raw: Any) -> Accuracy:
        a = cls()
        if not isinstance(raw, dict):
            return a
        for name in ("samples", "ours_closer", "ours_error", "theirs_error"):
            try:
                value = float(raw.get(name, 0))
            except (TypeError, ValueError):
                value = 0.0
            setattr(a, name, value if math.isfinite(value) else 0.0)
        return a

    def summary(self) -> str:
        if not self.samples:
            return "ingen COP-maalinger at doemme paa endnu"
        return (
            f"vores COP taettest paa i {self.ours_closer_percent:.0f} % af "
            f"{self.samples:.0f} maalinger | middelfejl {self.ours_mean_error:.3f} "
            f"mod {self.theirs_mean_error:.3f}"
        )


@dataclass
class Tally:
    """Regnskabet. Tællere, ikke en historik — vi skal bruge et tal, ikke en log."""

    since: str | None = None
    compared: float = 0.0
    agreed: float = 0.0
    # Opdelt paa retning: det er ikke det samme at vi vil koere varmepumpen
    # hvor Node-RED vil fyre med piller, som det omvendte.
    ours_heatpump: float = 0.0
    ours_boiler: float = 0.0
    # Varme leveret mens vi var uenige, og hvad forskellen var vaerd.
    heat_kwh: float = 0.0
    stake_kr: float = 0.0

    @property
    def disagreed(self) -> float:
        return self.ours_heatpump + self.ours_boiler

    @property
    def agreement_percent(self) -> float | None:
        if self.compared <= 0:
            return None
        return 100 * self.agreed / self.compared

    def observe(
        self,
        ours: str | None,
        theirs: str | None,
        heat_price: float | None,
        pellet_price: float,
        demand_kw: float | None,
        minutes: float,
        today: str | None = None,
    ) -> None:
        """Én cyklus. Kun sammenligninger hvor begge svarede, tæller med."""
        if ours is None or theirs is None:
            return
        if self.since is None:
            self.since = today

        self.compared += 1
        if ours == theirs:
            self.agreed += 1
            return

        if ours == "varmepumpe":
            self.ours_heatpump += 1
        else:
            self.ours_boiler += 1

        # Hvor meget varme blev der leveret mens vi var uenige, og hvad var
        # forskellen paa de to valg vaerd pr. kWh?
        if _finite(demand_kw) and demand_kw > 0 and minutes > 0:
            kwh = demand_kw * minutes / 60
            self.heat_kwh += kwh
            if _finite(heat_price):
                self.stake_kr += abs(heat_price - pellet_price) * kwh

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "compared": self.compared,
            "agreed": self.agreed,
            "ours_heatpump": self.ours_heatpump,
            "ours_boiler": self.ours_boiler,
            "heat_kwh": round(self.heat_kwh, 3),
            "stake_kr": round(self.stake_kr, 3),
        }

    @classmethod
    def from_raw(cls, raw: Any) -> Tally:
        tally = cls()
        if not isinstance(raw, dict):
            return tally
        since = raw.get("since")
        tally.since = since if isinstance(since, str) else None
        for name in ("compared", "agreed", "ours_heatpump", "ours_boiler",
                     "heat_kwh", "stake_kr"):
            try:
                value = float(raw.get(name, 0))
            except (TypeError, ValueError):
                value = 0.0
            setattr(tally, name, value if math.isfinite(value) else 0.0)
        return tally

    # ------------------------------------------------------------------ visning

    def summary(self) -> str:
        """Én linje til loggen."""
        if self.compared <= 0:
            return "ingen sammenligninger endnu"
        pct = self.agreement_percent or 0.0
        return (
            f"enige i {pct:.0f} % af {self.compared:.0f} cyklusser | "
            f"uenige {self.disagreed:.0f} "
            f"(vi vil VP {self.ours_heatpump:.0f}, pille {self.ours_boiler:.0f}) | "
            f"paa spil {self.stake_kr:.2f} kr over {self.heat_kwh:.1f} kWh"
        )
