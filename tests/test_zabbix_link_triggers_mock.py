"""get_link_commit_triggers with mocked Zabbix API."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker
from uplinks_config import TRIGGER_DESC_90_SUFFIX, TRIGGER_DESC_100_SUFFIX
from zabbix_map import get_link_commit_triggers


def test_get_link_commit_triggers(monkeypatch):
    iface = "Ethernet51/1"
    triggers = [
        {
            "triggerid": "9001",
            "description": "Interface {}: {}".format(iface, TRIGGER_DESC_90_SUFFIX),
            "hosts": [{"hostid": "42"}],
        },
        {
            "triggerid": "9002",
            "description": "Interface {}: {}".format(iface, TRIGGER_DESC_100_SUFFIX),
            "hosts": [{"hostid": "42"}],
        },
    ]

    ZabbixRpcMocker().on("trigger.get", lambda p: triggers).activate(monkeypatch)

    out = get_link_commit_triggers("https://zabbix.example/api_jsonrpc.php", "t", ["42"])
    key = ("42", "ethernet51/1")
    assert out[key] == ("9001", "9002")
