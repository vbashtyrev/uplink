"""zabbix_uplinks_dashboard provider list and edges from NetBox inventory."""

import json
import sys
from unittest.mock import patch

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import PROVIDERS_FOR_SUMMARY
from zabbix_uplinks_dashboard import (
    _build_edges,
    load_uplink_provider_context,
    create_dashboard_by_provider,
)


def test_build_edges_prefers_inventory_over_description_map():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Uplink: Cogent 10G"},
        ],
    }
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
        },
    }
    inventory_map = {("ALA-KZT-7280TR-1", "ethernet51/1"): "Cogent NetBox"}
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        items,
        {"Uplink: Cogent 10G": "Cogent Legacy"},
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
    )
    assert len(edges) == 1
    assert edges[0][3] == "Cogent NetBox"


def test_build_edges_fallback_description_without_inventory():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Uplink: Cogent 10G"},
        ],
    }
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
        },
    }
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        items,
        {"Uplink: Cogent 10G": "Cogent Legacy"},
        device_iface_to_provider={},
        inventory_scoped=False,
    )
    assert edges[0][3] == "Cogent Legacy"


def test_build_edges_inventory_without_uplink_description():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Transit handoff only"},
        ],
    }
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
        },
    }
    inventory_map = {("ALA-KZT-7280TR-1", "ethernet51/1"): "ManualISP"}
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        items,
        {},
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
    )
    assert len(edges) == 1
    assert edges[0][3] == "ManualISP"


def test_build_edges_excludes_out_of_scope_uplink_description():
    devices = {
        "WAW-EQX-7280QR-2": [
            {
                "name": "Ethernet23/1",
                "description": "Uplink: HurricaneE",
            },
            {
                "name": "Ethernet34/1",
                "description": "Uplink: Fiord and MSK PING-WIN 3Gbps link",
            },
        ],
    }
    items = {
        ("WAW-EQX-7280QR-2", "ethernet23/1"): {"itemid_in": "501", "itemid_out": "502"},
        ("WAW-EQX-7280QR-2", "ethernet34/1"): {"itemid_in": "601", "itemid_out": "602"},
    }
    inventory_map = {("WAW-EQX-7280QR-2", "ethernet23/1"): "Hurricane"}
    edges = _build_edges(
        devices,
        {"WAW-EQX-7280QR-2": "102"},
        items,
        {
            "Uplink: HurricaneE": "Hurricane Legacy",
            "Uplink: Fiord and MSK PING-WIN 3Gbps link": "Fiord Legacy",
        },
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
    )
    assert len(edges) == 1
    assert edges[0][2] == "Ethernet23/1"
    assert edges[0][3] == "Hurricane"


def _frn_lag_netbox_relations():
    return {
        "member_to_aggregate": {("FRN-MX-1", "et-0/0/3"): "ae5"},
        "parent_children": {("FRN-MX-1", "ae5"): {"ae5.0"}},
        "display_names": {
            ("FRN-MX-1", "et-0/0/3"): "et-0/0/3",
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.0"): "ae5.0",
        },
    }


def test_build_edges_excludes_lag_member_when_logical_in_scoped():
    devices = {
        "FRN-MX-1": [
            {"name": "et-0/0/3"},
            {"name": "ae5.0", "isLogical": True},
        ],
    }
    items = {
        ("FRN-MX-1", "et-0/0/3"): {"itemid_in": "1", "itemid_out": "2"},
        ("FRN-MX-1", "ae5.0"): {"itemid_in": "3", "itemid_out": "4"},
    }
    inventory_map = {
        ("FRN-MX-1", "et-0/0/3"): "Hurricane",
        ("FRN-MX-1", "ae5.0"): "Hurricane",
    }
    edges = _build_edges(
        devices,
        {"FRN-MX-1": "101"},
        items,
        {},
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
        netbox_relations=_frn_lag_netbox_relations(),
    )
    assert len(edges) == 1
    assert edges[0][2] == "ae5.0"
    assert edges[0][3] == "Hurricane"


