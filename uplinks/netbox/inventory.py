#!/usr/bin/env python3
"""Read-only NetBox inventory: providers -> circuits -> termination A -> cable -> border interface."""

import argparse
import json
import os
import re
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

ERROR_AUTH_DENIED = "auth_denied"
ERROR_PROVIDERS_UNAVAILABLE = "providers_unavailable"

REASON_LABELS = {
    REASON_NO_TERMINATION_A: "no side-A circuit termination",
    REASON_NO_CABLE: "termination has no cable",
    REASON_CABLE_NOT_TO_INTERFACE: "cable is not connected to dcim.interface",
    REASON_NO_INTERFACE: "interface not found in NetBox",
    REASON_NO_DEVICE: "interface has no device",
    REASON_NOT_BORDER_DEVICE: "device does not have required tag",
    REASON_MISSING_COMMIT_RATE: "circuit commit_rate is not set",
}


class NetBoxAuthError(Exception):
    """NetBox API returned 401/403 / expired or invalid token."""


_AUTH_HTTP_STATUS_CODES = {401, 403}


def _exc_http_status(exc):
    """Return HTTP status from pynetbox/requests exception, or None."""
    for attr in ("req", "response"):
        obj = getattr(exc, attr, None)
        if obj is not None:
            code = getattr(obj, "status_code", None)
            if code is not None:
                try:
                    return int(code)
                except (TypeError, ValueError):
                    pass
    return None


def is_netbox_auth_error(exc):
    """True when NetBox error looks like expired/invalid token or HTTP 401/403."""
    status = _exc_http_status(exc)
    if status is not None:
        return status in _AUTH_HTTP_STATUS_CODES

    msg = str(exc).lower()
    if "token expired" in msg or ("token" in msg and "invalid" in msg):
        return True
    if "unauthorized" in msg or "authentication failed" in msg:
        return True
    if re.search(r"(?:code|status|http)\s+401\b", msg):
        return True
    if re.search(r"(?:code|status|http)\s+403\b", msg):
        return True
    if re.search(r"\b401\s+unauthorized\b", msg):
        return True
    if re.search(r"\b403\s+forbidden\b", msg):
        return True
    return False


def _raise_if_netbox_auth(exc):
    if is_netbox_auth_error(exc):
        raise NetBoxAuthError(str(exc)) from exc


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


