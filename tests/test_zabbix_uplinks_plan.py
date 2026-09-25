"""zabbix_uplinks_plan / uplinks.zabbix.plan read-only helpers."""

import json
from pathlib import Path

import pytest

from tests.mocks.inventory_scope import with_provider_metadata
from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import (
    DASHBOARD_NAME,
    DASHBOARD_NAME_BY_PROVIDER,
    MAP_NAME,
    PROJECT_PROVIDER_SLO_PERCENT,
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
)
from uplinks.zabbix.plan import (
    NOT_EVALUATED,
    ZABBIX_MUTATING_METHODS,
    build_zabbix_plan,
    format_plan_text,
    inventory_plan_gate,
    _expected_main_dashboard_identities,
    _plan_one_dashboard,
    _plan_services,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _zabbix_filter_name(params):
    raw = (params.get("filter") or {}).get("name")
    if isinstance(raw, list):
        return raw[0] if raw else ""
    return raw or ""


def _activate_plan_mocker(monkeypatch, host_get=None, trigger_get=None, item_get=None, **extra_handlers):
    mocker = build_standard_zabbix_mocker()
    if host_get is not None:
        mocker.on("host.get", host_get)
    if trigger_get is not None:
        mocker.on("trigger.get", trigger_get)
    if item_get is not None:
        mocker.on("item.get", item_get)
    for method, handler in extra_handlers.items():
        mocker.on(method, handler)
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        "uplinks.zabbix.plan.collect_provider_limits_gbps",
        lambda nb, debug=False: ({"Cogent": 20.0}, None),
    )
    monkeypatch.setattr(
        "uplinks.zabbix.plan.collect_provider_slo_percent",
        lambda nb, debug=False: ({}, None),
    )
    monkeypatch.setattr("uplinks.zabbix.plan.netbox_client_from_env", lambda debug=False: object())
    return mocker


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

    mocker = _activate_plan_mocker(monkeypatch, host_get=host_get)

    report, err = build_zabbix_plan(str(dry))
    assert err is None
    assert report["read_only"] is True
    assert report["inventory"]["stats"]
    assert report["planned"]["maps"].get("status") != "not_evaluated"
    assert "sla.create" in ZABBIX_MUTATING_METHODS

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

    _activate_plan_mocker(monkeypatch, host_get=host_get)

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

    _activate_plan_mocker(monkeypatch, host_get=host_get)

    report, err = build_zabbix_plan(str(dry))
    assert err is None
    util_plan = report["planned"]["util_triggers"]
    planned_ifaces = {row["interface"] for row in util_plan["create"]}
    assert planned_ifaces == {"Ethernet23/1"}
    assert "Ethernet34/1" not in planned_ifaces


def test_format_plan_text_inventory_providers_total_and_in_scope():
    report = {
        "inventory": {
            "complete": [
                {"provider": "Cogent", "device": "d1", "interface": "eth0"},
                {"provider": "Hurricane", "device": "d2", "interface": "eth1"},
            ],
            "stats": {
                "providers": 20,
                "providers_in_scope": 2,
                "complete": 2,
                "incomplete": 0,
            },
        },
        "summary": {"create": 0, "update": 0, "unchanged": 0, "delete": 0, "skipped": 0, "not_evaluated": 0},
        "planned": {
            "macros": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "util_triggers": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "burst_triggers": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "aggregate_hosts": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "maps": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "dashboards": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "services": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
        },
    }
    text = format_plan_text(report)
    assert "providers_total=20" in text
    assert "providers_in_scope=2" in text


def test_format_plan_text_inventory_providers_in_scope_fallback_from_complete():
    report = {
        "inventory": {
            "complete": [{"provider": "Cogent", "device": "d1", "interface": "eth0"}],
            "stats": {"providers": 5, "complete": 1, "incomplete": 0},
        },
        "summary": {"create": 0, "update": 0, "unchanged": 0, "delete": 0, "skipped": 0, "not_evaluated": 0},
        "planned": {
            "macros": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "util_triggers": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "burst_triggers": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "aggregate_hosts": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "maps": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "dashboards": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "services": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
        },
    }
    text = format_plan_text(report)
    assert "providers_total=5" in text
    assert "providers_in_scope=1" in text


def test_format_plan_text_create_details_util_and_aggregate():
    report = {
        "inventory": {"stats": {"providers": 1, "providers_in_scope": 1, "complete": 1, "incomplete": 0}},
        "summary": {"create": 2, "update": 0, "unchanged": 0, "delete": 0, "skipped": 0, "not_evaluated": 0},
        "planned": {
            "macros": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "util_triggers": {
                "create": [
                    {
                        "host": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "triggers": [
                            "Interface Ethernet23/1: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX),
                            "Interface Ethernet23/1: {}".format(TRIGGER_DESC_UTIL_CRIT_SUFFIX),
                        ],
                    }
                ],
                "update": [],
                "unchanged": [],
                "delete": [],
                "skipped": [],
            },
            "burst_triggers": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "aggregate_hosts": {
                "create": [{"provider": "Cogent", "host": "Uplinks_Aggregate_Cogent"}],
                "update": [],
                "unchanged": [],
                "delete": [],
                "skipped": [],
            },
            "maps": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "dashboards": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "services": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
        },
    }
    text = format_plan_text(report)
    assert "util_triggers create (1 entries):" in text
    assert "WAW-EQX-7280QR-2/Ethernet23/1 (2 triggers):" in text
    assert TRIGGER_DESC_UTIL_WARN_SUFFIX in text
    assert "aggregate_hosts create (1 entries):" in text
    assert "provider=Cogent host=Uplinks_Aggregate_Cogent" in text


