"""Hent nyeste kode fra GitHub uden at gå gennem add-on-butikken.

Supervisor er langsom til at opdage et push, og under udvikling er ventetiden
den dyreste del af en rettelse. Python-koden kan derfor hentes direkte fra
master og lægges i ``/data/code``, som står før ``/app`` på ``PYTHONPATH``.
Bagefter starter processen sig selv forfra med ``os.execv``, og så kører den
nye kode — uden at Supervisor skal bygge et image.

**Det dækker kun Python-koden.** Nye indstillinger i ``config.yaml``, nye
pakker i ``requirements.txt`` og ændringer i Dockerfilen hører til imaget, ikke
til koden, og kræver stadig en rigtig add-on-opdatering. Sker det, siger
udgivelsen det, og så er butikken vejen.

Koden kommer fra et offentligt repo og køres uden menneskeligt mellemled. Det
er samme tillidsmodel som Predbats auto-update: den der kan pushe til master,
kan køre kode i denne container.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

REPO = "mbuhansen/varmeopt"
BRANCH = "master"
PACKAGE = "varmeopt"
REVISION_FILE = ".revision"

# Hvor den hentede kode lægges. Skal ligge før /app på PYTHONPATH, ellers
# vinder den indbyggede udgave. Sat i Dockerfilen.
DEFAULT_CODE_DIR = Path("/data/code")


def code_dir() -> Path:
    override = os.environ.get("VARMEOPT_CODE_DIR")
    return Path(override) if override else DEFAULT_CODE_DIR


@dataclass(frozen=True)
class Revision:
    sha: str
    message: str
    date: str

    @property
    def short(self) -> str:
        return self.sha[:8]


def current() -> str | None:
    """Den hentede udgave der kører nu, eller None hvis det er den indbyggede."""
    try:
        text = (code_dir() / REVISION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def running_downloaded() -> bool:
    """Kører vi kode fra /data/code, eller den der blev bygget ind i imaget?"""
    module = sys.modules.get(PACKAGE)
    path = getattr(module, "__file__", None)
    if path is None:
        return False
    try:
        Path(path).resolve().relative_to(code_dir().resolve())
    except (ValueError, OSError):
        return False
    return True


async def latest(session: aiohttp.ClientSession) -> Revision | None:
    """Slå den nyeste commit på master op. None hvis GitHub ikke svarer."""
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    try:
        async with session.get(
            url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as res:
            if res.status >= 400:
                log.warning("GitHub svarede HTTP %s på %s", res.status, url)
                return None
            body = await res.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("kunne ikke nå GitHub: %s", exc)
        return None

    commit = body.get("commit") or {}
    return Revision(
        sha=str(body.get("sha", "")),
        message=str(commit.get("message", "")).splitlines()[0] if commit.get("message") else "",
        date=str((commit.get("committer") or {}).get("date", "")),
    )


def _members(tar: tarfile.TarFile, prefix: str) -> list[tarfile.TarInfo]:
    """Kun pakkens .py-filer, og kun dem der bliver inde i mappen.

    Et tar-arkiv kan indeholde stier som ``../../etc/noget`` og symlinks der
    peger ud af træet. Vi henter fra vores eget repo, men et arkiv er
    stadig fremmed input, og en filtrering koster ingenting.
    """
    picked = []
    for member in tar.getmembers():
        if not member.isfile() or not member.name.startswith(prefix):
            continue
        relative = member.name[len(prefix) :]
        if not relative.endswith(".py"):
            continue
        if relative.startswith("/") or ".." in Path(relative).parts:
            log.warning("springer mistænkelig sti over: %s", member.name)
            continue
        picked.append(member)
    return picked


def _compiles(root: Path) -> bool:
    """Oversæt hver fil, så en syntaksfejl fanges før vi skifter til koden.

    Kildeteksten oversættes i hukommelsen frem for med ``py_compile``: den
    vil skrive en ``.pyc``, og at sende den til ``/dev/null`` fejler, fordi
    det ikke er en almindelig fil. Her skal der alligevel intet gemmes — vi
    vil bare vide om koden kan læses.
    """
    for path in sorted(root.rglob("*.py")):
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (SyntaxError, ValueError, OSError, UnicodeDecodeError) as exc:
            log.error("hentet kode kan ikke oversættes: %s", exc)
            return False
    return True


async def download(session: aiohttp.ClientSession) -> Revision | None:
    """Hent master og læg koden på plads. Returnerer den hentede udgave.

    Der skiftes først når hele pakken er pakket ud og oversætter. En halv
    eller ødelagt udpakning må aldrig blive den kode der starter næste gang —
    så ville add-on'en gå i genstartsløkke, og vejen tilbage ville gå gennem
    en terminal i stedet for en knap.
    """
    revision = await latest(session)
    if revision is None or not revision.sha:
        return None

    url = f"https://codeload.github.com/{REPO}/tar.gz/{revision.sha}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as res:
            if res.status >= 400:
                log.error("kunne ikke hente kildekoden: HTTP %s", res.status)
                return None
            blob = await res.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.error("kunne ikke hente kildekoden: %s", exc)
        return None

    root = code_dir()
    staging = root / ".staging"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            prefix = f"{REPO.split('/')[1]}-{revision.sha}/addon/{PACKAGE}/"
            members = _members(tar, prefix)
            if not members:
                log.error("arkivet indeholdt ingen %s-kode under %s", PACKAGE, prefix)
                return None
            for member in members:
                source = tar.extractfile(member)
                if source is None:
                    continue
                target = staging / PACKAGE / member.name[len(prefix) :]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())

        if not (staging / PACKAGE / "__main__.py").exists():
            log.error("hentet kode mangler __main__.py - skifter ikke")
            return None
        if not _compiles(staging):
            return None

        live = root / PACKAGE
        previous = root / f".{PACKAGE}.forrige"
        shutil.rmtree(previous, ignore_errors=True)
        if live.exists():
            live.rename(previous)
        (staging / PACKAGE).rename(live)
        (root / REVISION_FILE).write_text(revision.sha, encoding="utf-8")
    except (OSError, tarfile.TarError) as exc:
        log.error("kunne ikke lægge den hentede kode på plads: %s", exc)
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    log.info("hentet %s: %s", revision.short, revision.message)
    return revision


# --------------------------------------------------------------- boot-mærke
#
# py_compile fanger syntaksfejl, men ikke en ImportError. Kommer der kode ud
# som vælter under import, dør processen, Supervisor starter den igen, og den
# vælter igen — en løkke man kun kommer ud af med en terminal. Derfor sættes
# et mærke lige før genstarten og fjernes først når app'en er oppe. Findes det
# ved opstart, har sidste forsøg altså aldrig nået at køre.

_BOOT_MARKER = ".starter"

# Hvor længe mærket må stå, før vi konkluderer at opstarten aldrig lykkedes.
# Mærket sættes lige før genstarten, så den nye proces finder *sit eget*
# mærke et sekund senere — uden det her ville den altid tro at den fejlede og
# rulle den kode tilbage den lige selv hentede.
BOOT_GRACE_SECONDS = 180


def mark_boot() -> None:
    try:
        code_dir().mkdir(parents=True, exist_ok=True)
        (code_dir() / _BOOT_MARKER).write_text("1", encoding="utf-8")
    except OSError as exc:
        log.warning("kunne ikke sætte boot-mærke: %s", exc)


def boot_marker_age() -> float | None:
    """Mærkets alder i sekunder, eller None hvis der ikke er et."""
    try:
        return time.time() - (code_dir() / _BOOT_MARKER).stat().st_mtime
    except OSError:
        return None


def boot_failed() -> bool:
    """Har en opstart vitterligt hængt, eller er det bare vores egen genstart?"""
    age = boot_marker_age()
    return age is not None and age > BOOT_GRACE_SECONDS


def clear_boot() -> None:
    try:
        (code_dir() / _BOOT_MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def rollback() -> bool:
    """Skift tilbage til forrige hentede udgave, hvis der er en."""
    root = code_dir()
    previous = root / f".{PACKAGE}.forrige"
    if not previous.is_dir():
        return False
    live = root / PACKAGE
    shutil.rmtree(live, ignore_errors=True)
    previous.rename(live)
    try:
        (root / REVISION_FILE).unlink(missing_ok=True)
    except OSError:
        pass
    log.info("rullet tilbage til forrige hentede udgave")
    return True


def restart() -> None:
    """Erstat processen med en frisk én, så den nye kode importeres.

    execv beholder container og PID, så Supervisor ser ikke add-on'en falde —
    den bemærker slet ingenting, hvilket er hele pointen.

    Der startes gennem startskallen når den findes, så dens genopretning også
    kører ved en selvopdateret genstart og ikke kun ved en containerstart.
    """
    log.info("genstarter for at køre den hentede kode")
    sys.stdout.flush()
    sys.stderr.flush()

    bootstrap = os.environ.get("VARMEOPT_BOOTSTRAP")
    if bootstrap and Path(bootstrap).is_file():
        os.execv(sys.executable, [sys.executable, bootstrap])
    os.execv(sys.executable, [sys.executable, "-m", PACKAGE])
