"""get_commit_rates_from_netbox with MockNetBox."""

from tests.mocks.netbox_api import build_netbox_for_commit_rates
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
