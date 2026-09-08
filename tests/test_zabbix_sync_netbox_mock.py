"""get_commit_rates_from_netbox with MockNetBox."""

from unittest.mock import patch

import zabbix_sync_commit_rate as zsc
from tests.mocks.netbox_api import build_netbox_for_commit_rates
from uplinks.netbox import inventory as inv
from zabbix_sync_commit_rate import KBPS_TO_BPS, get_commit_rates_from_netbox


def test_get_commit_rates_from_netbox():
    nb = build_netbox_for_commit_rates(
        device_name="ALA-R1",
        iface_name="Ethernet51/1",
        commit_rate_kbps=10000,
        tag_device=True,
    )
    result = get_commit_rates_from_netbox(nb, tag="uplinks", debug=False)
    assert result == {("ALA-R1", "Ethernet51/1"): 10000 * KBPS_TO_BPS}


def test_get_commit_rates_skips_untagged_device():
    nb = build_netbox_for_commit_rates(tag_device=False)
    result = get_commit_rates_from_netbox(nb, tag="uplinks", debug=False)
    assert result == {}


def test_get_commit_rates_no_tag_filter():
    nb = build_netbox_for_commit_rates(tag_device=False)
    result = get_commit_rates_from_netbox(nb, tag=None, debug=False)
    assert ("router1", "Ethernet51/1") in result


def test_get_commit_rates_uses_inventory_collector():
    nb = build_netbox_for_commit_rates(commit_rate_kbps=5000)
    with patch.object(zsc, "collect_uplink_inventory", wraps=inv.collect_uplink_inventory) as collector:
        result = get_commit_rates_from_netbox(nb, tag="uplinks", debug=False)
    collector.assert_called_once()
    assert collector.call_args.kwargs.get("active_only") is True
    assert result == {("router1", "Ethernet51/1"): 5000 * KBPS_TO_BPS}


def test_get_commit_rates_skips_missing_commit_rate():
    nb = build_netbox_for_commit_rates(commit_rate_kbps=None)
    result = get_commit_rates_from_netbox(nb, tag="uplinks", debug=False)
    assert result == {}


def test_get_commit_rates_skips_inactive_circuit():
    nb = build_netbox_for_commit_rates(circuit_status="planned")
    result = get_commit_rates_from_netbox(nb, tag="uplinks", debug=False)
    assert result == {}
