-- pma_precursor_evidence.sql  placeholders: {fct_lead_time_samples}, {adjudicated_precursors}, {fct_replacement_events}
-- params: @component_key STRING, @analysis_as_of TIMESTAMP, @exclude_wo_uuids ARRAY<STRING>, @neighbour_wo_uuids ARRAY<STRING>, @limit INT64
WITH repl_dates AS (
  SELECT replacement_wo_uuid, MIN(replacement_date) AS replacement_date
  FROM {fct_replacement_events}
  GROUP BY replacement_wo_uuid
),
adj AS (
  SELECT replacement_wo_uuid, precursor_wo_uuid, reason, precursor_text
  FROM {adjudicated_precursors}
  WHERE component_key = @component_key AND verdict = 'symptom'
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY replacement_wo_uuid, precursor_wo_uuid ORDER BY llm_conf DESC, sim DESC) = 1
)
SELECT
  t.component_key, t.aircraft_reg, t.precursor_wo_id, t.precursor_wo_uuid, t.precursor_tac,
  t.replacement_wo_uuid, t.replacement_tac, r.replacement_date, t.lead_cycles, t.sim,
  adj.reason,
  SUBSTR(adj.precursor_text, 1, 400) AS precursor_snippet,
  t.precursor_wo_uuid IN UNNEST(@neighbour_wo_uuids) AS is_neighbour_hit
FROM {fct_lead_time_samples} t
JOIN repl_dates r ON r.replacement_wo_uuid = t.replacement_wo_uuid
LEFT JOIN adj
  ON adj.replacement_wo_uuid = t.replacement_wo_uuid AND adj.precursor_wo_uuid = t.precursor_wo_uuid
WHERE t.component_key = @component_key
  AND r.replacement_date < DATE(@analysis_as_of)
  AND t.replacement_wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
  AND t.precursor_wo_uuid  NOT IN UNNEST(@exclude_wo_uuids)
ORDER BY is_neighbour_hit DESC, t.sim DESC
LIMIT @limit
