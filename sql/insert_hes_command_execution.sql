INSERT INTO command_execution_info (
    execution_id, execution_status, command_info_id, command_name,
    device_info_id, device_serial, request_id,
    communication_protocol_id, device_identifier, batch_id,
    is_active, created_at, updated_at, remarks
) VALUES (
    %(execution_id)s, 'PENDING', %(command_info_id)s, %(command_name)s,
    %(device_info_id)s, %(device_serial)s, %(request_id)s,
    %(communication_protocol_id)s, %(device_identifier)s, %(batch_id)s,
    true, %(created_at)s, %(created_at)s,
    '{"remark": "Row backfilled by sla_force_correct --create-missing-hes — original command never received a HES execution ID"}'
);
