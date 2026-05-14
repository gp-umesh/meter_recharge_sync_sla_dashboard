# Feature Specification: Recharge Sync SLA Dashboard & Correction Script

**Feature Branch**: `001-sla-dashboard-correction`  
**Created**: 2026-05-14  
**Status**: Draft  
**Input**: User description: "i want a dashboard and sla_correction script refer @requirements.md"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - View Real-Time SLA Compliance Dashboard (Priority: P1)

An operations engineer wants to monitor whether the prepaid recharge sync pipeline (from MDMS to HES to Meter) is meeting the agreed SLA targets: 90% of recharges fully synced within 30 minutes, and 99% within 1 hour.

**Why this priority**: Without visibility into SLA compliance, breaches go undetected. This is the core monitoring requirement and must be addressed first.

**Independent Test**: A user can open the dashboard, select a date/time range, and immediately see the current SLA pass/fail rates for 30-minute and 60-minute thresholds — without requiring any other feature to be complete.

**Acceptance Scenarios**:

1. **Given** the dashboard is open and a date range is selected, **When** the page loads, **Then** the dashboard displays the percentage of recharges that completed sync within 30 minutes and within 60 minutes.
2. **Given** there are recharges in the system for the selected period, **When** viewing the SLA panel, **Then** the metrics show whether the 90%/30-min and 99%/60-min thresholds are being met (with pass/fail indication).
3. **Given** a recharge is initiated, **When** all 5 MDMS commands succeed, **Then** the final execution timestamp recorded is the latest completion time among all 5 commands.
4. **Given** a recharge is initiated, **When** at least one of the 5 MDMS commands fails and at least one succeeds, **Then** the final execution timestamp recorded is the completion time of the first successfully completed command.

---

### User Story 2 - Investigate SLA-Breached Meters (Priority: P2)

An operations engineer notices SLA compliance is below target and wants to drill into which specific meters and accounts had their sync breached for a given date.

**Why this priority**: Identifying the exact breached meters allows targeted remediation and auditing. This delivers actionable insight beyond aggregate metrics.

**Independent Test**: Can be tested by running the SLA correction script for a specific date and receiving a list of breached meter numbers, account IDs, and delay durations.

**Acceptance Scenarios**:

1. **Given** a date is provided, **When** the script is run, **Then** it outputs a list of meters where sync did not complete within the SLA window, including meter number, account ID, transaction ID, and time taken.
2. **Given** a meter appears in the breach list, **When** the details are inspected, **Then** the reason for breach is identifiable (e.g., all 5 commands failed, or only partial success).
3. **Given** there are no SLA breaches for the given date, **When** the script is run, **Then** it outputs a message confirming full SLA compliance for that date.

---

### User Story 3 - Audit Command-Level Execution for a Specific Recharge (Priority: P3)

A compliance engineer wants to verify that a specific recharge transaction was properly synced end-to-end across MDMS and HES, with full timestamps and statuses for each of the 5 commands, to ensure nothing looks suspicious.

**Why this priority**: Auditability and traceability are required for compliance. This is less urgent than aggregate monitoring but critical for investigations.

**Independent Test**: Can be tested independently by querying a transaction ID and verifying all 5 command execution records with timestamps and statuses are retrievable.

**Acceptance Scenarios**:

1. **Given** a transaction ID is known, **When** it is looked up, **Then** all 5 associated MDMS command executions are visible with their start time, end time, and status.
2. **Given** an execution ID is available, **When** HES command status is checked, **Then** the HES execution status and timing is displayed alongside the MDMS record.
3. **Given** an audit review is underway, **When** reviewing all records, **Then** no timestamps or statuses are missing or inconsistent.

---

### Edge Cases

