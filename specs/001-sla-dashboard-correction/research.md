# Research: Recharge Sync SLA Dashboard & Correction Script

**Date**: 2026-05-14  
**Feature**: specs/001-sla-dashboard-correction

---

## R-01: Database Technology

**Decision**: PostgreSQL for all three databases  
**Rationale**: The SQL in requirements.md uses PostgreSQL-specific syntax: JSONB operator (`->>`) for `"additionalInfo" ->> 'accountId'`, double-quoted identifiers, and timezone-aware timestamps (`+05:30`). This is unambiguous.  
**Alternatives considered**: MySQL, MSSQL — ruled out by syntax evidence.

---

## R-02: Script Language

**Decision**: Python 3.10+  
**Rationale**: Python is the standard choice for database-querying data-pipeline scripts. It has first-class PostgreSQL support (`psycopg2`/`psycopg3`), strong CSV/tabular output libraries, and is easily run ad-hoc by operations staff. It also pairs well with Grafana JSON and CSV exports.  
**Alternatives considered**: Shell (too fragile for multi-DB joins and business logic), Node.js (less ergonomic for ops scripting).

---

## R-03: Python PostgreSQL Library

**Decision**: `psycopg2-binary`  
**Rationale**: Most widely deployed, no compilation step required (binary wheel), works with Python 3.10+, battle-tested. The `-binary` variant avoids OS-level libpq dependency issues in ops environments.  
**Alternatives considered**: `psycopg3` (newer, async-capable, but less ubiquitous in ops tooling), `asyncpg` (async only — unnecessary overhead for a synchronous batch script), `SQLAlchemy` (too heavy for a standalone script).

---

## R-04: SLA Timestamp Resolution Logic

**Decision**: Per business rules defined in requirements.md, implemented as Python logic  
**Rationale**:
- **Success case** (all 5 commands completed): `resolved_ts = max(end_time for all 5 commands)`
- **Failure case** (≥1 success, ≥1 failure): `resolved_ts = min(end_time for completed commands)`  
  Rationale: "the end execution time of the first completed command" = the earliest completion among those that did succeed.
- **All-failure case** (0 commands completed): no `resolved_ts` — treated as SLA breached with unknown duration; flagged separately.
- **SLA elapsed**: `resolved_ts - recharge.created_at` (in seconds)
- **SLA window 1**: elapsed ≤ 1800s (30 min) → counts toward 90% target
- **SLA window 2**: elapsed ≤ 3600s (60 min) → counts toward 99% target

**Alternatives considered**: Doing this in SQL (possible but complex CASE logic across three joined databases — harder to audit and maintain).

---

## R-05: Script Output Format

**Decision**: CSV output to stdout with tabular summary to stderr  
**Rationale**: CSV is universally importable into spreadsheets and Grafana. Writing the breach list to stdout and the summary stats to stderr allows easy redirection (`python sla_check.py 2026-05-11 > breaches.csv`). A human-readable summary (counts, SLA %) is printed to stderr for immediate visibility.  
**Alternatives considered**: JSON (less immediately readable for ops staff), HTML (too heavy), direct DB write (risks polluting source databases).

---

## R-06: Grafana Dashboard Approach

**Decision**: New Grafana dashboard panels using existing PostgreSQL data sources  
**Rationale**: The dashboard is already live at `bi.analytics.polarisgrids.com`. The plan is to add/update panels for SLA tracking using direct SQL queries against the three databases. No new backend service is needed — Grafana's native PostgreSQL plugin handles it. Dashboard JSON (exported from Grafana) will be version-controlled in this repo for reproducibility.  
**Alternatives considered**: Building a custom web app (overkill when Grafana already exists), Metabase (not the tool already in use).

---

## R-07: Cross-Database Querying Strategy

**Decision**: Script performs Python-side joins; Grafana uses per-panel queries per DB  
**Rationale**: All three databases are separate PostgreSQL instances (different connection strings). Foreign Data Wrappers (FDW) would require DBA-level changes. For the script, Python fetches from each DB and joins in-memory — data volumes (daily recharges) are manageable (thousands of rows, not millions). For Grafana, each panel targets one DB, and the SLA computation panel uses `db_prepaid_engine` as the primary source with pre-joined data via a `LEFT JOIN` on an FDW view or a materialized view — or alternatively a single complex query if Grafana multi-source is configured.  
**Alternatives considered**: FDW (requires DBA involvement, out of scope for demo), Grafana multi-source plugin (Enterprise feature, may not be available).

**Pragmatic Grafana approach**: A dedicated **SLA computation view** or scheduled materialized data can be created in `db_prepaid_engine` that pulls from MDMS and HES via FDW — but for the demo/MVP, the script populates a results table in `db_prepaid_engine` for Grafana to query.

---

## R-08: Script Scheduling & Invocation

**Decision**: Manual invocation (ad-hoc script), with cron as optional future step  
**Rationale**: Requirements state "a script which will run for a given date" — implies on-demand execution by an operator. No automated scheduling required at this stage.  
**Interface**: `python sla_check.py --date 2026-05-11`

---

## R-09: Command Focus for Auditability

**Decision**: All 5 commands tracked; `US SET CURRENT BALANCE AMOUNT` highlighted  
**Rationale**: Requirements note that MDMS and HES changes are being made specifically for `US SET CURRENT BALANCE AMOUNT` to ensure it succeeds within SLA. The script and dashboard should surface this command's performance distinctly while still computing aggregate SLA across all 5.

---

## R-10: Database Connection Configuration

**Decision**: Environment variables for DB credentials  
**Rationale**: No hardcoded credentials. Script reads connection strings from environment variables (`DB_PREPAID_URL`, `DB_MDMS_URL`, `DB_HES_URL`). A `.env.example` file is provided; actual `.env` is gitignored.  
**Alternatives considered**: Config file (less portable, risk of accidental commit), CLI arguments (credentials visible in process list).

