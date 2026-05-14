def write_sla_results(records: list[dict], conn) -> int:
    if not records:
        return 0

    sql = """
        INSERT INTO sla_results (
            transaction_id, meter_number, account_id, amount,
            recharge_created_at, resolved_sync_ts, elapsed_seconds,
            sla_30min_met, sla_60min_met, breach_reason,
            balance_cmd_status, computed_at
        ) VALUES (
            %(transaction_id)s, %(meter_number)s, %(account_id)s, %(amount)s,
            %(recharge_created_at)s, %(resolved_sync_ts)s, %(elapsed_seconds)s,
            %(sla_30min_met)s, %(sla_60min_met)s, %(breach_reason)s,
            %(balance_cmd_status)s, NOW()
        )
        ON CONFLICT (transaction_id) DO UPDATE SET
            meter_number        = EXCLUDED.meter_number,
            account_id          = EXCLUDED.account_id,
            amount              = EXCLUDED.amount,
            recharge_created_at = EXCLUDED.recharge_created_at,
            resolved_sync_ts    = EXCLUDED.resolved_sync_ts,
            elapsed_seconds     = EXCLUDED.elapsed_seconds,
            sla_30min_met       = EXCLUDED.sla_30min_met,
            sla_60min_met       = EXCLUDED.sla_60min_met,
            breach_reason       = EXCLUDED.breach_reason,
            balance_cmd_status  = EXCLUDED.balance_cmd_status,
            computed_at         = NOW();
    """
    with conn.cursor() as cur:
        cur.executemany(sql, records)
    conn.commit()
    return len(records)
