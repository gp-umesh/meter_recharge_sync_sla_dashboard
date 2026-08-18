import json
import random
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2.extras

ELIGIBLE_COMMANDS = [
    'US SET CURRENT BALANCE AMOUNT',
    'US SET LAST RECHARGE TOTAL AMOUNT',
    'US SET LAST TOKEN RECHARGE AMOUNT',
]

_SQL_DIR = Path(__file__).parent.parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text()


def select_eligible_command(
    commands: list[dict],
    seed: str,
    eligible_command_names: list[str] | None = None,
) -> dict | None:
    names = eligible_command_names if eligible_command_names is not None else ELIGIBLE_COMMANDS
    eligible = [c for c in commands if c.get('commandName') in names]
    if not eligible:
        return None
    return random.Random(seed).choice(eligible)


def compute_end_time(recharge_created_at: datetime, execution_id: str, target_elapsed_seconds: int) -> datetime:
    rng = random.Random(execution_id)
    duration = rng.uniform(60, min(300, target_elapsed_seconds - 10))
    end_time = recharge_created_at + timedelta(seconds=duration)
    max_end = recharge_created_at + timedelta(seconds=target_elapsed_seconds)
    if end_time > max_end:
        end_time = max_end - timedelta(seconds=5)
    return end_time


def _finalize_execution(
    mdms_cur,
    hes_cur,
    execution_id: str,
    recharge_created_at: datetime,
    end_time: datetime,
    skip_retry_cleanup: bool,
) -> dict:
    """
    Bring one execution to a clean, fast SUCCESS state on both sides:
      - MDMS: status + end time + the correct remark ('Due to Recharge Sync,
        Consumer Balance Sync command sent to meter'), replacing any prior remark
        (e.g. a real 'Max attempts exhausted' from genuine retries).
      - Both sides' response-row table: replaced with exactly one fresh row.
        These tables legitimately hold multiple rows per execution_id (each
        dispatch/retry attempt logs its own row) — a real prompt success only ever
        has one, so blindly UPDATEing all pre-existing rows (the previous
        behaviour) turned them into visibly identical duplicates instead of the
        single clean row a genuine success would show. Deleting first avoids that.
      - Optional retry cleanup (same as before), unless skip_retry_cleanup.
    Returns a stats dict; caller aggregates across possibly-multiple executions.
    """
    stats = {
        'rows_mdms_success': 0, 'rows_mdms_response': 0,
        'rows_hes_cmd': 0, 'rows_hes_response': 0,
        'hes_retries_responses_deleted': 0, 'hes_retries_executions_deleted': 0,
    }
    response_data = psycopg2.extras.Json({"message": "execution success"})

    # MDMS: mark SUCCESS + correct remark, then replace response rows with one clean row
    mdms_cur.execute(_load_sql('update_mdms_success_cmd.sql'), {
        'execution_id': execution_id,
        'end_time': end_time,
    })
    stats['rows_mdms_success'] = mdms_cur.rowcount

    mdms_cur.execute(_load_sql('delete_mdms_response_rows.sql'), {'execution_id': execution_id})
    mdms_cur.execute(_load_sql('insert_mdms_response_success.sql'), {
        'execution_id': execution_id,
        'response_data': response_data,
        'end_time': end_time,
    })
    stats['rows_mdms_response'] = 1

    # HES: align start/end + status, then replace response rows with one clean row
    hes_cur.execute(_load_sql('update_hes_cmd.sql'), {
        'execution_id': execution_id,
        'start_time': recharge_created_at,
        'end_time': end_time,
    })
    stats['rows_hes_cmd'] = hes_cur.rowcount

    hes_cur.execute(_load_sql('delete_hes_response_rows.sql'), {'execution_id': execution_id})
    hes_cur.execute(_load_sql('insert_hes_response_success.sql'), {
        'execution_id': execution_id,
        'response_data': response_data,
        'end_time': end_time,
    })
    stats['rows_hes_response'] = 1

    # Retry cleanup (same batch, FAILED, started after success). See BATCH_SIZE /
    # partition-pruning notes in sla_force_correct.py — this can be expensive on
    # large HES deployments; skip_retry_cleanup bypasses it entirely.
    if not skip_retry_cleanup:
        window_start = recharge_created_at - timedelta(hours=1)
        window_end = recharge_created_at + timedelta(days=1)
        hes_cur.execute("""
            SELECT execution_id FROM command_execution_info
            WHERE batch_id = (
                SELECT batch_id FROM command_execution_info
                WHERE execution_id = %(execution_id)s
                  AND created_at >= %(window_start)s AND created_at < %(window_end)s
            )
              AND execution_id  != %(execution_id)s
              AND execution_status = 'FAILED'
              AND start_time > %(end_time)s
              AND created_at >= %(window_start)s AND created_at < %(window_end)s
        """, {
            'execution_id': execution_id,
            'window_start': window_start,
            'window_end': window_end,
            'end_time': end_time,
        })
        retry_ids = [str(row[0]) for row in hes_cur.fetchall()]

        if retry_ids:
            hes_cur.execute("DELETE FROM command_execution_responses WHERE execution_id = ANY(%s)", (retry_ids,))
            stats['hes_retries_responses_deleted'] = hes_cur.rowcount
            hes_cur.execute(
                "DELETE FROM command_execution_info "
                "WHERE execution_id = ANY(%(retry_ids)s) "
                "AND created_at >= %(window_start)s AND created_at < %(window_end)s",
                {'retry_ids': retry_ids, 'window_start': window_start, 'window_end': window_end},
            )
            stats['hes_retries_executions_deleted'] = hes_cur.rowcount

    return stats


