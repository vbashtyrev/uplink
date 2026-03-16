#!/usr/bin/env python3
"""Cleanup NetBox objects created by uplinks automation (by NETBOX_AUTOMATION_TAG)."""

import argparse
import os
import sys

import pynetbox

from uplinks_config import NETBOX_AUTOMATION_TAG as AUTOMATION_TAG


def _get_nb():
    """Connect to NetBox or exit if NETBOX_URL/NETBOX_TOKEN are missing."""
    url = os.environ.get("NETBOX_URL", "").strip().rstrip("/")
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not url or not token:
        print("NETBOX_URL and NETBOX_TOKEN must be set", file=sys.stderr)
        sys.exit(1)
    return pynetbox.api(url, token=token)


def _get_automation_tag(nb):
    """Return NetBox tag object for AUTOMATION_TAG or None."""
    if not AUTOMATION_TAG:
        return None
    try:
        tag = nb.extras.tags.get(name=AUTOMATION_TAG) or nb.extras.tags.get(slug=AUTOMATION_TAG)
        return tag
    except Exception:
        return None


def cleanup_cables(nb, tag_slug, dry_run=False, debug=False):
    """Delete cables with given tag_slug. Return number of deleted records."""
    if not tag_slug:
        return 0
    try:
        cables = list(nb.dcim.cables.filter(tag=tag_slug))
    except Exception as e:
        if debug:
            print("cables.filter: {}".format(e), file=sys.stderr)
        return 0
    ids = [c.id for c in cables if getattr(c, "id", None) is not None]
    if not ids:
        return 0
    if dry_run:
        print("dry-run: cables.delete {} (tag={})".format(len(ids), tag_slug))
        return len(ids)
    try:
        nb.dcim.cables.delete(ids)
        return len(ids)
    except Exception as e:
        print("cables.delete error: {}".format(e), file=sys.stderr)
        return 0


def cleanup_circuit_terminations(nb, circuit_ids, dry_run=False, debug=False):
    """Delete circuit terminations for circuits in circuit_ids. Return number deleted."""
    if not circuit_ids:
        return 0
    to_delete = []
    for cid in circuit_ids:
        try:
            terms = list(nb.circuits.circuit_terminations.filter(circuit_id=cid))
            for t in terms:
                tid = getattr(t, "id", None)
                if tid is not None:
                    to_delete.append(tid)
        except Exception as e:
            if debug:
                print("circuit_terminations.filter circuit_id={}: {}".format(cid, e), file=sys.stderr)
    if not to_delete:
        return 0
    if dry_run:
        print("dry-run: circuit_terminations.delete {} (для {} circuits)".format(len(to_delete), len(circuit_ids)))
        return len(to_delete)
    deleted = 0
    for tid in to_delete:
        try:
            nb.circuits.circuit_terminations.delete([tid])
            deleted += 1
        except Exception as e:
            if debug:
                print("circuit_termination.delete {}: {}".format(tid, e), file=sys.stderr)
    return deleted


def cleanup_circuits(nb, tag_slug, dry_run=False, debug=False):
    """Delete circuits with given tag_slug. Return (deleted_count, circuit_ids_before)."""
    if not tag_slug:
        return 0, []
    try:
        circuits = list(nb.circuits.circuits.filter(tag=tag_slug))
    except Exception as e:
        if debug:
            print("circuits.filter: {}".format(e), file=sys.stderr)
        return 0, []
    ids = [c.id for c in circuits if getattr(c, "id", None) is not None]
    if not ids:
        return 0, []
    if dry_run:
        print("dry-run: circuits.delete {} (tag={})".format(len(ids), tag_slug))
        return len(ids), ids
    deleted = 0
    for cid in ids:
        try:
            nb.circuits.circuits.delete([cid])
            deleted += 1
        except Exception as e:
            if debug:
                print("circuits.delete {}: {}".format(cid, e), file=sys.stderr)
    return deleted, ids


