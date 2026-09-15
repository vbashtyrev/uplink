


```bash
cp urls.env.example urls.env
python run_uplinks_full.py --refresh
```

---



```bash
python run_uplinks_full.py

python run_uplinks_full.py --refresh

python run_uplinks_full.py --no-fetch
python run_uplinks_full.py --from-file

python run_uplinks_full.py --report uplinks_run_report.txt --no-stop-on-error

# без Burst per-link триггеров (только макросы + util + агрегаты провайдера)
python run_uplinks_full.py --no-burst-triggers
```

Ключ `--location` допустим только вместе с `--auto`; в обычном запуске он
завершается ошибкой.

Предварительный отчёт без записи в NetBox и Zabbix:

```bash
python run_uplinks_full.py --plan --from-file \
  --report uplinks_plan_report.txt
```

Этот режим использует уже проверенный `dry-ssh.json`, запускает проверку
интерфейсов без `--apply` и отдельно читает текущие объекты Zabbix. Новый
опрос оборудования выполняется обычным запуском или через `--refresh`.

Обязателен `--from-file` или `--no-fetch`; совмещать с `--auto` нельзя. Отчёт
пишется в `run_logs/<дата>_zabbix_plan.json`. Карты, дашборды, сервисы и
триггеры Burst в отчёте помечены `not_evaluated` — они не сравниваются.
Отдельно тот же отчёт даёт `zabbix_uplinks_plan.py`:

```bash
python zabbix_uplinks_plan.py -d dry-ssh.json \
  --inventory-file netbox_inventory.json \
  --text uplinks_plan_report.txt -o uplinks_plan_report.json
```

Обычный запуск работает в режиме NetBox-first: провайдеры, контуры,
терминации и кабели заранее заводятся человеком в NetBox. Создание контуров
не вызывается, а проверка интерфейсов выполняется в режиме
`--existing-only`.

Область мониторинга определяется на уровне отдельных Circuit: нужен тег
`uplinks` и поле `uplinks_circuit_lifecycle=active`. Один Provider может
иметь одновременно активный Circuit и Circuit в `review`. Физический
интерфейс используется для проверки кабеля в NetBox, а если он входит в
логическое объединение, в Zabbix используется имя логического интерфейса
например `ae5.0`.

Переходный старый путь доступен только явно:

```bash
python run_uplinks_full.py --auto
```

Не запускайте `--auto` на рабочем NetBox: старый код может заменить
существующий кабель.

**Шаги `run_uplinks_full.py` (по порядку):**

- Шаг 0: `netbox_uplinks_inventory.py --json --dry-run` — чтение цепочек NetBox
  в локальный `netbox_inventory.json`
- Шаг 1: `uplinks_stats.py --fetch --json --inventory-file netbox_inventory.json`
  → `dry-ssh.json` только для найденных устройств
- Шаг 2: `netbox_checks.py --all --mt-ref --existing-only --apply` — сверка и
  обновление только уже существующих интерфейсов
- Шаг 3: сводка по цепочкам NetBox (по данным шага 0, без повторного чтения)
- Шаг 5: `zabbix_sync_commit_rate.py` — макросы и триггеры
- Шаг 6: `zabbix_provider_aggregate.py` — агрегаты провайдеров
- Шаг 7: `zabbix_map.py --zabbix --update-map` — карта
- Шаг 8: `zabbix_uplinks_dashboard.py` — дашборды
- Шаг 9: `zabbix_provider_services.py` — сервисы и SLA

В режиме `--plan` выполняются только шаги 0, 2 (без `--apply`), 3 и
`zabbix_uplinks_plan.py`. Карта, дашборды и сервисы не запускаются.

В режиме `--auto` дополнительно выполняются старые шаги
`generate_commit_rates.py` и `netbox_create_circuits.py --auto`, а шагам 5, 6 и
9 передаются `-f commit_rates.json` и `--legacy-commit-rates-fallback`.

**Не входит в full run** (отдельные команды ниже): `grafana_uplinks_graph.py`, `zabbix_provider_sla.py`, `netbox_interface_types.py`, cleanup-скрипты.

Логи: `run_logs/YYYY-MM-DD_HH-MM-SS_run.log` и `*_debug.log`.


---


```bash
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="your-netbox-api-token"

export SSH_PASSWORD="password"

export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="your-zabbix-api-token"

# export GRAFANA_URL="https://grafana.example.com"
# export GRAFANA_API_KEY="your-grafana-api-key"
```

---



```bash
python uplinks_stats.py --fetch --json > dry-ssh.json
```


```bash
python uplinks_stats.py --fetch --json --host "ALA-KZT-7280TR-1" > dry-ssh.json
python uplinks_stats.py --fetch --json --platform arista > dry-ssh.json
```

---



```bash
python netbox_checks.py -f dry-ssh.json
```


```bash
python netbox_checks.py -f dry-ssh.json --apply
```


```bash
python netbox_interface_types.py -o netbox_interface_types.json
python netbox_checks.py -f dry-ssh.json --mt-ref netbox_interface_types.json --apply
```

---


