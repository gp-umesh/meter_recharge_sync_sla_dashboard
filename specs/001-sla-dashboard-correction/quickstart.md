# Quickstart: Recharge Sync SLA Dashboard & Correction Script

**Date**: 2026-05-14

---

## Prerequisites

- Python 3.10+
- Access to three PostgreSQL databases (credentials via env vars)
- `pip` for dependency installation
- Grafana instance (already running at bi.analytics.polarisgrids.com)

---

## Setup

```bash
# Clone / navigate to project
cd meter_recharge_sync_sla_dashboard

# Install Python dependencies
pip install -r requirements.txt

# Copy and fill in database credentials
cp .env.example .env
# Edit .env with your actual connection strings
```

`.env` contents:
```
DB_PREPAID_URL=postgresql://user:pass@host:5432/db_prepaid_engine
DB_MDMS_URL=postgresql://user:pass@host:5432/db_cmd_exec
DB_HES_URL=postgresql://user:pass@host:5432/db_hes
```

---

## Running the SLA Check Script

```bash
# Analyse a specific date (outputs breach CSV to stdout, summary to stderr)
python sla_check.py --date 2026-05-11

# Save breach list to file
python sla_check.py --date 2026-05-11 > breaches_2026-05-11.csv

# Human-readable table output
python sla_check.py --date 2026-05-11 --output table

# JSON output
python sla_check.py --date 2026-05-11 --output json

# Verbose: show per-command detail for each breached meter
python sla_check.py --date 2026-05-11 --verbose

# Write results to sla_results table in db_prepaid_engine (for Grafana)
python sla_check.py --date 2026-05-11 --write-db
```

---

## Expected Output

```
# stderr (always shown):
[SLA Check] Date: 2026-05-11
[SLA Check] Total recharges analysed : 1,245
[SLA Check] SLA 30-min (target ≥90%) : 87.3%  ← BELOW TARGET
[SLA Check] SLA 60-min (target ≥99%) : 96.1%  ← BELOW TARGET
[SLA Check] Breach list written to stdout (161 rows)

# stdout (CSV breach list):
transaction_id,meter_number,account_id,...
"TXN123","MTR456","ACC789",...
```

---

## Grafana Dashboard

The Grafana dashboard reads from the `sla_results` table in `db_prepaid_engine`.

1. Run the script with `--write-db` once to populate the table for today's/yesterday's data
2. Set up a cron job to run nightly (optional):
   ```bash
   # Example cron: run at 01:00 daily for previous day
   0 1 * * * cd /path/to/project && python sla_check.py --date $(date -d yesterday +%F) --write-db >> /var/log/sla_check.log 2>&1
   ```
3. Open the Grafana dashboard and select the date range to view SLA compliance

---

## First-Time DB Setup

To create the `sla_results` table (run once):
```bash
python setup_db.py
```

This creates the `sla_results` table in `db_prepaid_engine` using the schema defined in `sql/create_sla_results.sql`.

---

## Project Structure

```
meter_recharge_sync_sla_dashboard/
├── sla_check.py          # Main SLA analysis script
├── setup_db.py           # One-time DB schema setup
├── requirements.txt      # Python dependencies (psycopg2-binary, python-dotenv)
├── .env.example          # Environment variable template
├── sql/
│   ├── create_sla_results.sql    # DDL for results table
│   ├── query_recharges.sql       # Parameterised recharge query
│   ├── query_mdms_commands.sql   # Parameterised MDMS command query
│   └── query_hes_executions.sql  # Parameterised HES query
└── grafana/
    └── dashboard.json            # Exportable Grafana dashboard definition
```
