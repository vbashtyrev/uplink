"""NetBox inventory CLI input helpers for Zabbix uplinks scripts."""

INVENTORY_FILE_REQUIRED_MSG = (
    "Provide --inventory-file (NetBox snapshot path for Zabbix uplinks scripts)."
)


def resolve_uplink_cli_input(inventory_file=None):
    """
    Validate NetBox inventory CLI input for Zabbix uplinks scripts.

    Return (mode, path, error_msg) where mode is 'inventory'.
    """
    if inventory_file:
        return "inventory", inventory_file, None
    return None, None, INVENTORY_FILE_REQUIRED_MSG
