WITH report AS (
  SELECT
    r.*,
    REGEXP_REPLACE(UPPER(COALESCE(PartNumber, '')), r'[^A-Z0-9]', '') AS part_number_key,
    REGEXP_REPLACE(UPPER(COALESCE(ComponentPartNumber, '')), r'[^A-Z0-9]', '') AS component_part_number_key,
    SAFE_CAST(aircraft_total_cycles_value AS INT64) AS aircraft_cycles,
    SAFE_CAST(part_total_cycles_value AS INT64) AS part_cycles,
    SAFE_CAST(component_total_cycles_value AS INT64) AS component_cycles
  FROM `${project_id}.${dataset_id}.${faa_table}` AS r
), evidence AS (
  SELECT
    report.*,
    ARRAY(
      SELECT role
      FROM UNNEST([
        IF(part_number_key IN ('2085M31G03', '62197301001', '820111000001'), 'PartNumber', NULL),
        IF(component_part_number_key IN ('2085M31G03', '62197301001', '820111000001'), 'ComponentPartNumber', NULL)
      ]) AS role
      WHERE role IS NOT NULL
    ) AS matched_pn_roles,
    CASE
      WHEN part_number_key IN ('2085M31G03', '62197301001', '820111000001')
        OR component_part_number_key IN ('2085M31G03', '62197301001', '820111000001') THEN 'exact_pn'
      WHEN REGEXP_CONTAINS(LOWER(CONCAT(' ', COALESCE(PartName, ''), ' ', COALESCE(ComponentName, ''))), r'\b(nozzle|boiler|water heater|oven)\b') THEN 'related_component_name'
      ELSE 'unmatched'
    END AS evidence_tier,
    CASE
      WHEN part_number_key IN ('2085M31G03', '62197301001', '820111000001') THEN 'PartNumber'
      WHEN component_part_number_key IN ('2085M31G03', '62197301001', '820111000001') THEN 'ComponentPartNumber'
      ELSE NULL
    END AS matched_pn_role
  FROM report
)
SELECT
  OperatorControlNumber AS report_id,
  source_year,
  source_file,
  source_row_number,
  source_snapshot_hash,
  DifficultyDate,
  difficulty_date_iso,
  SubmissionDate,
  submission_timestamp_iso,
  AircraftMake,
  AircraftModel,
  AircraftSerialNumber,
  AircraftTotalCycles,
  aircraft_cycles,
  PartName,
  PartNumber AS part_number_raw,
  part_number_key,
  PartSerialNumber,
  PartCondition,
  PartTotalCycles,
  part_cycles,
  ComponentTotalCycles,
  component_cycles,
  ComponentName,
  ComponentPartNumber AS component_part_number_raw,
  component_part_number_key,
  ComponentSerialNumber,
  Discrepancy,
  evidence_tier,
  matched_pn_role,
  matched_pn_roles,
  part_number_key IN ('2085M31G03', '62197301001', '820111000001') AS part_number_exact_match,
  component_part_number_key IN ('2085M31G03', '62197301001', '820111000001') AS component_part_number_exact_match,
  CASE
    WHEN evidence_tier = 'exact_pn' THEN 'unverified_application'
    WHEN evidence_tier = 'related_component_name' THEN 'unreviewed_related_component'
    ELSE 'unknown'
  END AS compatibility_status,
  CASE
    WHEN difficulty_date_valid = 'valid' AND submission_timestamp_valid = 'valid' THEN 'valid_dates'
    WHEN difficulty_date_valid = 'valid' OR submission_timestamp_valid = 'valid' THEN 'partial_dates'
    ELSE 'invalid_or_missing_dates'
  END AS date_quality,
  COALESCE(SAFE_CAST(submission_timestamp_iso AS TIMESTAMP) < TIMESTAMP(SAFE_CAST(difficulty_date_iso AS DATE)), FALSE) AS date_contradiction,
  CASE
    WHEN part_cycles IS NOT NULL THEN 'part_counter_available'
    WHEN component_cycles IS NOT NULL THEN 'component_counter_available'
    WHEN aircraft_cycles IS NOT NULL THEN 'aircraft_counter_only'
    ELSE 'counter_unavailable'
  END AS counter_quality,
  part_total_cycles_parse_status,
  component_total_cycles_parse_status,
  aircraft_total_cycles_parse_status,
  discrepancy IS NULL OR NULLIF(TRIM(discrepancy), '') IS NULL AS missing_narrative,
  OperatorControlNumber IS NULL OR NULLIF(TRIM(OperatorControlNumber), '') IS NULL AS missing_report_id
FROM evidence
WHERE evidence_tier != 'unmatched';
