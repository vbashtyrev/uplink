"""zabbix_uplinks_cleanup: apply paths, delete errors, validation."""

import sys

import pytest

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import (
    TRIGGER_DESC_90_SUFFIX,
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
)
from zabbix_uplinks_cleanup import (
    LEGACY_TRIGGER_DESC_90_SUFFIX,
    _validate_zabbix,
    cleanup_dashboards,
    cleanup_map,
    cleanup_threshold_items,
    cleanup_triggers,
)


def test_validate_zabbix_invalid_token(monkeypatch, zabbix_env):
    monkeypatch.setattr(
        "zabbix_uplinks_cleanup.validate_zabbix_token", lambda *a, **k: False
    )
    with pytest.raises(SystemExit):
        _validate_zabbix()


def test_cleanup_threshold_items_apply(monkeypatch):
    deleted = []
    mocker = (
        ZabbixRpcMocker()
        .on(
            "item.get",
            lambda p: [{"itemid": "1", "key_": 'net.if.threshold["Eth1"]'}],
        )
        .on("item.delete", lambda p: deleted.extend(p) or True)
    )
    mocker.activate(monkeypatch)
    n = cleanup_threshold_items("https://z.example/api_jsonrpc.php", "t", dry_run=False)
    assert n == 1
    assert deleted == ["1"]


def test_cleanup_threshold_items_delete_error(capsys, monkeypatch):
    mocker = (
        ZabbixRpcMocker()
        .on(
            "item.get",
            lambda p: [{"itemid": "2", "key_": 'net.if.threshold["Eth2"]'}],
        )
        .on("item.delete", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    mocker.activate(monkeypatch)
    assert cleanup_threshold_items("https://z.example/api_jsonrpc.php", "t") == 0
    assert "delete error" in capsys.readouterr().err


def test_cleanup_triggers_legacy_and_util_suffixes(monkeypatch):
    triggers = [
        {
            "triggerid": "20",
            "description": "Interface Eth1: {}".format(LEGACY_TRIGGER_DESC_90_SUFFIX),
            "tags": [],
        },
        {
            "triggerid": "21",
            "description": "Interface Eth2: {}".format(TRIGGER_DESC_UTIL_CRIT_SUFFIX),
            "tags": [],
        },
        {
            "triggerid": "22",
            "description": "Interface Eth3: {}".format(TRIGGER_DESC_SLA_BREACH_SUFFIX),
            "tags": [{"tag": "other", "value": "x"}],
        },
    ]
    deleted = []
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: deleted.extend(p) or True)
        .activate(monkeypatch)
    )
    n = cleanup_triggers("https://z.example/api_jsonrpc.php", "t", dry_run=False)
    assert n == 2
    assert set(deleted) == {"20", "21"}


def test_cleanup_triggers_skip_wrong_tag(monkeypatch):
    triggers = [
        {
            "triggerid": "30",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX),
            "tags": [{"tag": "scripts", "value": "manual"}],
        },
    ]
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: True)
        .activate(monkeypatch)
    )
    assert cleanup_triggers("https://z.example/api_jsonrpc.php", "t") == 0


def test_cleanup_triggers_delete_error(capsys, monkeypatch):
    triggers = [
        {
            "triggerid": "40",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX),
            "tags": [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}],
        },
    ]
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: (_ for _ in ()).throw(RuntimeError("trig err")))
        .activate(monkeypatch)
    )
    assert cleanup_triggers("https://z.example/api_jsonrpc.php", "t") == 0
    assert "trigger.delete error" in capsys.readouterr().err


def test_cleanup_map_delete_error(capsys, monkeypatch):
    (
        ZabbixRpcMocker()
        .on("map.get", lambda p: [{"sysmapid": "7", "name": "Uplinks"}])
        .on("map.delete", lambda p: (_ for _ in ()).throw(RuntimeError("map err")))
        .activate(monkeypatch)
    )
    assert cleanup_map("https://z.example/api_jsonrpc.php", "t") == 0
    assert "map.delete error" in capsys.readouterr().err


def test_cleanup_dashboards_apply_and_error(capsys, monkeypatch):
    assert cleanup_dashboards("https://z.example/api_jsonrpc.php", "t", []) == 0
    (
        ZabbixRpcMocker()
        .on(
            "dashboard.get",
            lambda p: [{"dashboardid": "3", "name": "Uplinks"}],
        )
        .on("dashboard.delete", lambda p: True)
        .activate(monkeypatch)
    )
    assert cleanup_dashboards("https://z.example/api_jsonrpc.php", "t", ["Uplinks"]) == 1

    (
        ZabbixRpcMocker()
        .on(
            "dashboard.get",
            lambda p: [{"dashboardid": "4", "name": "Uplinks"}],
        )
        .on("dashboard.delete", lambda p: (_ for _ in ()).throw(RuntimeError("dash err")))
        .activate(monkeypatch)
    )
    assert cleanup_dashboards("https://z.example/api_jsonrpc.php", "t", ["Uplinks"]) == 0
    assert "dashboard.delete error" in capsys.readouterr().err


def test_main_completed(monkeypatch, zabbix_env, capsys):
    import zabbix_uplinks_cleanup as mod

    items = [{"itemid": "1", "key_": 'net.if.threshold["Eth1"]'}]
    triggers = [
        {
            "triggerid": "10",
            "description": "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX),
            "tags": [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}],
        },
    ]
    (
        ZabbixRpcMocker()
        .on("item.get", lambda p: items)
        .on("item.delete", lambda p: True)
        .on("trigger.get", lambda p: triggers)
        .on("trigger.delete", lambda p: True)
        .on("map.get", lambda p: [{"sysmapid": "1", "name": "Uplinks"}])
        .on("map.delete", lambda p: True)
        .on(
            "dashboard.get",
            lambda p: [{"dashboardid": "2", "name": "Uplinks"}],
        )
        .on("dashboard.delete", lambda p: True)
        .activate(monkeypatch)
    )
    monkeypatch.setattr(sys, "argv", ["zabbix_uplinks_cleanup.py"])
    mod.main()
    out = capsys.readouterr().out
    assert "completed:" in out