---

## R-11: Commands Never Dispatched to HES (`--create-missing-hes`)

**Problem**: Some `db_cmd_exec.cmd_exec_info` rows have `"executionId" IS NULL` — the command was created in MDMS but never actually dispatched to HES, so `executionStatus` sits at `FAILED` (or similar) forever and there is no corresponding row in `db_hes.command_execution_info` to correct. Prior to this change, `sla_force_correct.py` silently skipped these (`skipped_hes`), and they were invisible in the summary counts.

**Detection** (run against `db_cmd_exec`, i.e. `DB_MDMS_URL`):
```sql
SELECT "meterSerial", count(*) FILTER (WHERE "executionId" IS NULL) AS null_execid_rows
FROM cmd_exec_info
WHERE "createdAt" >= '<from_date>' AND "createdAt" < '<to_date>'
GROUP BY 1 HAVING count(*) FILTER (WHERE "executionId" IS NULL) > 0;
```
Cross-check a specific meter/date pair the same way described in `quickstart.md` — if `cmd_exec_info.executionId` is NULL for the breaching commands, this is the cause, not a bug in the SLA resolution logic itself.

**Decision**: `sla_force_correct.py --create-missing-hes` resolves the meter's `device_info` row and the command's `command_info` row (disambiguated by matching `communication_protocol_id` between the two — `command_info` has multiple rows per `commandName`, one per protocol) in `db_hes` (`DB_HES_URL`), fabricates a new execution ID, inserts the missing `command_execution_info` + `command_execution_responses` rows, and backfills `cmd_exec_info."executionId"` — matched via `"clientRequestId"` (the real primary key; `executionId` is NULL so it can't be used as the join key here) — before running the normal correction flow.  
**If the meter isn't in `device_info`, or no `command_info` row matches the meter's protocol**: the correction is skipped (counted as `skipped_hes_lookup`), not guessed at with placeholder foreign keys.  
**Alternatives considered**: Leaving these uncorrectable (status quo — undercounts true SLA compliance); fabricating FK values without a real `device_info`/`command_info` lookup (rejected — risks wrong `command_info_id` since the table has no unique `(name)` constraint, only `(name, communication_protocol_id)` is effectively unique in practice).

**Portability note for other environments**: this fix assumes `db_hes` has `device_info` (keyed by `device_serial`) and `command_info` (keyed by `name` + `communication_protocol_id`) tables with the same shape. Verify column names/shapes before reusing `sql/lookup_device_info.sql` / `sql/lookup_command_info.sql` elsewhere.

---

## R-12: Retry-Cleanup Delete Performance (`--skip-retry-cleanup`)

**Problem**: `apply_correction()`'s Step 6 deletes stale `FAILED` retry rows in `db_hes.command_execution_info` for audit-trail consistency (see `src/corrector.py`). That table is partitioned by `created_at` (monthly, ~409M rows total in the observed environment). Its own `AFTER DELETE` trigger, `trg_delete_command_metadata`, runs:
```sql
IF NOT EXISTS (SELECT 1 FROM command_execution_info WHERE command_execution_meta_data_id = OLD.command_execution_meta_data_id) THEN
    DELETE FROM command_execution_metadata WHERE id = OLD.command_execution_meta_data_id;
END IF;
```
with **no index on `command_execution_meta_data_id` and no `created_at` bound**, so when the deleted row is the sole reference to its metadata, this becomes a full unpruned sequential scan across the entire partitioned table. Observed cost: single-digit minutes per affected delete, occasionally long enough to exceed the DB connection's idle/network timeout and drop the session mid-transaction (`psycopg2.OperationalError: SSL connection has been closed unexpectedly`) — which rolls back the *entire in-flight batch*, not just the slow row.

**Detection** (run against `db_hes`, i.e. `DB_HES_URL`): if a `sla_force_correct.py` run stalls or drops with an SSL error, check `pg_stat_activity` for a long-running `DELETE FROM command_execution_info WHERE execution_id = ANY(...)` with `wait_event = DataFileRead`. Confirm no index exists:
```sql
SELECT indexname FROM pg_indexes
WHERE tablename LIKE 'command_execution_info%' AND indexdef ILIKE '%meta_data_id%';
-- 0 rows in the observed environment
```

**Decision**: `sla_force_correct.py --skip-retry-cleanup` skips Step 6 entirely — corrections still apply (command marked `SUCCESS`, SLA-compliant), but stale `FAILED` retry rows are left in place instead of deleted. Also reduced `BATCH_SIZE` 100 → 20 (smaller blast radius per commit if a connection does drop) and, independent of the flag, bounded the Step 6 query itself by a `created_at` window so it can at least use partition pruning when the flag isn't set.  
**Real fix, out of scope for this script**: add an index on `command_execution_info.command_execution_meta_data_id` in `db_hes`. That fixes the cost for every writer of the table, not just this script — needs DBA sign-off since it's a schema change to a live, heavily-written 409M-row production table.  
**Alternatives considered**: statement_timeout wrapping just Step 6 (still leaves the slow-row problem unsolved, just fails faster); leaving it as-is and re-running failed dates after the fact (viable but wastes time repeatedly hitting the same unindexed scan).

**Portability note for other environments**: check whether `command_execution_info` (or its equivalent) has an `AFTER DELETE` trigger with an unindexed lookup before assuming retry-cleanup is cheap. `\d command_execution_info` in `psql` will show trigger names; `SELECT prosrc FROM pg_proc WHERE proname = '<trigger function name>'` shows what it actually does.
