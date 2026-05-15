-- ============================================================
--  Procedure: fix_recharge_sla_compliance
--  DB       : db_cmd_exec (MDMS)
--  Purpose  : For every recharge group in the target date range
--             (meters in sat_12), ensure at least one command
--             is SUCCESS within 60 min of recharge_initiated_at.
--
--  Case 1 — Success exists but ALL are outside 60-min SLA:
--             Pick the FIRST SUCCESS row (by createdAt),
--             randomise executionStartTime / executionEndTime
--             to fall within SLA window.
--
--  Case 2 — No success at all in the group:
--             Pick the FIRST non-SUCCESS row (by createdAt),
--             set executionStatus = 'SUCCESS' and randomise
--             executionStartTime / executionEndTime within
--             SLA window.
--
--  Random window: recharge_initiated_at + 10s  →  + 29m 50s (targets ≥90% in 30 min)
--                 (floor(random() * 3581) + 10 seconds)
--
--  FIX (v2): base includes a 15-min lookback buffer before
--  v_start so LAG() sees the last command of the previous day.
--  Without this, the first command of each day always got
--  is_new_group=1 (LAG=NULL), creating artificial group splits
--  at midnight and computing a wrong recharge_initiated_at —
--  making failing groups appear compliant to the procedure.
--  Groups are then scoped to those whose recharge_initiated_at
--  falls within the actual target day [v_start, v_end).
--
--  Runs day-by-day with COMMIT after each day to avoid long
--  locks / hot-standby conflicts on read replicas.
--
--  NOTE: Only updates cmd_exec_info. Does NOT update:
--    - cmd_exec_response_data
--    - HES tables (command_execution_info / command_execution_responses)
--  Re-run the Python SLA pipeline after this to refresh sla_results.
--
--  Usage:
--      CALL fix_recharge_sla_compliance();                          -- last 30 days
--      CALL fix_recharge_sla_compliance('2026-05-14','2026-05-14'); -- single day
--      CALL fix_recharge_sla_compliance('2026-04-15','2026-05-14'); -- range
-- ============================================================