Только для старой цепочки `--auto`: в обычном запуске `commit_rates.json` не
используется.

```bash
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json
```


```bash
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json --no-merge
```


```bash
python generate_commit_rates.py -f dry-ssh.json -m description_to_name.json -o commit_rates.json
```


---


```bash
# Безопасный lookup-only запуск: объекты только читаются.
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json
```


Старый режим с записью в NetBox. Может удалить существующий кабель и
подключить свой; на рабочем NetBox не запускать:

```bash
python netbox_create_circuits.py --auto -f commit_rates.json -d dry-ssh.json --location ALA
python netbox_create_circuits.py --auto -f commit_rates.json -d dry-ssh.json --dry-run
```

---


```bash
python zabbix_sync_commit_rate.py
```



```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json
```


Триггеры для Burst-контуров (схема расчёта берётся из поля NetBox
`billing_model`):

```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers
```

Ключ `--dry-run` нельзя совмещать с `--delete-link-triggers` и
`--delete-util-triggers`.


```bash
python zabbix_sync_commit_rate.py --delete-link-triggers
```


```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json --dry-run
python zabbix_sync_commit_rate.py -d dry-ssh.json --debug
```

---



```bash
python zabbix_map.py -f dry-ssh.json --print-table
python zabbix_map.py -f dry-ssh.json --zabbix --print-table
python zabbix_map.py -f dry-ssh.json --zabbix --create-map
```


```bash
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
```


```bash
python zabbix_map.py -f dry-ssh.json --zabbix --update-map --host "ALA-KZT-7280TR-1"
python zabbix_map.py -f dry-ssh.json --zabbix --update-map --no-cache
```

---



```bash
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```



```bash
python zabbix_uplinks_dashboard.py -f dry-ssh.json --no-show-threshold
```


```bash
python zabbix_uplinks_dashboard.py -f dry-ssh.json --no-cache
```

---



Общий лимит поставщика берётся из поля NetBox `aggregate_limit_gbps` у
Provider (в Гбит/с):

```bash
python zabbix_provider_aggregate.py -d dry-ssh.json
```

Старый источник — ключ `_provider_limits` в `commit_rates.json`, например
`{ "Cogent": 10, "Hurricane": 5 }`. Он читается только с явным ключом:

```bash
python zabbix_provider_aggregate.py -d dry-ssh.json -f commit_rates.json --legacy-commit-rates-fallback
```


---



```json
"_provider_sla": 99.95
```





Обычный запуск сервисов и SLA использует активные Circuit из NetBox и
`PROJECT_PROVIDER_SLO_PERCENT` из конфигурации проекта; `commit_rates.json`
для этого не нужен. Переходный источник включается только явно:

```bash
python zabbix_provider_services.py --parent-service "Uplinks providers"
python zabbix_provider_services.py -f commit_rates.json --parent-service "Uplinks providers" --legacy-commit-rates-fallback
```


```bash
python zabbix_provider_sla.py --days 30
python zabbix_provider_sla.py -f commit_rates.json --days 30 \
  --legacy-commit-rates-fallback
```


---



```bash
python grafana_uplinks_graph.py -f dry-ssh.json -o grafana_uplinks_graph.json
```


```bash
python grafana_uplinks_graph.py -f dry-ssh.json --grafana-api --dashboard-uid uplinks --dashboard-title "Uplinks"
```


```bash
python grafana_uplinks_graph.py -f dry-ssh.json --zabbix --grafana-api
```

---



```bash
python zabbix_uplinks_cleanup.py --dry-run
```


```bash
python zabbix_uplinks_cleanup.py
```

---



```bash
python netbox_uplinks_cleanup.py --dry-run

python netbox_uplinks_cleanup.py
```


---


Та же цепочка по шагам, если нужно выполнить её вручную вместо
`run_uplinks_full.py`. Файл `commit_rates.json` здесь не участвует.

```bash
python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json

python uplinks_stats.py --fetch --json --inventory-file netbox_inventory.json > dry-ssh.json

python netbox_checks.py -f dry-ssh.json --all --mt-ref --existing-only --apply

python zabbix_sync_commit_rate.py -d dry-ssh.json
# и с триггерами Burst:
# python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers

python zabbix_provider_aggregate.py -d dry-ssh.json

python zabbix_map.py -f dry-ssh.json --zabbix --update-map

python zabbix_uplinks_dashboard.py -f dry-ssh.json

python zabbix_provider_services.py --parent-service "Uplinks providers"
# python zabbix_provider_sla.py --dry-ssh dry-ssh.json

# python grafana_uplinks_graph.py -f dry-ssh.json --grafana-api
```

Старая цепочка с созданием объектов в NetBox — только по явному требованию и
не на рабочем NetBox:

```bash
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json
python netbox_create_circuits.py --auto -f commit_rates.json -d dry-ssh.json
python zabbix_provider_aggregate.py -d dry-ssh.json -f commit_rates.json --legacy-commit-rates-fallback
python zabbix_provider_services.py -f commit_rates.json --legacy-commit-rates-fallback --parent-service "Uplinks providers"
```

