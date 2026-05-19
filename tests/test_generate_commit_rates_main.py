"""Additional generate_commit_rates.main() coverage (merge, errors)."""

import json
import sys
from pathlib import Path

import pytest

from generate_commit_rates import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_merge_existing(tmp_path, monkeypatch, capsys):
    dry = FIXTURES / "dry_ssh_minimal.json"
    desc = tmp_path / "desc.json"
    desc.write_text(json.dumps({"Uplink: Cogent 10G": "Cogent"}), encoding="utf-8")
    out = tmp_path / "commit_rates.json"
    out.write_text(
        json.dumps(
            {
                "_provider_limits": {"Cogent": 10},
                "_provider_sla": 99.9,
                "_custom": "keep",
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "Old",
                        "commit_rate_gbps": 10,
                        "commit_rate_kbps": 10_000_000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_commit_rates.py",
            "-f",
            str(dry),
            "-m",
            str(desc),
            "-o",
            str(out),
        ],
    )
    main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["_provider_limits"]["Cogent"] == 10
    assert data["_provider_sla"] == 99.9
    assert data["_custom"] == "keep"
    entry = data["ALA-KZT-7280TR-1"]["Ethernet51/1"]
    assert entry["commit_rate_gbps"] == 10


def test_main_missing_devices_key(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["generate_commit_rates.py", "-f", str(bad), "--no-merge"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
