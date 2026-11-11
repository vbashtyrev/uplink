"""Tests for DCI dashboard command (mocks only, no live NetBox)."""

import sys
from unittest.mock import MagicMock

from tests.mocks.netbox_api import MockNetBox, _Record
from uplinks.netbox import dci as dci_mod
import zabbix_dci_dashboard as zdd


def _dci_type():
    return _Record(name="DCI", slug="dci")


def _make_dci_circuit(
    *,
    cid="DCI-1",
    provider_id=1,
    provider=None,
    status="active",
    circuit_id=100,
    type_obj=None,
    type_id=None,
):
    type_obj = type_obj if type_obj is not None else _dci_type()
    return _Record(
        id=circuit_id,
        cid=cid,
        provider_id=provider_id,
        provider=provider if provider is not None else provider_id,
        status=status,
        type=type_obj,
        type_id=type_id,
    )


def _wire_dci_nb(circuits, terminations, devices=None, interfaces=None, cables=None):
    devices = devices or []
    interfaces = interfaces or []
    cables = cables or []
    nb = MockNetBox(
        devices=devices,
        interfaces=interfaces,
        cables=cables,
        terminations=terminations,
        circuits=circuits,
    )
    nb.circuits.circuits._items = list(circuits)
    provider = _Record(id=1, name="Inetcom")
    nb.circuits.providers = type("P", (), {
        "get": lambda self, pk: provider if pk == provider.id else None,
    })()
    return nb


def _site_with_region(site_name, region_name=None):
    region = _Record(name=region_name) if region_name else None
    return _Record(name=site_name, region=region)


def _full_chain_side(
    ct_id,
    iface,
    cable_id=50,
    side="A",
    circuit_id=100,
    site=None,
):
    cable = _Record(
        id=cable_id,
        a_terminations=[
            {"object_type": "circuits.circuittermination", "object_id": ct_id}
        ],
        b_terminations=[{"object_type": "dcim.interface", "object_id": iface.id}],
    )
    ct = _Record(
        id=ct_id,
        term_side=side,
        cable=_Record(id=cable_id),
        circuit_id=circuit_id,
        termination=site,
    )
    return ct, cable


def test_ignore_non_dci_circuit():
    uplink = _make_dci_circuit(type_obj=_Record(name="Uplink", slug="uplink"), cid="U1")
    nb = _wire_dci_nb([uplink], [])
    report = dci_mod.collect_dci_channels(nb)
    assert report["channels"] == []


def test_full_pair_both_sides():
    dev_a = _Record(id=1, name="router-a")
    dev_z = _Record(id=2, name="router-z")
    iface_a = _Record(id=10, name="Eth1", device=dev_a, device_id=1, enabled=True, tags=[_Record(slug="dci")])
    iface_z = _Record(id=11, name="Eth2", device=dev_z, device_id=2, enabled=True, tags=[_Record(slug="dci")])
    circuit = _make_dci_circuit(cid="DCI: MSK-1")
    ct_a, cab_a = _full_chain_side(1, iface_a, cable_id=50, side="A")
    ct_z, cab_z = _full_chain_side(2, iface_z, cable_id=51, side="Z")
    nb = _wire_dci_nb(
        [circuit],
        [ct_a, ct_z],
        devices=[dev_a, dev_z],
        interfaces=[iface_a, iface_z],
        cables=[cab_a, cab_z],
    )
    report = dci_mod.collect_dci_channels(nb)
    assert len(report["channels"]) == 1
    ch = report["channels"][0]
    assert ch["complete"] is True
    assert ch["a_device"] == "router-a"
    assert ch["z_device"] == "router-z"


