UPDATE cmd_exec_info
SET "createdAt"          = %(created_at)s,
    "executionStartTime" = %(created_at)s
WHERE "executionId" = %(execution_id)s;
