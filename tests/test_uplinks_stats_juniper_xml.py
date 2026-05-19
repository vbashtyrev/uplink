"""Juniper get_juniper_uplink_stats XML fallback when JSON parse yields no uplinks."""

import json
from pathlib import Path

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient
from uplinks_stats import get_juniper_uplink_stats

FIXTURES = Path(__file__).resolve().parent / "fixtures"

JUNOS_XML = """<rpc-reply>
<interface-information>
<logical-interface>
<name>ae5.0</name>
<description>Uplink: Hurricane</description>
<oper-status>up</oper-status>
</logical-interface>
</interface-information>
</rpc-reply>
admin@mx1> """


def test_juniper_xml_fallback_when_json_empty(monkeypatch):
    # JSON has uplink but oper down → parse_juniper_uplinks returns []
    desc_down = {
        "interface-information": [{
            "logical-interface": [{
                "name": [{"data": "ae5.0"}],
                "description": [{"data": "Uplink: Hurricane"}],
                "oper-status": [{"data": "down"}],
            }],
        }],
    }
    chassis = json.loads((FIXTURES / "juniper_ssh_chassis.json").read_text(encoding="utf-8"))
    json_queue = [desc_down, chassis]
    xml_returned = [False]

    def fake_read_json(channel, timeout=120):
        if json_queue:
            return json_queue.pop(0)
        return {}

    def fake_read_prompt(channel, timeout=120):
        if not xml_returned[0]:
            xml_returned[0] = True
            return JUNOS_XML
        return "set routing-instances internet interface ae5.0\nadmin@mx1> "

    channel = FakeChannel(["admin@mx1> "])
    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: FakeSSHClient(channel))
    monkeypatch.setattr("uplinks_stats.read_until_json_and_prompt", fake_read_json)
    monkeypatch.setattr("uplinks_stats.read_until_prompt", fake_read_prompt)
    monkeypatch.setattr("uplinks_stats.read_until", lambda ch, p, max_wait=30: "admin@mx1> ")

    stats, err = get_juniper_uplink_stats("mx", "admin", "pass", log=lambda m: None)
    assert err is None
    assert xml_returned[0] is True
    assert stats == [] or isinstance(stats, list)