def test_incomplete_inactive_reads_cable():
    dev = _Record(id=1, name="r1")
    iface = _Record(id=10, name="Eth1", device=dev, enabled=False, tags=[_Record(slug="dci")])
    circuit = _make_dci_circuit(status="planned", cid="X")
    ct_a, cab = _full_chain_side(1, iface, side="A")
    ct_z = _Record(id=2, term_side="Z", cable=None, circuit_id=100)
    nb = _wire_dci_nb([circuit], [ct_a, ct_z], devices=[dev], interfaces=[iface], cables=[cab])
    nb.dcim.cables.get = MagicMock(wraps=nb.dcim.cables.get)
    report = dci_mod.collect_dci_channels(nb)
    ch = report["channels"][0]
    assert ch["complete"] is False
    assert "статус не active" in ch["reason_text"]
    nb.dcim.cables.get.assert_called()


def test_term_side_case_and_termination_side_fallback():
    circuit = _make_dci_circuit(cid="T1")
    ct_a = _Record(id=1, term_side="a", cable=None, circuit_id=100)
    ct_z = _Record(id=2, termination_side="Z", cable=None, circuit_id=100)
    nb = _wire_dci_nb([circuit], [ct_a, ct_z])
    ch = dci_mod.collect_dci_channels(nb)["channels"][0]
    assert "A: нет ровно одного окончания" in ch["reason_text"]
    assert "Z: нет номера кабеля" in ch["reason_text"]


def test_read_error_on_circuits_all():
    nb = MagicMock()
    nb.circuits.circuits.all.side_effect = RuntimeError("pagination failed")
    report = dci_mod.collect_dci_channels(nb)
    assert report["fatal"] is True
    assert report["stats"]["read_errors"] == 1


def test_duplicate_cid_provider():
    c1 = _make_dci_circuit(cid="SAME", circuit_id=1)
    c2 = _make_dci_circuit(cid="SAME", circuit_id=2)
    nb = _wire_dci_nb([c1, c2], [])
    channels = dci_mod.collect_dci_channels(nb)["channels"]
    assert dci_mod.find_duplicate_cid_provider(channels) == ("SAME", "Inetcom")


def test_channel_row_y():
    assert zdd._channel_row_y(0) == 0
    assert zdd._channel_row_y(1) == 5
    assert zdd._channel_row_y(2) == 10


def test_collect_region_from_termination_site():
    dev_a = _Record(id=1, name="router-a")
    dev_z = _Record(id=2, name="router-z")
    iface_a = _Record(id=10, name="Eth1", device=dev_a, device_id=1, enabled=True, tags=[_Record(slug="dci")])
    iface_z = _Record(id=11, name="Eth2", device=dev_z, device_id=2, enabled=True, tags=[_Record(slug="dci")])
    site_msk = _site_with_region("MSK-2", "Moscow")
    circuit = _make_dci_circuit(cid="DCI-MSK")
    ct_a, cab_a = _full_chain_side(1, iface_a, side="A", site=site_msk)
    ct_z, cab_z = _full_chain_side(2, iface_z, cable_id=51, side="Z", site=site_msk)
    nb = _wire_dci_nb(
        [circuit],
        [ct_a, ct_z],
        devices=[dev_a, dev_z],
        interfaces=[iface_a, iface_z],
        cables=[cab_a, cab_z],
    )
    ch = dci_mod.collect_dci_channels(nb)["channels"][0]
    assert ch["region"] == "Moscow"


def test_collect_missing_region_not_completeness_error():
    dev_a = _Record(id=1, name="router-a")
    dev_z = _Record(id=2, name="router-z")
    iface_a = _Record(id=10, name="Eth1", device=dev_a, enabled=True, tags=[_Record(slug="dci")])
    iface_z = _Record(id=11, name="Eth2", device=dev_z, enabled=True, tags=[_Record(slug="dci")])
    circuit = _make_dci_circuit(cid="DCI-X")
    ct_a, cab_a = _full_chain_side(1, iface_a, side="A", site=_Record(name="S1"))
    ct_z, cab_z = _full_chain_side(2, iface_z, cable_id=51, side="Z")
    nb = _wire_dci_nb(
        [circuit],
        [ct_a, ct_z],
        devices=[dev_a, dev_z],
        interfaces=[iface_a, iface_z],
        cables=[cab_a, cab_z],
    )
    ch = dci_mod.collect_dci_channels(nb)["channels"][0]
    assert ch["complete"] is True
    assert ch["region"] == ""


