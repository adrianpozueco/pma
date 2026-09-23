WITH raw_rows AS (
  SELECT
    w.*,
    COALESCE(NULLIF(w.workorder_uuid, ''), NULLIF(w.workorder_number, ''),
      TO_HEX(SHA256(TO_JSON_STRING(w)))) AS workorder_id,
    TO_HEX(SHA256(CONCAT(COALESCE(w.source_file, ''), '|', TO_JSON_STRING(w)))) AS source_row_hash
  FROM `${project_id}.${dataset_id}.${wo_table}` AS w
), source_rows AS (
  SELECT raw_rows.*, COUNT(*) OVER (PARTITION BY source_row_hash) > 1 AS duplicate_source_row,
    COUNT(DISTINCT source_row_hash) OVER (PARTITION BY workorder_id) > 1 AS conflicting_workorder_identity
  FROM raw_rows
)
SELECT
  source_file,
  source_row_hash,
  workorder_id,
  workorder_uuid,
  workorder_number,
  TO_HEX(SHA256(CONCAT(COALESCE(source_file, ''), '|', source_row_hash, '|', workorder_id,
    '|step|', CAST(step_offset AS STRING)))) AS step_id,
  step.uuid AS step_uuid,
  step_offset,
  TO_HEX(SHA256(CONCAT(COALESCE(source_file, ''), '|', source_row_hash, '|', workorder_id,
    '|step|', CAST(step_offset AS STRING), '|action|', CAST(action_offset AS STRING)))) AS action_id,
  action.uuid AS action_uuid,
  action_offset,
  TO_HEX(SHA256(CONCAT(COALESCE(source_file, ''), '|', source_row_hash, '|', workorder_id,
    '|step|', CAST(step_offset AS STRING), '|action|', CAST(action_offset AS STRING),
    '|change|', CAST(change_offset AS STRING)))) AS component_change_id,
  change.uuid AS component_change_uuid,
  change_offset,
  change.position,
  change.label_number,
  change.certificate_number,
  NULLIF(change.part_off_number, '') AS part_off_number,
  NULLIF(change.part_off_serial, '') AS part_off_serial,
  NULLIF(change.part_on_number, '') AS part_on_number,
  NULLIF(change.part_on_serial, '') AS part_on_serial,
  change.part_key,
  change.like_for_like,
  action.action_type,
  action.action_text,
  action.closing_action,
  COALESCE(action.performed_ts, step.ts) AS event_ts,
  CASE
    WHEN action.performed_ts IS NOT NULL THEN 'action.performed_ts'
    WHEN step.ts IS NOT NULL THEN 'work_step.ts'
    ELSE 'unavailable'
  END AS event_ts_basis,
  closing.total_aircraft_cycles AS closing_aircraft_cycles,
  closing.total_aircraft_hours_minutes AS closing_aircraft_hours_minutes,
  issue.tac AS issue_aircraft_cycles,
  CASE
    WHEN closing.total_aircraft_cycles IS NOT NULL THEN 'closing.total_aircraft_cycles'
    WHEN issue.tac IS NOT NULL THEN 'issue.tac'
    ELSE 'unavailable'
  END AS counter_basis,
  aircraft.type_raw AS aircraft_type_raw,
  aircraft.variant AS aircraft_variant,
  aircraft.full_registration,
  aircraft.registration,
  aircraft.msn,
  ata_chapter,
  ata_code,
  ata_major,
  change.part_off_number IS NOT NULL AND NULLIF(change.part_off_number, '') IS NOT NULL AS has_outgoing_part,
  change.part_on_number IS NOT NULL AND NULLIF(change.part_on_number, '') IS NOT NULL AS has_incoming_part,
  closing.total_aircraft_cycles IS NULL AS missing_closing_counter,
  COALESCE(issue.tac IS NOT NULL AND closing.total_aircraft_cycles IS NOT NULL
    AND closing.total_aircraft_cycles < issue.tac, FALSE) AS counter_regression,
  ARRAY_CONCAT(
    IF(NULLIF(change.part_off_number, '') IS NULL AND NULLIF(change.part_on_number, '') IS NULL,
      ['MISSING_PART_SIDES'], []),
    IF(closing.total_aircraft_cycles IS NULL, ['MISSING_CLOSING_COUNTER'], []),
    IF(issue.tac IS NOT NULL AND closing.total_aircraft_cycles IS NOT NULL
      AND closing.total_aircraft_cycles < issue.tac, ['COUNTER_REGRESSION'], []),
    IF(duplicate_source_row, ['DUPLICATE_SOURCE_ROW'], []),
    IF(conflicting_workorder_identity, ['CONFLICTING_WORKORDER_IDENTITY'], [])
  ) AS quality_flags
FROM source_rows
CROSS JOIN UNNEST(work_steps) AS step WITH OFFSET AS step_offset
CROSS JOIN UNNEST(step.actions) AS action WITH OFFSET AS action_offset
CROSS JOIN UNNEST(action.component_changes) AS change WITH OFFSET AS change_offset;
