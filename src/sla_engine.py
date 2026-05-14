from datetime import datetime


def resolve_sync_timestamp(commands: list[dict]) -> tuple[datetime | None, str]:
    successful = [
        c for c in commands
        if c.get("executionStatus") == "SUCCESS" and c.get("mdm_end") is not None
    ]
    if len(commands) == 5 and len(successful) == 5:
        return max(c["mdm_end"] for c in successful), "OK"
    if successful:
        return min(c["mdm_end"] for c in successful), "PARTIAL_SUCCESS"
    return None, "ALL_FAILED"


def compute_sla_record(
    recharge: dict,
    commands: list[dict],
    hes_records: dict,
) -> dict:
    resolved_ts, breach_reason = resolve_sync_timestamp(commands)
    recharge_ts = recharge["created_at"]

    elapsed = None
    if resolved_ts is not None:
        delta = resolved_ts - recharge_ts
        elapsed = delta.total_seconds()

    sla_30 = elapsed is not None and elapsed <= 1800
    sla_60 = elapsed is not None and elapsed <= 3600

    balance_cmd = next(
        (c for c in commands if c.get("commandName") == "US SET CURRENT BALANCE AMOUNT"),
        None,
    )
    balance_status = balance_cmd["executionStatus"] if balance_cmd else "MISSING"

    enriched_commands = []
    for cmd in commands:
        exec_id = str(cmd.get("hes_execution_id", ""))
        hes = hes_records.get(exec_id, {})
        enriched_commands.append({
            **cmd,
            "hes_start_time": hes.get("start_time"),
            "hes_update_time": hes.get("update_time"),
            "hes_status": hes.get("execution_status"),
        })

    return {
        "transaction_id": recharge["transaction_id"],
        "meter_number": recharge["meter_number"],
        "account_id": recharge["account_id"],
        "amount": recharge["amount"],
        "recharge_created_at": recharge_ts,
        "resolved_sync_ts": resolved_ts,
        "elapsed_seconds": elapsed,
        "sla_30min_met": sla_30,
        "sla_60min_met": sla_60,
        "breach_reason": breach_reason,
        "balance_cmd_status": balance_status,
        "commands": enriched_commands,
    }
