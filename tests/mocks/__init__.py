"""Test doubles for Zabbix JSON-RPC and NetBox API."""

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_rpc import ZabbixRpcMocker, mock_response

__all__ = [
    "ZabbixRpcMocker",
    "mock_response",
    "build_netbox_for_commit_rates",
]
