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
    "Provide --inventory-file (NetBox-only path). "
    "For legacy dry-ssh.json use --legacy-dry-ssh with -f/--file or -d/--dry-ssh."
)
LEGACY_DRY_SSH_REQUIRED_MSG = (
    "dry-ssh input requires --legacy-dry-ssh; use --inventory-file for the NetBox-only path"
)
LEGACY_DRY_SSH_PATH_REQUIRED_MSG = (
    "Pass -f/--file or -d/--dry-ssh with --legacy-dry-ssh"
)
INVENTORY_AND_DRY_SSH_CONFLICT_MSG = (
    "Cannot use --inventory-file together with -f/--file or -d/--dry-ssh"
)
MAP_LEGACY_UTILITY_REQUIRES_LEGACY_MSG = (
    "Map utility modes --create-map and --export-map require --legacy-dry-ssh; "
    "use --inventory-file with --zabbix/--update-map for the NetBox-only path"
)
GENERATE_DESCRIPTION_MAP_REQUIRES_LEGACY_MSG = (
    "--generate-description-map requires --legacy-dry-ssh with -f/--file"
)


def resolve_uplink_cli_input(inventory_file=None, dry_ssh_file=None, legacy_dry_ssh=False):
    """
    Validate NetBox-only vs legacy dry-ssh CLI input.

    Return (mode, path, error_msg) where mode is 'inventory' or 'legacy_dry_ssh'.
    """
    if inventory_file and dry_ssh_file:
        return None, None, INVENTORY_AND_DRY_SSH_CONFLICT_MSG
    if inventory_file:
        return "inventory", inventory_file, None
    if dry_ssh_file:
        if not legacy_dry_ssh:
            return None, None, LEGACY_DRY_SSH_REQUIRED_MSG
        return "legacy_dry_ssh", dry_ssh_file, None
    if legacy_dry_ssh:
        return None, None, LEGACY_DRY_SSH_PATH_REQUIRED_MSG
    return None, None, INVENTORY_FILE_REQUIRED_MSG
