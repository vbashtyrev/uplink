"""Shared JSON data loaders for dry-ssh and description maps."""

import json

DEFAULT_DRY_SSH_FILE = "dry-ssh.json"
DEFAULT_DESCRIPTION_MAP_FILE = "description_to_name.json"

# Backward-compatible aliases used by CLI scripts.
DEFAULT_INPUT = DEFAULT_DRY_SSH_FILE
DESCRIPTION_MAP_FILE = DEFAULT_DESCRIPTION_MAP_FILE


def load_devices_json(path):
    """Load JSON with the key devices. Return (data, None) or (None, error_msg)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "file not found: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "JSON error: {}".format(e)
    if "devices" not in data:
        return None, "the file does not contain the 'devices' key"
    return data, None


def load_description_map(path):
    """Load mapping description -> display name. Empty dict if file does not exist."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


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
