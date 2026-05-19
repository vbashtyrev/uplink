"""zabbix_provider_sla: aggregate triggers and SLA computation."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX
from zabbix_provider_sla import (
    _compute_sla_from_events,
    _get_aggregate_triggers,
    _load_events_for_trigger,
)


def test_get_aggregate_trigger_ids(monkeypatch):
    agg = UPLINKS_AGGREGATE_HOST_PREFIX + "Cogent"
    (
        ZabbixRpcMocker()
        .on(
            "host.get",
            lambda p: [{"hostid": "50", "host": agg, "name": agg}],
        )
        .on(
            "trigger.get",
            lambda p: [
                {
                    "triggerid": "1",
                    "description": "Provider aggregate traffic >= 90% of limit",
                    "hosts": [{"hostid": "50"}],
                },
                {
                    "triggerid": "2",
                    "description": "Provider aggregate traffic >= 100% of limit",
                    "hosts": [{"hostid": "50"}],
                },
                {
                    "triggerid": "3",
                    "description": "Provider aggregate SLA breach: Cogent",
                    "hosts": [{"hostid": "50"}],
                },
            ],
        )
        .activate(monkeypatch)
    )
    out = _get_aggregate_triggers(
        "https://z.example/api_jsonrpc.php", "t", ["Cogent"]
    )
    assert out["Cogent"][0] == "1"
    assert out["Cogent"][1] == "2"
    assert out["Cogent"][2] == "3"


def test_compute_sla_from_events():
    events = [(100, 1), (200, 0), (300, 1), (400, 0)]
    total, problem = _compute_sla_from_events(events, 0, 500)
    assert total == 500
    assert problem == 200


def test_load_events_for_trigger(monkeypatch):
    (
        ZabbixRpcMocker()
        .on(
            "event.get",
            lambda p: [
                {"clock": "100", "value": "1"},
                {"clock": "200", "value": "0"},
            ],
        )
        .activate(monkeypatch)
    )
    ev = _load_events_for_trigger("https://z.example/api_jsonrpc.php", "t", "99", 0, 1000)
    assert ev == [(100, 1), (200, 0)]
