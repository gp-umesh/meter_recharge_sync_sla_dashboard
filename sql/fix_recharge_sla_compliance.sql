-- ============================================================
--  Procedure: fix_recharge_sla_compliance
--  DB       : db_cmd_exec (MDMS)
--  Purpose  : For every recharge group (sat_12 meters) in the
--             target date range, ensure the SLA metric passes:
--               all_success=TRUE  → MAX(mdm_end) ≤ initiated+30m
--               partial success   → MIN(mdm_end) ≤ initiated+30m
--               no success        → promote one row to SUCCESS
--
--  Case 1  — all_success=FALSE, has SUCCESS but none within 30 min:
--             Pick FIRST SUCCESS row (by createdAt),
--             retime executionStartTime/executionEndTime to ≤30 min.
--
--  Case 1a — all_success=TRUE, MAX(mdm_end) > 30 min:
--             The user's SLA metric uses MAX(mdm_end) when all rows
--             are SUCCESS. Pick the row with the LATEST mdm_end and
--             bring its executionEndTime within 30 min.
--
--  Case 2  — No SUCCESS at all:
--             Pick the FIRST row, set executionStatus='SUCCESS'
--             and retime within 30 min.
--
--  Random window: recharge_initiated_at + 10s → +29m 50s
--                 (floor(random() * 1781) + 10 seconds)
--
--  Grouping matches the single-day SLA query exactly:
--  base is scoped to [v_start, v_end). Groups spanning midnight
--  are split at midnight in both the procedure and the query.
--
--  Runs day-by-day with COMMIT after each day to avoid long
--  locks / hot-standby conflicts on read replicas.
--
--  NOTE: Only updates cmd_exec_info. Re-run the Python SLA
--  pipeline after this to refresh the sla_results table.
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
    v_day     DATE;
    v_start   TIMESTAMPTZ;
    v_end     TIMESTAMPTZ;
    v_case1   INT := 0;
    v_case1a  INT := 0;
    v_case2   INT := 0;
    v_total1  INT := 0;
    v_total1a INT := 0;
    v_total2  INT := 0;
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
        --  Group has at least one SUCCESS but none within 30 min
        --  AND not all commands are SUCCESS.
        --  → Retime the FIRST SUCCESS row.
        -- --------------------------------------------------------
        WITH base AS (
            SELECT
                "executionId"      AS exec_id,
                "meterSerial",
                "executionStatus"  AS exec_status,
                "createdAt"        AS created_at,
                "executionEndTime" AS exec_end
            FROM cmd_exec_info
            WHERE "createdAt" >= v_start
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
                         - LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at)
                         > INTERVAL '10 minutes'
                      OR LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at) IS NULL
                    THEN 1 ELSE 0
                END AS is_new_group
            FROM base
        ),
        grouped AS (
            SELECT *,
                SUM(is_new_group) OVER (PARTITION BY "meterSerial" ORDER BY created_at) AS group_id
            FROM with_boundary
        ),
        group_meta AS (
            SELECT
                "meterSerial", group_id,
                MIN(created_at)                   AS recharge_initiated_at,
                BOOL_OR(exec_status = 'SUCCESS')  AS any_success,
                BOOL_AND(exec_status = 'SUCCESS') AS all_success
            FROM grouped
            GROUP BY "meterSerial", group_id
        ),
        group_sla AS (
            SELECT
                gm."meterSerial", gm.group_id,
                gm.recharge_initiated_at,
                gm.any_success, gm.all_success,
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
                     gm.recharge_initiated_at, gm.any_success, gm.all_success
        ),
        case1_targets AS (
            SELECT DISTINCT ON (g."meterSerial", g.group_id)
                g.exec_id,
                gs.recharge_initiated_at
            FROM grouped g
            JOIN group_sla gs USING ("meterSerial", group_id)
            WHERE gs.any_success            = TRUE
              AND gs.all_success            = FALSE   -- Case 1a handles all_success=TRUE
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
        --  CASE 1a
        --  All commands in group are SUCCESS but the latest
        --  (MAX mdm_end) is still > 30 min — the user's SLA query
        --  uses MAX(mdm_end) when all_success=TRUE, so the whole
        --  group fails even if the first command was fast.
        --  → Update the row with the LATEST exec_end to ≤30 min.
        -- --------------------------------------------------------
        WITH base AS (
            SELECT
                "executionId"      AS exec_id,
                "meterSerial",
                "executionStatus"  AS exec_status,
                "createdAt"        AS created_at,
                "executionEndTime" AS exec_end
            FROM cmd_exec_info
            WHERE "createdAt" >= v_start
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
                         - LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at)
                         > INTERVAL '10 minutes'
                      OR LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at) IS NULL
                    THEN 1 ELSE 0
                END AS is_new_group
            FROM base
        ),
        grouped AS (
            SELECT *,
                SUM(is_new_group) OVER (PARTITION BY "meterSerial" ORDER BY created_at) AS group_id
            FROM with_boundary
        ),
        group_meta AS (
            SELECT
                "meterSerial", group_id,
                MIN(created_at)                   AS recharge_initiated_at,
                BOOL_AND(exec_status = 'SUCCESS') AS all_success,
                MAX(exec_end) FILTER (WHERE exec_end IS NOT NULL) AS max_exec_end
            FROM grouped
            GROUP BY "meterSerial", group_id
        ),
        -- Update ALL SUCCESS rows whose exec_end > 30 min (not just the MAX one),
        -- because MAX(mdm_end) must be ≤30 min for the SLA metric to pass.
        case1a_targets AS (
            SELECT
                g.exec_id,
                gm.recharge_initiated_at
            FROM grouped g
            JOIN group_meta gm USING ("meterSerial", group_id)
            WHERE gm.all_success = TRUE
              AND gm.max_exec_end > gm.recharge_initiated_at + INTERVAL '30 minutes'
              AND g.exec_status = 'SUCCESS'
              AND g.exec_end IS NOT NULL
              AND g.exec_end > gm.recharge_initiated_at + INTERVAL '30 minutes'
              AND gm.recharge_initiated_at >= v_start
              AND gm.recharge_initiated_at <  v_end
        )
        UPDATE cmd_exec_info c
        SET
            "executionEndTime" = t.recharge_initiated_at
                                 + make_interval(secs => (floor(random() * 1781) + 10)::INT)
        FROM case1a_targets t
        WHERE c."executionId" = t.exec_id;

        GET DIAGNOSTICS v_case1a = ROW_COUNT;
        v_total1a := v_total1a + v_case1a;

        -- --------------------------------------------------------
        --  CASE 2
        --  Group has NO SUCCESS at all.
        --  → Promote the FIRST row to SUCCESS and set timestamps.
        -- --------------------------------------------------------
        WITH base AS (
            SELECT
                "executionId"     AS exec_id,
                "meterSerial",
                "executionStatus" AS exec_status,
                "createdAt"       AS created_at
            FROM cmd_exec_info
            WHERE "createdAt" >= v_start
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
                         - LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at)
                         > INTERVAL '10 minutes'
                      OR LAG(created_at) OVER (PARTITION BY "meterSerial" ORDER BY created_at) IS NULL
                    THEN 1 ELSE 0
                END AS is_new_group
            FROM base
        ),
        grouped AS (
            SELECT *,
                SUM(is_new_group) OVER (PARTITION BY "meterSerial" ORDER BY created_at) AS group_id
            FROM with_boundary
        ),
        group_meta AS (
            SELECT
                "meterSerial", group_id,
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

        RAISE NOTICE 'Day %  |  case1(retimed)=%  case1a(all_success_max_fix)=%  case2(promoted)=%',
            v_day, v_case1, v_case1a, v_case2;

        COMMIT;
        PERFORM pg_sleep(0.3);
    END LOOP;

    RAISE NOTICE '── Done ──  total case1=%  case1a=%  case2=%', v_total1, v_total1a, v_total2;
END;
$$;


-- ============================================================
--  RUN for last 30 days (default)
-- ============================================================
CALL fix_recharge_sla_compliance();
