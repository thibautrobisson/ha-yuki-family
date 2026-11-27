"""Yuki Family : relie ce Home Assistant à l'appli Yuki Family, sans l'ouvrir sur Internet.

La maison appelle Yuki (jamais l'inverse) : elle attend ses demandes (GET /ha-relais/attendre, 25 s au plus), les fait
ici, puis renvoie chaque réponse (POST /ha-relais/repondre/<id>), toujours en HTTPS vérifié. Seules les demandes de la
liste autorisée (allowlist.py, recopiée dans le README) passent :
- lectures et services : rejoués sur l'API locale (127.0.0.1) avec l'utilisateur système « Yuki Family », qui n'est pas
  administrateur ; Home Assistant y applique ses propres droits ;
- les quelques réglages réservés aux administrateurs dont l'appli a besoin (ajouter une marque, renommer un appareil,
  créer une pièce…) : faits ici même, un par un, avec leurs seuls paramètres attendus. Renommer, ranger ou recharger ne
  vaut que pour les intégrations ajoutées par un assistant de marque de Yuki (leurs entry_id sont gardés à la création).
Tout le reste est refusé. Le secret de la maison ne sert qu'à parler au serveur de Yuki ; le jeton de Home Assistant,
lui, ne sort jamais d'ici.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import aiohttp

from homeassistant import data_entry_flow
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.data_entry_flow import FlowManagerIndexView
from homeassistant.helpers.json import json_bytes
from homeassistant.loader import async_get_config_flows

from .allowlist import FLOW_STEP, REENABLE_FROM, Refused, automation_view, classify, owned
from .const import CONF_ENTRIES, CONF_RELAY_ID, CONF_SECRET, CONF_SERVER, CONF_USER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)
USER_NAME = "Yuki Family"
JSON = "application/json"
DONE = (data_entry_flow.FlowResultType.CREATE_ENTRY, data_entry_flow.FlowResultType.ABORT)


async def _user(hass: HomeAssistant, entry: ConfigEntry):
    """L'utilisateur système « Yuki Family », simple utilisateur (pas administrateur), créé une fois."""
    user = await hass.auth.async_get_user(entry.data[CONF_USER_ID]) if entry.data.get(CONF_USER_ID) else None
    if user is None:
        user = await hass.auth.async_create_system_user(USER_NAME, group_ids=[GROUP_ID_USER])
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_USER_ID: user.id})
    elif user.is_admin:  # créé administrateur jusqu'à la version 1.0.1
        await hass.auth.async_update_user(user, group_ids=[GROUP_ID_USER])
    return next(iter(user.refresh_tokens.values()), None) or await hass.auth.async_create_refresh_token(user)


