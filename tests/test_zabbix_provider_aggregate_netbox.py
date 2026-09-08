"""zabbix_provider_aggregate NetBox-first inventory helpers."""

import json
from pathlib import Path
from unittest.mock import patch

import zabbix_provider_aggregate as agg
from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_provider_aggregate import (
    _build_edges_with_keys,
    _collect_provider_limits_gbps,
    _device_iface_provider_map_from_inventory,
    _expand_provider_map_for_zabbix,
    _get_providers_from_netbox,
    _load_netbox_aggregate_context,
    _resolve_provider_limit_bps,
    _sanitize_provider_name,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_get_providers_no_env(monkeypatch, capsys):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    assert _get_providers_from_netbox("automatization", debug=True) == []
    assert "NETBOX_URL" in capsys.readouterr().err


def test_get_providers_success(monkeypatch):
    from unittest.mock import MagicMock

    nb = MagicMock()
    nb.circuits.providers.filter.return_value = [
        type("P", (), {"name": "Cogent"})(),
        type("P", (), {"name": "Hurricane"})(),
    ]
    with patch("zabbix_provider_aggregate.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        names = _get_providers_from_netbox("automatization", debug=False)
    assert "Cogent" in names


def test_build_edges_dedup():
    devices = {
        "H1": [
            {"name": "ae5", "description": "Uplink: ISP", "isLag": True},
            {"name": "ae5.0", "description": "Uplink: ISP", "isLogical": True},
        ],
    }
    items = {("H1", "ae5.0"): {"bits_in": "in", "bits_out": "out"}}
    edges = _build_edges_with_keys(devices, {"H1": "1"}, items, {})
    assert len(edges) == 1
    assert edges[0][2] == "in"


def test_build_edges_prefers_inventory_over_description_map():
    devices = {
        "ALA-KZT-7280TR-1": [
            {
                "name": "Ethernet51/1",
                "description": "Uplink: Cogent 10G",
            },
        ],
    }
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "bits_in": "net.if.in[51]",
            "bits_out": "net.if.out[51]",
        },
    }
    inventory_map = {("ALA-KZT-7280TR-1", "ethernet51/1"): "Cogent NetBox"}
    edges = _build_edges_with_keys(
        devices,
        {"ALA-KZT-7280TR-1": "101"},
        items,
        {"Uplink: Cogent 10G": "Cogent Legacy"},
        device_iface_to_provider=inventory_map,
    )
    assert edges[0][1] == "Cogent NetBox"


def test_build_edges_inventory_aware_uplink_filter():
    devices = {
        "R1": [
            {"name": "Ethernet51/1", "description": "Transit handoff only"},
            {"name": "Ethernet52/1", "description": "Management link"},
        ],
    }
    items = {
        ("R1", "ethernet51/1"): {"bits_in": "in", "bits_out": "out"},
        ("R1", "ethernet52/1"): {"bits_in": "in2", "bits_out": "out2"},
    }
    inventory_map = {("R1", "ethernet51/1"): "ManualISP"}
    edges = _build_edges_with_keys(
        devices,
        {"R1": "1"},
        items,
        {},
        device_iface_to_provider=inventory_map,
    )
    assert len(edges) == 1
    assert edges[0][1] == "ManualISP"


def test_build_edges_fallback_skips_non_uplink_without_inventory():
    devices = {
        "R1": [
            {"name": "Ethernet52/1", "description": "Management link"},
        ],
    }
    items = {("R1", "ethernet52/1"): {"bits_in": "in", "bits_out": "out"}}
    edges = _build_edges_with_keys(
        devices,
        {"R1": "1"},
        items,
        {},
        device_iface_to_provider={},
    )
    assert edges == []


def test_expand_provider_map_logical_context():
    inventory_map = {("FRN-MX-1", "et-0/0/1"): "Hurricane"}
    dry_ssh = {
        "FRN-MX-1": [
            {
                "name": "ae5.0",
                "physicalInterface": "et-0/0/1",
                "isLogical": True,
            },
        ],
    }
    expanded = _expand_provider_map_for_zabbix(inventory_map, dry_ssh)
    assert expanded[("FRN-MX-1", "ae5.0")] == "Hurricane"


def test_collect_provider_limits_gbps():
    from tests.mocks.netbox_api import _Record, _Providers

    nb = type("NB", (), {})()
    nb.circuits = type("C", (), {})()
    nb.circuits.providers = _Providers(
        [
            _Record(name="Cogent", custom_fields={"aggregate_limit_gbps": 10}),
            _Record(name="ManualISP", custom_fields={}),
        ]
    )
    limits = _collect_provider_limits_gbps(nb)
    assert limits == {"Cogent": 10.0}


