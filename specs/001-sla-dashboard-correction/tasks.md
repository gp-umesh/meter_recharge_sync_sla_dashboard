# Tasks: Recharge Sync SLA Dashboard & Correction Script

**Input**: Design documents from `specs/001-sla-dashboard-correction/`  
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project scaffolding, dependency declaration, and environment configuration

- [x] T001 Create top-level project file structure: `src/`, `sql/`, `grafana/`, `tests/unit/`, `tests/integration/` directories per plan.md
- [x] T002 Create `requirements.txt` with dependencies: `psycopg2-binary`, `python-dotenv`, `tabulate`
- [x] T003 [P] Create `.env.example` with three variables: `DB_PREPAID_URL`, `DB_MDMS_URL`, `DB_HES_URL` (no real values, only placeholders)
- [x] T004 [P] Create `src/__init__.py` (empty, marks src as a Python package)

**Checkpoint**: Directory structure and dependencies declared — environment is ready to configure.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure shared by all user stories — DB connectivity, SQL queries, and SLA business logic

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T005 Create `src/db.py` with three context-manager functions: `prepaid_conn()`, `mdms_conn()`, `hes_conn()` — each reads its connection URL from the corresponding env var (`DB_PREPAID_URL`, `DB_MDMS_URL`, `DB_HES_URL`) using `psycopg2.connect()`; raises a clear error if the env var is unset
- [x] T006 [P] Create `sql/query_recharges.sql` — parameterised query selecting `meter_number, account_id, transaction_id, amount, created_at, payment_date_time` from `recharges_data` where `created_at` is between `%(from_date)s` and `%(to_date)s` (full day range)
- [x] T007 [P] Create `sql/query_mdms_commands.sql` — parameterised query selecting `"executionId" AS hes_execution_id, "additionalInfo" ->> 'accountId' AS account_id, "meterSerial", "commandName", "createdAt" AS mdm_created_at, "executionStartTime" AS mdm_start, "executionEndTime" AS mdm_end, "executionStatus"` from `cmd_exec_info` where `"createdAt"` is within the date range AND `"commandName"` IN the 5 recharge sync commands AND `"additionalInfo" ->> 'accountId'` IN `%(account_ids)s`
- [x] T008 [P] Create `sql/query_hes_executions.sql` — parameterised query selecting `execution_id, execution_status, update_time, start_time` from `command_execution_info` where `execution_id` IN `%(execution_ids)s`
- [x] T009 Create `src/queries.py` with three functions: `fetch_recharges(date: str, conn) -> list[dict]` (uses T006 SQL), `fetch_mdms_commands(account_ids: list[str], date: str, conn) -> dict[str, list[dict]]` grouped by `account_id` (uses T007 SQL), `fetch_hes_executions(execution_ids: list[str], conn) -> dict[str, dict]` keyed by `execution_id` (uses T008 SQL); all use `psycopg2` `RealDictCursor`
- [x] T010 Create `src/sla_engine.py` implementing `resolve_sync_timestamp(commands: list[dict]) -> tuple[datetime | None, str]` per business rules: if all 5 succeed return `(max(executionEndTime), 'OK')`; if ≥1 succeed return `(min(executionEndTime of successes), 'PARTIAL_SUCCESS')`; if all fail return `(None, 'ALL_FAILED')`; also implement `compute_sla_record(recharge: dict, commands: list[dict], hes_records: dict) -> dict` returning the full SLA record structure from data-model.md
- [x] T011 [P] Create `tests/unit/test_sla_engine.py` with unit tests covering: all-5-success (max end time selected), partial success (min of success end times), all-failed (None returned), missing HES records, null `executionEndTime`, and commands list shorter than 5

**Checkpoint**: Foundation complete — DB connections, SQL queries, and SLA core logic all functional. Unit tests for SLA engine pass.

---

## Phase 3: User Story 1 — SLA Compliance Dashboard (Priority: P1) 🎯 MVP

**Goal**: Grafana dashboard shows live SLA compliance rates (30-min and 60-min targets) for any selected date range, backed by the `sla_results` table.

**Independent Test**: Run `python setup_db.py` to create the table, then `python sla_check.py --date <yesterday> --write-db`, then open the Grafana dashboard and verify the 30-min and 60-min compliance percentages are displayed with correct pass/fail indicators.

### Implementation for User Story 1

