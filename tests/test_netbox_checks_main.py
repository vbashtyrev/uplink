"""netbox_checks.main() integration with mocked NetBox."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.netbox_full import NetBoxTestEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _stats_file(tmp_path):
    stats = {
        "devices": {
            "ALA-KZT-7280TR-1": [
                {
                    "name": "Ethernet51/1",
                    "description": "Uplink: Cogent 10G",
                    "mediaType": "10GBASE-SR",
                    "bandwidth": 10000000000,
                    "duplex": "duplexFull",
                    "mtu": 9214,
                    "physicalAddress": "44:4c:a8:bf:2e:91",
                    "forwardingModel": "routed",
                },
            ],
        },
    }
    p = tmp_path / "stats.json"
    p.write_text(json.dumps(stats), encoding="utf-8")
    return p


def test_main_table_all_checks(monkeypatch, netbox_env, tmp_path, capsys):
    import netbox_checks as mod

    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    env.add_interface(dev, "Ethernet51/1", speed=10000000, iface_type="10gbase-x-sfpp")
    mt_ref = tmp_path / "netbox_interface_types.json"
    mt_ref.write_text(
        json.dumps({"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+"}]}),
        encoding="utf-8",
    )
    dev.tag = "border"
    with patch.object(mod.pynetbox, "api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_checks.py",
                "-f",
                str(_stats_file(tmp_path)),
                "--all",
                "--mt-ref",
                str(mt_ref),
            ],
        )
        assert mod.main() == 0
    out = capsys.readouterr().out
    assert "ALA-KZT-7280TR-1" in out or "Ethernet51/1" in out


def test_main_json_output(monkeypatch, netbox_env, tmp_path, capsys):
    import netbox_checks as mod

    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.tag = "border"
    env.add_interface(dev, "Ethernet51/1")
    dev.tag = "border"
    with patch.object(mod.pynetbox, "api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_checks.py",
                "-f",
                str(_stats_file(tmp_path)),
                "--description",
                "--json",
            ],
        )
        assert mod.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, (dict, list))


def test_main_hide_ok_hosts(monkeypatch, netbox_env, tmp_path, capsys):
    import netbox_checks as mod

    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.tag = "border"
    iface = env.add_interface(
        dev,
        "Ethernet51/1",
        speed=10000000,
        iface_type="10gbase-x-sfpp",
    )
    iface.description = "Uplink: Cogent 10G"
    dev.tag = "border"
    with patch.object(mod.pynetbox, "api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_checks.py",
                "-f",
                str(_stats_file(tmp_path)),
                "--hide-ok-hosts",
            ],
        )
        mod.main()
