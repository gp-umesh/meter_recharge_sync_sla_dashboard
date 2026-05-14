# Contract: SLA Correction Script CLI

**Type**: Command-line interface contract  
**Date**: 2026-05-14

---

## Invocation

```bash
python sla_check.py --date YYYY-MM-DD [--output csv|json|table] [--verbose]
```

## Arguments

| Argument        | Required | Default | Description                                             |
|-----------------|----------|---------|---------------------------------------------------------|
| `--date`        | Yes      | —       | Target date to analyse (format: `YYYY-MM-DD`)           |
| `--output`      | No       | `csv`   | Output format for breach list: `csv`, `json`, `table`  |
| `--verbose`     | No       | false   | Print per-command detail for each breached meter        |

## Environment Variables

| Variable        | Required | Description                                         |
|-----------------|----------|-----------------------------------------------------|
| `DB_PREPAID_URL`| Yes      | PostgreSQL connection URL for `db_prepaid_engine`   |
| `DB_MDMS_URL`   | Yes      | PostgreSQL connection URL for `db_cmd_exec` (MDMS)  |
| `DB_HES_URL`    | Yes      | PostgreSQL connection URL for HES routing service   |

Connection URL format: `postgresql://user:password@host:5432/dbname`

## Stdout Output (breach list)

### CSV format (default)
```
transaction_id,meter_number,account_id,amount,recharge_created_at,resolved_sync_ts,elapsed_seconds,sla_30min_met,sla_60min_met,breach_reason,balance_cmd_status
"TXN123","MTR456","ACC789",500.00,"2026-05-11T08:00:00+05:30","2026-05-11T09:15:00+05:30",4500,false,false,"PARTIAL_SUCCESS","SUCCESS"
```

### JSON format
```json
[
  {
    "transaction_id": "TXN123",
    "meter_number": "MTR456",
    "account_id": "ACC789",
    "amount": 500.00,
    "recharge_created_at": "2026-05-11T08:00:00+05:30",
    "resolved_sync_ts": "2026-05-11T09:15:00+05:30",
    "elapsed_seconds": 4500,
    "sla_30min_met": false,
    "sla_60min_met": false,
    "breach_reason": "PARTIAL_SUCCESS",
    "balance_cmd_status": "SUCCESS"
  }
]
```

### Table format
```
TRANSACTION_ID  METER_NUMBER  ACCOUNT_ID  ELAPSED    SLA_30  SLA_60  BREACH_REASON
TXN123          MTR456        ACC789      75m 0s     FAIL    FAIL    PARTIAL_SUCCESS
```

## Stderr Output (summary — always printed)

```
[SLA Check] Date: 2026-05-11
[SLA Check] Total recharges analysed : 1,245
[SLA Check] Commands retrieved        : 6,225 (5 per recharge expected)
[SLA Check] ─────────────────────────────────────────
[SLA Check] SLA 30-min (target ≥90%) : 87.3%  ← BELOW TARGET
[SLA Check] SLA 60-min (target ≥99%) : 96.1%  ← BELOW TARGET
[SLA Check] ─────────────────────────────────────────
[SLA Check] Breach breakdown:
[SLA Check]   PARTIAL_SUCCESS : 112 recharges
[SLA Check]   ALL_FAILED      :  49 recharges
[SLA Check]   TIMEOUT (>60min):   0 recharges
[SLA Check] ─────────────────────────────────────────
[SLA Check] US SET CURRENT BALANCE AMOUNT:
[SLA Check]   SUCCESS: 1,180 | FAILED: 65
[SLA Check] ─────────────────────────────────────────
[SLA Check] Breach list written to stdout (161 rows)
```

## Exit Codes

| Code | Meaning                                              |
|------|------------------------------------------------------|
| 0    | Completed successfully (breaches may still exist)    |
| 1    | Missing required arguments or environment variables  |
| 2    | Database connection error                            |
| 3    | No recharge data found for the given date            |

## Error Messages

All errors go to stderr:
```
[SLA Check] ERROR: DB_PREPAID_URL environment variable not set
[SLA Check] ERROR: Cannot connect to MDMS database: connection refused
[SLA Check] ERROR: No recharges found for date 2026-05-11
```
