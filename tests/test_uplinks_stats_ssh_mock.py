"""SSH path tests for uplinks_stats.get_arista_uplink_stats (mocked Paramiko)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.ssh_channel import FakeChannel, FakeSSHClient, arista_script_from_fixtures
from uplinks_stats import (
    get_arista_uplink_stats,
    read_until,
    read_until_json_and_prompt,
    read_until_prompt,
    _looks_like_cli_prompt,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_looks_like_cli_prompt():
    assert _looks_like_cli_prompt("admin@router# ") is True
    assert _looks_like_cli_prompt('{"x": 1}>') is False


def test_read_until_json_and_prompt():
    desc = (FIXTURES / "arista_ssh_descriptions.json").read_text(encoding="utf-8")
    ch = FakeChannel([desc + "\nadmin@router# "])
    data = read_until_json_and_prompt(ch, timeout=5)
    assert "interfaceDescriptions" in data


def test_read_until_prompt():
    ch = FakeChannel(["line\nadmin@mx1# "])
    text = read_until_prompt(ch, timeout=5)
    assert "admin@mx1#" in text


def test_get_arista_uplink_stats_full(monkeypatch):
    script = arista_script_from_fixtures(FIXTURES)
    channel = FakeChannel(script)
    client = FakeSSHClient(channel)

    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: client)
    stats, err = get_arista_uplink_stats("10.0.0.1", "admin", "pass", log=lambda m: None)
    assert err is None
    assert len(stats) == 1
    assert stats[0]["name"] == "Ethernet51/1"
    assert stats[0]["mediaType"] == "10GBASE-SR"
    assert stats[0].get("ip_vrf") == "internet"


def test_get_arista_uplink_stats_connect_error(monkeypatch):
    class BadClient:
        def set_missing_host_key_policy(self, _p):
            pass

        def connect(self, hostname=None, **_kw):
            raise OSError("refused")

        def close(self):
            pass

    monkeypatch.setattr("uplinks_stats.paramiko.SSHClient", lambda: BadClient())
    stats, err = get_arista_uplink_stats("10.0.0.1", "u", "p")
    assert stats is None
    assert "refused" in err.lower() or "OSError" in err
