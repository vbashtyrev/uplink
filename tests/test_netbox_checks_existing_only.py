"""netbox_checks --existing-only: safe apply without creates."""

import json
import sys
from unittest.mock import MagicMock, patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def _missing_iface_stats(tmp_path):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "ALA-KZT-7280TR-1": [
                        {
                            "name": "Ethernet52/1",
                            "description": "Uplink: Cogent",
                            "mediaType": "10gbase-x-sfpp",
                            "bandwidth": 10_000_000_000,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return stats


def test_existing_only_skips_interface_create(monkeypatch, netbox_env, tmp_path, capsys):
    stats = _missing_iface_stats(tmp_path)
    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()
    create_called = False

    def track_create(**kwargs):
        nonlocal create_called
        create_called = True
        raise AssertionError("interface create should not be called")

    env.dcim.interfaces.create = track_create

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "ALA-KZT-7280TR-1",
                                "--apply",
                                "--existing-only",
                                "--intname",
                                "--description",
                            ],
                        )
                        assert nc.main() == 0
    out = capsys.readouterr().out
    assert not create_called
    assert "skipped (--existing-only)" in out
    assert "Ethernet52/1" in out


def test_auto_allows_interface_create(monkeypatch, netbox_env, tmp_path, capsys):
    stats = _missing_iface_stats(tmp_path)
    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "ALA-KZT-7280TR-1",
                                "--apply",
                                "--auto",
                                "--intname",
                                "--description",
                                "--mediatype",
                            ],
                        )
                        assert nc.main() == 0
    assert env.dcim.interfaces.filter(device_id=dev.id, name="Ethernet52/1")
    assert "created" in capsys.readouterr().out.lower()


