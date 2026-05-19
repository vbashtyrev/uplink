"""run_uplinks_full: full pipeline with mocked subprocess steps."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_all_steps_success(monkeypatch, tmp_path):
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
        lambda self: full.argparse.Namespace(
            no_fetch=True,
            from_file=True,
            refresh=False,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=False,
            no_burst_triggers=False,
            location="ALA",
            stop_on_error=False,
            no_stop_on_error=True,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
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

    sync = next(c for c in calls if c[1] == "zabbix_sync_commit_rate.py")
    assert sync[2:6] == ["-d", "dry-ssh.json", "-f", "commit_rates.json"]
    assert "--create-link-triggers" in sync

    circuits = next(c for c in calls if c[1] == "netbox_create_circuits.py")
    assert circuits[2:6] == ["-f", "commit_rates.json", "-d", "dry-ssh.json"]
    assert circuits[-2:] == ["--location", "ALA"]

    services = next(c for c in calls if c[1] == "zabbix_provider_services.py")
    assert services[-2:] == ["--parent-service", "Uplinks providers"]

    assert scripts.index("zabbix_provider_aggregate.py") < scripts.index("zabbix_map.py")
    assert scripts.count("zabbix_map.py") == 1


def test_main_fetch_includes_uplinks_stats(monkeypatch, tmp_path):
    """Without --no-fetch/--from-file, step 1 calls uplinks_stats before NetBox."""
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
        return True, "{}", ""

    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: full.argparse.Namespace(
            no_fetch=False,
            from_file=False,
            refresh=True,
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
        ),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    scripts = [c[1] for c in calls]
    assert scripts[0] == "uplinks_stats.py"
    assert scripts[1:9] == [
        "netbox_checks.py",
        "generate_commit_rates.py",
        "netbox_create_circuits.py",
        "zabbix_sync_commit_rate.py",
        "zabbix_provider_aggregate.py",
        "zabbix_map.py",
        "zabbix_uplinks_dashboard.py",
        "zabbix_provider_services.py",
    ]


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
        lambda self: full.argparse.Namespace(
            no_fetch=True,
            from_file=True,
            refresh=False,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=False,
            no_burst_triggers=True,
            location=None,
            stop_on_error=False,
            no_stop_on_error=True,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        ),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 0

    sync = next(c for c in calls if c[1] == "zabbix_sync_commit_rate.py")
    assert "--create-link-triggers" not in sync
