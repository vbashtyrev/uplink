"""Tests for zabbix_provider_sla.main()."""

import json
import sys
from pathlib import Path

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_with_commit_rates(monkeypatch, zabbix_env, tmp_path, capsys):
    import zabbix_provider_sla as mod

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "_provider_limits": {"Cogent": 10},
                "_provider_sla": 99.9,
                "ALA-R1": {
                    "Eth1": {
                        "provider": "Cogent",
                        "circuit_id": "CKT-1",
                        "billing_model": "Burst",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
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
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_sla.py", "-f", str(cr), "--days", "1"])
    mod.main()
    out = capsys.readouterr().out
    assert "SLA window" in out
    assert "Cogent" in out or "Burst" in out