def test_build_edges_keeps_lag_member_without_logical_in_scoped():
    devices = {
        "FRN-MX-1": [
            {"name": "et-0/0/3"},
        ],
    }
    items = {
        ("FRN-MX-1", "et-0/0/3"): {"itemid_in": "1", "itemid_out": "2"},
    }
    inventory_map = {("FRN-MX-1", "et-0/0/3"): "Hurricane"}
    edges = _build_edges(
        devices,
        {"FRN-MX-1": "101"},
        items,
        {},
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
        netbox_relations=_frn_lag_netbox_relations(),
    )
    assert len(edges) == 1
    assert edges[0][2] == "et-0/0/3"


def test_build_edges_keeps_lag_member_when_logical_has_different_provider():
    devices = {
        "FRN-MX-1": [
            {"name": "et-0/0/3"},
            {"name": "ae5.0", "isLogical": True},
        ],
    }
    items = {
        ("FRN-MX-1", "et-0/0/3"): {"itemid_in": "1", "itemid_out": "2"},
        ("FRN-MX-1", "ae5.0"): {"itemid_in": "3", "itemid_out": "4"},
    }
    inventory_map = {
        ("FRN-MX-1", "et-0/0/3"): "Hurricane",
        ("FRN-MX-1", "ae5.0"): "Cogent",
    }
    edges = _build_edges(
        devices,
        {"FRN-MX-1": "101"},
        items,
        {},
        device_iface_to_provider=inventory_map,
        inventory_scoped=True,
        netbox_relations=_frn_lag_netbox_relations(),
    )
    assert len(edges) == 2
    assert {e[2] for e in edges} == {"et-0/0/3", "ae5.0"}


def test_build_edges_skips_non_uplink_without_inventory():
    devices = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet52/1", "description": "Management link"},
        ],
    }
    edges = _build_edges(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        {},
        {},
        device_iface_to_provider={},
        inventory_scoped=False,
    )
    assert edges == []


def test_get_providers_from_inventory_manual_without_automatization(monkeypatch):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        device_tag="border",
        tag_device=True,
    )
    dry_ssh = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Uplink: Manual"},
        ],
    }
    with patch("uplinks.netbox.inventory.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        monkeypatch.setenv("NETBOX_TAG", "border")
        ctx = load_uplink_provider_context(dry_ssh, debug=False)
    assert sorted(ctx.get("providers") or []) == ["ManualISP"]


def test_dashboard_by_provider_includes_manual_inventory_provider(monkeypatch):
    edges = [
        ("ALA-KZT-7280TR-1", "101", "Ethernet51/1", "ManualISP", "501", "502", True, False, False),
    ]
    created = []

    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: created.append(p) or {"dashboardids": ["77"]})
        .activate(monkeypatch)
    )
    monkeypatch.setattr(
        "zabbix_uplinks_dashboard._get_aggregate_itemids",
        lambda url, token, providers, debug=False: {},
    )

    dash_id, err = create_dashboard_by_provider(
        "https://z.example/api_jsonrpc.php",
        "t",
        edges,
        "Uplinks by provider",
        list(PROVIDERS_FOR_SUMMARY) + ["ManualISP"],
    )
    assert err is None
    assert dash_id == "77"
    assert created
    page_names = [page.get("name") for page in created[0].get("pages", [])]
    assert "ManualISP" in page_names


