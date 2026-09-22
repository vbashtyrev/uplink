#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the full uplinks pipeline: devices → NetBox → Zabbix with reporting."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

from env_urls import load_env_file
from uplinks.netbox.inventory import format_inventory_text
from uplinks.zabbix.plan import inventory_plan_gate

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DRY_SSH = "dry-ssh.json"
DEFAULT_NETBOX_INVENTORY = "netbox_inventory.json"
RUN_LOGS_DIR = "run_logs"  # log folder: date_time_run.log and date_time_debug.log


def _plan_mode(args):
    """True when --plan is set; safe for Namespace objects built without plan (tests, callers)."""
    return getattr(args, "plan", False)


def run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
    """
    Run the command. argv is a list of [python, 'script.py', ...].
    Return (success: bool, stdout: str, stderr: str).
    If capture_stdout_to_file is specified, the command's stdout is written to a file (and the return is left as an empty string).
    """
    env = env or os.environ
    try:
        if capture_stdout_to_file:
            with open(capture_stdout_to_file, "w", encoding="utf-8") as f:
                r = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                    stdout=f,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            stderr = (r.stderr or "").strip()
            return (r.returncode == 0, "", stderr)
        else:
            r = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
            stdout = (r.stdout or "").strip()
            stderr = (r.stderr or "").strip()
            return (r.returncode == 0, stdout, stderr)
    except subprocess.TimeoutExpired:
        return (False, "", "Execution timeout ({} s)".format(timeout))
    except FileNotFoundError as e:
        return (False, "", "Executable file or script not found: {}".format(e))
    except Exception as e:
        return (False, "", str(e))


def _append_debug(debug_log_path, step_name, stdout="", stderr="", ok=None, skip_reason=None):
    """Add a block to the debug log step by step: either SKIP or stdout/stderr/ok."""
    if not debug_log_path:
        return
    try:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write("=== {} ===\n".format(step_name))
            if skip_reason is not None:
                f.write("SKIP: {}\n\n".format(skip_reason))
            else:
                f.write("stdout:\n{}\n\nstderr:\n{}\n\nok: {}\n\n".format(
                    stdout if stdout else "(empty)",
                    stderr if stderr else "(empty)",
                    ok,
                ))
    except Exception as e:
        print("Failed to add {} to the debug log: {}".format(debug_log_path, e), file=sys.stderr)


def _write_run_report(report_lines, run_log_path, report_file, log_func=None):
    """Write a report to run_log_path and, if necessary, to report_file. log_func(msg) - optional output to the console."""
    if run_log_path:
        try:
            with open(run_log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(report_lines))
            if log_func:
                log_func("Run log: {}".format(run_log_path))
        except Exception as e:
            print("Failed to write log to {}: {}".format(run_log_path, e), file=sys.stderr)
    if report_file:
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                f.write("\n".join(report_lines))
            if log_func:
                log_func("The report is written in {}".format(report_file))
        except Exception as e:
            print("Failed to write report to {}: {}".format(report_file, e), file=sys.stderr)