def test_format_plan_text_create_details_util_one_trigger():
    report = {
        "inventory": {"stats": {"providers": 1, "providers_in_scope": 1, "complete": 1, "incomplete": 0}},
        "summary": {"create": 1, "update": 0, "unchanged": 0, "delete": 0, "skipped": 0, "not_evaluated": 0},
        "planned": {
            "macros": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "util_triggers": {
                "create": [
                    {
                        "host": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "triggers": [
                            "Interface Ethernet23/1: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX),
                        ],
                    }
                ],
                "update": [],
                "unchanged": [],
                "delete": [],
                "skipped": [],
            },
            "burst_triggers": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "aggregate_hosts": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "maps": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "dashboards": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
            "services": {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []},
        },
    }
    text = format_plan_text(report)
    assert "util_triggers create (1 entries):" in text
    assert "WAW-EQX-7280QR-2/Ethernet23/1 (1 trigger):" in text
    assert TRIGGER_DESC_UTIL_WARN_SUFFIX in text
    assert "2 triggers" not in text


def test_format_plan_text_burst_triggers_not_evaluated():
    report = {
        "inventory": {"stats": {"providers": 1, "providers_in_scope": 1, "complete": 1, "incomplete": 0}},
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


def test_build_zabbix_plan_fails_on_relations_read_error(monkeypatch, zabbix_env, netbox_env, tmp_path):
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
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    nb = build_netbox_for_commit_rates(
        device_name="ALA-KZT-7280TR-1",
        iface_name="Ethernet51/1",
        device_tag="border",
    )

    def fail_interfaces_filter(**kwargs):
        raise RuntimeError("dcim.interfaces.filter unavailable")

    nb.dcim.interfaces.filter = fail_interfaces_filter
    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", lambda url, token: nb)

    build_standard_zabbix_mocker().on("host.get", lambda p: []).on("trigger.get", lambda p: []).activate(monkeypatch)

    report, err = build_zabbix_plan(str(dry))
    assert report is None
    assert err is not None
    assert "partial read" in err.lower()


def test_build_zabbix_plan_skips_missing_zabbix_host(monkeypatch, zabbix_env, netbox_env, tmp_path):
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
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    nb = build_netbox_for_commit_rates(
        device_name="MISSING-HOST",
        iface_name="Ethernet51/1",
        device_tag="border",
    )
    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", lambda url, token: nb)

    _activate_plan_mocker(monkeypatch, host_get=lambda p: [])

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
                "netbox_interface_relations": {
                    "member_to_aggregate": [],
                    "parent_children": [],
                    "display_names": [],
                },
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
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

    _activate_plan_mocker(monkeypatch, host_get=host_get, trigger_get=trigger_get)

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
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
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

    _activate_plan_mocker(monkeypatch, host_get=host_get)

    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)

    def fail_netbox(*args, **kwargs):
        raise AssertionError("NetBox client must not be created when inventory_file is set")

    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", fail_netbox)

    report, err = build_zabbix_plan(str(dry), inventory_file=str(inv))
    assert err is None
    assert report["inventory"]["stats"]["complete"] == 1


def test_build_zabbix_plan_from_inventory_file_uses_embedded_relations(
    monkeypatch, zabbix_env, tmp_path
):
    from uplinks.netbox import inventory as inv_mod

    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    relations = {
        "member_to_aggregate": {("FRN-MX-1", "et-0/0/3"): "ae5"},
        "parent_children": {("FRN-MX-1", "ae5"): {"ae5.0"}},
        "display_names": {
            ("FRN-MX-1", "et-0/0/3"): "et-0/0/3",
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.0"): "ae5.0",
        },
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
                        "commit_rate_kbps": 10000,
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "netbox_interface_relations": inv_mod.serialize_netbox_interface_relations(
                    relations
                ),
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    def host_get(params):
        filt = params.get("filter") or {}
        names = filt.get("host") or filt.get("name") or []
        if "FRN-MX-1" in names:
            return [{"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"}]
        return []

    _activate_plan_mocker(monkeypatch, host_get=host_get)

    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)

    def fail_netbox(*args, **kwargs):
        raise AssertionError("NetBox client must not be created when relations are embedded")

    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", fail_netbox)
    monkeypatch.setattr(
        "uplinks.zabbix.plan.collect_netbox_interface_relations",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("collect_netbox_interface_relations must not be called")
        ),
    )

    report, err = build_zabbix_plan(str(dry), inventory_file=str(inv))
    assert err is None
    macro_hosts = {row["host"]: row for row in report["planned"]["macros"]["create"]}
    assert "FRN-MX-1" in macro_hosts
    assert macro_hosts["FRN-MX-1"]["interface"] == "ae5.0"


