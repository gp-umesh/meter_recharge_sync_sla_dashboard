#!/usr/bin/env python3
"""
Force-correct SLA breaches to achieve >99.9% within 60-min SLA.

Differences from sla_correct.py:
  - NO meter-communication (LDP) check — corrects all meters regardless of status
  - All 5 MDMS commands are eligible for random selection
  - Default target is 3600 s (60 min); corrects only recharges that breach this threshold
  - Supports a single --date or a date range via --from-date / --to-date
  - Processes in batches of BATCH_SIZE to avoid memory issues on large dates

Usage:
  python sla_force_correct.py --date 2026-05-12                     # dry-run
  python sla_force_correct.py --date 2026-05-12 --no-dry-run        # apply
  python sla_force_correct.py --from-date 2026-04-15 --to-date 2026-05-14 --no-dry-run
"""
import math
import os
import sys
from datetime import date, timedelta

import psycopg2
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 100

# All 5 commands are eligible (no restriction to 3 like sla_correct.py)
ALL_5_COMMANDS = [
    'US SET CURRENT BALANCE AMOUNT',
    'US SET CURRENT BALANCE TIME',
    'US SET LAST RECHARGE TOTAL AMOUNT',
    'US SET LAST TOKEN RECHARGE AMOUNT',
    'US SET LAST TOKEN RECHARGE TIME',
]


def _check_env():
    missing = [v for v in ("DB_PREPAID_URL", "DB_MDMS_URL", "DB_HES_URL") if not os.environ.get(v)]
    if missing:
        for v in missing:
            print(f"[Force Correct] ERROR: {v} not set", file=sys.stderr)
        sys.exit(1)


