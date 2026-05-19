"""Tests for zabbix_provider_sla.py data helpers."""

import json

from zabbix_provider_sla import (
    _burst_report_rows,
    _default_window,
    _get_providers_from_limits,
    _iter_burst_links,
    _load_commit_rates,
    _load_events_for_trigger,
)


def test_provider_sla_load_and_limits(tmp_path):
    path = tmp_path / "cr.json"
    path.write_text(json.dumps({"_provider_limits": {"A": 10, "B": 5}}), encoding="utf-8")
    data, err = _load_commit_rates(str(path))
    assert err is None
    assert _get_providers_from_limits(data) == ["A", "B"]


def test_burst_report_rows_dedupes_circuit():
    data = {
        "h1": {"e1": {"billing_model": "Burst", "provider": "P", "circuit_id": "P-1", "commit_rate_gbps": 10}},
        "h2": {"e2": {"billing_model": "Burst", "provider": "P", "circuit_id": "P-1"}},
    }
    rows = _burst_report_rows(data)
    assert len(rows) == 1
    assert rows[0][0] == "P-1"


def test_default_window():
    t_from, t_to = _default_window(7)
    assert t_to > t_from
    assert t_to - t_from >= 7 * 86400 - 60


def test_load_events_for_trigger(monkeypatch):
    events = [
        {"clock": "100", "value": "1"},
        {"clock": "200", "value": "0"},
    ]

    from tests.mocks.zabbix_rpc import ZabbixRpcMocker

    ZabbixRpcMocker().on("event.get", lambda p: events).activate(monkeypatch)
    out = _load_events_for_trigger("https://z.example/api_jsonrpc.php", "t", "99", 0, 1000)
    assert out == [(100, 1), (200, 0)]
