INSERT INTO command_execution_responses (
    execution_id, packet_index, packet_arrival_time,
    response_data, is_active, created_at, updated_at
) VALUES (
    %(execution_id)s, 1, %(end_time)s,
    %(response_data)s, true, %(end_time)s, %(end_time)s
);
