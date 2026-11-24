"""Logbogen: hvad planen så, og hvad der skete - én linje pr. halvtime.

Alt det varmeopt lærer i dag, lærer den af målinger: COP'en, kurven,
forbruget. Ingen af dem fortæller om *planen* var god. Regnskabet
(``ledger.py``) gør det for den enkelte opladning, men kun bagefter og kun
som én sum - det kan ikke sige om fejlen lå i behovet, i COP'en eller i
prisen, og det kan ikke prøve hvad en anden indstilling ville have gjort.

Til det skal der gemmes to ting pr. halvtime, og de skal gemmes *dengang*:

* **Det planen så.** Predbats plan som den lå, hvor langt priserne var
  låst, og udsigten frem - behov, COP, ladens COP og udetemperatur for
  hver halvtime i horisonten. Det er værdierne fra dengang, ikke dem dagens
  modeller ville give, for det er dem beslutningen blev truffet på.
* **Det der skete.** Varmepumpens strøm og varme, solvarme, elpatron,
  pillefyr og husets træk, summeret over halvtimen, og lagerets indhold ved
  slutningen.

Med fjorten dage af det kan man måle hvor meget behovsudsigten rammer ved
siden af i de dyre timer, og en genafspilning kan køre planlæggeren på det
den vidste dengang og holde resultatet op mod det der faktisk skete. Begge
dele kommer senere; uden logbogen kan ingen af dem begynde.

Den er også fejlsøgning: «hvorfor tog pillefyret over kl. 13» kan læses
bagud i stedet for at gættes ud fra grafer.

**Format.** JSONL, én linje pr. afsluttet halvtime, i ``/data``. Linjen
skrives når halvtimen skifter. En halvtime add-on'en kun så en del af, får
``dækning`` under 1 og skal springes over af den der regner. En linje der
blev halvt skrevet da strømmen gik, springes over ved læsning, aldrig hele
filen.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SLOT_SECONDS = 1800
KEEP_DAYS = 14
# Længste spring mellem to cyklusser der integreres. Samme grænse som
# regnskabet: har add-on'en været nede, ved vi ikke hvad pumpen lavede.
MAX_GAP_SECONDS = 300.0
# Så mange linjer holdes i hukommelsen til siden og debug-filen.
RECENT_ROWS = 96

# Det der summeres over halvtimen, som kW -> kWh.
ENERGY_KEYS = ("vp_el", "vp_varme", "solvarme", "elpatron", "pillefyr", "behov")


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _r(value: Any, digits: int = 2) -> Any:
    if _finite(value):
        return round(float(value), digits)
    return None


@dataclass
class Reading:
    """Én cyklus' målinger. ``None`` er «ved ikke», ikke nul."""

    vp_el: float | None = None
    vp_varme: float | None = None
    solvarme: float | None = None
    elpatron: float | None = None
    pillefyr: float | None = None
    behov: float | None = None
    lager_kwh: float | None = None
    ude: float | None = None
    kompressor: bool | None = None
    lad_op: bool = False
    # Det lave ladeflag for sig - se ``planner.choose_charge``.
    lad_op_lav: bool = False
    kilde: str | None = None


