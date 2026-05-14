UPDATE cmd_exec_info
SET
    "executionStartTime" = %(start_time)s,
    "executionEndTime"   = %(end_time)s,
    "executionStatus"    = 'SUCCESS'
WHERE "executionId"  = %(execution_id)s
  AND "commandName"  = 'US SET CURRENT BALANCE AMOUNT';
