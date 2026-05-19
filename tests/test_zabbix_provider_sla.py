"""Tests for zabbix_provider_sla.py SLA calculation."""

from zabbix_provider_sla import _compute_sla_from_events, _unix_ts


def test_compute_sla_no_events():
    total, problem = _compute_sla_from_events([], 0, 1000)
    assert total == 1000
    assert problem == 0


def test_compute_sla_single_problem_window():
    # OK until 100, PROBLEM 100-400, OK after
    events = [(100, 1), (400, 0)]
    total, problem = _compute_sla_from_events(events, 0, 1000)
    assert total == 1000
    assert problem == 300


def test_compute_sla_problem_until_end():
    events = [(200, 1)]
    total, problem = _compute_sla_from_events(events, 0, 1000)
    assert problem == 800


def test_unix_ts_int_passthrough():
    assert _unix_ts(1700000000) == 1700000000
