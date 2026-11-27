"""Liste autorisée de l'intégration : `python3 tests/test_allowlist.py` (ou pytest), sans Home Assistant."""

import importlib.util
import pathlib

_path = pathlib.Path(__file__).parents[1] / "custom_components/yuki_family/allowlist.py"
_spec = importlib.util.spec_from_file_location("allowlist", _path)
allowlist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(allowlist)


def ws(**cmd):
    return {"kind": "ws", "cmd": cmd}


def http(method, path):
    return {"kind": "http", "method": method, "path": path}


def refused(ask):
    try:
        allowlist.classify(ask)
    except allowlist.Refused:
        return True
    return False


def test_allowed():
    assert allowlist.classify(ws(type="get_states")) == ("user", "ws")
    assert allowlist.classify(ws(type="call_service", domain="light", service="turn_on", target={"entity_id": "light.x"})) == ("user", "ws")
    assert allowlist.classify(ws(type="recorder/statistics_during_period", start_time="x", statistic_ids=[], period="day")) == ("user", "ws")
    reload = ws(type="call_service", domain="homeassistant", service="reload_config_entry", service_data={"entry_id": "abc"})
    assert allowlist.classify(reload) == ("admin", "reload")
    assert allowlist.classify(ws(type="config/device_registry/update", device_id="d1", name_by_user="Frigo")) == ("admin", "config/device_registry/update")
    assert allowlist.classify(ws(type="config/device_registry/update", device_id="d1", area_id=None))[0] == "admin"
    assert allowlist.classify(ws(type="config/area_registry/create", name="Cave"))[0] == "admin"
    assert allowlist.classify(ws(type="config/entity_registry/update", entity_id="device_tracker.tel", disabled_by=None))[0] == "admin"
    assert allowlist.classify(ws(type="automation/config", entity_id="automation.pompe"))[0] == "admin"
    assert allowlist.classify(http("GET", "/api/camera_proxy/camera.jardin")) == ("user", "http")
    assert allowlist.classify(http("GET", "/api/config/config_entries/flow_handlers?type=integration")) == ("admin", "brands")
    assert allowlist.classify(http("POST", "/api/config/config_entries/flow")) == ("admin", "flow_start")
    assert allowlist.classify(http("POST", "/api/config/config_entries/flow/01JABC")) == ("admin", "flow_post")
    assert allowlist.classify(http("DELETE", "/api/config/config_entries/flow/01JABC")) == ("admin", "flow_delete")


def test_refused():
    for ask in (
        ws(type="config/auth/create", name="x"),
        ws(type="auth/long_lived_access_token", client_name="x", lifespan=3650),
        ws(type="config_entries/delete", entry_id="x"),
        ws(type="render_template", template="{{ states }}"),
        ws(type="subscribe_events"),
        ws(),
        {"kind": "ws", "cmd": "get_states"},
        ws(type="call_service", domain="homeassistant", service="reload_config_entry", service_data={"entry_id": "a", "x": 1}),
        ws(type="call_service", domain="homeassistant", service="reload_config_entry", service_data={"entry_id": "a"}, target={}),
        ws(type="config/device_registry/update", device_id="d1"),
        ws(type="config/device_registry/update", device_id="d1", disabled_by="user"),
        ws(type="config/device_registry/update", device_id="d1", name_by_user=""),
        ws(type="config/device_registry/update", device_id="d1", name_by_user="x" * 101),
        ws(type="config/area_registry/create", name="Cave", icon="mdi:x"),
        ws(type="config/entity_registry/update", entity_id="device_tracker.tel", disabled_by="user"),
        ws(type="config/entity_registry/update", entity_id="lock.porte", disabled_by=None),
        ws(type="config/entity_registry/update", entity_id="device_tracker.tel", disabled_by=None, new_entity_id="x"),
        ws(type="automation/config", entity_id="script.x"),
        http("GET", "/api/states"),
        http("POST", "/api/services/homeassistant/restart"),
        http("GET", "/api/camera_proxy/camera.jardin/../../config"),
        http("POST", "/api/camera_proxy/camera.jardin"),
        http("GET", "/api/config/config_entries/flow_handlers?type=helper"),
        http("GET", "/api/config/config_entries/flow/01JABC"),
        http("PUT", "/api/config/config_entries/flow/01JABC"),
        http("DELETE", "/api/config/config_entries/entry/01JABC"),
        http("GET", None),
        {"kind": "autre"},
    ):
        assert refused(ask), ask


def test_limits():
    # automatisation : la description seulement, jamais ses actions (webhooks, jetons…)
    raw = {"id": "1", "description": "Pompe l'été", "triggers": [{"trigger": "webhook", "webhook_id": "secret"}], "actions": [{"x": "jeton"}]}
    assert allowlist.automation_view(raw) == {"description": "Pompe l'été"}
    assert allowlist.automation_view({"triggers": []}) == {"description": ""}
    # renommer / ranger : seulement un appareil d'une intégration ajoutée par Yuki
    assert allowlist.owned({"tuya1", "yuki1"}, {"yuki1"})
    assert not allowlist.owned({"tuya1"}, {"yuki1"})
    assert not allowlist.owned({"tuya1"}, set())
    # réactiver : seulement ce que l'intégration a désactivé
    assert allowlist.REENABLE_FROM == "integration"


if __name__ == "__main__":
    test_allowed()
    test_refused()
    test_limits()
    print("liste autorisée : OK")
