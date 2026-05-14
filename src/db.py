import os
import psycopg2
from contextlib import contextmanager


def _connect(env_var: str):
    url = os.environ.get(env_var)
    if not url:
        raise EnvironmentError(f"{env_var} environment variable is not set")
    return psycopg2.connect(url)


@contextmanager
def prepaid_conn():
    conn = _connect("DB_PREPAID_URL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def mdms_conn():
    conn = _connect("DB_MDMS_URL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def hes_conn():
    conn = _connect("DB_HES_URL")
    try:
        yield conn
    finally:
        conn.close()
