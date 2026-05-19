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
    cr = tmp_path / "commit_rates.json"
    cr.write_text(json.dumps({"_provider_limits": {"Cogent": 10, "Hurricane": 5}}), encoding="utf-8")

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

    with patch.object(agg, "_get_providers_from_netbox", return_value=[]):
        with patch.object(agg, "fetch_zabbix_hosts_and_items", side_effect=fake_fetch):
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "zabbix_provider_aggregate.py",
                    "-f",
                    str(cr),
                    "-d",
                    str(FIXTURES / "dry_ssh_minimal.json"),
                    "-m",
                    str(desc_map),
                    "--no-cache",
                ],
            )
            agg.main()
    out = capsys.readouterr().out
    assert "OK:" in out


def test_main_no_providers_exits_zero(monkeypatch, zabbix_env, tmp_path):
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    desc = tmp_path / "desc.json"
    desc.write_text("{}", encoding="utf-8")

    mocker = ZabbixRpcMocker().on("user.get", lambda p: [{"userid": "1"}])
    mocker.activate(monkeypatch)

    with patch.object(agg, "run", return_value=([], None)):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_aggregate.py",
                "-f",
                str(cr),
                "-d",
                str(FIXTURES / "dry_ssh_minimal.json"),
                "-m",
                str(desc),
            ],
        )
        with pytest.raises(SystemExit) as exc:
            agg.main()
    assert exc.value.code == 0
