UPDATE cmd_exec_info
SET "executionId" = %(execution_id)s
WHERE "clientRequestId" = %(client_request_id)s;
