"""Liste autorisée : tout ce que le serveur de Yuki Family peut demander à cette maison (le README la recopie).

Sans Home Assistant, pour être testée seule (tests/test_allowlist.py). Les valeurs (appareil, pièce, intégration
existants) sont vérifiées au moment de faire la demande, dans __init__.py.
"""

from __future__ import annotations

import re
from typing import Any

# Lectures et services : rejoués tels quels sur l'API locale de Home Assistant avec l'utilisateur « Yuki Family », qui
# n'est pas administrateur (Home Assistant y applique ses propres droits : un service réservé aux administrateurs est refusé).
WS_AS_USER = frozenset(
    {
        "get_states",
        "call_service",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        "recorder/statistics_during_period",
        "manifest/list",
        "search/related",
    }
)
HTTP_AS_USER = (  # GET seulement
    re.compile(r"/api/camera_proxy/camera\.[0-9a-z_]+"),
    re.compile(r"/api/config/config_entries/flow_handlers\?type=integration"),
)

# Réservés aux administrateurs dans Home Assistant : faits par l'intégration elle-même, avec ces seuls paramètres.
FLOW_START = "/api/config/config_entries/flow"  # ajouter une marque (POST)
FLOW_STEP = re.compile(r"/api/config/config_entries/flow/([0-9A-Za-z]{1,64})")  # étape suivante (POST) ou abandon (DELETE)
RELOAD = ("homeassistant", "reload_config_entry")  # « Actualiser » : recharger une intégration


def _text(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= 100


def _id(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 255


# commande WebSocket : (paramètres obligatoires, facultatifs, contrôle des valeurs)
WS_ADMIN = {
    # renommer un appareil, ou le ranger dans une pièce (ou dans aucune)
    "config/device_registry/update": (
        {"device_id"},
        {"name_by_user", "area_id"},
        lambda c: _id(c["device_id"])
        and ("name_by_user" in c or "area_id" in c)
        and ("name_by_user" not in c or _text(c["name_by_user"]))
        and (c.get("area_id") is None or _id(c["area_id"])),
    ),
    # créer une pièce
    "config/area_registry/create": ({"name"}, set(), lambda c: _text(c["name"])),
    # réactiver un téléphone que son intégration a laissé désactivé (présence à la maison), jamais un choix de l'utilisateur
    "config/entity_registry/update": (
        {"entity_id", "disabled_by"},
        set(),
        lambda c: _id(c["entity_id"]) and c["entity_id"].startswith("device_tracker.") and c["disabled_by"] is None,
    ),
    # la description d'une automatisation (programme d'un appareil), rien d'autre de sa configuration
    "automation/config": ({"entity_id"}, set(), lambda c: _id(c["entity_id"]) and c["entity_id"].startswith("automation.")),
}


# Seule raison de désactivation que Yuki peut lever : celle de l'intégration (RegistryEntryDisabler.INTEGRATION).
REENABLE_FROM = "integration"


def automation_view(raw_config: dict[str, Any]) -> dict[str, str]:
    """Ce que l'appli lit d'une automatisation : sa description. Déclencheurs, conditions et actions (webhooks, jetons…) restent ici."""
    description = raw_config.get("description") if isinstance(raw_config, dict) else None
    return {"description": description if isinstance(description, str) else ""}


def owned(device_entries: set[str], yuki_entries: set[str]) -> bool:
    """Un appareil que Yuki peut renommer ou ranger : il vient d'une intégration ajoutée par un assistant de marque de Yuki."""
    return bool(device_entries & yuki_entries)


class Refused(Exception):
    """Une demande hors de la liste autorisée."""


def _only(cmd: dict[str, Any], required: set[str], optional: set[str]) -> None:
    keys = set(cmd) - {"type", "id"}
    if not required <= keys or keys - required - optional:
        raise Refused(f"paramètres de {cmd.get('type')!r}")


def classify(ask: dict[str, Any]) -> tuple[str, str]:
    """("user", "ws" ou "http") : rejouée avec l'utilisateur Yuki Family ; ("admin", nom) : faite ici ; sinon Refused."""
    if ask.get("kind") == "ws":
        cmd = ask.get("cmd")
        kind = cmd.get("type") if isinstance(cmd, dict) else None
        if kind == "call_service" and (cmd.get("domain"), cmd.get("service")) == RELOAD:
            _only(cmd, {"domain", "service", "service_data"}, set())
            data = cmd["service_data"]
            if not isinstance(data, dict) or set(data) != {"entry_id"} or not _id(data["entry_id"]):
                raise Refused("paramètres de reload_config_entry")
            return "admin", "reload"
        if kind in WS_AS_USER:
            return "user", "ws"
        if kind in WS_ADMIN:
            required, optional, valid = WS_ADMIN[kind]
            _only(cmd, required, optional)
            if not valid(cmd):
                raise Refused(f"valeurs de {kind!r}")
            return "admin", kind
        raise Refused(f"commande {kind!r}")
    if ask.get("kind") == "http":
        method, path = ask.get("method"), ask.get("path")
        if not isinstance(path, str):
            raise Refused("chemin")
        if method == "GET" and any(p.fullmatch(path) for p in HTTP_AS_USER):
            return "user", "http"
        if method == "POST" and path == FLOW_START:
            return "admin", "flow_start"
        if method in ("POST", "DELETE") and FLOW_STEP.fullmatch(path):
            return "admin", "flow_post" if method == "POST" else "flow_delete"
        raise Refused(f"{method} {path}")
    raise Refused("demande inconnue")
