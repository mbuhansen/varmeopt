#!/usr/bin/env python3
"""Startskal: rydder op efter en mislykket selvopdatering og starter så app'en.

Den ligger i add-on-imaget på ``/app/bootstrap.py`` og hentes aldrig ned fra
GitHub. Det er hele pointen: fejler den hentede kode allerede ved *import*,
når app'ens egen genopretning aldrig at køre, og containeren ville løbe i evig
genstartsløkke. Skallen kører før pakken overhovedet importeres og kan derfor
rydde op uanset hvor ødelagt koden i ``/data/code`` måtte være.

Derfor importerer den heller ikke ``varmeopt``. Den bruger kun
standardbiblioteket og kender selv de få stier den skal bruge.

Ved en selvopdatering sættes et mærke lige før genstarten, og app'en fjerner
det når web-UI'et er oppe. Står mærket der stadig efter henstandsperioden, er
opstarten aldrig lykkedes, og forrige udgave hentes frem igen. Krakelerer den
nye kode ved import, genstarter Supervisor containeren nogle gange — og efter
henstandsperioden retter skallen op af sig selv. Det er afgrænset, og det er
det vigtige: der findes altid en vej tilbage uden en terminal.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

PACKAGE = "varmeopt"
MARKER_NAME = ".starter"
REVISION_NAME = ".revision"
PREVIOUS_NAME = f".{PACKAGE}.forrige"

# Skal være rundelig nok til at rumme en langsom opstart, og kort nok til at
# en genstartsløkke ikke varer ved. Samme værdi som selfupdate.py bruger.
GRACE_SECONDS = 180


def code_dir() -> Path:
    return Path(os.environ.get("VARMEOPT_CODE_DIR", "/data/code"))


def say(message: str) -> None:
    # Ingen logging-opsætning her; app'en ejer den. print går i add-on-loggen.
    print(f"bootstrap: {message}", flush=True)


def marker_age(root: Path) -> float | None:
    try:
        return time.time() - (root / MARKER_NAME).stat().st_mtime
    except OSError:
        return None


def roll_back(root: Path) -> None:
    """Hent forrige hentede udgave frem, eller fald tilbage til imagets kode."""
    live = root / PACKAGE
    previous = root / PREVIOUS_NAME

    if previous.is_dir():
        shutil.rmtree(live, ignore_errors=True)
        previous.rename(live)
        say("rullede tilbage til forrige hentede udgave")
    elif live.exists():
        # Ingen forrige at gå tilbage til. Så fjernes den hentede kode helt,
        # og pakken i imaget overtager igen — den har vi altid.
        shutil.rmtree(live, ignore_errors=True)
        say("fjernede den hentede kode, kører imagets egen igen")
    else:
        say("intet at rulle tilbage")

    try:
        (root / REVISION_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def recover_if_needed(root: Path) -> None:
    age = marker_age(root)
    if age is None:
        return
    if age <= GRACE_SECONDS:
        # Vores egen genstart, sat for et øjeblik siden. App'en rydder mærket
        # når den er oppe.
        return

    say(f"sidste opstart naaede aldrig frem ({age:.0f} s) - retter op")
    roll_back(root)
    try:
        (root / MARKER_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def main() -> None:
    try:
        recover_if_needed(code_dir())
    except Exception as exc:  # skallen må aldrig selv blokere opstarten
        say(f"genopretning fejlede, starter alligevel: {exc}")

    os.execv(sys.executable, [sys.executable, "-m", PACKAGE])


if __name__ == "__main__":
    main()
