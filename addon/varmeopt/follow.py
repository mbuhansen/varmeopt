"""Følger anlægget ordren? Ikke om ordren var rigtig - kun om den blev udført.

Varmeopt skriver et flag, og så er den ude af billedet. Node-RED læser
flaget, skriver til UVR'en over CoE, UVR'en kalder på varmepumpen, og pumpen
bestemmer selv hvornår kompressoren går i gang. Hvert led kan svigte uden at
nogen af de andre opdager det: et Node-RED-flow der er gået i stå, en regel i
UVR'en der overstyrer, en kompressor der er låst af sin egen anti-pendling.
Den 15. september nåede «lad op» helt frem til UVR'en, men pumpen startede
ikke - og det kunne kun ses ved at sammenholde to grafer i hånden.

Det her holder øje med det ene sted hvor ordren er entydig: **ladeflaget**.
Kilden er det ikke. «Varmepumpe» betyder at pumpen skal tage varmen *når
der kaldes på den*, og står lageret varmt, kalder ingen. Men «lad op» med
``styrer`` sand betyder at kompressoren skal køre nu.

Det er ikke skyggedriften om igen. Den målte om Node-RED var enig, og
uenighed siger ikke hvem der har ret. Det her måler om en ordre blev
udført, og det har et svar.

**To slags hændelser:**

* ``startede_ikke`` - flaget er tændt og styrer, men kompressoren trækker
  ikke strøm. En advarsel når det har stået på i ``grace_minutes``. Starter
  pumpen inden, er det bare en start, og ventetiden gemmes - så det kan ses
  hvor lang tid kæden faktisk tager, og fristen kan sættes efter det.
* ``kører_mod_pille`` - kilden er pillefyr og styrer, der er ingen
  opladning, men kompressoren kører alligevel. UVR'en lader med vilje
  pumpen køre op til 20 minutter efter et skift til pillefyr, mens fyret
  varmer op, så en varmtvandsproduktion ikke falder. Det er
  ``PELLET_OVERLAP_MINUTES``, og først når pumpen har kørt længere end det
  plus en margin, er det en bemærkning. Kun en bemærkning: det er ikke
  sikkert det er en fejl, men det er værd at se på.

Kendes pumpens effekt ikke i en cyklus, dømmes der ikke: tavshed er ikke et
bevis for at den står stille.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any

# Så længe må kæden flag → Node-RED → UVR → kompressor tage, før det er en
# fejl. NIBE-pumpen har sin egen startforsinkelse efter et stop, og den skal
# kunne nå at løbe ud.
GRACE_MINUTES = 20.0

# Så længe lader UVR'en varmepumpen køre videre efter et skift til
# pillefyr - fyrets opvarmningstid. En gammel funktion brugeren har beholdt,
# så varmt vand der er i gang ikke falder. Plus en margin, så en pumpe der
# slutter præcis på minuttet ikke bliver til en bemærkning.
PELLET_OVERLAP_MINUTES = 20.0
PELLET_MARGIN_MINUTES = 10.0

# Længste spring mellem to cyklusser der tæller som sammenhængende. Har
# add-on'en været nede, ved vi ikke hvad der skete imens.
MAX_GAP_SECONDS = 300.0

# Fristen følger de målte starter, når der er nok af dem: den næstlængste af
# de seneste plus en margin, inden for et loft og en bund. Starter pumpen
# altid inden for 6 minutter, er 20 minutter for længe at vente på en fejl.
# Den næstlængste og ikke den længste: én langsom start skal ikke flytte
# fristen alene.
LEARN_AFTER_STARTS = 5
LEARN_MARGIN_MINUTES = 10.0
LEARN_MIN_MINUTES, LEARN_MAX_MINUTES = 15.0, 45.0
LEARN_FROM_LAST = 10

# Så mange afsluttede hændelser og startforsinkelser huskes.
MAX_EVENTS = 30
MAX_DELAYS = 20

NOT_STARTED = "startede_ikke"
AGAINST_PELLET = "kører_mod_pille"

PELLET = "pillefyr"


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass
class Event:
    """Én hændelse, åben eller afsluttet."""

    kind: str
    since: float
    seen_until: float
    # Sat når fristen er nået, så advarslen kun logges én gang.
    flagged: bool = False
    # Hvordan den sluttede: «pumpen startede», «ordren faldt bort» …
    outcome: str | None = None
    # Det lave ladeflag (``lad_op_lav``) og ikke det høje. Teksten skal nævne
    # det flag Node-RED faktisk skal følge.
    low: bool = False

    @property
    def flag(self) -> str:
        return "lad op lav" if self.low else "lad op"

    @property
    def minutes(self) -> float:
        return (self.seen_until - self.since) / 60

    def to_raw(self) -> dict[str, Any]:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_raw(cls, raw: Any) -> Event | None:
        if not isinstance(raw, dict) or not _finite(raw.get("since")):
            return None
        known = {f.name for f in fields(cls)}
        try:
            event = cls(**{k: v for k, v in raw.items() if k in known})
        except TypeError:
            return None
        if not _finite(event.seen_until) or event.kind not in (NOT_STARTED, AGAINST_PELLET):
            return None
        event.low = event.low is True
        return event


class FollowWatch:
    """Holder flaget op mod kompressorens strøm, cyklus for cyklus."""

    def __init__(self, grace_minutes: float = GRACE_MINUTES) -> None:
        self.grace_minutes = grace_minutes
        self.open: dict[str, Event] = {}
        self.events: list[Event] = []
        # Minutter fra flaget tændte til kompressoren gik i gang - for hvert
        # flag for sig. UVR'ens «lad til nederste føler» er en anden vej end
        # 56-graders-opladningen, og de to må ikke blandes i én median.
        self.delays: list[float] = []
        self.delays_low: list[float] = []
        self._last: float | None = None
        # Starten på en ordre der endnu ikke er nået frem. Den er ikke en
        # hændelse før fristen er gået, men ventetiden måles fra den.
        self._ordered_since: float | None = None
        # Er ordren afgjort? Sat når pumpen er set køre under den, eller når
        # vi har mistet overblikket. Bagefter dømmes ordren ikke mere: en
        # pumpe der stopper midt i en opladning (termostat, pressostat,
        # anti-pendling), har ikke «ikke startet», og genstarten er ikke en
        # ventetid (gennemgangen den 24. november).
        self._settled = False
        # Hvilket flag den igangværende ordre gælder.
        self._order_low: bool | None = None
        # Hvornår pumpens effekt holdt op med at kunne måles.
        self._unknown_since: float | None = None

    # ------------------------------------------------------------- iagttag

    def observe(
        self,
        now: float,
        *,
        ordered: bool,
        acting: bool,
        running: bool | None,
        source: str | None,
        low: bool = False,
    ) -> list[str]:
        """Én cyklus. Returnerer de linjer der skal logges som advarsler nu.

        ``ordered`` er ladeflaget som det blev udgivet, ``acting`` om
        ``styrer`` stod sandt, ``running`` om kompressoren trækker strøm
        (``None``: ved ikke), ``source`` den kilde vagten står ved, ``low``
        om ordren er det lave flag.
        """
        gap = now - self._last if self._last is not None else 0.0
        self._last = now
        if gap > MAX_GAP_SECONDS:
            # Vi har været væk. Det der var åbent, kan vi ikke sige noget
            # om, og en ventetid hen over et hul er ikke en ventetid.
            self._close_all("afbrudt: add-on'en var nede")
            self._ordered_since = None
            self._settled = self._order_low is not None

        if running is None:
            # Et minut uden måling er ingenting. Men et langt stræk er et hul
            # som ethvert andet: det åbne kan vi ikke sige noget om, og en
            # ventetid hen over det er ikke en ventetid. Her stod vagten bare
            # stille, og to timer uden effektføler blev til en ventetid på 121
            # minutter.
            if self._unknown_since is None:
                self._unknown_since = now
            elif now - self._unknown_since > MAX_GAP_SECONDS:
                self._close_all("afbrudt: varmepumpens effekt kunne ikke måles")
                self._ordered_since = None
                self._settled = self._order_low is not None
            return []
        self._unknown_since = None

        out: list[str] = []
        charge = ordered and acting

        # En ny ordre - eller den samme ordre skiftet til det andet flag - er
        # en ny ordre og dømmes forfra.
        if not charge:
            self._order_low, self._settled = None, False
        elif self._order_low is not None and self._order_low != low:
            self._close_not_started(now, "ordren skiftede flag")
            self._ordered_since, self._settled = None, False
        if charge:
            self._order_low = low

        # Ladeflaget: startede pumpen?
        if charge and not running and not self._settled:
            if self._ordered_since is None:
                self._ordered_since = now
            event = self.open.get(NOT_STARTED)
            if event is None:
                event = Event(NOT_STARTED, self._ordered_since, now, low=low)
                self.open[NOT_STARTED] = event
            event.seen_until = now
            if not event.flagged and event.minutes >= self.grace(low):
                event.flagged = True
                out.append(
                    f"{event.flag} har stået tændt i {event.minutes:.0f} min uden at "
                    "varmepumpen kører - se Node-RED og UVR'en"
                )
        else:
            if charge and running and not self._settled:
                # En start der kom efter en advarsel, var en fejl der blev
                # rettet - Node-RED der stod, en UVR der blev genstartet. Den
                # er ikke en ventetid, og den må ikke hæve fristen.
                flagged = (self.open.get(NOT_STARTED) or Event("", 0, 0)).flagged
                if self._ordered_since is not None and not flagged:
                    delay = round((now - self._ordered_since) / 60, 1)
                    if low:
                        self.delays_low = (self.delays_low + [delay])[-MAX_DELAYS:]
                    else:
                        self.delays = (self.delays + [delay])[-MAX_DELAYS:]
                self._settled = True
            event = self.open.pop(NOT_STARTED, None)
            if event is not None and event.flagged:
                event.outcome = (
                    f"pumpen startede efter {(now - event.since) / 60:.0f} min"
                    if charge and running
                    else "ordren faldt bort før pumpen startede"
                )
                event.seen_until = now
                self._keep(event)
            if not charge or running:
                self._ordered_since = None

        # Kilden er pillefyret, ingen opladning - kører pumpen alligevel?
        against = acting and source == PELLET and not ordered and running
        if against:
            event = self.open.get(AGAINST_PELLET)
            if event is None:
                event = Event(AGAINST_PELLET, now, now)
                self.open[AGAINST_PELLET] = event
            event.seen_until = now
            if not event.flagged and event.minutes >= (
                PELLET_OVERLAP_MINUTES + PELLET_MARGIN_MINUTES
            ):
                event.flagged = True
        else:
            event = self.open.pop(AGAINST_PELLET, None)
            if event is not None and event.flagged:
                event.outcome = "pumpen stoppede" if not running else "kilden skiftede"
                event.seen_until = now
                self._keep(event)

        return out

    def grace(self, low: bool = False) -> float:
        """Fristen for flaget: lært af de målte starter, eller den faste."""
        delays = (self.delays_low if low else self.delays)[-LEARN_FROM_LAST:]
        if len(delays) < LEARN_AFTER_STARTS:
            return self.grace_minutes
        second = sorted(delays)[-2]
        return min(
            LEARN_MAX_MINUTES,
            max(LEARN_MIN_MINUTES, second + LEARN_MARGIN_MINUTES),
        )

    def _close_not_started(self, now: float, outcome: str) -> None:
        event = self.open.pop(NOT_STARTED, None)
        if event is not None and event.flagged:
            event.outcome = outcome
            event.seen_until = now
            self._keep(event)

    def _close_all(self, outcome: str) -> None:
        for event in self.open.values():
            if event.flagged:
                event.outcome = outcome
                self._keep(event)
        self.open = {}

    def _keep(self, event: Event) -> None:
        self.events = (self.events + [event])[-MAX_EVENTS:]

    # ---------------------------------------------------------------- vises

    def active(self) -> list[tuple[str, str]]:
        """De åbne hændelser der har nået fristen: (niveau, tekst)."""
        out: list[tuple[str, str]] = []
        event = self.open.get(NOT_STARTED)
        if event is not None and event.flagged:
            out.append((
                "advarsel",
                f"{event.flag.capitalize()} er tændt og styrer, men varmepumpen "
                f"har ikke kørt i {event.minutes:.0f} min. Ordren når ikke frem - "
                "se Node-RED-flowet og UVR'en.",
            ))
        event = self.open.get(AGAINST_PELLET)
        if event is not None and event.flagged:
            out.append((
                "bemærkning",
                f"Kilden er pillefyr, men varmepumpen har kørt i "
                f"{event.minutes:.0f} min uden en opladning - længere end "
                f"UVR'ens {PELLET_OVERLAP_MINUTES:.0f} min mens fyret varmer op.",
            ))
        return out

    @property
    def typical_delay(self) -> float | None:
        """Medianen af de målte startforsinkelser på det høje flag, i minutter."""
        return _median(self.delays)

    @property
    def typical_delay_low(self) -> float | None:
        """Det samme for det lave flag."""
        return _median(self.delays_low)

    @property
    def note(self) -> str:
        active = self.active()
        if active:
            return active[0][1]
        parts = []
        for name, delay, count in (
            ("lad op", self.typical_delay, len(self.delays)),
            ("lad op lav", self.typical_delay_low, len(self.delays_low)),
        ):
            if delay is not None:
                low = name.endswith("lav")
                learned = count >= LEARN_AFTER_STARTS
                parts.append(
                    f"pumpen starter typisk {delay:.0f} min efter {name} ({count} starter, "
                    f"frist {self.grace(low):.0f} min{' lært' if learned else ''})"
                )
        if not parts:
            return "ingen opladninger set endnu"
        return "ordren følges - " + "; ".join(parts)

    # ---------------------------------------------------------------- disk

    def to_raw(self) -> dict[str, Any]:
        return {
            "events": [e.to_raw() for e in self.events],
            "delays": self.delays,
            "delays_low": self.delays_low,
        }

    @classmethod
    def from_raw(cls, raw: Any, grace_minutes: float = GRACE_MINUTES) -> FollowWatch:
        watch = cls(grace_minutes)
        if not isinstance(raw, dict):
            return watch
        events = raw.get("events")
        if isinstance(events, list):
            watch.events = [e for e in (Event.from_raw(r) for r in events) if e is not None][
                -MAX_EVENTS:
            ]
        for key in ("delays", "delays_low"):
            delays = raw.get(key)
            if isinstance(delays, list):
                setattr(
                    watch,
                    key,
                    [float(d) for d in delays if _finite(d) and d >= 0][-MAX_DELAYS:],
                )
        return watch


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
