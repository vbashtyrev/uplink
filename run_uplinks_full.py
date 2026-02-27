#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Запуск полной цепочки uplinks одним скриптом: сбор данных → commit_rates → NetBox circuits
→ Zabbix sync/map/dashboard → опционально Grafana. Вывод отчёта о проделанной работе и об ошибках.

- Шаг 1 (сбор dry-ssh.json): кэш на 24ч — при наличии свежего файла пропускается; обход: --refresh.
- Шаг 2 (NetBox checks): по умолчанию выполняется (netbox_checks --apply); пропуск: --no-netbox-apply.
- Логи каждого запуска: run_logs/YYYY-MM-DD_HH-MM-SS_run.log.

Переменные окружения (как у дочерних скриптов): NETBOX_URL, NETBOX_TOKEN, NETBOX_TAG,
SSH_USERNAME, SSH_PASSWORD — для сбора; ZABBIX_URL, ZABBIX_TOKEN — для Zabbix;
GRAFANA_URL, GRAFANA_API_KEY — для Grafana при --grafana.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DRY_SSH = "dry-ssh.json"
DEFAULT_COMMIT_RATES = "commit_rates.json"
DEFAULT_DESC_MAP = "description_to_name.json"
RUN_LOGS_DIR = "run_logs"       # папка логов запусков (дата_время_run.log)
CACHE_AGE_SECONDS = 24 * 3600    # кэш dry-ssh.json на 24 часа для шага 1


def run_cmd(argv, cwd, timeout=600, capture_stdout_to_file=None, env=None):
    """
    Запуск команды. argv — список [python, 'script.py', ...].
    Возврат (success: bool, stdout: str, stderr: str).
    Если capture_stdout_to_file задан, stdout команды записывается в файл (и в возврате остаётся пустая строка).
    """
    env = env or os.environ
    try:
        if capture_stdout_to_file:
            with open(capture_stdout_to_file, "w", encoding="utf-8") as f:
                r = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                    stdout=f,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            stderr = (r.stderr or "").strip()
            return (r.returncode == 0, "", stderr)
        else:
            r = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
            stdout = (r.stdout or "").strip()
            stderr = (r.stderr or "").strip()
            return (r.returncode == 0, stdout, stderr)
    except subprocess.TimeoutExpired:
        return (False, "", "Таймаут выполнения ({} с)".format(timeout))
    except FileNotFoundError as e:
        return (False, "", "Не найден исполняемый файл или скрипт: {}".format(e))
    except Exception as e:
        return (False, "", str(e))


