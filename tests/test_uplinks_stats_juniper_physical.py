"""get_juniper_uplink_stats with physical uplink interface."""

import json
from pathlib import Path

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient
from uplinks_stats import get_juniper_uplink_stats

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_get_juniper_physical_interface(monkeypatch):
    desc_phys = {
        "interface-information": [{
            "physical-interface": [{
                "name": [{"data": "et-0/0/1"}],
                "description": [{"data": "Uplink: Hurricane member"}],
                "oper-status": [{"data": "up"}],
            }],
        }],
    }
    chassis = json.loads((FIXTURES / "juniper_ssh_chassis.json").read_text(encoding="utf-8"))
    et = json.loads((FIXTURES / "juniper_ssh_et.json").read_text(encoding="utf-8"))
    optics = json.loads((FIXTURES / "juniper_ssh_optics.json").read_text(encoding="utf-8"))
    json_queue = [desc_phys, chassis, et, optics]
    prompt_text = "set routing-instances internet interface et-0/0/1\nadmin@mx1> "

    def fake_read_json(channel, timeout=120):
        if json_queue:
            return json_queue.pop(0)
        return {}

    channel = FakeChannel(["admin@mx1> "])
    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: FakeSSHClient(channel))
    monkeypatch.setattr("uplinks_stats.read_until_json_and_prompt", fake_read_json)
    monkeypatch.setattr("uplinks_stats.read_until_prompt", lambda ch, timeout=120: prompt_text)
    monkeypatch.setattr("uplinks_stats.read_until", lambda ch, p, max_wait=30: "admin@mx1> ")

    stats, err = get_juniper_uplink_stats("mx", "admin", "pass", log=lambda m: None)
    assert err is None
    names = {r["name"] for r in stats}
    assert "et-0/0/1" in names
