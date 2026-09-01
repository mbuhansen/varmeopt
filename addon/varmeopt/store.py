"""Persistent lager i add-on'ens ``/data``.

``/data`` er den mappe Home Assistant giver en add-on som varigt volumen — den
overlever genstart og opdatering af add-on'en. Det er hele grunden til at COP-
tabellen flyttes hertil: i Node-REDs context kunne vi ikke bekræfte at den
overhovedet blev skrevet til disk.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path("/data")


def default_root() -> Path:
    """``/data`` i add-on'en; overstyrbar til lokal afprøvning.

    Uden overstyringen ville et lokalt kald på Windows oprette ``C:\\data``.
    """
    override = os.environ.get("VARMEOPT_DATA_DIR")
    return Path(override) if override else DEFAULT_DATA_DIR


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.root / name

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def load(self, name: str, default: Any = None) -> Any:
        try:
            return json.loads(self.path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def save(self, name: str, data: Any) -> None:
        """Skriv atomisk, så en afbrudt skrivning ikke kan ødelægge filen.

        Uden det kan et strømudfald midt i en skrivning koste hele den
        indlærte tabel — den samme slags tab vi netop flyttede hertil for at
        undgå.
        """
        target = self.path(name)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def backup(self, name: str, suffix: str) -> Path | None:
        """Læg en kopi til side før noget destruktivt. Returnerer stien."""
        src = self.path(name)
        if not src.exists():
            return None
        dst = self.path(f"{name}.{suffix}.bak")
        dst.write_bytes(src.read_bytes())
        return dst
