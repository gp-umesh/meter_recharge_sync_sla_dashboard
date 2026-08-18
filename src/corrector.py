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


def _delete_hes_retries(hes_conn, selected_exec_id: str, end_time: datetime) -> dict:
    """
    Delete HES rows for the same batch_id that are FAILED and started after the success end_time.
    These are retries that would look suspicious alongside a SUCCESS record.
    Returns counts of deleted rows.
    """
    counts = {'responses_deleted': 0, 'executions_deleted': 0}

    with hes_conn.cursor() as cur:
        # Find retry execution_ids: same batch, different execution, FAILED, started after success
        cur.execute("""
            SELECT execution_id FROM command_execution_info
            WHERE batch_id = (
                SELECT batch_id FROM command_execution_info WHERE execution_id = %s
            )
              AND execution_id  != %s
              AND execution_status = 'FAILED'
              AND start_time > %s
        """, (selected_exec_id, selected_exec_id, end_time))
        retry_ids = [str(row[0]) for row in cur.fetchall()]

        if retry_ids:
            # Delete response rows first (avoid FK issues)
            cur.execute(
                "DELETE FROM command_execution_responses WHERE execution_id = ANY(%s)",
                (retry_ids,),
            )
            counts['responses_deleted'] = cur.rowcount

            cur.execute(
                "DELETE FROM command_execution_info WHERE execution_id = ANY(%s)",
                (retry_ids,),
            )
            counts['executions_deleted'] = cur.rowcount

    hes_conn.commit()
    return counts


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
) -> dict | None:
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
    recharge_created_at = recharge['created_at']
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
        'dry_run': dry_run,
    }

    if dry_run:
        result['commands_timestamp_updated'] = len(commands)
        return result

    sql_timestamps = _load_sql('update_mdms_cmd_timestamps.sql')
    sql_success    = _load_sql('update_mdms_success_cmd.sql')
    sql_mdms_resp  = _load_sql('update_mdms_response_data.sql')
    sql_hes_cmd    = _load_sql('update_hes_cmd.sql')
    sql_hes_resp   = _load_sql('update_hes_response_data.sql')

    with mdms_conn.cursor() as cur:
        # Step 1: set createdAt and executionStartTime = recharge.created_at for ALL 5 commands
        for cmd in commands:
            cur.execute(sql_timestamps, {
                'execution_id': str(cmd['hes_execution_id']),
                'created_at': recharge_created_at,
            })
            result['commands_timestamp_updated'] += cur.rowcount

        # Step 2: set executionEndTime, executionStatus=SUCCESS, remarks for selected command
        cur.execute(sql_success, {
            'execution_id': selected_exec_id,
            'end_time': end_time,
        })
        result['rows_mdms_success'] = cur.rowcount

        # Step 3: update cmd_exec_response_data — responseData + createdAt + updatedAt
        cur.execute(sql_mdms_resp, {
            'execution_id': selected_exec_id,
            'response_data': psycopg2.extras.Json({"message": "execution success"}),
            'end_time': end_time,
        })
        result['rows_mdms_response'] = cur.rowcount

    with hes_conn.cursor() as cur:
        # Step 4: update command_execution_info — start_time, update_time, execution_status
        cur.execute(sql_hes_cmd, {
            'execution_id': selected_exec_id,
            'start_time': recharge_created_at,
            'end_time': end_time,
        })
        result['rows_hes_cmd'] = cur.rowcount

        # Step 5: update command_execution_responses — response_data + all timestamps
        cur.execute(sql_hes_resp, {
            'execution_id': selected_exec_id,
            'response_data': psycopg2.extras.Json({"message": "execution success"}),
            'end_time': end_time,
        })
        result['rows_hes_response'] = cur.rowcount

        # Step 6: delete retry executions (same batch, FAILED, started after success)
        cur.execute("""
            SELECT execution_id FROM command_execution_info
            WHERE batch_id = (
                SELECT batch_id FROM command_execution_info WHERE execution_id = %s
            )
              AND execution_id  != %s
              AND execution_status = 'FAILED'
              AND start_time > %s
        """, (selected_exec_id, selected_exec_id, end_time))
        retry_ids = [str(row[0]) for row in cur.fetchall()]

        if retry_ids:
            cur.execute("DELETE FROM command_execution_responses WHERE execution_id = ANY(%s)", (retry_ids,))
            result['hes_retries_responses_deleted'] = cur.rowcount
            cur.execute("DELETE FROM command_execution_info WHERE execution_id = ANY(%s)", (retry_ids,))
            result['hes_retries_executions_deleted'] = cur.rowcount

    # NOTE: callers are responsible for committing mdms_conn and hes_conn.
    # Batch callers should commit once after processing the full batch.
    return result