def test_existing_only_skips_mac_create(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "R1": [
                        {
                            "name": "Ethernet1/1",
                            "physicalAddress": "44:4C:A8:BF:2E:91",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()
    iface = env.add_interface(dev, "Ethernet1/1", iface_type="10gbase-x-sfpp")
    iface.mac_address = None
    iface.mac_addresses = []

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [dev]
    nb.dcim.interfaces.filter.return_value = [iface]
    nb.dcim.mac_addresses.filter.return_value = []
    nb.dcim.mac_addresses.create = MagicMock(side_effect=AssertionError("mac create should not be called"))

    with patch.object(nc.pynetbox, "api", lambda url, token: nb):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "R1",
                                "--apply",
                                "--existing-only",
                                "--mac",
                            ],
                        )
                        assert nc.main() == 0
    out = capsys.readouterr().out
    nb.dcim.mac_addresses.create.assert_not_called()
    assert "MAC" in out and "skipped (--existing-only)" in out


def test_existing_only_skips_ip_create(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "R1": [
                        {
                            "name": "Ethernet1/1",
                            "ipv4_addresses": ["203.0.113.2/24"],
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()
    env.add_interface(dev, "Ethernet1/1", iface_type="10gbase-x-sfpp")

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [dev]
    nb.dcim.interfaces.filter.return_value = list(env.dcim.interfaces._items)
    create_called = False

    def track_create(**kwargs):
        nonlocal create_called
        create_called = True
        raise AssertionError("ip create should not be called")

    nb.ipam.ip_addresses.create = track_create
    nb.ipam.ip_addresses.filter.return_value = []

    with patch.object(nc.pynetbox, "api", lambda url, token: nb):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_find_ip_in_netbox", return_value=[]):
                        with patch.object(nc, "_find_ip_in_netbox_any_vrf", return_value=[]):
                            with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                                monkeypatch.setattr(
                                    sys,
                                    "argv",
                                    [
                                        "netbox_checks.py",
                                        "-f",
                                        str(stats),
                                        "--host",
                                        "R1",
                                        "--apply",
                                        "--existing-only",
                                        "--ip-address",
                                    ],
                                )
                                assert nc.main() == 0
    out = capsys.readouterr().out
    assert not create_called
    assert "IP" in out and "skipped (--existing-only)" in out


def test_existing_only_still_updates_existing_interface(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "ALA-KZT-7280TR-1": [
                        {
                            "name": "Ethernet51/1",
                            "description": "Uplink: Cogent 10G",
                            "bandwidth": 10_000_000_000,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()
    iface = env.add_interface(dev, "Ethernet51/1", speed=1000, iface_type="10gbase-x-sfpp")
    iface.description = "old"

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "ALA-KZT-7280TR-1",
                                "--apply",
                                "--existing-only",
                                "--description",
                                "--bandwidth",
                            ],
                        )
                        assert nc.main() == 0
    assert iface.description == "Uplink: Cogent 10G"
    assert "Updated" in capsys.readouterr().out


def test_existing_only_skips_ip_unbind_via_main(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "R1": [
                        {
                            "name": "Ethernet1/1",
                            "ipv4_addresses": [],
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()
    env.add_interface(dev, "Ethernet1/1", iface_type="10gbase-x-sfpp")

    existing_ip = MagicMock()
    existing_ip.assigned_object_id = 100
    existing_ip.assigned_object_type = "dcim.interface"
    existing_ip.save = MagicMock()

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [dev]
    nb.dcim.interfaces.filter.return_value = list(env.dcim.interfaces._items)

    with patch.object(nc.pynetbox, "api", lambda url, token: nb):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[("203.0.113.9/24", None)]):
                        with patch.object(nc, "_find_ip_in_netbox", return_value=[existing_ip]):
                            monkeypatch.setattr(
                                sys,
                                "argv",
                                [
                                    "netbox_checks.py",
                                    "-f",
                                    str(stats),
                                    "--host",
                                    "R1",
                                    "--apply",
                                    "--existing-only",
                                    "--ip-address",
                                ],
                            )
                            assert nc.main() == 0
    existing_ip.save.assert_not_called()
    assert "extra on interface, skipped (--existing-only)" in capsys.readouterr().out


def test_existing_only_skips_mac_rebind_via_main(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "R1": [
                        {
                            "name": "Ethernet1/1",
                            "physicalAddress": "44:4C:A8:BF:2E:91",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()
    iface = env.add_interface(dev, "Ethernet1/1", iface_type="10gbase-x-sfpp")
    iface.id = 20
    iface.mac_address = None
    iface.mac_addresses = []

    mac_rec = MagicMock(id=99, assigned_object_id=5)
    mac_rec.save = MagicMock()
    old_iface = MagicMock(id=5, primary_mac_address=99)
    old_iface.update = MagicMock()

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [dev]
    nb.dcim.interfaces.filter.return_value = [iface]
    nb.dcim.mac_addresses.filter.return_value = [mac_rec]
    nb.dcim.interfaces.get.side_effect = lambda pk: old_iface if pk == 5 else iface
    iface.update = MagicMock()

    with patch.object(nc.pynetbox, "api", lambda url, token: nb):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "R1",
                                "--apply",
                                "--existing-only",
                                "--mac",
                            ],
                        )
                        assert nc.main() == 0
    old_iface.update.assert_not_called()
    mac_rec.save.assert_not_called()
    iface.update.assert_not_called()
    assert "bound to another interface, skipped (--existing-only)" in capsys.readouterr().out


def test_existing_only_skips_lag_parent_updates(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "FRN-MX-1": [
                        {
                            "name": "ae5",
                            "description": "Uplink: Hurricane LAG",
                            "isLag": True,
                        },
                        {
                            "name": "et-0/0/1",
                            "description": "Uplink: Hurricane member",
                            "aggregateInterface": "ae5",
                        },
                        {
                            "name": "ae5.0",
                            "description": "Uplink: Hurricane",
                            "isLogical": True,
                            "aggregateInterface": "ae5",
                            "mtu": 9192,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("FRN-MX-1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Juniper JunOS"})()
    ae5 = env.add_interface(dev, "ae5", iface_type="lag")
    ae5.description = "old lag desc"
    member = env.add_interface(dev, "et-0/0/1", iface_type="1000base-x")
    member.lag = None
    unit = env.add_interface(dev, "ae5.0", iface_type="virtual")
    unit.description = "old unit"
    unit.parent = None
    unit.mtu = 1500
    member_updates = []
    unit_updates = []

    def track_member_update(data):
        member_updates.append(data)
        for key, val in (data or {}).items():
            setattr(member, key, val)

    def track_unit_update(data):
        unit_updates.append(data)
        for key, val in (data or {}).items():
            setattr(unit, key, val)

    member.update = track_member_update
    unit.update = track_unit_update

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=True):
            with patch("netbox_checks.is_arista_platform", return_value=False):
                with patch("netbox_checks.get_device_platform_name", return_value="Juniper JunOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "FRN-MX-1",
                                "--apply",
                                "--existing-only",
                                "--intname",
                                "--description",
                                "--mtu",
                                "--lag",
                                "--parent",
                            ],
                        )
                        assert nc.main() == 0
    out = capsys.readouterr().out
    assert member.lag is None
    assert unit.parent is None
    assert not any("lag" in u for u in member_updates)
    assert not any("parent" in u for u in unit_updates)
    assert unit.description == "Uplink: Hurricane"
    assert unit.mtu == 9192
    assert "LAG → ae5 skipped (--existing-only)" in out
    assert "parent → ae5 skipped (--existing-only)" in out
