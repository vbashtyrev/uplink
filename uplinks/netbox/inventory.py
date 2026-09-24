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
REASON_MISSING_PROVIDER_AGGREGATE_LIMIT = "missing_provider_aggregate_limit"
REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"

ERROR_AUTH_DENIED = "auth_denied"
ERROR_PROVIDERS_UNAVAILABLE = "providers_unavailable"
ERROR_PARTIAL_READ = "partial_read"

UPLINK_CIRCUIT_TYPE = "uplink"

REASON_LABELS = {
    REASON_NO_TERMINATION_A: "no side-A circuit termination",
    REASON_NO_CABLE: "termination has no cable",
    REASON_CABLE_NOT_TO_INTERFACE: "cable is not connected to dcim.interface",
    REASON_NO_INTERFACE: "interface not found in NetBox",
    REASON_NO_DEVICE: "interface has no device",
    REASON_NOT_BORDER_DEVICE: "device does not have required tag",
    REASON_MISSING_COMMIT_RATE: "circuit commit_rate is not set",
    REASON_MISSING_PROVIDER_AGGREGATE_LIMIT: (
        "FlatAggCap circuit has no commit_rate and provider aggregate_limit_gbps is not set"
    ),
    REASON_PROVIDER_UNAVAILABLE: "circuit provider could not be read from NetBox",
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


def _normalize_scope_token(value):
    """Normalize circuit type name/slug (case and surrounding whitespace)."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _circuit_type_tokens(circuit, nb=None, stats=None):
    """Normalized name/slug tokens for circuit.type (fail-closed when type is missing)."""
    tokens = set()
    type_obj = getattr(circuit, "type", None)
    if type_obj is None:
        type_id = getattr(circuit, "type_id", None)
        if type_id is not None and nb is not None:
            try:
                type_obj = nb.circuits.circuit_types.get(type_id)
            except Exception as e:
                _raise_if_netbox_auth(e)
                _bump_read_error(stats)
                type_obj = None
    if isinstance(type_obj, dict):
        for key in ("name", "slug", "label", "value"):
            val = type_obj.get(key)
            if val:
                tokens.add(_normalize_scope_token(val))
    elif type_obj is not None:
        for attr in ("name", "slug"):
            val = getattr(type_obj, attr, None)
            if val:
                tokens.add(_normalize_scope_token(val))
        choice = _normalize_choice(type_obj)
        if choice:
            tokens.add(_normalize_scope_token(choice))
    return tokens


def circuit_matches_scope(circuit, circuit_scope, nb=None, stats=None):
    """True when circuit_scope is unset or the circuit matches type filters (fail-closed)."""
    if not circuit_scope:
        return True
    circuit_type = circuit_scope.get("circuit_type")
    if circuit_type is not None:
        expected = _normalize_scope_token(circuit_type)
        tokens = _circuit_type_tokens(circuit, nb=nb, stats=stats)
        if not tokens or expected not in tokens:
            return False
    return True


def project_circuit_scope(circuit_type=None):
    """Project monitoring scope: Circuit type Uplink (name/slug) + built-in status Active."""
    if circuit_type is None:
        circuit_type = UPLINK_CIRCUIT_TYPE
    return {"circuit_type": circuit_type}


def _bump_read_error(stats):
    if stats is not None:
        stats["read_errors"] = stats.get("read_errors", 0) + 1


def finalize_inventory_read_stats(stats):
    """Set stats.error=partial_read when read_errors occurred (fail-closed gate)."""
    if stats is not None and stats.get("read_errors", 0) > 0 and "error" not in stats:
        stats["error"] = ERROR_PARTIAL_READ


def _resolve_provider(nb, circuit, stats=None):
    provider = getattr(circuit, "provider", None)
    if provider is None:
        provider_id = getattr(circuit, "provider_id", None)
        if provider_id is not None:
            try:
                provider = nb.circuits.providers.get(provider_id)
            except Exception as e:
                _raise_if_netbox_auth(e)
                _bump_read_error(stats)
                provider = None
    elif not isinstance(provider, str) and getattr(provider, "name", None) is None:
        try:
            provider = nb.circuits.providers.get(provider.id if hasattr(provider, "id") else provider)
        except Exception as e:
            _raise_if_netbox_auth(e)
            _bump_read_error(stats)
            provider = None
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


def _is_interface_object_type(ot_key):
    return "interface" in ot_key and "circuit" not in ot_key


def _safe_get_mapping(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return getattr(obj, key, default)
    except Exception:
        pass
    try:
        return obj[key]
    except Exception:
        pass
    return default


def _record_object_id(record):
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get("id")
    try:
        return getattr(record, "id", None)
    except Exception:
        return None


def _object_type_from_url(url):
    url = (url or "").lower()
    if "/dcim/interfaces/" in url:
        return "dcim.interface"
    if "/dcim/rear-ports/" in url:
        return "dcim.rearport"
    if "/dcim/front-ports/" in url:
        return "dcim.frontport"
    if "/dcim/cables/" in url:
        return "dcim.cable"
    if "/circuits/circuit-terminations/" in url:
        return "circuits.circuittermination"
    return ""


def _record_object_type_key(record):
    if record is None:
        return ""
    if isinstance(record, dict):
        url = record.get("url") or ""
        if url:
            return _object_type_from_url(url)
        ot = record.get("object_type") or record.get("object_type_id")
        return _object_type_key(ot)
    url = ""
    try:
        url = getattr(record, "url", "") or ""
    except Exception:
        pass
    if url:
        return _object_type_from_url(url)
    cls_name = type(record).__name__.lower()
    type_map = {
        "interfaces": "dcim.interface",
        "interface": "dcim.interface",
        "rearports": "dcim.rearport",
        "rearport": "dcim.rearport",
        "frontports": "dcim.frontport",
        "frontport": "dcim.frontport",
        "cables": "dcim.cable",
        "cable": "dcim.cable",
        "termination": "circuits.circuittermination",
        "circuittermination": "circuits.circuittermination",
    }
    return type_map.get(cls_name, "")


def _record_is_interface(record):
    ot_key = _record_object_type_key(record)
    return "interface" in ot_key and "circuit" not in ot_key


def _record_is_cable(record):
    return "cable" in _record_object_type_key(record)


def _record_is_circuit_termination(record):
    ot_key = _record_object_type_key(record)
    return "circuittermination" in ot_key or "circuit termination" in ot_key


def _iter_path_segment_objects(path_info):
    """Yield objects from the structural path only (not origin/destination)."""
    if path_info is None:
        return
    path_segments = _safe_get_mapping(path_info, "path") or []
    if not isinstance(path_segments, (list, tuple)):
        return
    for segment in path_segments:
        if isinstance(segment, (list, tuple)):
            for obj in segment:
                if obj is not None:
                    yield obj
        elif segment is not None:
            yield segment


def _iter_path_objects(path_info):
    if path_info is None:
        return
    for key in ("origin", "destination"):
        obj = _safe_get_mapping(path_info, key)
        if obj is not None:
            yield obj
    for obj in _iter_path_segment_objects(path_info):
        yield obj


def _path_info_has_flags(path_info):
    if not isinstance(path_info, dict):
        return False
    return any(key in path_info for key in ("is_active", "is_complete", "is_split"))


def _is_legacy_path_info(path_info):
    if not isinstance(path_info, dict):
        return False
    if _path_info_has_flags(path_info):
        return False
    return all(key in path_info for key in ("origin", "destination", "path"))


def _new_path_info_valid(path_info):
    return (
        path_info.get("is_split") is False
        and path_info.get("is_active") is True
        and path_info.get("is_complete") is True
    )


def _cable_connection_status(record):
    if not _record_is_cable(record):
        return None
    if isinstance(record, dict):
        status = record.get("status")
    else:
        try:
            status = getattr(record, "status", None)
        except Exception:
            status = None
    normalized = _normalize_choice(status)
    if normalized is None:
        return None
    return str(normalized).lower()


def _all_path_cables_connected(path_info):
    found_cable = False
    for obj in _iter_path_segment_objects(path_info):
        if _record_is_cable(obj):
            found_cable = True
            if _cable_connection_status(obj) != "connected":
                return False
    return found_cable


def _path_matches_cable_and_termination(path_info, cable_id, circuit_termination_id):
    if cable_id is None or circuit_termination_id is None:
        return False
    has_cable = False
    has_ct = False
    for obj in _iter_path_segment_objects(path_info):
        try:
            oid = _record_object_id(obj)
            if oid is None:
                continue
            if int(oid) == int(cable_id) and _record_is_cable(obj):
                has_cable = True
            if int(oid) == int(circuit_termination_id) and _record_is_circuit_termination(obj):
                has_ct = True
        except (TypeError, ValueError):
            continue
        except Exception:
            continue
    return has_cable and has_ct


def _interface_from_path_info(path_info, circuit_termination_id=None):
    """Extract confirmed terminal dcim.interface from a validated paths() entry."""
    objects = list(_iter_path_segment_objects(path_info))
    interfaces = []
    ct_index = None
    for index, obj in enumerate(objects):
        try:
            if _record_is_interface(obj):
                interfaces.append((index, obj))
            if circuit_termination_id is not None:
                oid = _record_object_id(obj)
                if (
                    oid is not None
                    and int(oid) == int(circuit_termination_id)
                    and _record_is_circuit_termination(obj)
                ):
                    ct_index = index
        except (TypeError, ValueError):
            continue
        except Exception:
            continue

    if not interfaces:
        return None
    if len(interfaces) == 1:
        return interfaces[0][1]
    if ct_index is not None:
        farthest = max(interfaces, key=lambda item: abs(item[0] - ct_index))
        return farthest[1]
    return interfaces[-1][1]


def _cable_terminations(cable_obj):
    a_terms = getattr(cable_obj, "a_terminations", None) or []
    b_terms = getattr(cable_obj, "b_terminations", None) or []
    if not isinstance(a_terms, list):
        a_terms = [a_terms] if a_terms else []
    if not isinstance(b_terms, list):
        b_terms = [b_terms] if b_terms else []
    return a_terms, b_terms


def _termination_object_type_and_id(term):
    if isinstance(term, dict):
        ot = term.get("object_type") or term.get("object_type_id")
        oid = term.get("object_id")
    else:
        ot = getattr(term, "object_type", None) or getattr(term, "object_type_id", None)
        oid = getattr(term, "object_id", None)
    return ot, oid


def _port_from_cable(nb, cable_obj, debug=False, stats=None):
    """Return rear/front port record from cable terminations, or None."""
    a_terms, b_terms = _cable_terminations(cable_obj)
    port_oid = None
    is_rear = None
    for term in a_terms + b_terms:
        ot, oid = _termination_object_type_and_id(term)
        if not oid:
            continue
        ot_key = _object_type_key(ot)
        if "rearport" in ot_key:
            port_oid = oid
            is_rear = True
            break
        if "frontport" in ot_key:
            port_oid = oid
            is_rear = False
            break
    if not port_oid:
        return None

    endpoint = nb.dcim.rear_ports if is_rear else nb.dcim.front_ports
    if endpoint is None or not hasattr(endpoint, "get"):
        return None
    try:
        return endpoint.get(port_oid)
    except Exception as e:
        _raise_if_netbox_auth(e)
        _bump_read_error(stats)
        if debug:
            label = "rear_ports" if is_rear else "front_ports"
            print("dcim.{}.get({}): {}".format(label, port_oid, e), file=sys.stderr)
        return None


def _interface_from_legacy_path_info(
    path_info, cable_id, circuit_termination_id, debug=False, port=None
):
    """Accept old pynetbox paths() shape when structural checks pass."""
    if not _all_path_cables_connected(path_info):
        if debug:
            print(
                "skip legacy path on {}: cable not connected".format(
                    getattr(port, "name", port)
                ),
                file=sys.stderr,
            )
        return None
    if not _path_matches_cable_and_termination(path_info, cable_id, circuit_termination_id):
        return None
    return _interface_from_path_info(path_info, circuit_termination_id=circuit_termination_id)


def _interface_from_port_paths(nb, port, cable_id, circuit_termination_id, debug=False, stats=None):
    """Trace through pass-through port paths() to find terminal dcim.interface."""
    paths_fn = getattr(port, "paths", None)
    if not callable(paths_fn):
        return None
    try:
        path_list = paths_fn()
    except Exception as e:
        _raise_if_netbox_auth(e)
        _bump_read_error(stats)
        if debug:
            print("{}.paths(): {}".format(getattr(port, "name", port), e), file=sys.stderr)
        return None
    if not path_list or not isinstance(path_list, (list, tuple)):
        return None

    legacy_entries = [
        path_info
        for path_info in path_list
        if isinstance(path_info, dict) and _is_legacy_path_info(path_info)
    ]
    if legacy_entries:
        if len(legacy_entries) != 1 or len(path_list) != 1:
            if debug:
                print(
                    "skip legacy paths on {}: expected exactly one path, got {}".format(
                        getattr(port, "name", port), len(path_list)
                    ),
                    file=sys.stderr,
                )
            return None
        return _interface_from_legacy_path_info(
            legacy_entries[0],
            cable_id,
            circuit_termination_id,
            debug=debug,
            port=port,
        )

    for path_info in path_list:
        if not isinstance(path_info, dict):
            continue
        if not _path_info_has_flags(path_info):
            if debug:
                print(
                    "skip path on {}: missing path flags".format(
                        getattr(port, "name", port)
                    ),
                    file=sys.stderr,
                )
            continue
        if not _new_path_info_valid(path_info):
            if debug:
                print(
                    "skip path on {}: is_active={} is_complete={} is_split={}".format(
                        getattr(port, "name", port),
                        path_info.get("is_active"),
                        path_info.get("is_complete"),
                        path_info.get("is_split"),
                    ),
                    file=sys.stderr,
                )
            continue
        if not _path_matches_cable_and_termination(path_info, cable_id, circuit_termination_id):
            continue
        iface = _interface_from_path_info(path_info, circuit_termination_id=circuit_termination_id)
        if iface:
            return iface
    return None


def interface_from_cable(nb, cable_obj, cable_id=None, circuit_termination_id=None, debug=False, stats=None):
    """Return interface from cable terminations or traced pass-through port paths()."""
    if cable_id is None:
        cable_id = _record_object_id(cable_obj)
    a_terms, b_terms = _cable_terminations(cable_obj)

    interface_oid = None
    for term in a_terms + b_terms:
        ot, oid = _termination_object_type_and_id(term)
        if not oid:
            continue
        ot_key = _object_type_key(ot)
        if _is_interface_object_type(ot_key):
            interface_oid = oid
            break
    if interface_oid:
        try:
            return nb.dcim.interfaces.get(interface_oid)
        except Exception as e:
            _raise_if_netbox_auth(e)
            _bump_read_error(stats)
            if debug:
                print("dcim.interfaces.get({}): {}".format(interface_oid, e), file=sys.stderr)
            return None

    port = _port_from_cable(nb, cable_obj, debug=debug, stats=stats)
    if not port:
        return None
    return _interface_from_port_paths(
        nb, port, cable_id, circuit_termination_id, debug=debug, stats=stats
    )


def _resolve_device(nb, iface, stats=None):
    device = getattr(iface, "device", None)
    if device is None:
        dev_id = getattr(iface, "device_id", None)
        if dev_id is not None:
            try:
                device = nb.dcim.devices.get(dev_id)
            except Exception as e:
                _raise_if_netbox_auth(e)
                _bump_read_error(stats)
                device = None
    elif not isinstance(device, str) and getattr(device, "name", None) is None:
        try:
            device = nb.dcim.devices.get(device.id if hasattr(device, "id") else device)
        except Exception as e:
            _raise_if_netbox_auth(e)
            _bump_read_error(stats)
            device = None
    return device


def is_flat_agg_cap_billing_model(billing_model):
    """True when billing_model is FlatAggCap (case-insensitive)."""
    return (billing_model or "").strip().lower() == "flataggcap"


def _commit_rate_warnings(circuit, provider):
    """Warnings for missing per-circuit commit_rate (shared cap exempt on FlatAggCap)."""
    if _commit_rate_kbps(circuit) is not None:
        return []
    billing_model = _billing_model(circuit)
    if is_flat_agg_cap_billing_model(billing_model):
        if provider_aggregate_limit_gbps_from_custom_fields(provider) is not None:
            return []
        return [REASON_MISSING_PROVIDER_AGGREGATE_LIMIT]
    return [REASON_MISSING_COMMIT_RATE]


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


def collect_uplink_inventory(nb, tag="border", debug=False, active_only=True, circuit_scope=None):
    """
    Walk providers -> circuits -> side-A termination -> cable -> interface -> border device.

    When circuit_scope is set (e.g. circuit type Uplink), only matching circuits are walked;
    out-of-scope circuits are omitted from complete/incomplete.

    Return dict with keys:
      complete: structurally valid border uplinks (may include warnings)
      incomplete: broken chains with reason codes
      stats: counters for reporting (error=auth_denied|providers_unavailable on fatal read errors)
    """
    result = {"complete": [], "incomplete": [], "stats": {}}
    try:
        _collect_uplink_inventory_body(nb, tag, debug, active_only, circuit_scope, result)
    except NetBoxAuthError as e:
        if debug:
            print("NetBox auth error: {}".format(e), file=sys.stderr)
        return {
            "complete": [],
            "incomplete": [],
            "stats": {"error": ERROR_AUTH_DENIED},
        }
    return result


def _collect_uplink_inventory_body(nb, tag, debug, active_only, circuit_scope, result):
    read_errors = 0
    device_ids_by_tag = set()
    if tag:
        try:
            devices_tagged = list(nb.dcim.devices.filter(tag=tag))
            device_ids_by_tag = {d.id for d in devices_tagged}
            if debug:
                print("Devices with tag {!r}: {}".format(tag, len(device_ids_by_tag)), file=sys.stderr)
        except Exception as e:
            _raise_if_netbox_auth(e)
            read_errors += 1
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
            result["stats"] = {
                "error": ERROR_PROVIDERS_UNAVAILABLE,
                "read_errors": read_errors,
            }
            return

    stats = {
        "providers": len(providers),
        "circuits_seen": 0,
        "circuits_in_scope": 0,
        "circuits_active": 0,
        "complete": 0,
        "incomplete": 0,
        "read_errors": read_errors,
    }

    for provider in providers:
        provider_id = getattr(provider, "id", None)
        if provider_id is None:
            continue
        try:
            circuits = list(nb.circuits.circuits.filter(provider_id=provider_id))
        except Exception as e:
            _raise_if_netbox_auth(e)
            stats["read_errors"] += 1
            if debug:
                print("circuits.filter(provider_id={}): {}".format(provider_id, e), file=sys.stderr)
            base = {
                "provider": _provider_name(provider),
                "circuit_id": "",
                "circuit_pk": None,
                "status": "",
            }
            result["incomplete"].append(_incomplete_entry(base, REASON_PROVIDER_UNAVAILABLE))
            stats["incomplete"] += 1
            continue

        for circuit in circuits:
            stats["circuits_seen"] += 1
            if circuit_scope and not circuit_matches_scope(
                circuit, circuit_scope, nb=nb, stats=stats
            ):
                continue
            stats["circuits_in_scope"] += 1
            circuit_provider = _resolve_provider(nb, circuit, stats)
            if circuit_provider is None and (
                getattr(circuit, "provider_id", None) is not None
                or getattr(circuit, "provider", None) is not None
            ):
                base = _base_entry(provider, circuit)
                result["incomplete"].append(
                    _incomplete_entry(base, REASON_PROVIDER_UNAVAILABLE)
                )
                stats["incomplete"] += 1
                continue
            entry_provider = circuit_provider if circuit_provider is not None else provider
            base = _base_entry(entry_provider, circuit)
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
                _bump_read_error(stats)
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
                _bump_read_error(stats)
                if debug:
                    print("dcim.cables.get({}): {}".format(cable_id, e), file=sys.stderr)
                cable_obj = None
            if not cable_obj:
                result["incomplete"].append(_incomplete_entry(base, REASON_NO_CABLE, cable_id=cable_id))
                stats["incomplete"] += 1
                continue

            ct_a_id = getattr(ct_a, "id", None)
            iface = interface_from_cable(
                nb,
                cable_obj,
                cable_id=cable_id,
                circuit_termination_id=ct_a_id,
                debug=debug,
                stats=stats,
            )
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

            device = _resolve_device(nb, iface, stats=stats)
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
            warnings = _commit_rate_warnings(circuit, entry_provider)

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
    enrich_inventory_provider_stats(result)
    finalize_inventory_read_stats(stats)
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


def resolve_border_device_tag(tag=None):
    """Return the NetBox tag used to identify border devices."""
    if tag is None:
        resolved = os.environ.get("NETBOX_TAG") or "border"
    else:
        resolved = tag
    return (resolved or "border").strip() or "border"


def netbox_border_tag():
    return resolve_border_device_tag()


def providers_from_complete_inventory(report):
    """Unique Provider.name values from structurally complete inventory rows."""
    names = set()
    for row in report.get("complete") or []:
        provider = (row.get("provider") or "").strip()
        if provider:
            names.add(provider)
    return names


def enrich_inventory_provider_stats(report):
    """Add providers_in_scope to stats from complete inventory rows."""
    stats = report.get("stats")
    if stats is None:
        return
    stats["providers_in_scope"] = len(providers_from_complete_inventory(report))


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
    ERROR_PARTIAL_READ on other read errors, or None on success (missing custom fields are omitted).
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
        return slo, ERROR_PARTIAL_READ
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


def _provider_metadata_read_status(err):
    """Map collect_* error to a serializable inventory read status."""
    if err is None:
        return "ok"
    if err == ERROR_AUTH_DENIED:
        return "auth_denied"
    if err == ERROR_PARTIAL_READ:
        return "partial_read"
    return "error"


def provider_slo_percent_from_inventory(report):
    """Provider SLO snapshot embedded in inventory JSON."""
    raw = (report or {}).get("provider_slo_percent")
    return dict(raw) if isinstance(raw, dict) else {}


def provider_limits_gbps_from_inventory(report):
    """Provider aggregate limit snapshot embedded in inventory JSON."""
    raw = (report or {}).get("provider_limits_gbps")
    return dict(raw) if isinstance(raw, dict) else {}


def inventory_provider_metadata_complete(report):
    """True when embedded provider SLO/limit metadata was read successfully."""
    if not report:
        return False
    return (
        report.get("provider_slo_read") == "ok"
        and report.get("provider_limits_read") == "ok"
        and isinstance(report.get("provider_slo_percent"), dict)
        and isinstance(report.get("provider_limits_gbps"), dict)
    )


def inventory_has_provider_metadata_snapshot(report):
    """True when inventory JSON carries a complete provider metadata snapshot."""
    if not report:
        return False
    return (
        "provider_slo_percent" in report
        and "provider_limits_gbps" in report
        and inventory_provider_metadata_complete(report)
    )


def enrich_inventory_provider_metadata(report, nb, debug=False):
    """Attach provider SLO/limit snapshots and read status to inventory report."""
    slo, slo_err = collect_provider_slo_percent(nb, debug=debug)
    limits, limits_err = collect_provider_limits_gbps(nb, debug=debug)
    report["provider_slo_read"] = _provider_metadata_read_status(slo_err)
    report["provider_limits_read"] = _provider_metadata_read_status(limits_err)
    report["provider_slo_percent"] = slo if slo_err is None else None
    report["provider_limits_gbps"] = limits if limits_err is None else None
    stats = report.get("stats")
    if slo_err == ERROR_AUTH_DENIED or limits_err == ERROR_AUTH_DENIED:
        if stats is not None:
            stats["error"] = ERROR_AUTH_DENIED
    elif slo_err == ERROR_PARTIAL_READ or limits_err == ERROR_PARTIAL_READ:
        _bump_read_error(stats)
    elif slo_err or limits_err:
        _bump_read_error(stats)
    return slo_err, limits_err


def collect_provider_limits_gbps(nb, debug=False):
    """Provider.name -> aggregate_limit_gbps from NetBox custom fields.

    Returns (mapping, error) where error is ERROR_AUTH_DENIED on token/auth failure,
    ERROR_PARTIAL_READ on other read errors, or None on success.
    """
    limits = {}
    try:
        providers = list(nb.circuits.providers.all())
    except Exception as e:
        if is_netbox_auth_error(e):
            if debug:
                print(
                    "NetBox: circuits.providers.all(): auth denied ({})".format(e),
                    file=sys.stderr,
                )
            return limits, ERROR_AUTH_DENIED
        if debug:
            print("NetBox: circuits.providers.all(): {}".format(e), file=sys.stderr)
        return limits, ERROR_PARTIAL_READ
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
    return limits, None


def _empty_netbox_interface_relations():
    return {
        "member_to_aggregate": {},
        "parent_children": {},
        "display_names": {},
    }


def _netbox_interface_relations_is_serialized(data):
    """True when relations are JSON-safe lists, not in-memory tuple-key dicts."""
    if not isinstance(data, dict):
        return False
    member = data.get("member_to_aggregate")
    if member is None:
        return True
    return isinstance(member, list)


def serialize_netbox_interface_relations(relations):
    """Convert in-memory relations (tuple keys, set values) to JSON-safe lists."""
    relations = relations or {}
    member_rows = []
    for (dev_name, member_iface), aggregate in sorted(
        (relations.get("member_to_aggregate") or {}).items()
    ):
        member_rows.append(
            {
                "device": dev_name,
                "member": member_iface,
                "aggregate": aggregate,
            }
        )
    parent_rows = []
    for (dev_name, parent_iface), children in sorted(
        (relations.get("parent_children") or {}).items()
    ):
        parent_rows.append(
            {
                "device": dev_name,
                "parent": parent_iface,
                "children": sorted(children or []),
            }
        )
    display_rows = []
    for (dev_name, iface_norm), display_name in sorted(
        (relations.get("display_names") or {}).items()
    ):
        display_rows.append(
            {
                "device": dev_name,
                "interface": iface_norm,
                "name": display_name,
            }
        )
    return {
        "member_to_aggregate": member_rows,
        "parent_children": parent_rows,
        "display_names": display_rows,
    }


def deserialize_netbox_interface_relations(data):
    """Restore in-memory relations from JSON inventory or serialized dict."""
    if not data:
        return _empty_netbox_interface_relations()
    if not _netbox_interface_relations_is_serialized(data):
        relations = _empty_netbox_interface_relations()
        for key in ("member_to_aggregate", "parent_children", "display_names"):
            source = data.get(key) or {}
            if key == "parent_children":
                relations[key] = {
                    pair: set(children or [])
                    for pair, children in source.items()
                }
            else:
                relations[key] = dict(source)
        return relations

    relations = _empty_netbox_interface_relations()
    for row in data.get("member_to_aggregate") or []:
        dev_name = (row.get("device") or "").strip()
        member_iface = (row.get("member") or "").strip()
        aggregate = (row.get("aggregate") or "").strip()
        if dev_name and member_iface and aggregate:
            relations["member_to_aggregate"][(dev_name, member_iface)] = aggregate
    for row in data.get("parent_children") or []:
        dev_name = (row.get("device") or "").strip()
        parent_iface = (row.get("parent") or "").strip()
        children = row.get("children") or []
        if dev_name and parent_iface:
            relations["parent_children"][(dev_name, parent_iface)] = {
                (child or "").strip() for child in children if (child or "").strip()
            }
    for row in data.get("display_names") or []:
        dev_name = (row.get("device") or "").strip()
        iface_norm = (row.get("interface") or "").strip()
        display_name = (row.get("name") or "").strip()
        if dev_name and iface_norm and display_name:
            relations["display_names"][(dev_name, iface_norm)] = display_name
    return relations


def netbox_interface_relations_from_report(report):
    """Return in-memory relations embedded in an inventory report, or None."""
    if not report:
        return None
    cached = report.get("_netbox_interface_relations")
    if cached is not None:
        return cached
    raw = report.get("netbox_interface_relations")
    if raw is None:
        return None
    cached = deserialize_netbox_interface_relations(raw)
    report["_netbox_interface_relations"] = cached
    return cached


def normalize_inventory_report(report):
    """Deserialize embedded NetBox interface relations after JSON load."""
    if not report:
        return report
    netbox_interface_relations_from_report(report)
    return report


def enrich_inventory_report_with_netbox_relations(report, nb, debug=False):
    """Attach JSON-safe NetBox interface relations to an inventory report."""
    device_names = device_names_from_complete_inventory(report)
    relations = collect_netbox_interface_relations(
        nb, device_names, debug=debug, stats=report.get("stats")
    )
    report["netbox_interface_relations"] = serialize_netbox_interface_relations(relations)
    report["_netbox_interface_relations"] = relations
    return relations


def inventory_report_json_payload(report, **extra):
    """Return a JSON-serializable copy of an inventory report."""
    payload = dict(report)
    payload.pop("_netbox_interface_relations", None)
    payload.update(extra)
    return payload


def load_inventory_report(path):
    """Load inventory JSON report written by netbox_uplinks_inventory.py --json."""
    with open(path, "r", encoding="utf-8") as f:
        report = json.load(f)
    normalize_inventory_report(report)
    return report


def collect_netbox_interface_relations(nb, device_names, debug=False, stats=None):
    """
    Read lag/parent relations from NetBox dcim.interface for scoped devices.
    Return dict with member_to_aggregate and parent_children (normalized keys).
    """
    relations = {
        "member_to_aggregate": {},
        "parent_children": {},
        "display_names": {},
    }
    if not device_names:
        return relations
    for dev_name in sorted(device_names):
        try:
            ifaces = list(nb.dcim.interfaces.filter(device=dev_name))
        except Exception as e:
            _raise_if_netbox_auth(e)
            _bump_read_error(stats)
            if debug:
                print(
                    "dcim.interfaces.filter(device={}): {}".format(dev_name, e),
                    file=sys.stderr,
                )
            continue
        for iface in ifaces:
            name = (getattr(iface, "name", None) or "").strip()
            if not name:
                continue
            name_norm = _normalize_iface_name(name)
            relations["display_names"][(dev_name, name_norm)] = name
            lag = getattr(iface, "lag", None)
            if lag is not None:
                lag_name = (getattr(lag, "name", None) or "").strip()
                if lag_name:
                    lag_norm = _normalize_iface_name(lag_name)
                    relations["member_to_aggregate"][(dev_name, name_norm)] = lag_norm
                    relations["display_names"].setdefault((dev_name, lag_norm), lag_name)
            parent = getattr(iface, "parent", None)
            if parent is not None:
                parent_name = (getattr(parent, "name", None) or "").strip()
                if parent_name:
                    parent_norm = _normalize_iface_name(parent_name)
                    relations["parent_children"].setdefault((dev_name, parent_norm), set()).add(
                        name_norm
                    )
                    relations["display_names"].setdefault((dev_name, parent_norm), parent_name)
    finalize_inventory_read_stats(stats)
    if debug and relations["member_to_aggregate"]:
        print(
            "NetBox interface relations: {} lag members".format(
                len(relations["member_to_aggregate"])
            ),
            file=sys.stderr,
        )
    return relations


def _inventory_alias_ifaces_netbox(dev_name, inv_iface, netbox_relations):
    """Expand inventory iface aliases using NetBox lag/parent relations."""
    inv_norm = _normalize_iface_name(inv_iface)
    aliases = {inv_norm}
    member_to_aggregate = (netbox_relations or {}).get("member_to_aggregate") or {}
    parent_children = (netbox_relations or {}).get("parent_children") or {}
    aggregate = member_to_aggregate.get((dev_name, inv_norm))
    if aggregate:
        aliases.add(aggregate)
    for anchor in list(aliases):
        anchor_norm = _normalize_iface_name(anchor)
        children = parent_children.get((dev_name, anchor_norm), set())
        if not children:
            continue
        picked = _pick_zabbix_iface_from_aliases(children)
        if picked:
            aliases.add(_normalize_iface_name(picked))
    return aliases


def expand_provider_map_for_netbox(inventory_map, netbox_relations, debug=False):
    """Map inventory interfaces to related NetBox names (LAG/logical units)."""
    result = {}
    substituted = []
    for (dev_name, inv_iface), provider in inventory_map.items():
        if not dev_name or not inv_iface or not provider:
            continue
        inv_iface_norm = _normalize_iface_name(inv_iface)
        alias_ifaces = _inventory_alias_ifaces_netbox(dev_name, inv_iface_norm, netbox_relations)
        for alias in alias_ifaces:
            key = _normalize_map_key(dev_name, alias)
            if key not in result:
                result[key] = provider
            if debug and alias != inv_iface_norm:
                substituted.append((dev_name, inv_iface_norm, alias, provider))
    if debug and substituted:
        for dev_name, inv_iface, logical, provider in substituted:
            print(
                "NetBox provider map (relations): {} {} -> {} ({})".format(
                    dev_name, inv_iface, logical, provider
                ),
                file=sys.stderr,
            )
    return result


def map_commit_rates_to_zabbix_ifaces(
    commit_rates, netbox_relations=None, dry_ssh_devices=None, debug=False
):
    """Map inventory (device, physical iface) commit rates to Zabbix-facing interface names."""
    result = {}
    substituted = []
    for (dev_name, iface_name), bps in (commit_rates or {}).items():
        zabbix_iface = _zabbix_iface_from_inventory_iface(
            dev_name,
            iface_name,
            dry_ssh_devices=dry_ssh_devices,
            netbox_relations=netbox_relations,
        )
        if not zabbix_iface:
            continue
        result[(dev_name, zabbix_iface)] = bps
        if _normalize_iface_name(zabbix_iface) != _normalize_iface_name(iface_name):
            substituted.append((dev_name, iface_name, zabbix_iface))
    if debug and substituted:
        for dev_name, inv_iface, zabbix_iface in substituted:
            print(
                "Commit rate for Zabbix: {} {} -> {}".format(
                    dev_name, inv_iface, zabbix_iface
                ),
                file=sys.stderr,
            )
    return result


def scoped_devices_from_inventory_report(report, expanded_provider_map=None):
    """
    Build dry-ssh-like devices dict from complete inventory rows.
    When expanded_provider_map is set, include alias interfaces (e.g. ae5.0).
    """
    devices = {}
    display_by_norm = {}
    for row in report.get("complete") or []:
        dev = (row.get("device") or "").strip()
        iface = (row.get("interface") or "").strip()
        if not dev or not iface:
            continue
        iface_norm = _normalize_iface_name(iface)
        display_by_norm[(dev, iface_norm)] = iface
        devices.setdefault(dev, [])
        if not any(_normalize_iface_name(e.get("name")) == iface_norm for e in devices[dev]):
            devices[dev].append({"name": iface})

    if expanded_provider_map:
        for (dev, iface_norm), _provider in expanded_provider_map.items():
            if not dev or not iface_norm:
                continue
            display = display_by_norm.get((dev, iface_norm)) or iface_norm
            devices.setdefault(dev, [])
            if any(_normalize_iface_name(e.get("name")) == iface_norm for e in devices[dev]):
                continue
            entry = {"name": display}
            if "." in display and display.lower().startswith("ae"):
                entry["isLogical"] = True
            elif display.lower().startswith("ae") and "." not in display:
                entry["isLag"] = True
            devices[dev].append(entry)
    return devices


def _pick_zabbix_iface_from_aliases(aliases):
    """Pick Zabbix-facing interface name from inventory/dry-ssh alias set."""
    if not aliases:
        return None
    with_dot = sorted(name for name in aliases if "." in name)
    for name in with_dot:
        if name.endswith(".0"):
            return name
    without_dot = sorted(name for name in aliases if "." not in name)
    if without_dot:
        return without_dot[0]
    return None


def _dry_ssh_display_name(dev_name, normalized_iface, dry_ssh_devices):
    """Return dry-ssh interface name (original casing) matching normalized iface."""
    if not dry_ssh_devices or not dev_name or not normalized_iface:
        return normalized_iface
    for entry in dry_ssh_devices.get(dev_name) or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if name and _normalize_iface_name(name) == normalized_iface:
            return name
    return normalized_iface


def _zabbix_iface_display_name(dev_name, iface_norm, netbox_relations=None, dry_ssh_devices=None):
    """Resolve normalized iface name to display casing from NetBox or dry-ssh."""
    if netbox_relations:
        display = (netbox_relations.get("display_names") or {}).get((dev_name, iface_norm))
        if display:
            return display
    return _dry_ssh_display_name(dev_name, iface_norm, dry_ssh_devices)


def _zabbix_iface_from_inventory_iface(
    dev_name, inv_iface, dry_ssh_devices=None, netbox_relations=None
):
    """Map inventory cable interface to Zabbix macro/trigger name via NetBox or dry-ssh."""
    if not dev_name or not inv_iface:
        return None
    inv_norm = _normalize_iface_name(inv_iface)
    if netbox_relations:
        aliases = _inventory_alias_ifaces_netbox(dev_name, inv_norm, netbox_relations)
        zabbix_norm = _pick_zabbix_iface_from_aliases(aliases)
        if zabbix_norm:
            return _zabbix_iface_display_name(
                dev_name, zabbix_norm, netbox_relations=netbox_relations, dry_ssh_devices=dry_ssh_devices
            )
        return (inv_iface or "").strip() or None
    if not dry_ssh_devices:
        return (inv_iface or "").strip() or None
    phys_to_logical = _build_physical_to_logical_normalized(dry_ssh_devices)
    member_to_aggregate = _build_member_to_aggregate(dry_ssh_devices)
    aliases = _inventory_alias_ifaces(
        dev_name,
        inv_iface,
        phys_to_logical,
        member_to_aggregate,
    )
    zabbix_norm = _pick_zabbix_iface_from_aliases(aliases)
    if not zabbix_norm:
        return (inv_iface or "").strip() or None
    return _dry_ssh_display_name(dev_name, zabbix_norm, dry_ssh_devices)


def expand_burst_pairs_for_zabbix(
    pairs, dry_ssh_devices=None, netbox_relations=None, debug=False
):
    """Map inventory (device, iface) Burst pairs to Zabbix logical interface names."""
    if not dry_ssh_devices and not netbox_relations:
        return set(pairs)
    expanded = set()
    substituted = []
    for dev_name, inv_iface in pairs:
        zabbix_iface = _zabbix_iface_from_inventory_iface(
            dev_name,
            inv_iface,
            dry_ssh_devices=dry_ssh_devices,
            netbox_relations=netbox_relations,
        )
        if not zabbix_iface:
            continue
        expanded.add((dev_name, zabbix_iface))
        if _normalize_iface_name(zabbix_iface) != _normalize_iface_name(inv_iface):
            substituted.append((dev_name, inv_iface, zabbix_iface))
    if debug and substituted:
        for dev_name, inv_iface, zabbix_iface in substituted:
            print(
                "Burst pair for Zabbix: {} {} -> {}".format(
                    dev_name, inv_iface, zabbix_iface
                ),
                file=sys.stderr,
            )
    return expanded


def expand_burst_metadata_for_zabbix(
    meta, dry_ssh_devices=None, netbox_relations=None, debug=False
):
    """Map inventory Burst metadata keys to Zabbix logical interface names."""
    if not dry_ssh_devices and not netbox_relations:
        return dict(meta)
    expanded = {}
    substituted = []
    for (dev_name, inv_iface), burst_meta in meta.items():
        zabbix_iface = _zabbix_iface_from_inventory_iface(
            dev_name,
            inv_iface,
            dry_ssh_devices=dry_ssh_devices,
            netbox_relations=netbox_relations,
        )
        if not zabbix_iface:
            continue
        expanded[(dev_name, zabbix_iface)] = burst_meta
        if _normalize_iface_name(zabbix_iface) != _normalize_iface_name(inv_iface):
            substituted.append((dev_name, inv_iface, zabbix_iface))
    if debug and substituted:
        for dev_name, inv_iface, zabbix_iface in substituted:
            print(
                "Burst metadata for Zabbix: {} {} -> {}".format(
                    dev_name, inv_iface, zabbix_iface
                ),
                file=sys.stderr,
            )
    return expanded


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


def device_iface_provider_map_from_inventory(
    report, dry_ssh_devices=None, netbox_relations=None, debug=False
):
    """Build (device, interface) -> provider from complete inventory rows."""
    inventory_map = {}
    for row in report.get("complete") or []:
        device_name = row.get("device") or ""
        iface_name = row.get("interface") or ""
        provider = (row.get("provider") or "").strip()
        if device_name and iface_name and provider:
            inventory_map[_normalize_map_key(device_name, iface_name)] = provider
    if netbox_relations is not None:
        return expand_provider_map_for_netbox(inventory_map, netbox_relations, debug=debug)
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


def lag_member_superseded_in_scoped(
    hostname,
    iface_name,
    isp,
    devices,
    device_iface_to_provider=None,
    netbox_relations=None,
):
    """
    True when a LAG member should be omitted from dashboard edges because a related
    aggregate/logical interface is already in scoped inventory with the same provider.
    """
    if not netbox_relations:
        return False
    member_to_aggregate = netbox_relations.get("member_to_aggregate") or {}
    iface_norm = _normalize_iface_name(iface_name)
    if (hostname, iface_norm) not in member_to_aggregate:
        return False
    aliases = _inventory_alias_ifaces_netbox(hostname, iface_norm, netbox_relations)
    superseding = aliases - {iface_norm}
    preferred = _pick_zabbix_iface_from_aliases(superseding)
    if not preferred:
        return False
    scoped_norms = {
        _normalize_iface_name((entry.get("name") or "").strip())
        for entry in (devices.get(hostname) or [])
        if isinstance(entry, dict) and (entry.get("name") or "").strip()
    }
    if preferred not in scoped_norms:
        return False
    preferred_provider = _provider_map_get(
        device_iface_to_provider or {}, hostname, preferred
    )
    return bool(preferred_provider) and preferred_provider == isp


def _iface_is_uplink_by_description(iface):
    """True when an unscoped SSH record contains ``Uplink:`` in its description."""
    desc = (iface.get("description") or "").strip()
    return "Uplink:" in desc


def is_uplink_iface(iface, hostname=None, device_iface_to_provider=None, inventory_scoped=False):
    """
    Uplink filter for map/dashboard/aggregate.

    When inventory_scoped is True (NetBox-first path), only interfaces present in
    scoped inventory count as uplinks (fail-closed).
    When inventory_scoped is False, use the Uplink: description marker.
    """
    if inventory_scoped:
        if not hostname:
            return False
        return iface_has_inventory_entry(hostname, iface, device_iface_to_provider or {})

    return _iface_is_uplink_by_description(iface)


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


def inventory_read_failed(report):
    """True when the NetBox walk hit a read error (auth, providers, or partial)."""
    error = (report.get("stats") or {}).get("error")
    return error in (ERROR_AUTH_DENIED, ERROR_PROVIDERS_UNAVAILABLE, ERROR_PARTIAL_READ)


def arm_netbox_incomplete_guard(stats):
    """Arm Zabbix transport guard from inventory stats.error."""
    from uplinks.zabbix.client import set_incomplete_netbox_data

    error = (stats or {}).get("error")
    if error == ERROR_AUTH_DENIED:
        set_incomplete_netbox_data("authentication refused")
    elif error == ERROR_PROVIDERS_UNAVAILABLE:
        set_incomplete_netbox_data("provider list unavailable")
    else:
        set_incomplete_netbox_data("partial read")


def load_uplink_provider_context(
    dry_ssh_devices=None,
    debug=False,
    border_tag=None,
    circuit_scope=True,
    inventory_report=None,
):
    """
    Read-only NetBox uplink provider context for map/dashboard/aggregate.
    Return dict with device_iface_to_provider, providers (set), stats, read_error; or None
    when NetBox is not configured.

    Uses project circuit scope (Circuit type Uplink + status Active) by default.
    Pass circuit_scope=None to audit all circuits.
    """
    scope = project_circuit_scope() if circuit_scope is True else circuit_scope
    tag = border_tag if border_tag is not None else netbox_border_tag()
    if inventory_report is not None:
        report = inventory_report
        netbox_relations = netbox_interface_relations_from_report(report)
        if netbox_relations is None:
            netbox_relations = _empty_netbox_interface_relations()
    else:
        nb = netbox_client_from_env(debug=debug)
        if nb is None:
            return None
        report = collect_uplink_inventory(
            nb, tag=tag, debug=debug, active_only=True, circuit_scope=scope
        )
        netbox_relations = netbox_interface_relations_from_report(report)
        if netbox_relations is None:
            device_names = device_names_from_complete_inventory(report)
            netbox_relations = collect_netbox_interface_relations(
                nb, device_names, debug=debug, stats=report.get("stats")
            )
    stats = report.get("stats") or {}
    finalize_inventory_read_stats(stats)
    error = stats.get("error")
    read_error = inventory_read_failed(report)
    if error == ERROR_AUTH_DENIED:
        print(
            "Warning: NetBox authentication failed; scoped inventory unavailable, uplinks are not counted",
            file=sys.stderr,
        )
        if debug:
            print("collect_uplink_inventory: {}".format(error), file=sys.stderr)
    elif error == ERROR_PROVIDERS_UNAVAILABLE:
        if debug:
            print(
                "NetBox: scoped inventory unavailable; uplinks are not counted",
                file=sys.stderr,
            )
    elif error == ERROR_PARTIAL_READ:
        print(
            "Warning: NetBox inventory is incomplete ({} read error(s)); "
            "deletions are disabled and the run will fail".format(stats.get("read_errors", 0)),
            file=sys.stderr,
        )

    return {
        "device_iface_to_provider": device_iface_provider_map_from_inventory(
            report,
            dry_ssh_devices=dry_ssh_devices,
            netbox_relations=netbox_relations,
            debug=debug,
        ),
        "providers": providers_from_complete_inventory(report),
        "netbox_interface_relations": netbox_relations,
        "inventory_report": report,
        "stats": stats,
        "read_error": read_error,
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
    border_tag = resolve_border_device_tag(args.tag)
    report = collect_uplink_inventory(
        nb,
        tag=border_tag,
        debug=args.debug,
        circuit_scope=project_circuit_scope(),
    )
    enrich_inventory_report_with_netbox_relations(report, nb, debug=args.debug)
    enrich_inventory_provider_metadata(report, nb, debug=args.debug)
    stats = report.get("stats") or {}
    auth_error = stats.get("error") == ERROR_AUTH_DENIED
    providers_error = stats.get("error") == ERROR_PROVIDERS_UNAVAILABLE

    if args.json:
        payload = inventory_report_json_payload(
            report,
            dry_run=bool(args.dry_run),
            read_only=True,
        )
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

    if inventory_read_failed(report):
        if stats.get("error") == ERROR_PARTIAL_READ:
            print(
                "Error: NetBox inventory is incomplete ({} read error(s))".format(
                    stats.get("read_errors", 0)
                ),
                file=sys.stderr,
            )
        return 1
    incomplete = len(report.get("incomplete") or [])
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
