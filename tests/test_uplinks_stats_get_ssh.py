"""get_ssh_uplinks report-mode SSH (Arista/Juniper banners)."""

import json
from pathlib import Path
from unittest.mock import patch

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient, arista_script_from_fixtures
from uplinks_stats import get_ssh_uplinks

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_get_ssh_uplinks_arista(monkeypatch):
    script = arista_script_from_fixtures(FIXTURES)
    channel = FakeChannel(script)
    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: FakeSSHClient(channel))
    result, err = get_ssh_uplinks(
        "10.0.0.1",
        "admin",
        "pass",
        platform_name="Arista EOS",
        log=None,
    )
    assert err is None
    assert result is not None


def test_get_ssh_uplinks_juniper(monkeypatch):
    desc = json.loads((FIXTURES / "juniper_descriptions.json").read_text(encoding="utf-8"))
    prompt = "admin@mx1> "
    script = [prompt, "Password: \n", prompt, json.dumps(desc) + "\n" + prompt]
    channel = FakeChannel(script)
    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: FakeSSHClient(channel))
    result, err = get_ssh_uplinks(
        "10.0.0.1",
        "admin",
        "pass",
        platform_name="Juniper JunOS",
    )
    assert err is None or result is not None
