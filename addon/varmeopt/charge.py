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
kan kun tre ting afbryde den: at lageret har nået det strækket skal bruge,
at lageret er fuldt, eller at pillefyret er blevet billigere. Alt andet venter.

**Det er lageret der måles på.** Blokkens kWh er et skøn over hvad der skal
til; om målet er nået, afgøres af tankenes termometre - se ``target_met`` i
``update``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

SLOT_SECONDS = 1800.0

# Så længe «lageret er fuldt» skal holde, før en kørende blok afsluttes.
#
# ``headroom`` er en sum over otte termometre, og ét af dem kan poste et
# udsving. Tre minutter mod et fuldt lager er tre minutter hvor kompressoren
# leverer i noget der ikke kan optage det - en ærlig, lille pris. Femten
# ville være et rigtigt overskud og et højtryk.
FULL_HOLD_SECONDS = 180.0

# Hvor tæt på sin mængde en blok skal være for at regnes for leveret.
#
# Varmeydelsen integreres minut for minut, så der samler sig en smule
# afrunding; og de sidste par hundrede watt-timer er ikke værd at holde
# kompressoren i gang for. Grænsen er den samme størrelsesorden som
# følerstøjen på lagerets otte termometre.
DELIVERED_TOLERANCE_KWH = 0.2

# Længste spring mellem to cyklusser der tælles med i den leverede varme.
#
# Add-on'en kører hvert minut. Er der gået længere, har den været nede - og
# så ved vi ikke hvad pumpen lavede imens. At lade være med at tælle er den
# konservative vej: blokken tror den har leveret mindre end den måske har,
# og forlænger derfor lidt for meget. Fristen er loftet der fanger det.
MAX_INTEGRATION_GAP_SECONDS = 300.0

# Hvor stor en del af den kørte blok der skal være målt, før den leverede
# mængde må forlænge den.
#
# Uden den her forlængede en blok sig blindt på ethvert anlæg uden
# varmeydelsesføler: ``delivered_kwh`` ville stå på nul, og nul er jo mindre
# end mængden. Der findes ikke en dårligere grund til at holde kompressoren
# i gang end at man ikke kan måle. Kan vi ikke se hvad der gik ind, kører
# blokken på uret som den altid har gjort.
MIN_MEASURED_SHARE = 0.5

# Så mange gange sin egen mængde må en automatisk blok levere, før den stoppes
# - uanset hvad lageret siger.
#
# Den 18. oktober blev en blok på 3,6 kWh lagt i den sidste nethalvtime kl.
# 04:45. Lageret nåede ikke målet, forlængelsen satte enden til fristen kl.
# 09:00, og bagefter svarede planlæggeren «intet at hente» - uden at regne
# manglen ud, så målet blev ukendt, og ingen af blokkens udgange kunne fyre.
# Den leverede 41,6 kWh på batteriets strøm, til den blev stoppet med
# knappen. Lageret er stadig det der afgør, men et skøn der rammer ved siden
# af med mere end det dobbelte, er ikke længere et skøn.
MAX_DELIVERED_SHARE = 2.0

# Hvor stor en del af blokkens egen længde der holdes fri mellem dens ende og
# fristen.
#
# Blokken lægges så sent som muligt i det billige - varme der er lavet for
# tidligt, står og taber sig, og imens kører huset af lageret i stedet for af
# en varmepumpe ved rumvarmens setpunkt. Men presses den helt op mod fristen,
# slutter den *præcis* der, og så kan ``_extend`` aldrig fyre: den kræver
# plads mellem enden og fristen. De to ønsker trækker mod hinanden, og det
# her er prisen for at beholde begge - et stykke slæk, betalt i lidt mere
# ståtab, så minutter pumpen taber, stadig kan hentes ind.
#
# Gulvet er ét minimumstræk. Kortere end det kan kompressoren alligevel ikke
# hente noget i.
CHARGE_RESERVE_SHARE = 0.3

# Noten når der ikke er noget at sige. Den står som en konstant, så loggen kan
# tie i netop det tilfælde uden at gætte på ordlyden - og sige noget i alle
# de andre, hvor noten er det eneste sted der står hvorfor flaget er slukket.
NO_PLAN = "ingen opladning planlagt"


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


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


