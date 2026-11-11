#!/usr/bin/env python3
"""Read-only NetBox collector for DCI circuits (side A/Z -> cable -> device interface)."""

import sys

from uplinks_config import DCI_CIRCUIT_TYPE, DCI_TAG_SLUG
from uplinks.netbox.inventory import (
    NetBoxAuthError,
    _bump_read_error,
    _circuit_status_value,
    _normalize_choice,
    _provider_name,
    _raise_if_netbox_auth,
    _record_has_tag,
    _resolve_device,
    _resolve_provider,
    circuit_matches_scope,
    interface_from_cable,
    is_netbox_auth_error,
    project_circuit_scope,
)

SIDE_REASON_NOT_EXACTLY_ONE_TERM = "нет ровно одного окончания"
SIDE_REASON_NO_CABLE_ID = "нет номера кабеля"
SIDE_REASON_CABLE_NOT_FOUND = "кабель не найден"
SIDE_REASON_NO_IFACE_VIA_CABLE = "интерфейс через кабель не найден"
SIDE_REASON_DEVICE_NOT_FOUND = "устройство не найдено"
SIDE_REASON_DEVICE_EMPTY = "устройство пусто"
SIDE_REASON_IFACE_EMPTY = "интерфейс пуст"
SIDE_REASON_IFACE_DISABLED = "интерфейс выключен"
SIDE_REASON_NO_DCI_TAG = "нет метки dci"

COMMON_REASON_NOT_ACTIVE = "статус не active"
COMMON_REASON_CID_EMPTY = "cid пуст"
COMMON_REASON_PROVIDER_EMPTY = "провайдер пуст"

SIDE_REASON_ORDER = [
    SIDE_REASON_NOT_EXACTLY_ONE_TERM,
    SIDE_REASON_NO_CABLE_ID,
    SIDE_REASON_CABLE_NOT_FOUND,
    SIDE_REASON_NO_IFACE_VIA_CABLE,
    SIDE_REASON_DEVICE_NOT_FOUND,
    SIDE_REASON_DEVICE_EMPTY,
    SIDE_REASON_IFACE_EMPTY,
    SIDE_REASON_IFACE_DISABLED,
    SIDE_REASON_NO_DCI_TAG,
]

COMMON_REASON_ORDER = [
    COMMON_REASON_NOT_ACTIVE,
    COMMON_REASON_CID_EMPTY,
    COMMON_REASON_PROVIDER_EMPTY,
]

DASH_PLACEHOLDER = "—"


def dci_circuit_scope(circuit_type=None):
    """Project scope for DCI circuit type (name/slug)."""
    if circuit_type is None:
        circuit_type = DCI_CIRCUIT_TYPE
    return project_circuit_scope(circuit_type)


def _is_dci_circuit(circuit, scope):
    """
    True when the circuit is in DCI scope using only the embedded type object.

    Circuits with empty type (with or without type_id) are out of scope and must
    not trigger a circuit_types.get lookup.
    """
    if getattr(circuit, "type", None) is None:
        return False
    return circuit_matches_scope(circuit, scope, nb=None, stats=None)


def _terminations_for_side(terminations, side_letter):
    """Return terminations whose normalized side equals side_letter (strict ==)."""
    matched = []
    for term in terminations:
        raw = getattr(term, "term_side", None)
        if not raw:
            raw = getattr(term, "termination_side", None)
        norm = _normalize_choice(raw)
        if norm == side_letter:
            matched.append(term)
    return matched


def _is_interface_enabled(iface):
    enabled = getattr(iface, "enabled", None)
    if enabled is None:
        return True
    if isinstance(enabled, bool):
        return enabled
    return str(enabled).lower() in ("true", "1", "yes", "active")


def _region_name_from_termination(term):
    """Region name from circuit termination's site (termination.region.name)."""
    if term is None:
        return ""
    site = getattr(term, "termination", None)
    if site is None:
        return ""
    region = getattr(site, "region", None)
    if region is None:
        return ""
    name = getattr(region, "name", None)
    if name is None:
        return ""
    return str(name).strip()


def _channel_region_name(term_a, term_z):
    """Prefer region from A side, then Z."""
    for term in (term_a, term_z):
        name = _region_name_from_termination(term)
        if name:
            return name
    return ""


