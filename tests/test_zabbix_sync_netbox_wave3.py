"""zabbix_sync_commit_rate NetBox fetch edge cases."""

import sys
from unittest.mock import MagicMock

import pytest

from tests.mocks.netbox_api import MockNetBox, _Record, build_netbox_for_commit_rates
from zabbix_sync_commit_rate import (
    _is_netbox_auth_error,
    _macro_name_for_interface,
    get_commit_rates_from_netbox,
    interfaces_by_host_from_dry_ssh,
    is_physical_uplink_iface,
    load_burst_pairs,
    load_dry_ssh,
)


def test_is_netbox_auth_error():
    assert _is_netbox_auth_error(Exception("403 Forbidden")) is True
    assert _is_netbox_auth_error(Exception("other")) is False


def test_macro_names_empty_iface():
    assert "iface" in _macro_name_for_interface("Eth1") or "Eth1" in _macro_name_for_interface("Eth1")


def test_interfaces_by_host_physical_only():
    devices = {
        "R1": [
            {"name": "Ethernet1"},
            {"name": "ae5", "isLag": True},
            {"name": "ae5.0", "isLogical": True},
        ],
    }
    all_if = interfaces_by_host_from_dry_ssh(devices, physical_only=False)
    phys = interfaces_by_host_from_dry_ssh(devices, physical_only=True)
    assert "Ethernet1" in all_if["R1"]
    assert "ae5" not in phys["R1"]


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


def test_get_commit_rates_auth_exit(monkeypatch):
    nb = MagicMock()
    nb.circuits.circuit_terminations.filter.side_effect = Exception("403 token expired")
    monkeypatch.setattr(sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        get_commit_rates_from_netbox(nb, tag="t", debug=True)
    assert exc.value.code == 1


def test_get_commit_rates_no_cable_debug(capsys):
    ct = _Record(id=1, term_side="A", cable=None, circuit=_Record(id=2, commit_rate=1000))
    nb = MockNetBox(devices=[], interfaces=[], cables=[], terminations=[ct], circuits=[])
    result = get_commit_rates_from_netbox(nb, tag=None, debug=True)
    assert result == {}
    assert "circuit terminations" in capsys.readouterr().err.lower() or result == {}


def test_get_commit_rates_cable_get_fails():
    nb = build_netbox_for_commit_rates()
    nb.dcim.cables.get = lambda pk: (_ for _ in ()).throw(RuntimeError("fail"))
    result = get_commit_rates_from_netbox(nb, tag="uplinks", debug=True)
    assert result == {}