def _sample_items(hostid="10"):
    return [
        {
            "itemid": "101",
            "status": "0",
            "hostid": hostid,
            "name": "Interface Eth1: Bits received",
            "key_": "net.if.in[Eth1]",
        },
        {
            "itemid": "102",
            "status": "0",
            "hostid": hostid,
            "name": "Interface Eth1: Bits sent",
            "key_": "net.if.out[Eth1]",
        },
    ]


def test_build_graph_plan_two_channels_one_region_two_rows():
    channels = [
        {
            "complete": True,
            "cid": "C1",
            "provider": "P",
            "region": "Moscow",
            "a_device": "h1",
            "a_iface": "Eth1",
            "z_device": "h2",
            "z_iface": "Eth1",
        },
        {
            "complete": True,
            "cid": "C2",
            "provider": "P",
            "region": "Moscow",
            "a_device": "h3",
            "a_iface": "Eth1",
            "z_device": "h4",
            "z_iface": "Eth1",
        },
    ]
    hosts = {"h1": "1", "h2": "2", "h3": "3", "h4": "4"}
    items = _sample_items("1") + _sample_items("2") + _sample_items("3") + _sample_items("4")
    pages, _, count = zdd.build_graph_plan(channels, channels, hosts, items)
    assert len(pages) == 1
    assert pages[0]["name"] == "Moscow"
    widgets = pages[0]["widgets"]
    assert count == 4
    ys = sorted({w["y"] for w in widgets})
    assert ys == [0, 5]
    a_row0 = [w for w in widgets if w["y"] == 0 and w["x"] == 0][0]
    z_row0 = [w for w in widgets if w["y"] == 0 and w["x"] == 35][0]
    assert a_row0["width"] == 35
    assert z_row0["width"] == 37


def test_build_graph_plan_two_regions_two_pages():
    channels = [
        {
            "complete": True,
            "cid": "C1",
            "provider": "P",
            "region": "Moscow",
            "a_device": "h1",
            "a_iface": "Eth1",
            "z_device": "h2",
            "z_iface": "Eth1",
        },
        {
            "complete": True,
            "cid": "C2",
            "provider": "P",
            "region": "Warsaw",
            "a_device": "h3",
            "a_iface": "Eth1",
            "z_device": "h4",
            "z_iface": "Eth1",
        },
    ]
    hosts = {"h1": "1", "h2": "2", "h3": "3", "h4": "4"}
    items = _sample_items("1") + _sample_items("2") + _sample_items("3") + _sample_items("4")
    pages, _, _ = zdd.build_graph_plan(channels, channels, hosts, items)
    assert [p["name"] for p in pages] == ["Moscow", "Warsaw"]


def test_build_graph_plan_incomplete_excluded_via_full_only():
    all_ch = [
        {"complete": False, "cid": "bad", "region": "Moscow"},
        {
            "complete": True,
            "cid": "ok",
            "provider": "P",
            "region": "Moscow",
            "a_device": "h1",
            "a_iface": "Eth1",
            "z_device": "",
            "z_iface": "",
        },
    ]
    full = [all_ch[1]]
    pages, _, count = zdd.build_graph_plan(full, all_ch, {"h1": "1"}, _sample_items("1"))
    assert len(pages) == 1
    assert pages[0]["name"] == "Moscow"
    assert count == 1
    assert pages[0]["widgets"][0]["x"] == 0


def test_build_graph_plan_no_region_page_name():
    ch = {
        "complete": True,
        "cid": "C",
        "provider": "P",
        "region": "",
        "a_device": "h1",
        "a_iface": "Eth1",
        "z_device": "",
        "z_iface": "",
    }
    pages, _, _ = zdd.build_graph_plan([ch], [ch], {"h1": "1"}, _sample_items("1"))
    assert pages[0]["name"] == zdd.PAGE_NAME_NO_REGION


