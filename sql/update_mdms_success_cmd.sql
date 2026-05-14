UPDATE cmd_exec_info
SET "executionEndTime" = %(end_time)s,
    "executionStatus"  = 'SUCCESS',
    "remarks"          = 'Due to Recharge Sync, Consumer Balance Sync command sent to meter'
WHERE "executionId" = %(execution_id)s;
