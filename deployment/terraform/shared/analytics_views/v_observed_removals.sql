SELECT
  source_file,
  source_row_hash,
  workorder_id,
  workorder_uuid,
  workorder_number,
  step_id,
  action_id,
  component_change_id,
  event_ts,
  event_ts_basis,
  aircraft_type_raw,
  aircraft_variant,
  full_registration,
  registration,
  msn,
  ata_chapter,
  ata_code,
  ata_major,
  position,
  part_off_number AS removed_part_number,
  REGEXP_REPLACE(UPPER(part_off_number), r'[^A-Z0-9]', '') AS removed_part_key,
  part_off_serial AS removed_serial_number,
  part_on_number AS incoming_part_number,
  REGEXP_REPLACE(UPPER(part_on_number), r'[^A-Z0-9]', '') AS incoming_part_key,
  part_on_serial AS incoming_serial_number,
  part_key,
  like_for_like,
  action_type,
  action_text,
  closing_aircraft_cycles,
  issue_aircraft_cycles,
  counter_basis,
  CASE
    WHEN closing_aircraft_cycles IS NULL THEN 'missing_counter'
    WHEN closing_aircraft_cycles < 0 THEN 'invalid_counter'
    WHEN counter_regression THEN 'regressed_counter'
    ELSE 'usable_closing_counter'
  END AS counter_quality,
  CASE
    WHEN REGEXP_CONTAINS(LOWER(COALESCE(action_text, '')), r'\b(scheduled|serviceable|preventive|cannibal)\b')
      THEN 'scheduled_or_serviceable'
    WHEN REGEXP_CONTAINS(LOWER(COALESCE(action_text, '')), r'\b(fail(?:ed|ure)?|defect|fault|leak|inoperative|damage|crack|remove and replace)\b')
      THEN 'defect_or_failure_indication'
    ELSE 'unknown'
  END AS removal_reason_class,
  'heuristic_unreviewed' AS removal_reason_quality,
  CASE
    WHEN closing_aircraft_cycles IS NOT NULL AND closing_aircraft_cycles >= 0 AND NOT counter_regression
      THEN 'candidate_with_counter'
    ELSE 'inventory_only'
  END AS eligibility_status,
  counter_regression,
  missing_closing_counter,
  quality_flags
FROM `${project_id}.${dataset_id}.${component_view}`
WHERE NULLIF(part_off_number, '') IS NOT NULL;
