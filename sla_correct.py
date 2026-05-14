#!/usr/bin/env python3
"""SLA correction script — updates MDMS and HES timestamps for US SET CURRENT BALANCE AMOUNT."""
import argparse
import os
import sys

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
            "Update executionStartTime/executionEndTime/executionStatus for "
            "'US SET CURRENT BALANCE AMOUNT' in MDMS and HES for SLA-breached recharges."
        ),
        epilog=(
            "Environment variables required:\n"
            "  DB_PREPAID_URL  PostgreSQL URL for db_prepaid_engine\n"
            "  DB_MDMS_URL     PostgreSQL URL for db_cmd_exec (MDMS)\n"
            "  DB_HES_URL      PostgreSQL URL for HES routing service\n\n"
            "Examples:\n"
            "  python sla_correct.py --date 2026-05-11                       # dry-run (safe default)\n"
            "  python sla_correct.py --date 2026-05-11 --no-dry-run          # apply corrections\n"
            "  python sla_correct.py --date 2026-05-11 --target-seconds 900  # 15-min target\n"
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
    from src.corrector import apply_correction

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

    # Collect all US SET CURRENT BALANCE AMOUNT commands that need correction
    candidates = []
    for recharge in recharges:
        cmds = commands_by_account.get(recharge["account_id"], [])
        for cmd in cmds:
            if cmd.get("commandName") != "US SET CURRENT BALANCE AMOUNT":
                continue
            exec_id = str(cmd.get("hes_execution_id", ""))
            already_ok = (
                cmd.get("executionStatus") == "SUCCESS"
                and cmd.get("mdm_end") is not None
            )
            if already_ok:
                delta = (cmd["mdm_end"] - recharge["created_at"]).total_seconds()
                if delta <= args.target_seconds:
                    continue  # already within target — skip (idempotent)
            candidates.append((recharge, cmd, exec_id))

    all_exec_ids = [exec_id for _, _, exec_id in candidates]

    try:
        with hes_conn() as h_conn_read:
            hes_records = fetch_hes_executions(all_exec_ids, h_conn_read)
    except Exception as exc:
        print(f"[SLA Correct] ERROR: Cannot connect to HES DB: {exc}", file=sys.stderr)
        sys.exit(2)

    corrections = []
    skipped = 0

    if dry_run:
        for recharge, cmd, exec_id in candidates:
            if exec_id not in hes_records:
                print(f"[SLA Correct] WARN: execution_id {exec_id} not found in HES — skipping", file=sys.stderr)
                skipped += 1
                continue
            result = apply_correction(exec_id, recharge["created_at"], None, None,
                                      args.target_seconds, dry_run=True)
            result["meter_number"] = recharge["meter_number"]
            result["account_id"] = recharge["account_id"]
            corrections.append(result)
    else:
        try:
            m_conn = __import__("psycopg2").connect(os.environ["DB_MDMS_URL"])
            h_conn = __import__("psycopg2").connect(os.environ["DB_HES_URL"])
        except Exception as exc:
            print(f"[SLA Correct] ERROR: DB connection failed: {exc}", file=sys.stderr)
            sys.exit(2)

        for recharge, cmd, exec_id in candidates:
            if exec_id not in hes_records:
                print(f"[SLA Correct] WARN: execution_id {exec_id} not found in HES — skipping", file=sys.stderr)
                skipped += 1
                continue
            result = apply_correction(exec_id, recharge["created_at"], m_conn, h_conn,
                                      args.target_seconds, dry_run=False)
            result["meter_number"] = recharge["meter_number"]
            result["account_id"] = recharge["account_id"]
            corrections.append(result)

        m_conn.close()
        h_conn.close()

    if corrections:
        headers = ["execution_id", "meter_number", "account_id", "new_start_time", "new_end_time",
                   "rows_updated_mdms", "rows_updated_hes"]
        rows = [[c.get(h, "") for h in headers] for c in corrections]
        print(tabulate(rows, headers=headers, tablefmt="simple"))

    mode_label = "DRY RUN — no changes written" if dry_run else "APPLIED"
    print(f"\n[SLA Correct] Date: {args.date}  Mode: {mode_label}", file=sys.stderr)
    print(f"[SLA Correct] Candidates found : {len(candidates)}", file=sys.stderr)
    print(f"[SLA Correct] Corrected        : {len(corrections)}", file=sys.stderr)
    print(f"[SLA Correct] Skipped (missing): {skipped}", file=sys.stderr)
    if dry_run:
        print("[SLA Correct] Run with --no-dry-run to apply corrections.", file=sys.stderr)


if __name__ == "__main__":
    main()
