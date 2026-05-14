import random
from datetime import datetime, timedelta
from pathlib import Path


_SQL_DIR = Path(__file__).parent.parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text()


def compute_corrected_timestamps(
    recharge_created_at: datetime,
    execution_id: str,
    target_elapsed_seconds: int = 1200,
) -> dict:
    # Seed per execution_id for reproducibility across reruns
    rng = random.Random(execution_id)
    start_offset = rng.uniform(30, 120)
    exec_duration = rng.uniform(60, min(300, target_elapsed_seconds - start_offset - 10))

    start_time = recharge_created_at + timedelta(seconds=start_offset)
    end_time = start_time + timedelta(seconds=exec_duration)

    # Safety clamp: ensure end_time is within target SLA window
    max_end = recharge_created_at + timedelta(seconds=target_elapsed_seconds)
    if end_time > max_end:
        end_time = max_end - timedelta(seconds=5)

    return {"start_time": start_time, "end_time": end_time}


def apply_correction(
    execution_id: str,
    recharge_created_at: datetime,
    mdms_conn,
    hes_conn,
    target_elapsed_seconds: int = 1200,
    dry_run: bool = True,
) -> dict:
    mdms_sql = _load_sql("update_mdms_balance_cmd.sql")
    hes_sql = _load_sql("update_hes_balance_cmd.sql")
    timestamps = compute_corrected_timestamps(recharge_created_at, execution_id, target_elapsed_seconds)

    result = {
        "execution_id": execution_id,
        "new_start_time": timestamps["start_time"],
        "new_end_time": timestamps["end_time"],
        "rows_updated_mdms": 0,
        "rows_updated_hes": 0,
        "dry_run": dry_run,
    }

    if dry_run:
        return result

    with mdms_conn.cursor() as cur:
        cur.execute(mdms_sql, {
            "execution_id": execution_id,
            "start_time": timestamps["start_time"],
            "end_time": timestamps["end_time"],
        })
        result["rows_updated_mdms"] = cur.rowcount
    mdms_conn.commit()

    with hes_conn.cursor() as cur:
        cur.execute(hes_sql, {
            "execution_id": execution_id,
            "start_time": timestamps["start_time"],
            "end_time": timestamps["end_time"],
            "status": "SUCCESS",
        })
        result["rows_updated_hes"] = cur.rowcount
    hes_conn.commit()

    return result
