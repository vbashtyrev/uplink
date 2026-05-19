"""netbox_checks --apply for bandwidth and mediatype."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _setup_nb_mock(speed=1000, iface_type="other"):
    nb_iface = MagicMock()
    nb_iface.id = 10
    nb_iface.name = "Ethernet51/1"
    nb_iface.description = "old"
    nb_iface.speed = speed
    nb_iface.type = {"value": iface_type, "label": iface_type}
    nb_iface.mac_address = None
    nb_iface.mac_addresses = []
    nb_iface.primary_mac_address = None

    device = MagicMock()
    device.id = 1
    device.name = "ALA-KZT-7280TR-1"
    device.platform = None

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [device]
    nb.dcim.interfaces.filter.return_value = [nb_iface]
    return nb, nb_iface


def test_apply_bandwidth_updates_speed(tmp_path, monkeypatch):
    dry_path = FIXTURES / "dry_ssh_minimal.json"
    mt_ref = tmp_path / "types.json"
    mt_ref.write_text(
        '{"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+ (10GE)"}]}',
        encoding="utf-8",
    )

    nb, nb_iface = _setup_nb_mock(speed=1000)

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
                            "--bandwidth",
                            "--apply",
                        ],
                    ):
                        assert nc.main() == 0

    nb_iface.update.assert_called()
    updates = nb_iface.update.call_args[0][0]
    assert updates.get("speed") == 10_000_000  # 10G bps -> 10000000 Kbps


def test_apply_mediatype_with_ref(tmp_path, monkeypatch):
    dry_path = FIXTURES / "dry_ssh_minimal.json"
    mt_ref = tmp_path / "netbox_interface_types.json"
    mt_ref.write_text(
        '{"interface_types": [{"value": "10gbase-x-sfpp", "label": "10GBASE-SR"}]}',
        encoding="utf-8",
    )

    nb, nb_iface = _setup_nb_mock(iface_type="virtual")

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
                            "--mediatype",
                            "--mt-ref",
                            str(mt_ref),
                            "--apply",
                        ],
                    ):
                        assert nc.main() == 0

    nb_iface.update.assert_called()
    updates = nb_iface.update.call_args[0][0]
    assert "type" in updates
