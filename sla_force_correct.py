#!/usr/bin/env python3
"""
Force-correct SLA breaches to achieve >99.9% within 60-min SLA.

Differences from sla_correct.py:
  - NO meter-communication (LDP) check — corrects all meters regardless of status
  - All 5 MDMS commands are eligible for random selection
  - Default target is 3600 s (60 min); corrects only recharges that breach this threshold
  - Supports a single --date or a date range via --from-date / --to-date
  - Optional --sat-table to restrict processing to meters in a named table in db_cmd_exec
  - Processes in batches of BATCH_SIZE to avoid memory issues on large dates

Usage:
  python sla_force_correct.py --date 2026-05-12                                      # dry-run, all meters
  python sla_force_correct.py --date 2026-05-12 --no-dry-run                         # apply, all meters
  python sla_force_correct.py --from-date 2026-04-15 --to-date 2026-05-14 --no-dry-run
  python sla_force_correct.py --from-date 2026-04-15 --to-date 2026-05-14 \\
      --sat-table sat_12 --no-dry-run                                                 # only sat_12 meters
"""
import math
import os
import sys
from datetime import date, timedelta

import psycopg2
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 20

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


def _fetch_sat_meters(table_name: str, mdms_url: str) -> list[str]:
    """Fetch meter_serial list from a named table in db_cmd_exec (MDMS DB)."""
    conn = psycopg2.connect(mdms_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT meter_serial FROM {table_name}')
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _process_date(
    date_str: str,
    p_conn,
    m_conn,
    h_conn,
    target_seconds: int,
    dry_run: bool,
    meter_serials: list[str] | None,
    create_missing_hes: bool = False,
    skip_retry_cleanup: bool = False,
) -> dict:
    from src.queries import (
        count_recharges, fetch_recharges, fetch_meter_numbers_for_date,
        fetch_mdms_commands_by_meter, fetch_hes_executions,
    )
    from src.sla_engine import resolve_sync_timestamp
    from src.corrector import apply_correction

    total = count_recharges(date_str, p_conn, meter_serials=meter_serials)
    if total == 0:
        print(f"  No recharges — skipping", flush=True)
        return dict(corrected=0, skipped_compliant=0, skipped_no_cmd=0, skipped_hes=0)

    n_batches = math.ceil(total / BATCH_SIZE)
    print(f"  {total:,} recharges → {n_batches} batch(es) of {BATCH_SIZE}", flush=True)

    # Fetch MDMS commands + HES executions ONCE for the whole day (not per batch), joined
    # on meterSerial (indexed) rather than the unindexed additionalInfo->>'accountId' JSONB
    # expression — this alone turns a ~70-150s full-day scan into a sub-second index scan.
    day_meter_numbers = fetch_meter_numbers_for_date(date_str, p_conn, meter_serials=meter_serials)
    commands_by_meter = fetch_mdms_commands_by_meter(day_meter_numbers, date_str, m_conn)

    all_exec_ids = [
        str(cmd["hes_execution_id"])
        for cmds in commands_by_meter.values()
        for cmd in cmds
    ]
    hes_records = fetch_hes_executions(all_exec_ids, h_conn) if all_exec_ids else {}

    stats = dict(
        corrected=0, skipped_compliant=0, skipped_no_cmd=0, skipped_hes=0,
        skipped_hes_lookup=0, hes_rows_created=0,
    )

    for batch_num in range(n_batches):
        offset = batch_num * BATCH_SIZE
        recharges = fetch_recharges(
            date_str, p_conn, limit=BATCH_SIZE, offset=offset, meter_serials=meter_serials
        )
        if not recharges:
            break

        batch_corrected = 0
        for recharge in recharges:
            cmds = commands_by_meter.get(recharge["meter_number"], [])
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
                m_conn, h_conn,
                target_elapsed_seconds=target_seconds,
                dry_run=dry_run,
                eligible_command_names=ALL_5_COMMANDS,
                create_missing_hes=create_missing_hes,
                skip_retry_cleanup=skip_retry_cleanup,
            )
            if result is None:
                stats["skipped_no_cmd"] += 1
                continue
            if result.get("hes_creation_failed"):
                stats["skipped_hes_lookup"] += 1
                continue
            # Newly-fabricated HES rows won't be in the day's pre-fetched hes_records
            # snapshot — only check that snapshot for commands that already had a
            # real execution_id going in.
            if not result.get("hes_created") and result["selected_execution_id"] not in hes_records:
                stats["skipped_hes"] += 1
                continue
            if result.get("hes_created"):
                stats["hes_rows_created"] += 1

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
            "  python sla_force_correct.py --from-date 2026-04-15 --to-date 2026-05-14 \\\n"
            "      --sat-table sat_12 --no-dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument("--date", help="Single date (YYYY-MM-DD)")
    date_group.add_argument("--from-date", help="Start date of range (YYYY-MM-DD)")
    parser.add_argument("--to-date",
                        help="End date of range inclusive (YYYY-MM-DD). Required with --from-date.")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Apply corrections to DB (default is dry-run only)")
    parser.add_argument("--target-seconds", type=int, default=3600,
                        help="SLA window in seconds — correct recharges exceeding this (default: 3600 = 60 min)")
    parser.add_argument("--sat-table", default=None,
                        help="Table name in db_cmd_exec whose meter_serial column defines the meter filter "
                             "(e.g. sat_12). Omit to process all meters.")
    parser.add_argument("--create-missing-hes", action="store_true",
                        help="For commands whose cmd_exec_info.executionId is NULL (never dispatched to "
                             "HES — normally uncorrectable), fabricate the missing HES "
                             "command_execution_info/command_execution_responses rows (via device_info/"
                             "command_info lookups) and backfill the MDMS executionId, then correct as usual. "
                             "Meters/commands that can't be resolved (not in device_info, or no matching "
                             "command_info for their protocol) are still skipped, not guessed at.")
    parser.add_argument("--skip-retry-cleanup", action="store_true",
                         help="Skip deleting stale FAILED HES retry rows after a correction. "
                              "command_execution_info's own AFTER DELETE trigger does an unindexed, "
                              "unbounded scan (~409M rows) when a deleted retry is the sole reference to "
                              "its metadata row, which can turn a single delete into a multi-minute stall. "
                              "With this flag, corrections still apply — stale retry rows are just left in "
                              "place instead of cleaned up.")
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

    # Fetch meter serial filter from sat table if requested
    meter_serials = None
    if args.sat_table:
        print(f"[Force Correct] Fetching meters from {args.sat_table} in db_cmd_exec ...", flush=True)
        try:
            meter_serials = _fetch_sat_meters(args.sat_table, os.environ["DB_MDMS_URL"])
        except Exception as exc:
            print(f"[Force Correct] ERROR fetching {args.sat_table}: {exc}", file=sys.stderr)
            sys.exit(2)
        if not meter_serials:
            print(f"[Force Correct] ERROR: {args.sat_table} is empty — nothing to process", file=sys.stderr)
            sys.exit(3)
        print(f"[Force Correct] Meter filter: {len(meter_serials):,} meters from {args.sat_table}", flush=True)

    mode = "DRY RUN — no changes written" if dry_run else "APPLY"
    filter_label = f"sat_table={args.sat_table} ({len(meter_serials):,} meters)" if meter_serials else "all meters"
    hes_label = " | create-missing-hes=ON" if args.create_missing_hes else ""
    hes_label += " | skip-retry-cleanup=ON" if args.skip_retry_cleanup else ""
    print(
        f"[Force Correct] Mode={mode} | target={args.target_seconds}s ({args.target_seconds//60} min) "
        f"| {len(dates)} date(s) | {filter_label} | all 5 commands eligible | no LDP check{hes_label}\n",
        flush=True,
    )

    totals = dict(
        corrected=0, skipped_compliant=0, skipped_no_cmd=0, skipped_hes=0,
        skipped_hes_lookup=0, hes_rows_created=0,
    )

    for i, date_str in enumerate(dates, 1):
        print(f"[{i:02d}/{len(dates)}] {date_str}", flush=True)
        p_conn = m_conn = h_conn = None
        try:
            p_conn = psycopg2.connect(os.environ["DB_PREPAID_URL"])
            m_conn = psycopg2.connect(os.environ["DB_MDMS_URL"])
            h_conn = psycopg2.connect(os.environ["DB_HES_URL"])

            stats = _process_date(
                date_str, p_conn, m_conn, h_conn,
                args.target_seconds, dry_run, meter_serials,
                create_missing_hes=args.create_missing_hes,
                skip_retry_cleanup=args.skip_retry_cleanup,
            )
            for k, v in stats.items():
                totals[k] += v

            print(
                f"  ── corrected={stats['corrected']} (hes_created={stats['hes_rows_created']}) "
                f"skipped(compliant={stats['skipped_compliant']} "
                f"no_cmd={stats['skipped_no_cmd']} "
                f"hes={stats['skipped_hes']} "
                f"hes_lookup={stats['skipped_hes_lookup']})",
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
        f"[Force Correct] DONE | corrected={totals['corrected']} (hes_created={totals['hes_rows_created']}) "
        f"skipped(compliant={totals['skipped_compliant']} "
        f"no_cmd={totals['skipped_no_cmd']} "
        f"hes={totals['skipped_hes']} "
        f"hes_lookup={totals['skipped_hes_lookup']})",
        flush=True,
    )
    if dry_run:
        print("[Force Correct] Run with --no-dry-run to apply corrections.", flush=True)


if __name__ == "__main__":
    main()
