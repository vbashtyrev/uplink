"""process_one_device_stats routing and print_table."""

from unittest.mock import MagicMock

import uplinks_stats as us


def test_process_one_device_stats_unknown_platform(capsys):
    dev = MagicMock()
    dev.name = "SW1"
    logs = []

    name, payload = us.process_one_device_stats(
        dev, None, "u", "p", ".example.com", lambda d, m: logs.append(m)
    )
    assert name == "SW1"
    assert payload is None
    assert any("pass" in x for x in logs)


def test_print_table_error_and_empty(capsys):
    us.print_table({})
    assert "No data" in capsys.readouterr().out
    us.print_table({"R1": {"error": "ssh failed"}})
    assert "ssh failed" in capsys.readouterr().out
