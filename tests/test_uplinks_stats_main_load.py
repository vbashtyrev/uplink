"""uplinks_stats main default load-from-file mode."""

import json
import sys
from pathlib import Path

import uplinks_stats as us

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_loads_stats_file_json(monkeypatch, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps({"devices": {"H1": [{"name": "Eth1", "description": "Uplink: X"}]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--from-file", str(stats), "--json"])
    assert us.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert "H1" in out["devices"]


def test_main_load_missing_file(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--from-file", "/no/such/stats.json"])
    assert us.main() == 1
