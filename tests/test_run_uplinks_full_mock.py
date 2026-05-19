"""run_uplinks_full.py step orchestration with mocked subprocess steps."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def minimal_dry_ssh(tmp_path):
    src = FIXTURES / "dry_ssh_minimal.json"
    dst = tmp_path / "dry-ssh.json"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


@pytest.fixture
def minimal_desc_map(tmp_path):
    path = tmp_path / "description_to_name.json"
    path.write_text(
        json.dumps(
            {
                "Uplink: Cogent 10G": "Cogent",
                "Uplink: Hurricane": "Hurricane",
                "Uplink: Hurricane member": "Hurricane",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_run_no_fetch_executes_steps(monkeypatch, tmp_path, minimal_dry_ssh, minimal_desc_map):
    monkeypatch.chdir(tmp_path)
    commit_rates = tmp_path / "commit_rates.json"
    commit_rates.write_text('{"_comment": "t"}', encoding="utf-8")

    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(argv[1] if len(argv) > 1 else str(argv))
        if "uplinks_stats.py" in str(argv):
            return False, "", "should not run"
        if "generate_commit_rates.py" in str(argv):
            commit_rates.write_text(
                json.dumps({"host1": {"Eth1": {"provider": "P", "circuit_id": "P-1"}}}),
                encoding="utf-8",
            )
        return True, "ok", ""

    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "description_to_name.json")
    monkeypatch.setattr(full, "run_cmd", fake_run_cmd)

    shutil_desc = minimal_desc_map
    (tmp_path / "description_to_name.json").write_text(
        shutil_desc.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "dry-ssh.json").write_text(minimal_dry_ssh.read_text(encoding="utf-8"), encoding="utf-8")

    with patch.object(full.argparse.ArgumentParser, "parse_args") as mock_parse:
        mock_parse.return_value = full.argparse.Namespace(
            no_fetch=True,
            from_file=False,
            refresh=False,
            dry_ssh="dry-ssh.json",
            commit_rates="commit_rates.json",
            no_netbox_apply=False,
            grafana=False,
            location=None,
            stop_on_error=True,
            report=None,
            timeout=60,
            env_file="urls.env",
            no_env_file=True,
        )
        with pytest.raises(SystemExit) as exc:
            full.main()
        assert exc.value.code == 0

    scripts = [c for c in calls if c.endswith(".py")]
    assert "netbox_checks.py" in scripts
    assert "generate_commit_rates.py" in scripts
    assert "netbox_create_circuits.py" in scripts
    assert "zabbix_sync_commit_rate.py" in scripts
    assert "zabbix_map.py" in scripts
    assert "zabbix_provider_aggregate.py" in scripts
    assert "uplinks_stats.py" not in scripts
