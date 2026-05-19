"""Juniper XML fallback with no uplink after parse returns []."""

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient
from uplinks_stats import get_juniper_uplink_stats

EMPTY_XML = """<rpc-reply>
<interface-information>
<logical-interface>
<name>ae5.100</name>
<description>Uplink: VLAN only</description>
<oper-status>up</oper-status>
</logical-interface>
</interface-information>
</rpc-reply>
admin@mx1> """


def test_juniper_xml_no_uplinks_returns_empty(monkeypatch):
    desc_down = {
        "interface-information": [{
            "logical-interface": [{
                "name": [{"data": "ae5.0"}],
                "description": [{"data": "Uplink: X"}],
                "oper-status": [{"data": "down"}],
            }],
        }],
    }
    xml_done = [False]

    def fake_read_json(channel, timeout=120):
        return desc_down

    def fake_read_prompt(channel, timeout=120):
        if not xml_done[0]:
            xml_done[0] = True
            return EMPTY_XML
        return "admin@mx1> "

    channel = FakeChannel(["admin@mx1> "])
    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: FakeSSHClient(channel))
    monkeypatch.setattr("uplinks_stats.read_until_json_and_prompt", fake_read_json)
    monkeypatch.setattr("uplinks_stats.read_until_prompt", fake_read_prompt)
    monkeypatch.setattr("uplinks_stats.read_until", lambda ch, p, max_wait=30: "admin@mx1> ")

    stats, err = get_juniper_uplink_stats("mx", "admin", "pass", log=lambda m: None)
    assert err is None
    assert stats == []
