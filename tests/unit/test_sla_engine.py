from datetime import datetime, timezone, timedelta

import pytest

from src.sla_engine import resolve_sync_timestamp, compute_sla_record

IST = timezone(timedelta(hours=5, minutes=30))


def ts(minute: int) -> datetime:
    base = datetime(2026, 5, 11, 8, 0, 0, tzinfo=IST)
    return base + timedelta(minutes=minute)


def make_cmd(name: str, status: str, end_minute: int | None) -> dict:
    return {
        "commandName": name,
        "executionStatus": status,
        "mdm_end": ts(end_minute) if end_minute is not None else None,
        "mdm_start": ts(0),
        "hes_execution_id": "999",
    }


FIVE_CMDS = [
    "US SET CURRENT BALANCE AMOUNT",
    "US SET CURRENT BALANCE TIME",
    "US SET LAST RECHARGE TOTAL AMOUNT",
    "US SET LAST TOKEN RECHARGE AMOUNT",
    "US SET LAST TOKEN RECHARGE TIME",
]


def all_success(end_minutes: list[int]) -> list[dict]:
    return [make_cmd(n, "SUCCESS", m) for n, m in zip(FIVE_CMDS, end_minutes)]


class TestResolveSync:
    def test_all_success_returns_max_end_time(self):
        cmds = all_success([5, 10, 15, 20, 25])
        resolved, reason = resolve_sync_timestamp(cmds)
        assert reason == "OK"
        assert resolved == ts(25)

    def test_partial_success_returns_min_of_successes(self):
        cmds = [
            make_cmd(FIVE_CMDS[0], "SUCCESS", 10),
            make_cmd(FIVE_CMDS[1], "FAILED", None),
            make_cmd(FIVE_CMDS[2], "SUCCESS", 20),
            make_cmd(FIVE_CMDS[3], "FAILED", None),
            make_cmd(FIVE_CMDS[4], "SUCCESS", 15),
        ]
        resolved, reason = resolve_sync_timestamp(cmds)
        assert reason == "PARTIAL_SUCCESS"
        assert resolved == ts(10)

    def test_all_failed_returns_none(self):
        cmds = [make_cmd(n, "FAILED", None) for n in FIVE_CMDS]
        resolved, reason = resolve_sync_timestamp(cmds)
        assert reason == "ALL_FAILED"
        assert resolved is None

    def test_null_end_time_treated_as_not_success(self):
        cmds = all_success([5, 10, 15, 20, 25])
        cmds[2]["mdm_end"] = None  # one command has null end time
        resolved, reason = resolve_sync_timestamp(cmds)
        assert reason == "PARTIAL_SUCCESS"

    def test_fewer_than_5_commands_is_partial(self):
        cmds = [make_cmd(FIVE_CMDS[0], "SUCCESS", 10)]
        resolved, reason = resolve_sync_timestamp(cmds)
        assert reason == "PARTIAL_SUCCESS"
        assert resolved == ts(10)

    def test_empty_commands_is_all_failed(self):
        resolved, reason = resolve_sync_timestamp([])
        assert reason == "ALL_FAILED"
        assert resolved is None


class TestComputeSlaRecord:
    def _recharge(self) -> dict:
        return {
            "transaction_id": "TXN001",
            "meter_number": "MTR123",
            "account_id": "ACC456",
            "amount": 500,
            "created_at": ts(0),
        }

    def test_within_30min_sla(self):
        cmds = all_success([5, 10, 15, 20, 25])
        record = compute_sla_record(self._recharge(), cmds, {})
        assert record["sla_30min_met"] is True
        assert record["sla_60min_met"] is True
        assert record["breach_reason"] == "OK"

    def test_exceeds_30min_within_60min(self):
        cmds = all_success([31, 32, 33, 34, 35])
        record = compute_sla_record(self._recharge(), cmds, {})
        assert record["sla_30min_met"] is False
        assert record["sla_60min_met"] is True

    def test_exceeds_60min(self):
        cmds = all_success([61, 62, 63, 64, 65])
        record = compute_sla_record(self._recharge(), cmds, {})
        assert record["sla_30min_met"] is False
        assert record["sla_60min_met"] is False

    def test_all_failed_marks_both_sla_false(self):
        cmds = [make_cmd(n, "FAILED", None) for n in FIVE_CMDS]
        record = compute_sla_record(self._recharge(), cmds, {})
        assert record["sla_30min_met"] is False
        assert record["sla_60min_met"] is False
        assert record["elapsed_seconds"] is None

    def test_balance_cmd_status_extracted(self):
        cmds = all_success([5, 10, 15, 20, 25])
        record = compute_sla_record(self._recharge(), cmds, {})
        assert record["balance_cmd_status"] == "SUCCESS"

    def test_missing_hes_records_handled(self):
        cmds = all_success([5, 10, 15, 20, 25])
        record = compute_sla_record(self._recharge(), cmds, {})
        for cmd in record["commands"]:
            assert cmd["hes_start_time"] is None
            assert cmd["hes_status"] is None
