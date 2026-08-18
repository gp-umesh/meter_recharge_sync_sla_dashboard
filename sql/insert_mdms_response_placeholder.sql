INSERT INTO cmd_exec_response_data (
    "executionId", "responseData", "createdAt", "updatedAt"
) VALUES (
    %(execution_id)s, '{}', %(created_at)s, %(created_at)s
);
