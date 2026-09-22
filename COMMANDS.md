


```bash
cp urls.env.example urls.env
```

---



```bash
python run_uplinks_full.py

python run_uplinks_full.py --report uplinks_run_report.txt --no-stop-on-error

# без Burst per-link триггеров (только макросы + util + агрегаты провайдера)
python run_uplinks_full.py --no-burst-triggers
```

Режим `--auto` удалён. Provider, Circuit, Termination и Cable создаются
вручную в NetBox.

Предварительный отчёт без записи в NetBox и Zabbix:

```bash
python run_uplinks_full.py --plan \
  --report uplinks_plan_report.txt
```

Этот режим читает NetBox inventory и текущие объекты Zabbix. Опрос устройств,
`dry-ssh.json` и сверка интерфейсов в него не входят.

Отчёт пишется в `run_logs/<дата>_zabbix_plan.json`. Карты, дашборды, сервисы и
агрегатные элементы теперь проходят практическое read-only сравнение:
показываются создание, изменение, удаление и отсутствие изменений. Координаты
карты и полное содержимое виджетов побитово не сравниваются. Триггеры Burst
пока помечаются `not_evaluated` — они не сравниваются.
Отдельно тот же отчёт даёт `zabbix_uplinks_plan.py`:

```bash
python zabbix_uplinks_plan.py \
  --inventory-file netbox_inventory.json \
  --text uplinks_plan_report.txt -o uplinks_plan_report.json
```

Обычный запуск работает в режиме NetBox-first: провайдеры, контуры,
терминации и кабели заранее заводятся человеком в NetBox. Создание контуров
не вызывается, а проверка интерфейсов выполняется в режиме
`--existing-only`.

Область мониторинга определяется на уровне Circuit: тип `Uplink`, встроенный
статус `Active`, активный и полный кабельный путь без разветвления до устройства
с тегом `border`. Если
физический интерфейс входит в логическое объединение, связь с логическим
интерфейсом берётся из NetBox.

**Шаги `run_uplinks_full.py` (по порядку):**

- Шаг 0: `netbox_uplinks_inventory.py --json --dry-run` — чтение цепочек NetBox
  в локальный `netbox_inventory.json`
- Шаг 1: `zabbix_sync_commit_rate.py` — макросы и триггеры
- Шаг 2: `zabbix_provider_aggregate.py` — агрегаты провайдеров
- Шаг 3: `zabbix_map.py --zabbix --update-map` — карта
- Шаг 4: `zabbix_uplinks_dashboard.py` — дашборды
- Шаг 5: `zabbix_provider_services.py` — сервисы и SLA

В режиме `--plan` выполняются только чтение inventory и
`zabbix_uplinks_plan.py`. Записи в NetBox и Zabbix не выполняются.
При ошибке чтения NetBox или Zabbix удаление и полная перезапись объектов
подавляются и отражаются в отчёте как `skipped` или `not_evaluated`.

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


Автоматическая генерация `commit_rates.json` удалена. Этот файл не используется
рабочим проектом.

```bash
# Старые команды генерации commit_rates удалены.
```


```bash
# Старые команды генерации commit_rates удалены.
```


```bash
# Старые команды генерации commit_rates удалены.
```


---


```bash
# Provider, Circuit, Termination и Cable создаются вручную в NetBox.
```


Старый режим с записью в NetBox. Может удалить существующий кабель и
подключить свой; на рабочем NetBox не запускать:

```bash
# Старые команды создания Circuit/Cable удалены.
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
python zabbix_map.py --legacy-dry-ssh -f dry-ssh.json --print-table
python zabbix_map.py --legacy-dry-ssh -f dry-ssh.json --zabbix --print-table
python zabbix_map.py --legacy-dry-ssh -f dry-ssh.json --zabbix --create-map
```


```bash
python zabbix_map.py --legacy-dry-ssh -f dry-ssh.json --zabbix --update-map
```


```bash
python zabbix_map.py --legacy-dry-ssh -f dry-ssh.json --zabbix --update-map --host "ALA-KZT-7280TR-1"
python zabbix_map.py --legacy-dry-ssh -f dry-ssh.json --zabbix --update-map --no-cache
```

---



```bash
python zabbix_uplinks_dashboard.py --legacy-dry-ssh -f dry-ssh.json
```



```bash
python zabbix_uplinks_dashboard.py --legacy-dry-ssh -f dry-ssh.json --no-show-threshold
```


```bash
python zabbix_uplinks_dashboard.py --legacy-dry-ssh -f dry-ssh.json --no-cache
```

---



Общий лимит поставщика берётся из поля NetBox `aggregate_limit_gbps` у
Provider (в Гбит/с):

```bash
python zabbix_provider_aggregate.py --legacy-dry-ssh -d dry-ssh.json
```

Старый источник — ключ `_provider_limits` в `commit_rates.json`, например
`{ "Cogent": 10, "Hurricane": 5 }`. Он читается только с явным ключом:

```bash
python zabbix_provider_aggregate.py --legacy-dry-ssh -d dry-ssh.json -f commit_rates.json --legacy-commit-rates-fallback
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
Автоматический NetBox cleanup удалён. Старые объекты NetBox не удаляются
рабочим проектом.
```


---


Та же цепочка по шагам, если нужно выполнить её вручную вместо
`run_uplinks_full.py`. Файл `commit_rates.json` здесь не участвует.

```bash
python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json

python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json

python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json
# и с триггерами Burst:
# python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json --create-link-triggers

python zabbix_provider_aggregate.py --inventory-file netbox_inventory.json

python zabbix_map.py --inventory-file netbox_inventory.json --zabbix --update-map

python zabbix_uplinks_dashboard.py --inventory-file netbox_inventory.json

python zabbix_provider_services.py --parent-service "Uplinks providers"
# python zabbix_provider_sla.py --inventory-file netbox_inventory.json

# python grafana_uplinks_graph.py -f dry-ssh.json --grafana-api
```

Автоматическая цепочка создания объектов удалена. Старые файлы не
используются рабочим запуском.