class Logbook:
    def __init__(self, path: Path | None, keep_days: int = KEEP_DAYS) -> None:
        self.path = path
        self.keep_days = keep_days
        self.recent: list[dict[str, Any]] = []
        self._slot: float | None = None
        self._seen: dict[str, Any] | None = None
        self._sums: dict[str, float] = {}
        self._seconds: dict[str, float] = {}
        self._covered = 0.0
        self._minutes_running = 0.0
        # Sekunder hvor det overhovedet vides om kompressoren kører. Ukendt er
        # ikke stilstand.
        self._seconds_known = 0.0
        self._minutes_charge = 0.0
        self._minutes_charge_low = 0.0
        self._source_minutes: dict[str, float] = {}
        self._outdoor_sum = 0.0
        self._outdoor_seconds = 0.0
        self._last_store: float | None = None
        self._last: float | None = None
        self._last_reading: Reading | None = None
        self._pruned_day: int | None = None
        self._load_recent()

    # ----------------------------------------------------------- iagttag

    def observe(
        self,
        now: float,
        reading: Reading,
        seen: Callable[[], dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Én cyklus. Returnerer halvtimens linje når en halvtime er sluttet.

        ``seen`` kaldes kun i den første cyklus af en halvtime: udsigten frem
        koster et opslag pr. række, og det er planen ved halvtimens start der
        skal stå på linjen.
        """
        slot = now - now % SLOT_SECONDS
        finished = None
        # Tiden siden forrige cyklus tilhører *forrige* cyklus' værdier: de
        # gjaldt mens den gik. Et spring over grænsen tælles ikke.
        span = (
            self._last is not None
            and self._last_reading is not None
            and 0 < now - self._last <= MAX_GAP_SECONDS
        )
        if span and self._slot is not None:
            boundary = self._slot + SLOT_SECONDS
            self._integrate(self._last_reading, min(now, boundary) - self._last)

        if self._slot is None or slot != self._slot:
            if self._slot is not None:
                finished = self._finish()
            self._begin(slot, seen)
            # Resten af springet, efter grænsen, hører til den nye halvtime.
            if span:
                self._integrate(self._last_reading, now - max(slot, self._last))

        self._last = now
        self._last_reading = reading
        return finished

    def _integrate(self, reading: Reading, seconds: float) -> None:
        if seconds <= 0:
            return
        self._covered += seconds
        for key in ENERGY_KEYS:
            value = getattr(reading, key)
            if _finite(value):
                self._sums[key] = self._sums.get(key, 0.0) + max(0.0, value) * seconds / 3600
                self._seconds[key] = self._seconds.get(key, 0.0) + seconds
        if reading.kompressor is not None:
            self._seconds_known += seconds
            if reading.kompressor:
                self._minutes_running += seconds / 60
        if reading.lad_op:
            if reading.lad_op_lav:
                self._minutes_charge_low += seconds / 60
            else:
                self._minutes_charge += seconds / 60
        if reading.kilde:
            self._source_minutes[reading.kilde] = (
                self._source_minutes.get(reading.kilde, 0.0) + seconds / 60
            )
        if _finite(reading.ude):
            self._outdoor_sum += reading.ude * seconds
            self._outdoor_seconds += seconds
        if _finite(reading.lager_kwh):
            self._last_store = reading.lager_kwh

    def _begin(self, slot: float, seen: Callable[[], dict[str, Any]]) -> None:
        self._slot = slot
        try:
            self._seen = seen()
        except Exception:  # noqa: BLE001 - logbogen må aldrig vælte en cyklus
            log.exception("logbogen kunne ikke læse planen")
            self._seen = {}
        self._sums, self._seconds = {}, {}
        self._covered = 0.0
        self._minutes_running = self._minutes_charge = 0.0
        self._minutes_charge_low = self._seconds_known = 0.0
        self._source_minutes = {}
        self._outdoor_sum = self._outdoor_seconds = 0.0
        self._last_store = None

    def _finish(self) -> dict[str, Any]:
        assert self._slot is not None
        measured: dict[str, Any] = {}
        for key in ENERGY_KEYS:
            # Kun når føleren svarede det meste af halvtimen. En sum over fem
            # minutter er ikke halvtimens energi.
            if self._seconds.get(key, 0.0) >= 0.8 * self._covered > 0:
                scale = self._covered / self._seconds[key]
                measured[f"{key}_kwh"] = _r(self._sums.get(key, 0.0) * scale, 3)
            else:
                measured[f"{key}_kwh"] = None
        measured["lager_slut_kwh"] = _r(self._last_store, 2)
        measured["ude_middel"] = (
            _r(self._outdoor_sum / self._outdoor_seconds, 1)
            if self._outdoor_seconds > 0
            else None
        )
        # Samme regel som energien: kun når det vidstes det meste af tiden.
        measured["kompressor_min"] = (
            _r(self._minutes_running, 1)
            if self._seconds_known >= 0.8 * self._covered > 0
            else None
        )
        measured["lad_op_min"] = _r(self._minutes_charge, 1)
        measured["lad_op_lav_min"] = _r(self._minutes_charge_low, 1)
        measured["kilde_min"] = {k: _r(v, 1) for k, v in self._source_minutes.items()}
        row = {
            "t": int(self._slot),
            "dækning": _r(min(1.0, self._covered / SLOT_SECONDS), 2),
            "så": self._seen or {},
            "skete": measured,
        }
        self._write(row)
        self.recent = (self.recent + [row])[-RECENT_ROWS:]
        return row

    # --------------------------------------------------------------- disk

    def _write(self, row: dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            line = _line(row)
            # Gik strømmen midt i forrige linje, mangler dens linjeskift, og
            # den nye linje ville blive limet på den halve - så var begge tabt.
            prefix = "\n" if not _ends_with_newline(self.path) else ""
            with self.path.open("a", encoding="utf-8") as f:
                f.write(prefix + line)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("logbogen kunne ikke skrives: %s", exc)
            return
        day = int(row["t"] // 86400)
        if day != self._pruned_day:
            self._pruned_day = day
            self.prune(row["t"])

    def prune(self, now: float) -> None:
        """Smid linjer ældre end ``keep_days``. Skrives atomisk."""
        if self.path is None:
            return
        # Hele oprydningen er værnet: en diskfejl her må ikke springe ud af
        # ``_finish``. Så ville halvtimen ikke være afsluttet, og næste cyklus
        # skrev den samme linje igen (gennemgangen den 24. november).
        try:
            if not self.path.exists():
                return
            cut = now - self.keep_days * 86400
            rows = read_rows(self.path)
            kept = [r for r in rows if r.get("t", 0) >= cut]
            if len(kept) == len(rows):
                return
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".logbog.", suffix=".tmp"
            )
        except OSError as exc:
            log.warning("logbogen kunne ikke ryddes: %s", exc)
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for r in kept:
                    f.write(_line(r))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("logbogen kunne ikke ryddes: %s", exc)
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def _load_recent(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            self.recent = read_rows(self.path)[-RECENT_ROWS:]
        except OSError:
            self.recent = []

    def light(self, rows: int = 48) -> list[dict[str, Any]]:
        """De sidste linjer uden planen og udsigten - til debug-filen.

        Hele linjen er nogle kilobyte, fordi planen og udsigten står i den.
        Debug-filen skal kunne sendes videre; filen i ``/data`` har resten.
        """
        out = []
        for row in self.recent[-rows:]:
            seen = {k: v for k, v in (row.get("så") or {}).items() if k not in ("plan", "frem")}
            out.append({**row, "så": seen})
        return out


def _ends_with_newline(path: Path) -> bool:
    """Tom eller manglende fil tæller som afsluttet."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return True
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"
    except OSError:
        return True


def _line(row: dict[str, Any]) -> str:
    """Én linje. Hele linjen bygges før der skrives, så en fejl i et felt ikke
    efterlader en halv linje i filen."""
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Filens linjer. En halvt skrevet linje springes over, ikke resten."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if _well_formed(row):
            out.append(row)
    return out


def _well_formed(row: Any) -> bool:
    """Har linjen den form siden og debug-filen regner med?

    En halvt skrevet linje er ugyldig JSON og springes over i forvejen. Det
    her er en gyldig linje med forkerte typer - håndredigeret eller fra en
    anden udgave - der ellers kunne vælte forsiden i to døgn.
    """
    if not isinstance(row, dict) or not _finite(row.get("t")):
        return False
    if not isinstance(row.get("så", {}), dict) or not isinstance(row.get("skete", {}), dict):
        return False
    cover = row.get("dækning")
    return cover is None or _finite(cover)


def compact_plan(raw: dict[str, Any] | None, rows: int) -> dict[str, Any] | None:
    """Planen som kolonner, kun horisonten ud. Navnene står én gang, ikke pr. række."""
    if not raw:
        return None
    slots = raw.get("slots") or []
    columns = ("minutes", "state", "import", "export", "soc", "floor", "export_limit", "target", "sells")
    out = {k: v for k, v in raw.items() if k != "slots"}
    out["kolonner"] = list(columns)
    out["rækker"] = [
        [_r(s.get(c), 4) if isinstance(s.get(c), float) else s.get(c) for c in columns]
        for s in slots[:rows]
    ]
    return out
