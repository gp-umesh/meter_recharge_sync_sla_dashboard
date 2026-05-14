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
