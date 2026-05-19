"""netbox_checks apply path for description/bandwidth via mocked NetBox device loop."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_apply_description_updates_interface(tmp_path, monkeypatch, capsys):
    dry_path = FIXTURES / "dry_ssh_minimal.json"
    data, err = nc.load_file(str(dry_path))
    assert err is None

    nb_iface = MagicMock()
    nb_iface.id = 10
    nb_iface.description = "old desc"
    nb_iface.name = "Ethernet51/1"

    device = MagicMock()
    device.id = 1
    device.name = "ALA-KZT-7280TR-1"
    device.platform = None

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [device]
    nb.dcim.interfaces.filter.return_value = [nb_iface]

    monkeypatch.setenv("NETBOX_URL", "https://netbox.example")
    monkeypatch.setenv("NETBOX_TOKEN", "token")
    monkeypatch.setenv("NETBOX_TAG", "uplinks")

    with patch("netbox_checks.pynetbox.api", return_value=nb):
        with patch("netbox_checks.get_device_platform_name", return_value="arista"):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.is_juniper_platform", return_value=False):
                    with patch.object(
                        nc.sys,
                        "argv",
                        [
                            "netbox_checks.py",
                            "-f",
                            str(dry_path),
                            "--host",
                            "ALA-KZT-7280TR-1",
                            "--description",
                            "--apply",
                        ],
                    ):
                        nc.main()

    nb_iface.update.assert_called()
    call_args = nb_iface.update.call_args[0][0]
    assert "description" in call_args
    assert "Uplink:" in call_args["description"]
