"""zabbix_sync_commit_rate NetBox fetch edge cases."""

import sys
from unittest.mock import MagicMock

import pytest

from tests.mocks.netbox_api import MockNetBox, _Record, build_netbox_for_commit_rates, wire_inventory_collector
from zabbix_sync_commit_rate import (
    _is_netbox_auth_error,
    _macro_name_for_interface,
    commit_rates_from_inventory_report,
    fetch_uplink_inventory_report,
    is_physical_uplink_iface,
    load_burst_pairs,
    load_dry_ssh,
)


def test_is_netbox_auth_error():
    assert _is_netbox_auth_error(Exception("403 Forbidden")) is True
    assert _is_netbox_auth_error(Exception("401 Unauthorized")) is True
    assert _is_netbox_auth_error(Exception("other")) is False


def test_macro_names_empty_iface():
    assert "iface" in _macro_name_for_interface("Eth1") or "Eth1" in _macro_name_for_interface("Eth1")


def test_is_physical_uplink_iface():
    assert is_physical_uplink_iface({"name": "ae5", "isLag": True}) is False
    assert is_physical_uplink_iface({"name": "Ethernet1"}) is True
    assert is_physical_uplink_iface("notdict") is True


def test_load_dry_ssh_missing(tmp_path):
    assert load_dry_ssh(str(tmp_path / "nope.json")) is None


def test_load_burst_pairs(tmp_path):
    p = tmp_path / "cr.json"
    p.write_text('{"H": {"Eth1": {"billing_model": "Burst"}}}', encoding="utf-8")
    pairs = load_burst_pairs(str(p))
    assert ("H", "Eth1") in pairs


def test_fetch_inventory_auth_exit(monkeypatch):
    nb = MagicMock()
    nb.circuits.providers.all.side_effect = Exception("403 token expired")
    monkeypatch.setattr(sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        fetch_uplink_inventory_report(nb, tag="t", debug=True)
    assert exc.value.code == 1


def test_fetch_inventory_auth_on_devices_filter_exit(monkeypatch):
    nb = build_netbox_for_commit_rates()
    nb.dcim.devices.filter = lambda **kw: (_ for _ in ()).throw(Exception("403 forbidden"))
    monkeypatch.setattr(sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        fetch_uplink_inventory_report(nb, tag="border", debug=True)
    assert exc.value.code == 1


def test_fetch_inventory_no_cable_debug(capsys):
    circuit = _Record(id=2, cid="CKT-2", commit_rate=1000, status="active", provider_id=1)
    ct = _Record(id=1, term_side="A", cable=None, circuit=circuit, circuit_id=2)
    nb = MockNetBox(devices=[], interfaces=[], cables=[], terminations=[ct], circuits=[circuit])
    wire_inventory_collector(nb)
    report = fetch_uplink_inventory_report(nb, tag=None, debug=True)
    result = commit_rates_from_inventory_report(report, debug=True)
    assert result == {}
    assert len(report.get("incomplete") or []) == 1
    assert report["incomplete"][0]["reason"] == "no_cable"
    err = capsys.readouterr().err.lower()
    assert "incomplete" in err


def test_fetch_inventory_cable_get_fails():
    nb = build_netbox_for_commit_rates()
    nb.dcim.cables.get = lambda pk: (_ for _ in ()).throw(RuntimeError("fail"))
    report = fetch_uplink_inventory_report(nb, tag="border", debug=True)
    result = commit_rates_from_inventory_report(report, debug=True)
    assert result == {}
    assert report["stats"]["read_errors"] >= 1
    assert report["stats"].get("error") == "partial_read"
    assert len(report.get("incomplete") or []) >= 1
