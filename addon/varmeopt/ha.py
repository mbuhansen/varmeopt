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
    # HA flytter kun ``last_changed`` når tilstanden faktisk skifter værdi, så
    # den identificerer en måling. Det er sådan en poller kan kende en ny
    # aflæsning fra den samme aflæsning set igen.
    last_changed: str | None = None

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
            last_changed=body.get("last_changed"),
        )

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> Any:
        """Kald en service og få svaret med tilbage.

        Vejrudsigten kan ikke læses som en tilstand: siden Home Assistant
        2023.7 ligger den bag ``weather.get_forecasts``, som svarer på kaldet
        i stedet for at lægge noget i en attribut. Derfor ``return_response``.
        """
        url = f"{self._url}/api/services/{domain}/{service}?return_response"
        try:
            async with self._session.post(url, headers=self._headers, json=data) as res:
                if res.status >= 400:
                    raise HaError(f"POST {domain}.{service} -> HTTP {res.status}")
                return await res.json()
        except (aiohttp.ClientError, ValueError) as exc:
            raise HaError(f"POST {domain}.{service}: {exc}") from exc

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