def _load_inventory_report(path):
    """Load inventory JSON written by netbox_uplinks_inventory.py --json."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _log_inventory_incomplete(log, report):
    """Log incomplete inventory rows without failing the run."""
    incomplete = report.get("incomplete") or []
    if not incomplete:
        return
    log("  Incomplete inventory entries ({}):".format(len(incomplete)))
    for row in incomplete:
        extra = []
        if row.get("device"):
            extra.append("device={}".format(row["device"]))
        if row.get("interface"):
            extra.append("interface={}".format(row["interface"]))
        suffix = (" " + " ".join(extra)) if extra else ""
        log(
            "  INCOMPLETE provider={} circuit={} reason={} ({}){}".format(
                row.get("provider") or "?",
                row.get("circuit_id") or "?",
                row.get("reason") or "?",
                row.get("reason_label") or row.get("reason") or "?",
                suffix,
            )
        )


def _run_plan_inventory(python, inventory_path, timeout, log, step, debug_log_path, report_lines, errors, report_file, run_log_path):
    """Plan mode: scoped read-only inventory; fail closed on auth, empty, or incomplete."""
    log(
        "Step 0: NetBox uplink inventory (scoped, read-only) -> {} ...".format(
            os.path.basename(inventory_path)
        )
    )
    ok, out, err = run_cmd(
        [python, "netbox_uplinks_inventory.py", "--json", "--dry-run"],
        cwd=SCRIPT_DIR,
        timeout=timeout,
        capture_stdout_to_file=inventory_path,
    )
    _append_debug(
        debug_log_path,
        "Step 0: NetBox inventory (plan)",
        stdout=out or "",
        stderr=err or "",
        ok=ok,
    )
    if not os.path.isfile(inventory_path):
        detail = err or out or "inventory file was not created"
        step("Step 0: NetBox inventory (plan)", False, detail)
        _finish(report_lines, errors, report_file, run_log_path)
        sys.exit(1)

    try:
        inventory_report = _load_inventory_report(inventory_path)
    except (OSError, json.JSONDecodeError) as e:
        step("Step 0: NetBox inventory (plan)", False, "failed to read {}: {}".format(inventory_path, e))
        _finish(report_lines, errors, report_file, run_log_path)
        sys.exit(1)

    inv_ok, inv_detail = inventory_plan_gate(inventory_report)
    complete_count = len(inventory_report.get("complete") or [])
    incomplete_count = len(inventory_report.get("incomplete") or [])
    if inv_ok:
        step(
            "Step 0: NetBox inventory (plan)",
            True,
            "complete={}, incomplete={} -> {}".format(
                complete_count, incomplete_count, os.path.basename(inventory_path)
            ),
        )
    else:
        _log_inventory_incomplete(log, inventory_report)
        step("Step 0: NetBox inventory (plan)", False, inv_detail)
        _finish(report_lines, errors, report_file, run_log_path)
        sys.exit(1)
    log("")
    return inventory_report


def main():
    parser = argparse.ArgumentParser(
        description="Full uplinks chain: NetBox inventory → Zabbix (sync, map, dashboards, services).",
    )
    parser.add_argument(
        "--dry-ssh",
        default=DEFAULT_DRY_SSH,
        metavar="FILE",
        help="Path to dry-ssh.json for optional --netbox-checks step (default {})".format(DEFAULT_DRY_SSH),
    )
    parser.add_argument(
        "--no-netbox-apply",
        action="store_true",
        help="Skip optional netbox_checks.py; it is not part of the default path.",
    )
    parser.add_argument(
        "--no-burst-triggers",
        action="store_true",
        help="Do not pass --create-link-triggers to zabbix_sync_commit_rate.py (Burst per-link 90%%/100%%/SLA)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        default=True,
        help="Stop at first error (default)",
    )
    parser.add_argument(
        "--no-stop-on-error",
        action="store_false",
        dest="stop_on_error",
        help="Continue execution if step fails",
    )
    parser.add_argument(
        "--report",
        default=None,
        metavar="FILE",
        help="Additionally write the report to a file",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        metavar="SEC",
        help="Timeout for one step in seconds (default 600)",
    )
    parser.add_argument(
        "--env-file",
        default="urls.env",
        metavar="FILE",
        help="Environment variable file KEY=VALUE (default urls.env)",
    )
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="Don't load the env file before starting the chain",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Read-only preview: scoped NetBox inventory and Zabbix plan report",
    )
    parser.add_argument(
        "--netbox-checks",
        action="store_true",
        help="Run netbox_checks.py (optional; not part of default human path)",
    )
    args = parser.parse_args()

    os.chdir(SCRIPT_DIR)
    env_file_path = os.path.join(SCRIPT_DIR, args.env_file)
    loaded_env_count = 0
    if not args.no_env_file:
        loaded_env_count = load_env_file(env_file_path, overwrite=True)
    python = sys.executable
    timeout = args.timeout
    dry_ssh_path = args.dry_ssh

    # Log folder: run_logs/YYYY-MM-DD_HH-MM-SS_run.log
    logs_dir = os.path.join(SCRIPT_DIR, RUN_LOGS_DIR)
    os.makedirs(logs_dir, exist_ok=True)
    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_log_path = os.path.join(logs_dir, "{}_run.log".format(run_ts))
    debug_log_path = os.path.join(logs_dir, "{}_debug.log".format(run_ts))

    report_lines = []
    errors = []

    def log(msg):
        report_lines.append(msg)
        print(msg)

    def step(name, success, detail=""):
        if success:
            log("[OK] {} {}".format(name, detail).strip())
        else:
            log("[FAIL] {} {}".format(name, detail).strip())
            errors.append((name, detail))

    log("=== Uplinks full run {} ===".format(datetime.now().isoformat(timespec="seconds")))
    log("Working directory: {}".format(SCRIPT_DIR))
    if args.no_env_file:
        log("Env file: [SKIP] (--no-env-file)")
    elif os.path.isfile(env_file_path):
        log("Env file: {} (loaded {} variables)".format(env_file_path, loaded_env_count))
    else:
        log("Env file: {} (not found, current environment is used)".format(env_file_path))
    log("Run log: {}".format(run_log_path))
    log("Debug log: {}".format(debug_log_path))
    if _plan_mode(args):
        mode_label = "plan (read-only NetBox + Zabbix preview)"
    else:
        mode_label = "human (NetBox-first, read-only inventory)"
    log("Mode: {}".format(mode_label))
    log("")
    try:
        with open(debug_log_path, "w", encoding="utf-8") as f:
            f.write("=== Debug log {} ===\n\n".format(datetime.now().isoformat(timespec="seconds")))
    except Exception as e:
        print("Failed to create debug log {}: {}".format(debug_log_path, e), file=sys.stderr)
        debug_log_path = None

    inventory_path = os.path.join(SCRIPT_DIR, DEFAULT_NETBOX_INVENTORY)
    plan_json_path = os.path.join(logs_dir, "{}_zabbix_plan.json".format(run_ts))

    plan_inventory_report = _run_plan_inventory(
        python,
        inventory_path,
        timeout,
        log,
        step,
        debug_log_path,
        report_lines,
        errors,
        args.report,
        run_log_path,
    )

    _append_debug(
        debug_log_path,
        "Step 1: Data collection",
        skip_reason="NetBox-first path (inventory drives Zabbix; no SSH fetch)",
    )
    log("[SKIP] Step 1: Data collection (NetBox inventory drives Zabbix)")
    report_lines.append("")

    run_netbox_checks = getattr(args, "netbox_checks", False)
    if run_netbox_checks and (_plan_mode(args) or not args.no_netbox_apply):
        netbox_checks_argv = [
            python,
            "netbox_checks.py",
            "-f",
            dry_ssh_path,
            "--all",
            "--no-tx-power",
            "--mt-ref",
            "--scope-to-file",
        ]
        if _plan_mode(args):
            netbox_checks_argv.append("--existing-only")
            netbox_checks_mode = "--existing-only (preview)"
        else:
            netbox_checks_argv.append("--existing-only")
            netbox_checks_argv.append("--apply")
            netbox_checks_mode = "--existing-only --apply"
        if _plan_mode(args):
            log(
                "Step 2: NetBox - read-only preview (netbox_checks.py -f {} --all --no-tx-power --mt-ref --scope-to-file --existing-only) ...".format(
                    dry_ssh_path
                )
            )
        else:
            log(
                "Step 2: NetBox - reconciliation and application (netbox_checks.py -f {} --all --no-tx-power --mt-ref --scope-to-file {}) ...".format(
                    dry_ssh_path, netbox_checks_mode
                )
            )
        ok, out, err = run_cmd(
            netbox_checks_argv,
            cwd=SCRIPT_DIR,
            timeout=timeout,
        )
        step_name = "Step 2: NetBox checks (preview)" if _plan_mode(args) else "Step 2: NetBox checks --apply"
        _append_debug(debug_log_path, step_name, stdout=out or "", stderr=err or "", ok=ok)
        step(step_name, ok, err or out or ("code != 0" if not ok else ""))
        if not ok and args.stop_on_error:
            _finish(report_lines, errors, args.report, run_log_path)
            sys.exit(1)
        log("")
    else:
        skip_reason = "--no-netbox-apply" if args.no_netbox_apply else "not requested (default human path)"
        _append_debug(debug_log_path, "Step 2: NetBox checks", skip_reason=skip_reason)
        log("[SKIP] Step 2: NetBox checks ({})".format(skip_reason))
        report_lines.append("")

    if _plan_mode(args):
        log("Step 3: NetBox uplink inventory summary (from step 0, no new NetBox read) ...")
        inv_summary = format_inventory_text(plan_inventory_report, dry_run=True)
        _append_debug(
            debug_log_path,
            "Step 3: NetBox uplink inventory (plan)",
            stdout=inv_summary or "",
            stderr="",
            ok=True,
        )
        step("Step 3: NetBox uplink inventory (plan)", True, "")
        if inv_summary:
            for line in inv_summary.splitlines():
                log("  {}".format(line))
        log("")

        plan_argv = [
            python,
            "zabbix_uplinks_plan.py",
            "--inventory-file",
            inventory_path,
            "-o",
            plan_json_path,
            "--text",
            plan_json_path.replace(".json", ".txt"),
        ]
        if not args.no_burst_triggers:
            plan_argv.append("--create-link-triggers")
        log("Step 4: Zabbix plan (read-only) -> {} ...".format(os.path.basename(plan_json_path)))
        ok, out, err = run_cmd(plan_argv, cwd=SCRIPT_DIR, timeout=timeout)
        _append_debug(debug_log_path, "Step 4: Zabbix plan", stdout=out or "", stderr=err or "", ok=ok)
        step("Step 4: Zabbix plan", ok, err or out or ("code != 0" if not ok else ""))
        if not ok and args.stop_on_error:
            _finish(report_lines, errors, args.report, run_log_path)
            sys.exit(1)
        if out:
            for line in out.splitlines():
                log("  {}".format(line))
        log("Zabbix plan JSON: {}".format(plan_json_path))
        log("")

        log("--- Total ---")
        if errors:
            log("Errors ({}):".format(len(errors)))
            for name, detail in errors:
                log(" {}: {}".format(name, (detail or "").strip() or "return code != 0"))
            _write_run_report(report_lines, run_log_path, args.report, log_func=log)
            sys.exit(1)
        log("Plan completed successfully (no NetBox or Zabbix writes).")
        _write_run_report(report_lines, run_log_path, args.report, log_func=log)
        sys.exit(0)

    log("Step 3: NetBox uplink inventory summary (from step 0) ...")
    try:
        inv_summary = format_inventory_text(
            _load_inventory_report(inventory_path), dry_run=True
        )
    except (OSError, json.JSONDecodeError) as e:
        step("Step 3: NetBox uplink inventory", False, "failed to read {}: {}".format(inventory_path, e))
        if args.stop_on_error:
            _finish(report_lines, errors, args.report, run_log_path)
            sys.exit(1)
        inv_summary = ""
    else:
        step("Step 3: NetBox uplink inventory", True, "")
        if inv_summary:
            for line in inv_summary.splitlines():
                log("  {}".format(line))
    log("")

    sync_argv = [python, "zabbix_sync_commit_rate.py", "--inventory-file", inventory_path]
    if not args.no_burst_triggers:
        sync_argv.append("--create-link-triggers")
    sync_detail = " ".join(sync_argv[1:])
    log("Step 5: Zabbix - macros and triggers ({}) ...".format(sync_detail))
    ok, out, err = run_cmd(sync_argv, cwd=SCRIPT_DIR, timeout=timeout)
    _append_debug(debug_log_path, "Step 5: Zabbix sync commit rate", stdout=out or "", stderr=err or "", ok=ok)
    step("Step 5: Zabbix sync commit rate", ok, err or ("code != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    if out:
        for line in out.splitlines():
            log("  {}".format(line))
    log("")

    agg_argv = [python, "zabbix_provider_aggregate.py", "--inventory-file", inventory_path]
    log(
        "Step 6: Zabbix - aggregate by provider ({}) ...".format(" ".join(agg_argv[1:]))
    )
    ok, out, err = run_cmd(
        agg_argv,
        cwd=SCRIPT_DIR,
        timeout=timeout,
    )
    _append_debug(debug_log_path, "Step 6: Zabbix provider aggregate", stdout=out or "", stderr=err or "", ok=ok)
    step("Step 6: Zabbix provider aggregate", ok, err or ("code != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    if out:
        for line in out.splitlines():
            log("  {}".format(line))
    log("")

    map_argv = [
        python,
        "zabbix_map.py",
        "--inventory-file",
        inventory_path,
        "--zabbix",
        "--update-map",
    ]
    map_log = "Step 7: Zabbix - map (zabbix_map.py --inventory-file {} --zabbix --update-map) ...".format(
        os.path.basename(inventory_path)
    )
    log(map_log)
    ok, out, err = run_cmd(map_argv, cwd=SCRIPT_DIR, timeout=timeout)
    _append_debug(debug_log_path, "Step 7: Zabbix map", stdout=out or "", stderr=err or "", ok=ok)
    step("Step 7: Zabbix map", ok, err or ("code != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    if out:
        for line in out.splitlines():
            log("  {}".format(line))
    log("")

    dash_argv = [python, "zabbix_uplinks_dashboard.py", "--inventory-file", inventory_path]
    dash_log = "Step 8: Zabbix - dashboards (zabbix_uplinks_dashboard.py --inventory-file {}) ...".format(
        os.path.basename(inventory_path)
    )
    log(dash_log)
    ok, out, err = run_cmd(dash_argv, cwd=SCRIPT_DIR, timeout=timeout)
    _append_debug(debug_log_path, "Step 8: Zabbix dashboard", stdout=out or "", stderr=err or "", ok=ok)
    step("Step 8: Zabbix dashboard", ok, err or ("code != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    log("")

    services_argv = [
        python,
        "zabbix_provider_services.py",
        "--parent-service",
        "Uplinks providers",
    ]
    log(
        "Step 9: Zabbix - services and SLAs by providers ({}) ...".format(
            " ".join(services_argv[1:])
        )
    )
    ok, out, err = run_cmd(
        services_argv,
        cwd=SCRIPT_DIR,
        timeout=timeout,
    )
    _append_debug(debug_log_path, "Step 9: Zabbix provider services/SLA", stdout=out or "", stderr=err or "", ok=ok)
    step("Step 9: Zabbix provider services/SLA", ok, err or ("code != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    if out:
        for line in out.splitlines():
            log("  {}".format(line))
    log("")

    log("--- Total ---")
    if errors:
        log("Errors ({}):".format(len(errors)))
        for name, detail in errors:
            log(" {}: {}".format(name, (detail or "").strip() or "return code != 0"))
        _write_run_report(report_lines, run_log_path, args.report, log_func=log)
        sys.exit(1)
    else:
        log("All steps completed successfully.")
        _write_run_report(report_lines, run_log_path, args.report, log_func=log)
        sys.exit(0)


def _finish(report_lines, errors, report_file, run_log_path=None):
    """Summarize when stopping early and write a report to run_logs and, if necessary, to report_file."""
    summary = ["", "--- Summary (stopped due to error) ---", "Errors:"]
    for name, detail in errors:
        summary.append(" {}: {}".format(name, (detail or "").strip() or "return code != 0"))
    report_lines.extend(summary)
    for line in summary:
        print(line)
    _write_run_report(report_lines, run_log_path, report_file, log_func=print)


if __name__ == "__main__":
    main()
