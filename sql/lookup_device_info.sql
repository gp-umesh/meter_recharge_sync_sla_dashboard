SELECT
    id                          AS device_info_id,
    device_identifier,
    communication_protocol_id
FROM device_info
WHERE device_serial = %(meter_serial)s
LIMIT 1;
