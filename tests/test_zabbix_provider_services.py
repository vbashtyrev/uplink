"""Tests for zabbix_provider_services.py pure helpers."""

import json

from zabbix_provider_services import (
    _burst_circuits_unique,
    _get_global_provider_sla,
    _get_providers_from_limits,
    _iter_burst_links,
    _load_commit_rates,
)


def test_load_commit_rates(tmp_path):
    path = tmp_path / "cr.json"
    path.write_text('{"_provider_limits": {"P": 1}}', encoding="utf-8")
    data, err = _load_commit_rates(str(path))
    assert err is None
    assert data["_provider_limits"]["P"] == 1


def test_get_providers_from_limits():
    data = {"_provider_limits": {"Cogent": 10, "": None, "HE": 5}}
    assert _get_providers_from_limits(data) == ["Cogent", "HE"]


def test_iter_burst_links_and_unique():
    data = {
        "h1": {
            "e1": {"billing_model": "Burst", "provider": "P", "circuit_id": "P-1"},
            "e2": {"billing_model": "Flat", "provider": "P", "circuit_id": "P-2"},
        },
        "h2": {
            "e3": {"billing_model": "burst", "provider": "P", "circuit_id": "P-1"},
        },
    }
    links = list(_iter_burst_links(data))
    assert len(links) == 2
    unique = _burst_circuits_unique(data)
    assert unique == [("P-1", "P")]


def test_get_global_provider_sla():
    assert _get_global_provider_sla({"_provider_sla": 99.95}) == 99.95
    assert _get_global_provider_sla({}) is None
