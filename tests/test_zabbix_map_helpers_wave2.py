"""zabbix_map helper functions wave 2."""

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from uplinks_config import TRIGGER_DESC_100_SUFFIX, TRIGGER_DESC_90_SUFFIX, UPLINKS_AGGREGATE_HOST_PREFIX
from zabbix_map import (
    _get_map_user_groups,
    _interface_from_item_name,
    _interface_from_key,
    _normalize_provider_name,
    fetch_zabbix_hosts_and_items,
    get_link_commit_triggers,
    get_provider_aggregate_triggers,
)


def test_interface_parsers():
    assert _interface_from_key('net.if.in["Ethernet51/1"]') == "Ethernet51/1"
    assert _interface_from_item_name("Interface Eth1(Uplink): Bits received") == "Eth1"
    assert _normalize_provider_name("ER-Telecom") == "ertelecom"


def test_get_map_user_groups(monkeypatch):
    mocker = build_standard_zabbix_mocker().on(
        "user.get",
        lambda p: [{"userid": "1", "usrgrps": [{"usrgrpid": "7"}]}],
    )
    mocker.activate(monkeypatch)
    groups, err = _get_map_user_groups("https://z.example/api_jsonrpc.php", "t")
    assert err is None
    assert groups[0]["usrgrpid"] == 7


def test_get_map_user_groups_no_groups(monkeypatch):
    from tests.mocks.zabbix_rpc import ZabbixRpcMocker

    ZabbixRpcMocker().on("user.get", lambda p: [{"userid": "1", "usrgrps": []}]).activate(monkeypatch)
    groups, err = _get_map_user_groups("https://z.example/api_jsonrpc.php", "t")
    assert groups is None and "no groups" in err


def test_get_provider_aggregate_triggers(monkeypatch):
    prov = "Cogent"
    host_name = UPLINKS_AGGREGATE_HOST_PREFIX + prov

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            return [{"hostid": "200", "host": host_name, "name": host_name}]
        return []

    def trigger_get(params):
        return [
            {
                "triggerid": "90",
                "description": "Aggregate",
                "priority": 1,
                "hosts": [{"hostid": "200"}],
            },
            {
                "triggerid": "100",
                "description": "Aggregate high",
                "priority": 2,
                "hosts": [{"hostid": "200"}],
            },
        ]

    mocker = build_standard_zabbix_mocker().on("host.get", host_get).on("trigger.get", trigger_get)
    mocker.activate(monkeypatch)
    out = get_provider_aggregate_triggers("https://z.example/api_jsonrpc.php", "t", [prov])
    assert out[prov] == ("90", "100")


def test_get_link_commit_triggers(monkeypatch):
    desc90 = "Interface Eth1: {}".format(TRIGGER_DESC_90_SUFFIX)
    desc100 = "Interface Eth1: {}".format(TRIGGER_DESC_100_SUFFIX)
    mocker = build_standard_zabbix_mocker().on(
        "trigger.get",
        lambda p: [
            {"triggerid": "1", "description": desc90, "hosts": [{"hostid": "101"}]},
            {"triggerid": "2", "description": desc100, "hosts": [{"hostid": "101"}]},
        ],
    )
    mocker.activate(monkeypatch)
    out = get_link_commit_triggers("https://z.example/api_jsonrpc.php", "t", ["101"])
    assert out[("101", "eth1")] == ("1", "2")


def test_fetch_zabbix_hosts_and_items_by_name(monkeypatch):
    items = [
        {
            "itemid": "501",
            "hostid": "101",
            "name": "Interface Ethernet51/1: Bits received",
            "key_": 'net.if.in["Ethernet51/1"]',
        },
        {
            "itemid": "502",
            "hostid": "101",
            "name": "Interface Ethernet51/1: Bits sent",
            "key_": 'net.if.out["Ethernet51/1"]',
        },
    ]

    def host_get(params):
        filt = params.get("filter") or {}
        if "name" in filt:
            return [{"hostid": "101", "host": "tech", "name": "ALA-R1"}]
        return []

    mocker = (
        build_standard_zabbix_mocker(hosts=[], items=items)
        .on("host.get", host_get)
        .on("user.get", lambda p: [{"userid": "1"}])
    )
    monkeypatch.setattr("zabbix_map.validate_zabbix_token", lambda *a, **k: (True, None))
    mocker.activate(monkeypatch)
    hosts, items_map, err = fetch_zabbix_hosts_and_items(
        "https://z.example/api_jsonrpc.php", "t", {"ALA-R1"}, debug=True
    )
    assert err is None
    assert hosts["ALA-R1"] == "101"
    assert items_map[("ALA-R1", "ethernet51/1")]["itemid_in"] == "501"
