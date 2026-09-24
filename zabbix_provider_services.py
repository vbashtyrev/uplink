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
    ERROR_PARTIAL_READ,
    ERROR_PROVIDERS_UNAVAILABLE,
    arm_netbox_incomplete_guard,
    burst_circuits_unique_from_inventory,
    collect_provider_limits_gbps,
    collect_provider_slo_percent,
    collect_uplink_inventory,
    inventory_provider_metadata_complete,
    inventory_read_failed,
    load_inventory_report,
    netbox_border_tag,
    netbox_client_from_env,
    project_circuit_scope,
    provider_limits_gbps_from_inventory,
    provider_slo_percent_from_inventory,
    providers_from_complete_inventory,
)
from uplinks_config import PROJECT_PROVIDER_SLO_PERCENT, SLA_EFFECTIVE_DATE_UTC

load_env_file_if_present()


PROVIDER_ROLE = "provider"
BURST_CIRCUIT_ROLE = "burst-circuit"
_NETBOX_AUTH_MESSAGE = (
    "NetBox error: token has expired or access is denied (403). "
    "Check NETBOX_TOKEN and update the token if necessary."
)


def _load_netbox_services_context(debug=False, inventory_report=None):
    """
    Read-only NetBox context for provider/Burst services.
    Return dict with report, providers, burst_circuits, provider_slo_percent,
    provider_limits_gbps, stats, read_error; or None when NetBox is unavailable.
    """
    if inventory_report is not None:
        report = inventory_report
        stats = dict(report.get("stats") or {})
        read_error = inventory_read_failed(report)
        provider_slo_percent = provider_slo_percent_from_inventory(report)
        provider_limits_gbps = provider_limits_gbps_from_inventory(report)
        if not inventory_provider_metadata_complete(report):
            read_error = True
    else:
        nb = netbox_client_from_env(debug=debug)
        if nb is None:
            return None

        scope = project_circuit_scope()
        tag = netbox_border_tag()
        report = collect_uplink_inventory(
            nb, tag=tag, debug=debug, active_only=True, circuit_scope=scope
        )
        stats = report.get("stats") or {}
        error = stats.get("error")
        read_error = inventory_read_failed(report)
        if error == ERROR_AUTH_DENIED:
            if debug:
                print("collect_uplink_inventory: {}".format(error), file=sys.stderr)
        elif error == ERROR_PROVIDERS_UNAVAILABLE:
            if debug:
                print(
                    "NetBox: providers unavailable; scoped inventory incomplete",
                    file=sys.stderr,
                )
        elif error == ERROR_PARTIAL_READ:
            if debug:
                print(
                    "NetBox: partial inventory read ({} error(s))".format(
                        stats.get("read_errors", 0)
                    ),
                    file=sys.stderr,
                )

        provider_slo_percent, slo_error = collect_provider_slo_percent(nb, debug=debug)
        if slo_error:
            read_error = True
            if debug:
                print("collect_provider_slo_percent: {}".format(slo_error), file=sys.stderr)

        provider_limits_gbps, limits_error = collect_provider_limits_gbps(nb, debug=debug)
        if limits_error:
            read_error = True
            if debug:
                print("collect_provider_limits_gbps: {}".format(limits_error), file=sys.stderr)

    return {
        "report": report,
        "providers": providers_from_complete_inventory(report),
        "burst_circuits": burst_circuits_unique_from_inventory(report),
        "provider_slo_percent": provider_slo_percent,
        "provider_limits_gbps": provider_limits_gbps,
        "stats": stats,
        "read_error": read_error,
    }


def _resolve_providers(netbox_ctx, debug=False):
    """Provider names from NetBox inventory snapshot."""
    providers = set()
    if netbox_ctx:
        providers = set(netbox_ctx.get("providers") or [])
        if providers and debug:
            print(
                "Providers from NetBox inventory: {}".format(", ".join(sorted(providers))),
                file=sys.stderr,
            )
    return sorted(providers)


