"""netbox_checks --scope-to-file: limit NetBox device scope to input JSON hostnames."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.netbox_full import NetBoxTestEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _stats_file(tmp_path, devices=None):
    stats = {
        "devices": devices
        or {
            "ALA-KZT-7280TR-1": [
                {"name": "Ethernet51/1", "description": "Uplink: Cogent 10G"},
            ],
        },
    }
    p = tmp_path / "stats.json"
    p.write_text(json.dumps(stats), encoding="utf-8")
    return p


def test_scope_to_file_suppresses_netbox_only_device(monkeypatch, netbox_env, tmp_path, capsys):
    """Scoped mode ignores border devices outside dry-ssh.json; unscoped still reports them."""
    import netbox_checks as mod

    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.tag = "border"
    env.add_interface(dev, "Ethernet51/1")
    extra = env.add_device("TERMINAL-PROXY-1")
    extra.tag = "border"

    stats_path = _stats_file(tmp_path)

    with patch.object(mod.pynetbox, "api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            ["netbox_checks.py", "-f", str(stats_path), "--intname"],
        )
        assert mod.main() == 0
        unscoped_out = capsys.readouterr().out
        assert "Netbox only (not in file): TERMINAL-PROXY-1" in unscoped_out

        monkeypatch.setattr(
            sys,
            "argv",
            ["netbox_checks.py", "-f", str(stats_path), "--intname", "--scope-to-file"],
        )
        assert mod.main() == 0
        scoped_out = capsys.readouterr().out
        assert "Netbox only (not in file)" not in scoped_out
