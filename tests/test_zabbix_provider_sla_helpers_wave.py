"""zabbix_provider_sla helper functions."""

import json

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_provider_sla import (
    _burst_report_rows,
    _get_hostid_for_device,
    _get_providers_from_limits,
    _iter_burst_links,
    _load_commit_rates,
)


def test_load_commit_rates_errors(tmp_path):
    data, err = _load_commit_rates(str(tmp_path / "nope.json"))
    assert data is None
    bad = tmp_path / "bad.json"
    bad.write_text("[1,2]", encoding="utf-8")
    data2, err2 = _load_commit_rates(str(bad))
    assert data2 is None


def test_iter_burst_and_report_rows():
    cr = {
        "_provider_limits": {"Cogent": 10},
        "H1": {
            "Eth1": {"billing_model": "Burst", "provider": "Cogent", "circuit_id": "C1", "commit_rate_gbps": 10},
            "Eth2": {"billing_model": "Commit", "provider": "X", "circuit_id": "C2"},
        },
    }
    links = list(_iter_burst_links(cr))
    assert len(links) == 1
    assert _get_providers_from_limits(cr) == ["Cogent"]
    rows = _burst_report_rows(cr)
    assert rows[0][0] == "C1"


def test_get_hostid_name_fallback(monkeypatch, zabbix_env):
    calls = []

    def host_get(params):
        calls.append(params)
        if "host" in (params.get("filter") or {}):
            return []
        return [{"hostid": "42"}]

    build_standard_zabbix_mocker().on("host.get", host_get).activate(monkeypatch)
    hid, err = _get_hostid_for_device("https://z/api", "t", "MyHost")
    assert hid == "42"
    assert err is None
    assert len(calls) == 2
