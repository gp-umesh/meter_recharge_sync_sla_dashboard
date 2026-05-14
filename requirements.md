i have to make a dashboard to show SLA of recharge sync commands from MDMS to hes to meter. 
i have three databases 
db_prepaid_engine  contains recharge history in table recharges_data 
select meter_number,account_id,transaction_id,amount,created_at,payment_date_time from recharges_data where created_at between date1 and date2
once recharge receives on prepaid engine it fires 5 command to mdms_cmd_execution 
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
  WHERE "createdAt" > '${from_date} 00:00:00+05:30' and "createdAt" <> '${to_date} 00:00:00+05:30'
    AND "commandName" IN (
      'US SET CURRENT BALANCE AMOUNT',
      'US SET CURRENT BALANCE TIME',
      'US SET LAST RECHARGE TOTAL AMOUNT',
      'US SET LAST TOKEN RECHARGE AMOUNT',
      'US SET LAST TOKEN RECHARGE TIME'
    )
from db_cmd_exec database of mdms 
once mdms receives these command it send command to HES 
and routing_service HES 
query 

select execution_id,execution_status,update_time,start_time from command_execution_info where execution_id  in ('2054075709131599872','2054075706083954688','2054075709194514432','2054075706146869248','2054075711917338624' -- execution_ids  )


SLA dashboard will be following below buisness rules 
15.2	Prepaid Recharge – Meter Update	Update meter via MDM→HES	90% in 30 mins, 99% in 1 hour
 	Criteria to be Added	 	 
 	 	 	 
 	1. Execution Timestamp Handling (Failure Scenario)	 	 
 	Timestamp of HES and MDM execution should be captured.	 	 
 	If any one of the 5 initiated commands completes first while the remaining 4 commands fail, then the end execution time of the first completed command should be considered as the final execution timestamp.	 	 
 	 	 	 
 	2. Execution Timestamp Handling (Success Scenario)	 	 
 	Timestamp of HES and MDM execution should be captured.	 	 
 	If all the initiated commands are completed successfully, then the latest (last) completion timestamp among all commands should be considered as the final execution timestamp.		

Dashbaord is live at https://bi.analytics.polarisgrids.com/d/um4rpxv/recharge-sync-to-meter?orgId=1&from=now-6h&to=now&timezone=browser&var-from_date=2026-05-11



i want to assure that we are meeting these SLAs 

so if there is low sla i want you to make a script which will run for a given date 
and compute first sla and find out sla breached meters 
we will be making changes to mdms and hes for one command 'US SET CURRENT BALANCE AMOUNT' to make it success in given sla time in hes as well as mdms to ensure these can be look fully auditable and no suspicious thing looking 


this is a demo project data bases 