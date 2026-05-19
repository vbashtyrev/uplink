


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

python run_uplinks_full.py --no-fetch --location ALA

# без Burst per-link триггеров (только макросы + util + агрегаты провайдера)
python run_uplinks_full.py --no-burst-triggers
```

**Шаги `run_uplinks_full.py` (по порядку):**

1. `uplinks_stats.py --fetch --json` → `dry-ssh.json` (кэш 24 ч; `--refresh` / `--no-fetch`)
2. `netbox_checks.py` — сверка и `--apply` в NetBox
3. `generate_commit_rates.py` → `commit_rates.json`
4. `netbox_create_circuits.py` — контуры и кабели в NetBox
5. `zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers` — макросы, util, Burst 90%/100%/SLA на линках
6. `zabbix_provider_aggregate.py` — хосты `Uplinks {Provider}`, агрегатные триггеры
7. `zabbix_map.py --zabbix --update-map` — карта с окраской линков (после aggregate)
8. `zabbix_uplinks_dashboard.py` — дашборды
9. `zabbix_provider_services.py` — сервисы и SLA в Zabbix

**Не входит в full run** (отдельные команды ниже): `grafana_uplinks_graph.py`, `zabbix_provider_sla.py`, `netbox_interface_types.py`, cleanup-скрипты.

Логи: `run_logs/YYYY-MM-DD_HH-MM-SS_run.log` и `*_debug.log`.

|----------|----------|


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
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json
```


```bash
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --location ALA
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --dry-run
```

---


```bash
python zabbix_sync_commit_rate.py
```



```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json
```


```bash
python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers
```


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



```json
"_provider_limits": { "Cogent": 10, "Hurricane": 5 }
```


```bash
python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
```


---



```json
"_provider_sla": 99.95
```





```bash
python zabbix_provider_services.py -f commit_rates.json --parent-service "Uplinks providers"
```


```bash
python zabbix_provider_sla.py -f commit_rates.json --days 30
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


```bash
```

---



```bash
python netbox_uplinks_cleanup.py --dry-run

python netbox_uplinks_cleanup.py
```


---


```bash
python uplinks_stats.py --fetch --json > dry-ssh.json

# python netbox_checks.py -f dry-ssh.json --apply

python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json

python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json

python zabbix_sync_commit_rate.py -d dry-ssh.json
# python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers

python zabbix_map.py -f dry-ssh.json --zabbix --update-map

python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json

python zabbix_uplinks_dashboard.py -f dry-ssh.json

python zabbix_provider_services.py -f commit_rates.json --parent-service "Uplinks providers"
# python zabbix_provider_sla.py -f commit_rates.json

# python grafana_uplinks_graph.py -f dry-ssh.json --grafana-api
```

