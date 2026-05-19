"""generate_commit_rates merge with _provider_limits and kbps conversion."""

import json
import sys
from pathlib import Path

from generate_commit_rates import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_merge_preserves_meta_and_kbps(tmp_path, monkeypatch, capsys):
    dry = tmp_path / "dry.json"
    dry.write_text((FIXTURES / "dry_ssh_minimal.json").read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "commit_rates.json"
    out.write_text(
        json.dumps(
            {
                "_provider_limits": {"Cogent": 10},
                "_provider_sla": 99.9,
                "_custom_meta": "keep",
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "Old",
                        "circuit_id": "OLD-1",
                        "commit_rate_kbps": 5_000_000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    desc = tmp_path / "desc.json"
    desc.write_text('{"Uplink: Cogent 10G": "Cogent"}', encoding="utf-8")
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
    assert data["_custom_meta"] == "keep"
    entry = data["ALA-KZT-7280TR-1"]["Ethernet51/1"]
    assert entry["commit_rate_gbps"] == 5.0