def test_plan_sections_expose_create_update_unchanged_delete(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Cogent",
                        "commit_rate_kbps": 10000000,
                        "billing_model": "Flat",
                        "circuit_id": "C-1",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )
    stale_limit_desc = "Provider aggregate traffic >= 100% of limit (5 Gbps)"

    def host_get(params):
        filt = params.get("filter") or {}
        names = set(filt.get("host") or filt.get("name") or [])
        if "WAW-EQX-7280QR-2" in names:
            return [{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}]
        if "Uplinks Cogent" in names or "Uplinks_Cogent" in names:
            return [{"hostid": "201", "host": "Uplinks_Cogent", "name": "Uplinks Cogent"}]
        return []

    def item_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        name_search = (params.get("search") or {}).get("name", "")
        key_search = (params.get("search") or {}).get("key_", "")
        if "201" in hostids and "aggregate.bits" in key_search:
            return [{"itemid": "501", "key_": "aggregate.bits.in[]"}]
        if "102" in hostids and name_search == "Bits received":
            return [
                {
                    "itemid": "601",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits received",
                    "key_": "net.if.in[eth23]",
                }
            ]
        if "102" in hostids and name_search == "Bits sent":
            return [
                {
                    "itemid": "602",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits sent",
                    "key_": "net.if.out[eth23]",
                }
            ]
        return []

    def trigger_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        if "201" in hostids:
            return [{"triggerid": "701", "description": stale_limit_desc}]
        return []

    def map_get(params):
        return [
            {
                "sysmapid": "9",
                "name": MAP_NAME,
                "selements": [
                    {
                        "selementid": "11",
                        "elementtype": 0,
                        "elementid": 999,
                        "elements": [{"hostid": "999"}],
                    },
                    {"selementid": "12", "elementtype": 4, "label": "StaleISP"},
                ],
                "links": [],
            }
        ]

    def dashboard_get(params):
        name = _zabbix_filter_name(params)
        if name == DASHBOARD_NAME:
            return [
                {
                    "dashboardid": "31",
                    "name": DASHBOARD_NAME,
                    "pages": [
                        {
                            "widgets": [
                                {"name": "WAW-EQX-7280QR-2 - Ethernet23/1 (Cogent)"},
                            ]
                        }
                    ],
                }
            ]
        return []

    def service_get(params):
        filt = set((params.get("filter") or {}).get("name") or [])
        search = (params.get("search") or {}).get("name", "")
        rows = []
        if "Uplinks providers" in filt:
            rows.append({"serviceid": "801", "name": "Uplinks providers", "parents": []})
        if "Uplinks Cogent" in filt:
            rows.append({"serviceid": "802", "name": "Uplinks Cogent", "parents": []})
        if "Uplinks Cogent SLA source" in filt:
            rows.append({"serviceid": "803", "name": "Uplinks Cogent SLA source"})
        if search == "Uplinks":
            rows.append({"serviceid": "804", "name": "Uplinks Orphan"})
        return rows

    def sla_get(params):
        filt = set((params.get("filter") or {}).get("name") or [])
        search = (params.get("search") or {}).get("name", "")
        rows = []
        if "Uplinks Cogent SLA" in filt:
            rows.append({"slaid": "901", "name": "Uplinks Cogent SLA", "slo": "99.9"})
        if search == "Uplinks":
            rows.append({"slaid": "902", "name": "Uplinks Orphan SLA"})
        return rows

    _activate_plan_mocker(
        monkeypatch,
        host_get=host_get,
        item_get=item_get,
        trigger_get=trigger_get,
        **{
            "map.get": map_get,
            "dashboard.get": dashboard_get,
            "service.get": service_get,
            "sla.get": sla_get,
        },
    )

    report, err = build_zabbix_plan(None, inventory_file=str(inv))
    assert err is None
    planned = report["planned"]

    assert planned["aggregate_hosts"]["unchanged"]
    assert planned["aggregate_hosts"]["calculated_items"]["create"]
    assert planned["aggregate_hosts"]["limit_triggers"]["delete"]

    assert planned["maps"]["update"]
    assert planned["maps"]["delete"]

    assert planned["dashboards"]["unchanged"] or planned["dashboards"]["create"]
    assert planned["dashboards"]["create"] or planned["dashboards"]["update"]

    assert planned["services"]["delete"]
    assert planned["services"]["sla"]["unchanged"]
    assert planned["services"]["sla"]["delete"]


def test_plan_services_partial_read_suppresses_delete(monkeypatch, zabbix_env):
    uplink_ctx = {
        "providers": ["Cogent"],
        "burst_circuits": [],
        "provider_slo_percent": {},
    }

    def service_get(params):
        filt = set((params.get("filter") or {}).get("name") or [])
        rows = []
        if "Uplinks providers" in filt:
            rows.append({"serviceid": "801", "name": "Uplinks providers", "parents": []})
        if "Uplinks Cogent" in filt:
            rows.append({"serviceid": "802", "name": "Uplinks Cogent", "parents": []})
        if "Uplinks Cogent SLA source" in filt:
            rows.append({"serviceid": "803", "name": "Uplinks Cogent SLA source"})
        return rows

    mocker = build_standard_zabbix_mocker()
    mocker.on("service.get", service_get)
    mocker.on("sla.get", lambda p: [{"slaid": "901", "name": "Uplinks Cogent SLA", "slo": "99.95"}])
    mocker.activate(monkeypatch)

    plan, _current, err = _plan_services(
        "https://zabbix.example/api_jsonrpc.php",
        "token",
        uplink_ctx,
        parent_service="Uplinks providers",
        allow_delete=False,
    )
    assert err is None
    assert plan["delete"] == []
    assert plan["sla"]["delete"] == []
    assert plan["skipped"]
    assert "sla.create" in ZABBIX_MUTATING_METHODS
    assert "sla.update" in ZABBIX_MUTATING_METHODS
    assert "sla.delete" in ZABBIX_MUTATING_METHODS
    assert "sla.create" not in mocker.method_names()


def test_plan_unread_limits_no_aggregate_trigger_delete(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Cogent",
                        "commit_rate_kbps": 10000000,
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.9},
                "provider_limits_gbps": {},
                "provider_slo_read": "ok",
                "provider_limits_read": "partial_read",
            }
        ),
        encoding="utf-8",
    )

    _activate_plan_mocker(monkeypatch)

    report, err = build_zabbix_plan(None, inventory_file=str(inv))
    assert report is None
    assert err is not None
    assert "provider_limits_read=partial_read" in err


