#!/usr/bin/env python3
"""Read-only Zabbix uplinks plan: export current objects and planned changes."""

import argparse
import os
import sys

from env_urls import load_env_file_if_present
from uplinks.zabbix.plan import build_zabbix_plan, write_plan_outputs

load_env_file_if_present()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only Zabbix uplinks plan (no create/update/delete in Zabbix or NetBox)."
    )
    parser.add_argument(
        "-d",
        "--dry-ssh",
        default="dry-ssh.json",
        metavar="FILE",
        help="dry-ssh.json path (default: dry-ssh.json)",
    )
    parser.add_argument(
        "--inventory-file",
        default=None,
        metavar="FILE",
        help="Scoped inventory JSON from netbox_uplinks_inventory.py --json (skip NetBox walk)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="FILE",
        help="Write machine-readable JSON plan to FILE",
    )
    parser.add_argument(
        "--text",
        default=None,
        metavar="FILE",
        help="Write concise text summary to FILE",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON plan to stdout")
    parser.add_argument("--debug", action="store_true", help="Verbose stderr diagnostics")
    parser.add_argument(
        "--create-link-triggers",
        action="store_true",
        help="Include Burst link triggers section (still read-only; may be not_evaluated)",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.dry_ssh):
        print("dry-ssh file not found: {}".format(args.dry_ssh), file=sys.stderr)
        return 1

    report, err = build_zabbix_plan(
        args.dry_ssh,
        debug=args.debug,
        create_link_triggers=args.create_link_triggers,
        inventory_file=args.inventory_file,
    )
    if err:
        print(err, file=sys.stderr)
        return 1

    write_plan_outputs(
        report,
        json_path=args.output,
        text_path=args.text,
        print_json=args.json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
