import json
import random
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


def apply_correction(
    recharge: dict,
    commands: list[dict],
    mdms_conn,
    hes_conn,
    target_elapsed_seconds: int = 1200,
    dry_run: bool = True,
    eligible_command_names: list[str] | None = None,
) -> dict | None:
    selected = select_eligible_command(commands, seed=recharge['transaction_id'],
                                       eligible_command_names=eligible_command_names)
    if selected is None:
        return None

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
