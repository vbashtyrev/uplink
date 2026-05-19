"""run_uplinks_full: stop_on_error, fetch failure, debug log."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _base_args(tmp_path):
    return full.argparse.Namespace(
        no_fetch=True,
        from_file=True,
        refresh=False,
        dry_ssh=str(tmp_path / "dry-ssh.json"),
        commit_rates=str(tmp_path / "commit_rates.json"),
        no_netbox_apply=False,
        no_burst_triggers=False,
        location="ALA",
        stop_on_error=True,
        no_stop_on_error=False,
        report=None,
        timeout=60,
        env_file="urls.env",
        no_env_file=True,
    )


def test_main_stop_on_error(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "run_cmd", lambda *a, **k: (False, "", "step failed"))
    monkeypatch.setattr(full.argparse.ArgumentParser, "parse_args", lambda self: _base_args(tmp_path))
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code != 0


def test_append_debug_and_report(tmp_path):
    log = tmp_path / "debug.log"
    full._append_debug(str(log), "step1", stdout="ok", stderr="", ok=True)
    full._append_debug(str(log), "step2", skip_reason="skipped")
    lines = log.read_text(encoding="utf-8")
    assert "step1" in lines
    assert "SKIP" in lines
    report = tmp_path / "report.txt"
    full._write_run_report(["line1", "line2"], str(report), str(report))
    assert "line1" in report.read_text(encoding="utf-8")
