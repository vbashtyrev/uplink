"""Direct tests for ensure_* trigger helpers in zabbix_sync_commit_rate."""

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_sync_commit_rate import (
    LEGACY_TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_90_SUFFIX,
    ensure_burst_sla_breach_trigger,
    ensure_simple_threshold_trigger,
    ensure_simple_warn_trigger,
    ensure_util_crit_trigger,
    ensure_util_warn_trigger,
)


def _bits_item():
    def item_get(params):
        if (params.get("search") or {}).get("name") == "Bits received":
            return [{"key_": "net.if.in[Eth1]"}]
        return []

    return item_get


def test_ensure_threshold_creates_trigger(monkeypatch, zabbix_env):
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", _bits_item()).on("trigger.get", lambda p: []).on(
        "trigger.create", lambda p: {"triggerids": ["100"]}
    ).activate(monkeypatch)
    ok, err = ensure_simple_threshold_trigger(
        "https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1"
    )
    assert ok is True
    assert err is None


def test_ensure_threshold_updates_existing(monkeypatch, zabbix_env):
    existing = [{
        "triggerid": "50",
        "description": "Interface Eth1: {}".format(LEGACY_TRIGGER_DESC_100_SUFFIX),
        "priority": "0",
        "status": "1",
        "expression": "old",
    }]
    updates = []
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", _bits_item()).on("trigger.get", lambda p: existing).on(
        "trigger.update", lambda p: updates.append(p) or True
    ).activate(monkeypatch)
    ok, err = ensure_simple_threshold_trigger(
        "https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1", link_tags=[{"tag": "x"}]
    )
    assert ok is True
    assert updates
    assert updates[0].get("description", "").endswith(TRIGGER_DESC_100_SUFFIX)


def test_ensure_warn_with_dependency(monkeypatch, zabbix_env):
    high = {
        "triggerid": "99",
        "description": "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX),
    }
    warn = {
        "triggerid": "51",
        "description": "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX),
        "status": "1",
        "expression": "old",
    }

    def trigger_get(params):
        return [warn, high]

    updates = []
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", _bits_item()).on("trigger.get", trigger_get).on(
        "trigger.update", lambda p: updates.append(p) or True
    ).activate(monkeypatch)
    ok, err = ensure_simple_warn_trigger(
        "https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1"
    )
    assert ok is True
    assert any("dependencies" in u for u in updates)


def test_ensure_burst_sla_creates(monkeypatch, zabbix_env):
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", _bits_item()).on("trigger.get", lambda p: []).on(
        "trigger.create", lambda p: {"triggerids": ["200"]}
    ).activate(monkeypatch)
    ok, err = ensure_burst_sla_breach_trigger(
        "https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1"
    )
    assert ok is True


def test_ensure_util_triggers_no_items(monkeypatch, zabbix_env):
    mocker = build_standard_zabbix_mocker()
    mocker.on("item.get", lambda p: []).activate(monkeypatch)
    ok, err = ensure_util_crit_trigger("https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1")
    assert ok is False
    ok2, err2 = ensure_util_warn_trigger("https://z/api_jsonrpc.php", "t", "host1", "101", "Eth1")
    assert ok2 is False
