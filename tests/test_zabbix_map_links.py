"""zabbix_map update_uplinks_map with link creation and triggers."""

from pathlib import Path

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import MAP_NAME, load_devices_json, update_uplinks_map

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_update_map_creates_links(monkeypatch, zabbix_env):
    data, _ = load_devices_json(str(FIXTURES / "dry_ssh_minimal.json"))
    devices = {"ALA-KZT-7280TR-1": data["devices"]["ALA-KZT-7280TR-1"]}
    host_id = {"ALA-KZT-7280TR-1": "101"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "501",
            "itemid_out": "502",
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
        },
    }
    desc = {"Uplink: Cogent 10G": "Cogent"}

    map_state = {
        "sysmapid": "55",
        "selements": [
            {"selementid": "1", "elementtype": 0, "elements": [{"hostid": "101"}], "label": "ALA-KZT-7280TR-1"},
            {"selementid": "2", "elementtype": 4, "label": "Cogent"},
        ],
        "links": [],
    }

    def map_get(params):
        if params.get("sysmapids"):
            return [dict(map_state)]
        if (params.get("filter") or {}).get("name") == MAP_NAME:
            return [{"sysmapid": "55"}]
        return []

    updates = []

    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", map_get)
        .on("map.create", lambda p: {"sysmapids": ["55"]})
        .on("map.update", lambda p: updates.append(p) or True)
    )
    mocker.activate(monkeypatch)
    monkeypatch.setattr(
        "zabbix_map.get_provider_aggregate_triggers",
        lambda url, token, providers, debug=False: {"Cogent": ("11", "12")},
    )
    monkeypatch.setattr(
        "zabbix_map.get_link_commit_triggers",
        lambda url, token, hostids, debug=False: {("101", "ethernet51/1"): ("21", "22")},
    )

    err, sid = update_uplinks_map(
        "https://z.example/api_jsonrpc.php",
        "t",
        devices,
        host_id,
        items,
        desc,
        debug=True,
    )
    assert err is None
    assert sid == "55"
    link_updates = [u for u in updates if u.get("links")]
    assert link_updates
    assert link_updates[-1]["links"]
