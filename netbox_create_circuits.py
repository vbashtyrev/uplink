#!/usr/bin/env python3
"""
Создание circuits в NetBox по commit_rates.json.
Пример: только локация ALA (--location ALA).
Для каждой пары (устройство, интерфейс): провайдер и circuit type создаются при отсутствии,
circuit — по circuit_id, termination (A) привязывается к site устройства, кабель — к интерфейсу.
Переменные: NETBOX_URL, NETBOX_TOKEN, NETBOX_TAG.
"""

import argparse
import json
import os
import sys

import pynetbox
import requests

from netbox_checks import resolve_interface
from uplinks_config import NETBOX_AUTOMATION_TAG as AUTOMATION_TAG

DEFAULT_COMMIT_RATES = "commit_rates.json"
DEFAULT_DRY_SSH = "dry-ssh.json"
CIRCUIT_TYPE_DEFAULT = "Internet"
CIRCUIT_STATUS_ACTIVE = "active"


def load_commit_rates(path):
    """Загрузить commit_rates.json. Возврат dict или (None, error)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "файл не найден: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "ошибка JSON: {}".format(e)
    return {k: v for k, v in data.items() if not k.startswith("_")}, None


def load_dry_ssh(path):
    """Загрузить dry-ssh.json для маппинга логический интерфейс -> физический. Возврат devices dict или None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data.get("devices") or None


def _get_or_create_automation_tag(nb):
    """
    Найти или создать в NetBox тег с именем/slug AUTOMATION_TAG.
    Возврат: объект тега (pynetbox Record) или None.
    """
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
    """
    Добавить тег tag_obj к объекту NetBox, если его ещё нет.
    При PATCH NetBox ожидает список ID тегов. endpoint — например nb.circuits.providers.
    """
    if not record or not tag_obj:
        return
    tag_id = getattr(tag_obj, "id", None)
    if not tag_id:
        return
    rid = getattr(record, "id", None)
    if not rid:
        return
    try:
        # Перезагрузка по id, чтобы в ответе были теги (filter часто их не возвращает)
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
    """
    Для виртуального/логического интерфейса (ae5.0 и т.п.) вернуть физический из dry-ssh.
    Иначе вернуть iface_name как есть.
    """
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
    """Первый сегмент до дефиса."""
    parts = (hostname or "").split("-")
    return parts[0] if parts and parts[0] else ""


def get_or_create_provider(nb, name):
    """Вернуть провайдера по имени; при отсутствии создать."""
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
        return p, "создан"
    except Exception as e:
        return None, str(e)


def get_or_create_circuit_type(nb, name=CIRCUIT_TYPE_DEFAULT):
    """Вернуть circuit type по имени; при отсутствии создать."""
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
        return ct, "создан"
    except Exception as e:
        return None, str(e)


def get_or_create_circuit(nb, cid, provider, circuit_type, commit_rate_kbps, status=CIRCUIT_STATUS_ACTIVE):
    """Вернуть circuit по cid и provider; при отсутствии создать; при наличии — обновить commit_rate по файлу."""
    existing = list(nb.circuits.circuits.filter(cid=cid, provider_id=provider.id))
    tag_obj = _get_or_create_automation_tag(nb)
    if existing:
        c = existing[0]
        if tag_obj:
            _ensure_record_tag(nb, c, tag_obj, nb.circuits.circuits)
        # Обновить commit_rate в NetBox по файлу, если отличается
        if commit_rate_kbps is not None:
            want = int(commit_rate_kbps)
            current = getattr(c, "commit_rate", None)
            if current is not None:
                try:
                    current = int(current)
                except (TypeError, ValueError):
                    current = None
            if current != want:
                try:
                    _patch_circuit_commit_rate(nb, c.id, want)
                    return c, "commit_rate обновлён"
                except Exception as e:
                    print("Ошибка обновления commit_rate для {}: {}".format(cid, e), file=sys.stderr)
                    return c, None
        return c, None
    try:
        kwargs = {"cid": cid, "provider": provider.id, "type": circuit_type.id, "status": status}
        if commit_rate_kbps is not None:
            kwargs["commit_rate"] = int(commit_rate_kbps)
        if tag_obj:
            kwargs["tags"] = [tag_obj.id]
        c = nb.circuits.circuits.create(**kwargs)
        # После create явно выставить commit_rate через PATCH (pynetbox create иногда не передаёт)
        if commit_rate_kbps is not None and c and getattr(c, "id", None):
            try:
                _patch_circuit_commit_rate(nb, c.id, commit_rate_kbps)
            except Exception as e:
                print("Предупреждение: commit_rate не установлен для {}: {}".format(cid, e), file=sys.stderr)
        return c, "создан"
    except Exception as e:
        return None, str(e)


