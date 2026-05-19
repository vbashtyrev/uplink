"""get_juniper_uplink_stats inner loop via patched read_until_json_and_prompt."""

import json
from pathlib import Path

from uplinks_stats import get_juniper_uplink_stats

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_get_juniper_full_loop_patched(monkeypatch):
    desc_only_ae = {
        "interface-information": [{
            "logical-interface": [{
                "name": [{"data": "ae5.0"}],
                "description": [{"data": "Uplink: Hurricane"}],
                "oper-status": [{"data": "up"}],
            }],
        }],
    }
    chassis = json.loads((FIXTURES / "juniper_ssh_chassis.json").read_text(encoding="utf-8"))
    lacp = json.loads((FIXTURES / "juniper_ssh_lacp.json").read_text(encoding="utf-8"))
    ae5 = json.loads((FIXTURES / "juniper_ssh_ae5.json").read_text(encoding="utf-8"))
    et = json.loads((FIXTURES / "juniper_ssh_et.json").read_text(encoding="utf-8"))
    optics = json.loads((FIXTURES / "juniper_ssh_optics.json").read_text(encoding="utf-8"))

    json_queue = [desc_only_ae, chassis, lacp, ae5, et, optics]
    prompt_text = "set routing-instances internet interface ae5.0\nadmin@mx1> "

    def fake_read_json(channel, timeout=120):
        if json_queue:
            return json_queue.pop(0)
        return {}

    def fake_read_prompt(channel, timeout=120):
        return prompt_text

    from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient

    channel = FakeChannel(["admin@mx1> "])
    monkeypatch.setattr(
        "uplinks_stats.paramiko.SSHClient",
        lambda: FakeSSHClient(channel),
    )
    monkeypatch.setattr("uplinks_stats.read_until_json_and_prompt", fake_read_json)
    monkeypatch.setattr("uplinks_stats.read_until_prompt", fake_read_prompt)
    monkeypatch.setattr("uplinks_stats.read_until", lambda channel, patterns, max_wait=30: "admin@mx1> ")

    stats, err = get_juniper_uplink_stats("h", "u", "p")
    assert err is None
    names = {r["name"] for r in stats}
    assert "ae5" in names
    assert "ae5.0" in names
    assert "et-0/0/1" in names
