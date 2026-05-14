UPDATE command_execution_responses
SET response_data        = %(response_data)s,
    packet_arrival_time  = %(end_time)s,
    created_at           = %(end_time)s,
    updated_at           = %(end_time)s
WHERE execution_id = %(execution_id)s;
