#!/usr/bin/env python3
"""SLA check script — analyses recharge sync compliance for a given date."""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _check_env():
    missing = [v for v in ("DB_PREPAID_URL", "DB_MDMS_URL", "DB_HES_URL") if not os.environ.get(v)]
    if missing:
        for v in missing:
            print(f"[SLA Check] ERROR: {v} environment variable is not set", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Analyse recharge sync SLA compliance for a given date.",
        epilog=(
            "Environment variables required:\n"
            "  DB_PREPAID_URL  PostgreSQL URL for db_prepaid_engine\n"
            "  DB_MDMS_URL     PostgreSQL URL for db_cmd_exec (MDMS)\n"
            "  DB_HES_URL      PostgreSQL URL for HES routing service\n\n"
            "Examples:\n"
            "  python sla_check.py --date 2026-05-11\n"
            "  python sla_check.py --date 2026-05-11 --output table\n"
            "  python sla_check.py --date 2026-05-11 --verbose > breaches.csv\n"
            "  python sla_check.py --date 2026-05-11 --write-db"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", required=True, help="Target date (YYYY-MM-DD)")
    parser.add_argument("--output", choices=["csv", "json", "table"], default="csv",
                        help="Breach list output format (default: csv)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-command detail for each breached meter")
    parser.add_argument("--write-db", action="store_true",
                        help="Write SLA results to sla_results table in db_prepaid_engine")
    args = parser.parse_args()

    _check_env()

    from src.db import prepaid_conn, mdms_conn, hes_conn
    from src.queries import fetch_recharges, fetch_mdms_commands, fetch_hes_executions
    from src.sla_engine import compute_sla_record
    from src.reporter import format_breach_list, format_verbose_breach_list, format_summary

    try:
        with prepaid_conn() as p_conn:
            recharges = fetch_recharges(args.date, p_conn)
    except Exception as exc:
        print(f"[SLA Check] ERROR: Cannot connect to prepaid DB: {exc}", file=sys.stderr)
        sys.exit(2)

    if not recharges:
        print(f"[SLA Check] ERROR: No recharges found for date {args.date}", file=sys.stderr)
        sys.exit(3)

    account_ids = [r["account_id"] for r in recharges]

    try:
        with mdms_conn() as m_conn:
            commands_by_account = fetch_mdms_commands(account_ids, args.date, m_conn)
    except Exception as exc:
        print(f"[SLA Check] ERROR: Cannot connect to MDMS DB: {exc}", file=sys.stderr)
        sys.exit(2)

    all_exec_ids = [
        str(cmd["hes_execution_id"])
        for cmds in commands_by_account.values()
        for cmd in cmds
    ]

    try:
        with hes_conn() as h_conn:
            hes_records = fetch_hes_executions(all_exec_ids, h_conn)
    except Exception as exc:
        print(f"[SLA Check] ERROR: Cannot connect to HES DB: {exc}", file=sys.stderr)
        sys.exit(2)

    sla_records = []
    for recharge in recharges:
        cmds = commands_by_account.get(recharge["account_id"], [])
        record = compute_sla_record(recharge, cmds, hes_records)
        sla_records.append(record)

    if args.verbose:
        output = format_verbose_breach_list(sla_records, args.output)
    else:
        output = format_breach_list(sla_records, args.output)

    print(output)
    print(format_summary(sla_records, args.date), file=sys.stderr)

    if args.write_db:
        try:
            with prepaid_conn() as p_conn:
                from src.writer import write_sla_results
                count = write_sla_results(sla_records, p_conn)
                print(f"[SLA Check] Wrote {count} records to sla_results", file=sys.stderr)
        except Exception as exc:
            print(f"[SLA Check] ERROR writing to sla_results: {exc}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
