

```bash
cd /path/to/uplinks
source .venv/bin/activate

export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="..."
export ZABBIX_URL="https://zabbix.example.com"
export ZABBIX_TOKEN="..."

export SSH_USERNAME="..."
export SSH_PASSWORD="..."
```

## Рабочий режим

Провайдер, контур, терминация и кабель создаются человеком в NetBox.
Обычный полный запуск только читает эту цепочку, получает данные с
оборудования и обновляет существующие поля интерфейса.

Проверка NetBox без изменений:

```bash
python netbox_uplinks_inventory.py --dry-run
python netbox_uplinks_inventory.py --json --dry-run
```

Обычный запуск не создаёт и не удаляет провайдеров, контуров, терминаций и
кабелей. Для интерфейсов используется безопасный режим `--existing-only`:
отсутствующие интерфейсы, MAC и IP не создаются, а перепривязки не выполняются.

Старый автоматический путь доступен только явно:

```bash
python run_uplinks_full.py --auto
```

Не запускайте `--auto` на рабочем NetBox: старый код может заменить кабель.


---



```bash
python run_uplinks_full.py

python run_uplinks_full.py --refresh

python run_uplinks_full.py --no-fetch
```

Полный запуск в рабочем режиме:

```bash
python run_uplinks_full.py
```

Он использует:

```text
NetBox inventory → SSH → existing-only checks → Zabbix
```


```bash
python uplinks_stats.py --fetch --json > dry-ssh.json

python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json


python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json

python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers

python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_uplinks_dashboard.py -f dry-ssh.json

python zabbix_provider_services.py -f commit_rates.json --parent-service 'Uplinks providers'
```

---



```bash
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json

python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers

python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
```


---




```bash
python run_uplinks_full.py --refresh
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json
python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers
python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```


---



```bash
python zabbix_provider_aggregate.py -f commit_rates.json -d dry-ssh.json
python zabbix_map.py -f dry-ssh.json --zabbix --update-map
python zabbix_uplinks_dashboard.py -f dry-ssh.json
```


---



```bash
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --dry-run
python zabbix_sync_commit_rate.py -d dry-ssh.json --dry-run
```

---




```bash

python zabbix_sync_commit_rate.py -d dry-ssh.json

python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers
```

---



```bash
python run_uplinks_full.py --no-fetch --location ALA
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json --location ALA
```

---