def _flow_json(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """Une étape d'ajout de marque, mise en forme comme par l'API de Home Assistant (/api/config/config_entries/flow)."""
    if result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY:
        entry = result["result"]
        data = {k: v for k, v in result.items() if k not in ("data", "context", "result")}
        return data | {"result": {"entry_id": entry.entry_id, "domain": entry.domain, "title": entry.title}}
    return FlowManagerIndexView(hass.config_entries.flow)._prepare_result_json(result)  # noqa: SLF001


class Relay:
    """Une maison reliée : la boucle d'attente, une session WebSocket locale gardée ouverte, les réponses."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, refresh) -> None:
        self.hass = hass
        self.entry = entry
        self.refresh = refresh
        self.server = entry.data[CONF_SERVER].rstrip("/")
        self.headers = {"x-relais": entry.data[CONF_RELAY_ID], "Authorization": f"Bearer {entry.data[CONF_SECRET]}"}
        # Le serveur de Yuki : certificat toujours vérifié (le secret de la maison part dans chaque requête).
        self.yuki = async_get_clientsession(hass)
        # Home Assistant lui-même, sur cette machine seulement (HTTPS local : certificat non vérifié, c'est le nôtre).
        api = hass.config.api
        self.local = f"{'https' if api and api.use_ssl else 'http'}://127.0.0.1:{api.port if api else 8123}"
        self.local_session = async_get_clientsession(hass, verify_ssl=False)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.ws_lock = asyncio.Lock()
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.flows: set[str] = set()  # ajouts de marque ouverts par Yuki : les seuls qu'il peut poursuivre ou abandonner
        self.task: asyncio.Task | None = None

    def yuki_entries(self) -> set[str]:
        """Les intégrations ajoutées par Yuki et toujours là."""
        return {e for e in self.entry.data.get(CONF_ENTRIES, []) if self.hass.config_entries.async_get_entry(e)}

    def remember(self, entry_id: str) -> None:
        kept = sorted(self.yuki_entries() | {entry_id})
        self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_ENTRIES: kept})

    def token(self) -> str:
        return self.hass.auth.async_create_access_token(self.refresh)

    # --- Lectures et services : l'API locale, avec l'utilisateur Yuki Family (non administrateur) ---

    async def _local_ws(self) -> aiohttp.ClientWebSocketResponse:
        async with self.ws_lock:
            if self.ws is not None and not self.ws.closed:
                return self.ws
            ws = await self.local_session.ws_connect(f"{self.local}/api/websocket", heartbeat=30)
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

    async def http(self, path: str) -> tuple[int, str, bytes]:
        async with self.local_session.get(
            f"{self.local}{path}",
            headers={"Authorization": f"Bearer {self.token()}"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=18),
        ) as res:
            return res.status, res.headers.get("Content-Type", "application/octet-stream"), await res.read()

    # --- Réglages réservés aux administrateurs : faits ici, un par un (paramètres contrôlés par classify) ---

    async def admin(self, what: str, cmd: dict[str, Any]) -> Any:
        hass = self.hass
        if what == "reload":
            entry_id = cmd["service_data"]["entry_id"]
            if entry_id not in self.yuki_entries():
                raise Refused("intégration qui n'a pas été ajoutée depuis Yuki : à recharger dans Home Assistant")
            await hass.config_entries.async_reload(entry_id)
            return {"context": None, "response": None}
        if what == "config/device_registry/update":
            registry = dr.async_get(hass)
            if (device := registry.async_get(cmd["device_id"])) is None:
                raise ValueError("Appareil inconnu")
            # Une seule intégration par appareil depuis 2026.11 (config_entries disparaît en 2027.10) ; plusieurs avant.
            ids = {device.config_entry_id} if hasattr(device, "config_entry_id") else set(device.config_entries)
            if not owned(ids, self.yuki_entries()):
                raise Refused("appareil qui n'a pas été ajouté depuis Yuki : à renommer ou ranger dans Home Assistant")
            if cmd.get("area_id") is not None and ar.async_get(hass).async_get_area(cmd["area_id"]) is None:
                raise ValueError("Pièce inconnue")
            changes = {k: cmd[k] for k in ("name_by_user", "area_id") if k in cmd}
            if "name_by_user" in changes:
                changes["name_by_user"] = changes["name_by_user"].strip()
            device = registry.async_update_device(cmd["device_id"], **changes)
            return {"id": device.id, "name_by_user": device.name_by_user, "area_id": device.area_id}
        if what == "config/area_registry/create":
            area = ar.async_get(hass).async_create(cmd["name"].strip())
            return {"area_id": area.id, "name": area.name}
        if what == "config/entity_registry/update":
            registry = er.async_get(hass)
            if (entity := registry.async_get(cmd["entity_id"])) is None:
                raise ValueError("Entité inconnue")
            if entity.disabled_by is not None and entity.disabled_by != REENABLE_FROM:
                raise Refused("téléphone désactivé par l'utilisateur : à réactiver dans Home Assistant")
            registry.async_update_entity(cmd["entity_id"], disabled_by=None)
            return {"entity_entry": {"entity_id": cmd["entity_id"], "disabled_by": None}}
        # automation/config : la description seulement
        component = hass.data.get("automation")
        automation = component.get_entity(cmd["entity_id"]) if component else None
        if automation is None:
            raise ValueError("Automatisation inconnue")
        return {"config": automation_view(automation.raw_config)}

    async def flow(self, what: str, ask: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Ajouter une marque : une intégration de la liste des marques (jamais Yuki Family), formulaires ouverts ici seulement."""
        flows = self.hass.config_entries.flow
        body = json.loads(ask.get("body") or "{}")
        if not isinstance(body, dict):
            raise Refused("formulaire")
        try:
            if what == "flow_start":
                handler = body.get("handler")
                if (
                    set(body) - {"handler", "show_advanced_options"}
                    or not isinstance(handler, str)
                    or handler == DOMAIN
                    or handler not in await async_get_config_flows(self.hass, "integration")
                ):
                    raise Refused(f"marque {handler!r}")
                result = await flows.async_init(handler, context={"source": SOURCE_USER, "show_advanced_options": False})
            else:
                flow_id = FLOW_STEP.fullmatch(ask["path"]).group(1)
                if flow_id not in self.flows:
                    raise Refused("formulaire que Yuki n'a pas ouvert")
                if what == "flow_delete":
                    self.flows.discard(flow_id)
                    flows.async_abort(flow_id)
                    return 200, {"message": "Flow aborted"}
                result = await flows.async_configure(flow_id, body)
        except data_entry_flow.UnknownHandler:
            return 404, {"message": "Invalid handler specified"}
        except data_entry_flow.UnknownFlow:
            return 404, {"message": "Invalid flow specified"}
        except data_entry_flow.InvalidData as err:
            return 400, {"errors": err.schema_errors}
        except data_entry_flow.UnknownStep as err:
            return 400, {"message": str(err)}
        if result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY:
            self.remember(result["result"].entry_id)
        (self.flows.discard if result["type"] in DONE else self.flows.add)(result["flow_id"])
        return 200, _flow_json(self.hass, result)

    # --- Yuki ---

    async def answer(self, ask: dict[str, Any]) -> tuple[int, str, bytes]:
        """Une demande de Yuki, si elle est dans la liste autorisée : (statut, type, contenu)."""
        where, what = classify(ask)
        if where == "user":
            if what == "ws":
                return 200, JSON, json_bytes(await self.command(ask["cmd"]))
            return await self.http(ask["path"])
        if what.startswith("flow_"):
            status, data = await self.flow(what, ask)
            return status, JSON, json_bytes(data)
        return 200, JSON, json_bytes({"type": "result", "success": True, "result": await self.admin(what, ask["cmd"])})

    async def handle(self, ask: dict[str, Any]) -> None:
        try:
            status, kind, body = await self.answer(ask)
        except Refused as err:
            _LOGGER.warning("Demande de Yuki Family refusée (hors de la liste autorisée) : %s", err)
            status, kind = 403, JSON
            body = json_bytes({"success": False, "error": {"code": "unauthorized", "message": f"Refusé par l'intégration Yuki Family ({err})"}})
        except Exception as err:  # noqa: BLE001 — chaque erreur part à Yuki, qui l'affiche
            _LOGGER.debug("Demande de Yuki en échec : %s", err)
            status, kind = 502, JSON
            body = json_bytes({"success": False, "error": {"message": str(err) or type(err).__name__}})
        try:
            async with self.yuki.post(
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
                async with self.yuki.get(
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
        for flow_id in self.flows:  # un ajout laissé ouvert bloquerait cette marque jusqu'au redémarrage
            with contextlib.suppress(data_entry_flow.UnknownFlow):
                self.hass.config_entries.flow.async_abort(flow_id)
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
