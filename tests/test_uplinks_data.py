"""NetBox inventory CLI input helpers."""
from uplinks.data import INVENTORY_FILE_REQUIRED_MSG, resolve_uplink_cli_input

def test_resolve_uplink_cli_input_inventory():
    mode, path, err = resolve_uplink_cli_input(inventory_file='/tmp/inv.json')
    assert err is None
    assert mode == 'inventory'
    assert path == '/tmp/inv.json'

def test_resolve_uplink_cli_input_missing():
    mode, path, err = resolve_uplink_cli_input()
    assert mode is None
    assert path is None
    assert err == INVENTORY_FILE_REQUIRED_MSG