def test_build_graph_plan_empty_widgets_page_kept():
    ch = {
        "complete": True,
        "cid": "C",
        "provider": "P",
        "region": "Moscow",
        "a_device": "h1",
        "a_iface": "Eth1",
        "z_device": "h2",
        "z_iface": "Eth1",
    }
    pages, _, count = zdd.build_graph_plan([ch], [ch], {}, [])
    assert count == 0
    assert pages == [{"name": "Moscow", "widgets": []}]


def test_build_graph_plan_no_full_channels_one_empty_page():
    pages, warns, count = zdd.build_graph_plan([], [], {}, [])
    assert count == 0
    assert warns == []
    assert pages == [{"name": zdd.PAGE_NAME_NO_REGION, "widgets": []}]


def test_select_item_status_enabled_preferred():
    items = [
        {"itemid": "1", "status": "1", "hostid": "10", "name": "Interface Eth1: Bits received", "key_": "net.if.in[Eth1]"},
        {"itemid": "2", "status": "0", "hostid": "10", "name": "Interface Eth1: Bits received", "key_": "net.if.in[Eth1]"},
    ]
    iin, iout, warns = zdd.select_items_for_end("h", "10", "Eth1", items)
    assert iin == "2"
    assert not warns


def test_select_item_ambiguous_warning():
    items = [
        {"itemid": "1", "status": "0", "hostid": "10", "name": "Interface Eth1: Bits received", "key_": "net.if.in[Eth1]"},
        {"itemid": "2", "status": "0", "hostid": "10", "name": "Interface Eth1: Bits received", "key_": "net.if.in[Eth1]"},
    ]
    iin, _, warns = zdd.select_items_for_end("h", "10", "Eth1", items)
    assert iin == ""
    assert any("ПРЕДУПРЕЖДЕНИЕ" in w for w in warns)


def test_host_filter_visible_name_rejected(monkeypatch):
    calls = []

    def fake_request(url, token, method, params, debug=False):
        calls.append((method, params))
        if method == "host.get":
            return [{"hostid": "99", "host": "other", "name": "router-a"}], None
        return [], None

    monkeypatch.setattr(zdd, "zabbix_request", fake_request)
    mapping, err = zdd.load_zabbix_hosts("u", "t", {"router-a"})
    assert err is None
    assert mapping == {}


def test_item_get_limit_error(monkeypatch):
    def fake_request(url, token, method, params, debug=False):
        if method == "item.get":
            return [{"itemid": str(i)} for i in range(100000)], None
        return [], None

    monkeypatch.setattr(zdd, "zabbix_request", fake_request)
    items, err = zdd.load_zabbix_items("u", "t", ["1"])
    assert items is None
    assert "100000" in err


def test_dashboard_zero_create_one_update_many_error(monkeypatch):
    seq = {"dashboard.get": [[], [{"dashboardid": "1"}]], "n": 0}

    def fake_request(url, token, method, params, debug=False):
        if method == "dashboard.get":
            idx = seq["n"]
            seq["n"] += 1
            if idx == 0:
                return [], None
            if idx == 1:
                return [{"dashboardid": "5"}], None
            return [{"dashboardid": "1"}, {"dashboardid": "2"}], None
        if method == "dashboard.create":
            return {"dashboardids": ["9"]}, None
        if method == "dashboard.update":
            return {"dashboardids": ["5"]}, None
        return None, "unexpected"

    monkeypatch.setattr(zdd, "zabbix_request", fake_request)
    did, err = zdd.write_dci_dashboard("u", "t", [], "DCI")
    assert err is None and did == "9"
    did2, err2 = zdd.write_dci_dashboard("u", "t", [], "DCI")
    assert err2 is None and did2 == "5"
    did3, err3 = zdd.write_dci_dashboard("u", "t", [], "DCI")
    assert did3 is None and "more than one" in err3


def test_dry_run_no_zabbix_writes(monkeypatch, capsys):
    monkeypatch.delenv("ZABBIX_URL", raising=False)
    monkeypatch.delenv("ZABBIX_TOKEN", raising=False)
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    nb = _wire_dci_nb([], [])
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    code = zdd.main(["--dry-run"])
    assert code == 0
    assert "dashboard" not in capsys.readouterr().out.lower()


