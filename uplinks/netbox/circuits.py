#!/usr/bin/env python3
"""Create or update NetBox circuits from commit_rates.json and dry-ssh.json."""

import argparse
import json
import os
import sys

import pynetbox
import requests

from env_urls import load_env_file_if_present
from uplinks.netbox.checks import resolve_interface
from uplinks_config import NETBOX_AUTOMATION_TAG as AUTOMATION_TAG

load_env_file_if_present()

DEFAULT_COMMIT_RATES = "commit_rates.json"
DEFAULT_DRY_SSH = "dry-ssh.json"
CIRCUIT_TYPE_DEFAULT = "Internet"
CIRCUIT_STATUS_ACTIVE = "active"


def load_commit_rates(path):
    """Load commit_rates.json. Return dict or (None, error)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "file not found: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "JSON error: {}".format(e)
    return {k: v for k, v in data.items() if not k.startswith("_")}, None


def load_dry_ssh(path):
    """Load dry-ssh.json for mapping logical interface -> physical. Return devices dict or None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data.get("devices") or None


def _get_or_create_automation_tag(nb):
    """Find or create a tag in NetBox named /slug AUTOMATION_TAG.
    Return: tag object (pynetbox Record) or None."""
    if not AUTOMATION_TAG:
        return None
    try:
        tag_obj = nb.extras.tags.get(name=AUTOMATION_TAG) or nb.extras.tags.get(
            slug=AUTOMATION_TAG
        )
        if tag_obj is None:
            slug = AUTOMATION_TAG.lower().replace(" ", "-")[:50]
            try:
                tag_obj = nb.extras.tags.create(name=AUTOMATION_TAG, slug=slug)
            except Exception:
                tag_obj = None
    except Exception:
        return None
    return tag_obj


def _ensure_record_tag(nb, record, tag_obj, endpoint):
    """Add a tag_obj tag to the NetBox object if it doesn't already exist.
    When PATCH NetBox expects a list of tag IDs. endpoint - for example nb.circuits.providers."""
    if not record or not tag_obj:
        return
    tag_id = getattr(tag_obj, "id", None)
    if not tag_id:
        return
    rid = getattr(record, "id", None)
    if not rid:
        return
    try:
        # Reload by id so that the response contains tags (filter often does not return them)
        full = endpoint.get(rid)
        current = getattr(full, "tags", []) or []
    except Exception:
        return
    current_ids = [getattr(t, "id", None) for t in current if getattr(t, "id", None) is not None]
    if tag_id in current_ids:
        return
    try:
        full.update({"tags": current_ids + [tag_id]})
    except Exception:
        pass


def resolve_physical_interface(dev_name, iface_name, dry_ssh_devices):
    """For a virtual/logical interface (ae5.0, etc.), return the physical one from dry-ssh.
    Otherwise return iface_name as is."""
    if not dry_ssh_devices or dev_name not in dry_ssh_devices:
        return iface_name
    for entry in dry_ssh_devices[dev_name]:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if name != iface_name:
            continue
        if entry.get("isLogical") or (name and "." in name and name.split(".")[0].startswith("ae")):
            phys = (entry.get("physicalInterface") or "").strip()
            if phys:
                return phys
        break
    return iface_name


def location_from_hostname(hostname):
    """The first segment before the hyphen."""
    parts = (hostname or "").split("-")
    return parts[0] if parts and parts[0] else ""


def get_or_create_provider(nb, name):
    """Return provider by name; in the absence of create."""
    existing = list(nb.circuits.providers.filter(name=name))
    tag_obj = _get_or_create_automation_tag(nb)
    if existing:
        if tag_obj:
            _ensure_record_tag(nb, existing[0], tag_obj, nb.circuits.providers)
        return existing[0], None
    try:
        slug = name.lower().replace(" ", "-").replace("/", "-")[:50]
        kwargs = {"name": name, "slug": slug}
        if tag_obj:
            kwargs["tags"] = [tag_obj.id]
        p = nb.circuits.providers.create(**kwargs)
        return p, "created"
    except Exception as e:
        return None, str(e)


