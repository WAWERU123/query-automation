from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os
import smtplib
import pandas as pd
import psycopg2

# 1. Fetch credentials from GitHub Secrets
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_NAME = os.environ.get('DB_NAME')

SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD')
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL')

# 2. SQL Query
sql_query = """
WITH products_per_jobcard AS (
  SELECT
    rp.repair_id,
    STRING_AGG(
      pp.name || ' x' || rp.quantity || ' @ ' || rp.unit_price || 
      ' [' || CASE WHEN rp.billable = true THEN 'BILLABLE' ELSE 'NON-BILLABLE' END || ']',
      '; '
      ORDER BY pp.name
    ) AS products_used,
    COUNT(*) AS total_parts_count,
    COUNT(*) FILTER (WHERE rp.billable = true) AS billable_parts_count,
    COUNT(*) FILTER (WHERE rp.billable = false OR rp.billable IS NULL) AS non_billable_parts_count
  FROM maintenance_repairproduct rp
  JOIN prices_productprice pp ON pp.id = rp.product_id
  GROUP BY rp.repair_id
),

technicians_per_jobcard AS (
  SELECT
    t.repair_id,
    STRING_AGG(
      u.first_name || ' ' || u.last_name,
      ', '
      ORDER BY u.first_name, u.last_name
    ) AS technicians
  FROM maintenances_jobcard_technicians t
  JOIN public.users_user u ON u.id = t.user_id
  GROUP BY t.repair_id
),

tasks_per_jobcard AS (
  SELECT 
    jc.id AS jobcard_id,
    STRING_AGG(t.x->>'description', '; ') AS itemized_task_descriptions,
    SUM((t.x->>'labor')::numeric) AS total_itemized_labor_hours
  FROM maintenances_jobcard jc
  CROSS JOIN LATERAL (
    SELECT jsonb_array_elements(
      CASE 
        WHEN jc.tasks IS NULL OR jc.tasks::text IN ('', '[]', '"{}"') THEN '[]'::jsonb
        WHEN pg_typeof(jc.tasks) = 'jsonb'::regtype THEN jc.tasks
        ELSE jc.tasks::jsonb 
      END
    ) AS x
  ) t
  GROUP BY jc.id
)

SELECT
  op.name                                         AS "Operator Name",
  v.registration_number                           AS "Reg Number",
  jc.jobcard_number                               AS "Job Card No.",
  i.periskope_ticket_id                           AS "Periskope Ticket ID",
  jc.odoo_repair_order_id                         AS "Odoo Repair Order ID",
  jc.date_created::date                           AS "Job Card Date",
  i.damage_description                            AS "Damage Description",
  jc.initial_diagnosis                            AS "Initial Diagnosis",  
  COALESCE(tk.itemized_task_descriptions, '')     AS "Task Descriptions",
  tk.total_itemized_labor_hours                   AS "Task Labor Hours",
  jc.status                                       AS "Job Status",
  
  CASE 
    WHEN jc.billable = false THEN 'non_billable'
    WHEN p.billable_parts_count > 0 AND p.non_billable_parts_count > 0 THEN 'partially_billable'
    WHEN p.billable_parts_count > 0 AND p.non_billable_parts_count = 0 THEN 'billable'
    WHEN p.total_parts_count > 0 AND p.billable_parts_count = 0 THEN 'non_billable'
    WHEN jc.billable = true THEN 'billable'
    ELSE 'non_billable'
  END                                             AS "Billable Status",

  inv.date_created::date                          AS "Invoice Date", 
  inv.invoice_id                                  AS "Invoice ID",  
  jc.labor_cost                                   AS "Total Labor Cost",
  jc.total_amount                                 AS "Total Cost",
  COALESCE(p.products_used, '')                   AS "Products Used",
  COALESCE(t.technicians, '')                     AS "Technicians"

FROM maintenances_jobcard jc
JOIN maintenance_incident i ON jc.incident_id = i.id
JOIN vehicles_vehicle v ON i.vehicle_id = v.id
JOIN operators_operator op ON v.operator_id = op.id 
LEFT JOIN invoices_repairinvoice inv ON jc.id = inv.repair_id 
LEFT JOIN products_per_jobcard p ON p.repair_id = jc.id
LEFT JOIN technicians_per_jobcard t ON t.repair_id = jc.id
LEFT JOIN tasks_per_jobcard tk ON tk.jobcard_id = jc.id

WHERE 
  v.registration_number ILIKE 'K%'
  AND jc.date_created >= '2026-08-31 00:00:00' 
  AND jc.date_created <= current_timestamp
  AND jc.deleted = false

ORDER BY 
  op.name ASC, 
  jc.date_created DESC;
"""

# 3. Connect to Database & Run Query
csv_filename = 'jobcard_report.csv'
try:
  conn = psycopg2.connect(
      host=DB_HOST,
      port=DB_PORT,
      user=DB_USER,
      password=DB_PASSWORD,
      dbname=DB_NAME,
  )
  df = pd.read_sql_query(sql_query, conn)
  conn.close()

  df.to_csv(csv_filename, index=False)
  print('CSV successfully created.')

except Exception as e:
  print(f'Database Error: {e}')
  raise e

# 4. Email the CSV File
msg = MIMEMultipart()
msg['From'] = SENDER_EMAIL
msg['To'] = RECIPIENT_EMAIL
msg['Subject'] = 'Scheduled Maintenance Job Card Report'

body = (
    'Hello,\n\nPlease find attached the automated maintenance job card report.'
    ' \n\nBest regards,'
)
msg.attach(MIMEText(body, 'plain'))

try:
  with open(csv_filename, 'rb') as attachment:
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(attachment.read())
    encoders.encode_base64(part)
    part.add_header(
        'Content-Disposition', f'attachment; filename= {csv_filename}'
    )
    msg.attach(part)

  server = smtplib.SMTP('smtp.gmail.com', 587)
  server.starttls()
  server.login(SENDER_EMAIL, SENDER_PASSWORD)
  server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
  server.quit()
  print('Email sent successfully!')

except Exception as e:
  print(f'Email Error: {e}')
  raise e
