SELECT
    execution_id,
    execution_status,
    start_time,
    update_time
FROM command_execution_info
WHERE execution_id = ANY(%(execution_ids)s);