def test_main_inventory_unavailable_excludes_fiord(monkeypatch, zabbix_env, tmp_path, capsys):
    """When scoped inventory is unavailable, description-only uplinks like Fiord are ignored."""
    import zabbix_uplinks_dashboard as mod

    dry = tmp_path / "dry.json"
    dry.write_text(
        json.dumps(
            {
                "devices": {
                    "WAW-EQX-7280QR-2": [
                        {
                            "name": "Ethernet23/1",
                            "description": "Uplink: HurricaneE",
                        },
                        {
                            "name": "Ethernet34/1",
                            "description": "Uplink: Fiord and MSK PING-WIN 3Gbps link",
                        },
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    desc = tmp_path / "desc.json"
    desc.write_text(
        json.dumps(
            {
                "Uplink: HurricaneE": "Hurricane Legacy",
                "Uplink: Fiord and MSK PING-WIN 3Gbps link": "Fiord Legacy",
            }
        ),
        encoding="utf-8",
    )

    mocker = (
        build_standard_zabbix_mocker(
            hosts=[
                {
                    "hostid": "102",
                    "host": "WAW-EQX-7280QR-2",
                    "name": "WAW-EQX-7280QR-2",
                },
            ],
            items=[
                {
                    "itemid": "501",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits received",
                    "key_": 'net.if.in["Ethernet23/1"]',
                },
                {
                    "itemid": "502",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits sent",
                    "key_": 'net.if.out["Ethernet23/1"]',
                },
                {
                    "itemid": "601",
                    "hostid": "102",
                    "name": "Interface Ethernet34/1: Bits received",
                    "key_": 'net.if.in["Ethernet34/1"]',
                },
                {
                    "itemid": "602",
                    "hostid": "102",
                    "name": "Interface Ethernet34/1: Bits sent",
                    "key_": 'net.if.out["Ethernet34/1"]',
                },
            ],
        )
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["1"]})
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_uplinks_dashboard.py",
            "--legacy-dry-ssh",
            "-f",
            str(dry),
            "-m",
            str(desc),
            "--no-cache",
            "--dashboard-by-location",
            "",
            "--dashboard-by-provider",
            "",
        ],
    )
    with patch.object(mod, "load_uplink_provider_context", return_value=None):
        with pytest.raises(SystemExit) as exc:
            mod.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Fiord" not in captured.out
    assert "Fiord" not in captured.err


def test_main_inventory_file_wires_netbox_relations_to_build_edges(
    monkeypatch, zabbix_env, tmp_path, capsys
):
    """Regression: --inventory-file main() passes netbox_interface_relations to _build_edges."""
    import zabbix_uplinks_dashboard as mod

    relations = _frn_lag_netbox_relations()
    inv_ctx = {
        "device_iface_to_provider": {
            ("FRN-MX-1", "et-0/0/3"): "Hurricane",
            ("FRN-MX-1", "ae5.0"): "Hurricane",
        },
        "providers": {"Hurricane"},
        "netbox_interface_relations": relations,
        "stats": {},
        "read_error": False,
    }

    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "FRN-MX-1",
                        "interface": "et-0/0/3",
                        "provider": "Hurricane",
                    },
                    {
                        "device": "FRN-MX-1",
                        "interface": "ae5.0",
                        "provider": "Hurricane",
                    },
                ],
                "incomplete": [],
                "stats": {},
            }
        ),
        encoding="utf-8",
    )
    desc = tmp_path / "desc.json"
    desc.write_text("{}", encoding="utf-8")

    items = [
        {
            "itemid": "1",
            "hostid": "102",
            "name": "Interface et-0/0/3: Bits received",
            "key_": 'net.if.in["et-0/0/3"]',
        },
        {
            "itemid": "2",
            "hostid": "102",
            "name": "Interface et-0/0/3: Bits sent",
            "key_": 'net.if.out["et-0/0/3"]',
        },
        {
            "itemid": "3",
            "hostid": "102",
            "name": "Interface ae5.0: Bits received",
            "key_": 'net.if.in["ae5.0"]',
        },
        {
            "itemid": "4",
            "hostid": "102",
            "name": "Interface ae5.0: Bits sent",
            "key_": 'net.if.out["ae5.0"]',
        },
    ]
    mocker = (
        build_standard_zabbix_mocker(
            hosts=[{"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"}],
            items=items,
        )
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["1"]})
    )
    mocker.activate(monkeypatch)

    build_edges_kwargs = []
    build_edges_results = []
    real_build_edges = mod._build_edges

    def _spy_build_edges(*args, **kwargs):
        build_edges_kwargs.append(kwargs)
        result = real_build_edges(*args, **kwargs)
        build_edges_results.append(result)
        return result

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_uplinks_dashboard.py",
            "--inventory-file",
            str(inv),
            "-m",
            str(desc),
            "--no-cache",
            "--dashboard-by-location",
            "",
            "--dashboard-by-provider",
            "",
        ],
    )
    with patch.object(mod, "load_uplink_provider_context", return_value=inv_ctx):
        with patch.object(mod, "_build_edges", side_effect=_spy_build_edges):
            mod.main()

    assert len(build_edges_kwargs) == 1
    assert build_edges_kwargs[0]["netbox_relations"] == relations
    assert build_edges_kwargs[0]["inventory_scoped"] is True
    assert len(build_edges_results[0]) == 1
    assert build_edges_results[0][0][2] == "ae5.0"
    assert "dashboard" in capsys.readouterr().out.lower()
