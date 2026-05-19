"""generate_commit_rates error paths."""

import json
import sys
from pathlib import Path

import pytest

from generate_commit_rates import load_json, main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_load_json_invalid(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    data, err = load_json(str(bad))
    assert data is None
    assert err


def test_main_json_error_in_dry_ssh(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["generate_commit_rates.py", "-f", str(bad), "--no-merge"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def test_main_merge_json_error(tmp_path, monkeypatch):
    dry = FIXTURES / "dry_ssh_minimal.json"
    out = tmp_path / "out.json"
    out.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["generate_commit_rates.py", "-f", str(dry), "-o", str(out)],
    )
    with pytest.raises(SystemExit):
        main()
