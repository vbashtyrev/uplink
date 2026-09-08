#!/usr/bin/env python3
"""Create or update Zabbix services per uplink provider and per-provider SLAs."""

import argparse
import json
import sys

from env_urls import load_env_file_if_present
from zabbix_map import (
    _get_zabbix_url_token,
    zabbix_request,
    validate_zabbix_token,
)
from uplinks.netbox.inventory import (
    ERROR_AUTH_DENIED,
    ERROR_PROVIDERS_UNAVAILABLE,
    burst_circuits_unique_from_inventory,
    collect_provider_limits_gbps,
    collect_provider_slo_percent,
    collect_uplink_inventory,
    netbox_border_tag,
    netbox_client_from_env,
    providers_from_complete_inventory,
)
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX, SLA_EFFECTIVE_DATE_UTC

load_env_file_if_present()


DEFAULT_COMMIT_RATES = "commit_rates.json"
PROVIDER_ROLE = "provider"
BURST_CIRCUIT_ROLE = "burst-circuit"
_NETBOX_AUTH_MESSAGE = (
    "NetBox error: token has expired or access is denied (403). "
    "Check NETBOX_TOKEN and update the token if necessary."
)


def _load_commit_rates(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None
    except json.JSONDecodeError as e:
        return None, "invalid JSON in {}: {}".format(path, e)
    if not isinstance(data, dict):
        return None, "unexpected JSON root in {}".format(path)
    return data, None


def _get_providers_from_limits(commit_rates):
    limits = commit_rates.get("_provider_limits")
    if not isinstance(limits, dict):
        return []
    providers = []
    for name, val in limits.items():
        if not name or val is None:
            continue
        providers.append(str(name).strip())
    return sorted(set(p for p in providers if p))


def _iter_burst_links(commit_rates):
    """Yield (device, iface, provider, circuit_id) for billing_model Burst."""
    for dev_name, ifaces in (commit_rates or {}).items():
        if not isinstance(dev_name, str) or dev_name.startswith("_"):
            continue
        if not isinstance(ifaces, dict):
            continue
        for iface_name, entry in ifaces.items():
            if not isinstance(entry, dict):
                continue
            if (entry.get("billing_model") or "").strip().lower() != "burst":
                continue
            cid = (entry.get("circuit_id") or "").strip()
            prov = (entry.get("provider") or "").strip()
            if not cid or not prov:
                continue
            yield dev_name, iface_name, prov, cid


def _burst_circuits_unique(commit_rates):
    """Unique circuit_id -> provider (first encountered)."""
    out = {}
    for _dev, _iface, prov, cid in _iter_burst_links(commit_rates):
        if cid not in out:
            out[cid] = prov
    return sorted(out.items(), key=lambda x: x[0])


def _get_global_provider_sla(commit_rates):
    """Return global target SLA (float) from commit_rates['_provider_sla'] or None."""
    val = commit_rates.get("_provider_sla")
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return None


def _load_netbox_services_context(debug=False):
    """
    Read-only NetBox context for provider/Burst services.
    Return dict with report, providers, burst_circuits, provider_slo_percent,
    provider_limits_gbps; or None when NetBox is unavailable.
    """
    nb = netbox_client_from_env(debug=debug)
    if nb is None:
        return None

    tag = netbox_border_tag()
    report = collect_uplink_inventory(nb, tag=tag, debug=debug, active_only=True)
    stats = report.get("stats") or {}
    error = stats.get("error")
    if error == ERROR_AUTH_DENIED:
        if debug:
            print("collect_uplink_inventory: {}".format(error), file=sys.stderr)
        return {"auth_denied": True}
    if error == ERROR_PROVIDERS_UNAVAILABLE:
        if debug:
            print(
                "NetBox: providers unavailable; falling back to commit_rates.json",
                file=sys.stderr,
            )
        return None

    provider_slo_percent, slo_error = collect_provider_slo_percent(nb, debug=debug)
    if slo_error == ERROR_AUTH_DENIED:
        if debug:
            print("collect_provider_slo_percent: {}".format(slo_error), file=sys.stderr)
        return {"auth_denied": True}

    return {
        "report": report,
        "providers": providers_from_complete_inventory(report),
        "burst_circuits": burst_circuits_unique_from_inventory(report),
        "provider_slo_percent": provider_slo_percent,
        "provider_limits_gbps": collect_provider_limits_gbps(nb, debug=debug),
    }


def _resolve_providers(netbox_ctx, commit_rates, debug=False):
    """Provider names: NetBox inventory first, per-provider _provider_limits fallback."""
    commit_rates = commit_rates or {}
    providers = set()
    netbox_available = bool(netbox_ctx) and not netbox_ctx.get("auth_denied")
    if netbox_available:
        providers = set(netbox_ctx.get("providers") or [])
        if providers and debug:
            print(
                "Providers from NetBox inventory: {}".format(", ".join(sorted(providers))),
                file=sys.stderr,
            )

    json_providers = _get_providers_from_limits(commit_rates)
    added = []
    for provider in json_providers:
        if provider not in providers:
            providers.add(provider)
            added.append(provider)

    if added:
        if netbox_available:
            for provider in added:
                print(
                    "Warning: provider {!r} not in NetBox inventory; "
                    "using commit_rates.json _provider_limits (transition fallback)".format(
                        provider
                    ),
                    file=sys.stderr,
                )
        else:
            print(
                "Warning: no providers in NetBox inventory; "
                "using commit_rates.json _provider_limits (transition fallback)",
                file=sys.stderr,
            )
        if debug:
            print(
                "Transition fallback providers from _provider_limits: {}".format(
                    ", ".join(added)
                ),
                file=sys.stderr,
            )
    return sorted(providers)


def _resolve_burst_circuits(netbox_ctx, commit_rates, debug=False):
    """Unique Burst circuits: NetBox inventory first, per-circuit commit_rates.json fallback."""
    commit_rates = commit_rates or {}
    merged = {}
    netbox_available = bool(netbox_ctx) and not netbox_ctx.get("auth_denied")
    if netbox_available:
        for circuit_id, provider in netbox_ctx.get("burst_circuits") or []:
            merged[circuit_id] = provider
        if merged and debug:
            print(
                "Burst circuits from NetBox inventory: {}".format(len(merged)),
                file=sys.stderr,
            )

    json_pairs = dict(_burst_circuits_unique(commit_rates))
    added = []
    for circuit_id, provider in json_pairs.items():
        if circuit_id not in merged:
            merged[circuit_id] = provider
            added.append(circuit_id)

    if added:
        if netbox_available:
            for circuit_id in added:
                print(
                    "Warning: Burst circuit {!r} not in NetBox inventory; "
                    "using commit_rates.json billing_model (transition fallback)".format(
                        circuit_id
                    ),
                    file=sys.stderr,
                )
        else:
            print(
                "Warning: no Burst billing_model in NetBox inventory; "
                "using commit_rates.json billing_model (transition fallback)",
                file=sys.stderr,
            )
        if debug:
            print(
                "Burst circuits from commit_rates.json: {}".format(len(added)),
                file=sys.stderr,
            )
    return sorted(merged.items(), key=lambda x: x[0])


def _resolve_provider_slo(provider, netbox_ctx, global_slo, debug=False):
    """Per-provider slo_percent from NetBox, else global _provider_sla fallback."""
    netbox_slo = (netbox_ctx or {}).get("provider_slo_percent") or {}
    if provider in netbox_slo:
        return netbox_slo[provider]
    if global_slo is not None:
        if netbox_ctx is not None and provider in (netbox_ctx.get("providers") or set()):
            print(
                "Warning: provider {!r} has no slo_percent in NetBox; "
                "using commit_rates.json _provider_sla (transition fallback)".format(provider),
                file=sys.stderr,
            )
            if debug:
                print(
                    "Transition fallback SLA for {} from _provider_sla: {:.4f}%".format(
                        provider, global_slo
                    ),
                    file=sys.stderr,
                )
        return global_slo
    return None


def _get_or_create_parent_service(url, token, name, debug=False):
    """Return parent serviceid by name, create if missing."""
    if not name:
        return None, None
    res, err = zabbix_request(
        url,
        token,
        "service.get",
        {
            "output": ["serviceid", "name"],
            "filter": {"name": [name]},
        },
        debug=debug,
    )
    if err:
        return None, "service.get (parent): {}".format(err)
    if res:
        return res[0]["serviceid"], None

    payload = {
        "name": name,
        "algorithm": 1,
        "sortorder": 1,
    }
    result, err = zabbix_request(url, token, "service.create", payload, debug=debug)
    if err:
        return None, "service.create (parent): {}".format(err)
    return result["serviceids"][0], None


def _get_or_create_provider_service(url, token, provider, parentid, debug=False):
    """Create or update service for a single provider."""
    service_name = "Uplinks {}".format(provider)
    problem_tags = [
        {
            "tag": "provider",
            "value": provider,
            "operator": 0,  # Equals
        },
        {
            "tag": "sla",
            "value": "true",
            "operator": 0,  # Equals
        },
    ]
    tags = [
        {
            "tag": "domain",
            "value": "uplinks",
        },
        {
            "tag": "role",
            "value": PROVIDER_ROLE,
        },
        {
            "tag": "provider",
            "value": provider,
        },
    ]

    # Try to find existing service by name.
    res, err = zabbix_request(
        url,
        token,
        "service.get",
        {
            "output": ["serviceid", "name"],
            "filter": {"name": [service_name]},
            "selectParents": ["serviceid"],
        },
        debug=debug,
    )
    if err:
        return None, "service.get: {}".format(err)

    if res:
        serviceid = res[0]["serviceid"]
        payload = {
            "serviceid": serviceid,
            "name": service_name,
            "problem_tags": problem_tags,
            "tags": tags,
        }
        # Optionally ensure parent link if parentid is given and not already set.
        if parentid:
            existing_parents = res[0].get("parents") or []
            has_parent = any(p.get("serviceid") == str(parentid) for p in existing_parents)
            if not has_parent:
                payload["parents"] = [{"serviceid": str(parentid)}]
        _, err = zabbix_request(url, token, "service.update", payload, debug=debug)
        if err:
            return None, "service.update: {}".format(err)
        return serviceid, None

    # Create new leaf service for this provider.
    payload = {
        "name": service_name,
        "algorithm": 0,  # leaf (no children) – status taken from its own problems
        "sortorder": 1,
        "problem_tags": problem_tags,
        "tags": tags,
    }
    if parentid:
        payload["parents"] = [{"serviceid": str(parentid)}]

    result, err = zabbix_request(url, token, "service.create", payload, debug=debug)
    if err:
        return None, "service.create: {}".format(err)
    return result["serviceids"][0], None


def _delete_legacy_sla_source_service(url, token, provider, debug=False):
    """Delete the old SLA source auxiliary service, if left after migration."""
    service_name = "Uplinks {} SLA source".format(provider)
    res, err = zabbix_request(
        url,
        token,
        "service.get",
        {"output": ["serviceid"], "filter": {"name": [service_name]}},
        debug=debug,
    )
    if err:
        return "service.get (legacy SLA source): {}".format(err)
    if not res:
        return None
    ids = [s["serviceid"] for s in res if s.get("serviceid")]
    if not ids:
        return None
    _, err = zabbix_request(url, token, "service.delete", ids, debug=debug)
    if err:
        return "service.delete (legacy SLA source): {}".format(err)
    return None


def _get_or_create_burst_circuit_service(url, token, provider, circuit_id, parentid, debug=False):
    """Service for one Burst circuit: problems with circuit + sla + billing=burst."""
    service_name = "Uplinks Burst {}".format(circuit_id)
    problem_tags = [
        {"tag": "circuit", "value": circuit_id, "operator": 0},
        {"tag": "sla", "value": "true", "operator": 0},
        {"tag": "billing", "value": "burst", "operator": 0},
    ]
    tags = [
        {"tag": "domain", "value": "uplinks"},
        {"tag": "role", "value": BURST_CIRCUIT_ROLE},
        {"tag": "circuit", "value": circuit_id},
    ]
    res, err = zabbix_request(
        url,
        token,
        "service.get",
        {
            "output": ["serviceid", "name"],
            "filter": {"name": [service_name]},
            "selectParents": ["serviceid"],
        },
        debug=debug,
    )
    if err:
        return None, "service.get: {}".format(err)
    if res:
        serviceid = res[0]["serviceid"]
        payload = {
            "serviceid": serviceid,
            "name": service_name,
            "problem_tags": problem_tags,
            "tags": tags,
        }
        if parentid:
            existing_parents = res[0].get("parents") or []
            has_parent = any(p.get("serviceid") == str(parentid) for p in existing_parents)
            if not has_parent:
                payload["parents"] = [{"serviceid": str(parentid)}]
        _, err = zabbix_request(url, token, "service.update", payload, debug=debug)
        if err:
            return None, "service.update: {}".format(err)
        return serviceid, None
    payload = {
        "name": service_name,
        "algorithm": 0,
        "sortorder": 1,
        "problem_tags": problem_tags,
        "tags": tags,
    }
    if parentid:
        payload["parents"] = [{"serviceid": str(parentid)}]
    result, err = zabbix_request(url, token, "service.create", payload, debug=debug)
    if err:
        return None, "service.create: {}".format(err)
    return result["serviceids"][0], None


def _ensure_burst_circuit_sla(url, token, circuit_id, slo, debug=False):
    """SLA for the Uplinks Burst service {circuit_id} (match by circuit + role tags)."""
    if slo is None:
        return None, None
    sla_name = "Uplinks Burst {} SLA".format(circuit_id)
    res, err = zabbix_request(
        url,
        token,
        "sla.get",
        {
            "output": ["slaid", "name", "slo", "period", "timezone", "status"],
            "filter": {"name": [sla_name]},
        },
        debug=debug,
    )
    if err:
        return None, "sla.get: {}".format(err)
    payload = {
        "name": sla_name,
        "slo": float(slo),
        "period": 1,
        "timezone": "UTC",
        "status": 1,
        "effective_date": SLA_EFFECTIVE_DATE_UTC,
        "schedule": [],
        "service_tags": [
            {"tag": "circuit", "operator": 0, "value": circuit_id},
        ],
    }
    if res:
        slaid = res[0]["slaid"]
        payload["slaid"] = slaid
        _, err = zabbix_request(url, token, "sla.update", payload, debug=debug)
        if err:
            return None, "sla.update: {}".format(err)
        return slaid, None
    result, err = zabbix_request(url, token, "sla.create", [payload], debug=debug)
    if err:
        return None, "sla.create: {}".format(err)
    sla_ids = result.get("slaids") or []
    slaid = sla_ids[0] if sla_ids else None
    return slaid, None


def _ensure_provider_sla(url, token, provider, slo, debug=False):
    """Create or update SLA for a single provider."""

    if slo is None:
        return None, None

    sla_name = "Uplinks {} SLA".format(provider)

    # Try to find existing SLA by name.
    res, err = zabbix_request(
        url,
        token,
        "sla.get",
        {
            "output": ["slaid", "name", "slo", "period", "timezone", "status"],
            "filter": {"name": [sla_name]},
        },
        debug=debug,
    )
    if err:
        return None, "sla.get: {}".format(err)

    payload = {
        "name": sla_name,
        "slo": float(slo),
        "period": 1,  # weekly
        "timezone": "UTC",
        "status": 1,
        "effective_date": SLA_EFFECTIVE_DATE_UTC,
        "schedule": [],  # 24x7
        "service_tags": [
            {
                "tag": "provider",
                "operator": 0,
                "value": provider,
            },
        ],
    }

    if res:
        slaid = res[0]["slaid"]
        payload["slaid"] = slaid
        _, err = zabbix_request(url, token, "sla.update", payload, debug=debug)
        if err:
            return None, "sla.update: {}".format(err)
        return slaid, None

    # Create new SLA with fixed effective date (start of month).
    result, err = zabbix_request(url, token, "sla.create", [payload], debug=debug)
    if err:
        return None, "sla.create: {}".format(err)
    sla_ids = result.get("slaids") or []
    slaid = sla_ids[0] if sla_ids else None
    return slaid, None


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Create/update Zabbix services: per-provider (aggregate) and per Burst circuit, "
            "with optional SLAs from _provider_sla."
        ),
    )
    parser.add_argument(
        "-f",
        "--commit-rates",
        default=DEFAULT_COMMIT_RATES,
        help="Path to commit_rates.json (for _provider_limits / provider list).",
    )
    parser.add_argument(
        "--parent-service",
        default=None,
        help="Optional parent service name for all provider services (created if missing).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose Zabbix API debug output.",
    )
    args = parser.parse_args()

    commit_rates, err = _load_commit_rates(args.commit_rates)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)
    commit_rates = commit_rates or {}

    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("ZABBIX_URL and ZABBIX_TOKEN are required.", file=sys.stderr)
        sys.exit(1)

    ok, err = validate_zabbix_token(url, token, debug=args.debug)
    if not ok:
        print("Authorization error in Zabbix (token): {}".format(err), file=sys.stderr)
        sys.exit(1)

    netbox_ctx = _load_netbox_services_context(debug=args.debug)
    if netbox_ctx and netbox_ctx.get("auth_denied"):
        print(_NETBOX_AUTH_MESSAGE, file=sys.stderr)
        sys.exit(1)

    providers = _resolve_providers(netbox_ctx, commit_rates, debug=args.debug)
    burst_pairs = _resolve_burst_circuits(netbox_ctx, commit_rates, debug=args.debug)
    if not providers and not burst_pairs and not args.parent_service:
        print(
            "No providers in NetBox/_provider_limits and no Burst circuits; nothing to do.",
            file=sys.stderr,
        )
        sys.exit(0)
    if not providers:
        print("No providers found; skipping aggregate services.", file=sys.stderr)
    if not burst_pairs:
        print("No Burst circuits; skipping Burst services.", file=sys.stderr)
    parentid = None
    if args.parent_service:
        parentid, err = _get_or_create_parent_service(
            url, token, args.parent_service, debug=args.debug
        )
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)

    global_slo = _get_global_provider_sla(commit_rates)

    if providers:
        for provider in providers:
            if not provider:
                continue
            serviceid, err = _get_or_create_provider_service(
                url, token, provider, parentid, debug=args.debug
            )
            if err:
                print("Provider {}: {}".format(provider, err), file=sys.stderr)
                continue
            print("OK: service for provider {} (serviceid={})".format(provider, serviceid))
            err = _delete_legacy_sla_source_service(url, token, provider, debug=args.debug)
            if err:
                print("Provider {} legacy SLA source cleanup error: {}".format(provider, err), file=sys.stderr)

            slo = _resolve_provider_slo(provider, netbox_ctx, global_slo, debug=args.debug)
            if slo is not None:
                slaid, err = _ensure_provider_sla(url, token, provider, slo, debug=args.debug)
                if err:
                    print("Provider {} SLA error: {}".format(provider, err), file=sys.stderr)
                    continue
                print(
                    "OK: SLA for provider {} (slaid={}, slo={:.4f}%)".format(
                        provider, slaid, slo
                    )
                )

    for circuit_id, b_provider in burst_pairs:
        serviceid, err = _get_or_create_burst_circuit_service(
            url, token, b_provider, circuit_id, parentid, debug=args.debug
        )
        if err:
            print("Burst {}: {}".format(circuit_id, err), file=sys.stderr)
            continue
        print(
            "OK: Burst service for circuit {} (serviceid={})".format(
                circuit_id, serviceid
            )
        )
        slo = _resolve_provider_slo(b_provider, netbox_ctx, global_slo, debug=args.debug)
        if slo is not None:
            slaid, err = _ensure_burst_circuit_sla(
                url, token, circuit_id, slo, debug=args.debug
            )
            if err:
                print(
                    "Burst {} SLA error: {}".format(circuit_id, err),
                    file=sys.stderr,
                )
                continue
            print(
                "OK: SLA for Burst {} (slaid={}, slo={:.4f}%)".format(
                    circuit_id, slaid, slo
                )
            )


if __name__ == "__main__":
    main()

