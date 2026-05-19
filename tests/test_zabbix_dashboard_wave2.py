"""zabbix_uplinks_dashboard: cache, providers from netbox, dashboard update."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from tests.mocks.netbox_full import NetBoxTestEnvironment
from zabbix_uplinks_dashboard import (
    _build_edges,
    _get_providers_from_netbox,
    _make_graph_widget,
    create_or_update_dashboard,
    load_zabbix_cache,
    save_zabbix_cache,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_build_edges_dedup():
    devices = {
        "h1": [
            {"name": "eth1", "description": "Uplink: Cogent", "isLag": False},
            {"name": "eth1.0", "description": "Uplink: Cogent", "isLogical": True},
        ],
    }
    items = {
        ("h1", "eth1"): {"itemid_in": "1", "itemid_out": "2"},
        ("h1", "eth1.0"): {"itemid_in": "", "itemid_out": ""},
    }
    edges = _build_edges(devices, {"h1": "101"}, items, {"Uplink: Cogent": "Cogent"})
    assert len(edges) == 1
    assert edges[0][3] == "Cogent"


def test_make_graph_widget_threshold():
    w = _make_graph_widget(0, "host", "Eth1", "ISP", "1", "2", 0, 0, show_threshold=True)
    assert any(f.get("name") == "simple_triggers" for f in w["fields"])


def test_cache_roundtrip(tmp_path):
    p = tmp_path / "cache.json"
    hosts = {"h1": "101"}
    items = {("h1", "eth1"): {"bits_in": "k1"}}
    save_zabbix_cache(str(p), hosts, items)
    h, i = load_zabbix_cache(str(p))
    assert h["h1"] == "101"
    assert ("h1", "eth1") in i


def test_get_providers_from_netbox(monkeypatch, netbox_env):
    env = NetBoxTestEnvironment()
    tag = env.seed_automation_tag()
    prov = env.circuits.providers.create(name="Cogent", slug="cogent")
    prov.tags = [tag]
    prov.tag_slug = tag.slug
    with patch("zabbix_uplinks_dashboard.pynetbox.api", lambda url, token: env):
        names = _get_providers_from_netbox(tag.slug, debug=True)
    assert "Cogent" in names


def test_create_or_update_dashboard_update(monkeypatch):
    updated = []
    edges = [("h1", "101", "eth1", "ISP", "1", "2", True, False, False)]
    (
        ZabbixRpcMocker()
        .on("dashboard.get", lambda p: [{"dashboardid": "9", "name": "Uplinks"}])
        .on("dashboard.update", lambda p: updated.append(p) or True)
        .activate(monkeypatch)
    )
    did, err = create_or_update_dashboard(
        "https://z.example/api_jsonrpc.php", "t", edges, "Uplinks"
    )
    assert err is None
    assert did == "9"
    assert updated


def test_main_uses_cache(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_uplinks_dashboard as mod

    dry = tmp_path / "dry.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    desc = tmp_path / "desc.json"
    desc.write_text('{"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane"}', encoding="utf-8")
    from zabbix_uplinks_dashboard import ZABBIX_CACHE_FILE

    cache = tmp_path / ZABBIX_CACHE_FILE
    save_zabbix_cache(
        str(cache),
        {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"},
        {
            ("ALA-KZT-7280TR-1", "Ethernet51/1"): {
                "itemid_in": "1",
                "itemid_out": "2",
                "bits_in": 'net.if.in["Ethernet51/1"]',
                "bits_out": 'net.if.out["Ethernet51/1"]',
            },
            ("FRN-MX-1", "ae5.0"): {
                "itemid_in": "3",
                "itemid_out": "4",
                "bits_in": 'net.if.in[ae5]',
                "bits_out": 'net.if.out[ae5]',
            },
        },
    )
    build_standard_zabbix_mocker().on("dashboard.get", lambda p: []).on(
        "dashboard.create", lambda p: {"dashboardids": ["1"]}
    ).activate(monkeypatch)

    def fake_fetch(url, token, hostnames, debug=False):
        return (
            {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"},
            {
                ("ALA-KZT-7280TR-1", "Ethernet51/1"): {
                    "itemid_in": "1",
                    "itemid_out": "2",
                    "bits_in": 'net.if.in["Ethernet51/1"]',
                    "bits_out": 'net.if.out["Ethernet51/1"]',
                },
                ("FRN-MX-1", "ae5.0"): {
                    "itemid_in": "3",
                    "itemid_out": "4",
                    "bits_in": 'net.if.in[ae5]',
                    "bits_out": 'net.if.out[ae5]',
                },
            },
            None,
        )

    monkeypatch.setattr(mod, "fetch_zabbix_hosts_and_items", fake_fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_uplinks_dashboard.py",
            "-f",
            str(dry),
            "-m",
            str(desc),
            "--dashboard-by-location",
            "",
            "--dashboard-by-provider",
            "",
        ],
    )
    mod.main()
    assert "OK:" in capsys.readouterr().out
