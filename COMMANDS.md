# Основные команды

Перед запуском задайте адреса и токены:

```bash
cp urls.env.example urls.env
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="..."
```

Поставщик, канал, терминация и кабель заранее создаются вручную в NetBox.
Рабочий запуск только читает их и настраивает Zabbix.

## Полный запуск

```bash
python run_uplinks_full.py
python run_uplinks_full.py --report uplinks_run_report.txt --no-stop-on-error
python run_uplinks_full.py --no-burst-triggers
```

Область мониторинга строится по цепочке NetBox:

```text
Provider → Circuit → Termination A → Cable → Interface → Device
```

Подходят только каналы типа `Uplink` со статусом `Active`, полным кабельным
путём без разветвления и конечным устройством с тегом `border`.

## Предварительный план

План (то есть отчёт без записи) читает NetBox и Zabbix, но ничего не меняет:

```bash
python run_uplinks_full.py --plan --report uplinks_plan_report.txt
python zabbix_uplinks_plan.py \
  --inventory-file netbox_inventory.json \
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

## Сбор данных устройств и проверка NetBox

Эти команды относятся к отдельному ручному сбору по SSH и не входят в
NetBox-first запуск:

```bash
python uplinks_stats.py --fetch --json > dry-ssh.json
python uplinks_stats.py --fetch --json --host "router-001" > dry-ssh.json
python netbox_checks.py -f dry-ssh.json --all --mt-ref --show-change
python netbox_checks.py -f dry-ssh.json --mediatype --mt-ref --apply
python netbox_interface_types.py -o netbox_interface_types.json
```

`--apply` записывает проверенные изменения в NetBox. Для безопасного полного
запуска сверки используется `--existing-only`: отсутствующие интерфейсы,
MAC-адреса и IP-адреса не создаются.

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

`--export-map` только читает карту через API. `--create-map` только создаёт
пустую карту, если карты с нужным именем ещё нет.

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

SLA (целевой уровень доступности) берётся из NetBox или из
`PROJECT_PROVIDER_SLO_PERCENT`. Для сопоставления физического и логического
имени отчёт SLA может дополнительно принять `--dry-ssh dry-ssh.json`.

## Grafana и очистка Zabbix

```bash
python grafana_uplinks_graph.py \
  -f dry-ssh.json -o grafana_uplinks_graph.json
python grafana_uplinks_graph.py \
  -f dry-ssh.json --zabbix --grafana-api

python zabbix_uplinks_cleanup.py --dry-run
python zabbix_uplinks_cleanup.py
```

Очистка Zabbix — отдельная ручная операция. Объекты NetBox рабочими
скриптами не удаляются.
