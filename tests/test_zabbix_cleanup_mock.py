"""Tests for zabbix_uplinks_cleanup.py with mocked Zabbix API."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import TRIGGER_DESC_90_SUFFIX, TRIGGER_TAG_NAME, TRIGGER_TAG_VALUE
from zabbix_uplinks_cleanup import (
    _has_our_tag,
    cleanup_dashboards,
    cleanup_map,
    cleanup_threshold_items,
    cleanup_triggers,
)


def test_has_our_tag():
    assert _has_our_tag([{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}]) is True
    assert _has_our_tag([{"tag": "other", "value": "x"}]) is False
    assert _has_our_tag([]) is False


def test_cleanup_threshold_items_dry_run(monkeypatch):
    items = [{"itemid": "1", "key_": 'net.if.threshold["Eth1"]'}]
    ZabbixRpcMocker().on("item.get", lambda p: items).activate(monkeypatch)
    n = cleanup_threshold_items("https://z.example/api_jsonrpc.php", "t", dry_run=True)
    assert n == 1


def test_cleanup_triggers_with_tag(monkeypatch):
    triggers = [
        {
            "triggerid": "10",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX),
            "tags": [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}],
        },
        {
            "triggerid": "11",
            "description": "Interface Eth1: unrelated",
            "tags": [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}],
        },
    ]
    deleted = []
    mocker = (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
    )
    mocker.activate(monkeypatch)
    n = cleanup_triggers("https://z.example/api_jsonrpc.php", "t", dry_run=False)
    assert n == 1
    assert deleted == ["10"]


def test_cleanup_map(monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on("map.get", lambda p: [{"sysmapid": "7", "name": "Uplinks"}])
        .on("map.delete", lambda p: True)
    )
    mocker.activate(monkeypatch)
    n = cleanup_map("https://z.example/api_jsonrpc.php", "t", dry_run=False)
    assert n == 1


def test_cleanup_dashboards_dry_run(monkeypatch):
    ZabbixRpcMocker().on(
        "dashboard.get",
        lambda p: [{"dashboardid": "3", "name": "Uplinks"}],
    ).activate(monkeypatch)
    n = cleanup_dashboards("https://z.example/api_jsonrpc.php", "t", ["Uplinks"], dry_run=True)
    assert n == 1
