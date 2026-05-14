CREATE TABLE IF NOT EXISTS sla_results (
    transaction_id       VARCHAR PRIMARY KEY,
    meter_number         VARCHAR,
    account_id           VARCHAR,
    amount               NUMERIC,
    recharge_created_at  TIMESTAMPTZ,
    resolved_sync_ts     TIMESTAMPTZ,
    elapsed_seconds      FLOAT,
    sla_30min_met        BOOLEAN,
    sla_60min_met        BOOLEAN,
    breach_reason        VARCHAR,
    balance_cmd_status   VARCHAR,
    computed_at          TIMESTAMPTZ DEFAULT NOW()
);
