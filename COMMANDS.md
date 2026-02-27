# Полный набор команд для актуализации и мониторинга uplinks

Переменные окружения задаются один раз в сессии (или в `.env` / профиле). Для Zabbix/NetBox обязательны `*_URL` и `*_TOKEN`.

---

## Один скрипт — вся цепочка (run_uplinks_full.py)

Запуск всех шагов с одним вызовом, с отчётом о проделанной работе и об ошибках:

```bash
# Полный прогон: сбор с устройств → commit_rates → NetBox circuits → Zabbix (sync, карта, дашборды)
# Шаг 1 (сбор) кэшируется на 24ч — при наличии свежего dry-ssh.json не выполняется. Обновить принудительно: --refresh
python run_uplinks_full.py

# Обновить кэш шага 1 (принудительно опросить устройства)
python run_uplinks_full.py --refresh

# Без опроса устройств (использовать существующий dry-ssh.json)
python run_uplinks_full.py --no-fetch

# С Grafana в конце (шаг 2 NetBox по умолчанию выполняется; пропустить: --no-netbox-apply)
python run_uplinks_full.py --grafana

# Записать отчёт в файл, не останавливаться на первой ошибке
python run_uplinks_full.py --report uplinks_run_report.txt --no-stop-on-error

# Только одна локация для circuits
python run_uplinks_full.py --no-fetch --location ALA
```

**Аргументы:**

| Аргумент | Описание |
|----------|----------|
| `--no-fetch` | Не опрашивать устройства по SSH; использовать существующий dry-ssh.json |
| `--refresh` | Обновить кэш шага 1: принудительно выполнить сбор (игнорировать кэш на 24ч) |
| `--dry-ssh FILE` | Путь к dry-ssh.json (по умолчанию dry-ssh.json) |
| `--commit-rates FILE` | Путь к commit_rates.json (по умолчанию commit_rates.json) |
| `--no-netbox-apply` | Пропустить шаг 2 (netbox_checks --apply). По умолчанию шаг выполняется. |
| `--grafana` | В конце запустить grafana_uplinks_graph.py --grafana-api |
| `--location LOC` | Передать --location в netbox_create_circuits |
| `--no-stop-on-error` | Продолжать выполнение при ошибке шага (по умолчанию — остановка) |
| `--report FILE` | Записать полный отчёт в файл |
| `--timeout SEC` | Таймаут одного шага в секундах (по умолчанию 600) |

