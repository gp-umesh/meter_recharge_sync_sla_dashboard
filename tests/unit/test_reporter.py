import json
from datetime import datetime, timezone, timedelta

import pytest

from src.reporter import format_breach_list, format_verbose_breach_list, format_summary

IST = timezone(timedelta(hours=5, minutes=30))


def make_record(elapsed: float | None, breach: str, balance: str = "SUCCESS") -> dict:
    created = datetime(2026, 5, 11, 8, 0, 0, tzinfo=IST)
    sla_30 = elapsed is not None and elapsed <= 1800
    sla_60 = elapsed is not None and elapsed <= 3600
    return {
        "transaction_id": "TXN001",
        "meter_number": "MTR001",
        "account_id": "ACC001",
        "amount": 500,
        "recharge_created_at": created,
        "resolved_sync_ts": None,
        "elapsed_seconds": elapsed,
        "sla_30min_met": sla_30,
        "sla_60min_met": sla_60,
        "breach_reason": breach,
        "balance_cmd_status": balance,
        "commands": [
            {
                "commandName": "US SET CURRENT BALANCE AMOUNT",
                "mdm_start": created,
                "mdm_end": created,
                "executionStatus": balance,
                "hes_start_time": None,
                "hes_update_time": None,
                "hes_status": None,
            }
        ],
    }


class TestFormatBreachList:
    def test_csv_has_header(self):
        records = [make_record(None, "ALL_FAILED")]
        output = format_breach_list(records, "csv")
        assert "transaction_id" in output.splitlines()[0]

    def test_csv_has_data_row(self):
        records = [make_record(None, "ALL_FAILED")]
        output = format_breach_list(records, "csv")
        assert "TXN001" in output

    def test_json_is_valid(self):
        records = [make_record(None, "ALL_FAILED")]
        output = format_breach_list(records, "json")
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert parsed[0]["transaction_id"] == "TXN001"

    def test_table_is_readable(self):
        records = [make_record(None, "ALL_FAILED")]
        output = format_breach_list(records, "table")
        assert "TXN001" in output
        assert "transaction_id" in output

    def test_only_breached_records_in_output(self):
        ok_record = make_record(600, "OK")           # 10 min — within SLA
        bad_record = make_record(None, "ALL_FAILED") # breach
        output = format_breach_list([ok_record, bad_record], "csv")
        lines = [l for l in output.splitlines() if l.strip()]
        assert len(lines) == 2  # header + 1 breach row


class TestFormatSummary:
    def test_identifies_sla_pass(self):
        records = [make_record(600, "OK")] * 10  # all within 10 min
        summary = format_summary(records, "2026-05-11")
        assert "OK" in summary

    def test_identifies_sla_fail(self):
        records = [make_record(None, "ALL_FAILED")] * 10
        summary = format_summary(records, "2026-05-11")
        assert "BELOW TARGET" in summary

    def test_contains_totals(self):
        records = [make_record(None, "ALL_FAILED")] * 5
        summary = format_summary(records, "2026-05-11")
        assert "5" in summary

    def test_no_records(self):
        summary = format_summary([], "2026-05-11")
        assert "No recharges" in summary
