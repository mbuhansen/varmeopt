"""De sidste loglinjer, holdt i hukommelsen så de kan hentes med et klik.

Home Assistant viser add-on-loggen, men den skal kopieres ud i hånden, og den
holder op med at være tilgængelig når containeren genstartes. En fejlsøgning
begynder næsten altid med «hvad skete der lige før», og det svar skal ligge
sammen med resten af tilstanden — ikke et andet sted i en anden fane.

Ringen er lille med vilje. Formålet er de sidste par timers cyklusser, ikke et
arkiv: et arkiv hører til i Home Assistants egen log.
"""

from __future__ import annotations

import logging
from collections import deque

DEFAULT_LINES = 600


class Journal(logging.Handler):
    """Log-håndtag der husker de sidste linjer i stedet for at skrive dem."""

    def __init__(self, capacity: int = DEFAULT_LINES) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        # En log-handler må aldrig vælte det den logger for. Fejler
        # formateringen, er den linje tabt, og det er hele skaden.
        try:
            self.lines.append(self.format(record))
        except Exception:  # pragma: no cover - defensivt
            self.handleError(record)

    def dump(self) -> list[str]:
        return list(self.lines)

    @property
    def count(self) -> int:
        return len(self.lines)


def install(capacity: int = DEFAULT_LINES) -> Journal:
    """Hæng en journal på rod-loggeren, med samme format som konsollen.

    **Skal kaldes efter ``logging.basicConfig``.** Rod-loggerens niveau
    filtrerer records inden nogen håndtag ser dem, og før ``basicConfig`` står
    det på WARNING — så en journal installeret for tidligt ville kun opsamle
    advarsler og fejl, og lige præcis miste de cyklus-linjer man skal bruge.
    """
    journal = Journal(capacity)
    journal.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    logging.getLogger().addHandler(journal)
    return journal
