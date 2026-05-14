import os
from pathlib import Path
from collections import defaultdict

import psycopg2.extras


_SQL_DIR = Path(__file__).parent.parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text()


def count_recharges(date: str, conn) -> int:
    from_date = f"{date} 00:00:00+05:30"
    to_date   = f"{date} 23:59:59.999999+05:30"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM recharges_data WHERE created_at >= %s AND created_at < %s",
            (from_date, to_date),
        )
        return cur.fetchone()[0]


def fetch_recharges(date: str, conn, limit: int = 10000, offset: int = 0) -> list[dict]:
    from_date = f"{date} 00:00:00+05:30"
    to_date   = f"{date} 23:59:59.999999+05:30"
    sql = _load_sql("query_recharges.sql")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"from_date": from_date, "to_date": to_date,
                          "limit": limit, "offset": offset})
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


def fetch_hes_executions(execution_ids: list[str], conn) -> dict[str, dict]:
    if not execution_ids:
        return {}
    sql = _load_sql("query_hes_executions.sql")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"execution_ids": execution_ids})
        return {str(row["execution_id"]): dict(row) for row in cur.fetchall()}
