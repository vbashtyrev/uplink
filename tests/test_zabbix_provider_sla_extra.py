"""zabbix_provider_sla helpers and main edge paths."""

import json
import sys
from datetime import datetime, timezone

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from uplinks_config import TRIGGER_DESC_90_SUFFIX, TRIGGER_DESC_SLA_BREACH_SUFFIX
import zabbix_provider_sla as sla_mod


def test_unix_ts_variants():
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert sla_mod._unix_ts(1717243200) == 1717243200
    assert sla_mod._unix_ts("2024-06-01T12:00:00+00:00") > 0
    assert sla_mod._unix_ts(now) > 0
    assert sla_mod._unix_ts("not-a-date") > 0


def test_get_hostid_by_name(monkeypatch):
    (
        build_standard_zabbix_mocker(
            hosts=[{"hostid": "77", "host": "other", "name": "ALA-R1"}]
        )
        .activate(monkeypatch)
    )
    hid, err = sla_mod._get_hostid_for_device(
        "https://z.example/api_jsonrpc.php", "t", "ALA-R1"
    )
    assert err is None
    assert hid == "77"


def test_get_burst_link_triggers_classify(monkeypatch):
    prefix = "Interface Eth1:"
    triggers = [
        {
            "triggerid": "1",
            "description": prefix + TRIGGER_DESC_90_SUFFIX,
        },
        {
            "triggerid": "2",
            "description": prefix + TRIGGER_DESC_SLA_BREACH_SUFFIX,
        },
    ]
    (
        build_standard_zabbix_mocker()
        .on("trigger.get", lambda p: triggers)
        .activate(monkeypatch)
    )
    warn, high, sla = sla_mod._get_burst_link_triggers(
        "https://z.example/api_jsonrpc.php", "t", "101", "Eth1"
    )
    assert warn == "1"
    assert sla == "2"
    assert high is None


def test_main_burst_host_missing(tmp_path, monkeypatch, zabbix_env, capsys):
    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "MISSING-DEV",
                        "interface": "Eth1",
                        "provider": "Cogent",
                        "circuit_id": "CKT-99",
                        "billing_model": "Burst",
                        "commit_rate_kbps": 10_000_000,
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.0},
                "provider_limits_gbps": {"Cogent": 10},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )
    build_standard_zabbix_mocker(hosts=[]).activate(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_provider_sla.py",
            "--inventory-file",
            str(inv),
            "--days",
            "1",
        ],
    )
    sla_mod.main()
    captured = capsys.readouterr()
    assert "host not found" in captured.err
    assert "CKT-99" in captured.out
