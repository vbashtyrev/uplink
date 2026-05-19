"""zabbix_map main: --export-map and --create-map only."""

import json
import sys

import pytest

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from zabbix_map import main as map_main


def test_main_export_map(monkeypatch, zabbix_env, capsys):
    mocker = build_standard_zabbix_mocker()
    mocker.on(
        "map.get",
        lambda p: [{"sysmapid": "9", "name": "Uplinks", "selements": [], "links": []}],
    ).activate(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["zabbix_map.py", "--export-map", "9"])
    with pytest.raises(SystemExit) as exc:
        map_main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert json.loads(out)[0]["sysmapid"] == "9"


def test_main_create_map_only(monkeypatch, zabbix_env, capsys):
    build_standard_zabbix_mocker().on("map.get", lambda p: []).on(
        "map.create", lambda p: {"sysmapids": ["77"]}
    ).activate(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["zabbix_map.py", "--create-map"])
    with pytest.raises(SystemExit) as exc:
        map_main()
    assert exc.value.code == 0
    err = capsys.readouterr().err
    assert "77" in err or "created" in err.lower()
