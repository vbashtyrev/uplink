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
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX, SLA_EFFECTIVE_DATE_UTC

load_env_file_if_present()


DEFAULT_COMMIT_RATES = "commit_rates.json"
PROVIDER_ROLE = "provider"
BURST_CIRCUIT_ROLE = "burst-circuit"


def _load_commit_rates(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "file not found: {}".format(path)
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

    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("ZABBIX_URL and ZABBIX_TOKEN are required.", file=sys.stderr)
        sys.exit(1)

    ok, err = validate_zabbix_token(url, token, debug=args.debug)
    if not ok:
        print("Authorization error in Zabbix (token): {}".format(err), file=sys.stderr)
        sys.exit(1)

    providers = _get_providers_from_limits(commit_rates)
    burst_pairs = _burst_circuits_unique(commit_rates)
    if not providers and not burst_pairs and not args.parent_service:
        print(
            "No _provider_limits entries and no Burst circuits (billing_model=burst); nothing to do.",
            file=sys.stderr,
        )
        sys.exit(0)
    if not providers:
        print("No providers in _provider_limits; skipping aggregate services.", file=sys.stderr)
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

    slo = _get_global_provider_sla(commit_rates)

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

