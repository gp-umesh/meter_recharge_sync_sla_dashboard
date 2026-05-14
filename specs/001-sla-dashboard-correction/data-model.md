# Data Model: Recharge Sync SLA Dashboard & Correction Script

**Date**: 2026-05-14  
**Feature**: specs/001-sla-dashboard-correction

---

## Source Entities (read-only — existing databases)

### 1. Recharge (`db_prepaid_engine.recharges_data`)

Represents a prepaid meter top-up event. This is the **SLA clock start point**.

| Field               | Type            | Description                                      |
|---------------------|-----------------|--------------------------------------------------|
| `meter_number`      | VARCHAR         | Meter serial/identifier                          |
| `account_id`        | VARCHAR         | Customer account identifier                      |
| `transaction_id`    | VARCHAR (PK)    | Unique recharge transaction identifier           |
| `amount`            | NUMERIC         | Recharge amount (currency)                       |
| `created_at`        | TIMESTAMPTZ     | When the recharge was received — SLA clock start |
| `payment_date_time` | TIMESTAMPTZ     | Payment processing timestamp                     |

**Key constraint**: `created_at` is the authoritative start of the SLA window.

---

### 2. MDMS Command Execution (`db_cmd_exec.cmd_exec_info`)

One row per command dispatched to HES. Each recharge generates exactly 5 rows — one per command name.

| Field               | Type            | Description                                          |
|---------------------|-----------------|------------------------------------------------------|
| `executionId`       | BIGINT (PK)     | Unique execution ID; join key to HES                 |
| `additionalInfo`    | JSONB           | Contains `accountId` linking back to recharge        |
| `meterSerial`       | VARCHAR         | Meter serial number                                  |
| `commandName`       | VARCHAR         | One of the 5 recharge sync command names             |
| `createdAt`         | TIMESTAMPTZ     | When MDMS received/created the command               |
| `executionStartTime`| TIMESTAMPTZ     | When MDMS started executing the command              |
| `executionEndTime`  | TIMESTAMPTZ     | When MDMS finished the command (null if incomplete)  |
| `executionStatus`   | VARCHAR         | Status: `SUCCESS`, `FAILED`, `IN_PROGRESS`, etc.    |

**The 5 command names**:
1. `US SET CURRENT BALANCE AMOUNT`
2. `US SET CURRENT BALANCE TIME`
3. `US SET LAST RECHARGE TOTAL AMOUNT`
4. `US SET LAST TOKEN RECHARGE AMOUNT`
5. `US SET LAST TOKEN RECHARGE TIME`

**Relationship**: `additionalInfo ->> 'accountId'` joins to `recharges_data.account_id`; `meterSerial` joins to `recharges_data.meter_number`.

---

### 3. HES Command Execution (`db_hes.command_execution_info`)

One row per command routed through HES. Linked to MDMS by execution ID.

| Field              | Type        | Description                                       |
|--------------------|-------------|---------------------------------------------------|
| `execution_id`     | BIGINT (FK) | Matches `cmd_exec_info.executionId`               |
| `execution_status` | VARCHAR     | HES-side status                                   |
| `start_time`       | TIMESTAMPTZ | When HES started processing the command           |
| `update_time`      | TIMESTAMPTZ | Last status update time (end time if complete)    |

**Join key**: `command_execution_info.execution_id = cmd_exec_info."executionId"`

---

## Derived / Computed Entities

### 4. SLA Record (computed in script, optionally persisted)

This is the primary output entity — one row per recharge, summarizing its sync outcome.

| Field                    | Type        | Description                                                    |
|--------------------------|-------------|----------------------------------------------------------------|
| `transaction_id`         | VARCHAR     | Recharge transaction ID                                        |
| `meter_number`           | VARCHAR     | Meter identifier                                               |
| `account_id`             | VARCHAR     | Account identifier                                             |
| `amount`                 | NUMERIC     | Recharge amount                                                |
| `recharge_created_at`    | TIMESTAMPTZ | SLA clock start                                                |
| `commands_total`         | INT         | Total commands fired (expected: 5)                             |
| `commands_success`       | INT         | Number of commands that completed successfully                 |
| `commands_failed`        | INT         | Number of commands that failed                                 |
| `resolved_sync_ts`       | TIMESTAMPTZ | Final sync timestamp per business rules (null if all failed)   |
| `elapsed_seconds`        | FLOAT       | `resolved_sync_ts - recharge_created_at` in seconds           |
| `sla_30min_met`          | BOOLEAN     | `elapsed_seconds <= 1800`                                      |
| `sla_60min_met`          | BOOLEAN     | `elapsed_seconds <= 3600`                                      |
| `breach_reason`          | VARCHAR     | `'ALL_FAILED'`, `'PARTIAL_SUCCESS'`, `'TIMEOUT'`, or `'OK'`   |
| `balance_cmd_status`     | VARCHAR     | Status of `US SET CURRENT BALANCE AMOUNT` specifically         |

---

## SLA Timestamp Resolution Rules

```
Given: commands = list of 5 MDMS commands for one recharge

successful = [c for c in commands if c.executionStatus == 'SUCCESS' and c.executionEndTime IS NOT NULL]
failed      = [c for c in commands if c.executionStatus != 'SUCCESS']

if len(successful) == 5:
    resolved_sync_ts = max(c.executionEndTime for c in successful)   # all succeeded: use last
    breach_reason = 'OK'

elif len(successful) >= 1:
    resolved_sync_ts = min(c.executionEndTime for c in successful)   # partial: use first success
    breach_reason = 'PARTIAL_SUCCESS'

else:
    resolved_sync_ts = NULL                                           # all failed
    breach_reason = 'ALL_FAILED'

elapsed_seconds = (resolved_sync_ts - recharge.created_at).total_seconds()  # null if ALL_FAILED
sla_30min_met = elapsed_seconds is not None and elapsed_seconds <= 1800
sla_60min_met = elapsed_seconds is not None and elapsed_seconds <= 3600
```

---

## State Transitions

```
Recharge Created
    │
    ▼
5 MDMS Commands Dispatched (executionStatus = IN_PROGRESS)
    │
    ├─► All 5 SUCCESS → resolved_ts = max(endTime) → SLA check
    │
    ├─► ≥1 SUCCESS, ≥1 FAILED → resolved_ts = min(endTime of successes) → SLA check
    │
    └─► All FAILED → resolved_ts = NULL → SLA BREACHED (ALL_FAILED)
```

---

## Entity Relationships

```
recharges_data (db_prepaid_engine)
    │  account_id ←→ additionalInfo->>'accountId'
    │  meter_number ←→ meterSerial
    └──► cmd_exec_info (db_cmd_exec) [1:5 per recharge]
             │  executionId ←→ execution_id
             └──► command_execution_info (db_hes) [1:1 per MDMS command]
```
