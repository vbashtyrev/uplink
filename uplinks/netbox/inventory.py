#!/usr/bin/env python3
"""Read-only NetBox inventory: providers -> circuits -> termination A -> cable -> border interface."""

import argparse
import json
import os
import sys

import pynetbox

from env_urls import load_env_file_if_present

load_env_file_if_present()

REASON_NO_TERMINATION_A = "no_termination_a"
REASON_NO_CABLE = "no_cable"
REASON_CABLE_NOT_TO_INTERFACE = "cable_not_to_interface"
REASON_NO_INTERFACE = "no_interface"
REASON_NO_DEVICE = "no_device"
REASON_NOT_BORDER_DEVICE = "not_border_device"
REASON_MISSING_COMMIT_RATE = "missing_commit_rate"

REASON_LABELS = {
    REASON_NO_TERMINATION_A: "no side-A circuit termination",
    REASON_NO_CABLE: "termination has no cable",
    REASON_CABLE_NOT_TO_INTERFACE: "cable is not connected to dcim.interface",
    REASON_NO_INTERFACE: "interface not found in NetBox",
    REASON_NO_DEVICE: "interface has no device",
    REASON_NOT_BORDER_DEVICE: "device does not have required tag",
    REASON_MISSING_COMMIT_RATE: "circuit commit_rate is not set",
}


def _record_has_tag(record, tag_slug):
    tags = getattr(record, "tags", None) or []
    for tag in tags:
        slug = getattr(tag, "slug", None) or (tag if isinstance(tag, str) else None)
        if slug == tag_slug:
            return True
    if getattr(record, "tag", None) == tag_slug:
        return True
    if getattr(record, "tag_slug", None) == tag_slug:
        return True
    return False


def _normalize_choice(value):
    """Normalize NetBox choice field (str, dict, or pynetbox Record with value/label)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("value") or value.get("label")
    choice = getattr(value, "value", None)
    if choice is not None:
        return choice
    label = getattr(value, "label", None)
    if label is not None:
        return label
    return str(value)


def _circuit_status_value(circuit):
    return _normalize_choice(getattr(circuit, "status", None))


def _provider_name(provider):
    if provider is None:
        return ""
    if isinstance(provider, str):
        return provider
    return getattr(provider, "name", None) or ""


def _resolve_provider(nb, circuit):
    provider = getattr(circuit, "provider", None)
    if provider is None:
        provider_id = getattr(circuit, "provider_id", None)
        if provider_id is not None:
            try:
                provider = nb.circuits.providers.get(provider_id)
            except Exception:
                provider = None
    elif not isinstance(provider, str) and getattr(provider, "name", None) is None:
        try:
            provider = nb.circuits.providers.get(provider.id if hasattr(provider, "id") else provider)
        except Exception:
            pass
    return provider


def _termination_side_a(terminations):
    for term in terminations:
        side = getattr(term, "term_side", None) or getattr(term, "termination_side", None)
        if _normalize_choice(side) == "A":
            return term
    return None


def _object_type_key(value):
    """Return lowercase object type identifier for cable termination matching."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, dict):
        raw = value.get("value") or value.get("label") or value.get("name")
        return str(raw).lower() if raw is not None else ""
    choice = _normalize_choice(value)
    if choice is not None:
        return str(choice).lower()
    try:
        return str(value).lower()
    except Exception:
        return ""


def _interface_from_cable(nb, cable_obj, debug=False):
    """Return interface record from cable a/b terminations, or None."""
    a_terms = getattr(cable_obj, "a_terminations", None) or []
    b_terms = getattr(cable_obj, "b_terminations", None) or []
    if not isinstance(a_terms, list):
        a_terms = [a_terms] if a_terms else []
    if not isinstance(b_terms, list):
        b_terms = [b_terms] if b_terms else []

    interface_oid = None
    for term in a_terms + b_terms:
        if isinstance(term, dict):
            ot = term.get("object_type") or term.get("object_type_id")
            oid = term.get("object_id")
        else:
            ot = getattr(term, "object_type", None) or getattr(term, "object_type_id", None)
            oid = getattr(term, "object_id", None)
        if not oid:
            continue
        ot_key = _object_type_key(ot)
        if "interface" in ot_key and "circuit" not in ot_key:
            interface_oid = oid
            break
    if not interface_oid:
        return None

    try:
        return nb.dcim.interfaces.get(interface_oid)
    except Exception as e:
        if debug:
            print("dcim.interfaces.get({}): {}".format(interface_oid, e), file=sys.stderr)
        return None


def _resolve_device(nb, iface):
    device = getattr(iface, "device", None)
    if device is None:
        dev_id = getattr(iface, "device_id", None)
        if dev_id is not None:
            try:
                device = nb.dcim.devices.get(dev_id)
            except Exception:
                device = None
    elif not isinstance(device, str) and getattr(device, "name", None) is None:
        try:
            device = nb.dcim.devices.get(device.id if hasattr(device, "id") else device)
        except Exception:
            device = None
    return device


