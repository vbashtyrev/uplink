"""Tests for zabbix_provider_sla.main()."""

import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_with_commit_rates(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_provider_sla as mod

    nb = build_netbox_for_commit_rates(
        device_name="ALA-R1",
        iface_name="Eth1",
        provider_name="Cogent",
        provider_custom_fields={"aggregate_limit_gbps": 10},
        circuit_id=1,
        circuit_custom_fields={"billing_model": "Burst"},
        commit_rate_kbps=10_000_000,
        tag_device=False,
    )
    cr = tmp_path / "commit_rates.json"
    mocker = build_standard_zabbix_mocker().on(
        "trigger.get",
        lambda p: [
            {
                "triggerid": "99",
                "description": "Provider Cogent aggregate SLA",
                "tags": [{"tag": "scripts", "value": "automatization"}],
            },
        ],
    ).on("event.get", lambda p: [])
    mocker.activate(monkeypatch)
    with patch("zabbix_provider_services.netbox_client_from_env", return_value=nb), patch(
        "zabbix_provider_services.netbox_border_tag", return_value=None
    ):
        monkeypatch.setattr(
            sys, "argv", ["zabbix_provider_sla.py", "-f", str(cr), "--days", "1"]
        )
        mod.main()
    out = capsys.readouterr().out
    assert "SLA window" in out
    assert "Cogent" in out or "Burst" in out
