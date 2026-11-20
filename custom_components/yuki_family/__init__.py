"""Yuki Family : relie ce Home Assistant à l'appli Yuki Family, sans l'ouvrir sur Internet.

La maison appelle Yuki (jamais l'inverse) : elle attend ses demandes (GET /ha-relais/attendre, 25 s au plus), les rejoue
ici avec son propre utilisateur « Yuki Family » (une commande WebSocket, ou un appel REST sous /api/), puis renvoie
chaque réponse (POST /ha-relais/repondre/<id>). Le secret de la maison ne sert qu'à parler au serveur de Yuki ; le jeton
de Home Assistant, lui, ne sort jamais d'ici.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_RELAY_ID, CONF_SECRET, CONF_SERVER, CONF_USER_ID

_LOGGER = logging.getLogger(__name__)
USER_NAME = "Yuki Family"


async def _user(hass: HomeAssistant, entry: ConfigEntry):
    """L'utilisateur système « Yuki Family » (administrateur : ajouter une marque, régler les appareils), créé une fois."""
    user = await hass.auth.async_get_user(entry.data[CONF_USER_ID]) if entry.data.get(CONF_USER_ID) else None
    if user is None:
        user = await hass.auth.async_create_system_user(USER_NAME, group_ids=[GROUP_ID_ADMIN])
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_USER_ID: user.id})
    refresh = next(iter(user.refresh_tokens.values()), None) or await hass.auth.async_create_refresh_token(user)
    return refresh


class Relay:
    """Une maison reliée : la boucle d'attente, une session WebSocket locale gardée ouverte, les réponses."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, refresh) -> None:
        self.hass = hass
        self.entry = entry
        self.refresh = refresh
        self.server = entry.data[CONF_SERVER].rstrip("/")
        self.headers = {"x-relais": entry.data[CONF_RELAY_ID], "Authorization": f"Bearer {entry.data[CONF_SECRET]}"}
        api = hass.config.api
        # Home Assistant lui-même, sur cette machine (HTTPS local : certificat non vérifié, c'est le nôtre).
        self.local = f"{'https' if api and api.use_ssl else 'http'}://127.0.0.1:{api.port if api else 8123}"
        self.session = async_get_clientsession(hass, verify_ssl=False)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.ws_lock = asyncio.Lock()
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.task: asyncio.Task | None = None

    def token(self) -> str:
        return self.hass.auth.async_create_access_token(self.refresh)

    # --- Home Assistant, en local ---

    async def _local_ws(self) -> aiohttp.ClientWebSocketResponse:
        async with self.ws_lock:
            if self.ws is not None and not self.ws.closed:
                return self.ws
            ws = await self.session.ws_connect(f"{self.local}/api/websocket", heartbeat=30)
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": self.token()})
            ok = await ws.receive_json()
            if ok.get("type") != "auth_ok":
                await ws.close()
                raise RuntimeError("Home Assistant refuse l'utilisateur Yuki Family")
            self.ws = ws
            self.hass.async_create_background_task(self._read(ws), "yuki_family_ws")
            return ws

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            for m in data if isinstance(data, list) else [data]:
                fut = self.pending.pop(m.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(m)
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("WebSocket fermé"))
        self.pending.clear()

    async def command(self, cmd: dict[str, Any]) -> dict[str, Any]:
        ws = await self._local_ws()
        self.next_id += 1
        cmd = {k: v for k, v in cmd.items() if k != "id"} | {"id": self.next_id}
        fut = self.hass.loop.create_future()
        self.pending[self.next_id] = fut
        await ws.send_json(cmd)
        return await asyncio.wait_for(fut, 18)

    async def http(self, method: str, path: str, body: str | None) -> tuple[int, str, bytes]:
        if not path.startswith("/api/"):
            return 403, "text/plain", b"chemin refuse"
        async with self.session.request(
            method,
            f"{self.local}{path}",
            data=body,
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=18),
        ) as res:
            return res.status, res.headers.get("Content-Type", "application/octet-stream"), await res.read()

    # --- Yuki ---

    async def handle(self, ask: dict[str, Any]) -> None:
        status, kind, body = 200, "application/json", b""
        try:
            if ask.get("kind") == "ws":
                body = json.dumps(await self.command(ask.get("cmd") or {})).encode()
            elif ask.get("kind") == "http":
                status, kind, body = await self.http(str(ask.get("method", "GET")), str(ask.get("path", "")), ask.get("body"))
        except Exception as err:  # noqa: BLE001 — chaque erreur part à Yuki, qui l'affiche
            _LOGGER.debug("Demande de Yuki en échec : %s", err)
            body = json.dumps({"success": False, "error": {"message": str(err) or type(err).__name__}}).encode()
            status = 502
        try:
            async with self.session.post(
                f"{self.server}/ha-relais/repondre/{ask['id']}",
                data=body,
                headers={**self.headers, "Content-Type": "application/octet-stream", "x-status": str(status), "x-type": kind},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as res:
                res.raise_for_status()
        except aiohttp.ClientError as err:
            _LOGGER.debug("Réponse non envoyée à Yuki : %s", err)

    async def run(self) -> None:
        pause = 2
        while True:
            try:
                async with async_get_clientsession(self.hass).get(
                    f"{self.server}/ha-relais/attendre", headers=self.headers, timeout=aiohttp.ClientTimeout(total=40)
                ) as res:
                    if res.status == 401:
                        _LOGGER.warning("Yuki Family ne reconnaît plus cette maison : relance l'appairage")
                        self.entry.async_start_reauth(self.hass)
                        return
                    res.raise_for_status()
                    asks = (await res.json()).get("asks", [])
                pause = 2
                for ask in asks:
                    self.hass.async_create_background_task(self.handle(ask), "yuki_family_ask")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — Internet coupé, serveur en mise à jour : on réessaie
                _LOGGER.debug("Yuki Family injoignable (%s), nouvel essai dans %s s", err, pause)
                await asyncio.sleep(pause)
                pause = min(pause * 2, 60)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.ws is not None:
            await self.ws.close()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    relay = Relay(hass, entry, await _user(hass, entry))
    relay.task = entry.async_create_background_task(hass, relay.run(), "yuki_family")
    entry.runtime_data = relay
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await entry.runtime_data.stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Intégration supprimée : son utilisateur aussi."""
    if entry.data.get(CONF_USER_ID) and (user := await hass.auth.async_get_user(entry.data[CONF_USER_ID])):
        await hass.auth.async_remove_user(user)