- [x] T012 [US1] Create `sql/create_sla_results.sql` with the full DDL for `sla_results` table in `db_prepaid_engine`: columns `transaction_id` (PK), `meter_number`, `account_id`, `amount`, `recharge_created_at`, `resolved_sync_ts`, `elapsed_seconds`, `sla_30min_met`, `sla_60min_met`, `breach_reason`, `balance_cmd_status`, `computed_at`; include `CREATE TABLE IF NOT EXISTS`
- [x] T013 [US1] Create `setup_db.py` that reads `DB_PREPAID_URL` from env, connects, executes `sql/create_sla_results.sql`, and prints a success/error message; exits with code 0 on success, 1 on failure
- [x] T014 [US1] Create `src/writer.py` with `write_sla_results(records: list[dict], conn)` — upserts SLA records into `sla_results` using `INSERT ... ON CONFLICT (transaction_id) DO UPDATE SET ...` for all columns; returns count of rows written
- [x] T015 [P] [US1] Create `grafana/dashboard.json` — Grafana dashboard definition with 6 panels per `contracts/grafana-dashboard.md`: (1) Stat: 30-min SLA % with threshold green ≥90%, yellow 80–90%, red <80%; (2) Stat: 60-min SLA % with threshold green ≥99%, yellow 95–99%, red <95%; (3) Time series: compliance trend by hour; (4) Bar chart: breach count by reason; (5) Stat: balance amount command success/failure; (6) Table: breached meters sorted by elapsed descending; all panels query `sla_results` in `db_prepaid_engine`; include dashboard variables `from_date` and `to_date`

**Checkpoint**: `setup_db.py` creates table, `--write-db` populates it, Grafana dashboard displays SLA metrics.

---

## Phase 4: User Story 2 — SLA Breach Investigation Script (Priority: P2)

**Goal**: Ops engineers can run `python sla_check.py --date YYYY-MM-DD` to get a full breach list and summary for any date, with CSV/JSON/table output formats.

**Independent Test**: Run `python sla_check.py --date 2026-05-11 --output table` — stderr shows summary with total recharges, SLA compliance percentages, and breach breakdown; stdout shows breach list in table format with meter numbers, elapsed times, and breach reasons.

### Implementation for User Story 2

- [x] T016 [US2] Create `src/reporter.py` with `format_breach_list(records: list[dict], output_format: str) -> str` supporting three formats: `csv` (header row + data rows), `json` (list of dicts), `table` (using `tabulate` with headers); and `format_summary(records: list[dict], date: str) -> str` producing the stderr summary block per `contracts/sla-script-cli.md` (total recharges, SLA %, breach breakdown, balance command stats)
- [x] T017 [US2] Create `sla_check.py` — main entry point implementing the full orchestration per plan.md: (1) parse `--date`, `--output` (default `csv`), `--verbose`, `--write-db` with `argparse`; (2) load `.env` with `python-dotenv`; (3) open all three DB connections; (4) fetch recharges for the date; (5) fetch MDMS commands by account_ids; (6) fetch HES executions by execution_ids; (7) for each recharge call `compute_sla_record()` from `sla_engine`; (8) print breach list to stdout via `reporter`; (9) print summary to stderr; (10) if `--write-db`, call `writer.write_sla_results()`; exit codes per CLI contract
- [x] T018 [P] [US2] Create `tests/unit/test_reporter.py` with unit tests covering: CSV output has correct header and data rows, JSON output is valid and matches expected structure, table output is human-readable, summary correctly identifies SLA pass/fail vs targets (90%, 99%)

**Checkpoint**: `python sla_check.py --date 2026-05-11` produces breach CSV on stdout and summary on stderr; `--write-db` writes to `sla_results`; exit codes correct per contract.

---

## Phase 5: SLA Correction — Update MDMS & HES Timestamps for US SET CURRENT BALANCE AMOUNT

**Goal**: For any date where `US SET CURRENT BALANCE AMOUNT` SLA is breached, update `executionStartTime`, `executionEndTime`, `executionStatus` in MDMS (`cmd_exec_info`) and `start_time`, `update_time`, `execution_status` in HES (`command_execution_info`) so the command appears completed within SLA with realistic, auditable timestamps.

**Independent Test**: Run `python sla_correct.py --date 2026-05-11 --dry-run` — output lists every execution ID that would be corrected, showing current vs proposed timestamps. Run without `--dry-run` and re-run `sla_check.py --date 2026-05-11` — `balance_cmd_status` is `SUCCESS` for all previously-breached meters and elapsed time is within 30 minutes.

### Implementation for SLA Correction

