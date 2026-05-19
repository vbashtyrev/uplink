"""Push zabbix_uplinks_dashboard to >=85%."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
import zabbix_uplinks_dashboard as dash

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _edge():
    return ("ALA-1", "101", "Eth1", "Cogent", "1001", "1002", True, False, False)


def test_get_providers_netbox_debug(capsys, monkeypatch):
    nb = MagicMock()
    nb.circuits.providers.filter.return_value = [type("P", (), {"name": "Cogent"})()]
    with patch("zabbix_uplinks_dashboard.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        names = dash._get_providers_from_netbox("automatization", debug=True)
    assert names == ["Cogent"]
    assert "Cogent" in capsys.readouterr().err


def test_create_or_update_dashboard_update_debug(monkeypatch, capsys):
    (
        ZabbixRpcMocker()
        .on(
            "dashboard.get",
            lambda p: [{"dashboardid": "10", "name": "Uplinks"}],
        )
        .on("dashboard.update", lambda p: True)
        .activate(monkeypatch)
    )
    did, err = dash.create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        [_edge()],
        "Uplinks",
        debug=True,
    )
    assert err is None
    assert did == "10"
    assert "updated" in capsys.readouterr().err


def test_create_or_update_dashboard_create_debug(monkeypatch, capsys):
    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["11"]})
        .activate(monkeypatch)
    )
    did, err = dash.create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        [_edge()],
        "Uplinks",
        debug=True,
    )
    assert err is None
    assert did == "11"
    assert "created" in capsys.readouterr().err


def test_create_or_update_no_items_error(monkeypatch):
    edge = ("ALA-1", "101", "Eth1", "Cogent", "", "", True, False, False)
    did, err = dash.create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        [edge],
        "Uplinks",
    )
    assert did is None
    assert "no interfaces" in err


def test_create_or_update_dashboard_get_error(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: (_ for _ in ()).throw(RuntimeError("dash get")))
        .activate(monkeypatch)
    )
    did, err = dash.create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php",
        "t",
        [_edge()],
        "Uplinks",
    )
    assert did is None
    assert "dashboard.get:" in err


def test_create_dashboard_by_location_create_debug(monkeypatch, capsys):
    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["22"]})
        .activate(monkeypatch)
    )
    did, err = dash.create_dashboard_by_location(
        "https://z.example/api_jsonrpc.php",
        "t",
        [_edge()],
        "By location",
        debug=True,
    )
    assert err is None
    assert did == "22"
    assert "created" in capsys.readouterr().err


def test_main_no_zabbix_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ZABBIX_URL", raising=False)
    monkeypatch.delenv("ZABBIX_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_uplinks_dashboard.py",
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--no-cache",
        ],
    )
    with pytest.raises(SystemExit):
        dash.main()
