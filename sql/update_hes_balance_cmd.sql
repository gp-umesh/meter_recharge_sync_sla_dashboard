UPDATE command_execution_info
SET
    start_time       = %(start_time)s,
    update_time      = %(end_time)s,
    execution_status = %(status)s
WHERE execution_id = %(execution_id)s;
