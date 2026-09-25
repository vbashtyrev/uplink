"""NetBox-only CLI paths: required inventory, explicit legacy dry-ssh."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.inventory_scope import (
    dry_ssh_minimal_inventory_context,
    write_dry_ssh_minimal_inventory,
)
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks.data import INVENTORY_FILE_REQUIRED_MSG

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_map_create_and_export_without_inventory(monkeypatch, zabbix_env, capsys):
    import zabbix_map as zm

    monkeypatch.setattr(sys, "argv", ["zabbix_map.py", "--create-map"])
    with patch.object(zm, "ensure_map_exists", return_value=("42", None)):
        with pytest.raises(SystemExit) as exc:
            zm.main()
    assert exc.value.code == 0

    monkeypatch.setattr(sys, "argv", ["zabbix_map.py", "--export-map", "1"])
    with patch.object(
        zm,
        "zabbix_request",
        return_value=([{"sysmapid": "1", "selements": [], "links": []}], None),
    ):
        with pytest.raises(SystemExit) as exc:
            zm.main()
    assert exc.value.code == 0


def test_map_requires_inventory_file(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_map as zm

    (tmp_path / "dry-ssh.json").write_text(
        json.dumps({"devices": {"HOST-1": []}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["zabbix_map.py", "--print-table"])
    with pytest.raises(SystemExit) as exc:
        zm.main()
    assert exc.value.code == 1
    assert INVENTORY_FILE_REQUIRED_MSG in capsys.readouterr().err


def test_map_inventory_file_ignores_description_map(
    monkeypatch, zabbix_env, tmp_path, capsys
):
    import zabbix_map as zm

    inv = write_dry_ssh_minimal_inventory(tmp_path)
    desc = tmp_path / "description_to_name.json"
    desc.write_text(json.dumps({"Uplink: Cogent 10G": "WrongName"}), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_map.py",
            "--inventory-file",
            str(inv),
            "--print-table",
        ],
    )
    with patch.object(
        zm, "load_uplink_provider_context", return_value=dry_ssh_minimal_inventory_context()
    ):
        zm.main()
    out = capsys.readouterr().out
    assert "Cogent" in out
    assert "WrongName" not in out


def test_dashboard_requires_inventory_file(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_uplinks_dashboard as dash

    (tmp_path / "dry-ssh.json").write_text(
        json.dumps({"devices": {"HOST-1": []}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["zabbix_uplinks_dashboard.py"])
    with pytest.raises(SystemExit) as exc:
        dash.main()
    assert exc.value.code == 1
    assert INVENTORY_FILE_REQUIRED_MSG in capsys.readouterr().err


def test_aggregate_no_implicit_dry_ssh(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_provider_aggregate as agg

    (tmp_path / "dry-ssh.json").write_text(
        json.dumps({"devices": {"HOST-1": []}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_aggregate.py"])
    with pytest.raises(SystemExit) as exc:
        agg.main()
    assert exc.value.code == 1
    assert INVENTORY_FILE_REQUIRED_MSG in capsys.readouterr().err


def test_aggregate_inventory_file_ignores_description_map(tmp_path, monkeypatch):
    import zabbix_provider_aggregate as agg

    inv = write_dry_ssh_minimal_inventory(tmp_path)
    desc = tmp_path / "description_to_name.json"
    desc.write_text(json.dumps({"Uplink: Cogent 10G": "WrongName"}), encoding="utf-8")
    nb_ctx = dict(dry_ssh_minimal_inventory_context())
    nb_ctx["provider_limits_gbps"] = {"Cogent": 10, "Hurricane": 5}

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
    )
    mocker.activate(monkeypatch)

    with patch.object(agg, "_load_netbox_aggregate_context", return_value=nb_ctx):
        with patch.object(
            agg,
            "fetch_zabbix_hosts_and_items",
            lambda url, token, hostnames, debug=False: (
                {k: host_items[k] for k in hostnames if k in host_items},
                {k: v for k, v in items_by_host.items() if k[0] in hostnames},
                None,
            ),
        ):
            with patch.object(agg, "load_description_map") as load_desc:
                done, err = agg.run(
                    "https://z.example/api_jsonrpc.php",
                    "token",
                    None,
                    str(desc),
                    cache_path=None,
                    inventory_file=str(inv),
                )
    load_desc.assert_not_called()
    assert err is None
    assert done
    assert done[0][0] == "Cogent"
