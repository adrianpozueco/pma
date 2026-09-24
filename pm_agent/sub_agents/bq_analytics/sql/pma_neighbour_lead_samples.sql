-- pma_neighbour_lead_samples.sql  placeholders: {fct_lead_time_samples}, {v_work_orders}
-- params: @neighbour_wo_uuids ARRAY<STRING>, @analysis_as_of TIMESTAMP, @exclude_wo_uuid STRING ('' never NULL), @limit INT64
-- Condensed recommendation (approved 2026-09-24): lead_cycles samples whose
-- precursor_wo_uuid is one of the recommendation's own similar-work-order
-- neighbours (pma_wo_neighbours.sql, rec_neighbour_k/rec_neighbour_min_sim),
-- grouped by component_key in Python (policy.aggregate_neighbour_lead_samples)
-- to build the "similar_workorders" basis. Cut-off joins {v_work_orders} on
-- replacement_wo_uuid for closing_ts/closing_date, the same style as
-- pma_last_replacement.sql (fct_lead_time_samples carries no replacement_date
-- of its own), and the uploaded WO's own uuid is excluded whether it appears
-- as a precursor or as a replacement.
SELECT
  t.component_key,
  t.precursor_wo_uuid,
  t.aircraft_reg,
  t.lead_cycles
FROM {fct_lead_time_samples} t
JOIN {v_work_orders} w ON w.workorder_uuid = t.replacement_wo_uuid
WHERE t.precursor_wo_uuid IN UNNEST(@neighbour_wo_uuids)
  AND COALESCE(w.closing_ts, TIMESTAMP(w.closing_date)) < @analysis_as_of
  AND t.replacement_wo_uuid != @exclude_wo_uuid
  AND t.precursor_wo_uuid != @exclude_wo_uuid
LIMIT @limit