def _cable_id_from_termination(term):
    cable = getattr(term, "cable", None)
    if cable is None:
        return None
    if isinstance(cable, int):
        return cable
    cid = getattr(cable, "id", None)
    if cid is not None:
        return cid
    try:
        return int(cable)
    except (TypeError, ValueError):
        return None


def _resolve_side(nb, term, stats, tag_slug, debug=False):
    """Trace one circuit termination to device/interface; return side dict and reason codes."""
    reasons = []
    device_name = ""
    iface_name = ""
    if term is None:
        reasons.append(SIDE_REASON_NOT_EXACTLY_ONE_TERM)
        return device_name, iface_name, reasons

    cable_id = _cable_id_from_termination(term)
    if not cable_id:
        reasons.append(SIDE_REASON_NO_CABLE_ID)
        return device_name, iface_name, reasons

    try:
        cable_obj = nb.dcim.cables.get(cable_id)
    except Exception as e:
        _raise_if_netbox_auth(e)
        _bump_read_error(stats)
        if debug:
            print("dcim.cables.get({}): {}".format(cable_id, e), file=sys.stderr)
        cable_obj = None
    if not cable_obj:
        reasons.append(SIDE_REASON_CABLE_NOT_FOUND)
        return device_name, iface_name, reasons

    ct_id = getattr(term, "id", None)
    iface = interface_from_cable(
        nb,
        cable_obj,
        cable_id=cable_id,
        circuit_termination_id=ct_id,
        debug=debug,
        stats=stats,
    )
    if not iface:
        reasons.append(SIDE_REASON_NO_IFACE_VIA_CABLE)
        return device_name, iface_name, reasons

    iface_name = getattr(iface, "name", None) or ""
    if not iface_name:
        reasons.append(SIDE_REASON_IFACE_EMPTY)
    elif not _is_interface_enabled(iface):
        reasons.append(SIDE_REASON_IFACE_DISABLED)

    device = _resolve_device(nb, iface, stats=stats)
    if not device:
        reasons.append(SIDE_REASON_DEVICE_NOT_FOUND)
    else:
        device_name = getattr(device, "name", None) or ""
        if not device_name:
            reasons.append(SIDE_REASON_DEVICE_EMPTY)

    if iface and not _record_has_tag(iface, tag_slug):
        reasons.append(SIDE_REASON_NO_DCI_TAG)

    return device_name, iface_name, reasons


def _format_end(device, iface):
    if device and iface:
        return "{}:{}".format(device, iface)
    if device:
        return "{}:{}".format(device, DASH_PLACEHOLDER)
    if iface:
        return "{}:{}".format(DASH_PLACEHOLDER, iface)
    return DASH_PLACEHOLDER


def _ordered_reasons(side_a_reasons, side_z_reasons, common_reasons):
    parts = []
    for reason in SIDE_REASON_ORDER:
        if reason in side_a_reasons:
            parts.append("A: {}".format(reason))
    for reason in SIDE_REASON_ORDER:
        if reason in side_z_reasons:
            parts.append("Z: {}".format(reason))
    for reason in COMMON_REASON_ORDER:
        if reason in common_reasons:
            parts.append(reason)
    return parts


def format_channel_line(channel):
    """One stdout line for a DCI channel (full or incomplete)."""
    label = "ПОЛНАЯ" if channel.get("complete") else "НЕПОЛНАЯ"
    cid = channel.get("cid") or DASH_PLACEHOLDER
    provider = channel.get("provider") or DASH_PLACEHOLDER
    a_end = _format_end(channel.get("a_device"), channel.get("a_iface"))
    z_end = _format_end(channel.get("z_device"), channel.get("z_iface"))
    line = "{} cid={} provider={} A={} Z={}".format(label, cid, provider, a_end, z_end)
    if not channel.get("complete"):
        reason_text = channel.get("reason_text") or ""
        if reason_text:
            line += " ({})".format(reason_text)
    return line


def find_duplicate_cid_provider(channels):
    """Return (cid, provider) duplicate key or None."""
    seen = {}
    for ch in channels:
        cid = (ch.get("cid") or "").strip()
        provider = (ch.get("provider") or "").strip()
        if not cid:
            continue
        key = (cid, provider)
        if key in seen:
            return key
        seen[key] = True
    return None


