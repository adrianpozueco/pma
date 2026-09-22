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
  workorder_type,
  workorder_state,
  workorder_origin_type,
  origin_workorder_of_finding_uuid,
  external_workorder_number,
  ata_chapter,
  ata_code,
  ata_major,
  remarks,
  aircraft.type_raw AS aircraft_type_raw,
  aircraft.variant AS aircraft_variant,
  aircraft.full_registration,
  aircraft.registration,
  aircraft.reg_country_prefix,
  aircraft.aoc,
  aircraft.msn,
  aircraft.operator,
  issue.date AS issue_date,
  issue.ts AS issue_ts,
  issue.tac AS issue_aircraft_cycles,
  issue.tah_hours AS issue_aircraft_hours,
  closing.date AS closing_date,
  closing.ts AS closing_ts,
  closing.total_aircraft_cycles AS closing_aircraft_cycles,
  closing.total_aircraft_hours_minutes AS closing_aircraft_hours_minutes,
  envelope.envelope_ts AS source_envelope_ts,
  envelope.amos_version AS source_amos_version,
  envelope.transfer_workorder_version AS source_transfer_workorder_version,
  IFNULL(ARRAY_LENGTH(work_steps), 0) > 0 AS has_work_steps,
  IFNULL(ARRAY_LENGTH(ARRAY(
    SELECT change.uuid
    FROM UNNEST(work_steps) AS step
    CROSS JOIN UNNEST(step.actions) AS action
    CROSS JOIN UNNEST(action.component_changes) AS change
  )), 0) > 0 AS has_component_changes,
  issue.tac IS NULL AS missing_issue_counter,
  closing.total_aircraft_cycles IS NULL AS missing_closing_counter,
  issue.tac IS NOT NULL AND closing.total_aircraft_cycles IS NOT NULL
    AND closing.total_aircraft_cycles < issue.tac AS counter_regression,
  duplicate_source_row,
  CASE
    WHEN closing.total_aircraft_cycles IS NOT NULL THEN 'closing.total_aircraft_cycles'
    WHEN issue.tac IS NOT NULL THEN 'issue.tac'
    ELSE 'unavailable'
  END AS counter_basis,
  CASE
    WHEN NULLIF(workorder_uuid, '') IS NOT NULL THEN 'workorder_uuid'
    WHEN NULLIF(workorder_number, '') IS NOT NULL THEN 'workorder_number'
    ELSE 'source_row_hash'
  END AS workorder_id_basis,
  ARRAY_CONCAT(
    IF(NULLIF(workorder_uuid, '') IS NULL AND NULLIF(workorder_number, '') IS NULL,
      ['MISSING_WORKORDER_ID'], []),
    IF(issue.tac IS NULL, ['MISSING_ISSUE_COUNTER'], []),
    IF(closing.total_aircraft_cycles IS NULL, ['MISSING_CLOSING_COUNTER'], []),
    IF(issue.tac IS NOT NULL AND closing.total_aircraft_cycles IS NOT NULL
      AND closing.total_aircraft_cycles < issue.tac, ['COUNTER_REGRESSION'], []),
    IF(duplicate_source_row, ['DUPLICATE_SOURCE_ROW'], []),
    IF(conflicting_workorder_identity, ['CONFLICTING_WORKORDER_IDENTITY'], [])
  ) AS quality_flags
FROM source_rows;
