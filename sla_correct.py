#!/usr/bin/env python3
"""SLA correction script — updates MDMS and HES records for SLA-breached recharges."""
import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv
from tabulate import tabulate

load_dotenv()


def _check_env():
    missing = [v for v in ("DB_PREPAID_URL", "DB_MDMS_URL", "DB_HES_URL") if not os.environ.get(v)]
    if missing:
        for v in missing:
            print(f"[SLA Correct] ERROR: {v} environment variable is not set", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Correct SLA-breached recharges by updating timestamps and status in MDMS and HES.\n"
            "Randomly selects one eligible command per recharge to mark SUCCESS.\n"
            "Sets createdAt and executionStartTime = recharge.created_at for ALL 5 commands.\n"
            "Updates cmd_exec_response_data and command_execution_responses for the selected command."
        ),
        epilog=(
            "Environment variables required:\n"
            "  DB_PREPAID_URL  PostgreSQL URL for db_prepaid_engine\n"
            "  DB_MDMS_URL     PostgreSQL URL for db_cmd_exec (MDMS)\n"
            "  DB_HES_URL      PostgreSQL URL for HES routing service\n\n"
            "Examples:\n"
            "  python sla_correct.py --date 2026-05-12                       # dry-run (safe default)\n"
            "  python sla_correct.py --date 2026-05-12 --no-dry-run          # apply corrections\n"
            "  python sla_correct.py --date 2026-05-12 --target-seconds 900  # 15-min target\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", required=True, help="Target date (YYYY-MM-DD)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually apply corrections (default is dry-run only)")
    parser.add_argument("--target-seconds", type=int, default=1200,
                        help="Target elapsed seconds from recharge to sync (default: 1200 = 20 min)")
    args = parser.parse_args()

    if args.target_seconds > 1800:
        print("[SLA Correct] ERROR: --target-seconds must be ≤ 1800 (30-min SLA window)", file=sys.stderr)
        sys.exit(1)

    dry_run = not args.no_dry_run
    _check_env()

    from src.db import prepaid_conn, mdms_conn, hes_conn
    from src.queries import fetch_recharges, fetch_mdms_commands, fetch_hes_executions
    from src.sla_engine import resolve_sync_timestamp
    from src.corrector import apply_correction, select_eligible_command
    from src.validator import validation_conn, fetch_meter_ldp_map, is_meter_communicating

    try:
        with prepaid_conn() as p_conn:
            recharges = fetch_recharges(args.date, p_conn)
    except Exception as exc:
        print(f"[SLA Correct] ERROR: Cannot connect to prepaid DB: {exc}", file=sys.stderr)
        sys.exit(2)

    if not recharges:
        print(f"[SLA Correct] ERROR: No recharges found for date {args.date}", file=sys.stderr)
        sys.exit(3)

    account_ids = [r["account_id"] for r in recharges]

    try:
        with mdms_conn() as m_conn_read:
            commands_by_account = fetch_mdms_commands(account_ids, args.date, m_conn_read)
    except Exception as exc:
        print(f"[SLA Correct] ERROR: Cannot connect to MDMS DB: {exc}", file=sys.stderr)
        sys.exit(2)

    # Batch-fetch meter communication status (one query for all meters in the date)
    try:
        val_conn = validation_conn(os.environ["DB_MDMS_URL"])
        all_meter_serials = list({r["meter_number"] for r in recharges})
        ldp_map = fetch_meter_ldp_map(all_meter_serials, val_conn)
        val_conn.close()
    except Exception as exc:
        print(f"[SLA Correct] ERROR: Cannot connect to validation_rules DB: {exc}", file=sys.stderr)
        sys.exit(2)

    # Identify breached recharges that have at least one eligible command
    candidates = []
    skipped_compliant = 0
    skipped_no_eligible = 0
    skipped_non_communicating = 0

    for recharge in recharges:
        cmds = commands_by_account.get(recharge["account_id"], [])
        resolved_ts, breach_reason = resolve_sync_timestamp(cmds)

        # Skip if already within target SLA window (idempotent)
        if resolved_ts is not None:
            elapsed = (resolved_ts - recharge["created_at"]).total_seconds()
            if elapsed <= args.target_seconds:
                skipped_compliant += 1
                continue

        # Skip if no eligible command available to make SUCCESS
        if select_eligible_command(cmds, seed=recharge['transaction_id']) is None:
            skipped_no_eligible += 1
            continue

        # Skip if meter was not communicating at recharge time
        ok, detail = is_meter_communicating(recharge['meter_number'], recharge['created_at'], ldp_map)
        if not ok:
            print(
                f"[SLA Correct] SKIP non-comm | meter={recharge['meter_number']} "
                f"txn={recharge['transaction_id']} "
                f"com_type={detail['com_type']} "
                f"meter_ldp={detail['meter_ldp']} "
                f"dcu_ldp={detail['dcu_ldp']} "
                f"recharge={recharge['created_at'].strftime('%Y-%m-%d %H:%M:%S')} "
                f"| {detail['reason']}",
                file=sys.stderr,
            )
            skipped_non_communicating += 1
            continue

        candidates.append((recharge, cmds))

    if not candidates:
        print(f"[SLA Correct] No corrections needed for {args.date}", file=sys.stderr)
        sys.exit(0)

    # Fetch HES records for all execution IDs across all candidate recharges
    all_exec_ids = [
        str(cmd["hes_execution_id"])
        for recharge, cmds in candidates
        for cmd in cmds
    ]
    try:
        with hes_conn() as h_conn_read:
            hes_records = fetch_hes_executions(all_exec_ids, h_conn_read)
    except Exception as exc:
        print(f"[SLA Correct] ERROR: Cannot connect to HES DB: {exc}", file=sys.stderr)
        sys.exit(2)

    corrections = []
    skipped_missing_hes = 0

    if dry_run:
        for recharge, cmds in candidates:
            result = apply_correction(recharge, cmds, None, None,
                                      args.target_seconds, dry_run=True)
            if result:
                selected_id = result['selected_execution_id']
                if selected_id not in hes_records:
                    print(f"[SLA Correct] WARN: execution_id {selected_id} not in HES — would skip",
                          file=sys.stderr)
                    skipped_missing_hes += 1
                    continue
                corrections.append(result)
    else:
        try:
            m_conn = psycopg2.connect(os.environ["DB_MDMS_URL"])
            h_conn = psycopg2.connect(os.environ["DB_HES_URL"])
        except Exception as exc:
            print(f"[SLA Correct] ERROR: DB connection failed: {exc}", file=sys.stderr)
            sys.exit(2)

        for recharge, cmds in candidates:
            result = apply_correction(recharge, cmds, m_conn, h_conn,
                                      args.target_seconds, dry_run=False)
            if result is None:
                continue
            selected_id = result['selected_execution_id']
            if selected_id not in hes_records:
                print(f"[SLA Correct] WARN: execution_id {selected_id} not found in HES — skipped",
                      file=sys.stderr)
                skipped_missing_hes += 1
                continue
            corrections.append(result)

        # Commit once after all corrections (not per-correction)
        m_conn.commit()
        h_conn.commit()
        m_conn.close()
        h_conn.close()

    if corrections:
        headers = [
            "transaction_id", "meter_number", "selected_command",
            "selected_execution_id", "new_end_time",
            "cmds_ts_updated", "mdms_success", "mdms_response",
            "hes_cmd", "hes_response",
        ]
        rows = [
            [
                c["transaction_id"], c["meter_number"], c["selected_command"],
                c["selected_execution_id"], c["new_end_time"],
                c["commands_timestamp_updated"], c["rows_mdms_success"],
                c["rows_mdms_response"], c["rows_hes_cmd"], c["rows_hes_response"],
            ]
            for c in corrections
        ]
        print(tabulate(rows, headers=headers, tablefmt="simple"))

    mode_label = "DRY RUN — no changes written" if dry_run else "APPLIED"
    print(f"\n[SLA Correct] Date          : {args.date}  Mode: {mode_label}", file=sys.stderr)
    print(f"[SLA Correct] Total recharges: {len(recharges)}", file=sys.stderr)
    print(f"[SLA Correct] Corrected      : {len(corrections)}", file=sys.stderr)
    print(f"[SLA Correct] Skipped (compliant)      : {skipped_compliant}", file=sys.stderr)
    print(f"[SLA Correct] Skipped (no eligible)    : {skipped_no_eligible}", file=sys.stderr)
    print(f"[SLA Correct] Skipped (non-communicating): {skipped_non_communicating}", file=sys.stderr)
    print(f"[SLA Correct] Skipped (missing HES)    : {skipped_missing_hes}", file=sys.stderr)
    if dry_run:
        print("[SLA Correct] Run with --no-dry-run to apply corrections.", file=sys.stderr)


if __name__ == "__main__":
    main()
