-- pma_lead_time_stats.sql  placeholders: {fct_lead_time_samples}, {fct_replacement_events}
-- params: @component_key STRING, @analysis_as_of TIMESTAMP, @exclude_wo_uuids ARRAY<STRING>, @limit (unused)
-- Always returns exactly one row. n=0 -> all stats NULL.
WITH repl_dates AS (
  SELECT replacement_wo_uuid, MIN(replacement_date) AS replacement_date
  FROM {fct_replacement_events}
  GROUP BY replacement_wo_uuid
),
same_key_repl AS (
  SELECT DISTINCT component_key, replacement_wo_uuid AS wo_uuid
  FROM {fct_replacement_events}
),
s AS (
  SELECT
    t.*,
    r.replacement_date,
    k.wo_uuid IS NOT NULL AS precursor_is_same_key_replacement
  FROM {fct_lead_time_samples} t
  JOIN repl_dates r ON r.replacement_wo_uuid = t.replacement_wo_uuid
  LEFT JOIN same_key_repl k
    ON k.component_key = t.component_key AND k.wo_uuid = t.precursor_wo_uuid
  WHERE t.component_key = @component_key
    AND r.replacement_date < DATE(@analysis_as_of)
    AND t.replacement_wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
    AND t.precursor_wo_uuid  NOT IN UNNEST(@exclude_wo_uuids)
),
q AS (
  SELECT
    PERCENTILE_CONT(lead_cycles, 0.5) OVER () AS p50,
    PERCENTILE_CONT(lead_cycles, 0.9) OVER () AS p90,
    PERCENTILE_CONT(lead_cycles, 0.95) OVER () AS p95
  FROM s
  LIMIT 1
)
SELECT
  @component_key AS component_key,
  COUNT(s.lead_cycles) AS n,
  COUNT(DISTINCT s.aircraft_reg) AS aircraft_n,
  COUNT(DISTINCT s.replacement_wo_uuid) AS replacement_n,
  MIN(s.lead_cycles) AS lead_min,
  MAX(s.lead_cycles) AS lead_max,
  ANY_VALUE(q.p50) AS lead_p50,
  ANY_VALUE(q.p90) AS lead_p90,
  ANY_VALUE(q.p95) AS lead_p95,
  AVG(s.lead_cycles) AS lead_mean,
  STDDEV_SAMP(s.lead_cycles) AS lead_sd,
  SAFE_DIVIDE(STDDEV_SAMP(s.lead_cycles), AVG(s.lead_cycles)) AS cv,
  SAFE_DIVIDE(COUNTIF(s.precursor_is_same_key_replacement), COUNT(s.lead_cycles)) AS replacement_interval_share,
  COUNTIF(s.precursor_wo_uuid IN (SELECT replacement_wo_uuid FROM s)) AS chained_sample_n,
  ARRAY_AGG(DISTINCT s.replacement_wo_uuid IGNORE NULLS) AS replacement_wo_uuids
FROM s LEFT JOIN q ON TRUE
