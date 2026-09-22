-- get_workorder: header and recorded top-level descriptions for one work
-- order, with source provenance. No array is unnested here, so this query
-- can never inflate a work-order count with component-change or action rows.
--
-- `{workorders_table}` is substituted by the runner from a code-owned
-- allowlist (pm_agent.sub_agents.bq_analytics.queries.WORKORDERS_TABLE);
-- @workorder_number and @limit are real BigQuery query parameters bound by
-- the caller, never interpolated into this text.
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
  aircraft.full_registration AS aircraft_full_registration,
  aircraft.registration AS aircraft_registration,
  aircraft.msn AS aircraft_msn,
  aircraft.operator AS aircraft_operator,
  issue.date AS issue_date,
  issue.tac AS issue_aircraft_cycles,
  closing.date AS closing_date,
  closing.total_aircraft_cycles AS closing_aircraft_cycles
FROM raw_rows
ORDER BY source_row_hash
LIMIT @limit;
