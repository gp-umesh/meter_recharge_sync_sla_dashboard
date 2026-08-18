import os
from pathlib import Path
from collections import defaultdict

import psycopg2.extras


_SQL_DIR = Path(__file__).parent.parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text()


def count_recharges(date: str, conn, meter_serials: list[str] | None = None) -> int:
    from_date = f"{date} 00:00:00+05:30"
    to_date   = f"{date} 23:59:59.999999+05:30"
    with conn.cursor() as cur:
        if meter_serials:
            cur.execute(
                "SELECT COUNT(*) FROM recharges_data "
                "WHERE created_at >= %s AND created_at < %s AND meter_number = ANY(%s)",
                (from_date, to_date, meter_serials),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM recharges_data WHERE created_at >= %s AND created_at < %s",
                (from_date, to_date),
            )
        return cur.fetchone()[0]


def fetch_meter_numbers_for_date(date: str, conn, meter_serials: list[str] | None = None) -> list[str]:
    """Lightweight day-level meter_number list, used to fetch MDMS commands ONCE per day
    instead of once per batch (avoids repeated full-day scans of cmd_exec_info)."""
    from_date = f"{date} 00:00:00+05:30"
    to_date   = f"{date} 23:59:59.999999+05:30"
    with conn.cursor() as cur:
        if meter_serials:
            cur.execute(
                "SELECT DISTINCT meter_number FROM recharges_data "
                "WHERE created_at >= %s AND created_at < %s AND meter_number = ANY(%s)",
                (from_date, to_date, meter_serials),
            )
        else:
            cur.execute(
                "SELECT DISTINCT meter_number FROM recharges_data WHERE created_at >= %s AND created_at < %s",
                (from_date, to_date),
            )
        return [row[0] for row in cur.fetchall()]


def fetch_recharges(
    date: str,
    conn,
    limit: int = 10000,
    offset: int = 0,
    meter_serials: list[str] | None = None,
) -> list[dict]:
    from_date = f"{date} 00:00:00+05:30"
    to_date   = f"{date} 23:59:59.999999+05:30"
    if meter_serials:
        sql    = _load_sql("query_recharges_filtered.sql")
        params = {"from_date": from_date, "to_date": to_date,
                  "limit": limit, "offset": offset, "meter_serials": meter_serials}
    else:
        sql    = _load_sql("query_recharges.sql")
        params = {"from_date": from_date, "to_date": to_date,
                  "limit": limit, "offset": offset}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def fetch_mdms_commands(account_ids: list[str], date: str, conn) -> dict[str, list[dict]]:
    if not account_ids:
        return {}
    from_date = f"{date} 00:00:00+05:30"
    to_date = f"{date} 23:59:59.999999+05:30"
    sql = _load_sql("query_mdms_commands.sql")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"from_date": from_date, "to_date": to_date, "account_ids": account_ids})
        rows = [dict(row) for row in cur.fetchall()]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["account_id"]].append(row)
    return dict(grouped)


def fetch_mdms_commands_by_meter(meter_serials: list[str], date: str, conn) -> dict[str, list[dict]]:
    """Like fetch_mdms_commands, but joins on meterSerial instead of the JSONB
    additionalInfo->>'accountId' expression. meterSerial has a supporting
    (meterSerial, createdAt) index in cmd_exec_info — accountId does not — so this
    is dramatically faster (index scan vs. full-day scan) when a meter list is
    already available (e.g. from a --sat-table filter or the recharge rows themselves)."""
    if not meter_serials:
        return {}
    from_date = f"{date} 00:00:00+05:30"
    to_date = f"{date} 23:59:59.999999+05:30"
    sql = _load_sql("query_mdms_commands_by_meter.sql")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"from_date": from_date, "to_date": to_date, "meter_serials": meter_serials})
        rows = [dict(row) for row in cur.fetchall()]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["meterSerial"]].append(row)
    return dict(grouped)


def fetch_hes_executions(execution_ids: list[str], conn) -> dict[str, dict]:
    if not execution_ids:
        return {}
    sql = _load_sql("query_hes_executions.sql")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"execution_ids": execution_ids})
        return {str(row["execution_id"]): dict(row) for row in cur.fetchall()}
