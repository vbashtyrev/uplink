"""Tests for zabbix_provider_aggregate.py."""

from pathlib import Path

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_provider_aggregate import (
    _build_edges_with_keys,
    _ensure_triggers,
    _get_or_create_host,
    _sanitize_provider_name,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_sanitize_provider_name():
    assert _sanitize_provider_name("ER-Telecom") == "ER-Telecom"
    assert _sanitize_provider_name("Foo/Bar") == "Foo Bar"
    assert _sanitize_provider_name("") == ""


def test_build_edges_with_keys():
    devices = {
        "host1": [
            {"name": "ae5", "description": "Uplink: ISP", "isLag": True},
            {"name": "ae5.0", "description": "Uplink: ISP", "isLogical": True},
        ],
    }
    items = {
        ("host1", "ae5.0"): {"bits_in": "net.if.in[1]", "bits_out": "net.if.out[1]"},
    }
    edges = _build_edges_with_keys(devices, {"host1": "101"}, items, {"Uplink: ISP": "ISP"})
    assert len(edges) == 1
    assert edges[0][0] == "host1"
    assert edges[0][1] == "ISP"
    assert edges[0][2] == "net.if.in[1]"


def test_get_or_create_host_existing(monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on("hostgroup.get", lambda p: [{"groupid": "5"}])
        .on("host.get", lambda p: [{"hostid": "99", "host": "Uplinks_Cogent"}])
    )
    mocker.activate(monkeypatch)
    hostid, err = _get_or_create_host("https://z.example/api_jsonrpc.php", "t", "Uplinks Cogent", "Uplinks")
    assert err is None
    assert hostid == "99"
    assert "host.create" not in mocker.method_names()


def test_get_or_create_host_creates(monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on("hostgroup.get", lambda p: [{"groupid": "5"}])
        .on("host.get", lambda p: [])
        .on("host.create", lambda p: {"hostids": ["100"]})
    )
    mocker.activate(monkeypatch)
    hostid, err = _get_or_create_host("https://z.example/api_jsonrpc.php", "t", "Uplinks Cogent", "Uplinks")
    assert err is None
    assert hostid == "100"


def test_ensure_triggers_create_and_dependency(monkeypatch):
    trigger_updates = []
    trigger_creates = []

    def trigger_get(params):
        return []

    def trigger_create(params):
        trigger_creates.append(params)
        return {"triggerids": ["t{}".format(len(trigger_creates))]}

    def trigger_update(params):
        trigger_updates.append(params)
        return True

    mocker = (
        ZabbixRpcMocker()
        .on("trigger.get", trigger_get)
        .on("trigger.create", trigger_create)
        .on("trigger.update", trigger_update)
    )
    mocker.activate(monkeypatch)

    err = _ensure_triggers(
        "https://z.example/api_jsonrpc.php",
        "t",
        "50",
        "Uplinks_Cogent",
        "Cogent",
        "item-in",
        10_000_000_000,
    )
    assert err is None
    assert len(trigger_creates) == 3
    dep_updates = [u for u in trigger_updates if u.get("dependencies")]
    assert dep_updates
