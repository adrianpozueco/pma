-- get_component_changes: exact on/off part-number and serial pairs for one
-- work order, optionally filtered to one part number.
--
-- This UNNESTs the full parent-child chain (work_steps -> actions ->
-- component_changes) in one pass, so each component-change row is counted
-- exactly once against the action/step it actually belongs to; it does not
-- separately UNNEST work_steps and component_changes side by side, which is
-- the pattern that would cross-product the counts.
--
-- `position` is copied through verbatim from the source record
-- (component_changes.position, e.g. "#2"). It is never renumbered or mapped
-- to a narrative position - several structured positions can legitimately be
-- identical (e.g. four changes all recorded as "#2").
--
-- `{workorders_table}` is substituted by the runner from a code-owned
-- allowlist; @workorder_number, @part_number (nullable) and @limit are real
-- BigQuery query parameters. @part_number is pre-normalized by the caller
-- (amos_data.parser.part_key) and compared against both sides of the swap
-- after the same normalization, never string-interpolated.
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
  action_offset,
  change_offset,
  change.uuid AS component_change_uuid,
  change.position AS position,
  change.label_number AS label_number,
  change.certificate_number AS certificate_number,
  NULLIF(change.part_off_number, '') AS part_off_number,
  NULLIF(change.part_off_serial, '') AS part_off_serial,
  NULLIF(change.part_on_number, '') AS part_on_number,
  NULLIF(change.part_on_serial, '') AS part_on_serial,
  change.part_key AS part_key,
  change.like_for_like AS like_for_like,
  action.action_type AS action_type,
  COALESCE(action.performed_ts, step.ts) AS event_ts
FROM raw_rows
CROSS JOIN UNNEST(work_steps) AS step WITH OFFSET AS step_offset
CROSS JOIN UNNEST(step.actions) AS action WITH OFFSET AS action_offset
CROSS JOIN UNNEST(action.component_changes) AS change WITH OFFSET AS change_offset
WHERE
  @part_number IS NULL
  OR REGEXP_REPLACE(UPPER(COALESCE(change.part_off_number, '')), r'[^A-Z0-9]', '') = @part_number
  OR REGEXP_REPLACE(UPPER(COALESCE(change.part_on_number, '')), r'[^A-Z0-9]', '') = @part_number
ORDER BY event_ts, step_offset, action_offset, change_offset
LIMIT @limit;
