from datetime import datetime, timezone, timedelta

import psycopg2

IST = timezone(timedelta(hours=5, minutes=30))


def validation_conn(mdms_url: str):
    """Open a connection to the validation_rules DB (same host/creds as MDMS, different DB)."""
    url = mdms_url.rsplit("/", 1)[0] + "/validation_rules"
    return psycopg2.connect(url)


def fetch_meter_ldp_map(meter_serials: list[str], conn) -> dict[str, dict]:
    """
    Batch-fetch communication status for all given meter serials in one query.
    Returns dict of {meter_serial: {effective_ldp, meter_ldp, dcu_ldp, com_type}}.
    effective_ldp = GREATEST(meter_ldp, dcu_ldp) — handles RF meters whose meter_ldp
    is stale but dcu_ldp is recent (commands route through the DCU).
    Missing meters are absent (treated as non-communicating).
    """
    if not meter_serials:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT meter_serial,
                   com_type,
                   meter_ldp,
                   dcu_ldp,
                   GREATEST(meter_ldp, dcu_ldp) AS effective_ldp
            FROM meters_communication_status
            WHERE meter_serial = ANY(%s)
            """,
            (meter_serials,),
        )
        result = {}
        for row in cur.fetchall():
            serial, com_type, meter_ldp, dcu_ldp, effective_ldp = row
            if effective_ldp is not None:
                result[serial] = {
                    "com_type":     com_type,
                    "meter_ldp":    meter_ldp,
                    "dcu_ldp":      dcu_ldp,
                    "effective_ldp": effective_ldp,
                }
        return result


def is_meter_communicating(meter_serial: str, recharge_created_at, ldp_map: dict) -> tuple[bool, dict | None]:
    """
    Check against a pre-fetched ldp_map (from fetch_meter_ldp_map).
    Returns (True, None) if communicating.
    Returns (False, detail_dict) if non-communicating or not found, where detail_dict
    contains meter_ldp, dcu_ldp, effective_ldp, com_type, and gap_days for logging.
    """
    info = ldp_map.get(meter_serial)
    recharge_ist_naive = recharge_created_at.astimezone(IST).replace(tzinfo=None)

    if info is None:
        return False, {
            "com_type":     "UNKNOWN",
            "meter_ldp":    None,
            "dcu_ldp":      None,
            "effective_ldp": None,
            "gap_days":     None,
            "reason":       "not found in meters_communication_status",
        }

    effective_ldp = info["effective_ldp"]
    gap = recharge_ist_naive - effective_ldp
    gap_days = gap.days + gap.seconds / 86400

    if effective_ldp >= recharge_ist_naive:
        return True, None

    return False, {
        "com_type":     info["com_type"],
        "meter_ldp":    info["meter_ldp"],
        "dcu_ldp":      info["dcu_ldp"],
        "effective_ldp": effective_ldp,
        "gap_days":     round(gap_days, 1),
        "reason":       (
            f"last contact {effective_ldp.strftime('%Y-%m-%d %H:%M:%S')} "
            f"({round(gap_days, 1)}d before recharge)"
        ),
    }
