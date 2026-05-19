"""fetch_zabbix_hosts_and_items with mocked Zabbix API."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from zabbix_map import BITS_RECEIVED_NAME, BITS_SENT_NAME, fetch_zabbix_hosts_and_items


def test_fetch_zabbix_hosts_and_items(monkeypatch):
    host = {"hostid": "101", "host": "ALA-R1", "name": "ALA-R1"}

    def item_get(params):
        hostid = str(params.get("hostids", [""])[0])
        search = (params.get("search") or {}).get("name", "")
        if search == BITS_RECEIVED_NAME:
            return [
                {
                    "itemid": "1001",
                    "hostid": hostid,
                    "name": "Interface Ethernet51/1(ethernet51/1): Bits received",
                    "key_": "net.if.in[ifHCInOctets.51/1]",
                },
            ]
        if search == BITS_SENT_NAME:
            return [
                {
                    "itemid": "1002",
                    "hostid": hostid,
                    "name": "Interface Ethernet51/1(ethernet51/1): Bits sent",
                    "key_": "net.if.out[ifHCOutOctets.51/1]",
                },
            ]
        return []

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("host.get", lambda p: [host] if p.get("filter", {}).get("host") else [])
        .on("item.get", item_get)
    )
    mocker.activate(monkeypatch)

    host_ids, items, err = fetch_zabbix_hosts_and_items(
        "https://zabbix.example/api_jsonrpc.php",
        "token",
        {"ALA-R1"},
        debug=False,
    )
    assert err is None
    assert host_ids == {"ALA-R1": "101"}
    rec = items[("ALA-R1", "ethernet51/1")]
    assert "ifHCInOctets" in rec["bits_in"]
    assert "ifHCOutOctets" in rec["bits_out"]
    assert rec["itemid_in"] == "1001"
    assert rec["itemid_out"] == "1002"
    item_searches = [p["search"]["name"] for m, p in mocker.calls if m == "item.get"]
    assert BITS_RECEIVED_NAME in item_searches
    assert BITS_SENT_NAME in item_searches


def test_fetch_zabbix_hosts_missing(monkeypatch):
    (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [])
        .on("host.get", lambda p: [])
        .activate(monkeypatch)
    )
    host_ids, items, err = fetch_zabbix_hosts_and_items(
        "https://zabbix.example/api_jsonrpc.php",
        "t",
        {"missing-host"},
    )
    assert host_ids is None
    assert items is None
    assert "not found" in err.lower()
