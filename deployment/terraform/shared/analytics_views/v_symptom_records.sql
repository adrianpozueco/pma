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
  step.sequence_number AS step_sequence_number,
  step_offset,
  step.work_phase,
  step.description,
  step.description_sign,
  step.headline,
  step.state AS step_state,
  step.ts AS step_ts,
  aircraft.type_raw AS aircraft_type_raw,
  aircraft.variant AS aircraft_variant,
  aircraft.full_registration,
  aircraft.registration,
  aircraft.msn,
  ata_chapter,
  ata_code,
  ata_major,
  issue.date AS issue_date,
  issue.ts AS issue_ts,
  issue.tac AS issue_aircraft_cycles,
  envelope.envelope_ts AS available_at,
  COALESCE(issue.ts, TIMESTAMP(issue.date)) AS issue_observation_at,
  CASE
    WHEN envelope.envelope_ts IS NOT NULL THEN 'snapshot_only'
    ELSE 'unknown'
  END AS available_at_status,
  ARRAY_CONCAT(
    IF(NULLIF(step.description, '') IS NULL, ['EMPTY_SYMPTOM_TEXT'], []),
    IF(envelope.envelope_ts IS NULL, ['MISSING_SNAPSHOT_TIME'], []),
    IF(duplicate_source_row, ['DUPLICATE_SOURCE_ROW'], []),
    IF(conflicting_workorder_identity, ['CONFLICTING_WORKORDER_IDENTITY'], [])
  ) AS quality_flags
FROM source_rows
CROSS JOIN UNNEST(work_steps) AS step WITH OFFSET AS step_offset;
