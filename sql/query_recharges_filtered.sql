SELECT
    meter_number,
    account_id,
    transaction_id,
    amount,
    created_at,
    payment_date_time
FROM recharges_data
WHERE created_at    >= %(from_date)s
  AND created_at    <  %(to_date)s
  AND meter_number  = ANY(%(meter_serials)s)
ORDER BY created_at
LIMIT  %(limit)s
OFFSET %(offset)s;
