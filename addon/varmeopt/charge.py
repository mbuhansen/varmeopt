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

# Så længe «lageret er fuldt» skal holde, før en kørende blok afsluttes.
#
# ``headroom`` er en sum over otte termometre, og ét af dem kan poste et
# udsving. Tre minutter mod et fuldt lager er tre minutter hvor kompressoren
# leverer i noget der ikke kan optage det - en ærlig, lille pris. Femten
# ville være et rigtigt overskud og et højtryk.
FULL_HOLD_SECONDS = 180.0


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def slot_start(at: float) -> float:
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
    # Enden på det dyre stræk der er ladet op imod. Så længe det stræk
    # vi nu sigter mod, begynder inden det her, er det det samme stræk - og
    # ét stræk giver én opladning.
    done_until: float | None = None
    note: str = "ingen opladning planlagt"
    # Hvornår lageret første gang meldte sig fuldt i det her forløb.
    _full_since: float | None = None

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
        source: str | None = None,
        min_runtime_minutes: float = 0.0,
    ) -> bool:
        """Ét skridt. Returnerer om der skal lades lige nu.

        ``source`` er den kilde vagten står ved - den med hviletiden. Uden
        den læste vi planlæggerens rå svar, og så kunne ét minuts udsving
        i COP eller pris afslutte en opladning som vagten samtidig holdt på
        varmepumpen. Er den ukendt, spørges beslutningen som før.
        """
        chosen = source if source is not None else getattr(decision, "source", None)

        # 1. Kører en blok, er den bundet. Kun to ting bryder den.
        if self.block is not None and self.block.running(now):
            if full:
                # Ét minut er ikke nok. ``headroom`` er en sum over otte
                # termometre, og et enkelt udsving må ikke afslutte en
                # opladning - og *brænde* strækket med, så der ikke kan
                # lægges en ny.
                if self._full_since is None:
                    self._full_since = now
                if now - self._full_since >= FULL_HOLD_SECONDS:
                    return self._finish(now, "lageret blev fuldt")
            else:
                self._full_since = None
            # Kortcykling slider. En blok der lige er startet, afsluttes ikke
            # fordi pillefyret vandt et minut - men et fuldt lager går
            # forud, for der er ingen varme at levere ind i.
            young = (now - self.block.starts_at) / 60 < min_runtime_minutes
            if chosen == "pillefyr" and not young:
                return self._finish(now, "pillefyret blev billigere")
            self._running = True
            self.note = (
                f"lader {self.block.kwh:.1f} kWh, "
                f"{self.block.minutes_left(now):.0f} min tilbage"
            )
            return True

        # 2. Er den kørt til ende, er den klaret.
        #
        #    Der behøves ikke et skridt mere for «strækket er forbi». En
        #    blok slutter altid inden strækket begynder - det er hele dens
        #    formål - så når strækkets ende er passeret, er blokkens ende
        #    passeret for længst, og den her linje har allerede taget den.
        if self.block is not None and now >= self.block.ends_at:
            return self._finish(now, "kørt")

        self._running = False

        # 3. En blok der venter, droppes kun af de samme to grunde som en der
        #    kører.
        if self.block is not None and (full or chosen == "pillefyr"):
            self.block = None
            self.note = "opladning droppet — " + (
                "lageret er fuldt" if full else "pillefyret vinder"
            )
            return False

        # 4. Er der en hensigt at planlægge efter?
        #
        #    Er der ikke, står en allerede lagt blok ved magt. Det er hele
        #    pointen: behovet vipper omkring nul minut for minut, og en blok
        #    der forsvandt hver gang det gjorde, ville være det samme flimmer
        #    en etage højere oppe.
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
            # Det her stræk er klaret. Ét dyrt stræk giver én opladning;
            # først når et *nyt* stræk begynder, lægges der en ny blok.
            #
            # Sammenligningen går mod strækkets **ende** og ikke mod dets
            # start. Grænsen for hvad der er dyrt, kan rykke sig nogle øre
            # fra minut til minut, og så flytter starten sig en halvtime;
            # enden ligger fast, så længe det er det samme stræk. Begynder
            # det vi nu sigter mod, inden det vi allerede har dækket er
            # forbi, er det det samme.
            #
            # **Og der er ingen undtagelse for et tømt lager.** Den var
            # planlagt - «genlæg hvis lageret løber tørt, og der stadig
            # ligger en billigere halvtime inden det dyre er forbi» - men den
            # kan ikke fyre. Strækket *er* de timer hvor varmepumpen taber
            # til pillefyret; en halvtime derinde der var billig nok til at
            # lade op i, ville have afsluttet strækket. Betingelsen modsiger
            # sin egen forudsætning.
            #
            # Det er heller ikke et hul. Løber lageret tørt midt i det dyre,
            # starter UVR'en selv pumpen ved det setpunkt fremløbet kræver -
            # og tager kun den varme huset beder om, ved den bedre COP der
            # hører til 32 grader frem for 56. En genlagt blok ville køre
            # 56 og fylde *hele* lageret til aftenpris. Kildevalget siger
            # samtidig pillefyr, for det er derfor strækket er et stræk.
            # Alle tre veje er billigere end den undtagelse der udgik.
            self.block = None
            self.note = "allerede ladet op mod det her dyre stræk"
            return False

        # 5. Læg blokken - eller flyt den, hvis priserne har rykket sig.
        if rate_kw <= 0:
            return False
        minutes = max(1.0, want / rate_kw * 60)
        # Blokken kan aldrig kræve flere halvtimer end der er til fristen.
        #
        # Uden ``min`` her faldt hver ellevte cyklus på en flydendetalskant:
        # planlæggeren kapper mængden med ``charge_kw * window / 60``, og
        # ``want / rate * 60`` regner det tilbage til 90,000000000000014
        # minutter. ``ceil`` gør det til 91, og et vindue på 90 minutter har
        # ikke plads til 91. Målt over 200.000 kombinationer af ladehastighed
        # og vindue skete det i 9,4 % af tilfældene.
        needed = min(int(math.ceil(minutes)), max(1, int(window)))
        found = plan.cheapest_window(needed, int(window))
        if found is None:
            # Ingen plads er ikke det samme som «drop det der allerede er
            # lagt». En blok der venter, er lagt på priser vi har set efter;
            # at der ikke kan lægges en *ny* i det her minut, siger ingenting
            # om den. Før stod her ``self.block = None``, og så slettede en
            # forbigående trangt vindue en opladning der var klar.
            self.note = f"ingen plads til {minutes:.0f} min inden prisen stiger"
            return False

        offset, _price = found
        # ``offset`` tælles i hele halvtimer fra den halvtime vi *står i*,
        # ikke fra det her sekund. Uden gulvet gled en ventende bloks start
        # ét minut frem pr. cyklus og sprang 30 minutter tilbage ved hver
        # :00/:30 - så den stod aldrig stille længe nok til at kunne læses.
        starts = slot_start(now) + offset * 60
        # Og længden måles fra det seneste af de to. Starter blokken nu, kan
        # ``slot_start(now)`` ligge op til 29 minutter tilbage i tiden, og så
        # ville blokken blive tilsvarende for kort.
        ends = max(starts, now) + minutes * 60
        self.block = Block(dear_from, dear_until, starts, ends, float(want))
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
        """Det dyre stræk som to absolutte tidspunkter.

        Planlæggeren giver strækket som minutter frem; her bliver det til
        vægurstid, gulvet til halvtimen, så det samme stræk ser ens ud
        hvert minut.

        Har planlæggeren intet stræk at give - der er ingen timer hvor
        pumpen taber, og heller ingen der er dyrere end nu - falder vi tilbage
        på den dyreste halvtime, som før. Netop dér er det ufarligt: den
        dyreste halvtime vandrer kun når flere halvtimer er lige dyre, og det
        sker kun når de rammer pillefyrets loft. Gælder det, findes der et
        stræk, og så er vi ikke her.
        """
        starts = getattr(decision, "dear_starts_in", None)
        span = getattr(decision, "dear_span_minutes", None)
        if starts is not None and span is not None and _finite(starts) and _finite(span):
            first, length = float(starts), float(span)
            if length > 0:
                return (
                    slot_start(now + first * 60),
                    slot_start(now + (first + length) * 60),
                )
        top = slot_start(now + (decision.window_minutes or window) * 60)
        return top, top + SLOT_SECONDS

    def _finish(self, now: float, why: str) -> bool:
        if self.block is not None:
            self.done_until = self.block.dear_until
        self.block = None
        self._running = False
        self._full_since = None
        self.note = f"opladning slut — {why}"
        return False

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        # Blokken gemmes med, også den der er i gang: en genstart midt i en
        # opladning må ikke starte kompressoren forfra på den anden side.
        # Det er forskellen fra ståtabsmålingen, hvor et afbrudt vindue er
        # ubrugeligt - her er en halvfærdig opladning stadig en opladning.
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
        # ``done_top`` fra en ældre udgave læses ikke. Det var enden på en
        # *halvtime*, ikke på et stræk, og at læse det ville kun kunne
        # spærre for meget. Prisen er én ekstra tilladt blok den dag
        # add-on'en opdateres.
        until = raw.get("done_until")
        if _finite(until):
            plan.done_until = float(until)
        if plan.block is not None:
            plan.note = "genoptager planlagt opladning"
        return plan
