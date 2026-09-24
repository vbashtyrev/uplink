"""Scoped NetBox inventory fixtures for dry-ssh integration tests."""

import json

from uplinks.netbox import inventory as inv_mod


def dry_ssh_minimal_netbox_interface_relations():
    """LAG relations for FRN-MX-1 in tests/fixtures/dry_ssh_minimal.json."""
    return {
        "member_to_aggregate": {("FRN-MX-1", "et-0/0/1"): "ae5"},
        "parent_children": {("FRN-MX-1", "ae5"): {"ae5.0"}},
        "display_names": {
            ("FRN-MX-1", "et-0/0/1"): "et-0/0/1",
            ("FRN-MX-1", "ae5"): "ae5",
            ("FRN-MX-1", "ae5.0"): "ae5.0",
        },
    }


def with_provider_metadata(report, slo=None, limits=None):
    """Attach successful provider metadata snapshot fields to an inventory report."""
    enriched = dict(report)
    enriched.setdefault(
        "provider_slo_percent",
        slo or {"Cogent": 99.9, "Hurricane": 99.9},
    )
    enriched.setdefault(
        "provider_limits_gbps",
        limits or {"Cogent": 10.0, "Hurricane": 5.0},
    )
    enriched["provider_slo_read"] = "ok"
    enriched["provider_limits_read"] = "ok"
    return enriched


def dry_ssh_minimal_inventory_report():
    """Inventory report matching tests/fixtures/dry_ssh_minimal.json."""
    return with_provider_metadata(
        {
            "complete": [
                {
                    "device": "ALA-KZT-7280TR-1",
                    "interface": "ethernet51/1",
                    "provider": "Cogent",
                    "commit_rate_kbps": 10_000_000,
                },
                {
                    "device": "FRN-MX-1",
                    "interface": "ae5.0",
                    "provider": "Hurricane",
                    "commit_rate_kbps": 10_000_000,
                },
                {
                    "device": "FRN-MX-1",
                    "interface": "et-0/0/1",
                    "provider": "Hurricane",
                    "commit_rate_kbps": 10_000_000,
                },
                {
                    "device": "FRN-MX-1",
                    "interface": "ae5",
                    "provider": "Hurricane",
                    "commit_rate_kbps": 10_000_000,
                },
            ],
            "incomplete": [],
            "stats": {"complete": 4, "providers": 2, "providers_in_scope": 2},
            "netbox_interface_relations": inv_mod.serialize_netbox_interface_relations(
                dry_ssh_minimal_netbox_interface_relations()
            ),
        }
    )


def write_dry_ssh_minimal_inventory(tmp_path, filename="inventory.json"):
    """Write minimal scoped inventory JSON for NetBox-only CLI tests."""
    path = tmp_path / filename
    path.write_text(json.dumps(dry_ssh_minimal_inventory_report()), encoding="utf-8")
    return path


def write_inventory_report(tmp_path, report, filename="inventory.json"):
    """Write an arbitrary inventory report JSON for CLI tests."""
    path = tmp_path / filename
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def dry_ssh_minimal_inventory_context():
    """
    Circuit-scoped inventory matching tests/fixtures/dry_ssh_minimal.json.
    Physical et-0/0/1 is expanded to logical ae5.0 for Zabbix-facing lookups.
    """
    return {
        "device_iface_to_provider": {
            ("ALA-KZT-7280TR-1", "ethernet51/1"): "Cogent",
            ("FRN-MX-1", "ae5.0"): "Hurricane",
            ("FRN-MX-1", "et-0/0/1"): "Hurricane",
            ("FRN-MX-1", "ae5"): "Hurricane",
        },
        "providers": {"Cogent", "Hurricane"},
        "netbox_interface_relations": dry_ssh_minimal_netbox_interface_relations(),
        "stats": {},
        "read_error": False,
    }
