"""Klient mod Home Assistants REST-API.

En add-on når HA på ``http://supervisor/core`` med det ``SUPERVISOR_TOKEN`` den
får i miljøet — samme mønster som Predbat bruger i standalone-tilstand
(``apps/predbat/components.py``). Til lokal udvikling kan begge dele overstyres
med miljøvariabler, så koden kan afprøves uden for HA.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor/core"


class HaError(RuntimeError):
    pass


@dataclass(frozen=True)
class State:
    entity_id: str
    state: str
    attributes: dict[str, Any]

    def as_float(self) -> float | None:
        """Tilstanden som tal, eller None hvis den ikke er et.

        HA leverer ``unknown`` og ``unavailable`` som helt almindelige
        tilstande, og de skal ikke ende som 0 i en beregning.
        """
        try:
            value = float(self.state)
        except (TypeError, ValueError):
            return None
        return value if value == value else None


class HomeAssistant:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._url = os.environ.get("VARMEOPT_HA_URL", SUPERVISOR_URL).rstrip("/")
        self._token = os.environ.get("VARMEOPT_HA_TOKEN") or os.environ.get(
            "SUPERVISOR_TOKEN"
        )
        if not self._token:
            raise HaError(
                "hverken SUPERVISOR_TOKEN eller VARMEOPT_HA_TOKEN er sat - "
                "kan ikke nå Home Assistant"
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def get_state(self, entity_id: str) -> State | None:
        if not entity_id:
            return None
        url = f"{self._url}/api/states/{entity_id}"
        try:
            async with self._session.get(url, headers=self._headers) as res:
                if res.status == 404:
                    return None
                if res.status >= 400:
                    raise HaError(f"GET {entity_id} -> HTTP {res.status}")
                body = await res.json()
        except aiohttp.ClientError as exc:
            raise HaError(f"GET {entity_id}: {exc}") from exc

        return State(
            entity_id=body.get("entity_id", entity_id),
            state=str(body.get("state", "")),
            attributes=body.get("attributes") or {},
        )

    async def set_state(
        self, entity_id: str, state: Any, attributes: dict[str, Any] | None = None
    ) -> None:
        url = f"{self._url}/api/states/{entity_id}"
        payload = {"state": state, "attributes": attributes or {}}
        try:
            async with self._session.post(
                url, headers=self._headers, json=payload
            ) as res:
                if res.status >= 400:
                    raise HaError(f"POST {entity_id} -> HTTP {res.status}")
        except aiohttp.ClientError as exc:
            raise HaError(f"POST {entity_id}: {exc}") from exc