def _billing_model(circuit):
    custom_fields = getattr(circuit, "custom_fields", None) or {}
    if isinstance(custom_fields, dict) and custom_fields.get("billing_model") is not None:
        return _normalize_choice(custom_fields.get("billing_model"))
    raw = getattr(circuit, "billing_model", None)
    if raw is not None:
        return _normalize_choice(raw)
    return None


def _commit_rate_kbps(circuit):
    value = getattr(circuit, "commit_rate", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base_entry(provider, circuit):
    provider_name = _provider_name(provider)
    return {
        "provider": provider_name,
        "circuit_id": getattr(circuit, "cid", None) or "",
        "circuit_pk": getattr(circuit, "id", None),
        "status": _circuit_status_value(circuit),
    }


def _incomplete_entry(base, reason, **extra):
    entry = dict(base)
    entry["reason"] = reason
    entry["reason_label"] = REASON_LABELS.get(reason, reason)
    entry.update(extra)
    return entry


def _complete_entry(base, device_name, interface_name, commit_rate_kbps, billing_model, warnings=None):
    entry = dict(base)
    entry["device"] = device_name
    entry["interface"] = interface_name
    entry["commit_rate_kbps"] = commit_rate_kbps
    entry["billing_model"] = billing_model
    if warnings:
        entry["warnings"] = list(warnings)
    return entry


def collect_uplink_inventory(nb, tag="border", debug=False, active_only=True):
    """
    Walk providers -> circuits -> side-A termination -> cable -> interface -> border device.

    Return dict with keys:
      complete: structurally valid border uplinks (may include warnings)
      incomplete: broken chains with reason codes
      stats: counters for reporting
    """
    result = {"complete": [], "incomplete": [], "stats": {}}
    device_ids_by_tag = set()
    if tag:
        try:
            devices_tagged = list(nb.dcim.devices.filter(tag=tag))
            device_ids_by_tag = {d.id for d in devices_tagged}
            if debug:
                print("Devices with tag {!r}: {}".format(tag, len(device_ids_by_tag)), file=sys.stderr)
        except Exception as e:
            if debug:
                print("dcim.devices.filter(tag={}): {}".format(tag, e), file=sys.stderr)

    try:
        providers = list(nb.circuits.providers.all())
    except Exception:
        try:
            providers = list(nb.circuits.providers.filter())
        except Exception as e:
            if debug:
                print("circuits.providers: {}".format(e), file=sys.stderr)
            result["stats"]["error"] = "providers_unavailable"
            return result

    stats = {
        "providers": len(providers),
        "circuits_seen": 0,
        "circuits_active": 0,
        "complete": 0,
        "incomplete": 0,
    }

    for provider in providers:
        provider_id = getattr(provider, "id", None)
        if provider_id is None:
            continue
        try:
            circuits = list(nb.circuits.circuits.filter(provider_id=provider_id))
        except Exception as e:
            if debug:
                print("circuits.filter(provider_id={}): {}".format(provider_id, e), file=sys.stderr)
            continue

        for circuit in circuits:
            stats["circuits_seen"] += 1
            base = _base_entry(provider, circuit)
            status = _circuit_status_value(circuit)
            if active_only and status and str(status).lower() != "active":
                continue
            stats["circuits_active"] += 1

            circuit_pk = getattr(circuit, "id", None)
            if circuit_pk is None:
                result["incomplete"].append(_incomplete_entry(base, REASON_NO_TERMINATION_A))
                stats["incomplete"] += 1
                continue

            try:
                terminations = list(nb.circuits.circuit_terminations.filter(circuit_id=circuit_pk))
            except Exception as e:
                if debug:
                    print("circuit_terminations.filter(circuit_id={}): {}".format(circuit_pk, e), file=sys.stderr)
                result["incomplete"].append(_incomplete_entry(base, REASON_NO_TERMINATION_A))
                stats["incomplete"] += 1
                continue

            ct_a = _termination_side_a(terminations)
            if not ct_a:
                result["incomplete"].append(_incomplete_entry(base, REASON_NO_TERMINATION_A))
                stats["incomplete"] += 1
                continue

            cable = getattr(ct_a, "cable", None)
            cable_id = cable.id if cable is not None and hasattr(cable, "id") else cable
            if not cable_id:
                result["incomplete"].append(_incomplete_entry(base, REASON_NO_CABLE))
                stats["incomplete"] += 1
                continue

            try:
                cable_obj = nb.dcim.cables.get(cable_id)
            except Exception as e:
                if debug:
                    print("dcim.cables.get({}): {}".format(cable_id, e), file=sys.stderr)
                cable_obj = None
            if not cable_obj:
                result["incomplete"].append(_incomplete_entry(base, REASON_NO_CABLE, cable_id=cable_id))
                stats["incomplete"] += 1
                continue

            iface = _interface_from_cable(nb, cable_obj, debug=debug)
            if not iface:
                result["incomplete"].append(
                    _incomplete_entry(base, REASON_CABLE_NOT_TO_INTERFACE, cable_id=cable_id)
                )
                stats["incomplete"] += 1
                continue

            iface_name = getattr(iface, "name", None) or ""
            if not iface_name:
                result["incomplete"].append(_incomplete_entry(base, REASON_NO_INTERFACE, cable_id=cable_id))
                stats["incomplete"] += 1
                continue

            device = _resolve_device(nb, iface)
            if not device:
                result["incomplete"].append(
                    _incomplete_entry(
                        base,
                        REASON_NO_DEVICE,
                        cable_id=cable_id,
                        interface=iface_name,
                    )
                )
                stats["incomplete"] += 1
                continue

            device_name = getattr(device, "name", None) or ""
            dev_id = device.id if hasattr(device, "id") else device
            if tag:
                if dev_id not in device_ids_by_tag and not _record_has_tag(device, tag):
                    result["incomplete"].append(
                        _incomplete_entry(
                            base,
                            REASON_NOT_BORDER_DEVICE,
                            cable_id=cable_id,
                            interface=iface_name,
                            device=device_name,
                            required_tag=tag,
                        )
                    )
                    stats["incomplete"] += 1
                    continue

            commit_rate_kbps = _commit_rate_kbps(circuit)
            billing_model = _billing_model(circuit)
            warnings = []
            if commit_rate_kbps is None:
                warnings.append(REASON_MISSING_COMMIT_RATE)

            result["complete"].append(
                _complete_entry(
                    base,
                    device_name,
                    iface_name,
                    commit_rate_kbps,
                    billing_model,
                    warnings=warnings or None,
                )
            )
            stats["complete"] += 1

    result["stats"] = stats
    if debug:
        print(
            "Inventory: providers={}, circuits_seen={}, active={}, complete={}, incomplete={}".format(
                stats["providers"],
                stats["circuits_seen"],
                stats["circuits_active"],
                stats["complete"],
                stats["incomplete"],
            ),
            file=sys.stderr,
        )
    return result


def format_inventory_text(report, dry_run=False):
    """Human-readable inventory report."""
    lines = []
    prefix = "[dry-run] " if dry_run else ""
    lines.append("{}NetBox uplink inventory (read-only)".format(prefix))
    stats = report.get("stats") or {}
    lines.append(
        "Providers: {}, circuits: {} (active: {}), complete: {}, incomplete: {}".format(
            stats.get("providers", 0),
            stats.get("circuits_seen", 0),
            stats.get("circuits_active", 0),
            stats.get("complete", 0),
            stats.get("incomplete", 0),
        )
    )
    for row in report.get("complete") or []:
        warn = ""
        if row.get("warnings"):
            warn = " warnings={}".format(",".join(row["warnings"]))
        lines.append(
            "OK  provider={} circuit={} device={} interface={} commit_rate_kbps={} billing_model={}{}".format(
                row.get("provider") or "?",
                row.get("circuit_id") or "?",
                row.get("device") or "?",
                row.get("interface") or "?",
                row.get("commit_rate_kbps"),
                row.get("billing_model"),
                warn,
            )
        )
    for row in report.get("incomplete") or []:
        extra = []
        if row.get("device"):
            extra.append("device={}".format(row["device"]))
        if row.get("interface"):
            extra.append("interface={}".format(row["interface"]))
        suffix = (" " + " ".join(extra)) if extra else ""
        lines.append(
            "INCOMPLETE provider={} circuit={} reason={} ({}){}".format(
                row.get("provider") or "?",
                row.get("circuit_id") or "?",
                row.get("reason") or "?",
                row.get("reason_label") or row.get("reason") or "?",
                suffix,
            )
        )
    return "\n".join(lines)


def _get_nb():
    url = os.environ.get("NETBOX_URL", "").strip().rstrip("/")
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not url or not token:
        print("NETBOX_URL and NETBOX_TOKEN must be set", file=sys.stderr)
        sys.exit(1)
    return pynetbox.api(url, token=token)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only NetBox uplink inventory (providers -> circuits -> border interfaces)."
    )
    parser.add_argument(
        "--tag",
        default=os.environ.get("NETBOX_TAG", "border"),
        help="Device tag required on the border end (default: NETBOX_TAG or border)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only preview (no create/update/delete; same data as normal run)",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose stderr diagnostics")
    args = parser.parse_args(argv)

    nb = _get_nb()
    report = collect_uplink_inventory(nb, tag=args.tag, debug=args.debug)
    stats = report.get("stats") or {}
    providers_error = stats.get("error") == "providers_unavailable"

    if args.json:
        payload = dict(report)
        payload["dry_run"] = bool(args.dry_run)
        payload["read_only"] = True
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if providers_error:
            print("Error: NetBox providers unavailable", file=sys.stderr)
        print(format_inventory_text(report, dry_run=args.dry_run))

    if providers_error:
        return 1
    incomplete = len(report.get("incomplete") or [])
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
