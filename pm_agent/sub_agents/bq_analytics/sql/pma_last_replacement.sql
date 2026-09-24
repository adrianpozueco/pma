-- pma_last_replacement.sql  placeholders: {fct_replacement_events}, {v_work_orders}
-- params: @component_key STRING, @aircraft_reg STRING, @analysis_as_of TIMESTAMP, @exclude_wo_uuid STRING ('' never NULL), @limit INT64 (pass 1)
-- Option B projected window (approved 2026-09-24): this aircraft's own most
-- recent earlier replacement of the same component_key, used to anchor the
-- fleet-pattern p50/p90 interval forward. Not part of the base plan flow.
-- Cut-off uses the replacement WO's closing timestamp, the same predicate as
-- pma_latest_closing_tac.sql, so a replacement closed earlier on the
-- analysis day is visible to both queries (never to only one of them).
SELECT
  r.replacement_tac,
  r.replacement_date,
  r.replacement_wo_id,
  r.replacement_wo_uuid
FROM {fct_replacement_events} r
LEFT JOIN {v_work_orders} w
  ON w.workorder_uuid = r.replacement_wo_uuid
WHERE r.component_key = @component_key
  AND r.aircraft_reg = @aircraft_reg
  AND r.replacement_tac IS NOT NULL
  AND COALESCE(w.closing_ts, TIMESTAMP(r.replacement_date)) < @analysis_as_of
  AND IFNULL(r.replacement_wo_uuid, '') != @exclude_wo_uuid
ORDER BY COALESCE(w.closing_ts, TIMESTAMP(r.replacement_date)) DESC, r.replacement_tac DESC
LIMIT @limit