def collect_dci_channels(nb, debug=False, circuit_type=None, tag_slug=None):
    """
    Walk circuits.all(), collect DCI-type channels with A/Z ends.

    Return dict: channels (non-ignored DCI rows), stats, fatal (bool), auth_error (bool).
    """
    if circuit_type is None:
        circuit_type = DCI_CIRCUIT_TYPE
    if tag_slug is None:
        tag_slug = DCI_TAG_SLUG
    scope = dci_circuit_scope(circuit_type)
    stats = {"read_errors": 0}
    channels = []
    fatal = False
    auth_error = False

    try:
        circuits = list(nb.circuits.circuits.all())
    except NetBoxAuthError:
        raise
    except Exception as e:
        if is_netbox_auth_error(e):
            raise NetBoxAuthError(str(e)) from e
        stats["read_errors"] += 1
        fatal = True
        if debug:
            print("circuits.all(): {}".format(e), file=sys.stderr)
        return {
            "channels": channels,
            "stats": stats,
            "fatal": True,
            "auth_error": False,
        }

    stop_netbox = False
    for circuit in circuits:
        if stop_netbox:
            break
        if stats.get("read_errors", 0) > 0:
            stop_netbox = True
            break

        if not _is_dci_circuit(circuit, scope):
            continue

        cid = (getattr(circuit, "cid", None) or "").strip()
        status_raw = _circuit_status_value(circuit)
        status_lower = str(status_raw or "").lower()

        provider_obj = _resolve_provider(nb, circuit, stats=stats)
        if stats.get("read_errors", 0) > 0:
            stop_netbox = True
            break
        provider_name = _provider_name(provider_obj).strip()

        common_reasons = []
        if status_lower != "active":
            common_reasons.append(COMMON_REASON_NOT_ACTIVE)
        if not cid:
            common_reasons.append(COMMON_REASON_CID_EMPTY)
        if not provider_name:
            common_reasons.append(COMMON_REASON_PROVIDER_EMPTY)

        side_a_reasons = []
        side_z_reasons = []
        a_device, a_iface, z_device, z_iface = "", "", "", ""

        circuit_pk = getattr(circuit, "id", None)
        terminations = []
        if circuit_pk is not None:
            try:
                terminations = list(
                    nb.circuits.circuit_terminations.filter(circuit_id=circuit_pk)
                )
            except Exception as e:
                _raise_if_netbox_auth(e)
                _bump_read_error(stats)
                if debug:
                    print(
                        "circuit_terminations.filter(circuit_id={}): {}".format(
                            circuit_pk, e
                        ),
                        file=sys.stderr,
                    )
                fatal = True
                stop_netbox = True
                terminations = []

        if stats.get("read_errors", 0) > 0:
            stop_netbox = True

        terms_a = _terminations_for_side(terminations, "A")
        terms_z = _terminations_for_side(terminations, "Z")

        term_a = None
        term_z = None
        if len(terms_a) != 1:
            side_a_reasons.append(SIDE_REASON_NOT_EXACTLY_ONE_TERM)
        else:
            term_a = terms_a[0]
            a_device, a_iface, side_a_reasons = _resolve_side(
                nb, term_a, stats, tag_slug, debug=debug
            )

        if len(terms_z) != 1:
            side_z_reasons.append(SIDE_REASON_NOT_EXACTLY_ONE_TERM)
        else:
            term_z = terms_z[0]
            z_device, z_iface, side_z_reasons = _resolve_side(
                nb, term_z, stats, tag_slug, debug=debug
            )

        if stats.get("read_errors", 0) > 0:
            stop_netbox = True

        all_reasons = _ordered_reasons(side_a_reasons, side_z_reasons, common_reasons)
        complete = not all_reasons
        reason_text = "; ".join(all_reasons)
        region_name = _channel_region_name(term_a, term_z)

        channels.append(
            {
                "cid": cid,
                "provider": provider_name,
                "status": status_raw,
                "complete": complete,
                "a_device": a_device,
                "a_iface": a_iface,
                "z_device": z_device,
                "z_iface": z_iface,
                "region": region_name,
                "reason_text": reason_text,
                "circuit_id": circuit_pk,
            }
        )

    if stats.get("read_errors", 0) > 0:
        fatal = True

    return {
        "channels": channels,
        "stats": stats,
        "fatal": fatal,
        "auth_error": auth_error,
    }
