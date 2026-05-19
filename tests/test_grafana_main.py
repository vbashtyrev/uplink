"""Tests for grafana_uplinks_graph.main()."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_stdout_json(monkeypatch, capsys):
    import grafana_uplinks_graph as mod

    monkeypatch.setattr(
        sys,
        "argv",
        ["grafana_uplinks_graph.py", "-f", str(FIXTURES / "dry_ssh_minimal.json")],
    )
    mod.main()
    out = json.loads(capsys.readouterr().out)
    assert "nodes" in out
    assert "edges" in out


def test_main_with_zabbix_cache(monkeypatch, zabbix_env, tmp_path, capsys):
    import grafana_uplinks_graph as mod

    items = [
        {
            "itemid": "501",
            "hostid": "101",
            "name": 'Interface Ethernet51/1: Bits received',
            "key_": 'net.if.in["Ethernet51/1"]',
        },
        {
            "itemid": "502",
            "hostid": "101",
            "name": 'Interface Ethernet51/1: Bits sent',
            "key_": 'net.if.out["Ethernet51/1"]',
        },
    ]
    build_standard_zabbix_mocker(
        hosts=[
            {"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"},
            {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
        ],
        items=items,
    ).activate(monkeypatch)
    out_file = tmp_path / "graph.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "grafana_uplinks_graph.py",
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--zabbix",
            "--no-cache",
            "-o",
            str(out_file),
        ],
    )
    mod.main()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(data["edges"]) >= 1


def test_main_grafana_api_push(monkeypatch, zabbix_env, capsys):
    import grafana_uplinks_graph as mod

    monkeypatch.setenv("GRAFANA_URL", "https://grafana.example")
    monkeypatch.setenv("GRAFANA_API_KEY", "key")
    monkeypatch.setattr(mod, "_grafana_push_dashboard", lambda *a, **k: None)
    build_standard_zabbix_mocker(
        hosts=[
            {"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"},
            {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
        ],
        items=[
            {
                "itemid": "501",
                "hostid": "101",
                "name": 'Interface Ethernet51/1: Bits received',
                "key_": 'net.if.in["Ethernet51/1"]',
            },
        ],
    ).activate(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "grafana_uplinks_graph.py",
            "-f",
            str(FIXTURES / "dry_ssh_minimal.json"),
            "--zabbix",
            "--no-cache",
            "--grafana-api",
        ],
    )
    mod.main()
