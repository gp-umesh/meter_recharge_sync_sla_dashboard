SELECT id
FROM command_info
WHERE name = %(command_name)s
  AND communication_protocol_id = %(protocol_id)s
  AND is_active = true
ORDER BY id
LIMIT 1;