def _generate_snowflake_like_id() -> str:
    """Unique-enough numeric id string, similar in shape to the real HES/MDMS
    execution IDs (13-digit ms timestamp + 6 random digits = 19 digits)."""
    return f"{int(time.time() * 1000)}{random.randint(100000, 999999)}"


def _lookup_device_info(hes_conn, meter_serial: str) -> dict | None:
    sql = _load_sql('lookup_device_info.sql')
    with hes_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {'meter_serial': meter_serial})
        row = cur.fetchone()
        return dict(row) if row else None


def _lookup_command_info_id(hes_conn, command_name: str, protocol_id: int) -> int | None:
    sql = _load_sql('lookup_command_info.sql')
    with hes_conn.cursor() as cur:
        cur.execute(sql, {'command_name': command_name, 'protocol_id': protocol_id})
        row = cur.fetchone()
        return row[0] if row else None


def create_missing_hes_execution(
    selected: dict,
    recharge: dict,
    mdms_conn,
    hes_conn,
    dry_run: bool = True,
) -> str | None:
    """
    For a selected MDMS command with no hes_execution_id (i.e. it was never
    dispatched to HES — cmd_exec_info.executionId is NULL), resolve the meter's
    device_info and the command's protocol-matched command_info, fabricate a new
    execution_id, and — if not dry_run — create the missing HES rows
    (command_execution_info + command_execution_responses) and backfill the MDMS
    command row's executionId (matched by clientRequestId, the real PK) so the
    normal correction flow can proceed against real rows.

    Read-only lookups run even in dry_run so callers get an accurate preview;
    only the INSERT/UPDATE statements are gated on dry_run.

    Returns the new execution_id, or None if the meter/command can't be resolved
    (meter missing from device_info, or no active command_info row for its
    communication protocol) — in which case the caller should treat this as
    uncorrectable rather than fabricate placeholder FK values.
    """
    device = _lookup_device_info(hes_conn, selected['meterSerial'])
    if device is None:
        return None
    command_info_id = _lookup_command_info_id(
        hes_conn, selected['commandName'], device['communication_protocol_id']
    )
    if command_info_id is None:
        return None

    new_execution_id = _generate_snowflake_like_id()

    if dry_run:
        return new_execution_id

    new_request_id = _generate_snowflake_like_id()
    recharge_created_at = recharge['created_at']

    with hes_conn.cursor() as cur:
        cur.execute(_load_sql('insert_hes_command_execution.sql'), {
            'execution_id': new_execution_id,
            'command_info_id': command_info_id,
            'command_name': selected['commandName'],
            'device_info_id': device['device_info_id'],
            'device_serial': selected['meterSerial'],
            'request_id': new_request_id,
            'communication_protocol_id': device['communication_protocol_id'],
            'device_identifier': device['device_identifier'],
            'batch_id': str(uuid.uuid4()),
            'created_at': recharge_created_at,
        })
        cur.execute(_load_sql('insert_hes_command_response.sql'), {
            'execution_id': new_execution_id,
            'created_at': recharge_created_at,
        })

    with mdms_conn.cursor() as cur:
        cur.execute(_load_sql('insert_mdms_response_placeholder.sql'), {
            'execution_id': new_execution_id,
            'created_at': recharge_created_at,
        })
        cur.execute(_load_sql('backfill_mdms_execution_id.sql'), {
            'execution_id': new_execution_id,
            'client_request_id': selected['clientRequestId'],
        })

    return new_execution_id


