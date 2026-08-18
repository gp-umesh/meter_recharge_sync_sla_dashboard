INSERT INTO cmd_exec_response_data (
    "executionId", "responseData", "createdAt", "updatedAt"
) VALUES (
    %(execution_id)s, %(response_data)s, %(end_time)s, %(end_time)s
);