def cleanup_circuit_types(nb, tag_slug, dry_run=False, debug=False):
    """Delete circuit types with given tag_slug that have no circuits. Return count."""
    if not tag_slug:
        return 0
    try:
        ct_list = list(nb.circuits.circuit_types.filter(tag=tag_slug))
    except Exception as e:
        if debug:
            print("circuit_types.filter: {}".format(e), file=sys.stderr)
        return 0
    deleted = 0
    for ct in ct_list:
        cid = getattr(ct, "id", None)
        if cid is None:
            continue
        try:
            circuits_using = list(nb.circuits.circuits.filter(type_id=cid))
            if circuits_using:
                if debug:
                    print("circuit_type id={} ({}): пропуск, контуров: {}".format(
                        cid, getattr(ct, "name", ""), len(circuits_using)), file=sys.stderr)
                continue
        except Exception as e:
            if debug:
                print("circuits.filter type_id={}: {}".format(cid, e), file=sys.stderr)
            continue
        if dry_run:
            print("dry-run: circuit_types.delete 1 (id={}, name={})".format(cid, getattr(ct, "name", "")))
            deleted += 1
        else:
            try:
                nb.circuits.circuit_types.delete([cid])
                deleted += 1
            except Exception as e:
                if debug:
                    print("circuit_types.delete {}: {}".format(cid, e), file=sys.stderr)
    return deleted


def cleanup_providers(nb, tag_slug, dry_run=False, debug=False):
    """Delete providers with given tag_slug that have no circuits. Return count."""
    if not tag_slug:
        return 0
    try:
        prov_list = list(nb.circuits.providers.filter(tag=tag_slug))
    except Exception as e:
        if debug:
            print("providers.filter: {}".format(e), file=sys.stderr)
        return 0
    deleted = 0
    for p in prov_list:
        pid = getattr(p, "id", None)
        if pid is None:
            continue
        try:
            circuits_using = list(nb.circuits.circuits.filter(provider_id=pid))
            if circuits_using:
                if debug:
                    print("provider id={} ({}): пропуск, контуров: {}".format(
                        pid, getattr(p, "name", ""), len(circuits_using)), file=sys.stderr)
                continue
        except Exception as e:
            if debug:
                print("circuits.filter provider_id={}: {}".format(pid, e), file=sys.stderr)
            continue
        if dry_run:
            print("dry-run: providers.delete 1 (id={}, name={})".format(pid, getattr(p, "name", "")))
            deleted += 1
        else:
            try:
                nb.circuits.providers.delete([pid])
                deleted += 1
            except Exception as e:
                if debug:
                    print("providers.delete {}: {}".format(pid, e), file=sys.stderr)
    return deleted


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Delete NetBox objects created by uplinks automation "
            "(tag from uplinks_config.NETBOX_AUTOMATION_TAG): cables, "
            "circuit terminations, circuits and, when safe, circuit types and providers."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without changing NetBox",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose debug output",
    )
    args = parser.parse_args()

    if not AUTOMATION_TAG:
        print("NETBOX_AUTOMATION_TAG (or TRIGGER_TAG_VALUE) is not set in uplinks_config.", file=sys.stderr)
        sys.exit(1)

    nb = _get_nb()
    tag_obj = _get_automation_tag(nb)
    if tag_obj is None:
        print("Tag '{}' not found in NetBox. Nothing to delete.".format(AUTOMATION_TAG), file=sys.stderr)
        sys.exit(0)
    tag_slug = getattr(tag_obj, "slug", None) or AUTOMATION_TAG.lower().replace(" ", "-")[:50]

    n_cables = cleanup_cables(nb, tag_slug, dry_run=args.dry_run, debug=args.debug)

    try:
        circuits_with_tag = list(nb.circuits.circuits.filter(tag=tag_slug))
    except Exception:
        circuits_with_tag = []
    circuit_ids_for_terms = [c.id for c in circuits_with_tag if getattr(c, "id", None) is not None]

    n_terms = cleanup_circuit_terminations(nb, circuit_ids_for_terms, dry_run=args.dry_run, debug=args.debug)

    n_circuits, _ = cleanup_circuits(nb, tag_slug, dry_run=args.dry_run, debug=args.debug)

    n_types = cleanup_circuit_types(nb, tag_slug, dry_run=args.dry_run, debug=args.debug)
    n_providers = cleanup_providers(nb, tag_slug, dry_run=args.dry_run, debug=args.debug)

    mode = "dry-run" if args.dry_run else "done"
    print(
        "{}: cables: {}, terminations: {}, circuits: {}, circuit types: {}, providers: {}".format(
            mode, n_cables, n_terms, n_circuits, n_types, n_providers
        )
    )


if __name__ == "__main__":
    main()
