"""zabbix_map update_uplinks_map with prune_obsolete removing stale elements."""

from pathlib import Path

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import load_devices_json, update_uplinks_map

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

    def map_get(params):
        if params.get("sysmapids"):
            return [dict(map_state)]
        return [{"sysmapid": "55"}]

    updates = []
    (
        build_standard_zabbix_mocker()
        .on("map.get", map_get)
        .on("map.update", lambda p: updates.append(p) or True)
        .on("map.create", lambda p: {"sysmapids": ["55"]})
    ).activate(monkeypatch)
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
    )
    assert err is None
    assert updates
    selements = updates[-1].get("selements", [])
    labels = {s.get("label") for s in selements}
    assert "OLD-HOST" not in labels
    assert "Stale-ISP" not in labels
