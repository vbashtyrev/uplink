"""zabbix_provider_aggregate.main() integration."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import zabbix_provider_aggregate as agg
from tests.mocks.zabbix_rpc import ZabbixRpcMocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_success(monkeypatch, zabbix_env, tmp_path, capsys):
    desc_map = tmp_path / "description_to_name.json"
    desc_map.write_text(
        json.dumps(
            {
                "Uplink: Cogent 10G": "Cogent",
                "Uplink: Hurricane": "Hurricane",
            }
        ),
        encoding="utf-8",
    )
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [],
                "incomplete": [],
                "stats": {"complete": 0, "providers": 2},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_percent": {},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    host_items = {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"}
    items_by_host = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "bits_in": "net.if.in[51]",
            "bits_out": "net.if.out[51]",
        },
        ("FRN-MX-1", "ae5.0"): {
            "bits_in": "net.if.in[ae5]",
            "bits_out": "net.if.out[ae5]",
        },
    }

    def fake_fetch(url, token, hostnames, debug=False):
        h = {k: host_items[k] for k in hostnames if k in host_items}
        i = {k: v for k, v in items_by_host.items() if k[0] in h}
        return h, i, None

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on(
            "host.get",
            lambda p: [
                {"hostid": host_items[h], "host": h, "name": h}
                for h in (p.get("filter", {}).get("host") or [])
                if h in host_items
            ],
        )
        .on("host.create", lambda p: {"hostids": ["999"]})
        .on("item.get", lambda p: [])
        .on("item.create", lambda p: {"itemids": ["i1"]})
        .on("item.update", lambda p: True)
        .on("trigger.get", lambda p: [])
        .on("trigger.create", lambda p: {"triggerids": ["t1"]})
        .on("trigger.update", lambda p: True)
        .on("trigger.delete", lambda p: True)
    )
    mocker.activate(monkeypatch)

    nb_ctx = {
        "device_iface_to_provider": {
            ("ALA-KZT-7280TR-1", "ethernet51/1"): "Cogent",
            ("FRN-MX-1", "ae5.0"): "Hurricane",
            ("FRN-MX-1", "et-0/0/1"): "Hurricane",
            ("FRN-MX-1", "ae5"): "Hurricane",
        },
        "providers": {"Cogent", "Hurricane"},
        "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
        "stats": {},
    }

    with patch.object(
        agg,
        "run",
        return_value=(
            [
                ("Cogent", "Uplinks Cogent", True),
                ("Hurricane", "Uplinks Hurricane", True),
            ],
            None,
        ),
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_aggregate.py",
                "--inventory-file",
                str(inv),
                "--no-cache",
            ],
        )
        agg.main()
    out = capsys.readouterr().out
    assert "OK:" in out


def test_main_no_providers_exits_zero(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [],
                "incomplete": [],
                "stats": {"complete": 0, "providers": 0},
                "provider_limits_gbps": {},
                "provider_slo_percent": {},
            }
        ),
        encoding="utf-8",
    )

    mocker = ZabbixRpcMocker().on("user.get", lambda p: [{"userid": "1"}])
    mocker.activate(monkeypatch)

    with patch.object(agg, "run", return_value=([], None)):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_aggregate.py",
                "--inventory-file",
                str(inv),
            ],
        )
        with pytest.raises(SystemExit) as exc:
            agg.main()
    assert exc.value.code == 0