def test_resolve_provider_limit_prefers_netbox():
    limit_bps = _resolve_provider_limit_bps("Cogent", {"Cogent": 12}, {"Cogent": 5})
    assert limit_bps == 12 * 1e9


def test_resolve_provider_limit_fallback_commit_rates(capsys):
    limit_bps = _resolve_provider_limit_bps("Cogent", {}, {"Cogent": 8}, debug=True)
    assert limit_bps == 8 * 1e9
    err = capsys.readouterr().err
    assert "transition fallback" in err


def test_load_netbox_aggregate_context_manual_provider(monkeypatch):
    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="ManualISP",
        provider_custom_fields={"aggregate_limit_gbps": 7},
        device_tag="border",
        tag_device=True,
    )
    dry_ssh = {
        "ALA-KZT-7280TR-1": [
            {"name": "Ethernet51/1", "description": "Uplink: Manual"},
        ],
    }
    with patch("zabbix_provider_aggregate.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        monkeypatch.setenv("NETBOX_TAG", "border")
        ctx = _load_netbox_aggregate_context(dry_ssh, debug=False)
    assert ctx is not None
    assert "ManualISP" in ctx["providers"]
    assert ctx["device_iface_to_provider"][("ALA-KZT-7280TR-1", "ethernet51/1")] == "ManualISP"
    assert ctx["provider_limits_gbps"]["ManualISP"] == 7.0


def test_device_iface_provider_map_from_inventory():
    report = {
        "complete": [
            {
                "provider": "Cogent",
                "device": "R1",
                "interface": "Ethernet51/1",
            },
        ],
    }
    mapping = _device_iface_provider_map_from_inventory(report, {})
    assert mapping[("R1", "ethernet51/1")] == "Cogent"


def test_run_netbox_inventory_provider_and_limit(tmp_path, monkeypatch):
    dry_ssh = FIXTURES / "dry_ssh_minimal.json"
    desc_map = tmp_path / "description_to_name.json"
    desc_map.write_text(json.dumps({"Uplink: Cogent 10G": "WrongName"}), encoding="utf-8")
    commit_rates = tmp_path / "commit_rates.json"
    commit_rates.write_text(json.dumps({"_provider_limits": {"Cogent": 99}}), encoding="utf-8")

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        provider_name="Cogent",
        provider_custom_fields={"aggregate_limit_gbps": 10},
        device_tag="border",
    )
    with patch("zabbix_provider_aggregate.pynetbox.api", return_value=nb):
        monkeypatch.setenv("NETBOX_URL", "https://nb.example")
        monkeypatch.setenv("NETBOX_TOKEN", "tok")
        monkeypatch.setenv("NETBOX_TAG", "border")
        nb_ctx = _load_netbox_aggregate_context(
            json.loads(dry_ssh.read_text(encoding="utf-8"))["devices"],
            debug=False,
        )

    host_items = {"ALA-KZT-7280TR-1": "101"}
    items_by_host = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "bits_in": "net.if.in[51]",
            "bits_out": "net.if.out[51]",
        },
    }

    def fake_fetch(url, token, hostnames, debug=False):
        return dict(host_items), dict(items_by_host), None

    trigger_created = []

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on(
            "host.get",
            lambda p: [
                {"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}
            ],
        )
        .on("host.create", lambda p: {"hostids": ["999"]})
        .on("item.get", lambda p: [])
        .on("item.create", lambda p: {"itemids": ["i1"]})
        .on("item.update", lambda p: True)
        .on("trigger.get", lambda p: [])
        .on("trigger.create", lambda p: trigger_created.append(p) or {"triggerids": ["t1"]})
        .on("trigger.update", lambda p: True)
    )
    mocker.activate(monkeypatch)

    with patch.object(agg, "_load_netbox_aggregate_context", return_value=nb_ctx):
        with patch.object(agg, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
            done, err = agg.run(
                "https://z.example/api_jsonrpc.php",
                "token",
                str(commit_rates),
                str(dry_ssh),
                str(desc_map),
                cache_path=None,
            )

    assert err is None
    assert done
    assert done[0][0] == "Cogent"
    assert done[0][2] is True
    assert trigger_created
    expressions = [t["expression"] for t in trigger_created]
    assert any("9000000000" in expr for expr in expressions)
    assert any("10000000000" in expr for expr in expressions)


def test_sanitize_provider_name():
    assert _sanitize_provider_name("Cogent/Level3") == "Cogent Level3"
    assert _sanitize_provider_name("") == ""
