"""zabbix_sync utilization triggers and sync_uplink_utilization_for_host."""

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_sync_commit_rate import (
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    ensure_util_crit_trigger,
    ensure_util_warn_trigger,
    sync_uplink_utilization_for_host,
)


def _iface_items():
    def item_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search.startswith("Interface "):
            iface = search.replace("Interface ", "").strip()
            return [
                {"key_": "net.if.in[1]", "name": "Interface {}: Bits received".format(iface)},
                {"key_": "net.if.out[1]", "name": "Interface {}: Bits sent".format(iface)},
                {"key_": "net.if.speed[1]", "name": "Interface {}: Speed".format(iface)},
            ]
        return []

    return item_get


def test_ensure_util_crit_create(monkeypatch, zabbix_env):
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", _iface_items()).on("trigger.get", lambda p: []).on(
        "trigger.create", lambda p: {"triggerids": ["10"]}
    ).activate(monkeypatch)
    ok, err = ensure_util_crit_trigger("https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1")
    assert ok is True


def test_ensure_util_warn_update_with_crit_dep(monkeypatch, zabbix_env):
    crit = {
        "triggerid": "99",
        "description": "Interface Eth1: {}".format(TRIGGER_DESC_UTIL_CRIT_SUFFIX),
    }
    warn = {
        "triggerid": "51",
        "description": "Interface Eth1: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX),
    }

    def trigger_get(params):
        return [warn, crit]

    updates = []
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", _iface_items()).on("trigger.get", trigger_get).on(
        "trigger.update", lambda p: updates.append(p) or True
    ).activate(monkeypatch)
    ok, err = ensure_util_warn_trigger("https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1")
    assert ok is True
    assert updates


def test_sync_uplink_utilization_for_host(monkeypatch, zabbix_env):
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", _iface_items()).on("trigger.get", lambda p: []).on(
        "trigger.create", lambda p: {"triggerids": ["1"]}
    ).on("usermacro.get", lambda p: []).on("usermacro.create", lambda p: {"hostmacroids": ["1"]})
    mocker.activate(monkeypatch)
    macros, triggers, errors = sync_uplink_utilization_for_host(
        "https://z/api_jsonrpc.php", "t", "host1", "101", ["Eth1", "Eth2"]
    )
    assert macros == 4
    assert triggers >= 1
    assert not errors
