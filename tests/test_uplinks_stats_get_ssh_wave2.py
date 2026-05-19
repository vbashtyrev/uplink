"""get_ssh_uplinks Arista per-interface and Juniper bulk paths."""

import json
from pathlib import Path

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient, arista_script_from_fixtures
from uplinks_stats import get_ssh_uplinks

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_get_ssh_uplinks_arista_per_interface(monkeypatch):
    desc = json.loads((FIXTURES / "arista_descriptions.json").read_text(encoding="utf-8"))
    prompt = "admin@router# "
    per_iface = json.dumps(
        {"interfaceDescriptions": {"Ethernet51/1": desc["interfaceDescriptions"]["Ethernet51/1"]}}
    )
    script = [prompt, "Password: \n", prompt, per_iface + "\n" + prompt]
    monkeypatch.setattr(
        "uplinks_stats.paramiko.SSHClient",
        lambda: FakeSSHClient(FakeChannel(script)),
    )
    result, err = get_ssh_uplinks(
        "10.0.0.1",
        "admin",
        "pass",
        netbox_interface_names=["Ethernet51/1"],
        platform_name="Arista EOS",
    )
    assert err is None
    assert result and result[0][0] == "Ethernet51/1"


def test_get_ssh_uplinks_juniper_banner(monkeypatch):
    desc = json.loads((FIXTURES / "juniper_descriptions.json").read_text(encoding="utf-8"))
    prompt = "admin@mx1> "
    script = [
        "JUNOS banner\n",
        prompt,
        "Password: \n",
        prompt,
        json.dumps(desc) + "\n" + prompt,
    ]
    monkeypatch.setattr(
        "uplinks_stats.paramiko.SSHClient",
        lambda: FakeSSHClient(FakeChannel(script)),
    )
    result, err = get_ssh_uplinks(
        "10.0.0.1",
        "admin",
        "pass",
        platform_name=None,
    )
    assert err is None
    assert isinstance(result, list)
