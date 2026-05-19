"""run_uplinks_full step 1 failure and write error paths."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full


def _ns(**overrides):
    base = dict(
        no_fetch=False,
        from_file=None,
        refresh=True,
        dry_ssh="dry-ssh.json",
        commit_rates="commit_rates.json",
        no_netbox_apply=True,
        no_burst_triggers=False,
        location=None,
        stop_on_error=True,
        no_stop_on_error=False,
        report=None,
        timeout=60,
        env_file="urls.env",
        no_env_file=True,
    )
    base.update(overrides)
    return full.argparse.Namespace(**base)


def test_main_fetch_failure_stops(monkeypatch, tmp_path):
    dry = tmp_path / "dry-ssh.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "cr.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "desc.json")
    monkeypatch.setattr(full, "run_cmd", lambda *a, **k: (False, "", "ssh failed"))
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _ns(),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 1


def test_main_from_file_missing_dry_ssh(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(full, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(full, "RUN_LOGS_DIR", "run_logs")
    monkeypatch.setattr(full, "DEFAULT_DRY_SSH", "dry-ssh.json")
    monkeypatch.setattr(full, "DEFAULT_COMMIT_RATES", "cr.json")
    monkeypatch.setattr(full, "DEFAULT_DESC_MAP", "desc.json")
    monkeypatch.setattr(
        full.argparse.ArgumentParser,
        "parse_args",
        lambda self: _ns(no_fetch=True, from_file=True, stop_on_error=True),
    )
    with pytest.raises(SystemExit) as exc:
        full.main()
    assert exc.value.code == 1
