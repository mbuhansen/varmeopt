"""Vagten: må beslutningen overhovedet handles på lige nu?

Planlæggeren svarer altid. Det er dens opgave. Men et svar er ikke det samme
som et svar man tør styre efter, og forskellen er det her modul.

Vagten siger aldrig hvad der skal gøres — kun om nogen bør gøre det. Siger den
nej, står beslutningen der stadig, og Node-RED bruger sin egen logik. Det er
med vilje: en styring der falder tilbage til noget der virker, er bedre end en
der insisterer på at have ret.

**Om at give slip.** Add-on'en skriver ikke til UVR'en. Den udstiller sin
beslutning og et flag der siger om den skal følges, og Node-RED gør resten.
Dermed er der kun ét sted der styrer, og det sted kan altid sige nej til os.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    """Hvad Node-RED skal gøre — og om den overhovedet skal lytte."""

    source: str | None
    acting: bool
    reason: str

    @property
    def note(self) -> str:
        return f"{self.source or '—'} ({self.reason})"


class Guard:
    """Holder styr på om og hvornår vi tør styre."""

    def __init__(
        self,
        enabled: bool = False,
        min_dwell_minutes: float = 15.0,
        warmup_minutes: float = 5.0,
        confirm_minutes: float = 3.0,
    ) -> None:
        self.enabled = enabled
        # Anlægget må ikke vippe frem og tilbage. Hysteresen i planlæggeren
        # daemper prisstøj; det her sætter en bund under hvor tit kilden
        # overhovedet får lov at skifte.
        self.min_dwell_minutes = min_dwell_minutes
        # Efter en opstart er tabellerne lige indlæst og der er kørt én
        # cyklus. At kaste anlægget om i det øjeblik ville være at handle på
        # den mindst oplyste tilstand vi nogensinde har.
        self.warmup_minutes = warmup_minutes
        # Hvor længe en ny kilde skal holde før den overhovedet tæller.
        #
        # Det er ikke det samme som hviletiden, og den ene kan ikke gøre den
        # andens arbejde. Hviletiden alene gør et ét-minuts udsving
        # *værre*: er den for længst udløbet - og det er den altid efter en
        # rolig time - binder vagten sig til fejlen i samme øjeblik den
        # opstår og holder den et helt kvarter. Et minuts støj bliver til
        # femten. Bekræftelsen afviser udsvinget; hviletiden bærer de
        # skift der er ægte.
        self.confirm_minutes = confirm_minutes

        self.started: float | None = None
        self.committed: str | None = None
        self.committed_at: float | None = None
        # Den kilde der banker på, og hvornår den begyndte at banke.
        self.pending: str | None = None
        self.pending_since: float | None = None

    # ------------------------------------------------------------------ tid

    def start(self, now: float) -> None:
        self.started = now

    def _minutes_since(self, then: float | None, now: float) -> float | None:
        return None if then is None else (now - then) / 60

    # -------------------------------------------------------------- vurdering

    def check(
        self, decision: Any, lookup: Any, plan: Any, now: float
    ) -> Command:
        """Afgør hvilken kilde der står ved magt — og om den må følges.

        ``now`` er **vægurstid** i sekunder, ikke et monotont ur. Det er en
        bevidst afvejning: et monotont ur nulstilles ved hver genstart, og så
        ville opholdstiden kunne omgås ved at genstarte. Prisen er at et
        urspring kan forlænge eller forkorte ét ophold, hvilket er skade nok
        til at kunne leve med.

        **Bindingen regnes også når styringen er slået fra.** Det er nyt.
        Før returnerede metoden med det samme, og så var
        ``sensor.varmeopt_beslutning`` planlæggerens rå svar hvert minut —
        den 10. september skiftede den fjorten gange på seks timer. Entiteten
        er det Node-RED hænger en ``server-state-changed`` på, så den skal
        være rolig uanset om det er os eller Node-RED der styrer.

        **Rækkefølgen er ikke til forhandling.** De tre porte foran
        ``_hold`` er der hver for sig:

        * Uden en beslutning er der ingenting at binde sig til.
        * Uden en COP er varmeprisen et gæt, og ``source_now`` svarer
          "varmepumpe" som standard. Bandt vi os til det, ville en forældet
          Predbat-plan kunne låse anlægget på et prisløst gæt et kvarter.
        * Under opvarmningen er tilstanden den mindst oplyste vi har — se
          ``warmup_minutes``. Det gælder bindingen lige så meget som flaget.
        """
        if self.started is None:
            self.started = now

        if decision is None or decision.source is None:
            return Command(None, False, "ingen beslutning")

        # Uden en COP er varmeprisen et gæt, og så er valget det også.
        if lookup is None or decision.heat_price is None:
            return Command(decision.source, False, self._off("ingen COP — styrer ikke"))

        warm = self._minutes_since(self.started, now) or 0.0
        if warm < self.warmup_minutes:
            left = self.warmup_minutes - warm
            return Command(decision.source, False, self._off(f"varmer op, {left:.0f} min endnu"))

        held, note = self._hold(decision.source, now)

        if not self.enabled:
            return Command(held, False, f"styring slået fra — {note}")

        # Uden en plan kan vi stadig vælge kilde — planlæggeren er bygget til
        # det. Men det skal siges, så det ikke ligner mere end det er.
        planless = "" if plan is not None and len(plan) else " (uden plan)"
        return Command(held, True, f"{note}{planless}")

    def _off(self, reason: str) -> str:
        """Sæt "styring slået fra" foran, når den er det.

        Portene foran ``_hold`` svarer før vi når at se på ``enabled``, og
        uden det her ville ``styring_grund`` sige "varmer op" til en bruger
        der aldrig har slået styringen til.
        """
        return reason if self.enabled else f"styring slået fra, {reason}"

    def _hold(self, candidate: str, now: float) -> tuple[str, str]:
        """Hvilken kilde står ved magt, og hvorfor.

        Bekræftelsen ligger foran hviletiden, og det er den rækkefølge der
        gør arbejdet: et udsving på én cyklus når aldrig frem til
        hviletiden og nulstiller den derfor heller ikke.
        """
        if self.committed is None:
            return self._commit(candidate, now), "overtager"

        if candidate == self.committed:
            # Den der bankede på, gav op. Så skal næste kandidat begynde
            # forfra i stedet for at arve en ventetid der ikke var dens.
            self.pending = None
            self.pending_since = None
            return self.committed, "uændret"

        if candidate != self.pending:
            self.pending = candidate
            self.pending_since = now

        waited = self._minutes_since(self.pending_since, now) or 0.0
        if waited < self.confirm_minutes:
            return self.committed, f"afventer bekræftelse af {candidate}"

        held = self._minutes_since(self.committed_at, now) or 0.0
        if held < self.min_dwell_minutes:
            left = self.min_dwell_minutes - held
            return self.committed, f"holder {self.committed} i {left:.0f} min endnu"

        was = self.committed
        return self._commit(candidate, now), f"skifter fra {was}"

    def _commit(self, source: str, now: float) -> str:
        self.committed = source
        self.committed_at = now
        self.pending = None
        self.pending_since = None
        return source

    # ------------------------------------------------------------------ lager

    def to_raw(self) -> dict[str, Any]:
        """Bindingen, så opholdstiden overlever en genstart.

        ``started`` gemmes med vilje *ikke*: opvarmningen skal gælde efter
        hver opstart, for tilstanden er den mindst oplyste vi har lige der.
        Men opholdet skal fortsætte hvor det slap — ellers bliver 15 minutter
        til 5, og med en modulerende varmepumpe er det kortcykling.
        """
        return {
            "committed": self.committed,
            "committed_at": self.committed_at,
            # Bekræftelsen gemmes af samme grund som opholdet: en genstart
            # midt i et udsving må ikke nulstille ventetiden, for så er
            # bekræftelsen en genstart værd.
            "pending": self.pending,
            "pending_since": self.pending_since,
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        source = raw.get("committed")
        self.committed = source if source in ("varmepumpe", "pillefyr") else None
        self.committed_at = self._seconds(raw.get("committed_at"))
        if self.committed is None:
            self.committed_at = None

        pending = raw.get("pending")
        self.pending = pending if pending in ("varmepumpe", "pillefyr") else None
        self.pending_since = self._seconds(raw.get("pending_since"))
        # En ventende kilde der er blevet den bundne, er ikke en ventende
        # kilde - og en uden tidspunkt kan ikke tælles.
        if self.pending is None or self.pending_since is None or self.pending == self.committed:
            self.pending = None
            self.pending_since = None

    @staticmethod
    def _seconds(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def release(self) -> None:
        """Giv slip, så næste overtagelse begynder forfra."""
        self.committed = None
        self.committed_at = None
        self.pending = None
        self.pending_since = None