def _circuit_passes_active_filter(circuit, active_only):
    """When active_only, only explicit status=active passes (fail-closed)."""
    if not active_only:
        return True
    status = _circuit_status_value(circuit)
    return bool(status) and str(status).lower() == "active"


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
            except Exception as e:
                _raise_if_netbox_auth(e)
                provider = None
    elif not isinstance(provider, str) and getattr(provider, "name", None) is None:
        try:
            provider = nb.circuits.providers.get(provider.id if hasattr(provider, "id") else provider)
        except Exception as e:
            _raise_if_netbox_auth(e)
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
        _raise_if_netbox_auth(e)
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
            except Exception as e:
                _raise_if_netbox_auth(e)
                device = None
    elif not isinstance(device, str) and getattr(device, "name", None) is None:
        try:
            device = nb.dcim.devices.get(device.id if hasattr(device, "id") else device)
        except Exception as e:
            _raise_if_netbox_auth(e)
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
      stats: counters for reporting (error=auth_denied|providers_unavailable on fatal read errors)
    """
    result = {"complete": [], "incomplete": [], "stats": {}}
    try:
        _collect_uplink_inventory_body(nb, tag, debug, active_only, result)
    except NetBoxAuthError as e:
        if debug:
            print("NetBox auth error: {}".format(e), file=sys.stderr)
        return {
            "complete": [],
            "incomplete": [],
            "stats": {"error": ERROR_AUTH_DENIED},
        }
    return result


def _collect_uplink_inventory_body(nb, tag, debug, active_only, result):
    device_ids_by_tag = set()
    if tag:
        try:
            devices_tagged = list(nb.dcim.devices.filter(tag=tag))
            device_ids_by_tag = {d.id for d in devices_tagged}
            if debug:
                print("Devices with tag {!r}: {}".format(tag, len(device_ids_by_tag)), file=sys.stderr)
        except Exception as e:
            _raise_if_netbox_auth(e)
            if debug:
                print("dcim.devices.filter(tag={}): {}".format(tag, e), file=sys.stderr)

    try:
        providers = list(nb.circuits.providers.all())
    except Exception as e:
        _raise_if_netbox_auth(e)
        try:
            providers = list(nb.circuits.providers.filter())
        except Exception as e2:
            _raise_if_netbox_auth(e2)
            if debug:
                print("circuits.providers: {}".format(e2), file=sys.stderr)
            result["stats"]["error"] = ERROR_PROVIDERS_UNAVAILABLE
            return

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
            _raise_if_netbox_auth(e)
            if debug:
                print("circuits.filter(provider_id={}): {}".format(provider_id, e), file=sys.stderr)
            continue

        for circuit in circuits:
            stats["circuits_seen"] += 1
            base = _base_entry(provider, circuit)
            if not _circuit_passes_active_filter(circuit, active_only):
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
                _raise_if_netbox_auth(e)
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
                _raise_if_netbox_auth(e)
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


def netbox_client_from_env(debug=False):
    """Return pynetbox API client or None when NetBox is not configured."""
    url = os.environ.get("NETBOX_URL", "").strip()
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not url or not token:
        if debug:
            print(
                "NetBox: NETBOX_URL/NETBOX_TOKEN are not set - providers only from local data",
                file=sys.stderr,
            )
        return None
    try:
        return pynetbox.api(url, token=token)
    except Exception as e:
        if debug:
            print("NetBox: failed to connect: {}".format(e), file=sys.stderr)
        return None


def netbox_border_tag():
    return (os.environ.get("NETBOX_TAG") or "border").strip() or "border"


def providers_from_complete_inventory(report):
    """Unique Provider.name values from structurally complete inventory rows."""
    names = set()
    for row in report.get("complete") or []:
        provider = (row.get("provider") or "").strip()
        if provider:
            names.add(provider)
    return names


def device_names_from_complete_inventory(report):
    """Unique device names from structurally complete inventory rows."""
    names = set()
    for row in report.get("complete") or []:
        device = (row.get("device") or "").strip()
        if device:
            names.add(device)
    return names


def is_burst_billing_model(billing_model):
    """True when billing_model is Burst (case-insensitive)."""
    return (billing_model or "").strip().lower() == "burst"


def burst_pairs_from_inventory(report):
    """(device, interface) pairs with billing_model=Burst from complete inventory rows."""
    pairs = set()
    for row in report.get("complete") or []:
        if not is_burst_billing_model(row.get("billing_model")):
            continue
        device_name = (row.get("device") or "").strip()
        iface_name = (row.get("interface") or "").strip()
        if device_name and iface_name:
            pairs.add((device_name, iface_name))
    return pairs


def burst_metadata_from_inventory(report):
    """(device, interface) -> {provider, circuit_id} for billing_model=Burst."""
    out = {}
    for row in report.get("complete") or []:
        if not is_burst_billing_model(row.get("billing_model")):
            continue
        provider = (row.get("provider") or "").strip()
        circuit_id = (row.get("circuit_id") or "").strip()
        device_name = (row.get("device") or "").strip()
        iface_name = (row.get("interface") or "").strip()
        if not device_name or not iface_name or not provider or not circuit_id:
            continue
        out[(device_name, iface_name)] = {"provider": provider, "circuit_id": circuit_id}
    return out


def burst_circuits_unique_from_inventory(report):
    """Unique circuit_id -> provider (first encountered) for Burst rows."""
    out = {}
    for row in report.get("complete") or []:
        if not is_burst_billing_model(row.get("billing_model")):
            continue
        circuit_id = (row.get("circuit_id") or "").strip()
        provider = (row.get("provider") or "").strip()
        if not circuit_id or not provider:
            continue
        if circuit_id not in out:
            out[circuit_id] = provider
    return sorted(out.items(), key=lambda x: x[0])


def _provider_custom_field_float(provider, field_name):
    custom_fields = getattr(provider, "custom_fields", None) or {}
    if not isinstance(custom_fields, dict):
        return None
    value = custom_fields.get(field_name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def provider_slo_percent_from_custom_fields(provider):
    """Read slo_percent from Provider custom fields."""
    return _provider_custom_field_float(provider, "slo_percent")


def provider_aggregate_limit_gbps_from_custom_fields(provider):
    """Read aggregate_limit_gbps from Provider custom fields."""
    return _provider_custom_field_float(provider, "aggregate_limit_gbps")


def collect_provider_slo_percent(nb, debug=False):
    """Provider.name -> slo_percent from NetBox custom fields.

    Returns (mapping, error) where error is ERROR_AUTH_DENIED on token/auth failure,
    or None on success / non-auth read errors (missing custom fields are omitted).
    """
    slo = {}
    try:
        providers = list(nb.circuits.providers.all())
    except Exception as e:
        if is_netbox_auth_error(e):
            if debug:
                print(
                    "NetBox: circuits.providers.all(): auth denied ({})".format(e),
                    file=sys.stderr,
                )
            return slo, ERROR_AUTH_DENIED
        if debug:
            print("NetBox: circuits.providers.all(): {}".format(e), file=sys.stderr)
        return slo, None
    for provider in providers:
        name = getattr(provider, "name", None)
        if not name:
            continue
        slo_percent = provider_slo_percent_from_custom_fields(provider)
        if slo_percent is not None:
            slo[name] = slo_percent
    if debug and slo:
        print(
            "NetBox: slo_percent for: {}".format(", ".join(sorted(slo.keys()))),
            file=sys.stderr,
        )
    return slo, None


def collect_provider_limits_gbps(nb, debug=False):
    """Provider.name -> aggregate_limit_gbps from NetBox custom fields."""
    limits = {}
    try:
        providers = list(nb.circuits.providers.all())
    except Exception as e:
        if debug:
            print("NetBox: circuits.providers.all(): {}".format(e), file=sys.stderr)
        return limits
    for provider in providers:
        name = getattr(provider, "name", None)
        if not name:
            continue
        limit_gbps = provider_aggregate_limit_gbps_from_custom_fields(provider)
        if limit_gbps is not None:
            limits[name] = limit_gbps
    if debug and limits:
        print(
            "NetBox: aggregate_limit_gbps for: {}".format(", ".join(sorted(limits.keys()))),
            file=sys.stderr,
        )
    return limits


def _normalize_iface_name(iface_name):
    """Lowercase interface name for case-insensitive inventory/dry-ssh joins."""
    from uplinks.zabbix.client import normalize_interface_name

    return normalize_interface_name(iface_name)


def _normalize_map_key(hostname, iface_name):
    """Case-insensitive (device, interface) key for inventory provider map."""
    return (hostname, _normalize_iface_name(iface_name))


def _provider_map_get(device_iface_to_provider, hostname, iface_name):
    """Lookup provider by interface name with case normalization."""
    if not hostname or not iface_name:
        return None
    return device_iface_to_provider.get(_normalize_map_key(hostname, iface_name))


def _build_physical_to_logical_normalized(dry_ssh_devices):
    """Like build_physical_to_logical, with case-normalized physical interface keys."""
    out = {}
    if not dry_ssh_devices:
        return out
    for dev_name, ifaces in dry_ssh_devices.items():
        if not isinstance(ifaces, list):
            continue
        for entry in ifaces:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            phys = _normalize_iface_name((entry.get("physicalInterface") or "").strip())
            if not name or not phys:
                continue
            key = (dev_name, phys)
            out.setdefault(key, []).append(name)
    return out


def _build_member_to_aggregate(dry_ssh_devices):
    """Map (device, member iface) -> aggregate name (aeN) from dry-ssh aggregateInterface."""
    out = {}
    if not dry_ssh_devices:
        return out
    for dev_name, ifaces in dry_ssh_devices.items():
        if not isinstance(ifaces, list):
            continue
        for entry in ifaces:
            if not isinstance(entry, dict):
                continue
            name = _normalize_iface_name((entry.get("name") or "").strip())
            aggregate = _normalize_iface_name((entry.get("aggregateInterface") or "").strip())
            if name and aggregate:
                out[(dev_name, name)] = aggregate
    return out


def _inventory_alias_ifaces(dev_name, inv_iface, phys_to_logical, member_to_aggregate):
    """
    Inventory cable may terminate on member, aggregate, or logical iface.
    Return related interface names that should inherit the same provider.
    """
    inv_norm = _normalize_iface_name(inv_iface)
    aliases = {inv_norm}
    aggregate = member_to_aggregate.get((dev_name, inv_norm))
    if aggregate:
        aliases.add(aggregate)
    for anchor in list(aliases):
        anchor_norm = _normalize_iface_name(anchor)
        for logical in phys_to_logical.get((dev_name, anchor_norm), []):
            aliases.add(_normalize_iface_name(logical))
    return aliases


def expand_provider_map_for_zabbix(inventory_map, dry_ssh_devices, debug=False):
    """Map inventory interfaces to Zabbix names (logical aeN.0, case-normalized keys)."""
    phys_to_logical = _build_physical_to_logical_normalized(dry_ssh_devices)
    member_to_aggregate = _build_member_to_aggregate(dry_ssh_devices)
    result = {}
    substituted = []

    for (dev_name, inv_iface), provider in inventory_map.items():
        if not dev_name or not inv_iface or not provider:
            continue
        inv_iface_norm = _normalize_iface_name(inv_iface)
        alias_ifaces = _inventory_alias_ifaces(
            dev_name, inv_iface_norm, phys_to_logical, member_to_aggregate
        )
        for alias in alias_ifaces:
            key = _normalize_map_key(dev_name, alias)
            if key not in result:
                result[key] = provider
            if debug and alias != inv_iface_norm:
                substituted.append((dev_name, inv_iface_norm, alias, provider))

    if debug and substituted:
        for dev_name, inv_iface, logical, provider in substituted:
            print(
                "NetBox provider map: {} {} -> {} ({})".format(
                    dev_name, inv_iface, logical, provider
                ),
                file=sys.stderr,
            )
    return result


def device_iface_provider_map_from_inventory(report, dry_ssh_devices=None, debug=False):
    """Build (device, interface) -> provider from complete inventory rows."""
    inventory_map = {}
    for row in report.get("complete") or []:
        device_name = row.get("device") or ""
        iface_name = row.get("interface") or ""
        provider = (row.get("provider") or "").strip()
        if device_name and iface_name and provider:
            inventory_map[_normalize_map_key(device_name, iface_name)] = provider
    if dry_ssh_devices is not None:
        return expand_provider_map_for_zabbix(inventory_map, dry_ssh_devices, debug=debug)
    return inventory_map


def iface_has_inventory_entry(hostname, iface, device_iface_to_provider=None):
    """True when expanded inventory contains a provider for this interface."""
    device_iface_to_provider = device_iface_to_provider or {}
    iface_name = (iface.get("name") or "").strip()
    if _provider_map_get(device_iface_to_provider, hostname, iface_name):
        return True
    phys = (iface.get("physicalInterface") or "").strip()
    if _provider_map_get(device_iface_to_provider, hostname, phys):
        return True
    aggregate = (iface.get("aggregateInterface") or "").strip()
    if _provider_map_get(device_iface_to_provider, hostname, aggregate):
        return True
    return False


def is_uplink_iface(iface, hostname=None, device_iface_to_provider=None):
    """Uplink by description, or by complete inventory when hostname is known."""
    from generate_commit_rates import is_uplink

    if is_uplink(iface):
        return True
    if not hostname:
        return False
    return iface_has_inventory_entry(hostname, iface, device_iface_to_provider)


def resolve_provider_name_for_iface(hostname, iface, desc_to_name, device_iface_to_provider=None):
    """Resolve provider name: NetBox inventory first, description map as fallback."""
    device_iface_to_provider = device_iface_to_provider or {}
    iface_name = (iface.get("name") or "").strip()
    for candidate in (
        iface_name,
        (iface.get("physicalInterface") or "").strip(),
        (iface.get("aggregateInterface") or "").strip(),
    ):
        provider = _provider_map_get(device_iface_to_provider, hostname, candidate)
        if provider:
            return provider
    description = iface.get("description", "")
    return desc_to_name.get(description, description)


def load_uplink_provider_context(dry_ssh_devices, debug=False, border_tag=None):
    """
    Read-only NetBox uplink provider context for map/dashboard/aggregate.
    Return dict with device_iface_to_provider, providers (set), stats; or None.
    """
    nb = netbox_client_from_env(debug=debug)
    if nb is None:
        return None

    tag = border_tag if border_tag is not None else netbox_border_tag()
    report = collect_uplink_inventory(nb, tag=tag, debug=debug, active_only=True)
    stats = report.get("stats") or {}
    error = stats.get("error")
    if error == ERROR_AUTH_DENIED:
        print(
            "Warning: NetBox authentication failed; falling back to local provider data",
            file=sys.stderr,
        )
        if debug:
            print("collect_uplink_inventory: {}".format(error), file=sys.stderr)
        return None
    if error == ERROR_PROVIDERS_UNAVAILABLE:
        if debug:
            print("NetBox: providers unavailable; falling back to local provider data", file=sys.stderr)
        return None

    return {
        "device_iface_to_provider": device_iface_provider_map_from_inventory(
            report, dry_ssh_devices, debug=debug
        ),
        "providers": providers_from_complete_inventory(report),
        "stats": stats,
    }


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
    auth_error = stats.get("error") == ERROR_AUTH_DENIED
    providers_error = stats.get("error") == ERROR_PROVIDERS_UNAVAILABLE

    if args.json:
        payload = dict(report)
        payload["dry_run"] = bool(args.dry_run)
        payload["read_only"] = True
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if auth_error:
            print(
                "Error: NetBox authentication failed (403 or invalid/expired token). Check NETBOX_TOKEN.",
                file=sys.stderr,
            )
        elif providers_error:
            print("Error: NetBox providers unavailable", file=sys.stderr)
        print(format_inventory_text(report, dry_run=args.dry_run))

    if auth_error or providers_error:
        return 1
    incomplete = len(report.get("incomplete") or [])
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
