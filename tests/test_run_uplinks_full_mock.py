"""run_uplinks_full.py step orchestration with mocked subprocess steps."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run_uplinks_full as full


def test_run_no_fetch_human_mode(monkeypatch, tmp_path):
    """Default human mode: inventory read-only, no generate/create."""
    monkeypatch.chdir(tmp_path)
    commit_rates = tmp_path / 'commit_rates.json'
    commit_rates.write_text('{"_comment": "t"}', encoding='utf-8')
    calls = []

    def fake_run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
        calls.append(argv[1] if len(argv) > 1 else str(argv))
        if 'netbox_uplinks_inventory.py' in str(argv) and capture_stdout_to_file:
            Path(capture_stdout_to_file).write_text(
                json.dumps(
                    {
                        'complete': [
                            {'device': 'ALA-KZT-7280TR-1', 'interface': 'Eth1', 'provider': 'P'}
                        ],
                        'incomplete': [],
                        'stats': {'complete': 1},
                    }
                ),
                encoding='utf-8',
            )
        return True, 'ok', ''

    monkeypatch.setattr(full, 'SCRIPT_DIR', str(tmp_path))
    monkeypatch.setattr(full, 'RUN_LOGS_DIR', 'run_logs')
    monkeypatch.setattr(full, 'DEFAULT_NETBOX_INVENTORY', 'netbox_inventory.json')
    monkeypatch.setattr(full, 'run_cmd', fake_run_cmd)

    with patch.object(full.argparse.ArgumentParser, 'parse_args') as mock_parse:
        mock_parse.return_value = full.argparse.Namespace(
            plan=False,
            no_burst_triggers=False,
            stop_on_error=True,
            report=None,
            timeout=60,
            env_file='urls.env',
            no_env_file=True,
        )
        with pytest.raises(SystemExit) as exc:
            full.main()
        assert exc.value.code == 0

    scripts = [c for c in calls if c.endswith('.py')]
    assert scripts == [
        'netbox_uplinks_inventory.py',
        'zabbix_sync_commit_rate.py',
        'zabbix_provider_aggregate.py',
        'zabbix_map.py',
        'zabbix_uplinks_dashboard.py',
        'zabbix_provider_services.py',
    ]
