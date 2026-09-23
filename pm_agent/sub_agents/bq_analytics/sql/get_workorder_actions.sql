-- get_workorder_actions: recorded action text and times for one work order.
--
-- This UNNESTs exactly one parent-child chain (work_steps -> actions) and
-- stops there: it deliberately does NOT also UNNEST actions.component_changes,
-- which would multiply each action row by however many component changes it
-- recorded (a cross product). Component changes have their own separate query
-- (get_component_changes.sql) so action counts and component-change counts
-- are never merged into one number.
--
-- Completed actions are historical evidence, not pre-event symptom features;
-- callers must not feed action_text back in as a "symptom" for this same work
-- order.
--
-- `{workorders_table}` is substituted by the runner from a code-owned
-- allowlist; @workorder_number and @limit are real BigQuery query parameters.
WITH raw_rows AS (
  SELECT
    w.*,
    COALESCE(
      NULLIF(w.workorder_uuid, ''),
      NULLIF(w.workorder_number, ''),
      TO_HEX(SHA256(TO_JSON_STRING(w)))
    ) AS workorder_id,
    TO_HEX(SHA256(CONCAT(COALESCE(w.source_file, ''), '|', TO_JSON_STRING(w))))
      AS source_row_hash
  FROM {workorders_table} AS w
  WHERE w.workorder_number = @workorder_number
)
SELECT DISTINCT
  workorder_id,
  workorder_number,
  source_row_hash,
  step_offset,
  step.uuid AS step_uuid,
  action_offset,
  action.uuid AS action_uuid,
  action.action_type AS action_type,
  action.action_text AS action_text,
  action.closing_action AS closing_action,
  COALESCE(action.performed_ts, step.ts) AS event_ts,
  CASE
    WHEN action.performed_ts IS NOT NULL THEN 'action.performed_ts'
    WHEN step.ts IS NOT NULL THEN 'work_step.ts'
    ELSE 'unavailable'
  END AS event_ts_basis
FROM raw_rows
CROSS JOIN UNNEST(work_steps) AS step WITH OFFSET AS step_offset
CROSS JOIN UNNEST(step.actions) AS action WITH OFFSET AS action_offset
ORDER BY event_ts, step_offset, action_offset
LIMIT @limit;