def _resolve_burst_circuits(netbox_ctx, debug=False):
    """Unique Burst circuits from NetBox inventory snapshot."""
    merged = {}
    if netbox_ctx:
        for circuit_id, provider in netbox_ctx.get("burst_circuits") or []:
            merged[circuit_id] = provider
        if merged and debug:
            print(
                "Burst circuits from NetBox inventory: {}".format(len(merged)),
                file=sys.stderr,
            )
    return sorted(merged.items(), key=lambda x: x[0])


def _resolve_provider_slo(provider, netbox_ctx, project_slo=None, debug=False):
    """Per-provider slo_percent from inventory snapshot, else project default."""
    netbox_slo = (netbox_ctx or {}).get("provider_slo_percent") or {}
    if provider in netbox_slo:
        return netbox_slo[provider]
    if project_slo is not None:
        return project_slo
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
            "with SLAs from NetBox or PROJECT_PROVIDER_SLO_PERCENT."
        ),
    )
    parser.add_argument(
        "--inventory-file",
        default=None,
        metavar="FILE",
        help="Scoped inventory JSON from netbox_uplinks_inventory.py --json",
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

    inventory_report = None
    if args.inventory_file:
        try:
            inventory_report = load_inventory_report(args.inventory_file)
        except (OSError, json.JSONDecodeError) as e:
            print(
                "failed to read inventory file {}: {}".format(args.inventory_file, e),
                file=sys.stderr,
            )
            sys.exit(1)

    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("ZABBIX_URL and ZABBIX_TOKEN are required.", file=sys.stderr)
        sys.exit(1)

    ok, err = validate_zabbix_token(url, token, debug=args.debug)
    if not ok:
        print("Authorization error in Zabbix (token): {}".format(err), file=sys.stderr)
        sys.exit(1)

    netbox_ctx = _load_netbox_services_context(
        debug=args.debug,
        inventory_report=inventory_report,
    )
    read_error = bool(netbox_ctx and netbox_ctx.get("read_error"))
    if read_error:
        stats = netbox_ctx.get("stats") or {}
        arm_netbox_incomplete_guard(stats)
        error = stats.get("error")
        if error == ERROR_AUTH_DENIED:
            print(_NETBOX_AUTH_MESSAGE, file=sys.stderr)
        elif error == ERROR_PROVIDERS_UNAVAILABLE:
            print(
                "Warning: NetBox provider list unavailable; scoped inventory incomplete",
                file=sys.stderr,
            )
        elif error == ERROR_PARTIAL_READ:
            print(
                "Warning: NetBox inventory is incomplete ({} read error(s)); "
                "deletions are disabled and the run will fail".format(
                    stats.get("read_errors", 0)
                ),
                file=sys.stderr,
            )
        else:
            print(
                "Warning: NetBox data read did not fully succeed; "
                "deletions are disabled and the run will fail",
                file=sys.stderr,
            )

    providers = _resolve_providers(netbox_ctx, debug=args.debug)
    burst_pairs = _resolve_burst_circuits(netbox_ctx, debug=args.debug)
    if read_error and not providers and not burst_pairs and not args.parent_service:
        sys.exit(1)
    if not providers and not burst_pairs and not args.parent_service:
        print(
            "No providers in NetBox and no Burst circuits; nothing to do.",
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

    project_slo = PROJECT_PROVIDER_SLO_PERCENT

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
            if not read_error:
                err = _delete_legacy_sla_source_service(
                    url, token, provider, debug=args.debug
                )
                if err:
                    print(
                        "Provider {} legacy SLA source cleanup error: {}".format(
                            provider, err
                        ),
                        file=sys.stderr,
                    )
            else:
                print(
                    "Skipped legacy SLA source cleanup for provider {} "
                    "(NetBox data incomplete)".format(provider),
                    file=sys.stderr,
                )

            slo = _resolve_provider_slo(
                provider,
                netbox_ctx,
                project_slo=project_slo,
                debug=args.debug,
            )
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
        slo = _resolve_provider_slo(
            b_provider,
            netbox_ctx,
            project_slo=project_slo,
            debug=args.debug,
        )
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

    if read_error:
        print(
            "Apply was partial: NetBox data read did not fully succeed.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

