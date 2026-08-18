INSERT INTO command_execution_responses (
    execution_id, packet_index, packet_arrival_time,
    response_data, is_active, created_at, updated_at
) VALUES (
    %(execution_id)s, 0, %(created_at)s,
    '{}', true, %(created_at)s, %(created_at)s
);
