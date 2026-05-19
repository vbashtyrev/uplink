"""get_arista_uplink_stats: bridged interface + switchport configuration."""

import json
from pathlib import Path

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient
from uplinks_stats import get_arista_uplink_stats

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _bridged_script():
    prompt = "admin@router# "
    desc = (FIXTURES / "arista_ssh_descriptions.json").read_text(encoding="utf-8")
    vrf = (FIXTURES / "arista_ssh_vrf.json").read_text(encoding="utf-8")
    iface_raw = json.loads((FIXTURES / "arista_ssh_interface.json").read_text(encoding="utf-8"))
    iface_raw["interfaces"]["Ethernet51/1"]["forwardingModel"] = "bridged"
    iface = json.dumps(iface_raw)
    trans = (FIXTURES / "arista_ssh_transceiver.json").read_text(encoding="utf-8")
    sw = json.dumps(
        {
            "interfaces": {
                "Ethernet51/1": {"source": "dynamic", "vlan": 100},
            },
        }
    )
    return [
        prompt,
        "Password: \n",
        prompt,
        desc + "\n" + prompt,
        vrf + "\n" + prompt,
        iface + "\n" + prompt,
        trans + "\n" + prompt,
        sw + "\n" + prompt,
    ]


def test_get_arista_bridged_switchport(monkeypatch):
    monkeypatch.setattr(
        "uplinks_stats.paramiko.SSHClient",
        lambda: FakeSSHClient(FakeChannel(_bridged_script())),
    )
    stats, err = get_arista_uplink_stats("10.0.0.1", "admin", "pass")
    assert err is None
    assert len(stats) == 1
    assert stats[0].get("switchportConfiguration") is not None


def test_get_arista_no_uplinks_returns_empty(monkeypatch):
    prompt = "admin@router# "
    empty_desc = json.dumps({"interfaceDescriptions": {}})
    script = [prompt, "Password: \n", prompt, empty_desc + "\n" + prompt]
    monkeypatch.setattr(
        "uplinks_stats.paramiko.SSHClient",
        lambda: FakeSSHClient(FakeChannel(script)),
    )
    stats, err = get_arista_uplink_stats("10.0.0.1", "admin", "pass")
    assert err is None
    assert stats == []
