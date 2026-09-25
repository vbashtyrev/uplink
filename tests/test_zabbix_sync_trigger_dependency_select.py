"""Regression tests for trigger dependency reads during apply."""

import pytest

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import TRIGGER_DESC_100_SUFFIX
from zabbix_sync_commit_rate import (
    ensure_burst_sla_breach_trigger,
    ensure_simple_threshold_trigger,
    ensure_simple_warn_trigger,
)


def _bits_item(params):
    if (params.get("search") or {}).get("name") == "Bits received":
        return [{"key_": 'net.if.in["Eth1"]'}]
    return []


@pytest.mark.parametrize(
    "ensure_trigger",
    [
        ensure_simple_threshold_trigger,
        ensure_simple_warn_trigger,
        ensure_burst_sla_breach_trigger,
    ],
)
def test_apply_dependency_reads_select_dependencies(monkeypatch, ensure_trigger):
    trigger_get_params = []
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)

    def trigger_get(params):
        trigger_get_params.append(params)
        out = params.get("output") or []
        if ensure_trigger is ensure_simple_warn_trigger and out == ["triggerid", "description"]:
            return [{"triggerid": "high1", "description": desc100}]
        return []

    mocker = (
        ZabbixRpcMocker()
        .on("item.get", _bits_item)
        .on("trigger.get", trigger_get)
        .on("trigger.create", lambda params: {"triggerids": ["1"]})
    )
    mocker.activate(monkeypatch)

    ok, err = ensure_trigger(
        "https://z.example/api_jsonrpc.php",
        "t",
        "host1",
        "50",
        "Eth1",
    )

    assert ok is True
    assert err is None
    dependency_reads = [
        params
        for params in trigger_get_params
        if "dependencies" in (params.get("output") or [])
    ]
    assert len(dependency_reads) == 1
    assert dependency_reads[0]["selectDependencies"] == "extend"