def test_plan_dashboard_no_aggregate_items_no_false_update(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Cogent",
                        "commit_rate_kbps": 10000000,
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    def host_get(params):
        filt = params.get("filter") or {}
        names = set(filt.get("host") or filt.get("name") or [])
        if "WAW-EQX-7280QR-2" in names:
            return [{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}]
        if "Uplinks Cogent" in names or "Uplinks_Cogent" in names:
            return [{"hostid": "201", "host": "Uplinks_Cogent", "name": "Uplinks Cogent"}]
        return []

    def item_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        name_search = (params.get("search") or {}).get("name", "")
        key_search = (params.get("search") or {}).get("key_", "")
        if "201" in hostids and "aggregate.bits" in key_search:
            return []
        if "102" in hostids and name_search == "Bits received":
            return [
                {
                    "itemid": "601",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits received",
                    "key_": "net.if.in[eth23]",
                }
            ]
        if "102" in hostids and name_search == "Bits sent":
            return [
                {
                    "itemid": "602",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits sent",
                    "key_": "net.if.out[eth23]",
                }
            ]
        return []

    def dashboard_get(params):
        name = _zabbix_filter_name(params)
        if name == DASHBOARD_NAME_BY_PROVIDER:
            return [
                {
                    "dashboardid": "41",
                    "name": DASHBOARD_NAME_BY_PROVIDER,
                    "pages": [
                        {
                            "name": "Cogent",
                            "widgets": [
                                {"name": "Cogent - Bits received (summary)"},
                                {"name": "Cogent - Bits sent (summary)"},
                            ],
                        }
                    ],
                }
            ]
        return []

    _activate_plan_mocker(
        monkeypatch,
        host_get=host_get,
        item_get=item_get,
        **{"dashboard.get": dashboard_get},
    )

    report, err = build_zabbix_plan(None, inventory_file=str(inv))
    assert err is None
    dashboards = report["planned"]["dashboards"]
    provider_updates = [
        row for row in dashboards.get("update") or [] if row.get("dashboard") == DASHBOARD_NAME_BY_PROVIDER
    ]
    assert provider_updates == []
    provider_unchanged = [
        row for row in dashboards.get("unchanged") or [] if row.get("dashboard") == DASHBOARD_NAME_BY_PROVIDER
    ]
    assert provider_unchanged


def test_plan_zabbix_items_read_failure_no_destructive_plan(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Cogent",
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )
    our_tag = {"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}
    stale_desc = "Interface ae5: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX)

    def host_get(params):
        filt = params.get("filter") or {}
        names = set(filt.get("host") or filt.get("name") or [])
        if "WAW-EQX-7280QR-2" in names:
            return [{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}]
        return []

    def trigger_get(params):
        return [{"triggerid": "900", "description": stale_desc, "tags": [our_tag]}]

    mocker = _activate_plan_mocker(monkeypatch, host_get=host_get, trigger_get=trigger_get)
    monkeypatch.setattr(
        "uplinks.zabbix.plan.fetch_zabbix_hosts_and_items",
        lambda *a, **k: (None, None, "item.get: simulated read failure"),
    )

    report, err = build_zabbix_plan(None, inventory_file=str(inv))
    assert err is None
    planned = report["planned"]
    assert planned["util_triggers"]["delete"] == []
    assert planned["dashboards"].get("status") == NOT_EVALUATED
    assert planned["aggregate_hosts"]["calculated_items"].get("status") == NOT_EVALUATED
    assert planned["aggregate_hosts"]["limit_triggers"].get("status") == NOT_EVALUATED
    assert planned["maps"].get("status") == NOT_EVALUATED
    assert planned["maps"]["create"] == []
    assert planned["maps"]["update"] == []
    assert planned["maps"]["delete"] == []
    assert "trigger.delete" not in mocker.method_names()
    util_skipped = [s for s in planned["util_triggers"]["skipped"] if s.get("suppressed") == "delete"]
    assert util_skipped


def test_plan_aggregate_item_get_error_dashboards_not_evaluated(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Cogent",
                        "commit_rate_kbps": 10000000,
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    def host_get(params):
        filt = params.get("filter") or {}
        names = set(filt.get("host") or filt.get("name") or [])
        if "WAW-EQX-7280QR-2" in names:
            return [{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}]
        if "Uplinks Cogent" in names or "Uplinks_Cogent" in names:
            return [{"hostid": "201", "host": "Uplinks_Cogent", "name": "Uplinks Cogent"}]
        return []

    def item_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        name_search = (params.get("search") or {}).get("name", "")
        key_search = (params.get("search") or {}).get("key_", "")
        if "201" in hostids and "aggregate.bits" in key_search:
            return None  # force mocker error path
        if "102" in hostids and name_search == "Bits received":
            return [
                {
                    "itemid": "601",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits received",
                    "key_": "net.if.in[eth23]",
                }
            ]
        if "102" in hostids and name_search == "Bits sent":
            return [
                {
                    "itemid": "602",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits sent",
                    "key_": "net.if.out[eth23]",
                }
            ]
        return []

    def item_get_fail(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        key_search = (params.get("search") or {}).get("key_", "")
        if "201" in hostids and "aggregate.bits" in key_search:
            raise RuntimeError("item.get aggregate simulated failure")
        return item_get(params)

    _activate_plan_mocker(monkeypatch, host_get=host_get, item_get=item_get_fail)

    report, err = build_zabbix_plan(None, inventory_file=str(inv))
    assert err is None
    dashboards = report["planned"]["dashboards"]
    assert dashboards.get("status") == NOT_EVALUATED
    assert "item.get aggregate" in dashboards.get("reason", "")
    assert dashboards.get("update") == []


def test_plan_slo_read_error_no_sla_plan(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Cogent",
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "provider_slo_percent": {},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "partial_read",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    _activate_plan_mocker(monkeypatch)

    report, err = build_zabbix_plan(None, inventory_file=str(inv))
    assert report is None
    assert err is not None
    assert "provider_slo_read=partial_read" in err


def test_plan_dashboard_update_suppressed_when_destructive_disabled(monkeypatch, zabbix_env):
    edges = [
        (
            "WAW-EQX-7280QR-2",
            "102",
            "Ethernet23/1",
            "Cogent",
            "601",
            "602",
            False,
            False,
            False,
        )
    ]
    expected = _expected_main_dashboard_identities(edges)

    def dashboard_get(params):
        return [
            {
                "dashboardid": "31",
                "name": DASHBOARD_NAME,
                "pages": [{"widgets": [{"name": "stale widget only"}]}],
            }
        ]

    mocker = build_standard_zabbix_mocker()
    mocker.on("dashboard.get", dashboard_get)
    mocker.activate(monkeypatch)

    plan, _current, err = _plan_one_dashboard(
        "https://zabbix.example/api_jsonrpc.php",
        "token",
        DASHBOARD_NAME,
        expected,
        allow_destructive=False,
    )
    assert err is None
    assert plan["update"] == []
    suppressed = [row for row in plan["skipped"] if row.get("suppressed") == "update"]
    assert len(suppressed) == 1
    assert suppressed[0]["dashboard"] == DASHBOARD_NAME


def test_plan_maps_not_evaluated_when_zabbix_items_unread(monkeypatch, zabbix_env, tmp_path):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "WAW-EQX-7280QR-2",
                        "interface": "Ethernet23/1",
                        "provider": "Cogent",
                        "billing_model": "Flat",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "incomplete": 0, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.9, "Hurricane": 99.9},
                "provider_limits_gbps": {"Cogent": 10, "Hurricane": 5},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    def host_get(params):
        filt = params.get("filter") or {}
        names = set(filt.get("host") or filt.get("name") or [])
        if "WAW-EQX-7280QR-2" in names:
            return [{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}]
        return []

    def map_get(params):
        return [
            {
                "sysmapid": "9",
                "name": MAP_NAME,
                "selements": [
                    {
                        "selementid": "11",
                        "elementtype": 0,
                        "elementid": 999,
                        "elements": [{"hostid": "999"}],
                    },
                    {"selementid": "12", "elementtype": 4, "label": "StaleISP"},
                ],
                "links": [],
            }
        ]

    mocker = _activate_plan_mocker(
        monkeypatch,
        host_get=host_get,
        **{"map.get": map_get},
    )
    monkeypatch.setattr(
        "uplinks.zabbix.plan.fetch_zabbix_hosts_and_items",
        lambda *a, **k: (None, None, "item.get: simulated read failure"),
    )

    report, err = build_zabbix_plan(None, inventory_file=str(inv))
    assert err is None
    maps = report["planned"]["maps"]
    assert maps.get("status") == NOT_EVALUATED
    assert maps.get("create") == []
    assert maps.get("update") == []
    assert maps.get("delete") == []
    assert "map diff suppressed" in maps.get("reason", "")
    assert report["zabbix_current"]["maps"]["sysmapid"] == "9"
    assert "map.get" in mocker.method_names()
    assert "map.create" not in mocker.method_names()
    assert "map.update" not in mocker.method_names()
    assert "map.delete" not in mocker.method_names()


def test_plan_stale_aggregate_limit_trigger_skipped_when_allow_delete_false(
    monkeypatch, zabbix_env, netbox_env, tmp_path
):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text(
        json.dumps(
            {
                "devices": {
                    "WAW-EQX-7280QR-2": [
                        {
                            "name": "Ethernet23/1",
                            "description": "Uplink: Cogent 10G",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    stale_limit_desc = "Provider aggregate traffic >= 100% of limit (5 Gbps)"

    def host_get(params):
        filt = params.get("filter") or {}
        names = set(filt.get("host") or filt.get("name") or [])
        if "WAW-EQX-7280QR-2" in names:
            return [{"hostid": "102", "host": "WAW-EQX-7280QR-2", "name": "WAW-EQX-7280QR-2"}]
        if "Uplinks Cogent" in names or "Uplinks_Cogent" in names:
            return [{"hostid": "201", "host": "Uplinks_Cogent", "name": "Uplinks Cogent"}]
        return []

    def item_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        name_search = (params.get("search") or {}).get("name", "")
        key_search = (params.get("search") or {}).get("key_", "")
        if "201" in hostids and "aggregate.bits" in key_search:
            return [
                {"itemid": "501", "key_": "aggregate.bits.in[]"},
                {"itemid": "502", "key_": "aggregate.bits.out[]"},
            ]
        if "102" in hostids and name_search == "Bits received":
            return [
                {
                    "itemid": "601",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits received",
                    "key_": "net.if.in[eth23]",
                }
            ]
        if "102" in hostids and name_search == "Bits sent":
            return [
                {
                    "itemid": "602",
                    "hostid": "102",
                    "name": "Interface Ethernet23/1: Bits sent",
                    "key_": "net.if.out[eth23]",
                }
            ]
        return []

    def trigger_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        if "201" in hostids:
            return [{"triggerid": "701", "description": stale_limit_desc}]
        return []

    nb = build_netbox_for_commit_rates(
        device_name="WAW-EQX-7280QR-2",
        iface_name="Ethernet23/1",
        provider_name="Cogent",
        device_tag="border",
        tag_device=True,
    )
    mocker = _activate_plan_mocker(monkeypatch, host_get=host_get, item_get=item_get, trigger_get=trigger_get)
    monkeypatch.setattr("uplinks.zabbix.plan.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(
        "uplinks.zabbix.plan.collect_provider_slo_percent",
        lambda nb, debug=False: ({}, "partial_read"),
    )
    monkeypatch.setattr(
        "uplinks.zabbix.plan.collect_provider_limits_gbps",
        lambda nb, debug=False: ({"Cogent": 10.0}, None),
    )

    report, err = build_zabbix_plan(str(dry))
    assert err is None
    limit_triggers = report["planned"]["aggregate_hosts"]["limit_triggers"]
    assert limit_triggers.get("delete") == []
    assert limit_triggers.get("update") == []
    suppressed = [
        row for row in limit_triggers.get("skipped") or [] if row.get("suppressed") == "delete"
    ]
    assert len(suppressed) == 1
    assert suppressed[0]["triggerid"] == "701"
    assert "stale aggregate limit" in suppressed[0].get("reason", "")
    assert "trigger.delete" not in mocker.method_names()


def test_burst_trigger_matches_spec_dependency_exact_match():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec

    spec = {
        "description": "d",
        "expression": "e",
        "priority": "4",
        "tags": [],
        "depends_on_role": "high",
    }
    trig = {
        "description": "d",
        "expression": "e",
        "priority": "4",
        "tags": [],
        "status": "0",
        "dependencies": [{"triggerid": "100"}],
    }
    assert _burst_trigger_matches_spec(trig, spec, {"high": "100"}) is True


def test_burst_trigger_matches_spec_missing_expected_dependency():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec

    spec = {
        "description": "d",
        "expression": "e",
        "priority": "3",
        "tags": [],
        "depends_on_role": "high",
    }
    trig = {
        "description": "d",
        "expression": "e",
        "priority": "3",
        "tags": [],
        "dependencies": [],
    }
    assert _burst_trigger_matches_spec(trig, spec, {"high": "100"}) is False


def test_burst_trigger_matches_spec_extra_dependency():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec

    spec = {
        "description": "d",
        "expression": "e",
        "priority": "3",
        "tags": [],
        "depends_on_role": "high",
    }
    trig = {
        "description": "d",
        "expression": "e",
        "priority": "3",
        "tags": [],
        "dependencies": [{"triggerid": "100"}, {"triggerid": "999"}],
    }
    assert _burst_trigger_matches_spec(trig, spec, {"high": "100"}) is False


def test_burst_trigger_matches_spec_no_dependency_requires_empty():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec

    spec = {
        "description": "d",
        "expression": "e",
        "priority": "5",
        "tags": [],
        "depends_on_role": None,
    }
    ok = {
        "description": "d",
        "expression": "e",
        "priority": "5",
        "tags": [],
        "status": "0",
        "dependencies": [],
    }
    extra = dict(ok, dependencies=[{"triggerid": "100"}])
    assert _burst_trigger_matches_spec(ok, spec, {}) is True
    assert _burst_trigger_matches_spec(extra, spec, {}) is False


def test_burst_trigger_matches_spec_duplicate_dependency():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec

    spec = {
        "description": "d",
        "expression": "e",
        "priority": "3",
        "tags": [],
        "depends_on_role": "high",
    }
    trig = {
        "description": "d",
        "expression": "e",
        "priority": "3",
        "tags": [],
        "status": "0",
        "dependencies": [{"triggerid": "100"}, {"triggerid": "100"}],
    }
    assert _burst_trigger_matches_spec(trig, spec, {"high": "100"}) is False


def test_burst_trigger_matches_spec_disabled_trigger_mismatch():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec

    spec = {
        "description": "d",
        "expression": "e",
        "priority": "4",
        "tags": [],
        "depends_on_role": None,
    }
    trig = {
        "description": "d",
        "expression": "e",
        "priority": "4",
        "tags": [],
        "status": "1",
        "dependencies": [],
    }
    assert _burst_trigger_matches_spec(trig, spec, {}) is False


def test_burst_trigger_matches_spec_enabled_exact_match():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec

    spec = {
        "description": "d",
        "expression": "e",
        "priority": "4",
        "tags": [],
        "depends_on_role": "high",
    }
    trig = {
        "description": "d",
        "expression": "e",
        "priority": "4",
        "tags": [],
        "status": "0",
        "dependencies": [{"triggerid": "100"}],
    }
    assert _burst_trigger_matches_spec(trig, spec, {"high": "100"}) is True


def _zabbix_canonical_burst_row(spec, triggerid, functionid, itemid, dependencies=None):
    from uplinks.zabbix.plan import _parse_burst_simple_expression

    parsed = _parse_burst_simple_expression(spec["expression"])
    assert parsed is not None
    return {
        "triggerid": triggerid,
        "description": spec["description"],
        "expression": "{{{}}}>{}".format(functionid, parsed["threshold"]),
        "priority": spec["priority"],
        "tags": spec["tags"],
        "status": "0",
        "dependencies": dependencies or [],
        "functions": [
            {
                "functionid": functionid,
                "itemid": itemid,
                "function": parsed["function"],
                "parameter": "$,{}".format(parsed["parameter"]),
            }
        ],
        "items": [{"itemid": itemid, "key_": parsed["item_key"]}],
    }


def test_burst_trigger_matches_spec_canonical_expression_equivalent():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    spec = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )[0]
    trig = _zabbix_canonical_burst_row(spec, "100", "50001", "60001")
    assert trig["functions"][0]["parameter"] == "$,15m"
    assert _burst_trigger_matches_spec(trig, spec, {}) is True

    spaced = _zabbix_canonical_burst_row(spec, "102", "50004", "60001")
    spaced["functions"][0]["parameter"] = "$ , 15m"
    assert _burst_trigger_matches_spec(spaced, spec, {}) is True


def test_burst_trigger_expression_exact_match_rejects_contradictory_metadata():
    from uplinks.zabbix.plan import _burst_trigger_expression_matches
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    spec = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )[0]
    expression = spec["expression"]
    base = {
        "expression": expression,
        "functions": [
            {
                "functionid": "50001",
                "itemid": "60001",
                "function": "max",
                "parameter": "$,15m",
            }
        ],
        "items": [{"itemid": "60001", "key_": item_key}],
    }

    assert _burst_trigger_expression_matches(base, expression) is True

    wrong_key = dict(base, items=[{"itemid": "60001", "key_": 'net.if.in["Ethernet52/1"]'}])
    assert _burst_trigger_expression_matches(wrong_key, expression) is False

    wrong_fn = dict(
        base,
        functions=[dict(base["functions"][0], function="last")],
    )
    assert _burst_trigger_expression_matches(wrong_fn, expression) is False

    wrong_param = dict(
        base,
        functions=[dict(base["functions"][0], parameter="$,5m")],
    )
    assert _burst_trigger_expression_matches(wrong_param, expression) is False


def test_burst_trigger_matches_spec_canonical_wrong_item_key():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    spec = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )[0]
    trig = _zabbix_canonical_burst_row(spec, "100", "50001", "60001")
    trig["items"][0]["key_"] = 'net.if.in["Ethernet52/1"]'
    assert _burst_trigger_matches_spec(trig, spec, {}) is False


def test_burst_trigger_matches_spec_canonical_wrong_function_or_parameter():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    spec = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )[0]
    wrong_fn = _zabbix_canonical_burst_row(spec, "100", "50001", "60001")
    wrong_fn["functions"][0]["function"] = "last"
    assert _burst_trigger_matches_spec(wrong_fn, spec, {}) is False

    wrong_period = _zabbix_canonical_burst_row(spec, "101", "50002", "60002")
    wrong_period["functions"][0]["parameter"] = "$,5m"
    assert _burst_trigger_matches_spec(wrong_period, spec, {}) is False


def test_burst_trigger_matches_spec_canonical_wrong_functionid():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    spec = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )[0]
    trig = _zabbix_canonical_burst_row(spec, "100", "50001", "60001")
    trig["expression"] = trig["expression"].replace("{50001}", "{50002}", 1)
    assert _burst_trigger_matches_spec(trig, spec, {}) is False


def test_burst_trigger_matches_spec_canonical_sla_period_parameter():
    from uplinks.zabbix.plan import _burst_trigger_matches_spec
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    spec = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )[2]
    trig = _zabbix_canonical_burst_row(spec, "300", "50003", "60001")
    assert trig["functions"][0]["parameter"] == "$,1h"
    assert _burst_trigger_matches_spec(trig, spec, {}) is True


def test_plan_burst_triggers_unchanged_canonical_expression(monkeypatch, zabbix_env):
    from uplinks.zabbix.plan import _plan_burst_triggers
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    specs = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )
    high, warn, sla = specs
    existing = [
        _zabbix_canonical_burst_row(high, "100", "50001", "60001"),
        _zabbix_canonical_burst_row(warn, "200", "50002", "60001", dependencies=[{"triggerid": "100"}]),
        _zabbix_canonical_burst_row(sla, "300", "50003", "60001"),
    ]

    monkeypatch.setattr(
        "zabbix_sync_commit_rate.get_bits_received_item_key",
        lambda url, token, hostid, iface_name, debug=False: item_key,
    )
    captured = []

    def trigger_get(params):
        captured.append(params)
        return existing

    (ZabbixRpcMocker().on("trigger.get", trigger_get).activate(monkeypatch))

    plan, _, err = _plan_burst_triggers(
        "https://z.example/api_jsonrpc.php",
        "token",
        [(dev, iface)],
        {(dev, iface): {"provider": "Cogent", "circuit_id": "CKT-1"}},
        {dev: "101"},
        {"101": dev},
        allow_delete=True,
    )
    assert err is None
    assert plan["update"] == []
    assert plan["create"] == []
    assert plan["unchanged"]
    assert captured
    assert captured[0].get("selectFunctions") == "extend"
    assert captured[0].get("selectItems") == ["itemid", "key_"]


def test_plan_burst_triggers_create_update_unchanged(monkeypatch, zabbix_env):
    from uplinks.zabbix.plan import _plan_burst_triggers
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    specs = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )
    high = specs[0]
    existing = [
        {
            "triggerid": "100",
            "description": high["description"],
            "expression": high["expression"],
            "priority": high["priority"],
            "tags": high["tags"],
            "dependencies": [],
        },
    ]

    monkeypatch.setattr(
        "zabbix_sync_commit_rate.get_bits_received_item_key",
        lambda url, token, hostid, iface_name, debug=False: item_key,
    )
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: existing)
        .activate(monkeypatch)
    )

    plan, current, err = _plan_burst_triggers(
        "https://z.example/api_jsonrpc.php",
        "token",
        [(dev, iface)],
        {(dev, iface): {"provider": "Cogent", "circuit_id": "CKT-1"}},
        {dev: "101"},
        {"101": dev},
        allow_delete=True,
    )
    assert err is None
    assert plan["create"]
    assert any("SLA breach" in t for row in plan["create"] for t in row.get("triggers", []))
    assert plan["unchanged"]
    assert current.get(dev)


