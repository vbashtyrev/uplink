"""Scoped NetBox inventory fixtures for dry-ssh integration tests."""


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
        "stats": {},
        "read_error": False,
    }
