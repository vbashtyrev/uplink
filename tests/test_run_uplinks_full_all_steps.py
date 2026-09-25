"""run_uplinks_full: full pipeline with mocked subprocess steps."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _default_ns(**overrides):
    base = dict(
        plan=False,
        dry_ssh="dry-ssh.json",
        no_netbox_apply=False,
        no_burst_triggers=False,
        stop_on_error=False,
        no_stop_on_error=True,
        report=None,
        timeout=60,
        env_file="urls.env",
        no_env_file=True,
        netbox_checks=False,
    )
    base.update(overrides)
    return full.argparse.Namespace(**base)


def _inventory_json_payload():
    return {
        "complete": [{"device": "ALA-KZT-7280TR-1", "interface": "Eth1", "provider": "P"}],
        "incomplete": [],
        "stats": {"complete": 1, "incomplete": 0, "providers": 1},
    }


def test_main_human_mode_all_steps_success(monkeypatch, tmp_path):
    """Default (human/NetBox-first): inventory then Zabbix without SSH or netbox_checks."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(_inventory_json_payload()), encoding="utf-8"
            )
        return True, "ok line", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _default_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    expected_order = [
        "netbox_uplinks_inventory.py",
        "zabbix_sync_commit_rate.py",
        "zabbix_provider_aggregate.py",
        "zabbix_map.py",
        "zabbix_uplinks_dashboard.py",
        "zabbix_provider_services.py",
    ]
    assert scripts == expected_order
    assert "uplinks_stats.py" not in scripts
    assert "netbox_checks.py" not in scripts
    assert "generate_commit_rates.py" not in scripts
    assert "netbox_create_circuits.py" not in scripts

    sync = next(c for c in calls if c[1] == "zabbix_sync_commit_rate.py")
    assert "--inventory-file" in sync
    assert "-d" not in sync
    assert "--create-link-triggers" in sync

    aggregate = next(c for c in calls if c[1] == "zabbix_provider_aggregate.py")
    assert "--inventory-file" in aggregate
    assert "-d" not in aggregate

    services = next(c for c in calls if c[1] == "zabbix_provider_services.py")
    assert "--inventory-file" in services
    assert "--parent-service" in services
    assert "-f" not in services

    assert scripts.index("zabbix_provider_aggregate.py") < scripts.index("zabbix_map.py")
    assert scripts.count("zabbix_map.py") == 1


def test_main_human_mode_optional_netbox_checks(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(_inventory_json_payload()), encoding="utf-8"
            )
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _default_ns(netbox_checks=True),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0
    scripts = [c[1] for c in calls]
    assert scripts[1] == "netbox_checks.py"


def test_main_provider_services_without_commit_rates_file(monkeypatch, tmp_path):
    """Human mode must not require commit_rates.json for provider services step."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(_inventory_json_payload()), encoding="utf-8"
            )
        return True, "ok line", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _default_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    services = next(c for c in calls if c[1] == "zabbix_provider_services.py")
    assert "--inventory-file" in services
    assert "--parent-service" in services
    assert "-f" not in services
    assert not (tmp_path / "commit_rates.json").exists()


def test_main_inventory_failure_stop_on_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        if "netbox_uplinks_inventory.py" in argv:
            return False, "", "inventory failed"
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _default_ns(stop_on_error=True, no_stop_on_error=False),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 1


def test_main_no_burst_triggers_omits_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(_inventory_json_payload()), encoding="utf-8"
            )
        return True, "", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _default_ns(no_burst_triggers=True),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    sync = next(c for c in calls if c[1] == "zabbix_sync_commit_rate.py")
    assert "--create-link-triggers" not in sync
