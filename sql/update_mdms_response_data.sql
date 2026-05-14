UPDATE cmd_exec_response_data
SET "responseData" = %(response_data)s,
    "createdAt"    = %(end_time)s,
    "updatedAt"    = %(end_time)s
WHERE "executionId" = %(execution_id)s;
