"""Opladningen som en blok, ikke som en beslutning der tages forfra hvert minut.

Den 6. september tændte og slukkede `binary_sensor.varmeopt_lad_op` fem gange
på en eftermiddag. Sensoren var tro mod beslutningen; det var beslutningen der
svingede, fordi `need = displaced − stored` er differensen mellem to store tal
der ligger oven i hinanden, og den skal blot krydse nul for at udløse et helt
minimumstræk. En kompressor må ikke behandles sådan.

Men rettelsen er ikke hysterese. Anlæggets ejer har sagt hvad funktionen er
til, og det ændrer spørgsmålet:

    Hvis der er behov for varme og det mangler, starter UVR'en den selv. Den
    her optimering er til at planlægge de slot den kan lade tankene op i når
    strømmen er billig, og gøre det én gang.

Opladningen er altså ikke en reaktion på et underskud. Den er en **blok**:
find de billigste halvtimer inden prisen stiger, læg dem fast, kør dem én
gang. Flimret var ikke et symptom der skulle dæmpes — det var et tegn på at
spørgsmålet blev stillet forkert.

**Hvorfor det er det værd.** Formålet er at holde varmepumpen ude af
eksportvinduet. Sælges der til 1,57 kr/kWh mens pumpen kører, er det tabt
indtjening, og koden har ingen «kør ikke»-udgang: kildevalget kan kun vælge
mellem varmepumpe og pillefyr, og ved 1,57 vinder varmepumpen stadig. Den
eneste vej udenom er et lager der er fyldt nok til at UVR'en aldrig
efterspørger varme imens — og det kræver at opladningen ligger rigtigt og er
stor nok.

**Blokken flytter sig indtil den starter.** Priserne opdateres, og en blok der
ikke er begyndt, er ikke et løfte. Det er først ved start den bindes — og så
kan kun to ting afbryde den: at lageret er fuldt, eller at pillefyret er
blevet billigere. Alt andet venter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SLOT_SECONDS = 1800.0


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _slot_start(at: float) -> float:
    """Rund ned til halvtimen. En top skal ikke flytte sig af sig selv.

    ``window_minutes`` regnes forfra hver cyklus og vipper et minut hid og did.
    Uden afrundingen ville den samme pristop se ud som en ny hvert minut, og
    så ville «kør det én gang» aldrig kunne holdes.
    """
    return math.floor(at / SLOT_SECONDS) * SLOT_SECONDS


@dataclass(frozen=True)
class Block:
    """Én planlagt opladning: hvornår, hvor længe, hvor meget.

    ``dear_from``/``dear_until`` er det dyre stræk blokken blev lagt til, som
    absolutte tidspunkter. De afgøres én gang, når blokken lægges — ``_finish``
    har ingen beslutning i hånden og kan ikke regne dem ud bagefter.

    Her stod ``top_at``: den *dyreste* halvtime. Den vandrede. Fordi
    ``cheapest_heat`` er loftet af pillefyrets pris, har hver dyr halvtime i
    et stræk præcis samme værdi, den tidligste vinder — og når den bliver til
    «nu», arver den næste titlen. Spærren «kør det én gang» så derfor en ny
    top hver halve time gennem et stræk der ikke havde rørt sig, og lagde en
    ny blok hver gang. Den 9. september blev det til otte.
    """

    dear_from: float
    dear_until: float
    starts_at: float
    ends_at: float
    kwh: float

    def running(self, now: float) -> bool:
        return self.starts_at <= now < self.ends_at

    def minutes_until(self, now: float) -> float:
        return (self.starts_at - now) / 60

    def minutes_left(self, now: float) -> float:
        return (self.ends_at - now) / 60

    def to_raw(self) -> dict[str, Any]:
        return {
            "dear_from": round(self.dear_from, 1),
            "dear_until": round(self.dear_until, 1),
            "starts_at": round(self.starts_at, 1),
            "ends_at": round(self.ends_at, 1),
            "kwh": round(self.kwh, 3),
        }

    @classmethod
    def from_raw(cls, raw: Any) -> Block | None:
        if not isinstance(raw, dict):
            return None
        try:
            block = cls(
                dear_from=float(raw["dear_from"]),
                dear_until=float(raw["dear_until"]),
                starts_at=float(raw["starts_at"]),
                ends_at=float(raw["ends_at"]),
                kwh=float(raw["kwh"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if block.ends_at <= block.starts_at or block.kwh <= 0:
            return None
        if block.dear_until <= block.dear_from:
            return None
        return block


@dataclass
class ChargePlan:
    """Den blok der er lagt, og det stræk der allerede er klaret."""

    block: Block | None = None
    # Enden paa det dyre straek der er ladet op imod. Saa laenge det straek
    # vi nu sigter mod, begynder inden det her, er det det samme straek - og
    # ét straek giver én opladning.
    done_until: float | None = None
    note: str = "ingen opladning planlagt"

    # ---------------------------------------------------------------- opslag

    @property
    def charging(self) -> bool:
        return self._running

    _running: bool = False

    def slots(self) -> tuple[float, float] | None:
        """Blokkens start og slut, til at tegne «lad op» med."""
        return None if self.block is None else (self.block.starts_at, self.block.ends_at)

    # -------------------------------------------------------------- skridtet

    def update(
        self,
        now: float,
        decision: Any,
        plan: Any,
        rate_kw: float,
        full: bool = False,
    ) -> bool:
        """Ét skridt. Returnerer om der skal lades lige nu."""
        # 1. Kører en blok, er den bundet. Kun to ting bryder den.
        if self.block is not None and self.block.running(now):
            if full:
                return self._finish(now, "lageret blev fuldt")
            if decision is not None and decision.source == "pillefyr":
                return self._finish(now, "pillefyret blev billigere")
            self._running = True
            self.note = (
                f"lader {self.block.kwh:.1f} kWh, "
                f"{self.block.minutes_left(now):.0f} min tilbage"
            )
            return True

        # 2. Er den kørt til ende, er den klaret.
        #
        #    Der behoeves ikke et skridt mere for «straekket er forbi». En
        #    blok slutter altid inden straekket begynder - det er hele dens
        #    formaal - saa naar straekkets ende er passeret, er blokkens ende
        #    passeret for laengst, og den her linje har allerede taget den.
        if self.block is not None and now >= self.block.ends_at:
            return self._finish(now, "kørt")

        self._running = False

        # 3. En blok der venter, droppes kun af de samme to grunde som en der
        #    koerer.
        if self.block is not None and (full or decision is not None
                                       and decision.source == "pillefyr"):
            self.block = None
            self.note = "opladning droppet — " + (
                "lageret er fuldt" if full else "pillefyret vinder"
            )
            return False

        # 4. Er der en hensigt at planlaegge efter?
        #
        #    Er der ikke, staar en allerede lagt blok ved magt. Det er hele
        #    pointen: behovet vipper omkring nul minut for minut, og en blok
        #    der forsvandt hver gang det gjorde, ville vaere det samme flimmer
        #    en etage hoejere oppe.
        want = getattr(decision, "planned_kwh", None) if decision else None
        window = getattr(decision, "window_starts_in", None) if decision else None
        if not _finite(want) or want <= 0 or not window or plan is None:
            if self.block is not None:
                self.note = (
                    f"venter — lader {self.block.kwh:.1f} kWh om "
                    f"{self.block.minutes_until(now):.0f} min"
                )
            else:
                self.note = "ingen opladning planlagt"
            return False

        dear_from, dear_until = self._dear_key(now, decision, window)
        if self.done_until is not None and dear_from < self.done_until:
            # Det her straek er klaret. Ét dyrt straek giver én opladning;
            # foerst naar et *nyt* straek begynder, laegges der en ny blok.
            #
            # Sammenligningen gaar mod straekkets **ende** og ikke mod dets
            # start. Graensen for hvad der er dyrt, kan rykke sig nogle oere
            # fra minut til minut, og saa flytter starten sig en halvtime;
            # enden ligger fast, saa laenge det er det samme straek. Begynder
            # det vi nu sigter mod, inden det vi allerede har daekket er
            # forbi, er det det samme.
            self.block = None
            self.note = "allerede ladet op mod det her dyre stræk"
            return False

        # 5. Laeg blokken - eller flyt den, hvis priserne har rykket sig.
        if rate_kw <= 0:
            return False
        minutes = max(1.0, want / rate_kw * 60)
        # Blokken kan aldrig kraeve flere halvtimer end der er til fristen.
        #
        # Uden ``min`` her faldt hver ellevte cyklus paa en flydendetalskant:
        # planlaeggeren kapper maengden med ``charge_kw * window / 60``, og
        # ``want / rate * 60`` regner det tilbage til 90,000000000000014
        # minutter. ``ceil`` goer det til 91, og et vindue paa 90 minutter har
        # ikke plads til 91. Maalt over 200.000 kombinationer af ladehastighed
        # og vindue skete det i 9,4 % af tilfaeldene.
        needed = min(int(math.ceil(minutes)), max(1, int(window)))
        found = plan.cheapest_window(needed, int(window))
        if found is None:
            # Ingen plads er ikke det samme som «drop det der allerede er
            # lagt». En blok der venter, er lagt paa priser vi har set efter;
            # at der ikke kan lægges en *ny* i det her minut, siger ingenting
            # om den. Foer stod her ``self.block = None``, og saa slettede en
            # forbigaaende trangt vindue en opladning der var klar.
            self.note = f"ingen plads til {minutes:.0f} min inden prisen stiger"
            return False

        offset, _price = found
        starts = now + offset * 60
        self.block = Block(
            dear_from, dear_until, starts, starts + minutes * 60, float(want)
        )
        if self.block.running(now):
            self._running = True
            self.note = f"lader {want:.1f} kWh nu, {minutes:.0f} min"
            return True

        self.note = (
            f"lader {want:.1f} kWh om {self.block.minutes_until(now):.0f} min "
            f"i {minutes:.0f} min"
        )
        return False

    def _dear_key(self, now: float, decision: Any, window: int) -> tuple[float, float]:
        """Det dyre straek som to absolutte tidspunkter.

        Planlaeggeren giver straekket som minutter frem; her bliver det til
        vaegurstid, gulvet til halvtimen, saa det samme straek ser ens ud
        hvert minut.

        Har planlaeggeren intet straek at give - der er ingen timer hvor
        pumpen taber, og heller ingen der er dyrere end nu - falder vi tilbage
        paa den dyreste halvtime, som foer. Netop dér er det ufarligt: den
        dyreste halvtime vandrer kun naar flere halvtimer er lige dyre, og det
        sker kun naar de rammer pillefyrets loft. Gaelder det, findes der et
        straek, og saa er vi ikke her.
        """
        starts = getattr(decision, "dear_starts_in", None)
        span = getattr(decision, "dear_span_minutes", None)
        if _finite(starts) and _finite(span) and span > 0:
            return (
                _slot_start(now + starts * 60),
                _slot_start(now + (starts + span) * 60),
            )
        top = _slot_start(now + (decision.window_minutes or window) * 60)
        return top, top + SLOT_SECONDS

    def _finish(self, now: float, why: str) -> bool:
        if self.block is not None:
            self.done_until = self.block.dear_until
        self.block = None
        self._running = False
        self.note = f"opladning slut — {why}"
        return False

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        # Blokken gemmes med, ogsaa den der er i gang: en genstart midt i en
        # opladning maa ikke starte kompressoren forfra paa den anden side.
        # Det er forskellen fra staatabsmaalingen, hvor et afbrudt vindue er
        # ubrugeligt - her er en halvfaerdig opladning stadig en opladning.
        return {
            "block": None if self.block is None else self.block.to_raw(),
            "done_until": self.done_until,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> ChargePlan:
        plan = cls()
        if not isinstance(raw, dict):
            return plan
        plan.block = Block.from_raw(raw.get("block"))
        # ``done_top`` fra en aeldre udgave laeses ikke. Det var enden paa en
        # *halvtime*, ikke paa et straek, og at laese det ville kun kunne
        # spaerre for meget. Prisen er én ekstra tilladt blok den dag
        # add-on'en opdateres.
        until = raw.get("done_until")
        if _finite(until):
            plan.done_until = float(until)
        if plan.block is not None:
            plan.note = "genoptager planlagt opladning"
        return plan