def _write_run_report(report_lines, run_log_path, report_file, log_func=None):
    """Записать отчёт в run_log_path и при необходимости в report_file. log_func(msg) — опциональный вывод в консоль."""
    if run_log_path:
        try:
            with open(run_log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(report_lines))
            if log_func:
                log_func("Лог запуска: {}".format(run_log_path))
        except Exception as e:
            print("Не удалось записать лог в {}: {}".format(run_log_path, e), file=sys.stderr)
    if report_file:
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                f.write("\n".join(report_lines))
            if log_func:
                log_func("Отчёт записан в {}".format(report_file))
        except Exception as e:
            print("Не удалось записать отчёт в {}: {}".format(report_file, e), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Полная цепочка uplinks: сбор → commit_rates → NetBox → Zabbix (sync, карта, дашборды) → опционально Grafana. Отчёт о работе и об ошибках.",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Не опрашивать устройства по SSH; использовать существующий dry-ssh.json",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Обновить кэш шага 1: принудительно выполнить uplinks_stats.py --fetch --json (игнорировать кэш на 24ч)",
    )
    parser.add_argument(
        "--dry-ssh",
        default=DEFAULT_DRY_SSH,
        metavar="FILE",
        help="Путь к dry-ssh.json (по умолчанию {})".format(DEFAULT_DRY_SSH),
    )
    parser.add_argument(
        "--commit-rates",
        default=DEFAULT_COMMIT_RATES,
        metavar="FILE",
        help="Путь к commit_rates.json (по умолчанию {})".format(DEFAULT_COMMIT_RATES),
    )
    parser.add_argument(
        "--no-netbox-apply",
        action="store_true",
        help="Пропустить шаг 2 (netbox_checks --apply). По умолчанию шаг выполняется.",
    )
    parser.add_argument(
        "--grafana",
        action="store_true",
        help="В конце создать/обновить дашборд в Grafana (grafana_uplinks_graph.py --grafana-api)",
    )
    parser.add_argument(
        "--location",
        default=None,
        metavar="LOC",
        help="Передать --location в netbox_create_circuits (только указанная локация)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        default=True,
        help="Остановиться на первой ошибке (по умолчанию)",
    )
    parser.add_argument(
        "--no-stop-on-error",
        action="store_false",
        dest="stop_on_error",
        help="Продолжать выполнение при ошибке шага",
    )
    parser.add_argument(
        "--report",
        default=None,
        metavar="FILE",
        help="Дополнительно записать отчёт в файл",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        metavar="SEC",
        help="Таймаут одного шага в секундах (по умолчанию 600)",
    )
    args = parser.parse_args()

    os.chdir(SCRIPT_DIR)
    python = sys.executable
    timeout = args.timeout
    dry_ssh_path = args.dry_ssh
    commit_rates_path = args.commit_rates

    # Папка логов: run_logs/YYYY-MM-DD_HH-MM-SS_run.log
    logs_dir = os.path.join(SCRIPT_DIR, RUN_LOGS_DIR)
    os.makedirs(logs_dir, exist_ok=True)
    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_log_path = os.path.join(logs_dir, "{}_run.log".format(run_ts))

    report_lines = []
    errors = []

    def log(msg):
        report_lines.append(msg)
        print(msg)

    def step(name, success, detail=""):
        if success:
            log("[OK] {} {}".format(name, detail).strip())
        else:
            log("[FAIL] {} {}".format(name, detail).strip())
            errors.append((name, detail))

    log("=== Uplinks full run {} ===".format(datetime.now().isoformat(timespec="seconds")))
    log("Рабочая директория: {}".format(SCRIPT_DIR))
    log("Лог запуска: {}".format(run_log_path))
    log("")

    # 1. Сбор данных с устройств; кэш на 24ч — при наличии свежего dry-ssh.json шаг пропускается (обход: --refresh)
    if args.no_fetch:
        if not os.path.isfile(dry_ssh_path):
            step("Шаг 1: Пропуск (--no-fetch)", False, "файл {} не найден".format(dry_ssh_path))
            if args.stop_on_error:
                _finish(report_lines, errors, args.report, run_log_path)
                sys.exit(1)
        else:
            log("[SKIP] Шаг 1: Сбор данных (--no-fetch), используется {}".format(dry_ssh_path))
            report_lines.append("")
    else:
        use_cache = False
        if not args.refresh and os.path.isfile(dry_ssh_path):
            try:
                age = time.time() - os.path.getmtime(dry_ssh_path)
                if age <= CACHE_AGE_SECONDS:
                    use_cache = True
            except OSError:
                pass
        if use_cache:
            log("[SKIP] Шаг 1: Сбор данных (кэш актуален, {} < 24ч). Для обновления запустите с --refresh".format(dry_ssh_path))
            report_lines.append("")
        else:
            log("Шаг 1: Сбор данных с устройств (uplinks_stats.py --fetch --json) -> {} ...".format(dry_ssh_path))
            ok, out, err = run_cmd(
                [python, "uplinks_stats.py", "--fetch", "--json"],
                cwd=SCRIPT_DIR,
                timeout=timeout,
            )
            if not ok:
                err_msg = err or out or "код возврата != 0"
                if out and err:
                    err_msg = "stderr: {} | stdout: {}".format(err[:500], out[:500])
                elif out:
                    err_msg = out[:1000] if len(out) > 1000 else out
                step("Шаг 1: Сбор данных", False, err_msg)
                if args.stop_on_error:
                    _finish(report_lines, errors, args.report, run_log_path)
                    sys.exit(1)
            else:
                try:
                    with open(dry_ssh_path, "w", encoding="utf-8") as f:
                        f.write(out or "")
                except Exception as e:
                    step("Шаг 1: Сбор данных", False, "не удалось записать {}: {}".format(dry_ssh_path, e))
                    if args.stop_on_error:
                        _finish(report_lines, errors, args.report, run_log_path)
                        sys.exit(1)
                else:
                    step("Шаг 1: Сбор данных", True, "-> {}".format(dry_ssh_path))

    # 2. NetBox checks (опционально)
    if not args.no_netbox_apply:
        log("Шаг 2: NetBox — сверка и применение (netbox_checks.py -f {} --apply) ...".format(dry_ssh_path))
        ok, out, err = run_cmd(
            [python, "netbox_checks.py", "-f", dry_ssh_path, "--apply"],
            cwd=SCRIPT_DIR,
            timeout=timeout,
        )
        step("Шаг 2: NetBox checks --apply", ok, err or out or ("код != 0" if not ok else ""))
        if not ok and args.stop_on_error:
            _finish(report_lines, errors, args.report, run_log_path)
            sys.exit(1)
        log("")
    else:
        log("[SKIP] Шаг 2: NetBox checks (пропущен: --no-netbox-apply)")
        report_lines.append("")

    # 3. Генерация commit_rates.json
    log("Шаг 3: Генерация {} ...".format(commit_rates_path))
    ok, out, err = run_cmd(
        [python, "generate_commit_rates.py", "-f", dry_ssh_path, "-m", DEFAULT_DESC_MAP, "-o", commit_rates_path],
        cwd=SCRIPT_DIR,
        timeout=timeout,
    )
    step("Шаг 3: generate_commit_rates", ok, err or ("код != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    log("")

    # 4. NetBox create circuits
    cmd_circuits = [python, "netbox_create_circuits.py", "-f", commit_rates_path, "-d", dry_ssh_path]
    if args.location:
        cmd_circuits.extend(["--location", args.location])
    log("Шаг 4: NetBox circuits (netbox_create_circuits.py) ...")
    ok, out, err = run_cmd(cmd_circuits, cwd=SCRIPT_DIR, timeout=timeout)
    step("Шаг 4: NetBox circuits", ok, err or ("код != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    log("")

    # 5. Zabbix sync commit rate
    log("Шаг 5: Zabbix — макросы и триггеры (zabbix_sync_commit_rate.py -d {}) ...".format(dry_ssh_path))
    ok, out, err = run_cmd(
        [python, "zabbix_sync_commit_rate.py", "-d", dry_ssh_path],
        cwd=SCRIPT_DIR,
        timeout=timeout,
    )
    step("Шаг 5: Zabbix sync commit rate", ok, err or ("код != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    if out:
        for line in out.splitlines():
            log("  {}".format(line))
    log("")

    # 6. Zabbix map
    log("Шаг 6: Zabbix — карта (zabbix_map.py -f {} --zabbix --update-map) ...".format(dry_ssh_path))
    ok, out, err = run_cmd(
        [python, "zabbix_map.py", "-f", dry_ssh_path, "--zabbix", "--update-map"],
        cwd=SCRIPT_DIR,
        timeout=timeout,
    )
    step("Шаг 6: Zabbix map", ok, err or ("код != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    if out:
        for line in out.splitlines():
            log("  {}".format(line))
    log("")

    # 7. Zabbix dashboard
    log("Шаг 7: Zabbix — дашборды (zabbix_uplinks_dashboard.py -f {}) ...".format(dry_ssh_path))
    ok, out, err = run_cmd(
        [python, "zabbix_uplinks_dashboard.py", "-f", dry_ssh_path],
        cwd=SCRIPT_DIR,
        timeout=timeout,
    )
    step("Шаг 7: Zabbix dashboard", ok, err or ("код != 0" if not ok else ""))
    if not ok and args.stop_on_error:
        _finish(report_lines, errors, args.report, run_log_path)
        sys.exit(1)
    log("")

    # 8. Grafana (опционально)
    if args.grafana:
        log("Шаг 8: Grafana — Node graph (grafana_uplinks_graph.py -f {} --grafana-api) ...".format(dry_ssh_path))
        ok, out, err = run_cmd(
            [python, "grafana_uplinks_graph.py", "-f", dry_ssh_path, "--grafana-api"],
            cwd=SCRIPT_DIR,
            timeout=timeout,
        )
        step("Шаг 8: Grafana", ok, err or ("код != 0" if not ok else ""))
        if not ok and args.stop_on_error:
            _finish(report_lines, errors, args.report, run_log_path)
            sys.exit(1)
        log("")
    else:
        log("[SKIP] Шаг 8: Grafana (запустите с --grafana при необходимости)")
        report_lines.append("")

    # Итог
    log("--- Итог ---")
    if errors:
        log("Ошибки ({}):".format(len(errors)))
        for name, detail in errors:
            log("  {}: {}".format(name, (detail or "").strip() or "код возврата != 0"))
        _write_run_report(report_lines, run_log_path, args.report, log_func=log)
        sys.exit(1)
    else:
        log("Все шаги выполнены успешно.")
        _write_run_report(report_lines, run_log_path, args.report, log_func=log)
        sys.exit(0)


def _finish(report_lines, errors, report_file, run_log_path=None):
    """Вывести итог при досрочной остановке и записать отчёт в run_logs и при необходимости в report_file."""
    summary = ["", "--- Итог (остановка из-за ошибки) ---", "Ошибки:"]
    for name, detail in errors:
        summary.append("  {}: {}".format(name, (detail or "").strip() or "код возврата != 0"))
    report_lines.extend(summary)
    for line in summary:
        print(line)
    _write_run_report(report_lines, run_log_path, report_file, log_func=print)


if __name__ == "__main__":
    main()
