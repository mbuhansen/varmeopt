"""Read-only klient mod Node-REDs admin-API.

Node-RED er stadig den der taler med UVR'en, varmepumpen og CMI'en, og i fase
0-1 er den også stadig den der bestemmer. Vi læser med over skulderen: COP-
tabellen én gang ved migrering, og de samme inputvariabler den selv regner på,
så add-on'en kan køre i skyggedrift og sammenlignes.

Der skrives aldrig herfra.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Varmestyring-fanen. Dens flow-context har udeTemp, flowTemp, battery_power
# og grid_power, sat af change-noder fra MQTT og HA.
DEFAULT_FLOW_ID = "d8e88a85aed0c143"


def _unwrap(value: Any) -> Any:
    """Skræl Node-REDs context-indpakning af.

    Værdier kommer som ``{"msg": ..., "format": "..."}``, og store objekter
    kan være serialiseret som streng.
    """
    if isinstance(value, dict) and "msg" in value:
        value = value["msg"]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class NodeRed:
    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._url = base_url.rstrip("/")

    async def _get(self, path: str, timeout: float = 20.0) -> Any | None:
        url = f"{self._url}{path}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as res:
                if res.status >= 400:
                    log.warning("Node-RED %s -> HTTP %s", path, res.status)
                    return None
                return await res.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.warning("kunne ikke naa Node-RED paa %s: %s", url, exc)
            return None

    async def context(self, scope: str) -> dict[str, Any]:
        """Hent et helt context-scope, udpakket. ``scope`` er fx ``global``."""
        body = await self._get(f"/context/{scope}")
        if not isinstance(body, dict):
            return {}
        # Svaret er grupperet efter context-store; instansen her har kun
        # "default", men vi flader ud i tilfælde af flere.
        store = body.get("default")
        if not isinstance(store, dict):
            store = body
        return {key: _unwrap(value) for key, value in store.items()}

    async def global_context(self) -> dict[str, Any]:
        return await self.context("global")

    async def flow_context(self, flow_id: str = DEFAULT_FLOW_ID) -> dict[str, Any]:
        return await self.context(f"flow/{flow_id}")