def test_missing_netbox_credentials(monkeypatch):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    assert zdd.main(["--dry-run"]) == 1


def test_zabbix_unavailable_no_prompt(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_URL", "https://z.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "bad")
    nb = _wire_dci_nb([], [])
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(zdd, "validate_zabbix_token", lambda *a, **k: (False, "auth fail"))
    monkeypatch.setattr(zdd, "prompt_write_dashboard", lambda: (_ for _ in ()).throw(AssertionError("prompt")))
    assert zdd.main([]) == 1


def test_confirm_yes_writes(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_URL", "https://z.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "tok")
    nb = _wire_dci_nb([], [])
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(zdd, "validate_zabbix_token", lambda *a, **k: (True, None))
    monkeypatch.setattr(zdd, "prompt_write_dashboard", lambda: True)
    written = []

    def fake_write(url, token, pages, name, debug=False):
        written.append(pages)
        return "1", None

    monkeypatch.setattr(zdd, "write_dci_dashboard", fake_write)
    assert zdd.main([]) == 0
    assert written == [[{"name": "Без региона", "widgets": []}]]


def test_confirm_no_exit_zero(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_URL", "https://z.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "tok")
    nb = _wire_dci_nb([], [])
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(zdd, "validate_zabbix_token", lambda *a, **k: (True, None))
    monkeypatch.setattr(zdd, "prompt_write_dashboard", lambda: False)
    called = []
    monkeypatch.setattr(zdd, "write_dci_dashboard", lambda *a, **k: called.append(1) or ("1", None))
    assert zdd.main([]) == 0
    assert called == []


def test_confirm_eof_exit_zero(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_URL", "https://z.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "tok")
    nb = _wire_dci_nb([], [])
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(zdd, "validate_zabbix_token", lambda *a, **k: (True, None))
    monkeypatch.setattr(zdd, "prompt_write_dashboard", lambda: False)
    assert zdd.main([]) == 0


def test_ctrl_c_exit_130(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_URL", "https://z.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "tok")
    nb = _wire_dci_nb([], [])
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(zdd, "validate_zabbix_token", lambda *a, **k: (True, None))

    def boom():
        raise KeyboardInterrupt()

    monkeypatch.setattr(zdd, "prompt_write_dashboard", boom)
    assert zdd.main([]) == 130


def test_run_uplinks_full_does_not_call_dci():
    with open("run_uplinks_full.py", encoding="utf-8") as f:
        text = f.read()
    assert "zabbix_dci_dashboard" not in text


def test_reason_order_incomplete():
    circuit = _make_dci_circuit(cid="", status="planned")
    nb = _wire_dci_nb([circuit], [])
    ch = dci_mod.collect_dci_channels(nb)["channels"][0]
    assert ch["reason_text"].startswith("A: нет ровно одного окончания")
    parts = ch["reason_text"].split("; ")
    assert parts.index("статус не active") > parts.index("A: нет ровно одного окончания")


def test_graph_header_provider_prefix():
    channels = [
        {"cid": "X", "provider": "P1"},
        {"cid": "X", "provider": "P2", "a_device": "d", "a_iface": "i"},
    ]
    amb = zdd._cid_needs_provider_prefix(channels)
    title = zdd.graph_header(channels[1], "A", amb)
    assert title.startswith("P2 X")


def test_dashboard_write_pages_create_update(monkeypatch):
    seen = []

    def fake_request(url, token, method, params, debug=False):
        seen.append((method, params))
        if method == "dashboard.get":
            if len(seen) == 1:
                return [], None
            return [{"dashboardid": "5"}], None
        if method == "dashboard.create":
            return {"dashboardids": ["9"]}, None
        if method == "dashboard.update":
            return {"dashboardids": ["5"]}, None
        return None, "unexpected"

    monkeypatch.setattr(zdd, "zabbix_request", fake_request)
    pages = [{"name": "Moscow", "widgets": []}, {"name": "Warsaw", "widgets": []}]
    did, err = zdd.write_dci_dashboard("u", "t", pages, "DCI")
    assert err is None and did == "9"
    assert seen[1][1]["pages"] == pages
    did2, err2 = zdd.write_dci_dashboard("u", "t", pages, "DCI")
    assert err2 is None and did2 == "5"
    assert seen[-1][1]["pages"] == pages


def test_build_widget_uses_itemids_not_patterns():
    w = zdd.build_dci_graph_widget(0, "title", "12345", "67890", 0, 0, 35)
    names = [f["name"] for f in w["fields"]]
    assert "ds.0.itemids.0" in names
    assert not any("hosts" in n for n in names)


def test_ignore_empty_type_with_type_id():
    circuit = _Record(
        id=100,
        cid="orphan",
        provider_id=1,
        provider=1,
        status="active",
        type=None,
        type_id=99,
    )
    nb = _wire_dci_nb([circuit], [])
    report = dci_mod.collect_dci_channels(nb)
    assert report["channels"] == []


def test_non_digit_itemid_warning():
    items = [
        {
            "itemid": "abc",
            "status": "0",
            "hostid": "10",
            "name": "Interface Eth1: Bits received",
            "key_": "net.if.in[Eth1]",
        },
    ]
    iin, _, warns = zdd.select_items_for_end("h", "10", "Eth1", items)
    assert iin == ""
    assert warns


def test_full_pairs_no_items_empty_dashboard_on_yes(monkeypatch):
    dev_a = _Record(id=1, name="router-a")
    dev_z = _Record(id=2, name="router-z")
    iface_a = _Record(id=10, name="Eth1", device=dev_a, device_id=1, enabled=True, tags=[_Record(slug="dci")])
    iface_z = _Record(id=11, name="Eth2", device=dev_z, device_id=2, enabled=True, tags=[_Record(slug="dci")])
    circuit = _make_dci_circuit(cid="C1")
    ct_a, cab_a = _full_chain_side(1, iface_a, side="A")
    ct_z, cab_z = _full_chain_side(2, iface_z, cable_id=51, side="Z")
    nb = _wire_dci_nb(
        [circuit],
        [ct_a, ct_z],
        devices=[dev_a, dev_z],
        interfaces=[iface_a, iface_z],
        cables=[cab_a, cab_z],
    )
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_URL", "https://z.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "tok")
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(zdd, "validate_zabbix_token", lambda *a, **k: (True, None))
    monkeypatch.setattr(
        zdd,
        "load_zabbix_hosts",
        lambda *a, **k: ({"router-a": "5", "router-z": "6"}, None),
    )
    monkeypatch.setattr(zdd, "load_zabbix_items", lambda *a, **k: ([], None))
    monkeypatch.setattr(zdd, "prompt_write_dashboard", lambda: True)
    captured = []

    def fake_write(url, token, pages, name, debug=False):
        captured.append(pages)
        return "1", None

    monkeypatch.setattr(zdd, "write_dci_dashboard", fake_write)
    assert zdd.main([]) == 0
    assert captured == [[{"name": "Без региона", "widgets": []}]]


def test_empty_pairs_no_host_item_before_prompt(monkeypatch):
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    monkeypatch.setenv("NETBOX_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_URL", "https://z.example")
    monkeypatch.setenv("ZABBIX_TOKEN", "tok")
    nb = _wire_dci_nb([], [])
    monkeypatch.setattr("pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(zdd, "validate_zabbix_token", lambda *a, **k: (True, None))
    calls = []

    def track_host(*a, **k):
        calls.append("host")
        return {}, None

    def track_item(*a, **k):
        calls.append("item")
        return [], None

    monkeypatch.setattr(zdd, "load_zabbix_hosts", track_host)
    monkeypatch.setattr(zdd, "load_zabbix_items", track_item)
    monkeypatch.setattr(zdd, "prompt_write_dashboard", lambda: False)
    assert zdd.main([]) == 0
    assert calls == []
