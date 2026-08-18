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

## Force-Correcting SLA Breaches (`sla_force_correct.py`)

Unlike `sla_check.py` (read-only analysis), `sla_force_correct.py` writes to `db_cmd_exec` and `db_hes` to bring breached recharges within the 60-min SLA. Always dry-run first (default) before adding `--no-dry-run`.

```bash
# Dry run for a single date, all meters
python sla_force_correct.py --date 2026-05-12

# Dry run for a date range, restricted to meters in a sat table
python sla_force_correct.py --from-date 2026-07-10 --to-date 2026-08-18 --sat-table sat_14

# Apply for real
python sla_force_correct.py --from-date 2026-07-10 --to-date 2026-08-18 --sat-table sat_14 --no-dry-run
```

### `--create-missing-hes`

Some `cmd_exec_info` rows have `"executionId" IS NULL` — the command was never dispatched to HES, so there is no `command_execution_info` row to correct and the recharge was silently uncorrectable (counted under `skipped_hes`, invisible without checking DB directly). This flag fabricates the missing `command_execution_info` + `command_execution_responses` rows (via `device_info`/`command_info` lookups) and backfills the MDMS `executionId`, then corrects as usual. Meters/commands that can't be resolved (not in `device_info`, or no `command_info` row matching the meter's protocol) are still skipped — counted under `skipped_hes_lookup` — not guessed at. See `research.md` R-11 for the full root-cause writeup.

```bash
python sla_force_correct.py --date 2026-08-02 --sat-table sat_14 --create-missing-hes --no-dry-run
```

### `--skip-retry-cleanup`

By default, a correction also deletes stale `FAILED` HES retry rows for audit-trail consistency (same batch, started after the corrected success time). On some environments, `command_execution_info`'s own `AFTER DELETE` trigger does an **unindexed** scan to decide whether to also delete orphaned metadata — on a large enough table this can turn a single delete into a multi-minute stall, and has been observed to drop the DB connection mid-batch (`SSL connection has been closed unexpectedly`), silently rolling back that entire batch. If a run stalls or drops with that error, rerun with this flag — corrections still apply, only the stale-retry-row cleanup is skipped. See `research.md` R-12 before assuming this is needed; check first whether it actually applies to your environment (does the trigger exist, is there an index on `command_execution_meta_data_id`).

```bash
python sla_force_correct.py --from-date 2026-07-10 --to-date 2026-08-18 --sat-table sat_14 \
    --create-missing-hes --skip-retry-cleanup --no-dry-run
```

### Reading the summary line

```
[Force Correct] DONE | corrected=33 (hes_created=0) skipped(compliant=501 no_cmd=7307 hes=0 hes_lookup=0)
```

| Field | Meaning |
|---|---|
| `corrected` | Recharges successfully brought within SLA |
| `hes_created` | Of those, how many required `--create-missing-hes` to fabricate HES rows |
| `skipped_compliant` | Already within SLA — no action needed (idempotent reruns land here) |
| `skipped_no_cmd` | No MDMS commands at all for that recharge — cannot correct |
| `skipped_hes` | Selected command has a real `executionId` but no matching HES row (rare without `--create-missing-hes`) |
| `skipped_hes_lookup` | `--create-missing-hes` was set but the meter/command couldn't be resolved via `device_info`/`command_info` |

If a date errors out (check stderr for a traceback), that date's corrections were **not** applied — the whole batch rolls back on any exception. Rerun that specific date once the underlying issue (see `research.md` R-11/R-12) is addressed; reruns are idempotent and cheaply skip already-corrected recharges.

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