def get_or_create_circuit_type(nb, name=CIRCUIT_TYPE_DEFAULT):
    """Return circuit type by name; in the absence of create."""
    existing = list(nb.circuits.circuit_types.filter(name=name))
    tag_obj = _get_or_create_automation_tag(nb)
    if existing:
        if tag_obj:
            _ensure_record_tag(nb, existing[0], tag_obj, nb.circuits.circuit_types)
        return existing[0], None
    try:
        slug = name.lower().replace(" ", "-")[:50]
        kwargs = {"name": name, "slug": slug}
        if tag_obj:
            kwargs["tags"] = [tag_obj.id]
        ct = nb.circuits.circuit_types.create(**kwargs)
        return ct, "created"
    except Exception as e:
        return None, str(e)


def get_or_create_circuit(
    nb, cid, provider, circuit_type, commit_rate_kbps, status=CIRCUIT_STATUS_ACTIVE, clear_null_commit=False
):
    """Return circuit by cid and provider; in the absence of create; if available, update commit_rate for the file."""
    existing = list(nb.circuits.circuits.filter(cid=cid, provider_id=provider.id))
    tag_obj = _get_or_create_automation_tag(nb)
    if existing:
        c = existing[0]
        if tag_obj:
            _ensure_record_tag(nb, c, tag_obj, nb.circuits.circuits)
        # Update commit_rate in NetBox by file if different.
        # With clear_null_commit and commit_rate_kbps=None, we clear commit_rate in NetBox.
        current = getattr(c, "commit_rate", None)
        if current is not None:
            try:
                current = int(current)
            except (TypeError, ValueError):
                current = None
        if commit_rate_kbps is not None:
            want = int(commit_rate_kbps)
            if current != want:
                try:
                    _patch_circuit_commit_rate(nb, c.id, want)
                    return c, "commit_rate updated"
                except Exception as e:
                    print("Error updating commit_rate for {}: {}".format(cid, e), file=sys.stderr)
                    return c, None
        elif clear_null_commit and current is not None:
            try:
                _patch_circuit_commit_rate(nb, c.id, None)
                return c, "commit_rate cleared"
            except Exception as e:
                print("Error clearing commit_rate for {}: {}".format(cid, e), file=sys.stderr)
                return c, None
        return c, None
    try:
        kwargs = {"cid": cid, "provider": provider.id, "type": circuit_type.id, "status": status}
        if commit_rate_kbps is not None:
            kwargs["commit_rate"] = int(commit_rate_kbps)
        if tag_obj:
            kwargs["tags"] = [tag_obj.id]
        c = nb.circuits.circuits.create(**kwargs)
        # After create, explicitly set commit_rate via PATCH (pynetbox create sometimes does not transmit)
        if commit_rate_kbps is not None and c and getattr(c, "id", None):
            try:
                _patch_circuit_commit_rate(nb, c.id, commit_rate_kbps)
            except Exception as e:
                print("Warning: commit_rate not set for {}: {}".format(cid, e), file=sys.stderr)
        return c, "created"
    except Exception as e:
        return None, str(e)