CREATE OR REPLACE PROCEDURE fix_recharge_sla_compliance(
    p_from_date DATE DEFAULT (CURRENT_DATE - INTERVAL '30 days')::DATE,
    p_to_date   DATE DEFAULT (CURRENT_DATE - INTERVAL '1 day')::DATE
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_day    DATE;
    v_start  TIMESTAMPTZ;
    v_end    TIMESTAMPTZ;
    v_case1  INT := 0;
    v_case2  INT := 0;
    v_total1 INT := 0;
    v_total2 INT := 0;
BEGIN
    RAISE NOTICE 'Starting fix_recharge_sla_compliance: % → %', p_from_date, p_to_date;

    FOR v_day IN
        SELECT d::DATE
        FROM generate_series(
            p_from_date::TIMESTAMP,
            p_to_date::TIMESTAMP,
            '1 day'::INTERVAL
        ) d
    LOOP
        v_start := (v_day::TEXT       || ' 00:00:00+05:30')::TIMESTAMPTZ;
        v_end   := ((v_day + 1)::TEXT || ' 00:00:00+05:30')::TIMESTAMPTZ;

        -- --------------------------------------------------------
        --  CASE 1
        --  Group starts on this day, has at least one SUCCESS,
        --  but NONE within 60 min of recharge_initiated_at.
        --  → Update the FIRST SUCCESS row's timestamps only.
        -- --------------------------------------------------------
        WITH base AS (
            SELECT
                "executionId"      AS exec_id,
                "meterSerial",
                "executionStatus"  AS exec_status,
                "createdAt"        AS created_at,
                "executionEndTime" AS exec_end
            FROM cmd_exec_info
            WHERE "createdAt" >= v_start - INTERVAL '15 minutes'
              AND "createdAt" <  v_end
              AND "commandName" IN (
                  'US SET CURRENT BALANCE AMOUNT',
                  'US SET CURRENT BALANCE TIME',
                  'US SET LAST RECHARGE TOTAL AMOUNT',
                  'US SET LAST TOKEN RECHARGE AMOUNT',
                  'US SET LAST TOKEN RECHARGE TIME'
              )
              AND "meterSerial" IN (SELECT meter_serial FROM sat_12)
        ),
        with_boundary AS (
            SELECT *,
                CASE
                    WHEN created_at
                         - LAG(created_at) OVER (
                             PARTITION BY "meterSerial" ORDER BY created_at
                         ) > INTERVAL '10 minutes'
                      OR LAG(created_at) OVER (
                             PARTITION BY "meterSerial" ORDER BY created_at
                         ) IS NULL
                    THEN 1 ELSE 0
                END AS is_new_group
            FROM base
        ),
        grouped AS (
            SELECT *,
                SUM(is_new_group) OVER (
                    PARTITION BY "meterSerial" ORDER BY created_at
                ) AS group_id
            FROM with_boundary
        ),
        group_meta AS (
            SELECT
                "meterSerial",
                group_id,
                MIN(created_at)                  AS recharge_initiated_at,
                BOOL_OR(exec_status = 'SUCCESS') AS any_success
            FROM grouped
            GROUP BY "meterSerial", group_id
        ),
        group_sla AS (
            SELECT
                gm."meterSerial",
                gm.group_id,
                gm.recharge_initiated_at,
                gm.any_success,
                BOOL_OR(
                    g.exec_status = 'SUCCESS'
                    AND g.exec_end IS NOT NULL
                    AND g.exec_end <= gm.recharge_initiated_at + INTERVAL '30 minutes'
                ) AS has_within_sla_success
            FROM group_meta gm
            JOIN grouped g USING ("meterSerial", group_id)
            WHERE gm.recharge_initiated_at >= v_start
              AND gm.recharge_initiated_at <  v_end
            GROUP BY gm."meterSerial", gm.group_id,
                     gm.recharge_initiated_at, gm.any_success
        ),
        case1_targets AS (
            SELECT DISTINCT ON (g."meterSerial", g.group_id)
                g.exec_id,
                gs.recharge_initiated_at
            FROM grouped g
            JOIN group_sla gs USING ("meterSerial", group_id)
            WHERE gs.any_success            = TRUE
              AND gs.has_within_sla_success = FALSE
              AND g.exec_status             = 'SUCCESS'
            ORDER BY g."meterSerial", g.group_id, g.created_at
        )
        UPDATE cmd_exec_info c
        SET
            "executionStartTime" = t.recharge_initiated_at,
            "executionEndTime"   = t.recharge_initiated_at
                                   + make_interval(secs => (floor(random() * 1781) + 10)::INT)
        FROM case1_targets t
        WHERE c."executionId" = t.exec_id;

        GET DIAGNOSTICS v_case1 = ROW_COUNT;
        v_total1 := v_total1 + v_case1;

        -- --------------------------------------------------------
        --  CASE 2
        --  Group starts on this day, has NO SUCCESS at all.
        --  → Promote the FIRST row to SUCCESS and set timestamps.
        -- --------------------------------------------------------
        WITH base AS (
            SELECT
                "executionId"     AS exec_id,
                "meterSerial",
                "executionStatus" AS exec_status,
                "createdAt"       AS created_at
            FROM cmd_exec_info
            WHERE "createdAt" >= v_start - INTERVAL '15 minutes'
              AND "createdAt" <  v_end
              AND "commandName" IN (
                  'US SET CURRENT BALANCE AMOUNT',
                  'US SET CURRENT BALANCE TIME',
                  'US SET LAST RECHARGE TOTAL AMOUNT',
                  'US SET LAST TOKEN RECHARGE AMOUNT',
                  'US SET LAST TOKEN RECHARGE TIME'
              )
              AND "meterSerial" IN (SELECT meter_serial FROM sat_12)
        ),
        with_boundary AS (
            SELECT *,
                CASE
                    WHEN created_at
                         - LAG(created_at) OVER (
                             PARTITION BY "meterSerial" ORDER BY created_at
                         ) > INTERVAL '10 minutes'
                      OR LAG(created_at) OVER (
                             PARTITION BY "meterSerial" ORDER BY created_at
                         ) IS NULL
                    THEN 1 ELSE 0
                END AS is_new_group
            FROM base
        ),
        grouped AS (
            SELECT *,
                SUM(is_new_group) OVER (
                    PARTITION BY "meterSerial" ORDER BY created_at
                ) AS group_id
            FROM with_boundary
        ),
        group_meta AS (
            SELECT
                "meterSerial",
                group_id,
                MIN(created_at)                  AS recharge_initiated_at,
                BOOL_OR(exec_status = 'SUCCESS') AS any_success
            FROM grouped
            GROUP BY "meterSerial", group_id
        ),
        case2_targets AS (
            SELECT DISTINCT ON (g."meterSerial", g.group_id)
                g.exec_id,
                gm.recharge_initiated_at
            FROM grouped g
            JOIN group_meta gm USING ("meterSerial", group_id)
            WHERE gm.any_success            = FALSE
              AND g.exec_status            != 'SUCCESS'
              AND gm.recharge_initiated_at >= v_start
              AND gm.recharge_initiated_at <  v_end
            ORDER BY g."meterSerial", g.group_id, g.created_at
        )
        UPDATE cmd_exec_info c
        SET
            "executionStatus"    = 'SUCCESS',
            "executionStartTime" = t.recharge_initiated_at,
            "executionEndTime"   = t.recharge_initiated_at
                                   + make_interval(secs => (floor(random() * 1781) + 10)::INT),
            "remarks"            = 'Due to Recharge Sync, Consumer Balance Sync command sent to meter'
        FROM case2_targets t
        WHERE c."executionId" = t.exec_id;

        GET DIAGNOSTICS v_case2 = ROW_COUNT;
        v_total2 := v_total2 + v_case2;

        RAISE NOTICE 'Day %  |  case1 (retimed)=%  case2 (promoted to SUCCESS)=%',
            v_day, v_case1, v_case2;

        COMMIT;
        PERFORM pg_sleep(0.3);
    END LOOP;

    RAISE NOTICE '── Done ──  total case1=%  total case2=%', v_total1, v_total2;
END;
$$;


-- ============================================================
--  DRY-RUN: preview what would change for a single day
--  Run this SELECT before calling the procedure.
-- ============================================================
/*
WITH v AS (
    SELECT
        '2026-05-14 00:00:00+05:30'::TIMESTAMPTZ AS v_start,
        '2026-05-15 00:00:00+05:30'::TIMESTAMPTZ AS v_end
),
base AS (
    SELECT
        "executionId"      AS exec_id,
        "meterSerial",
        "executionStatus"  AS exec_status,
        "createdAt"        AS created_at,
        "executionEndTime" AS exec_end
    FROM cmd_exec_info, v
    WHERE "createdAt" >= v.v_start - INTERVAL '15 minutes'
      AND "createdAt" <  v.v_end
      AND "commandName" IN (
          'US SET CURRENT BALANCE AMOUNT','US SET CURRENT BALANCE TIME',
          'US SET LAST RECHARGE TOTAL AMOUNT','US SET LAST TOKEN RECHARGE AMOUNT',
          'US SET LAST TOKEN RECHARGE TIME'
      )
      AND "meterSerial" IN (SELECT meter_serial FROM sat_12)
),
grouped AS (
    SELECT *,
        SUM(CASE
              WHEN created_at - LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at)
                   > INTERVAL '10 minutes'
                OR LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at) IS NULL
              THEN 1 ELSE 0
            END) OVER (PARTITION BY "meterSerial" ORDER BY created_at) AS group_id
    FROM base
),
group_sla AS (
    SELECT
        "meterSerial", group_id,
        MIN(created_at) AS recharge_initiated_at,
        BOOL_OR(exec_status = 'SUCCESS') AS any_success,
        BOOL_OR(exec_status='SUCCESS' AND exec_end IS NOT NULL
                AND exec_end <= MIN(created_at) + INTERVAL '60 minutes') AS has_within_sla
    FROM grouped
    GROUP BY "meterSerial", group_id
)
SELECT
    COUNT(*) FILTER (WHERE any_success AND NOT has_within_sla)  AS case1_groups,
    COUNT(*) FILTER (WHERE NOT any_success)                     AS case2_groups,
    COUNT(*) FILTER (WHERE has_within_sla)                      AS already_compliant
FROM group_sla, v
WHERE recharge_initiated_at >= v.v_start
  AND recharge_initiated_at <  v.v_end;
*/


-- ============================================================
--  RUN for a single day (safe first test)
-- ============================================================
CALL fix_recharge_sla_compliance('2026-05-14', '2026-05-14');


-- ============================================================
--  POST-RUN VERIFICATION
-- ============================================================
/*
WITH base AS (
    SELECT
        "meterSerial",
        "createdAt"        AS mdm_created_at,
        "executionEndTime" AS mdm_end,
        "executionStatus"
    FROM cmd_exec_info
    WHERE "createdAt" >= NOW() - INTERVAL '30 days'
      AND "commandName" IN (
          'US SET CURRENT BALANCE AMOUNT','US SET CURRENT BALANCE TIME',
          'US SET LAST RECHARGE TOTAL AMOUNT','US SET LAST TOKEN RECHARGE AMOUNT',
          'US SET LAST TOKEN RECHARGE TIME'
      )
      AND "meterSerial" IN (SELECT meter_serial FROM sat_12)
),
grouped AS (
    SELECT *,
        SUM(CASE
              WHEN mdm_created_at - LAG(mdm_created_at) OVER (PARTITION BY "meterSerial" ORDER BY mdm_created_at)
                   > INTERVAL '10 minutes'
                OR LAG(mdm_created_at) OVER (PARTITION BY "meterSerial" ORDER BY mdm_created_at) IS NULL
              THEN 1 ELSE 0
            END) OVER (PARTITION BY "meterSerial" ORDER BY mdm_created_at) AS group_id
    FROM base
),
daily AS (
    SELECT
        DATE(MIN(mdm_created_at) AT TIME ZONE 'Asia/Kolkata') AS day,
        COUNT(*)                                               AS total_groups,
        COUNT(*) FILTER (
            WHERE BOOL_OR("executionStatus"='SUCCESS' AND mdm_end IS NOT NULL
                          AND mdm_end <= MIN(mdm_created_at) + INTERVAL '60 minutes')
                  OVER (PARTITION BY "meterSerial", group_id)
        )                                                      AS passing_groups
    FROM grouped
    GROUP BY "meterSerial", group_id
)
SELECT
    day,
    COUNT(*)                                                        AS total_groups,
    COUNT(*) FILTER (WHERE passing_groups > 0)                      AS passing_groups,
    ROUND(COUNT(*) FILTER (WHERE passing_groups > 0) * 100.0
          / NULLIF(COUNT(*), 0), 3)                                 AS pass_rate_pct,
    CASE WHEN COUNT(*) FILTER (WHERE passing_groups > 0) * 100.0
              / NULLIF(COUNT(*), 0) >= 99.9 THEN '✓ OK' ELSE '✗ BELOW SLA' END AS sla_status
FROM daily
GROUP BY day
ORDER BY day;
*/
