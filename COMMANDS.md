# Основные команды

Перед запуском задайте адреса и токены:

```bash
cp urls.env.example urls.env
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="..."
```

Рабочий запуск читает готовые цепочки подключений в NetBox и настраивает
Zabbix. По умолчанию NetBox не изменяется; ключ `--netbox-checks` запускает
`netbox_checks.py --apply` для сверки `dry-ssh.json` и обновления разрешённых
полей.

## Полный запуск

```bash
python run_uplinks_full.py
python run_uplinks_full.py --report uplinks_run_report.txt --no-stop-on-error
python run_uplinks_full.py --no-burst-triggers
python run_uplinks_full.py --netbox-checks
```

Область мониторинга строится по цепочке NetBox:

```text
Provider → Circuit → Termination A → Cable → Interface → Device
```

Подходят только каналы типа `Uplink` со статусом `Active`, полным кабельным
путём без разветвления и конечным устройством с тегом `border`.

## Предварительный план

План читает NetBox и Zabbix и ничего не меняет:

```bash
python run_uplinks_full.py --plan --report uplinks_run_report.txt
python zabbix_uplinks_plan.py \
  --inventory-file netbox_inventory.json \
  --create-link-triggers \
  --text uplinks_plan_report.txt \
  --output uplinks_plan_report.json
```

В плане сравниваются макросы, триггеры, агрегаты, карта, дашборды, сервисы и
Burst-триггеры. При изменении выражения, приоритета, тегов или зависимости
план показывает `update`. При неоднозначном совпадении объект попадает в
`skipped`, а создание дубликата не планируется.

## Inventory NetBox

Создать локальный снимок цепочек NetBox:

```bash
python netbox_uplinks_inventory.py --json --dry-run > netbox_inventory.json
```

Один и тот же файл можно передать всем шагам:

```bash
python zabbix_sync_commit_rate.py \
  --inventory-file netbox_inventory.json \
  --create-link-triggers
python zabbix_provider_aggregate.py \
  --inventory-file netbox_inventory.json
python zabbix_map.py \
  --inventory-file netbox_inventory.json \
  --zabbix --update-map
python zabbix_uplinks_dashboard.py \
  --inventory-file netbox_inventory.json
python zabbix_provider_services.py \
  --inventory-file netbox_inventory.json \
  --parent-service "Uplinks providers"
```

Если снимок неполный или содержит ошибку чтения, опасные удаления и полные
перезаписи подавляются. Скрипты не обращаются к NetBox повторно, если снимок
уже передан.

## Ручная проверка интерфейсов

Эти команды не входят в полный запуск:

```bash
python uplinks_stats.py --fetch --json > dry-ssh.json
python uplinks_stats.py --fetch --json --host "router-001" --merge-into
python netbox_checks.py -f dry-ssh.json --all --mt-ref --show-change
python netbox_checks.py -f dry-ssh.json --mediatype --mt-ref --apply
python netbox_interface_types.py -o netbox_interface_types.json
```

`--apply` записывает проверенные изменения в NetBox. Отсутствующие интерфейсы,
MAC-адреса и IP-адреса не создаются. Ключ `--existing-only` дополнительно
запрещает менять связи LAG и parent.

## Макросы и триггеры

```bash
python zabbix_sync_commit_rate.py \
  --inventory-file netbox_inventory.json
python zabbix_sync_commit_rate.py \
  --inventory-file netbox_inventory.json \
  --create-link-triggers
python zabbix_sync_commit_rate.py \
  --inventory-file netbox_inventory.json \
  --dry-run
python zabbix_sync_commit_rate.py \
  --inventory-file netbox_inventory.json \
  --delete-link-triggers
python zabbix_sync_commit_rate.py \
  --inventory-file netbox_inventory.json \
  --delete-util-triggers
```

Файл `dry-ssh.json` можно передать через `-d/--dry-ssh` для сопоставления
физического и логического имени интерфейса. Макросы скорости, область
мониторинга и интерфейсы для утилизации берутся из NetBox.

## Карта

```bash
python zabbix_map.py \
  --inventory-file netbox_inventory.json \
  --zabbix --print-table
python zabbix_map.py \
  --inventory-file netbox_inventory.json \
  --zabbix --update-map
python zabbix_map.py --create-map
python zabbix_map.py --export-map 10 > map_10.json
```

`--export-map` читает карту. `--create-map` создаёт пустую карту, если её ещё
нет.

## Агрегаты, сервисы и SLA

Общий лимит поставщика хранится в поле NetBox `aggregate_limit_gbps`:

```bash
python zabbix_provider_aggregate.py \
  --inventory-file netbox_inventory.json
python zabbix_provider_services.py \
  --inventory-file netbox_inventory.json \
  --parent-service "Uplinks providers"
python zabbix_provider_sla.py \
  --inventory-file netbox_inventory.json \
  --days 30
```

Отчёт доступности берёт целевой уровень из NetBox или из
`PROJECT_PROVIDER_SLO_PERCENT`. Для сопоставления физического и логического
имени отчёт SLA может дополнительно принять `--dry-ssh dry-ssh.json`.

## Отчёты и очистка Zabbix

Подробное описание снимка NetBox, плана Zabbix, журналов запуска, отчёта
доступности и других результатов находится в
[`reports/README.md`](reports/README.md).

Очистка Zabbix — отдельная ручная операция:

```bash
python zabbix_uplinks_cleanup.py --dry-run
python zabbix_uplinks_cleanup.py
```

Рабочие скрипты не удаляют объекты NetBox.
