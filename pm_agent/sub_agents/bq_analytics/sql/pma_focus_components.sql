-- pma_focus_components.sql  placeholders: {dim_focus_components}, {fct_replacement_events}; params: @limit (unused)
WITH aliases AS (
  SELECT r.component_key,
         ARRAY_AGG(DISTINCT REGEXP_REPLACE(UPPER(pn), r'[^A-Z0-9]', '') IGNORE NULLS) AS part_aliases
  FROM {fct_replacement_events} r, UNNEST([r.part_off_pn, r.part_on_pn]) AS pn
  WHERE r.component_key IN (SELECT component_key FROM {dim_focus_components})
    AND pn IS NOT NULL AND TRIM(pn) <> ''
  GROUP BY r.component_key
)
SELECT
  f.component_key,
  SPLIT(f.component_key, '|')[OFFSET(0)] AS part_number,
  SPLIT(f.component_key, '|')[SAFE_OFFSET(1)] AS position,
  REGEXP_REPLACE(UPPER(SPLIT(f.component_key, '|')[OFFSET(0)]), r'[^A-Z0-9]', '') AS part_key,
  IFNULL(a.part_aliases, []) AS part_aliases,
  f.freq_rank, f.replacement_count, f.aircraft_with_replacement
FROM {dim_focus_components} f
LEFT JOIN aliases a USING (component_key)
ORDER BY f.freq_rank
