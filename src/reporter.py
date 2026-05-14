import csv
import io
import json

from tabulate import tabulate


_BREACH_FIELDS = [
    "transaction_id", "meter_number", "account_id", "amount",
    "recharge_created_at", "resolved_sync_ts", "elapsed_seconds",
    "sla_30min_met", "sla_60min_met", "breach_reason", "balance_cmd_status",
]

_VERBOSE_FIELDS = [
    "transaction_id", "commandName", "mdm_start", "mdm_end", "executionStatus",
    "hes_start_time", "hes_update_time", "hes_status",
]


def _breaches(records: list[dict]) -> list[dict]:
    return [r for r in records if not r["sla_60min_met"]]


def format_breach_list(records: list[dict], output_format: str) -> str:
    breached = _breaches(records)
    if output_format == "json":
        rows = [{k: str(r.get(k, "")) for k in _BREACH_FIELDS} for r in breached]
        return json.dumps(rows, indent=2, default=str)
    if output_format == "table":
        rows = [[r.get(k, "") for k in _BREACH_FIELDS] for r in breached]
        return tabulate(rows, headers=_BREACH_FIELDS, tablefmt="simple")
    # default: csv
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_BREACH_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(breached)
    return buf.getvalue()


def format_verbose_breach_list(records: list[dict], output_format: str) -> str:
    breached = _breaches(records)
    expanded = []
    for rec in breached:
        for cmd in rec.get("commands", []):
            row = {
                "transaction_id": rec["transaction_id"],
                "commandName": cmd.get("commandName"),
                "mdm_start": cmd.get("mdm_start"),
                "mdm_end": cmd.get("mdm_end"),
                "executionStatus": cmd.get("executionStatus"),
                "hes_start_time": cmd.get("hes_start_time"),
                "hes_update_time": cmd.get("hes_update_time"),
                "hes_status": cmd.get("hes_status"),
            }
            expanded.append(row)

    if output_format == "json":
        return json.dumps(expanded, indent=2, default=str)
    if output_format == "table":
        rows = [[r.get(k, "") for k in _VERBOSE_FIELDS] for r in expanded]
        return tabulate(rows, headers=_VERBOSE_FIELDS, tablefmt="simple")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_VERBOSE_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(expanded)
    return buf.getvalue()


def format_summary(records: list[dict], date: str) -> str:
    total = len(records)
    if total == 0:
        return f"[SLA Check] Date: {date}\n[SLA Check] No recharges found."

    sla_30 = sum(1 for r in records if r["sla_30min_met"])
    sla_60 = sum(1 for r in records if r["sla_60min_met"])
    pct_30 = 100.0 * sla_30 / total
    pct_60 = 100.0 * sla_60 / total

    flag_30 = "OK" if pct_30 >= 90 else "BELOW TARGET"
    flag_60 = "OK" if pct_60 >= 99 else "BELOW TARGET"

    partial = sum(1 for r in records if r["breach_reason"] == "PARTIAL_SUCCESS")
    all_failed = sum(1 for r in records if r["breach_reason"] == "ALL_FAILED")
    timeout = sum(1 for r in records if not r["sla_60min_met"] and r["breach_reason"] == "OK")

    bal_success = sum(1 for r in records if r["balance_cmd_status"] == "SUCCESS")
    bal_failed = total - bal_success
    breach_count = total - sla_60

    lines = [
        f"[SLA Check] Date: {date}",
        f"[SLA Check] Total recharges analysed : {total:,}",
        f"[SLA Check] ─────────────────────────────────────────",
        f"[SLA Check] SLA 30-min (target ≥90%) : {pct_30:.1f}%  ← {flag_30}",
        f"[SLA Check] SLA 60-min (target ≥99%) : {pct_60:.1f}%  ← {flag_60}",
        f"[SLA Check] ─────────────────────────────────────────",
        f"[SLA Check] Breach breakdown:",
        f"[SLA Check]   PARTIAL_SUCCESS : {partial} recharges",
        f"[SLA Check]   ALL_FAILED      : {all_failed} recharges",
        f"[SLA Check]   TIMEOUT (>60min): {timeout} recharges",
        f"[SLA Check] ─────────────────────────────────────────",
        f"[SLA Check] US SET CURRENT BALANCE AMOUNT:",
        f"[SLA Check]   SUCCESS: {bal_success} | FAILED: {bal_failed}",
        f"[SLA Check] ─────────────────────────────────────────",
        f"[SLA Check] Breach list written to stdout ({breach_count} rows)",
    ]
    return "\n".join(lines)
