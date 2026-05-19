"""netbox_checks main --json --hide-ok-hosts."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_json_hide_ok_hosts(monkeypatch, netbox_env, capsys):
    stats = FIXTURES / "dry_ssh_minimal.json"
    env = NetBoxTestEnvironment()
    for name in json.loads(stats.read_text(encoding="utf-8"))["devices"]:
        dev = env.add_device(name)
        dev.tag = "border"
        dev.platform = type("P", (), {"name": "Arista EOS"})()
        for entry in json.loads(stats.read_text(encoding="utf-8"))["devices"][name]:
            env.add_interface(dev, entry["name"])

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    monkeypatch.setattr(
                        sys,
                        "argv",
                        [
                            "netbox_checks.py",
                            "-f",
                            str(stats),
                            "--json",
                            "--hide-ok-hosts",
                            "--all",
                        ],
                    )
                    assert nc.main() == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.find("{") :])
    assert "rows" in payload
    assert payload["stats"]["hosts_not_ok"] >= 1