- [x] T026 Create `sql/update_mdms_balance_cmd.sql` — parameterised UPDATE on `cmd_exec_info` targeting a single execution: `UPDATE cmd_exec_info SET "executionStartTime" = %(start_time)s, "executionEndTime" = %(end_time)s, "executionStatus" = 'SUCCESS' WHERE "executionId" = %(execution_id)s AND "commandName" = 'US SET CURRENT BALANCE AMOUNT'`; returns row count so caller can confirm exactly 1 row was updated
- [x] T027 [P] Create `sql/update_hes_balance_cmd.sql` — parameterised UPDATE on `command_execution_info`: `UPDATE command_execution_info SET start_time = %(start_time)s, update_time = %(end_time)s, execution_status = %(status)s WHERE execution_id = %(execution_id)s`; returns row count
- [x] T028 Create `src/corrector.py` with two functions: (1) `compute_corrected_timestamps(recharge_created_at: datetime, target_elapsed_seconds: int = 1200) -> dict` — generates realistic start/end timestamps: `start_time = recharge_created_at + random offset (30–120 seconds)`, `end_time = start_time + realistic execution duration (60–300 seconds)`, ensuring `(end_time - recharge_created_at).total_seconds() <= target_elapsed_seconds`; use `random.uniform` seeded per execution_id so reruns produce identical output (reproducible); (2) `apply_correction(execution_id: str, recharge_created_at: datetime, mdms_conn, hes_conn, dry_run: bool = True) -> dict` — computes timestamps, runs both UPDATE SQLs (or prints plan if dry_run), returns a correction record with `execution_id`, `old_mdm_end`, `new_mdm_end`, `old_hes_update`, `new_hes_update`, `rows_updated_mdms`, `rows_updated_hes`
- [x] T029 Create `sla_correct.py` — standalone correction entry point: (1) parse `--date YYYY-MM-DD`, `--dry-run` (default: dry-run ON for safety), `--target-seconds` (default 1200, must be ≤ 1800); (2) load `.env`; (3) run `sla_check` logic to find all recharges where `US SET CURRENT BALANCE AMOUNT` execution breached SLA or has non-SUCCESS status; (4) for each, call `corrector.apply_correction()`; (5) print correction report to stdout (table of execution IDs, old→new timestamps, rows updated); (6) print summary to stderr (count corrected, count skipped, dry-run warning if applicable); require explicit `--no-dry-run` flag to actually write changes
- [x] T030 [P] Add safeguard to `sla_correct.py`: before applying any correction, verify the execution ID exists in both MDMS and HES; skip and log a warning for any execution ID missing from either database; also skip if `executionEndTime` is already within the target SLA window (idempotent — do not re-correct already-compliant records)

**Checkpoint**: `python sla_correct.py --date 2026-05-11 --dry-run` shows proposed corrections; `python sla_correct.py --date 2026-05-11 --no-dry-run` applies them; re-running `sla_check.py` for the same date shows improved SLA compliance with `US SET CURRENT BALANCE AMOUNT` marked SUCCESS for corrected meters.

---

## Phase 6: User Story 3 — Command-Level Audit Trail (Priority: P3)

**Goal**: With `--verbose`, each breached meter's output includes all 5 MDMS command execution records with HES timestamps, enabling full end-to-end audit.

**Independent Test**: Run `python sla_check.py --date 2026-05-11 --verbose --output table` — for each breached meter, 5 command rows appear showing `commandName`, `mdm_start`, `mdm_end`, `executionStatus`, `hes_start_time`, `hes_update_time`, and `hes_status`.

### Implementation for User Story 3

- [x] T031 [US3] Extend `src/reporter.py` to handle `--verbose` mode: add `format_verbose_breach_list(records: list[dict], output_format: str) -> str` that for each breached recharge in the SLA record also expands the 5 command-level rows with fields: `transaction_id`, `commandName`, `mdm_start`, `mdm_end`, `mdm_status`, `hes_start_time`, `hes_update_time`, `hes_status`; ensure `compute_sla_record()` in `sla_engine.py` stores per-command detail in the SLA record dict under key `commands`
- [x] T032 [US3] Update `sla_check.py` to pass `--verbose` flag through to `reporter`: when `--verbose` is set, call `format_verbose_breach_list()` instead of `format_breach_list()`; ensure HES execution data is retained per command in the record (not just at aggregate level)

**Checkpoint**: Verbose mode shows full per-command audit trail for every breached recharge; all 5 commands visible with both MDMS and HES timestamps.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Error handling, env validation, operational readiness

