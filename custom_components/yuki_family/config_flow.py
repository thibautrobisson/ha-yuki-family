"""Appairage avec Yuki Family : le code affiché dans l'appli (Domotique Maison), collé ici."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_CODE, CONF_RELAY_ID, CONF_SECRET, CONF_SERVER, DOMAIN, SERVER


async def pair(hass, server: str, code: str, name: str) -> dict[str, Any]:
    """Échange le code contre l'identifiant et le secret de la maison ; KeyError si le code est refusé."""
    session = async_get_clientsession(hass)
    async with session.post(
        f"{server.rstrip('/')}/ha-relais/appairer",
        json={"code": code.strip(), "name": name[:100]},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as res:
        if res.status in (400, 404):
            raise KeyError(code)
        res.raise_for_status()
        return await res.json()


class YukiFamilyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Un seul Yuki Family par Home Assistant : une maison, une famille."""

    VERSION = 1

    def _schema(self) -> vol.Schema:
        # shortcut: plus de choix du serveur (show_advanced_options disparaît en 2027.6) ; s'il en faut un, une section repliée.
        return vol.Schema({vol.Required(CONF_CODE): str})

    async def _pair(self, user_input: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        server = user_input.get(CONF_SERVER) or SERVER
        try:
            home = await pair(self.hass, server, user_input[CONF_CODE], self.hass.config.location_name or "Maison")
        except KeyError:
            return None, {"base": "invalid_code"}
        except (aiohttp.ClientError, TimeoutError):
            return None, {"base": "cannot_connect"}
        return {CONF_SERVER: server, CONF_RELAY_ID: home["id"], CONF_SECRET: home["secret"], "family": home.get("family")}, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._pair(user_input)
            if data:
                family = data.pop("family")
                return self.async_create_entry(title=f"Yuki Family ({family})" if family else "Yuki Family", data=data)
        return self.async_show_form(step_id="user", data_schema=self._schema(), errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """La maison n'est plus reliée (déconnectée dans l'appli) : un nouveau code."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Le même serveur que la première fois.
            user_input.setdefault(CONF_SERVER, self._get_reauth_entry().data.get(CONF_SERVER, SERVER))
            data, errors = await self._pair(user_input)
            if data:
                data.pop("family")
                return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        return self.async_show_form(step_id="reauth_confirm", data_schema=self._schema(), errors=errors)