- What happens when a recharge has no corresponding MDMS commands (commands never fired)?
- How does the system handle a recharge where all 5 commands fail completely (no completion timestamp available)?
- What happens when HES execution data is missing for an MDMS command?
- How are duplicate or retried commands for the same recharge handled?
- What happens if the script is run for a date with no recharges?
- What if a command's end time is null (still in progress at time of query)?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The dashboard MUST display SLA compliance rates for two thresholds: 90% of recharges synced within 30 minutes, and 99% of recharges synced within 60 minutes, for a user-selected date/time range.
- **FR-002**: The dashboard MUST allow users to filter by date range to view historical SLA performance.
- **FR-003**: The dashboard MUST apply the correct timestamp determination rules: (a) if all 5 commands succeed, use the latest completion time; (b) if any command fails but at least one succeeds, use the completion time of the first successfully completed command.
- **FR-004**: The SLA correction script MUST accept a target date as input and compute SLA compliance for all recharges on that date.
- **FR-005**: The SLA correction script MUST identify and list all meters/accounts where the SLA was breached, including meter number, account ID, transaction ID, amount, and time taken for sync.
- **FR-006**: The script MUST join data from three sources: the recharge history (prepaid engine), MDMS command execution records, and HES routing service execution records.
- **FR-007**: The script MUST specifically focus on the five commands: `US SET CURRENT BALANCE AMOUNT`, `US SET CURRENT BALANCE TIME`, `US SET LAST RECHARGE TOTAL AMOUNT`, `US SET LAST TOKEN RECHARGE AMOUNT`, `US SET LAST TOKEN RECHARGE TIME`.
- **FR-008**: The dashboard MUST display both MDMS and HES execution timestamps for auditability.
- **FR-009**: The script MUST produce output in a structured, readable format (e.g., CSV or tabular) suitable for review and further action.
- **FR-010**: The dashboard MUST show an alert or indicator when SLA thresholds are not being met.

### Key Entities

- **Recharge**: A prepaid meter top-up event with meter number, account ID, transaction ID, amount, and creation timestamp.
- **MDMS Command Execution**: One of 5 commands dispatched to HES per recharge, with execution ID, command name, status, start time, and end time.
- **HES Execution**: The HES-side record of a command's execution, linked by execution ID, with status and timing.
- **SLA Window**: The time elapsed from recharge creation to the final resolved sync timestamp; measured against 30-minute and 60-minute thresholds.
- **Resolved Sync Timestamp**: The computed final execution time per the business rules — latest completion if all succeed; earliest completion of the first success if any fail.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 90% or more of recharges for any given day are fully synced within 30 minutes, as shown on the dashboard.
- **SC-002**: 99% or more of recharges for any given day are fully synced within 60 minutes, as shown on the dashboard.
- **SC-003**: The SLA correction script completes analysis for a full day's recharges in under 5 minutes.
- **SC-004**: The breach list produced by the script is 100% accurate — every meter in the list genuinely breached SLA, and no breached meter is omitted.
- **SC-005**: Operations engineers can identify all SLA-breached meters for any given date within 10 minutes of running the script.
- **SC-006**: The dashboard reflects data no older than 15 minutes from the live databases (near real-time).
- **SC-007**: Every recharge can be traced end-to-end (recharge → 5 MDMS commands → HES executions) with no missing audit records for 100% of investigated transactions.

## Assumptions

- The three databases (prepaid engine, MDMS cmd_exec, HES routing service) are accessible from the environment where the script and dashboard run.
- The `executionId` in MDMS command execution records matches the `execution_id` in HES routing service records (join key between the two systems).
- The `additionalInfo ->> 'accountId'` field in MDMS is reliable for linking MDMS commands back to recharge accounts.
- A recharge always generates exactly 5 commands in MDMS; fewer than 5 commands indicates a failure in command dispatch.
- The dashboard is built on Grafana (already deployed at the provided URL) and will use SQL-based data sources connected to the three databases.
- The SLA correction script is a standalone script (e.g., Python or shell) run manually or on a schedule by operations staff.
- "Sync complete" means the resolved sync timestamp (per business rules) falls within the SLA window from recharge creation.
- Mobile/browser support for the dashboard follows whatever is already configured in the existing Grafana instance.
- The `created_at` field in the prepaid engine is the start of the SLA clock for each recharge.