def _patch_circuit_commit_rate(nb, circuit_id, commit_rate_kbps):
    """Установить commit_rate у контура через REST PATCH (надёжнее, чем pynetbox update)."""
    base_url = (getattr(nb, "base_url", None) or getattr(nb, "url", None) or os.environ.get("NETBOX_URL", "")).rstrip("/")
    token = getattr(nb, "token", None) or os.environ.get("NETBOX_TOKEN")
    if not base_url or not token:
        raise RuntimeError("нет base_url или token у pynetbox api")
    if base_url.endswith("/api"):
        url = "{}/circuits/circuits/{}/".format(base_url, circuit_id)
    else:
        url = "{}/api/circuits/circuits/{}/".format(base_url, circuit_id)
    r = requests.patch(
        url,
        headers={"Authorization": "Token {}".format(token), "Content-Type": "application/json"},
        json={"commit_rate": int(commit_rate_kbps)},
        timeout=30,
    )
    r.raise_for_status()


def _patch_interface_mark_connected(nb, interface_id, value):
    """Сброс mark_connected у интерфейса через REST PATCH (pynetbox не всегда принимает этот аргумент)."""
    base_url = (getattr(nb, "base_url", None) or getattr(nb, "url", None) or os.environ.get("NETBOX_URL", "")).rstrip("/")
    token = getattr(nb, "token", None) or os.environ.get("NETBOX_TOKEN")
    if not base_url or not token:
        raise RuntimeError("нет base_url или token у pynetbox api")
    # base_url уже может заканчиваться на /api — не дублировать
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
    """
    Создать circuit termination (site устройства) и кабель до интерфейса.
    Если termination уже есть — не создаём повторно; кабель создаём при отсутствии.
    report — опциональный dict для отчёта: deleted_cables, disabled_mark_connected, created_cables (списки кортежей (dev_name, iface_name, ...)).
    """
    site = getattr(device, "site", None)
    if not site:
        return None, "у устройства {} нет site".format(device.name)
    site_id = site if isinstance(site, int) else getattr(site, "id", None)
    if not site_id:
        return None, "у устройства {} нет site".format(device.name)

    # Есть ли уже termination у этого circuit (A или Z)
    terminations = list(nb.circuits.circuit_terminations.filter(circuit_id=circuit.id))
    ct = None
    for t in terminations:
        if getattr(t, "term_side", None) == term_side or getattr(t, "termination_side", None) == term_side:
            ct = t
            break
    if not ct:
        # NetBox 4.2+: termination привязывается через termination_type + termination_id (site, location и т.д.)
        try:
            ct = nb.circuits.circuit_terminations.create(
                circuit=circuit.id,
                term_side=term_side,
                termination_type="dcim.site",
                termination_id=site_id,
            )
        except Exception as e1:
            # NetBox 3.x: поле site=
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

    # Кабель: circuit termination <-> interface. Сначала отключаем существующий кабель и mark_connected
    if getattr(ct, "cable", None) is not None:
        tag_obj = _get_or_create_automation_tag(nb)
        if tag_obj:
            try:
                cable_id = ct.cable.id if hasattr(ct.cable, "id") else ct.cable
                if cable_id:
                    cable_rec = nb.dcim.cables.get(cable_id)
                    _ensure_record_tag(nb, cable_rec, tag_obj, nb.dcim.cables)
            except Exception:
                pass
        return ct, None
    # У интерфейса уже есть кабель или mark_connected — отключаем, чтобы подключить как в файле
    try:
        existing_cable = getattr(nb_iface, "cable", None)
        if existing_cable is not None:
            cable_id = existing_cable.id if hasattr(existing_cable, "id") else existing_cable
            # pynetbox delete() ожидает список id
            nb.dcim.cables.delete([cable_id])
            if report is not None:
                report["deleted_cables"].append((dev_name, iface_name, cable_id))
        if getattr(nb_iface, "mark_connected", False):
            # pynetbox Record.update() не принимает mark_connected в части версий — сбрасываем через REST PATCH
            _patch_interface_mark_connected(nb, nb_iface.id, False)
            if report is not None:
                report["disabled_mark_connected"].append((dev_name, iface_name))
    except Exception as e:
        return ct, "отключение старого кабеля/mark_connected: {}".format(e)
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
    parser = argparse.ArgumentParser(description="Создать circuits в NetBox по commit_rates.json (по умолчанию — все площадки).")
    parser.add_argument("-f", "--commit-rates", default=DEFAULT_COMMIT_RATES, help="Путь к commit_rates.json")
    parser.add_argument("-d", "--dry-ssh", default=None, metavar="FILE", help="dry-ssh.json для маппинга логический интерфейс -> физический (кабель к физическому)")
    parser.add_argument("--location", default=None, metavar="LOC", help="Обработать только указанную локацию (первый сегмент hostname); по умолчанию — все")
    parser.add_argument("--dry-run", action="store_true", help="Не вносить изменения в NetBox")
    args = parser.parse_args()

    rates, err = load_commit_rates(args.commit_rates)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    tag = os.environ.get("NETBOX_TAG", "border")
    if not url or not token:
        print("Задайте NETBOX_URL и NETBOX_TOKEN", file=sys.stderr)
        sys.exit(1)

    nb = pynetbox.api(url, token=token)

    # Устройства NetBox по тегу
    try:
        devices = list(nb.dcim.devices.filter(tag=tag))
    except Exception as e:
        print("Ошибка NetBox: {}".format(e), file=sys.stderr)
        sys.exit(1)
    nb_devices_by_name = {d.name: d for d in devices}

    circuit_type_obj, ct_err = get_or_create_circuit_type(nb, CIRCUIT_TYPE_DEFAULT)
    if not circuit_type_obj:
        print("Не удалось получить/создать circuit type: {}".format(ct_err or "?"), file=sys.stderr)
        sys.exit(1)

    dry_ssh_path = args.dry_ssh or (DEFAULT_DRY_SSH if os.path.isfile(DEFAULT_DRY_SSH) else None)
    dry_ssh_devices = load_dry_ssh(dry_ssh_path) if dry_ssh_path else None
    if dry_ssh_path and not dry_ssh_devices:
        print("Внимание: dry-ssh не загружен (файл не найден или пустой), виртуальные интерфейсы не будут заменены на физические.", file=sys.stderr)

    report = {
        "created_providers": [],
        "created_circuits": [],
        "updated_commit_rate": [],
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
            errors.append("{}: устройство не найдено в NetBox (tag={})".format(dev_name, tag))
            continue
        ifaces = list(nb.dcim.interfaces.filter(device_id=device.id))
        nb_by_iface = {i.name: i for i in ifaces}
        for iface_name, entry in rates[dev_name].items():
            if not isinstance(entry, dict):
                continue
            provider_name = (entry.get("provider") or "").strip() or "Uplink"
            circuit_id = (entry.get("circuit_id") or "").strip()
            if not circuit_id:
                errors.append("{} {}: пустой circuit_id".format(dev_name, iface_name))
                continue
            rate_gbps = entry.get("commit_rate_gbps")
            commit_rate_kbps = int(rate_gbps * 1_000_000) if rate_gbps is not None else None

            # Для виртуальных (ae5.0 и т.п.) берём физический интерфейс из dry-ssh для кабеля
            cable_iface_name = resolve_physical_interface(dev_name, iface_name, dry_ssh_devices)
            if cable_iface_name != iface_name:
                report["virtual_to_physical"].append((dev_name, iface_name, cable_iface_name))
            _, nb_iface = resolve_interface(cable_iface_name, nb_by_iface)
            if not nb_iface:
                errors.append("{} {}: интерфейс не найден в NetBox (для кабеля: {})".format(
                    dev_name, iface_name, cable_iface_name if cable_iface_name != iface_name else iface_name))
                continue

            if args.dry_run:
                print("[dry-run] {} {} -> circuit {} provider {} commit_rate_kbps={}".format(
                    dev_name, iface_name, circuit_id, provider_name, commit_rate_kbps))
                ok += 1
                continue

            provider_obj, prov_msg = get_or_create_provider(nb, provider_name)
            if not provider_obj:
                errors.append("{} {}: провайдер {}: {}".format(dev_name, iface_name, provider_name, prov_msg))
                continue
            if prov_msg:
                report["created_providers"].append(provider_name)
                print("Провайдер {}: {}".format(provider_name, prov_msg))

            circuit_obj, circ_msg = get_or_create_circuit(
                nb, circuit_id, provider_obj, circuit_type_obj, commit_rate_kbps
            )
            if not circuit_obj:
                errors.append("{} {}: circuit {}: {}".format(dev_name, iface_name, circuit_id, circ_msg))
                continue
            if circ_msg:
                if circ_msg == "commit_rate обновлён":
                    report["updated_commit_rate"].append(circuit_id)
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
    print("Готово: {} успешно, {} ошибок.".format(ok, len(errors)))

    # Отчёт: что создано, удалено, отключено, где была не физика
    def _report_section(title, items, fmt):
        if not items:
            return
        print("\n--- {} ---".format(title))
        for x in items:
            print(fmt(x))

    _report_section("Создано провайдеров", report["created_providers"], lambda x: "  {}".format(x))
    _report_section("Создано контуров (circuits)", report["created_circuits"], lambda x: "  {}".format(x))
    _report_section("Обновлён commit_rate в NetBox (по файлу)", report["updated_commit_rate"], lambda x: "  {}".format(x))
    _report_section("Создано кабелей", report["created_cables"], lambda x: "  {} {}".format(x[0], x[1]))
    _report_section("Удалено кабелей", report["deleted_cables"], lambda x: "  {} {} (cable id {})".format(x[0], x[1], x[2]))
    _report_section("Отключено mark_connected", report["disabled_mark_connected"], lambda x: "  {} {}".format(x[0], x[1]))
    _report_section("Вместо виртуального использован физический интерфейс", report["virtual_to_physical"], lambda x: "  {} {} -> {}".format(x[0], x[1], x[2]))

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
