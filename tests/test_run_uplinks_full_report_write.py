"""run_uplinks_full _write_run_report and _append_debug errors."""

from run_uplinks_full import _append_debug, _write_run_report


def test_write_run_report_io_error(tmp_path, capsys):
    bad_path = str(tmp_path / "nosuch" / "deep" / "run.log")
    _write_run_report(["line"], bad_path, None)
    _write_run_report(["line"], None, bad_path)


def test_append_debug_io_error(tmp_path, capsys):
    _append_debug(str(tmp_path / "nosuch" / "d.log"), "step", stdout="x", ok=True)
