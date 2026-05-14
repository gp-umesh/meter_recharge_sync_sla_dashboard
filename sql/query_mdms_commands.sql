SELECT
    "executionId"                          AS hes_execution_id,
    "additionalInfo" ->> 'accountId'       AS account_id,
    "meterSerial",
    "commandName",
    "createdAt"                            AS mdm_created_at,
    "executionStartTime"                   AS mdm_start,
    "executionEndTime"                     AS mdm_end,
    "executionStatus"
FROM cmd_exec_info
WHERE "createdAt" >= %(from_date)s
  AND "createdAt" <  %(to_date)s
  AND "commandName" IN (
      'US SET CURRENT BALANCE AMOUNT',
      'US SET CURRENT BALANCE TIME',
      'US SET LAST RECHARGE TOTAL AMOUNT',
      'US SET LAST TOKEN RECHARGE AMOUNT',
      'US SET LAST TOKEN RECHARGE TIME'
  )
  AND "additionalInfo" ->> 'accountId' = ANY(%(account_ids)s);
