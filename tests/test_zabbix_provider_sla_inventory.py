"""zabbix_provider_sla: NetBox inventory-file path (stage 13)."""

import json
import sys

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from tests.test_zabbix_sync_extended import MSK_MX204_1_DRY_SSH
from uplinks.netbox import inventory as inv_mod
from uplinks.netbox.inventory import ERROR_PARTIAL_READ, collect_uplink_inventory
from uplinks_config import TRIGGER_DESC_SLA_BREACH_SUFFIX
import zabbix_provider_sla as sla_mod


def _mx204_beeline_netbox_relations():
    return {
        "member_to_aggregate": {("MSK-M9-MX204-1", "et-0/0/3"): "ae5"},
        "parent_children": {("MSK-M9-MX204-1", "ae5"): {"ae5.0"}},
        "display_names": {
            ("MSK-M9-MX204-1", "et-0/0/3"): "et-0/0/3",
            ("MSK-M9-MX204-1", "ae5"): "ae5",
            ("MSK-M9-MX204-1", "ae5.0"): "ae5.0",
        },
    }


def _inventory_report_for_circuit(nb):
    from unittest.mock import patch

    with patch("uplinks.netbox.inventory.netbox_border_tag", return_value=None):
        return collect_uplink_inventory(nb, tag=None)


def _burst_row_for_circuit(output, circuit_id):
    for line in output.splitlines():
        if circuit_id in line and "Burst circuits" not in line and "Circuit" not in line:
            return line
    return ""


def _zabbix_burst_mocker(device_name, iface_name):
    iface_prefix = "Interface {}:".format(iface_name)
    sla_desc = iface_prefix + " " + TRIGGER_DESC_SLA_BREACH_SUFFIX

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt and device_name in filt["host"]:
            return [{"hostid": "101", "host": device_name}]
        return []

    def trigger_get(params):
        search = (params.get("search") or {}).get("description") or ""
        if "hostids" in params and search == iface_prefix:
            return [{"triggerid": "sla1", "description": sla_desc}]
        return []

    return (
        ZabbixRpcMocker()
        .on("host.get", host_get)
        .on("trigger.get", trigger_get)
        .on(
            "event.get",
            lambda p: [{"clock": "1000", "value": "1"}, {"clock": "2000", "value": "0"}],
        )
    )


def test_burst_report_rows_inventory_relations_map_physical_to_logical():
    report = {
        "complete": [
            {
                "device": "MSK-M9-MX204-1",
                "interface": "et-0/0/3",
                "provider": "Beeline",
                "circuit_id": "CKT-41",
                "billing_model": "Burst",
                "commit_rate_kbps": 10_000_000,
            }
        ],
        "incomplete": [],
        "stats": {},
    }
    relations = _mx204_beeline_netbox_relations()
    rows = sla_mod._burst_report_rows_from_inventory(
        report,
        netbox_relations=relations,
    )
    assert len(rows) == 1
    cid, prov, dev, iface, cr = rows[0]
    assert cid == "CKT-41"
    assert prov == "Beeline"
    assert dev == "MSK-M9-MX204-1"
    assert iface == "ae5.0"
    assert cr == 10.0


def test_main_inventory_file_maps_physical_to_logical_without_dry_ssh(
    tmp_path, monkeypatch, capsys, zabbix_env
):
    iface_name = "ae5.0"
    nb = build_netbox_for_commit_rates(
        device_name="MSK-M9-MX204-1",
        iface_name="et-0/0/3",
        provider_name="Beeline",
        circuit_id=41,
        circuit_custom_fields={"billing_model": "Burst"},
        commit_rate_kbps=10_000_000,
        tag_device=False,
    )
    report = _inventory_report_for_circuit(nb)
    report["netbox_interface_relations"] = inv_mod.serialize_netbox_interface_relations(
        _mx204_beeline_netbox_relations()
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(report), encoding="utf-8")

    _zabbix_burst_mocker("MSK-M9-MX204-1", iface_name).activate(monkeypatch)

    from unittest.mock import patch

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_sla.py",
                "--inventory-file",
                str(inventory),
                "--days",
                "1",
                "--from-ts",
                "0",
                "--to-ts",
                "10000",
            ],
        )
        sla_mod.main()

    out = capsys.readouterr().out
    row = _burst_row_for_circuit(out, "CKT-41")
    assert "CKT-41" in out
    assert "Beeline" in out
    assert " n/a " not in row
    assert "90.00000" in row


def test_main_no_implicit_dry_ssh_for_burst_mapping(
    tmp_path, monkeypatch, capsys, zabbix_env
):
    """dry-ssh.json in cwd must not be loaded unless --dry-ssh is passed."""
    nb = build_netbox_for_commit_rates(
        device_name="MSK-M9-MX204-1",
        iface_name="et-0/0/3",
        provider_name="Beeline",
        circuit_id=41,
        circuit_custom_fields={"billing_model": "Burst"},
        commit_rate_kbps=10_000_000,
        tag_device=False,
    )
    report = _inventory_report_for_circuit(nb)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(report), encoding="utf-8")

    dry_ssh = tmp_path / "dry-ssh.json"
    dry_ssh.write_text(json.dumps({"devices": MSK_MX204_1_DRY_SSH}), encoding="utf-8")

    _zabbix_burst_mocker("MSK-M9-MX204-1", "ae5.0").activate(monkeypatch)

    from unittest.mock import patch

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_sla.py",
                "--inventory-file",
                str(inventory),
                "--days",
                "1",
                "--from-ts",
                "0",
                "--to-ts",
                "10000",
            ],
        )
        sla_mod.main()

    out = capsys.readouterr().out
    row = _burst_row_for_circuit(out, "CKT-41")
    assert "CKT-41" in out
    assert " n/a " in row


def test_inventory_read_failure_blocks_sla_report(
    monkeypatch, zabbix_env, tmp_path, capsys
):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "ALA-KZT-7280TR-1",
                        "interface": "Ethernet51/1",
                        "provider": "ManualISP",
                        "billing_model": "Burst",
                    }
                ],
                "incomplete": [],
                "stats": {"error": ERROR_PARTIAL_READ, "read_errors": 1},
            }
        ),
        encoding="utf-8",
    )

    mocker = (
        ZabbixRpcMocker()
        .on("host.get", lambda p: [{"hostid": "101", "host": "ALA-KZT-7280TR-1"}])
        .on("trigger.get", lambda p: [{"triggerid": "t1", "description": "x"}])
        .on("event.get", lambda p: [{"eventid": "e1"}])
    )
    mocker.activate(monkeypatch)

    from tests.test_netbox_read_failure_guarantee import _disarm_guard

    _disarm_guard(monkeypatch, sla_mod)

    from unittest.mock import patch

    with patch("zabbix_provider_services.netbox_client_from_env", return_value=None):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "zabbix_provider_sla.py",
                "--inventory-file",
                str(inventory),
                "--days",
                "1",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            sla_mod.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "NetBox error" in captured.err
    assert "host.get" not in mocker.method_names()
    assert "trigger.get" not in mocker.method_names()
    assert "event.get" not in mocker.method_names()
