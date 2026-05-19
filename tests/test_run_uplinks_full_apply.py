"""run_uplinks_full with netbox apply step."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_with_netbox_apply(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "commit_rates.json")

    steps = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        steps.append(" ".join(str(x) for x in argv))
        return True, "ok", ""

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
    with patch.object(full, "_write_run_report"):
        with pytest.raises(SystemExit) as exc:
            full.main()
    assert exc.value.code == 0
    assert any("netbox_checks" in s for s in steps)