def test_plan_burst_triggers_ambiguous_match_skips_create(monkeypatch, zabbix_env):
    from uplinks.zabbix.plan import _plan_burst_triggers
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    specs = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )
    high = specs[0]
    existing = [
        {
            "triggerid": "100",
            "description": high["description"],
            "expression": high["expression"],
            "priority": high["priority"],
            "tags": high["tags"],
            "dependencies": [],
        },
        {
            "triggerid": "101",
            "description": high["description"],
            "expression": "other-expression",
            "priority": high["priority"],
            "tags": high["tags"],
            "dependencies": [],
        },
    ]

    monkeypatch.setattr(
        "zabbix_sync_commit_rate.get_bits_received_item_key",
        lambda url, token, hostid, iface_name, debug=False: item_key,
    )
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: existing)
        .activate(monkeypatch)
    )

    plan, _, err = _plan_burst_triggers(
        "https://z.example/api_jsonrpc.php",
        "token",
        [(dev, iface)],
        {(dev, iface): {"provider": "Cogent", "circuit_id": "CKT-1"}},
        {dev: "101"},
        {"101": dev},
        allow_delete=True,
    )
    assert err is None
    assert any(
        row.get("reason") == "ambiguous burst trigger description match"
        and row.get("role") == high["role"]
        for row in plan["skipped"]
    )
    assert not any(
        high["description"] in (row.get("triggers") or [])
        for row in plan["create"]
    )
    assert plan["delete"] == []