def block_window(
    plan: Any,
    want_kwh: float,
    rate_kw: float,
    window: int,
    min_runtime_minutes: float = 0.0,
    grid: Any = None,
    elapsed: float = 0.0,
) -> tuple[int, float] | None:
    """Hvor en blok på ``want_kwh`` skal ligge - og hvor langt den kan nå.

    Returnerer (minutter fra halvtimens start til blokken, minutter fra
    nettet der kan nås derfra) eller ``None``. **Planlæggeren og blokken
    kalder begge den her.** Før ledte de hver for sig: blokken med en reserve
    før fristen, planlæggeren uden. Gennemgangen den 18. oktober fandt et
    vindue hvor planlæggeren regnede gevinsten på en nethalvtime til 1,20 og
    sagde «venter», mens blokken lagde sig nu til 1,54 - fordi reserven
    skubbede den sene halvtime ud af blokkens søgning.

    Reglerne er blokkens:

    * Først i et vindue der slutter en reserve før fristen, så der er noget
      at strække sig ud i - se ``CHARGE_RESERVE_SHARE``. Kan den ikke være
      der, i hele vinduet.
    * Kun hvor strømmen kommer fra nettet.
    * En nu-halvtime tæller kun med det der er tilbage af den
      (``elapsed``). Er der mindre tilbage end én mindstekørselstid, er den
      ikke en blok, og så prøves de senere nethalvtimer; den 18. oktober kl.
      04:52 var der otte minutter tilbage, og svaret blev «for lidt», selv om
      en senere nethalvtime kunne have båret det hele. Er der mindst én
      mindstekørselstid tilbage, står den billigste halvtime - også selv om
      den ikke bærer hele mængden, for en kortere kørsel til 1,54 er stadig
      billigere end en hel til 1,70. Så kortes blokken ned, som før.
    * Kommer strømmen fra solen nu, lades der nu. Det er ejerens regel, og
      den stod før kun i planlæggeren: er det solen der leverer, skal
      tankene bare fyldes; det er kun når der lades fra nettet, at timen skal
      være den billigste. Blokken ventede imens på en billigere nethalvtime,
      mens planlæggeren sagde «lader nu». Kun når *prisen* siger sol - står
      batteriet frit, er den næste kilowatt-time batteriets.
    """
    if rate_kw <= 0 or not _finite(want_kwh) or want_kwh <= 0 or window <= 0:
        return None
    minutes = max(1.0, want_kwh / rate_kw * 60)
    needed = min(int(math.ceil(minutes)), max(1, int(window)))
    reserve = max(float(min_runtime_minutes), CHARGE_RESERVE_SHARE * minutes)

    def search(not_before: int) -> tuple[int, float] | None:
        room = int(window - reserve)
        found = None
        if room >= needed:
            found = plan.cheapest_window(
                needed, room, grid=grid, grid_only=True, not_before_minutes=not_before
            )
        if found is None:
            found = plan.cheapest_window(
                needed,
                int(window),
                grid=grid,
                grid_only=True,
                not_before_minutes=not_before,
            )
        return found

    def reach(offset: int) -> float:
        run = plan.grid_run(offset, window, grid)
        return max(0.0, run - elapsed) if offset == 0 else run

    from .prices import SUN

    first = plan.marginal(0, grid=grid)
    if (
        first is not None
        and first.source == SUN
        and reach(0) + 1e-6 >= min(minutes, float(min_runtime_minutes))
    ):
        return 0, reach(0)

    found = search(0)
    if found is None:
        return None
    offset = found[0]
    if offset == 0 and reach(0) + 1e-6 < min(minutes, float(min_runtime_minutes)):
        later = search(SLOT_SECONDS // 60)
        if later is not None:
            offset = later[0]
    return offset, reach(offset)


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
    # Den yderste grænse blokken må forlænges til: der hvor det dyre stræk
    # begynder. En blok der løber ind i det dyre, modarbejder sit eget
    # formål, så fristen er et hårdt loft og ikke et ønske.
    deadline: float = 0.0
    # Hvor meget varme pumpen faktisk har leveret siden blokken begyndte.
    #
    # Blokkens længde regnes af en *målt* ladehastighed, men hastigheden er
    # et gennemsnit, og et minut hvor pumpen står stille - en afrimning, en
    # genstart, vagtens indkøring - er et minut uret bruger alligevel. Den
    # 17. september stod pumpen stille de første 25 minutter af en blok på
    # 119, og de 20,8 kWh ville være landet på ca. 16. Derfor tælles der med.
    delivered_kwh: float = 0.0
    # Hvor mange sekunder af blokken vi rent faktisk har haft en måling for.
    # En ydelse på nul er en måling - pumpen stod stille - mens en føler der
    # er væk, ikke er det. Forskellen afgør om ``delivered_kwh`` må bruges.
    measured_seconds: float = 0.0
    # Hvornår kompressoren faktisk starter. ``starts_at`` står på halvtimen,
    # så planens rækker kan tegne blokken; lægges den kl. :20 og starter med
    # det samme, er den første halvtime :00 - men den er ikke tyve minutter
    # gammel. Mindstekørselstiden regnes herfra.
    began_at: float | None = None
    # Startet med knappen på plan-siden. En manuel blok er brugerens ordre:
    # den stoppes ikke af at pillefyret vinder, og den brænder ikke et dyrt
    # stræk - planlæggeren må gerne lægge sin egen bagefter, hvis der stadig
    # mangler noget.
    manual: bool = False
    # Lagt mod et stræk der allerede er ladet op imod - se «efteropladningen»
    # i ``ChargePlan.update``. Når den er kørt, er der ikke flere.
    top_up: bool = False
    # Planlæggerens løfte: besparelsen og de to varmepriser den er regnet af.
    # Blokken husker dem, fordi beslutningen i det minut blokken starter, ofte
    # ikke har noget løfte - den er lagt mens svaret var «venter», og så stod
    # regnskabets «Lovet» tom på ni af tolv opladninger.
    expected_kr: float | None = None
    expected_paid: float | None = None
    expected_alt: float | None = None
    # Forlænget fordi lageret ikke havde nået målet. Så hviler blokkens ende
    # på at målet kan måles, og forsvinder målingen, skal der være en anden
    # vej ud - se ``update``.
    extended: bool = False
    # Enden på det spænd mængden blev regnet over. Normalt det samme som
    # ``dear_until``, men ikke for planlæggerens relative tilbagefald: dér
    # slutter spærren med den første top, mens mængden er regnet over hele
    # spændet. Lagerets mål skal måles over det samme spænd som mængden -
    # ellers siger blokken «nået» mens planlæggeren siger at der mangler.
    # Nul fra en ældre udgave betyder ``dear_until``.
    need_until: float = 0.0

    @property
    def target_until(self) -> float:
        """Enden på det spænd lagerets mål måles over."""
        return self.need_until if self.need_until > 0 else self.dear_until

    @property
    def began(self) -> float:
        return self.starts_at if self.began_at is None else self.began_at

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
            "deadline": round(self.deadline, 1),
            "delivered_kwh": round(self.delivered_kwh, 3),
            "measured_seconds": round(self.measured_seconds, 1),
            "began_at": None if self.began_at is None else round(self.began_at, 1),
            "manual": self.manual,
            "top_up": self.top_up,
            "expected_kr": _rounded(self.expected_kr),
            "expected_paid": _rounded(self.expected_paid),
            "expected_alt": _rounded(self.expected_alt),
            "extended": self.extended,
            "need_until": round(self.need_until, 1),
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
                # Fra en ældre udgave mangler de to. Uden en frist kan
                # blokken ikke forlænges - den kører som den altid har
                # gjort, og det er den rigtige vej at fejle.
                deadline=float(raw.get("deadline") or 0.0),
                delivered_kwh=float(raw.get("delivered_kwh") or 0.0),
                measured_seconds=float(raw.get("measured_seconds") or 0.0),
                # Fra en ældre udgave mangler den; så gælder halvtimen.
                began_at=(
                    float(raw["began_at"]) if raw.get("began_at") is not None else None
                ),
                manual=raw.get("manual") is True,
                top_up=raw.get("top_up") is True,
                expected_kr=_number(raw.get("expected_kr")),
                expected_paid=_number(raw.get("expected_paid")),
                expected_alt=_number(raw.get("expected_alt")),
                extended=raw.get("extended") is True,
                need_until=float(raw.get("need_until") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if block.ends_at <= block.starts_at or block.kwh <= 0:
            return None
        if block.dear_until <= block.dear_from:
            return None
        if block.began_at is not None and not (
            block.starts_at <= block.began_at <= block.ends_at
        ):
            # En start uden for blokken ville kunne holde den «ung» hele
            # vejen og skærme den mod pillefyret. Så hellere halvtimen.
            block = replace(block, began_at=None)
        return block


@dataclass
class ChargePlan:
    """Den blok der er lagt, og det stræk der allerede er klaret."""

    block: Block | None = None
    # Enden på det dyre stræk der er ladet op imod. Så længe det stræk
    # vi nu sigter mod, begynder inden det her, er det det samme stræk - og
    # ét stræk giver én opladning.
    done_until: float | None = None
    # Må der lægges én blok mere mod det stræk ``done_until`` dækker? Kun
    # når den forrige leverede sin mængde, målt - se efteropladningen i
    # ``update``.
    top_up_open: bool = False
    note: str = NO_PLAN
    # Den regel der står bag noten, som en kode - til regelkortet på
    # plan-siden. Noten er en sætning; koden er det siden kan sætte en prik
    # efter.
    rule: str = "no_plan"
    # Hvorfor den seneste blok sluttede: tidspunkt, kode og noten.
    last_end: dict[str, Any] | None = None
    # Hvornår lageret første gang meldte sig fuldt i det her forløb.
    _full_since: float | None = None
    # De Predbat-planer ønsket om at lade har holdt på, som deres stempler.
    #
    # Add-on'en regner hvert minut; Predbat cirka hvert femte. Den 23.
    # september kl. 08:11 skiftede nu-timen fra «eksport» til «batteri» midt
    # i én og samme Predbat-plan, og ét minuts svar blev bundet som en blok på
    # 24,5 kWh. Et ønske skal overleve en ny Predbat-beregning, før det bliver
    # til en blok.
    _wanted_stamps: list[str] = field(default_factory=list)
    # Stemplet på den plan en ventende blok senest blev lagt efter. Mellem to
    # Predbat-planer står den stille; der er ingen nye priser at flytte den på.
    _placed_stamp: str | None = None
    # Hvornår vi sidst så varmeydelsen. Integrationen har brug for et
    # mellemrum, ikke et øjebliksbillede.
    _last_seen: float | None = None
    # De Predbat-planer der har lagt halvtimen vi står i, på batteriet, mens
    # en blok kørte. Samme regel som ønsket om at lade: ét minuts svar binder
    # ikke, en ny Predbat-beregning gør.
    _battery_stamps: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- opslag

    @property
    def charging(self) -> bool:
        return self._running

    _running: bool = False

    def slots(self) -> tuple[float, float] | None:
        """Blokkens start og slut, til at tegne «lad op» med."""
        return None if self.block is None else (self.block.starts_at, self.block.ends_at)

    def running(self, now: float) -> bool:
        """Kører der en blok lige nu - automatisk eller manuel?"""
        return self.block is not None and self.block.running(now)

    @property
    def manual(self) -> bool:
        return self.block is not None and self.block.manual

    # ----------------------------------------------------------------- knappen

    def remember_promise(self, decision: Any, now: float) -> None:
        """Skriv planlæggerens løfte på blokken, så regnskabet kan finde det.

        Det seneste løfte før blokken begynder, gælder - priserne kan have
        flyttet sig siden den blev lagt. Når den er begyndt, står løftet fast,
        for så er det regnet på det der faktisk sker.
        """
        block = self.block
        if block is None or block.manual:
            return
        if now >= block.began and block.expected_kr is not None:
            return
        saving = getattr(decision, "saving_kr", None)
        if not getattr(decision, "charge", False) or not _finite(saving):
            return
        paid = getattr(decision, "expected_paid", None)
        alt = getattr(decision, "expected_alt", None)
        self.block = replace(
            block,
            expected_kr=float(saving),
            expected_paid=float(paid) if _finite(paid) else None,
            expected_alt=float(alt) if _finite(alt) else None,
        )

    def start_manual(self, now: float, minutes: float, rate_kw: float) -> str:
        """Lad op nu, i så mange minutter - startet med knappen på plan-siden.

        En ventende automatisk blok erstattes; en der allerede kører, får lov
        at køre færdig, for den er lagt på priser og bundet af sin start.
        """
        if self.running(now) and not self.manual:
            return "der kører allerede en opladning"
        if minutes <= 0 or rate_kw <= 0:
            return "ingen plads i lageret at lade op i"
        start = slot_start(now)
        ends = now + minutes * 60
        self.block = Block(
            dear_from=start,
            dear_until=ends,
            starts_at=start,
            ends_at=ends,
            kwh=rate_kw * minutes / 60,
            deadline=ends,
            began_at=now,
            manual=True,
        )
        self._running = True
        self._full_since = None
        self._forget_wish()
        self.rule = "manual"
        self.note = f"manuel opladning, {minutes:.0f} min"
        return f"opladning startet — {self.block.kwh:.1f} kWh over {minutes:.0f} min"

    def stop(self, now: float) -> str:
        """Stop den blok der kører - med knappen.

        En manuel blok efterlader intet. En automatisk regnes som klaret, ellers
        ville planlæggeren lægge den igen i næste minut.
        """
        if not self.running(now):
            return "der kører ingen opladning"
        manual = self.manual
        self._finish(now, "stoppet med knappen", code="button")
        return "manuel opladning stoppet" if manual else "opladning stoppet"

    # -------------------------------------------------------------- skridtet

    def _track(self, now: float, heat_kw: float | None) -> None:
        """Tæl den varme pumpen har leveret siden sidste cyklus.

        Kun mens blokken kører. Varmen inden den begyndte, hører til huset;
        varmen efter hører til den næste blok.

        Føleren er UVR'ens egen på varmepumpens kreds - den samme
        ``charge_rate`` allerede måler hastigheden med. Er den væk, tælles
        der ikke: et gæt ville enten forlænge en blok der var færdig, eller
        afslutte en der ikke var.
        """
        seen, self._last_seen = self._last_seen, now
        block = self.block
        if block is None or block.manual:
            return
        if not (block.began <= now and now < block.ends_at):
            return
        if seen is None or heat_kw is None or not _finite(heat_kw):
            return
        gap = now - seen
        if gap <= 0 or gap > MAX_INTEGRATION_GAP_SECONDS:
            # Add-on'en har været nede. Vi ved ikke hvad pumpen lavede.
            return
        # En ydelse på nul tælles med i *begge* tal: der kom ingen varme, og
        # vi ved det. Det er netop de minutter forlængelsen findes for.
        self.block = replace(
            block,
            delivered_kwh=block.delivered_kwh + max(0.0, heat_kw) * gap / 3600,
            measured_seconds=block.measured_seconds + gap,
        )

    def _extend(
        self,
        now: float,
        rate_kw: float,
        target_met: bool | None = None,
        plan: Any = None,
        grid: Any = None,
    ) -> None:
        """Lad blokken køre videre hvis den ikke nåede sin mængde.

        Blokkens længde regnes af en målt ladehastighed, og den hastighed er
        et gennemsnit over døgn. Et minut hvor pumpen ikke leverer, er
        stadig et minut uret bruger: den 17. september stod pumpen stille de
        første 25 minutter af en blok på 119 - add-on'en var lige genstartet
        - og de 20,8 kWh ville være landet på ca. 16. Bagefter markerede
        ``_finish`` strækket som klaret, så der kom ingen ny blok.

        Fristen er et hårdt loft. En blok der løber ind i det dyre stræk,
        modarbejder sit eget formål: pointen er at lageret er fyldt *inden*
        prisen stiger. Når fristen er nået, slutter blokken - også med en
        mængde der mangler. Det står allerede på siden som «dækker ikke
        vinduet».

        En manuel blok forlænges ikke. Den er en ordre på så mange minutter,
        ikke på så mange kilowatt-timer.

        **Og kun gennem halvtimer fra nettet.** Blokken lægges kun hvor
        Predbat selv tager strømmen fra nettet; forlængelsen må ikke smugle
        den ud i batteriets timer. Den 18. oktober blev en blok lagt i den
        sidste nethalvtime kl. 04:45 og forlænget lige til fristen kl. 09:00 -
        fire timer på batteriet, til 1,85-2,08 kr/kWh.
        """
        block = self.block
        if block is None or block.manual or rate_kw <= 0:
            return
        if now < block.ends_at or block.deadline <= block.ends_at:
            return
        limit = self._grid_reach(plan, now, block.deadline, grid)
        if limit <= now:
            return
        # Lageret går forud for pumpens kWh. Har det ikke nået målet, kører
        # blokken videre til fristen - eller til den første halvtime fra
        # batteriet - og slutter i ``update`` i det minut målet er nået. Har
        # det nået målet, er der intet at forlænge for.
        if target_met is not None:
            if not target_met:
                self.block = replace(block, ends_at=limit, extended=True)
            return
        # Uden måling af en rimelig del af blokken ved vi ikke hvad der gik
        # ind, og så er uret det bedste vi har.
        ran = now - block.began
        if ran <= 0 or block.measured_seconds < MIN_MEASURED_SHARE * ran:
            return
        missing = block.kwh - block.delivered_kwh
        if missing <= DELIVERED_TOLERANCE_KWH:
            return
        ends = min(limit, now + missing / rate_kw * 3600)
        if ends <= now:
            return
        self.block = replace(block, ends_at=ends)

    @staticmethod
    def _grid_reach(
        plan: Any, now: float, until: float, grid: Any, start: float | None = None
    ) -> float:
        """Hvor langt frem strømmen kommer fra nettet uden afbrydelse.

        Fra halvtimen ``start`` står i - den vi står i, hvis den ikke er givet
        - og frem til den første der tager af batteriet, højst til ``until``.
        Nu-halvtimen prissættes med målingen, ligesom planlæggeren gør. Uden
        en plan ved vi intet, og så gælder ``until`` som før.
        """
        if plan is None:
            return until
        from .prices import BATTERY

        base = slot_start(now)
        index = 0
        if start is not None:
            index = max(0, int((slot_start(start) - base) // SLOT_SECONDS))
        while base + index * SLOT_SECONDS < until:
            price = plan.marginal(index * 30, grid=grid if index == 0 else None)
            if price is not None and price.source == BATTERY:
                return base + index * SLOT_SECONDS
            index += 1
        return until

    def update(
        self,
        now: float,
        decision: Any,
        plan: Any,
        rate_kw: float,
        full: bool = False,
        source: str | None = None,
        min_runtime_minutes: float = 0.0,
        heat_kw: float | None = None,
        grid: Any = None,
        plan_stamp: str | None = None,
        confirm_plans: int = 1,
        target_met: bool | None = None,
    ) -> bool:
        """Ét skridt. Returnerer om der skal lades lige nu.

        ``source`` er den kilde vagten står ved - den med hviletiden. Uden
        den læste vi planlæggerens rå svar, og så kunne ét minuts udsving
        i COP eller pris afslutte en opladning som vagten samtidig holdt på
        varmepumpen. Er den ukendt, spørges beslutningen som før.

        ``grid`` er den fysiske strømretning, og den gælder kun den halvtime
        vi står i - men den *skal* med. Uden den prissætter ``cheapest_window``
        række 0 som om batteriet var låst, og så kan blokken lægges oven på
        en halvtime planlæggeren i samme cyklus har kaldt for dyr.

        ``plan_stamp`` er Predbat-planens ``last_updated`` - den flytter sig
        hver gang Predbat regner. En blok lægges først når ønsket har holdt på
        ``confirm_plans`` forskellige stempler, og en ventende blok flyttes kun
        når stemplet er nyt. Uden stempel lægges blokken straks, som før.

        ``heat_kw`` er varmepumpens ydelse lige nu, målt. Den tælles op i
        blokkens ``delivered_kwh``, så blokken kan slutte på den mængde den
        blev lagt for og ikke bare på uret - se ``_extend``. Uden den
        opfører blokken sig som før.

        ``target_met`` er lagerets eget svar: står der nu så meget over 55
        grader som varmtvandet skal bruge i strækket, og har rumvarmen sit?
        **Det er lageret der skal fyldes til en bestemt tid, ikke pumpen der
        skal levere et tal.** Pumpens kWh var et skøn over hvad der skulle
        til, og den 30. september ramte skønnet ved siden af - 5,4 kWh
        leveret, nul over 55. Så længe målet kendes, er det derfor det der
        afslutter en blok, og pumpens kWh bruges kun når det ikke kendes.
        """
        chosen = source if source is not None else getattr(decision, "source", None)

        # 0. Lad blokken strække sig hvis den ikke nåede sin mængde, og tæl
        #    så den leverede varme op. Det skal ske *før* alt andet: er
        #    blokken forlænget, er den stadig i gang, og så gælder alle de
        #    regler der holder en kørende blok - også dem der afbryder den.
        #
        #    **Rækkefølgen er ikke til forhandling.** ``_track`` tæller kun
        #    mens blokken kører, og præcis i det minut hvor uret løber ud,
        #    gør den ikke. Talte vi først, ville det minut falde på gulvet:
        #    ``_extend`` så en mængde der manglede, forlængede med nøjagtig
        #    det ene minut den manglede - og landede på kanten igen. Målt i
        #    testen blev det til en blok der voksede et minut ad gangen uden
        #    nogensinde at blive færdig. Strækker vi først, er blokken i gang
        #    når minuttet tælles, og det lander hvor det hører hjemme.
        self._extend(now, rate_kw, target_met, plan, grid)
        self._track(now, heat_kw)

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
                    return self._finish(now, "lageret blev fuldt", code="full")
            else:
                self._full_since = None
            # Kortcykling slider. En blok der lige er startet, afsluttes ikke
            # fordi pillefyret vandt et minut - men et fuldt lager går
            # forud, for der er ingen varme at levere ind i.
            young = (now - self.block.began) / 60 < min_runtime_minutes
            # Målet er nået: lageret har det strækket skal bruge. Men først
            # når mindstekørselstiden er gået - en kompressor der lige er
            # startet, stoppes ikke af at et termometer krydsede en grænse.
            if target_met and not young and not self.block.manual:
                return self._finish(now, "lageret har nået målet", code="target_met")
            if not young and not self.block.manual:
                block = self.block
                ran = now - block.began
                measured = (
                    ran > 0 and block.measured_seconds >= MIN_MEASURED_SHARE * ran
                )
                # Loftet. Lageret afgør, men ikke uden ende - se
                # ``MAX_DELIVERED_SHARE``.
                # Mindst ét minimumstræk: en lille blok kan ikke stoppes før
                # mindstekørselstiden, og så leverer den altid mindst det.
                floor = rate_kw * min_runtime_minutes / 60
                cap = MAX_DELIVERED_SHARE * max(block.kwh, floor)
                if measured and block.delivered_kwh >= cap:
                    return self._finish(
                        now,
                        f"leveret {block.delivered_kwh:.1f} kWh, "
                        f"over {MAX_DELIVERED_SHARE:.0f} gange de {block.kwh:.1f} "
                        "den blev lagt for",
                        code="cap",
                    )
                # En blok der er forlænget på lagerets mål, men hvor målet nu
                # ikke kendes: planlæggeren regner ikke manglen ud, når der
                # intet er at hente. Så er pumpens kWh det bedste vi har -
                # ellers er der ingen udgang tilbage, kun fristen.
                if (
                    block.extended
                    and target_met is None
                    and measured
                    and block.kwh - block.delivered_kwh <= DELIVERED_TOLERANCE_KWH
                ):
                    return self._finish(
                        now,
                        "leveret sin mængde — lagerets mål kendes ikke længere",
                        code="kwh_done",
                    )
            if chosen == "pillefyr" and not young and not self.block.manual:
                return self._finish(now, "pillefyret blev billigere", code="pellet")
            # Predbat har lagt om, så halvtimen vi står i, tager af
            # batteriet. Blokken blev lagt på strøm fra nettet, og en blok må
            # kun køre der - den regel gælder også efter start, ikke kun når
            # den lægges og forlænges.
            if (
                not self.block.manual
                and self._battery_now(plan, grid, plan_stamp, confirm_plans)
                and not young
            ):
                return self._finish(
                    now,
                    "Predbat tager nu strømmen af batteriet",
                    top_up=self._may_resume(self.block),
                    code="battery",
                )
            self._running = True
            # Den leverede mængde står med, for det er den der afgør
            # hvornår blokken er færdig - ikke minutterne alene.
            leveret = (
                ""
                if self.block.manual
                else f" ({self.block.delivered_kwh:.1f} leveret)"
            )
            self.rule = "manual" if self.block.manual else "running"
            self.note = (
                f"{self._label(self.block)} "
                f"{self.block.kwh:.1f} kWh{leveret}, "
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
            # ``_extend`` har allerede haft chancen. Kommer vi hertil med en
            # mængde der mangler, er det fristen der lukkede blokken - og så
            # skal det stå der, i stedet for et «kørt» der lyder som om alt
            # kom i lageret.
            ran = now - self.block.began
            measured = (
                ran > 0 and self.block.measured_seconds >= MIN_MEASURED_SHARE * ran
            )
            missing = self.block.kwh - self.block.delivered_kwh
            # Er det batteriet der stoppede blokken, skal det stå der - og
            # strækket er ikke klaret: der må lægges én blok mere, hvis der
            # kommer net-halvtimer før fristen. Kendes lagerets mål ikke, er
            # pumpens kWh målet; mangler der målt noget, er det det samme
            # stop. Her stod kun «målet ikke nået», og et ukendt mål endte
            # som «tiden løb ud» og brændte strækket (gennemgangen den 18.
            # oktober).
            fell_short = target_met is False or (
                target_met is None
                and measured
                and missing > DELIVERED_TOLERANCE_KWH
            )
            if (
                not self.block.manual
                and fell_short
                and now < self.block.deadline
                and self._grid_reach(plan, now, self.block.deadline, grid) <= now
            ):
                return self._finish(
                    now,
                    "strømmen kommer nu fra batteriet — "
                    + (
                        "lageret nåede ikke målet"
                        if target_met is False
                        else f"{missing:.1f} kWh nåede ikke i lageret"
                    ),
                    top_up=self._may_resume(self.block),
                    code="battery",
                )
            if not self.block.manual and target_met is False:
                # Ellers er det enten fristen, eller at der ikke kunne
                # forlænges af en anden grund - fx en ladehastighed der ikke
                # kendes. Noten må ikke sige «fristen», når den ikke er nået.
                if now < self.block.deadline:
                    return self._finish(
                        now, "tiden løb ud — lageret nåede ikke målet", code="time_out"
                    )
                return self._finish(
                    now, "fristen nået — lageret nåede ikke målet", code="deadline"
                )
            if not self.block.manual and measured and missing > DELIVERED_TOLERANCE_KWH:
                return self._finish(
                    now,
                    f"tiden løb ud — {missing:.1f} kWh nåede ikke i lageret",
                    code="time_out",
                )
            # Kun en blok der *målt* leverede sin mængde, åbner for en
            # efteropladning. Uden måling ved vi ikke om det var pumpen eller
            # regnestykket der kom til kort, og så gælder spærren som før.
            return self._finish(
                now,
                "kørt",
                code="ran",
                top_up=(
                    not self.block.manual
                    and not self.block.top_up
                    and measured
                    and missing <= DELIVERED_TOLERANCE_KWH
                ),
            )

        self._running = False

        # 3. En blok der venter, droppes kun af de samme to grunde som en der
        #    kører.
        if self.block is not None and (full or chosen == "pillefyr"):
            self.block = None
            self._forget_wish()
            self.rule = "dropped_full" if full else "dropped_pellet"
            self.note = "opladning droppet — " + (
                "lageret er fuldt" if full else "pillefyret vinder"
            )
            return False

        # 3b. En ventende blok der nu ligger på batteriet, droppes.
        #
        #     Normalt står en ventende blok ved magt, også når der ikke kan
        #     lægges en ny - se «Ingen plads» nedenfor. Den her er en
        #     undtagelse, og den er bevidst: siden 23. september må en blok
        #     kun ligge hvor strømmen kommer fra nettet. Har Predbat siden
        #     lagt om, så en af dens halvtimer nu tager af batteriet, er den
        #     ikke længere en blok vi må køre.
        #
        #     Den står *før* trin 4, og det er med vilje. Her stod den efter,
        #     og trin 4 vender om når planlæggeren ikke har en hensigt - så
        #     en blok lagt kl. 13 overlevede at Predbat flyttede dens
        #     halvtimer til batteriet, bare fordi planlæggeren samtidig sagde
        #     «gevinsten er for lille». Den startede og kørte 15 minutter på
        #     batteriets strøm. Fundet i gennemgangen den 18. oktober.
        #
        #     Og ønsket glemmes: en ny blok skal bekræftes forfra, ellers
        #     lægges den igen i næste minut, og noten skifter mellem «droppet»
        #     og «lader» i Predbats takt.
        if (
            plan is not None
            and self.block is not None
            and not self.block.manual
            and not self.block.running(now)
            and self._on_battery(self.block, plan, now)
        ):
            self.block = None
            self._forget_wish()
            self.rule = "dropped_battery"
            self.note = "opladning droppet — Predbat tager nu strømmen af batteriet"
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
            self._wanted_stamps = []
            if self.block is not None:
                self.rule = "waiting"
                self.note = (
                    f"venter — {self._label(self.block)} {self.block.kwh:.1f} kWh om "
                    f"{self.block.minutes_until(now):.0f} min"
                )
            else:
                self.rule = "no_plan"
                self.note = NO_PLAN
            return False

        dear_from, dear_until, need_until = self._dear_key(now, decision, window)
        covered = self.done_until is not None and dear_from < self.done_until
        # **Efteropladningen.** Én undtagelse fra spærren nedenfor, og den er
        # ikke den udgåede om et tømt lager: den gælder *før* strækket, ikke
        # inde i det, og kun når blokken leverede den mængde den blev lagt
        # for - målt - og planlæggeren alligevel stadig beder om mindst ét
        # minimumstræk.
        #
        # Så er det regnestykket der kom til kort, ikke pumpen. Den 30.
        # september kl. 13:00-13:41 leverede en blok 5,4 af 5,2 kWh til
        # varmt vand, men tankene endte på 49/50/50 og 48/33/31 - nul over
        # 55. UVR'en lader med setpunktet toppen + 2 grader, højst 58, og
        # bygger temperaturen op over hele tanken; ``energy_to_reach`` regnede
        # dengang lag for lag oppefra. Bagefter bad planen om 3,3 kWh mere, fristen
        # kl. 17 stod der stadig, og kl. 14-15 kom strømmen fra nettet - men
        # spærren sagde «allerede ladet op» resten af eftermiddagen.
        #
        # Én og ikke flere: en efteropladning åbner ikke for endnu en. Kan
        # lageret slet ikke nå 55, bliver det ved én ekstra blok og ikke ved
        # en kompressor der kører hele eftermiddagen efter et tal.
        top_up = covered and self.top_up_open
        if covered and not top_up:
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
            #
            # Efteropladningen ovenfor er noget andet - se dér.
            self.block = None
            self._forget_wish()
            self.rule = "covered"
            self.note = "allerede ladet op mod det her dyre stræk"
            return False

        # 4c. Blokken følger Predbats takt, ikke add-on'ens minut.
        if plan_stamp is not None:
            if self.block is not None and plan_stamp == self._placed_stamp:
                # Samme Predbat-plan som da den blev lagt: ingen nye priser at
                # flytte den på.
                self.rule = "waiting"
                self.note = (
                    f"venter — {self._label(self.block)} {self.block.kwh:.1f} kWh om "
                    f"{self.block.minutes_until(now):.0f} min"
                )
                return False
            if self.block is None:
                if plan_stamp not in self._wanted_stamps:
                    self._wanted_stamps = (self._wanted_stamps + [plan_stamp])[-8:]
                if len(self._wanted_stamps) < max(1, int(confirm_plans)):
                    self.rule = "waiting_predbat"
                    self.note = "afventer næste Predbat-beregning"
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
        # Samme søgning som planlæggeren - se ``block_window``. Reserven før
        # fristen, kun nethalvtimer, og en nu-halvtime der næsten er gået,
        # viger for en senere.
        found = block_window(
            plan,
            float(want),
            rate_kw,
            int(window),
            min_runtime_minutes,
            grid,
            (now - slot_start(now)) / 60,
        )
        if found is None:
            # Ingen plads er ikke det samme som «drop det der allerede er
            # lagt». En blok der venter, er lagt på priser vi har set efter;
            # at der ikke kan lægges en *ny* i det her minut, siger ingenting
            # om den. Før stod her ``self.block = None``, og så slettede en
            # forbigående trangt vindue en opladning der var klar.
            self.rule = "no_grid_minutes"
            self.note = (
                f"ingen {minutes:.0f} min fra nettet inden prisen stiger"
            )
            return False

        offset, _reach = found
        # ``offset`` tælles i hele halvtimer fra den halvtime vi *står i*,
        # ikke fra det her sekund. Uden gulvet gled en ventende bloks start
        # ét minut frem pr. cyklus og sprang 30 minutter tilbage ved hver
        # :00/:30 - så den stod aldrig stille længe nok til at kunne læses.
        starts = slot_start(now) + offset * 60
        # Og længden måles fra det seneste af de to. Starter blokken nu, kan
        # ``slot_start(now)`` ligge op til 29 minutter tilbage i tiden, og så
        # ville blokken blive tilsvarende for kort.
        ends = max(starts, now) + minutes * 60
        # Men den må ikke løbe ind i det dyre. Fristen tæller fra halvtimens
        # start, ligesom planens rækker: kl. 16:17 er en frist på 60 minutter
        # kl. 17:00, og en blok på 60 minutter der starter nu, sluttede før
        # kl. 17:17 - sytten minutter inde i den halvtime den skulle undgå.
        deadline = slot_start(now) + window * 60
        # Og den må ikke løbe ud af nettets halvtimer. ``cheapest_window``
        # tæller hele halvtimer fra halvtimens start, men en blok der starter
        # nu, måles fra nu: lagt kl. 04:45 med 22 minutter slutter den kl.
        # 05:07, syv minutter inde i en halvtime på batteriet.
        stop = min(deadline, self._grid_reach(plan, now, deadline, grid, starts))
        if ends > stop:
            ends = stop
            if ends <= max(starts, now):
                self.rule = "no_room_minutes"
                self.note = f"ingen plads til {minutes:.0f} min inden prisen stiger"
                return False
            minutes = (ends - max(starts, now)) / 60
            want = min(float(want), rate_kw * minutes / 60)
            # Kortere end mindstekørselstiden er ikke en opladning, det er en
            # start. Lagt kl. 04:52 mod en nethalvtime der slutter kl. 05:00,
            # blev det til otte minutter og 1,9 kWh.
            if minutes < min_runtime_minutes:
                self.rule = "below_min_runtime"
                self.note = (
                    f"kun {minutes:.0f} min fra nettet inden prisen stiger "
                    "— under mindstekørselstiden"
                )
                return False
        self.block = Block(
            dear_from,
            dear_until,
            starts,
            ends,
            float(want),
            deadline=deadline,
            began_at=max(starts, now),
            top_up=top_up,
            need_until=need_until,
        )
        # En ny blok har ikke leveret noget, og den forrige cyklus hører til
        # den gamle. Uden nulstillingen ville det første mellemrum blive
        # talt med i en blok der ikke var begyndt.
        self._last_seen = now
        self._placed_stamp = plan_stamp
        if self.block.running(now):
            self._running = True
            self.rule = "running"
            self.note = f"{self._label(self.block)} {want:.1f} kWh nu, {minutes:.0f} min"
            return True

        self.rule = "waiting"
        self.note = (
            f"{self._label(self.block)} {want:.1f} kWh om "
            f"{self.block.minutes_until(now):.0f} min "
            f"i {minutes:.0f} min"
        )
        return False

    def _dear_key(
        self, now: float, decision: Any, window: int
    ) -> tuple[float, float, float]:
        """Det dyre stræk som to absolutte tidspunkter.

        Planlæggeren giver strækket som minutter frem; her bliver det til
        vægurstid, gulvet til halvtimen, så det samme stræk ser ens ud
        hvert minut.

        Enden er ``dear_ends_in`` når planlæggeren giver den, og ellers
        strækkets egen. De to er ens for et ægte stræk. For et stræk fra
        tilbagefaldet slutter ``dear_ends_in`` med toppen, fordi det relative
        spænd kan række til horisontens kant - og så låste én blok hele døgnet.

        Har planlæggeren intet stræk at give - der er ingen timer hvor
        pumpen taber, og heller ingen der er dyrere end nu - falder vi tilbage
        på den dyreste halvtime, som før. Netop dér er det ufarligt: den
        dyreste halvtime vandrer kun når flere halvtimer er lige dyre, og det
        sker kun når de rammer pillefyrets loft. Gælder det, findes der et
        stræk, og så er vi ikke her.
        """
        # Minutterne tæller fra halvtimens start, som planens rækker.
        base = slot_start(now)
        starts = getattr(decision, "dear_starts_in", None)
        span = getattr(decision, "dear_span_minutes", None)
        if starts is not None and span is not None and _finite(starts) and _finite(span):
            first, length = float(starts), float(span)
            if length > 0:
                last = first + length
                ends = getattr(decision, "dear_ends_in", None)
                if _finite(ends) and first < ends:
                    last = float(ends)
                return (
                    slot_start(base + first * 60),
                    slot_start(base + last * 60),
                    # Mængden er regnet over hele spændet, også når spærren
                    # slutter med toppen - se ``Block.need_until``.
                    slot_start(base + (first + length) * 60),
                )
        top = slot_start(base + (decision.window_minutes or window) * 60)
        # Uden et stræk er mængden ikke regnet over noget spænd, og så kan
        # målet ikke måles: ``need_until`` på starten giver et tomt spænd.
        return top, top + SLOT_SECONDS, top

    def _forget_wish(self) -> None:
        self._wanted_stamps = []
        self._placed_stamp = None
        self._battery_stamps = []

    @staticmethod
    def _may_resume(block: Block) -> bool:
        """Må et batteristop lægge én blok mere mod samme stræk?

        Samme regel som efteropladningen: ét dyrt stræk giver én opladning
        og højst én til. Skifter Predbat plan tit, bliver det ved to blokke -
        ikke en ny kort kørsel for hvert skift.
        """
        return not block.manual and not block.top_up

    def _battery_now(
        self, plan: Any, grid: Any, plan_stamp: str | None, confirm_plans: int
    ) -> bool:
        """Har halvtimen vi står i, holdt sig på batteriet længe nok?

        Uden stempel svarer ét minut, som før stemplerne fandtes.
        """
        from .prices import BATTERY

        price = plan.marginal(0, grid=grid) if plan is not None else None
        if price is None or price.source != BATTERY:
            self._battery_stamps = []
            return False
        if plan_stamp is None:
            return True
        if plan_stamp not in self._battery_stamps:
            self._battery_stamps = (self._battery_stamps + [plan_stamp])[-8:]
        return len(self._battery_stamps) >= max(1, int(confirm_plans))

    @staticmethod
    def _on_battery(block: Block, plan: Any, now: float) -> bool:
        """Tager en af blokkens halvtimer nu strømmen af batteriet?"""
        from .prices import BATTERY

        base = slot_start(now)
        first = int((slot_start(block.starts_at) - base) // SLOT_SECONDS)
        last = int(math.ceil((block.ends_at - base) / SLOT_SECONDS))
        for index in range(max(0, first), max(0, last)):
            price = plan.marginal(index * 30)
            if price is not None and price.source == BATTERY:
                return True
        return False

    @staticmethod
    def _label(block: Block) -> str:
        if block.manual:
            return "manuel opladning"
        return "efteroplader" if block.top_up else "lader"

    def _finish(
        self, now: float, why: str, top_up: bool = False, code: str = "ran"
    ) -> bool:
        if self.block is not None:
            # Hvorfor den seneste opladning sluttede - til regelkortet. Det
            # var netop det der var svært at se den 18. oktober.
            self.last_end = {"at": round(now, 1), "code": code, "why": why}
        if self.block is not None and not self.block.manual:
            self.done_until = self.block.dear_until
            # Hver automatisk blok der slutter, afgør det på ny. Kun to veje
            # åbner for én blok mere mod samme stræk: en blok der leverede
            # sin mængde, målt, og en blok som batteriet stoppede - se
            # ``_may_resume``. Stoppet med knappen, fuldt lager, pillefyret,
            # tiden der løb ud: ingen af dem.
            self.top_up_open = top_up
        self.block = None
        self._running = False
        self._full_since = None
        self._forget_wish()
        self.rule = "ended"
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
            "top_up_open": self.top_up_open,
            # Stemplerne overlever en genstart. Ellers ville hver opdatering
            # nulstille bekræftelsen - og et ønske der har holdt på én plan,
            # skal ikke begynde forfra fordi add-on'en blev genstartet.
            "wanted_stamps": list(self._wanted_stamps),
            "placed_stamp": self._placed_stamp,
            "battery_stamps": list(self._battery_stamps),
            "last_end": self.last_end,
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
        plan.top_up_open = raw.get("top_up_open") is True
        stamps = raw.get("wanted_stamps")
        if isinstance(stamps, list):
            plan._wanted_stamps = [s for s in stamps if isinstance(s, str)][-8:]
        placed = raw.get("placed_stamp")
        plan._placed_stamp = placed if isinstance(placed, str) else None
        battery = raw.get("battery_stamps")
        if isinstance(battery, list):
            plan._battery_stamps = [s for s in battery if isinstance(s, str)][-8:]
        end = raw.get("last_end")
        if (
            isinstance(end, dict)
            and _finite(end.get("at"))
            and isinstance(end.get("code"), str)
            and isinstance(end.get("why"), str)
        ):
            plan.last_end = {"at": float(end["at"]), "code": end["code"], "why": end["why"]}
        if plan.block is not None:
            plan.rule = "waiting"
            plan.note = "genoptager planlagt opladning"
        return plan
