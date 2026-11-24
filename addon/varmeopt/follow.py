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
  opladning, men kompressoren kører alligevel. Kun en bemærkning: det kan
  være spaen eller varmt vand, hvis Node-RED lader pumpen tage dem uanset
  kilden. Optræder den hver dag på samme tid, er det sådan flowet er bygget;
  optræder den tilfældigt, er det værd at se på.

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

# Længste spring mellem to cyklusser der tæller som sammenhængende. Har
# add-on'en været nede, ved vi ikke hvad der skete imens.
MAX_GAP_SECONDS = 300.0

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
        return event


class FollowWatch:
    """Holder flaget op mod kompressorens strøm, cyklus for cyklus."""

    def __init__(self, grace_minutes: float = GRACE_MINUTES) -> None:
        self.grace_minutes = grace_minutes
        self.open: dict[str, Event] = {}
        self.events: list[Event] = []
        # Minutter fra flaget tændte til kompressoren gik i gang.
        self.delays: list[float] = []
        self._last: float | None = None
        # Starten på en ordre der endnu ikke er nået frem. Den er ikke en
        # hændelse før fristen er gået, men ventetiden måles fra den.
        self._ordered_since: float | None = None

    # ------------------------------------------------------------- iagttag

    def observe(
        self,
        now: float,
        *,
        ordered: bool,
        acting: bool,
        running: bool | None,
        source: str | None,
    ) -> list[str]:
        """Én cyklus. Returnerer de linjer der skal logges som advarsler nu.

        ``ordered`` er ladeflaget som det blev udgivet, ``acting`` om
        ``styrer`` stod sandt, ``running`` om kompressoren trækker strøm
        (``None``: ved ikke), ``source`` den kilde vagten står ved.
        """
        gap = now - self._last if self._last is not None else 0.0
        self._last = now
        if gap > MAX_GAP_SECONDS:
            # Vi har været væk. Det der var åbent, kan vi ikke sige noget
            # om, og en ventetid hen over et hul er ikke en ventetid.
            self._close_all("afbrudt: add-on'en var nede")
            self._ordered_since = None

        if running is None:
            return []

        out: list[str] = []
        charge = ordered and acting

        # Ladeflaget: startede pumpen?
        if charge and not running:
            if self._ordered_since is None:
                self._ordered_since = now
            event = self.open.get(NOT_STARTED)
            if event is None:
                event = Event(NOT_STARTED, self._ordered_since, now)
                self.open[NOT_STARTED] = event
            event.seen_until = now
            if not event.flagged and event.minutes >= self.grace_minutes:
                event.flagged = True
                out.append(
                    f"lad op har stået tændt i {event.minutes:.0f} min uden at "
                    "varmepumpen kører - se Node-RED og UVR'en"
                )
        else:
            if charge and running and self._ordered_since is not None:
                delay = (now - self._ordered_since) / 60
                self.delays = (self.delays + [round(delay, 1)])[-MAX_DELAYS:]
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
            if not event.flagged and event.minutes >= self.grace_minutes:
                event.flagged = True
        else:
            event = self.open.pop(AGAINST_PELLET, None)
            if event is not None and event.flagged:
                event.outcome = "pumpen stoppede" if not running else "kilden skiftede"
                event.seen_until = now
                self._keep(event)

        return out

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
                f"Lad op er tændt og styrer, men varmepumpen har ikke kørt "
                f"i {event.minutes:.0f} min. Ordren når ikke frem - se "
                "Node-RED-flowet og UVR'en.",
            ))
        event = self.open.get(AGAINST_PELLET)
        if event is not None and event.flagged:
            out.append((
                "bemærkning",
                f"Kilden er pillefyr, men varmepumpen har kørt i "
                f"{event.minutes:.0f} min uden en opladning. Det kan være "
                "spa eller varmt vand.",
            ))
        return out

    @property
    def typical_delay(self) -> float | None:
        """Medianen af de målte startforsinkelser, i minutter."""
        if not self.delays:
            return None
        ordered = sorted(self.delays)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    @property
    def note(self) -> str:
        active = self.active()
        if active:
            return active[0][1]
        delay = self.typical_delay
        if delay is None:
            return "ingen opladninger set endnu"
        return (
            f"ordren følges - pumpen starter typisk {delay:.0f} min efter "
            f"lad op ({len(self.delays)} starter)"
        )

    # ---------------------------------------------------------------- disk

    def to_raw(self) -> dict[str, Any]:
        return {
            "events": [e.to_raw() for e in self.events],
            "delays": self.delays,
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
        delays = raw.get("delays")
        if isinstance(delays, list):
            watch.delays = [float(d) for d in delays if _finite(d) and d >= 0][-MAX_DELAYS:]
        return watch