В консоль выводится ход выполнения ([OK]/[FAIL]/[SKIP] по каждому шагу); в конце — блок «Итог» и список ошибок (если были). Отчёт каждого запуска сохраняется в папку **run_logs/** в файл с датой и временем (например `run_logs/2026-02-27_14-30-00_run.log`). При `--report FILE` тот же отчёт дополнительно сохраняется в указанный файл.

---

## Переменные окружения

```bash
# NetBox (обязательно для шагов 1, 2, 3, 5)
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="your-netbox-api-token"
export NETBOX_TAG="border"   # тег устройств в NetBox (по умолчанию border)

# SSH для сбора с устройств (обязательно для шага 1 при --fetch)
export SSH_USERNAME="your-ssh-user"   # обязательно при шаге 1 (сбор с устройств)
export SSH_PASSWORD="password"
export SSH_HOST_SUFFIX=".3hc.io"   # опционально
export PARALLEL_DEVICES=6           # опционально
# export USE_SSH_CONFIG=0             # отключить использование ~/.ssh/config (по умолчанию включено)

# Zabbix (обязательно для шагов 5, 6, 7)
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="your-zabbix-api-token"

# Grafana (только для шага 8)
# export GRAFANA_URL="https://grafana.example.com"
# export GRAFANA_API_KEY="your-grafana-api-key"
```

---

## Порядок выполнения

### 1. Сбор данных с устройств → dry-ssh.json

```bash
python uplinks_stats.py --fetch --json > dry-ssh.json
```

Опционально: только один хост, только платформа, вывод в файл с другим именем:

```bash
python uplinks_stats.py --fetch --json --host "ALA-KZT-7280TR-1" > dry-ssh.json
python uplinks_stats.py --fetch --json --platform arista > dry-ssh.json
```

---

### 2. Сверка/обновление интерфейсов в NetBox (опционально)

Проверка расхождений без записи:

```bash
python netbox_checks.py -f dry-ssh.json
```

Применение изменений в NetBox:

```bash
python netbox_checks.py -f dry-ssh.json --apply
```

С привязкой к справочнику типов интерфейсов:

```bash
python netbox_interface_types.py -o netbox_interface_types.json
python netbox_checks.py -f dry-ssh.json --mt-ref netbox_interface_types.json --apply
```

---

### 3. Генерация commit_rates.json

```bash
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json
```

Без подмешивания существующего файла (полная перезапись):

```bash
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json --no-merge
```

С другим маппингом description → провайдер:

```bash
python generate_commit_rates.py -f dry-ssh.json -m description_to_name.json -o commit_rates.json
```

После генерации при необходимости вручную отредактировать `commit_rates.json` (circuit_id, commit_rate_gbps).

---

### 4. Создание/обновление circuits в NetBox

```bash
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json
```

Только одна локация, без изменений (dry-run):

```bash
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --location ALA
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --dry-run
```

---

### 5. Синхронизация макросов и триггеров в Zabbix

```bash
python zabbix_sync_commit_rate.py
```

Если в NetBox кабель на физическом интерфейсе (например et-0/0/3), а в Zabbix — логический (ae5.0), передать dry-ssh для подстановки имён:

```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json
```

Проверка без записи в Zabbix:

```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json --dry-run
python zabbix_sync_commit_rate.py -d dry-ssh.json --debug
```

---

### 6. Карта Zabbix (создание/обновление)

Сначала только таблица или создание пустой карты:

```bash
python zabbix_map.py -f dry-ssh.json --print-table
python zabbix_map.py -f dry-ssh.json --zabbix --print-table
python zabbix_map.py -f dry-ssh.json --zabbix --create-map
```

Полное обновление карты (хосты, провайдеры, линки, привязка триггеров к линкам):

```bash
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
```

Только один хост, без кэша:

```bash
python zabbix_map.py -f dry-ssh.json --zabbix --update-map --host "ALA-KZT-7280TR-1"
python zabbix_map.py -f dry-ssh.json --zabbix --update-map --no-cache
```

---

### 7. Дашборды Zabbix

Основной дашборд и дашборд по локациям:

```bash
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```

Без линий порога на графиках, с другими именами дашбордов:

```bash
python zabbix_uplinks_dashboard.py -f dry-ssh.json --no-show-threshold
python zabbix_uplinks_dashboard.py -f dry-ssh.json --dashboard-name "Uplinks" --dashboard-by-location "Uplinks (по локациям)"
```

Без кэша Zabbix:

```bash
python zabbix_uplinks_dashboard.py -f dry-ssh.json --no-cache
```

---

### 8. Grafana Node graph (опционально)

Только JSON для панели:

```bash
python grafana_uplinks_graph.py -f dry-ssh.json -o grafana_uplinks_graph.json
```

Создание/обновление дашборда через API:

```bash
python grafana_uplinks_graph.py -f dry-ssh.json --grafana-api --dashboard-uid uplinks --dashboard-title "Uplinks"
```

С данными из Zabbix (хосты/items):

```bash
python grafana_uplinks_graph.py -f dry-ssh.json --zabbix --grafana-api
```

---

### 9. Очистка артефактов в Zabbix (откат)

Сначала посмотреть, что будет удалено:

```bash
python zabbix_uplinks_cleanup.py --dry-run
```

Выполнить удаление (триггеры, item'ы порога, карта, дашборды):

```bash
python zabbix_uplinks_cleanup.py
```

С другими именами дашбордов (как в uplinks_config):

```bash
python zabbix_uplinks_cleanup.py --dashboard-name "Uplinks" --dashboard-by-location "Uplinks (по локациям)"
```

---

## Одной цепочкой (после настройки dry-ssh.json и commit_rates.json)

```bash
# 1) Сбор с устройств
python uplinks_stats.py --fetch --json > dry-ssh.json

# 2) При необходимости — сверка NetBox
# python netbox_checks.py -f dry-ssh.json --apply

# 3) Генерация commit_rates
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json

# 4) Circuits в NetBox
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json

# 5) Макросы и триггеры в Zabbix
python zabbix_sync_commit_rate.py -d dry-ssh.json

# 6) Карта Zabbix
python zabbix_map.py -f dry-ssh.json --zabbix --update-map

# 7) Дашборды Zabbix
python zabbix_uplinks_dashboard.py -f dry-ssh.json

# 8) Grafana (опционально)
# python grafana_uplinks_graph.py -f dry-ssh.json --grafana-api
```

Перед запуском: активировать venv (`source .venv/bin/activate`), выставить `NETBOX_URL`, `NETBOX_TOKEN`, `ZABBIX_URL`, `ZABBIX_TOKEN` (и при шаге 1 — обязательно `SSH_USERNAME` и `SSH_PASSWORD`, при необходимости `NETBOX_TAG`).
