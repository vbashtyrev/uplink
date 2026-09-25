"""zabbix_map update_uplinks_map with prune_obsolete removing stale elements."""

from pathlib import Path

from tests.mocks.inventory_scope import dry_ssh_minimal_inventory_context
from tests.mocks.map_state import MapStateTracker
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from uplinks.data import load_devices_json
from zabbix_map import update_uplinks_map

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_update_map_prunes_obsolete_selements(monkeypatch, zabbix_env):
    data, _ = load_devices_json(str(FIXTURES / "dry_ssh_minimal.json"))
    devices = data["devices"]
    host_id = {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
        },
        ("FRN-MX-1", "ae5.0"): {
            "itemid_in": "503",
            "itemid_out": "504",
            "bits_in": 'net.if.in[ae5]',
            "bits_out": 'net.if.out[ae5]',
        },
    }
    desc = {"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane"}

    map_state = {
        "sysmapid": "55",
        "selements": [
            {"selementid": "1", "elementtype": 0, "elements": [{"hostid": "101"}], "label": "ALA-KZT-7280TR-1"},
            {"selementid": "99", "elementtype": 0, "elements": [{"hostid": "999"}], "label": "OLD-HOST"},
            {"selementid": "2", "elementtype": 4, "label": "Cogent"},
            {"selementid": "3", "elementtype": 4, "label": "Stale-ISP"},
        ],
        "links": [],
    }

    map_tracker = MapStateTracker(
        sysmapid="55",
        selements=map_state["selements"],
        links=map_state["links"],
    )

    (
        build_standard_zabbix_mocker()
        .on("map.get", map_tracker.map_get)
        .on("map.update", map_tracker.map_update)
        .on("map.create", map_tracker.map_create)
    ).activate(monkeypatch)
    updates = map_tracker.updates
    monkeypatch.setattr("zabbix_map.get_provider_aggregate_triggers", lambda *a, **k: {})
    monkeypatch.setattr("zabbix_map.get_link_commit_triggers", lambda *a, **k: {})

    err, sid = update_uplinks_map(
        "https://z.example/api_jsonrpc.php",
        "t",
        devices,
        host_id,
        items,
        desc,
        prune_obsolete=True,
        device_iface_to_provider=dry_ssh_minimal_inventory_context()["device_iface_to_provider"],
    )
    assert err is None
    assert updates
    labels = {s.get("label") for s in map_tracker.selements}
    assert "OLD-HOST" not in labels
    assert "Stale-ISP" not in labels