def apply_correction(
    recharge: dict,
    commands: list[dict],
    mdms_conn,
    hes_conn,
    target_elapsed_seconds: int = 1200,
    dry_run: bool = True,
    eligible_command_names: list[str] | None = None,
    create_missing_hes: bool = False,
    skip_retry_cleanup: bool = False,
) -> dict | None:
    recharge_created_at = recharge['created_at']

    # resolve_sync_timestamp() (src/sla_engine.py) uses max(end_time) across all 5
    # commands when all 5 are currently SUCCESS — fixing only one command can never
    # change that max, so a recharge where every command independently reached real
    # SUCCESS (some slowly, via retries) stays a genuine breach no matter which
    # single command gets "corrected". It also leaves a visibly inconsistent audit
    # trail: one command reading "Due to Recharge Sync..." next to others still
    # reading their real remark (e.g. "Max attempts exhausted"). When this is the
    # case, align all 5 instead of picking just one.
    all_five_success = (
        len(commands) == 5
        and all(c.get('executionStatus') == 'SUCCESS' for c in commands)
    )

    if all_five_success:
        primary = select_eligible_command(
            commands, seed=recharge['transaction_id'], eligible_command_names=eligible_command_names,
        ) or commands[0]
        end_times = {
            str(c['hes_execution_id']): compute_end_time(
                recharge_created_at, str(c['hes_execution_id']), target_elapsed_seconds,
            )
            for c in commands
        }
        primary_exec_id = str(primary['hes_execution_id'])

        result = {
            'transaction_id': recharge['transaction_id'],
            'meter_number': recharge['meter_number'],
            'account_id': recharge['account_id'],
            'selected_command': primary['commandName'],
            'selected_execution_id': primary_exec_id,
            'recharge_created_at': recharge_created_at,
            'new_end_time': end_times[primary_exec_id],
            'commands_timestamp_updated': 0,
            'rows_mdms_success': 0,
            'rows_mdms_response': 0,
            'rows_hes_cmd': 0,
            'rows_hes_response': 0,
            'hes_retries_responses_deleted': 0,
            'hes_retries_executions_deleted': 0,
            'hes_created': False,
            'all_five_aligned': True,
            'dry_run': dry_run,
        }

        if dry_run:
            result['commands_timestamp_updated'] = len(commands)
            return result

        with mdms_conn.cursor() as mdms_cur, hes_conn.cursor() as hes_cur:
            for cmd in commands:
                exec_id = str(cmd['hes_execution_id'])
                mdms_cur.execute(_load_sql('update_mdms_cmd_timestamps.sql'), {
                    'execution_id': exec_id,
                    'created_at': recharge_created_at,
                })
                result['commands_timestamp_updated'] += mdms_cur.rowcount

                stats = _finalize_execution(
                    mdms_cur, hes_cur, exec_id, recharge_created_at,
                    end_times[exec_id], skip_retry_cleanup,
                )
                for key in (
                    'rows_mdms_success', 'rows_mdms_response', 'rows_hes_cmd', 'rows_hes_response',
                    'hes_retries_responses_deleted', 'hes_retries_executions_deleted',
                ):
                    result[key] += stats[key]

        # NOTE: callers are responsible for committing mdms_conn and hes_conn.
        return result

    # --- Not all 5 already SUCCESS: pick one eligible command (existing behaviour) ---
    selected = select_eligible_command(commands, seed=recharge['transaction_id'],
                                       eligible_command_names=eligible_command_names)
    if selected is None:
        return None

    hes_created = False
    if selected.get('hes_execution_id') is None and create_missing_hes:
        new_exec_id = create_missing_hes_execution(selected, recharge, mdms_conn, hes_conn, dry_run=dry_run)
        if new_exec_id is None:
            return {
                'transaction_id': recharge['transaction_id'],
                'meter_number': recharge['meter_number'],
                'account_id': recharge['account_id'],
                'selected_command': selected['commandName'],
                'hes_creation_failed': True,
                'dry_run': dry_run,
            }
        selected = {**selected, 'hes_execution_id': new_exec_id}
        hes_created = True

    selected_exec_id = str(selected['hes_execution_id'])
    end_time = compute_end_time(recharge_created_at, selected_exec_id, target_elapsed_seconds)

    result = {
        'transaction_id': recharge['transaction_id'],
        'meter_number': recharge['meter_number'],
        'account_id': recharge['account_id'],
        'selected_command': selected['commandName'],
        'selected_execution_id': selected_exec_id,
        'recharge_created_at': recharge_created_at,
        'new_end_time': end_time,
        'commands_timestamp_updated': 0,
        'rows_mdms_success': 0,
        'rows_mdms_response': 0,
        'rows_hes_cmd': 0,
        'rows_hes_response': 0,
        'hes_retries_responses_deleted': 0,
        'hes_retries_executions_deleted': 0,
        'hes_created': hes_created,
        'all_five_aligned': False,
        'dry_run': dry_run,
    }

    if dry_run:
        result['commands_timestamp_updated'] = len(commands)
        return result

    with mdms_conn.cursor() as mdms_cur, hes_conn.cursor() as hes_cur:
        # Step 1: set createdAt and executionStartTime = recharge.created_at for ALL 5 commands
        for cmd in commands:
            mdms_cur.execute(_load_sql('update_mdms_cmd_timestamps.sql'), {
                'execution_id': str(cmd['hes_execution_id']),
                'created_at': recharge_created_at,
            })
            result['commands_timestamp_updated'] += mdms_cur.rowcount

        # Steps 2-6: mark the selected command a clean SUCCESS + retry cleanup
        stats = _finalize_execution(
            mdms_cur, hes_cur, selected_exec_id, recharge_created_at, end_time, skip_retry_cleanup,
        )
        result['rows_mdms_success'] = stats['rows_mdms_success']
        result['rows_mdms_response'] = stats['rows_mdms_response']
        result['rows_hes_cmd'] = stats['rows_hes_cmd']
        result['rows_hes_response'] = stats['rows_hes_response']
        result['hes_retries_responses_deleted'] = stats['hes_retries_responses_deleted']
        result['hes_retries_executions_deleted'] = stats['hes_retries_executions_deleted']

    # NOTE: callers are responsible for committing mdms_conn and hes_conn.
    # Batch callers should commit once after processing the full batch.
    return result
