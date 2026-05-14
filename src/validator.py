from datetime import datetime, timezone, timedelta

import psycopg2

IST = timezone(timedelta(hours=5, minutes=30))


def validation_conn(mdms_url: str):
    """Open a connection to the validation_rules DB (same host/creds as MDMS, different DB)."""
    url = mdms_url.rsplit("/", 1)[0] + "/validation_rules"
    return psycopg2.connect(url)


def fetch_meter_ldp_map(meter_serials: list[str], conn) -> dict[str, datetime]:
    """
    Batch-fetch the effective last communication time for all given meter serials.
    Uses GREATEST(meter_ldp, dcu_ldp) so RF meters communicating via DCU are not
    incorrectly flagged as non-communicating when meter_ldp is stale but dcu_ldp is recent.
    Returns dict of {meter_serial: effective_ldp (naive datetime, assumed IST)}.
    Missing meters are absent from the result (treated as non-communicating).
    """
    if not meter_serials:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT meter_serial,
                   GREATEST(meter_ldp, dcu_ldp) AS effective_ldp
            FROM meters_communication_status
            WHERE meter_serial = ANY(%s)
            """,
            (meter_serials,),
        )
        return {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}


def is_meter_communicating(meter_serial: str, recharge_created_at, ldp_map: dict) -> bool:
    """
    Check against a pre-fetched ldp_map (from fetch_meter_ldp_map).
    Returns True if effective_ldp (GREATEST of meter_ldp and dcu_ldp) >= recharge time.
    False if meter not found or both ldp fields are before the recharge.
    """
    effective_ldp = ldp_map.get(meter_serial)
    if effective_ldp is None:
        return False
    recharge_ist_naive = recharge_created_at.astimezone(IST).replace(tzinfo=None)
    return effective_ldp >= recharge_ist_naive
