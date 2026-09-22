"""Extended tests for zabbix_sync_commit_rate.py."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_sync_commit_rate import (
    _is_netbox_auth_error,
    _macro_name_for_interface,
    _macro_name_util_crit_for_interface,
    _macro_name_util_warn_for_interface,
    _macro_name_warn_for_interface,
    _pick_one_logical,
    apply_logical_context,
    build_bandwidth_util_expression,
    build_physical_to_logical,
    get_bits_received_item_key,
    get_net_if_bandwidth_item_keys,
    set_zabbix_host_macros_for_prefixes,
)


def test_macro_names():
    assert _macro_name_for_interface("Eth1") == '{$UPLINK.BPS.MAX:"Eth1"}'
    assert _macro_name_warn_for_interface("Eth1") == '{$UPLINK.BPS.WARN:"Eth1"}'
    assert _macro_name_util_warn_for_interface("Eth1") == '{$UPLINK.UTIL.WARN:"Eth1"}'
    assert _macro_name_util_crit_for_interface("Eth1") == '{$UPLINK.UTIL.CRIT:"Eth1"}'


def test_pick_one_logical_prefers_unit0():
    assert _pick_one_logical(["ae5.100", "ae5.0"]) == "ae5.0"
    assert _pick_one_logical(["ae5"]) == "ae5"


def test_build_physical_to_logical_and_apply_context():
    devices = {
        "mx1": [
            {"name": "ae5.0", "physicalInterface": "ae5"},
            {"name": "ae5.100", "physicalInterface": "ae5"},
        ],
    }
    phys_map = build_physical_to_logical(devices)
    assert phys_map[("mx1", "ae5")] == ["ae5.0", "ae5.100"]

    commit = {("mx1", "ae5"): 10_000_000_000}
    mapped = apply_logical_context(commit, devices)
    assert mapped[("mx1", "ae5.0")] == 10_000_000_000


MSK_MX204_1_DRY_SSH = {
    "MSK-M9-MX204-1": [
        {"name": "ae5", "isLag": True},
        {
            "name": "ae5.0",
            "physicalInterface": "ae5",
            "isLogical": True,
            "description": "Uplink: Beeline 5",
        },
        {
            "name": "et-0/0/3",
            "aggregateInterface": "ae5",
            "description": "Beeline 5",
        },
    ],
}

MSK_MX204_2_DRY_SSH = {
    "MSK-M9-MX204-2": [
        {"name": "ae3", "isLag": True},
        {
            "name": "ae3.0",
            "physicalInterface": "ae3",
            "isLogical": True,
            "description": "Uplink: Ertelecom 1",
        },
        {
            "name": "et-0/0/3",
            "aggregateInterface": "ae3",
            "description": "Ertelecom",
        },
    ],
}


def test_build_physical_to_logical_lag_member_et003_to_ae50():
    """NetBox cable on et-0/0/3; Zabbix commit macro uses ae5.0 via aggregateInterface."""
    phys_map = build_physical_to_logical(MSK_MX204_1_DRY_SSH)
    assert phys_map[("MSK-M9-MX204-1", "ae5")] == ["ae5.0"]
    assert phys_map[("MSK-M9-MX204-1", "et-0/0/3")] == ["ae5.0"]

    commit = {("MSK-M9-MX204-1", "et-0/0/3"): 20_000_000_000}
    mapped = apply_logical_context(commit, MSK_MX204_1_DRY_SSH)
    assert mapped == {("MSK-M9-MX204-1", "ae5.0"): 20_000_000_000}


def test_build_physical_to_logical_lag_member_et003_to_ae30():
    """NetBox cable on et-0/0/3; Zabbix commit macro uses ae3.0 via aggregateInterface."""
    phys_map = build_physical_to_logical(MSK_MX204_2_DRY_SSH)
    assert phys_map[("MSK-M9-MX204-2", "ae3")] == ["ae3.0"]
    assert phys_map[("MSK-M9-MX204-2", "et-0/0/3")] == ["ae3.0"]

    commit = {("MSK-M9-MX204-2", "et-0/0/3"): 25_000_000_000}
    mapped = apply_logical_context(commit, MSK_MX204_2_DRY_SSH)
    assert mapped == {("MSK-M9-MX204-2", "ae3.0"): 25_000_000_000}


def test_build_bandwidth_util_expression():
    keys = {"in": "net.if.in[1]", "out": "net.if.out[1]", "speed": "net.if.speed[1]"}
    expr = build_bandwidth_util_expression("host1", keys, "{$UPLINK.UTIL.WARN:\"Eth1\"}", "10m")
    assert "avg(/host1/net.if.in[1],10m)" in expr
    assert "{$UPLINK.UTIL.WARN:\"Eth1\"}" in expr
    assert "net.if.speed[1]" in expr


def test_is_netbox_auth_error():
    assert _is_netbox_auth_error(Exception("HTTP 403 Forbidden")) is True
    assert _is_netbox_auth_error(Exception("The request failed with code 401 Unauthorized: {}")) is True
    assert _is_netbox_auth_error(Exception("token expired")) is True
    assert _is_netbox_auth_error(
        Exception("The requested url: https://netbox.example/api/dcim/cables/403/ could not be found.")
    ) is False
    assert _is_netbox_auth_error(Exception("timeout")) is False


def test_set_zabbix_host_macros_for_prefixes(monkeypatch):
    deleted = []
    created = []

    def macro_get(params):
        if "search" in params:
            return [{"hostmacroid": "10", "macro": "{$UPLINK.BPS.MAX}"}]
        return []

    mocker = (
        ZabbixRpcMocker()
        .on("usermacro.get", macro_get)
        .on("usermacro.delete", lambda p: deleted.extend(p) or True)
        .on("usermacro.create", lambda p: created.extend(p) or {"hostmacroids": ["1"]})
    )
    mocker.activate(monkeypatch)

    ok, err = set_zabbix_host_macros_for_prefixes(
        "https://z.example/api_jsonrpc.php",
        "t",
        "1",
        [{"macro": "{$UPLINK.BPS.MAX}", "value": "2000", "type": "0"}],
        ["{$UPLINK.BPS.MAX"],
    )
    assert ok is True
    assert err is None
    assert deleted == ["10"]
    assert created


def test_get_bits_received_item_key(monkeypatch):
    items = [
        {
            "key_": "net.if.in[ifHCInOctets.51/1]",
            "name": "Interface Ethernet51/1(ethernet51/1): Bits received",
        },
    ]
    ZabbixRpcMocker().on("item.get", lambda p: items).activate(monkeypatch)
    key = get_bits_received_item_key("https://z.example/api_jsonrpc.php", "t", "1", "Ethernet51/1")
    assert key == "net.if.in[ifHCInOctets.51/1]"


def test_get_net_if_bandwidth_item_keys(monkeypatch):
    items = [
        {"key_": "net.if.in[1]", "name": "Interface Ethernet51/1(x): ..."},
        {"key_": "net.if.out[1]", "name": "Interface Ethernet51/1(x): ..."},
        {"key_": "net.if.speed[1]", "name": "Interface Ethernet51/1(x): ..."},
    ]
    ZabbixRpcMocker().on("item.get", lambda p: items).activate(monkeypatch)
    keys = get_net_if_bandwidth_item_keys("https://z.example/api_jsonrpc.php", "t", "1", "Ethernet51/1")
    assert keys["in"] == "net.if.in[1]"
    assert keys["out"] == "net.if.out[1]"
    assert keys["speed"] == "net.if.speed[1]"
