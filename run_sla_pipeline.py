#!/usr/bin/env python3
"""
Batched SLA correction + sla_results population.
For each date: fetches recharges in batches of BATCH_SIZE (LIMIT/OFFSET),
applies corrections and writes SLA results batch-by-batch.
All DB connections are opened once per date and reused across batches.
"""
import math
import os
import sys
from datetime import date, timedelta

import psycopg2
from dotenv import load_dotenv

load_dotenv()

START      = date(2026, 5, 1)
END        = date(2026, 5, 14)
BATCH_SIZE = 100


def _require_env():
    missing = [v for v in ("DB_PREPAID_URL", "DB_MDMS_URL", "DB_HES_URL") if not os.environ.get(v)]
    if missing:
        for v in missing: print(f"ERROR: {v} not set", file=sys.stderr)
        sys.exit(1)


def _open_connections():
    return (
        psycopg2.connect(os.environ["DB_PREPAID_URL"]),
        psycopg2.connect(os.environ["DB_MDMS_URL"]),
        psycopg2.connect(os.environ["DB_HES_URL"]),
    )


def _close(*conns):
    for c in conns:
        try: c.close()
        except: pass


def process_date(date_str: str, p_conn, m_conn, h_conn, ldp_map: dict):
    from src.queries import count_recharges, fetch_recharges, fetch_mdms_commands, fetch_hes_executions
    from src.sla_engine import resolve_sync_timestamp, compute_sla_record
    from src.corrector import apply_correction, select_eligible_command
    from src.writer import write_sla_results

    total = count_recharges(date_str, p_conn)
    if total == 0:
        print(f"  No recharges found — skipping", flush=True)
        return

    n_batches = math.ceil(total / BATCH_SIZE)
    print(f"  {total:,} recharges → {n_batches} batch(es) of {BATCH_SIZE}", flush=True)

    date_stats = dict(corrected=0, skipped_compliant=0, skipped_no_eligible=0,
                      skipped_non_comm=0, skipped_hes=0, sla_written=0)

    for batch_num in range(n_batches):
        offset = batch_num * BATCH_SIZE
        batch_label = f"Batch {batch_num+1}/{n_batches} (offset {offset})"

        # ── 1. Fetch recharges for this batch ─────────────────────────────
        recharges = fetch_recharges(date_str, p_conn, limit=BATCH_SIZE, offset=offset)
        if not recharges:
            break

        account_ids = [r["account_id"] for r in recharges]

        # ── 2. Fetch MDMS commands for this batch's accounts ───────────────
        commands_by_account = fetch_mdms_commands(account_ids, date_str, m_conn)

        # ── 3. Identify correction candidates ─────────────────────────────
        candidates = []
        for recharge in recharges:
            cmds = commands_by_account.get(recharge["account_id"], [])
            resolved_ts, _ = resolve_sync_timestamp(cmds)

            if resolved_ts is not None:
                elapsed = (resolved_ts - recharge["created_at"]).total_seconds()
                if elapsed <= 1200:
                    date_stats["skipped_compliant"] += 1
                    continue

            if select_eligible_command(cmds, seed=recharge["transaction_id"]) is None:
                date_stats["skipped_no_eligible"] += 1
                continue

            from src.validator import is_meter_communicating
            ok, detail = is_meter_communicating(recharge["meter_number"], recharge["created_at"], ldp_map)
            if not ok:
                import sys
                print(
                    f"  [non-comm] meter={recharge['meter_number']} "
                    f"com_type={detail['com_type']} "
                    f"effective_ldp={detail['effective_ldp']} "
                    f"gap={detail['gap_days']}d | {detail['reason']}",
                    flush=True,
                    file=sys.stderr,
                )
                date_stats["skipped_non_comm"] += 1
                continue

            candidates.append((recharge, cmds))

        # ── 4. Fetch HES records for all candidate executions ──────────────
        all_exec_ids = [
            str(cmd["hes_execution_id"])
            for recharge, cmds in candidates
            for cmd in cmds
        ]
        hes_records = fetch_hes_executions(all_exec_ids, h_conn) if all_exec_ids else {}

        # ── 5. Apply corrections (no per-record commits — batch commit below) ─
        batch_corrections = 0
        for recharge, cmds in candidates:
            result = apply_correction(recharge, cmds, m_conn, h_conn,
                                      target_elapsed_seconds=1200, dry_run=False)
            if result is None:
                continue
            if result["selected_execution_id"] not in hes_records:
                date_stats["skipped_hes"] += 1
                continue
            batch_corrections += 1
            date_stats["corrected"] += 1

        # ONE commit per DB per batch (instead of per correction)
        m_conn.commit()
        h_conn.commit()

        # ── 6. Re-fetch recharges + commands post-correction for SLA write ─
        recharges_fresh = fetch_recharges(date_str, p_conn, limit=BATCH_SIZE, offset=offset)
        fresh_accounts  = [r["account_id"] for r in recharges_fresh]
        fresh_cmds      = fetch_mdms_commands(fresh_accounts, date_str, m_conn)

        fresh_exec_ids = [
            str(cmd["hes_execution_id"])
            for cmds in fresh_cmds.values()
            for cmd in cmds
        ]
        fresh_hes = fetch_hes_executions(fresh_exec_ids, h_conn) if fresh_exec_ids else {}

        sla_records = [
            compute_sla_record(r, fresh_cmds.get(r["account_id"], []), fresh_hes)
            for r in recharges_fresh
        ]
        written = write_sla_results(sla_records, p_conn)
        date_stats["sla_written"] += written

        print(
            f"  {batch_label} | corrected={batch_corrections} "
            f"sla_written={written}",
            flush=True,
        )

    print(
        f"  ── Summary: corrected={date_stats['corrected']} "
        f"sla_written={date_stats['sla_written']} "
        f"skipped(compliant={date_stats['skipped_compliant']} "
        f"no_cmd={date_stats['skipped_no_eligible']} "
        f"non_comm={date_stats['skipped_non_comm']} "
        f"hes={date_stats['skipped_hes']})",
        flush=True,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

_require_env()

from src.validator import validation_conn, fetch_meter_ldp_map

total_dates = (END - START).days + 1
print(f"SLA Pipeline | {START} → {END} | batch={BATCH_SIZE} | {total_dates} dates\n", flush=True)

current = START
processed = 0

while current <= END:
    date_str = current.strftime("%Y-%m-%d")
    print(f"[{processed+1:02d}/{total_dates}] {date_str}", flush=True)

    p_conn, m_conn, h_conn = _open_connections()

    try:
        from src.queries import count_recharges
        total = count_recharges(date_str, p_conn)
        if total == 0:
            print(f"  No recharges — skipping", flush=True)
        else:
            # Fetch distinct meter serials for this date in one lightweight query
            from_date = f"{date_str} 00:00:00+05:30"
            to_date   = f"{date_str} 23:59:59.999999+05:30"
            with p_conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT meter_number FROM recharges_data "
                    "WHERE created_at >= %s AND created_at < %s",
                    (from_date, to_date),
                )
                meter_serials = [r[0] for r in cur.fetchall()]

            val_conn = validation_conn(os.environ["DB_MDMS_URL"])
            ldp_map  = fetch_meter_ldp_map(meter_serials, val_conn)
            val_conn.close()
            print(f"  Meters: {len(meter_serials):,} | In LDP map: {len(ldp_map):,}", flush=True)

            process_date(date_str, p_conn, m_conn, h_conn, ldp_map)

    except Exception as exc:
        print(f"  ERROR on {date_str}: {exc}", flush=True)
        import traceback; traceback.print_exc()
    finally:
        _close(p_conn, m_conn, h_conn)

    print(flush=True)
    current += timedelta(days=1)
    processed += 1

print(f"Done. {processed} dates processed.", flush=True)