def test_plan_burst_triggers_duplicate_high_skips_warn(monkeypatch, zabbix_env):
    from uplinks.zabbix.plan import _plan_burst_triggers
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    specs = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )
    high = specs[0]
    warn = specs[1]
    existing = [
        {
            "triggerid": "100",
            "description": high["description"],
            "expression": high["expression"],
            "priority": high["priority"],
            "tags": high["tags"],
            "dependencies": [],
        },
        {
            "triggerid": "101",
            "description": high["description"],
            "expression": "other-expression",
            "priority": high["priority"],
            "tags": high["tags"],
            "dependencies": [],
        },
        {
            "triggerid": "200",
            "description": warn["description"],
            "expression": "stale-warn-expression",
            "priority": warn["priority"],
            "tags": warn["tags"],
            "dependencies": [],
        },
    ]

    monkeypatch.setattr(
        "zabbix_sync_commit_rate.get_bits_received_item_key",
        lambda url, token, hostid, iface_name, debug=False: item_key,
    )
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: existing)
        .activate(monkeypatch)
    )

    plan, _, err = _plan_burst_triggers(
        "https://z.example/api_jsonrpc.php",
        "token",
        [(dev, iface)],
        {(dev, iface): {"provider": "Cogent", "circuit_id": "CKT-1"}},
        {dev: "101"},
        {"101": dev},
        allow_delete=True,
    )
    assert err is None
    assert any(
        row.get("reason") == "ambiguous burst trigger description match"
        and row.get("role") == "high"
        for row in plan["skipped"]
    )
    assert any(
        row.get("reason") == "burst trigger dependency unavailable"
        and row.get("role") == "warn"
        for row in plan["skipped"]
    )
    assert plan["update"] == []
    assert not any(
        warn["description"] in (row.get("triggers") or [])
        for row in plan["create"]
    )


