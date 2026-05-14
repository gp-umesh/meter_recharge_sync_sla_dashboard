#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def main():
    url = os.environ.get("DB_PREPAID_URL")
    if not url:
        print("[setup_db] ERROR: DB_PREPAID_URL is not set", file=sys.stderr)
        sys.exit(1)

    import psycopg2

    ddl = (Path(__file__).parent / "sql" / "create_sla_results.sql").read_text()
    try:
        conn = psycopg2.connect(url)
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        conn.close()
        print("[setup_db] sla_results table created (or already exists) in db_prepaid_engine")
    except Exception as exc:
        print(f"[setup_db] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
