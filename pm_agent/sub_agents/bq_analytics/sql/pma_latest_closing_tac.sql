-- pma_latest_closing_tac.sql  placeholders: {v_work_orders}
-- params: @aircraft_reg STRING, @analysis_as_of TIMESTAMP, @exclude_wo_uuid STRING ('' when the upload has no uuid; never NULL), @limit INT64 (pass 1)
WITH w AS (
  SELECT
    workorder_number,
    closing_aircraft_cycles AS tac,
    COALESCE(closing_ts, TIMESTAMP(closing_date)) AS observed_at
  FROM {v_work_orders}
  WHERE full_registration = @aircraft_reg
    AND closing_aircraft_cycles IS NOT NULL
    AND COALESCE(closing_ts, TIMESTAMP(closing_date)) < @analysis_as_of
    AND IFNULL(workorder_uuid, '') != @exclude_wo_uuid
)
SELECT
  workorder_number, tac, observed_at,
  MAX(tac) OVER () AS max_tac_before_cutoff
FROM w
ORDER BY observed_at DESC, tac DESC
LIMIT @limit