def _patch_circuit_commit_rate(nb, circuit_id, commit_rate_kbps):
    """Set/clear commit_rate for a circuit via REST PATCH."""
    base_url = (getattr(nb, "base_url", None) or getattr(nb, "url", None) or os.environ.get("NETBOX_URL", "")).rstrip("/")
    token = getattr(nb, "token", None) or os.environ.get("NETBOX_TOKEN")
    if not base_url or not token:
        raise RuntimeError("there is no base_url or token for pynetbox api")
    if base_url.endswith("/api"):
        url = "{}/circuits/circuits/{}/".format(base_url, circuit_id)
    else:
        url = "{}/api/circuits/circuits/{}/".format(base_url, circuit_id)
    payload = {"commit_rate": int(commit_rate_kbps)} if commit_rate_kbps is not None else {"commit_rate": None}
    r = requests.patch(
        url,
        headers={"Authorization": "Token {}".format(token), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()


def _patch_interface_mark_connected(nb, interface_id, value):
    """Resetting mark_connected on the interface via REST PATCH (pynetbox does not always accept this argument)."""
    base_url = (getattr(nb, "base_url", None) or getattr(nb, "url", None) or os.environ.get("NETBOX_URL", "")).rstrip("/")
    token = getattr(nb, "token", None) or os.environ.get("NETBOX_TOKEN")
    if not base_url or not token:
        raise RuntimeError("there is no base_url or token for pynetbox api")
    # base_url can already end in /api - do not duplicate
    if base_url.endswith("/api"):
        url = "{}/dcim/interfaces/{}/".format(base_url, interface_id)
    else:
        url = "{}/api/dcim/interfaces/{}/".format(base_url, interface_id)
    r = requests.patch(
        url,
        headers={"Authorization": "Token {}".format(token), "Content-Type": "application/json"},
        json={"mark_connected": value},
        timeout=30,
    )
    r.raise_for_status()


def create_termination_and_cable(nb, circuit, device, nb_iface, term_side="A", report=None):
    """Create a circuit termination (site device) and a cable to the interface.
    If termination already exists, we do not create it again; We create a cable if it is missing.
    report - optional dict for the report: deleted_cables, disabled_mark_connected, created_cables (lists of tuples (dev_name, iface_name, ...))."""
    site = getattr(device, "site", None)
    if not site:
        return None, "device {} does not have a site".format(device.name)
    site_id = site if isinstance(site, int) else getattr(site, "id", None)
    if not site_id:
        return None, "device {} does not have a site".format(device.name)

    # Does this circuit already have a termination (A or Z)
    terminations = list(nb.circuits.circuit_terminations.filter(circuit_id=circuit.id))
    ct = None
    for t in terminations:
        if getattr(t, "term_side", None) == term_side or getattr(t, "termination_side", None) == term_side:
            ct = t
            break
    if not ct:
        # NetBox 4.2+: termination is bound via termination_type + termination_id (site, location, etc.)
        try:
            ct = nb.circuits.circuit_terminations.create(
                circuit=circuit.id,
                term_side=term_side,
                termination_type="dcim.site",
                termination_id=site_id,
            )
        except Exception as e1:
            # NetBox 3.x: site= field
            try:
                ct = nb.circuits.circuit_terminations.create(
                    circuit=circuit.id,
                    term_side=term_side,
                    site=site_id,
                )
            except Exception as e2:
                return None, "termination: {} (4.2: {})".format(e2, e1)

    dev_name = getattr(device, "name", "")
    iface_name = getattr(nb_iface, "name", "")

    # Cable: circuit termination <-> interface.
    # If termination already has a cable to this interface — keep it (optionally tag).
    # If the cable points at another interface — delete and recreate below.
    existing_ct_cable = getattr(ct, "cable", None)
    if existing_ct_cable is not None:
        cable_id = existing_ct_cable.id if hasattr(existing_ct_cable, "id") else existing_ct_cable
        same_iface = False
        try:
            cable_rec = nb.dcim.cables.get(cable_id) if cable_id else None
            for terms in (
                getattr(cable_rec, "a_terminations", None) or [],
                getattr(cable_rec, "b_terminations", None) or [],
            ):
                for term in terms:
                    obj = term.get("object") if isinstance(term, dict) else getattr(term, "object", term)
                    obj_id = getattr(obj, "id", None)
                    if obj_id is None and isinstance(term, dict):
                        obj_id = term.get("object_id")
                    if obj_id is not None and int(obj_id) == int(nb_iface.id):
                        same_iface = True
                        break
                if same_iface:
                    break
        except Exception:
            same_iface = False
        if same_iface:
            tag_obj = _get_or_create_automation_tag(nb)
            if tag_obj and cable_id:
                try:
                    cable_rec = nb.dcim.cables.get(cable_id)
                    _ensure_record_tag(nb, cable_rec, tag_obj, nb.dcim.cables)
                except Exception:
                    pass
            return ct, None
        try:
            if cable_id:
                nb.dcim.cables.delete([cable_id])
                if report is not None:
                    report["deleted_cables"].append((dev_name, iface_name, cable_id))
            # Refresh termination so we recreate the cable below
            ct = nb.circuits.circuit_terminations.get(ct.id)
        except Exception as e:
            return ct, "moving cable from old interface: {}".format(e)

    # The interface already has a cable or mark_connected - disable it to connect as in the file
    try:
        existing_cable = getattr(nb_iface, "cable", None)
        if existing_cable is not None:
            cable_id = existing_cable.id if hasattr(existing_cable, "id") else existing_cable
            # pynetbox delete() expects a list of ids
            nb.dcim.cables.delete([cable_id])
            if report is not None:
                report["deleted_cables"].append((dev_name, iface_name, cable_id))
        if getattr(nb_iface, "mark_connected", False):
            # pynetbox Record.update() does not accept mark_connected regarding versions - reset via REST PATCH
            _patch_interface_mark_connected(nb, nb_iface.id, False)
            if report is not None:
                report["disabled_mark_connected"].append((dev_name, iface_name))
    except Exception as e:
        return ct, "disconnecting the old cable/mark_connected: {}".format(e)
    try:
        tag_obj = _get_or_create_automation_tag(nb)
        cable_kwargs = {
            "a_terminations": [
                {"object_type": "circuits.circuittermination", "object_id": ct.id}
            ],
            "b_terminations": [
                {"object_type": "dcim.interface", "object_id": nb_iface.id}
            ],
        }
        if tag_obj:
            cable_kwargs["tags"] = [tag_obj.id]
        nb.dcim.cables.create(**cable_kwargs)
        if report is not None:
            report["created_cables"].append((dev_name, iface_name))
    except Exception as e:
        return ct, "cable: {}".format(e)
    return ct, None


def main():
    parser = argparse.ArgumentParser(description="Create circuits in NetBox using commit_rates.json (by default - all sites).")
    parser.add_argument("-f", "--commit-rates", default=DEFAULT_COMMIT_RATES, help="Path to commit_rates.json")
    parser.add_argument("-d", "--dry-ssh", default=None, metavar="FILE", help="dry-ssh.json for mapping logical interface -> physical (cable to physical)")
    parser.add_argument("--location", default=None, metavar="LOC", help="Process only the specified location (the first hostname segment); default - all")
    parser.add_argument("--dry-run", action="store_true", help="Do not make changes to NetBox")
    parser.add_argument(
        "--clear-null-commit",
        action="store_true",
        help="If commit_rate_gbps=null in file, clear commit_rate in NetBox for existing circuit",
    )
    args = parser.parse_args()

    rates, err = load_commit_rates(args.commit_rates)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    tag = os.environ.get("NETBOX_TAG", "border")
    if not url or not token:
        print("Set NETBOX_URL and NETBOX_TOKEN", file=sys.stderr)
        sys.exit(1)

    nb = pynetbox.api(url, token=token)

    # NetBox devices by tag
    try:
        devices = list(nb.dcim.devices.filter(tag=tag))
    except Exception as e:
        print("NetBox Error: {}".format(e), file=sys.stderr)
        sys.exit(1)
    nb_devices_by_name = {d.name: d for d in devices}

    circuit_type_obj, ct_err = get_or_create_circuit_type(nb, CIRCUIT_TYPE_DEFAULT)
    if not circuit_type_obj:
        print("Failed to get/create circuit type: {}".format(ct_err or "?"), file=sys.stderr)
        sys.exit(1)

    dry_ssh_path = args.dry_ssh or (DEFAULT_DRY_SSH if os.path.isfile(DEFAULT_DRY_SSH) else None)
    dry_ssh_devices = load_dry_ssh(dry_ssh_path) if dry_ssh_path else None
    if dry_ssh_path and not dry_ssh_devices:
        print("Attention: dry-ssh is not loaded (file not found or empty), virtual interfaces will not be replaced with physical ones.", file=sys.stderr)

    report = {
        "created_providers": [],
        "created_circuits": [],
        "updated_commit_rate": [],
        "cleared_commit_rate": [],
        "created_cables": [],
        "deleted_cables": [],
        "disabled_mark_connected": [],
        "virtual_to_physical": [],
    }
    ok = 0
    errors = []
    for dev_name in sorted(rates.keys()):
        if args.location is not None and location_from_hostname(dev_name) != args.location:
            continue
        device = nb_devices_by_name.get(dev_name)
        if not device:
            errors.append("{}: device not found in NetBox (tag={})".format(dev_name, tag))
            continue
        ifaces = list(nb.dcim.interfaces.filter(device_id=device.id))
        nb_by_iface = {i.name: i for i in ifaces}
        for iface_name, entry in rates[dev_name].items():
            if not isinstance(entry, dict):
                continue
            provider_name = (entry.get("provider") or "").strip() or "Uplink"
            circuit_id = (entry.get("circuit_id") or "").strip()
            if not circuit_id:
                errors.append("{} {}: empty circuit_id".format(dev_name, iface_name))
                continue
            rate_gbps = entry.get("commit_rate_gbps")
            commit_rate_kbps = int(rate_gbps * 1_000_000) if rate_gbps is not None else None

            # For virtual ones (ae5.0, etc.) we take the physical interface from dry-ssh for the cable
            cable_iface_name = resolve_physical_interface(dev_name, iface_name, dry_ssh_devices)
            if cable_iface_name != iface_name:
                report["virtual_to_physical"].append((dev_name, iface_name, cable_iface_name))
            _, nb_iface = resolve_interface(cable_iface_name, nb_by_iface)
            if not nb_iface:
                errors.append("{} {}: interface not found in NetBox (for cable: {})".format(
                    dev_name, iface_name, cable_iface_name if cable_iface_name != iface_name else iface_name))
                continue

            if args.dry_run:
                print("[dry-run] {} {} -> circuit {} provider {} commit_rate_kbps={}".format(
                    dev_name, iface_name, circuit_id, provider_name, commit_rate_kbps))
                ok += 1
                continue

            provider_obj, prov_msg = get_or_create_provider(nb, provider_name)
            if not provider_obj:
                errors.append("{} {}: provider {}: {}".format(dev_name, iface_name, provider_name, prov_msg))
                continue
            if prov_msg:
                report["created_providers"].append(provider_name)
                print("Provider {}: {}".format(provider_name, prov_msg))

            circuit_obj, circ_msg = get_or_create_circuit(
                nb, circuit_id, provider_obj, circuit_type_obj, commit_rate_kbps, clear_null_commit=args.clear_null_commit
            )
            if not circuit_obj:
                errors.append("{} {}: circuit {}: {}".format(dev_name, iface_name, circuit_id, circ_msg))
                continue
            if circ_msg:
                if circ_msg == "commit_rate updated":
                    report["updated_commit_rate"].append(circuit_id)
                elif circ_msg == "commit_rate cleared":
                    report["cleared_commit_rate"].append(circuit_id)
                else:
                    report["created_circuits"].append(circuit_id)
                print("Circuit {}: {}".format(circuit_id, circ_msg))

            ct, cable_err = create_termination_and_cable(nb, circuit_obj, device, nb_iface, report=report)
            if cable_err:
                errors.append("{} {}: {}".format(dev_name, iface_name, cable_err))
            else:
                ok += 1
                print("OK: {} {} -> {} (termination + cable)".format(dev_name, iface_name, circuit_id))

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
    print("Done: {} success, {} errors.".format(ok, len(errors)))

    # Report: what was created, deleted, disabled, where there was no physics
    def _report_section(title, items, fmt):
        if not items:
            return
        print("\n--- {} ---".format(title))
        for x in items:
            print(fmt(x))

    _report_section("Providers created", report["created_providers"], lambda x: "  {}".format(x))
    _report_section("Created circuits", report["created_circuits"], lambda x: "  {}".format(x))
    _report_section("Updated commit_rate in NetBox (by file)", report["updated_commit_rate"], lambda x: "  {}".format(x))
    _report_section("Cleared commit_rate in NetBox (commit_rate_gbps=null)", report["cleared_commit_rate"], lambda x: "  {}".format(x))
    _report_section("Created cables", report["created_cables"], lambda x: "  {} {}".format(x[0], x[1]))
    _report_section("Removed cables", report["deleted_cables"], lambda x: "  {} {} (cable id {})".format(x[0], x[1], x[2]))
    _report_section("Disabled mark_connected", report["disabled_mark_connected"], lambda x: "  {} {}".format(x[0], x[1]))
    _report_section("A physical interface is used instead of a virtual one", report["virtual_to_physical"], lambda x: "  {} {} -> {}".format(x[0], x[1], x[2]))

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
