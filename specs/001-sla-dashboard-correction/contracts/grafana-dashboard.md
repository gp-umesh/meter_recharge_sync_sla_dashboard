# Contract: Grafana Dashboard Panels

**Type**: Grafana dashboard panel specification  
**Date**: 2026-05-14

---

## Dashboard Identity

- **Name**: Recharge Sync to Meter — SLA Monitoring
- **Folder**: Prepaid / Recharge
- **Variables**: `from_date`, `to_date` (date pickers, default: last 6 hours)
- **Refresh**: Every 5 minutes (auto)
- **Timezone**: Browser-local (IST default)

---

## Panel Specifications

### Panel 1: SLA Compliance — 30-Minute Target

| Property       | Value                                                        |
|----------------|--------------------------------------------------------------|
| **Type**       | Stat / Gauge                                                 |
| **Title**      | SLA Compliance: 30 Minutes (Target: ≥90%)                   |
| **Data source**| db_prepaid_engine (via SLA result view/table)                |
| **Unit**       | Percentage (0–100%)                                          |
| **Thresholds** | Green ≥90%, Yellow 80–90%, Red <80%                         |

**Query** (conceptual — actual SQL in implementation):
```sql
-- % of recharges where elapsed_seconds <= 1800 for selected date range
SELECT 100.0 * COUNT(*) FILTER (WHERE sla_30min_met) / COUNT(*) AS pct_30min
FROM sla_results
WHERE recharge_created_at BETWEEN $from_date AND $to_date
```

---

### Panel 2: SLA Compliance — 60-Minute Target

| Property       | Value                                                        |
|----------------|--------------------------------------------------------------|
| **Type**       | Stat / Gauge                                                 |
| **Title**      | SLA Compliance: 60 Minutes (Target: ≥99%)                   |
| **Data source**| db_prepaid_engine (via SLA result view/table)                |
| **Unit**       | Percentage (0–100%)                                          |
| **Thresholds** | Green ≥99%, Yellow 95–99%, Red <95%                         |

---

### Panel 3: SLA Compliance Over Time

| Property       | Value                                                        |
|----------------|--------------------------------------------------------------|
| **Type**       | Time series                                                  |
| **Title**      | SLA Compliance Trend                                         |
| **Series**     | 30-min compliance %, 60-min compliance % (both on same axes) |
| **X-axis**     | Time (bucketed by hour)                                      |
| **Reference lines** | 90% (dashed), 99% (dashed)                            |

---

### Panel 4: Breach Count by Reason

| Property       | Value                                            |
|----------------|--------------------------------------------------|
| **Type**       | Bar chart / Pie chart                            |
| **Title**      | Breach Distribution by Type                     |
| **Categories** | ALL_FAILED, PARTIAL_SUCCESS, TIMEOUT (>60min)   |

---

### Panel 5: US SET CURRENT BALANCE AMOUNT Status

| Property       | Value                                                      |
|----------------|------------------------------------------------------------|
| **Type**       | Stat panel (2 stats: Success count, Failure count)         |
| **Title**      | Balance Amount Command: Success vs Failure                 |
| **Data source**| db_cmd_exec (MDMS)                                        |
| **Filter**     | commandName = 'US SET CURRENT BALANCE AMOUNT'              |

---

### Panel 6: SLA Breached Meters Table

| Property       | Value                                                            |
|----------------|------------------------------------------------------------------|
| **Type**       | Table                                                            |
| **Title**      | SLA Breached Meters                                              |
| **Columns**    | meter_number, account_id, recharge_created_at, elapsed, breach_reason |
| **Filter**     | sla_60min_met = false (all breaches)                            |
| **Sort**       | elapsed descending (worst first)                                |

---

## Data Strategy for Multi-DB Grafana

Since Grafana cannot join across different PostgreSQL instances natively (without Enterprise multi-source), the SLA results are materialized into a dedicated table in `db_prepaid_engine`:

```sql
-- Table: db_prepaid_engine.sla_results (populated by sla_check.py or a scheduled job)
CREATE TABLE IF NOT EXISTS sla_results (
    transaction_id       VARCHAR PRIMARY KEY,
    meter_number         VARCHAR,
    account_id           VARCHAR,
    amount               NUMERIC,
    recharge_created_at  TIMESTAMPTZ,
    resolved_sync_ts     TIMESTAMPTZ,
    elapsed_seconds      FLOAT,
    sla_30min_met        BOOLEAN,
    sla_60min_met        BOOLEAN,
    breach_reason        VARCHAR,
    balance_cmd_status   VARCHAR,
    computed_at          TIMESTAMPTZ DEFAULT NOW()
);
```

The `sla_check.py` script can optionally write results to this table (via `--write-db` flag) to enable live Grafana querying without FDW setup.
