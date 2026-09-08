"""run_uplinks_full: full pipeline with mocked subprocess steps."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _default_ns(**overrides):
    base = dict(
        auto=False,
        no_fetch=True,
        from_file=True,
        refresh=False,
        dry_ssh="dry-ssh.json",
        commit_rates="commit_rates.json",
        no_netbox_apply=False,
        no_burst_triggers=False,
        location=None,
        stop_on_error=False,
        no_stop_on_error=True,
        report=None,
        timeout=60,
        env_file="urls.env",
        no_env_file=True,
    )
    base.update(overrides)
    return full.argparse.Namespace(**base)


def test_main_human_mode_all_steps_success(monkeypatch, tmp_path):
    """Default (human/NetBox-first): inventory read-only, no generate/create."""
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    desc = tmp_path / "description_to_name.json"
    desc.write_text("{}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        return True, "ok line", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _default_ns())
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    expected_order = [
        "netbox_checks.py",
        "netbox_uplinks_inventory.py",
        "zabbix_sync_commit_rate.py",
        "zabbix_provider_aggregate.py",
        "zabbix_map.py",
        "zabbix_uplinks_dashboard.py",
        "zabbix_provider_services.py",
    ]
    assert scripts == expected_order
    assert "generate_commit_rates.py" not in scripts
    assert "netbox_create_circuits.py" not in scripts

    netbox_checks = next(c for c in calls if c[1] == "netbox_checks.py")
    assert "--existing-only" in netbox_checks
    assert "--auto" not in netbox_checks

    inventory = next(c for c in calls if c[1] == "netbox_uplinks_inventory.py")
    assert "--dry-run" in inventory

    sync = next(c for c in calls if c[1] == "zabbix_sync_commit_rate.py")
    assert sync[2:6] == ["-d", "dry-ssh.json", "-f", "commit_rates.json"]
    assert "--create-link-triggers" in sync

    services = next(c for c in calls if c[1] == "zabbix_provider_services.py")
    assert services[-2:] == ["--parent-service", "Uplinks providers"]

    assert scripts.index("zabbix_provider_aggregate.py") < scripts.index("zabbix_map.py")
    assert scripts.count("zabbix_map.py") == 1


def test_main_auto_all_steps_success(monkeypatch, tmp_path):
    """--auto: legacy chain with generate_commit_rates and netbox_create_circuits."""
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    desc = tmp_path / "description_to_name.json"
    desc.write_text("{}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        return True, "ok line", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _default_ns(auto=True, location="ALA"),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    expected_order = [
        "netbox_checks.py",
        "generate_commit_rates.py",
        "netbox_create_circuits.py",
        "zabbix_sync_commit_rate.py",
        "zabbix_provider_aggregate.py",
        "zabbix_map.py",
        "zabbix_uplinks_dashboard.py",
        "zabbix_provider_services.py",
    ]
    assert scripts == expected_order
    assert "netbox_uplinks_inventory.py" not in scripts

    netbox_checks = next(c for c in calls if c[1] == "netbox_checks.py")
    assert "--auto" in netbox_checks
    assert "--existing-only" not in netbox_checks

    circuits = next(c for c in calls if c[1] == "netbox_create_circuits.py")
    assert circuits[2:6] == ["-f", "commit_rates.json", "-d", "dry-ssh.json"]
    assert circuits[-2:] == ["--location", "ALA"]


def test_main_location_without_auto_exits(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "run_cmd", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _default_ns(location="ALA"),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 2


def test_main_fetch_includes_uplinks_stats(monkeypatch, tmp_path):
    """Without --no-fetch/--from-file, human mode fetches inventory then uplinks_stats."""
    dry = tmp_path / "dry-ssh.json"
    dry.write_text("{}", encoding="utf-8")
    (tmp_path / "commit_rates.json").write_text("{}", encoding="utf-8")
    (tmp_path / "description_to_name.json").write_text("{}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")
    monkeypatch.setattr(full, "DEFAULT_NETBOX_INVENTORY", "netbox_inventory.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
        if "netbox_uplinks_inventory.py" in argv and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(
                    {
                        "complete": [{"device": "ALA-KZT-7280TR-1", "interface": "Eth1", "provider": "P"}],
                        "incomplete": [],
                        "stats": {"complete": 1, "incomplete": 0},
                    }
                ),
                encoding="utf-8",
            )
            return True, "", ""
        return True, "{}", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _default_ns(no_fetch=False, from_file=False, refresh=True),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    assert scripts[0] == "netbox_uplinks_inventory.py"
    assert scripts[1] == "uplinks_stats.py"
    assert "--inventory-file" in calls[1]
    assert scripts[2:9] == [
        "netbox_checks.py",
        "netbox_uplinks_inventory.py",
        "zabbix_sync_commit_rate.py",
        "zabbix_provider_aggregate.py",
        "zabbix_map.py",
        "zabbix_uplinks_dashboard.py",
        "zabbix_provider_services.py",
    ]


def test_main_inventory_failure_stop_on_error(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text("{}", encoding="utf-8")
    (tmp_path / "commit_rates.json").write_text("{}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")

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
    dry = tmp_path / "dry-ssh.json"
    dry.write_text("{}", encoding="utf-8")
    (tmp_path / "commit_rates.json").write_text("{}", encoding="utf-8")
    (tmp_path / "description_to_name.json").write_text("{}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(list(argv))
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