def test_plan_burst_triggers_update_on_expression_mismatch(monkeypatch, zabbix_env):
    from uplinks.zabbix.plan import _plan_burst_triggers
    from zabbix_sync_commit_rate import expected_burst_trigger_specs

    dev = "ALA-KZT-7280TR-1"
    iface = "Ethernet51/1"
    item_key = 'net.if.in["Ethernet51/1"]'
    specs = expected_burst_trigger_specs(
        dev, iface, item_key, provider="Cogent", circuit_id="CKT-1"
    )
    high = specs[0]
    stale_tags = [{"tag": "billing", "value": "burst"}, {"tag": "scripts", "value": "automatization"}]
    existing = [
        {
            "triggerid": "100",
            "description": high["description"],
            "expression": "broken-expression",
            "priority": high["priority"],
            "tags": stale_tags,
            "dependencies": [],
        },
    ]

    monkeypatch.setattr(
        "zabbix_sync_commit_rate.get_bits_received_item_key",
        lambda url, token, hostid, iface_name, debug=False: item_key,
    )
    (
        ZabbixRpcMocker()
        .on("trigger.get", lambda p: existing)
        .activate(monkeypatch)
    )

    plan, _, err = _plan_burst_triggers(
        "https://z.example/api_jsonrpc.php",
        "token",
        [(dev, iface)],
        {(dev, iface): {"provider": "Cogent", "circuit_id": "CKT-1"}},
        {dev: "101"},
        {"101": dev},
        allow_delete=True,
    )
    assert err is None
    assert plan["update"]
    assert plan["update"][0]["triggerid"] == "100"
    assert plan["update"][0]["description"] == high["description"]
    assert plan["create"]


def test_build_zabbix_plan_empty_burst_scope_with_create_link_triggers(
    monkeypatch, zabbix_env, netbox_env, tmp_path
):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "ALA-KZT-7280TR-1",
                        "interface": "Ethernet51/1",
                        "provider": "Cogent",
                        "billing_model": "Commit",
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "providers": 1, "providers_in_scope": 1},
                "provider_slo_percent": {"Cogent": 99.9},
                "provider_limits_gbps": {"Cogent": 10},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
                "netbox_interface_relations": {},
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

    _activate_plan_mocker(monkeypatch, host_get=host_get)

    report, err = build_zabbix_plan(
        None,
        inventory_file=str(inv),
        create_link_triggers=True,
    )
    assert err is None
    burst = report["planned"]["burst_triggers"]
    assert burst.get("status") != "not_evaluated"
    assert burst["create"] == []
    assert burst["update"] == []
    assert burst["delete"] == []