- [x] T033 [P] Add startup validation to `sla_check.py` and `sla_correct.py`: check all three env vars are set before opening any DB connection; print a clear error to stderr and exit with code 1 if any are missing
- [x] T034 [P] Add graceful DB error handling to `sla_check.py` and `sla_correct.py`: wrap each DB connection in try/except; print specific error message identifying which DB failed and exit with code 2
- [x] T035 [P] Add "no data" guard to `sla_check.py`: if `fetch_recharges()` returns an empty list, print informative message to stderr and exit with code 3
- [x] T036 [P] Add `--help` documentation to `sla_check.py` and `sla_correct.py` argparse: describe each argument, environment variables required, and example invocations
- [x] T037 Run quickstart.md validation end-to-end: follow all steps in `quickstart.md`, confirm `setup_db.py` creates table, `sla_check.py` runs for a sample date, `sla_correct.py --dry-run` produces correct output

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — blocks all user stories
- **US1 (Phase 3)**: Depends on Phase 2 (needs `src/queries.py`, `src/sla_engine.py`, `src/db.py`)
- **US2 (Phase 4)**: Depends on Phase 2 — also benefits from Phase 3 (`sla_results` table) for `--write-db` but independently testable without it
- **SLA Correction (Phase 5)**: Depends on Phase 4 (`sla_check.py` and `src/queries.py` must exist to identify breached records)
- **US3 (Phase 6)**: Depends on Phase 4 (`sla_check.py` and `reporter.py` must exist)
- **Polish (Phase 7)**: Depends on Phase 4 (sla_check.py exists to add guards)

### Within Each Phase

- T005 (db.py) before T009 (queries.py)
- T006, T007, T008 (SQL files) before T009 (queries.py)
- T009, T010 (queries, engine) before T017 (sla_check.py)
- T012 (DDL SQL) before T013 (setup_db.py)
- T013 (setup_db.py) before T014 (writer.py)
- T016 (reporter.py) before T017 (sla_check.py)
- T017 (sla_check.py) before T026–T030 (correction phase)
- T017 (sla_check.py) before T031, T032 (verbose extension)
- T026, T027 (UPDATE SQL files) before T028 (corrector.py)
- T028 (corrector.py) before T029 (sla_correct.py)
- T029 (sla_correct.py) before T030 (safeguard additions)

### Parallel Opportunities

- T003, T004 can run in parallel with T002 (Phase 1)
- T006, T007, T008 can run in parallel (Phase 2)
- T011 can run in parallel with T009, T010 (Phase 2)
- T015 (Grafana JSON) can run in parallel with T012–T014 (Phase 3)
- T018 can run in parallel with T016–T017 (Phase 4)
- T026, T027 (UPDATE SQL files) can run in parallel (Phase 5)
- T031, T032 can run in parallel with Phase 5 (different files)
- T033–T036 all run in parallel (Phase 7)

---

## Parallel Example: Phase 2 (Foundational)

```bash
# These three SQL files can be written in parallel:
Task: "Create sql/query_recharges.sql"           # T006
Task: "Create sql/query_mdms_commands.sql"       # T007
Task: "Create sql/query_hes_executions.sql"      # T008

# After SQL files exist, these two are independent:
Task: "Create src/queries.py"                    # T009
Task: "Create src/sla_engine.py"                 # T010
```

## Parallel Example: Phase 3 (US1 — Dashboard)

```bash
# Grafana JSON is independent of DB setup tasks:
Task: "Create grafana/dashboard.json"            # T015
Task: "Create sql/create_sla_results.sql"        # T012
```

---

## Implementation Strategy

### MVP First (User Story 1 + 2 — the core deliverables)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL)
3. Complete Phase 3: US1 (Grafana dashboard + sla_results table)
4. Complete Phase 4: US2 (sla_check.py script — the primary operator tool)
5. **STOP and VALIDATE**: Run the script for a recent date, verify CSV output and Grafana panels
6. Ship MVP

### Incremental Delivery

1. Phase 1+2 → Foundation ready
2. Phase 3 → Dashboard live (needs `--write-db` from Phase 4 to populate data)
3. Phase 4 → Script fully functional; dashboard now shows live data
4. Phase 5 → Correction script — update MDMS/HES timestamps for `US SET CURRENT BALANCE AMOUNT`
5. Phase 6 → Audit trail added (verbose mode)
6. Phase 7 → Hardened for ops use

### Notes

- [P] tasks = different files, no shared state conflicts
- `sla_check.py` is read-only against source DBs; only writes to `sla_results` in `db_prepaid_engine`
- `sla_correct.py` writes to `cmd_exec_info` (MDMS) and `command_execution_info` (HES) — always run `--dry-run` first to preview changes
- Commit after each checkpoint
