"""Zabbix sync: util/link triggers, delete, ensure_* with mocked API."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import (
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_90_SUFFIX,
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
)
from zabbix_sync_commit_rate import (
    delete_link_triggers,
    delete_util_triggers,
    ensure_burst_sla_breach_trigger,
    ensure_simple_threshold_trigger,
    ensure_simple_warn_trigger,
    ensure_util_crit_trigger,
    ensure_util_warn_trigger,
    prune_util_triggers_on_host,
    sync_uplink_utilization_for_host,
)


def _our_tag():
    return {"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}


def test_delete_util_triggers(monkeypatch):
    triggers = [
        {
            "triggerid": "1",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX),
            "tags": [_our_tag()],
        },
        {
            "triggerid": "2",
            "description": "Interface Eth1: other",
            "tags": [_our_tag()],
        },
    ]
    deleted = []
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )
    assert delete_util_triggers("https://z.example/api_jsonrpc.php", "t") == 1
    assert deleted == ["1"]


def test_delete_link_triggers_burst_and_legacy(monkeypatch):
    triggers = [
        {
            "triggerid": "10",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX),
            "tags": [_our_tag()],
        },
        {
            "triggerid": "11",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_SLA_BREACH_SUFFIX),
            "tags": [_our_tag()],
        },
    ]
    deleted = []
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )
    assert delete_link_triggers("https://z.example/api_jsonrpc.php", "t") == 2


def test_prune_util_triggers_on_host(monkeypatch):
    triggers = [
        {
            "triggerid": "20",
            "description": "Interface ae5: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX),
            "tags": [_our_tag()],
        },
        {
            "triggerid": "21",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX),
            "tags": [_our_tag()],
        },
    ]
    deleted = []
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )
    n = prune_util_triggers_on_host("https://z.example/api_jsonrpc.php", "t", "50", ["Eth1"])
    assert n == 1
    assert deleted == ["20"]


def _item_handlers_for_util():
    def item_get(params):
        search = (params.get("search") or {}).get("name", "")
        if search == "Bits received":
            return [{"key_": "net.if.in[1]", "name": "Interface Eth1(x): Bits received"}]
        if search.startswith("Interface "):
            return [
                {"key_": "net.if.in[1]", "name": "Interface Eth1(x): ..."},
                {"key_": "net.if.out[1]", "name": "Interface Eth1(x): ..."},
                {"key_": "net.if.speed[1]", "name": "Interface Eth1(x): ..."},
            ]
        return []

    return item_get


def test_ensure_util_crit_trigger_create(monkeypatch):
    created = []

    def trigger_get(params):
        return []

    mocker = (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_for_util())
        .on("trigger.get", trigger_get)
        .on("trigger.create", lambda p: created.append(p) or {"triggerids": ["tc1"]})
    )
    mocker.activate(monkeypatch)

    ok, err = ensure_util_crit_trigger("https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1")
    assert ok is True
    assert err is None
    assert created
    assert TRIGGER_DESC_UTIL_CRIT_SUFFIX in created[0]["description"]


def test_ensure_util_warn_trigger_with_dependency(monkeypatch):
    def trigger_get(params):
        search = (params.get("search") or {}).get("description", "")
        if TRIGGER_DESC_UTIL_CRIT_SUFFIX in str(params):
            return [{"triggerid": "crit1", "description": "Interface Eth1: {}".format(TRIGGER_DESC_UTIL_CRIT_SUFFIX)}]
        return []

    mocker = (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_for_util())
        .on(
            "trigger.get",
            lambda p: [{"triggerid": "crit1", "description": "Interface Eth1: {}".format(TRIGGER_DESC_UTIL_CRIT_SUFFIX)}]
            if "hostids" in p
            else [],
        )
        .on("trigger.create", lambda p: {"triggerids": ["tw1"]})
    )
    mocker.activate(monkeypatch)

    ok, err = ensure_util_warn_trigger("https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1")
    assert ok is True
    assert err is None


def test_ensure_simple_threshold_trigger_update(monkeypatch):
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    updated = []

    mocker = (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_for_util())
        .on(
            "trigger.get",
            lambda p: [{"triggerid": "h1", "description": desc100, "priority": "0", "status": "0", "expression": "old"}],
        )
        .on("trigger.update", lambda p: updated.append(p) or True)
    )
    mocker.activate(monkeypatch)

    ok, err = ensure_simple_threshold_trigger("https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1")
    assert ok is True
    assert updated


def test_ensure_simple_warn_trigger_create(monkeypatch):
    created = []
    mocker = (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_for_util())
        .on("trigger.get", lambda p: [])
        .on("trigger.create", lambda p: created.append(p) or {"triggerids": ["w1"]})
    )
    mocker.activate(monkeypatch)

    ok, err = ensure_simple_warn_trigger("https://z.example/api_jsonrpc.php", "t", "host1", "50", "Eth1")
    assert ok is True
    assert TRIGGER_DESC_90_SUFFIX in created[0]["description"]


def test_ensure_burst_sla_breach_trigger(monkeypatch):
    created = []
    (
        ZabbixRpcMocker()
        .on("item.get", _item_handlers_for_util())
        .on("trigger.get", lambda p: [])
        .on("trigger.create", lambda p: created.append(p) or {"triggerids": ["sla1"]})
        .activate(monkeypatch)
    )
    ok, err = ensure_burst_sla_breach_trigger(
        "https://z.example/api_jsonrpc.php",
        "t",
        "host1",
        "50",
        "Eth1",
        link_tags=[_our_tag(), {"tag": "sla", "value": "true"}],
    )
    assert ok is True
    assert TRIGGER_DESC_SLA_BREACH_SUFFIX in created[0]["description"]


def test_sync_uplink_utilization_for_host(monkeypatch):
    macro_deleted = []
    trigger_deleted = []

    def usermacro_get(params):
        if "search" in (params or {}):
            return []
        return []

    mocker = (
        ZabbixRpcMocker()
        .on("usermacro.get", usermacro_get)
        .on("usermacro.create", lambda p: {"hostmacroids": ["1"]})
        .on("trigger.get", lambda p: [])
        .on("item.get", _item_handlers_for_util())
        .on("trigger.create", lambda p: {"triggerids": ["1"]})
        .on("trigger.delete", lambda p: trigger_deleted.extend(p) or True)
        .on("usermacro.delete", lambda p: macro_deleted.extend(p) or True)
    )
    mocker.activate(monkeypatch)

    n_macros, n_triggers, errors = sync_uplink_utilization_for_host(
        "https://z.example/api_jsonrpc.php",
        "t",
        "host1",
        "50",
        ["Eth1"],
        dry_run=False,
    )
    assert n_macros == 2
    assert n_triggers == 1
    assert errors == []
