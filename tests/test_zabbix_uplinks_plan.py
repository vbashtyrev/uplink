"""zabbix_uplinks_plan / uplinks.zabbix.plan read-only helpers."""

import json
from pathlib import Path

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import (
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
)
from uplinks.zabbix.plan import (
    ZABBIX_MUTATING_METHODS,
    build_zabbix_plan,
    format_plan_text,
    inventory_plan_gate,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_inventory_plan_gate_fail_closed():
    assert inventory_plan_gate({"stats": {"error": "auth_denied"}, "complete": [], "incomplete": []})[0] is False
    assert inventory_plan_gate({"stats": {"error": "partial_read"}, "complete": [{"device": "d"}], "incomplete": []})[0] is False
    assert inventory_plan_gate({"stats": {}, "complete": [], "incomplete": []})[0] is False
    assert inventory_plan_gate({"stats": {}, "complete": [{"device": "d"}], "incomplete": [{"reason": "x"}]})[0] is True
    assert inventory_plan_gate({"stats": {}, "complete": [{"device": "d"}], "incomplete": []})[0] is True


def test_build_zabbix_plan_read_only_no_mutating_calls(monkeypatch, zabbix_env, netbox_env, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )
    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", lambda url, token: nb)

    def host_get(params):
        filt = params.get("filter") or {}
        names = filt.get("host") or filt.get("name") or []
        if "ALA-KZT-7280TR-1" in names:
            return [
                {
                    "hostid": "101",
                    "host": "ALA-KZT-7280TR-1",
                    "name": "ALA-KZT-7280TR-1",
                }
            ]
        return []

    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.on("host.get", host_get)
    mocker.on("usermacro.get", lambda p: [])
    mocker.on("trigger.get", lambda p: [])
    mocker.activate(monkeypatch)

    report, err = build_zabbix_plan(str(dry))
    assert err is None
    assert report["read_only"] is True
    assert report["inventory"]["stats"]
    assert report["planned"]["maps"]["status"] == "not_evaluated"

    mutating = [m for m in mocker.method_names() if m in ZABBIX_MUTATING_METHODS]
    assert mutating == []


def test_build_zabbix_plan_macro_create_category(monkeypatch, zabbix_env, netbox_env, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )
    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", lambda url, token: nb)

    def host_get(params):
        filt = params.get("filter") or {}
        names = filt.get("host") or filt.get("name") or []
        if "ALA-KZT-7280TR-1" in names:
            return [
                {
                    "hostid": "101",
                    "host": "ALA-KZT-7280TR-1",
                    "name": "ALA-KZT-7280TR-1",
                }
            ]
        return []

    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.on("host.get", host_get)
    mocker.on("usermacro.get", lambda p: [])
    mocker.on("trigger.get", lambda p: [])
    mocker.activate(monkeypatch)

    report, err = build_zabbix_plan(str(dry))
    assert err is None
    macros = report["planned"]["macros"]
    assert macros["create"] or macros["unchanged"]


def test_build_zabbix_plan_macro_read_error_fails_closed(monkeypatch, zabbix_env, netbox_env, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )
    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", lambda url, token: nb)

    def host_get(params):
        filt = params.get("filter") or {}
        names = filt.get("host") or filt.get("name") or []
        if "ALA-KZT-7280TR-1" in names:
            return [
                {
                    "hostid": "101",
                    "host": "ALA-KZT-7280TR-1",
                    "name": "ALA-KZT-7280TR-1",
                }
            ]
        return []

    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.on("host.get", host_get)
    mocker.on("trigger.get", lambda p: [])
    mocker.activate(monkeypatch)

    report, err = build_zabbix_plan(str(dry))
    assert report is None
    assert err is not None
    assert "usermacro.get" in err


def test_build_zabbix_plan_util_triggers_exclude_out_of_scope_dry_ssh(
    monkeypatch, zabbix_env, netbox_env, tmp_path
):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text(
        """{
  "devices": {
    "WAW-EQX-7280QR-2": [
      {"name": "Ethernet23/1", "description": "Uplink: HurricaneE"},
      {"name": "Ethernet34/1", "description": "Uplink: Fiord and MSK PING-WIN 3Gbps link"}
    ]
  }
}""",
        encoding="utf-8",
    )

    nb = build_netbox_for_commit_rates(
        device_name="WAW-EQX-7280QR-2",
        iface_name="Ethernet23/1",
        provider_name="Hurricane",
        device_tag="border",
        tag_device=True,
    )
    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", lambda url, token: nb)

    def host_get(params):
        filt = params.get("filter") or {}
        names = filt.get("host") or filt.get("name") or []
        if "WAW-EQX-7280QR-2" in names:
            return [
                {
                    "hostid": "102",
                    "host": "WAW-EQX-7280QR-2",
                    "name": "WAW-EQX-7280QR-2",
                }
            ]
        return []

    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.on("host.get", host_get)
    mocker.on("usermacro.get", lambda p: [])
    mocker.on("trigger.get", lambda p: [])
    mocker.activate(monkeypatch)

    report, err = build_zabbix_plan(str(dry))
    assert err is None
    util_plan = report["planned"]["util_triggers"]
    planned_ifaces = {row["interface"] for row in util_plan["create"]}
    assert planned_ifaces == {"Ethernet23/1"}
    assert "Ethernet34/1" not in planned_ifaces


def test_format_plan_text_burst_triggers_not_evaluated():
    report = {
        "inventory": {"stats": {"providers": 1, "complete": 1, "incomplete": 0}},
        "summary": {"create": 0, "update": 0, "unchanged": 0, "delete": 0, "skipped": 0, "not_evaluated": 4},
        "planned": {
            "macros": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "util_triggers": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "burst_triggers": {
                "status": "not_evaluated",
                "reason": "Burst link triggers disabled (no --create-link-triggers)",
                "create": [],
                "update": [],
                "unchanged": [],
                "delete": [],
                "skipped": [],
            },
            "aggregate_hosts": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "maps": {"status": "not_evaluated", "reason": "map diff", "create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "dashboards": {"status": "not_evaluated", "reason": "dash diff", "create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "services": {"status": "not_evaluated", "reason": "svc diff", "create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
        },
    }
    text = format_plan_text(report)
    assert "burst_triggers: not_evaluated" in text
    assert "Burst link triggers disabled" in text
    assert "burst_triggers: create=" not in text


def test_inventory_plan_gate_rejects_partial_read():
    ok, detail = inventory_plan_gate(
        {"stats": {"error": "partial_read"}, "complete": [{"device": "d"}], "incomplete": []}
    )
    assert ok is False
    assert "partial read" in detail.lower()


def test_build_zabbix_plan_skips_missing_zabbix_host(monkeypatch, zabbix_env, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "MISSING-HOST",
                        "interface": "Ethernet51/1",
                        "provider": "Cogent",
                        "commit_rate_kbps": 10000,
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
            }
        ),
        encoding="utf-8",
    )

    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.on("host.get", lambda p: [])
    mocker.on("trigger.get", lambda p: [])
    mocker.activate(monkeypatch)

    report, err = build_zabbix_plan(str(dry), inventory_file=str(inv))
    assert err is None
    macros = report["planned"]["macros"]
    assert macros["create"] == []
    assert len(macros["skipped"]) == 1
    assert macros["skipped"][0]["host"] == "MISSING-HOST"
    assert macros["skipped"][0]["reason"] == "host not found in Zabbix"


def test_build_zabbix_plan_util_trigger_delete_stale_only(monkeypatch, zabbix_env, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text(
        """{
  "devices": {
    "WAW-EQX-7280QR-2": [
      {"name": "Ethernet23/1", "description": "Uplink: HurricaneE"}
    ]
  }
}""",
        encoding="utf-8",
    )
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Hurricane",
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
            }
        ),
        encoding="utf-8",
    )
    our_tag = {"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}
    foreign_tag = {"tag": "owner", "value": "manual"}
    stale_desc = "Interface ae5: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX)
    foreign_desc = "Interface Eth9: {}".format(TRIGGER_DESC_UTIL_CRIT_SUFFIX)

    def host_get(params):
        filt = params.get("filter") or {}
        names = filt.get("host") or filt.get("name") or []
        if "WAW-EQX-7280QR-2" in names:
            return [{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}]
        return []

    def trigger_get(params):
        return [
            {"triggerid": "900", "description": stale_desc, "tags": [our_tag]},
            {"triggerid": "901", "description": foreign_desc, "tags": [foreign_tag]},
            {"triggerid": "902", "description": stale_desc, "tags": []},
        ]

    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.on("host.get", host_get)
    mocker.on("usermacro.get", lambda p: [])
    mocker.on("trigger.get", trigger_get)
    mocker.activate(monkeypatch)

    report, err = build_zabbix_plan(str(dry), inventory_file=str(inv))
    assert err is None
    util_plan = report["planned"]["util_triggers"]
    delete_ids = {row["triggerid"] for row in util_plan["delete"]}
    assert delete_ids == {"900", "902"}
    assert "901" not in delete_ids


def test_build_zabbix_plan_from_inventory_file_without_netbox(monkeypatch, zabbix_env, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "ALA-KZT-7280TR-1",
                        "interface": "Ethernet51/1",
                        "provider": "Cogent",
                        "commit_rate_kbps": 10000,
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
            }
        ),
        encoding="utf-8",
    )

    def host_get(params):
        filt = params.get("filter") or {}
        names = filt.get("host") or filt.get("name") or []
        if "ALA-KZT-7280TR-1" in names:
            return [
                {
                    "hostid": "101",
                    "host": "ALA-KZT-7280TR-1",
                    "name": "ALA-KZT-7280TR-1",
                }
            ]
        return []

    mocker = ZabbixRpcMocker()
    mocker.on("user.get", lambda p: [{"userid": "1"}])
    mocker.on("host.get", host_get)
    mocker.on("usermacro.get", lambda p: [])
    mocker.on("trigger.get", lambda p: [])
    mocker.activate(monkeypatch)

    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)

    def fail_netbox(*args, **kwargs):
        raise AssertionError("NetBox client must not be created when inventory_file is set")

    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", fail_netbox)

    report, err = build_zabbix_plan(str(dry), inventory_file=str(inv))
    assert err is None
    assert report["inventory"]["stats"]["complete"] == 1
