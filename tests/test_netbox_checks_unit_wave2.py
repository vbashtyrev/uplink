"""Unit tests for netbox_checks helpers (wave 2)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_load_file_errors(tmp_path):
    missing, err = nc.load_file(str(tmp_path / "nope.json"))
    assert missing is None and "not found" in err

    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    data, err = nc.load_file(str(bad))
    assert data is None and "JSON" in err

    nodev = tmp_path / "nodev.json"
    nodev.write_text("{}", encoding="utf-8")
    data, err = nc.load_file(str(nodev))
    assert data is None and "devices" in err


def test_interface_name_variants_and_check_intname():
    variants = nc.interface_name_variants("Ethernet51/1")
    assert "Ethernet51/1" in variants
    assert "ethernet51/1" in variants
    assert "Eth51/1" in variants

    nb = {"ethernet51/1": MagicMock()}
    status, name, code = nc.check_intname("dev", "Ethernet51/1", nb)
    assert status == "found" and code == nc.NOTE_LOWER

    nb2 = {"Eth51": MagicMock()}
    status, name, code = nc.check_intname("dev", "Ethernet51/1", nb2)
    assert status == "found" and code == nc.NOTE_NO_SLASH

    nb3 = {"Eth51/1": MagicMock()}
    status, name, code = nc.check_intname("dev", "Ethernet51/1", nb3)
    assert status == "found" and code == nc.NOTE_ALT

    status, name, code = nc.check_intname("dev", "missing0", {})
    assert status == "missing" and code == nc.NOTE_MISSING


def test_normalize_duplex_mac_fwd():
    assert nc._normalize_duplex("duplexFull") == "full"
    assert nc._fwd_file_to_netbox_mode("bridged") == "tagged"
    assert nc._fwd_file_to_netbox_mode("routed") is None
    assert nc._mac_netbox_format("44:4c:a8:bf:2e:91") == "44:4C:A8:BF:2E:91"


def test_load_mt_ref_and_mt_helpers(tmp_path):
    path = tmp_path / "types.json"
    path.write_text(
        json.dumps({"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+ (10GE)"}]}),
        encoding="utf-8",
    )
    values, lst, err = nc.load_mt_ref(str(path))
    assert err is None
    assert "10gbase-x-sfpp" in values
    assert nc._mt_in_ref("SFP+ (10GE)", values, lst)
    assert nc._mt_to_value("SFP+ (10GE)", values, lst) == "10gbase-x-sfpp"

    iface = MagicMock()
    iface.type = {"value": "virtual", "label": "Virtual"}
    assert nc._netbox_type_to_str(iface) == "virtual"


def test_apply_aggregate_relation_second_pass(capsys):
    member = MagicMock()
    member.id = 11
    lag = MagicMock()
    lag.id = 22
    member.update = MagicMock()
    nb_by_name = {"et-0/0/1": member, "ae5": lag}
    payload = [
        {
            "name": "et-0/0/1",
            "aggregateInterface": "ae5",
            "isLag": False,
        },
    ]

    def is_physical(entry, name):
        return not entry.get("isLag")

    nc._apply_aggregate_relation_second_pass(
        "mx1", payload, nb_by_name, "lag", is_physical, "LAG"
    )
    member.update.assert_called_with({"lag": 22})
    assert "LAG" in capsys.readouterr().out


def test_apply_ip_addresses(monkeypatch, capsys):
    nb_iface = MagicMock()
    nb_iface.id = 10
    ip_existing = MagicMock()
    ip_existing.assigned_object_id = None
    ip_existing.assigned_object_type = None
    ip_existing.save = MagicMock()

    nb = MagicMock()
    nb.ipam.ip_addresses.filter.return_value = [ip_existing]
    nb.ipam.ip_addresses.create = MagicMock()

    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
        nc._apply_ip_addresses_to_interface(
            nb, "dev", "Eth1", nb_iface, ["203.0.113.1/24"], vrf_id_f=None
        )
    assert nb.ipam.ip_addresses.create.called or ip_existing.save.called


def test_apply_mac_create_and_move(capsys):
    nb_iface = MagicMock()
    nb_iface.id = 10
    nb_iface.update = MagicMock()
    old_iface = MagicMock()
    old_iface.id = 5
    old_iface.primary_mac_address = 99
    old_iface.update = MagicMock()

    mac_rec = MagicMock()
    mac_rec.id = 99
    mac_rec.assigned_object_id = 5
    mac_rec.save = MagicMock()
    mac_rec.url = "http://nb/mac/99"

    nb = MagicMock()
    nb.dcim.mac_addresses.filter.return_value = [mac_rec]
    nb.dcim.interfaces.get.return_value = old_iface

    nc._apply_mac_to_interface(nb, "dev", "Eth1", nb_iface, "44:4c:a8:bf:2e:91")
    assert mac_rec.save.called
    assert nb_iface.update.called


def test_table_helpers():
    row = ("h",) + ("x",) * 40
    assert nc._row_has_diff(row) is True
    col_spec = nc._build_col_spec(
        type("A", (), {
            "intname": True,
            "description": False,
            "mediatype": False,
            "bandwidth": False,
            "duplex": False,
            "mac": False,
            "mtu": False,
            "tx_power": False,
            "forwarding_model": False,
            "ip_address": False,
            "lag": False,
            "parent": False,
            "show_change": False,
        })()
    )
    assert col_spec
    filtered = nc._filter_empty_note_cols(col_spec, [row])
    assert filtered