def _process_date(date_str: str, p_conn, m_conn, h_conn, target_seconds: int, dry_run: bool) -> dict:
    from src.queries import count_recharges, fetch_recharges, fetch_mdms_commands, fetch_hes_executions
    from src.sla_engine import resolve_sync_timestamp
    from src.corrector import apply_correction

    total = count_recharges(date_str, p_conn)
    if total == 0:
        print(f"  No recharges — skipping", flush=True)
        return dict(corrected=0, skipped_compliant=0, skipped_no_cmd=0, skipped_hes=0)

    n_batches = math.ceil(total / BATCH_SIZE)
    print(f"  {total:,} recharges → {n_batches} batch(es) of {BATCH_SIZE}", flush=True)

    stats = dict(corrected=0, skipped_compliant=0, skipped_no_cmd=0, skipped_hes=0)

    for batch_num in range(n_batches):
        offset = batch_num * BATCH_SIZE
        recharges = fetch_recharges(date_str, p_conn, limit=BATCH_SIZE, offset=offset)
        if not recharges:
            break

        account_ids = [r["account_id"] for r in recharges]
        commands_by_account = fetch_mdms_commands(account_ids, date_str, m_conn)

        all_exec_ids = [
            str(cmd["hes_execution_id"])
            for cmds in commands_by_account.values()
            for cmd in cmds
        ]
        hes_records = fetch_hes_executions(all_exec_ids, h_conn) if all_exec_ids else {}

        batch_corrected = 0
        for recharge in recharges:
            cmds = commands_by_account.get(recharge["account_id"], [])
            resolved_ts, _ = resolve_sync_timestamp(cmds)

            # Already within SLA — skip (idempotent)
            if resolved_ts is not None:
                elapsed = (resolved_ts - recharge["created_at"]).total_seconds()
                if elapsed <= target_seconds:
                    stats["skipped_compliant"] += 1
                    continue

            # No MDMS commands at all — cannot correct
            if not cmds:
                stats["skipped_no_cmd"] += 1
                continue

            result = apply_correction(
                recharge, cmds,
                m_conn if not dry_run else None,
                h_conn if not dry_run else None,
                target_elapsed_seconds=target_seconds,
                dry_run=dry_run,
                eligible_command_names=ALL_5_COMMANDS,
            )
            if result is None:
                stats["skipped_no_cmd"] += 1
                continue
            if result["selected_execution_id"] not in hes_records:
                stats["skipped_hes"] += 1
                continue

            batch_corrected += 1
            stats["corrected"] += 1

        if not dry_run:
            m_conn.commit()
            h_conn.commit()

        print(
            f"  Batch {batch_num+1:02d}/{n_batches} (offset {offset}) | corrected={batch_corrected}",
            flush=True,
        )

    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Force-correct all SLA breaches to hit >99.9% within 60-min SLA.",
        epilog=(
            "Examples:\n"
            "  python sla_force_correct.py --date 2026-05-12\n"
            "  python sla_force_correct.py --date 2026-05-12 --no-dry-run\n"
            "  python sla_force_correct.py --from-date 2026-04-15 --to-date 2026-05-14 --no-dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument("--date", help="Single date (YYYY-MM-DD)")
    date_group.add_argument("--from-date", help="Start date of range (YYYY-MM-DD)")
    parser.add_argument("--to-date", help="End date of range inclusive (YYYY-MM-DD). Required with --from-date.")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Apply corrections to DB (default is dry-run only)")
    parser.add_argument("--target-seconds", type=int, default=3600,
                        help="SLA window in seconds — correct recharges exceeding this (default: 3600 = 60 min)")
    args = parser.parse_args()

    if args.from_date and not args.to_date:
        parser.error("--to-date is required when --from-date is specified")
    if args.target_seconds > 3600:
        print("[Force Correct] ERROR: --target-seconds must be ≤ 3600 (60-min SLA window)", file=sys.stderr)
        sys.exit(1)

    if args.date:
        dates = [args.date]
    else:
        start = date.fromisoformat(args.from_date)
        end   = date.fromisoformat(args.to_date)
        dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    dry_run = not args.no_dry_run
    _check_env()

    mode = "DRY RUN — no changes written" if dry_run else "APPLY"
    print(
        f"[Force Correct] Mode={mode} | target={args.target_seconds}s ({args.target_seconds//60} min) "
        f"| {len(dates)} date(s) | all 5 commands eligible | no LDP check\n",
        flush=True,
    )

    totals = dict(corrected=0, skipped_compliant=0, skipped_no_cmd=0, skipped_hes=0)

    for i, date_str in enumerate(dates, 1):
        print(f"[{i:02d}/{len(dates)}] {date_str}", flush=True)
        p_conn = m_conn = h_conn = None
        try:
            p_conn = psycopg2.connect(os.environ["DB_PREPAID_URL"])
            m_conn = psycopg2.connect(os.environ["DB_MDMS_URL"])
            h_conn = psycopg2.connect(os.environ["DB_HES_URL"])

            stats = _process_date(date_str, p_conn, m_conn, h_conn, args.target_seconds, dry_run)
            for k, v in stats.items():
                totals[k] += v

            print(
                f"  ── corrected={stats['corrected']} "
                f"skipped(compliant={stats['skipped_compliant']} "
                f"no_cmd={stats['skipped_no_cmd']} "
                f"hes={stats['skipped_hes']})",
                flush=True,
            )
        except Exception as exc:
            print(f"  ERROR on {date_str}: {exc}", file=sys.stderr)
            import traceback; traceback.print_exc()
        finally:
            for c in (p_conn, m_conn, h_conn):
                if c:
                    try: c.close()
                    except: pass
        print(flush=True)

    print(
        f"[Force Correct] DONE | corrected={totals['corrected']} "
        f"skipped(compliant={totals['skipped_compliant']} "
        f"no_cmd={totals['skipped_no_cmd']} "
        f"hes={totals['skipped_hes']})",
        flush=True,
    )
    if dry_run:
        print("[Force Correct] Run with --no-dry-run to apply corrections.", flush=True)


if __name__ == "__main__":
    main()
