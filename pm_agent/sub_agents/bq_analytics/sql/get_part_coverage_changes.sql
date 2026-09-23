-- get_part_coverage_changes: AMOS component-change rows for one part number
-- within a date range, optionally scoped to one aircraft family. Feeds two of
-- get_part_coverage's three counting units (distinct work orders, component
-- changes); own-work-order exclusion and case dedup are applied by the
-- Python caller after fetch, same reason as get_part_history.sql (the shared
-- QueryRunner binds only scalar parameters).
--
-- Reuses the same UNNEST chain and matching pattern as
-- get_component_changes.sql / get_part_history.sql.
--
-- `{workorders_table}` is substituted by the runner from a code-owned
-- allowlist. @part_number, @range_start, @range_end and @limit are real
-- BigQuery query parameters; @family is nullable. @part_number is
-- pre-normalized by the caller (amos_data.parser.part_key).
WITH raw_rows AS (
  SELECT
    w.*,
    COALESCE(
      NULLIF(w.workorder_uuid, ''),
      NULLIF(w.workorder_number, ''),
      TO_HEX(SHA256(TO_JSON_STRING(w)))
    ) AS workorder_id
  FROM {workorders_table} AS w
  WHERE @family IS NULL OR NULLIF(w.aircraft.variant, '') IS NULL OR w.aircraft.variant = @family
)
SELECT DISTINCT
  workorder_id,
  workorder_number,
  change.uuid AS component_change_uuid,
  COALESCE(action.performed_ts, step.ts) AS event_ts
FROM raw_rows
CROSS JOIN UNNEST(work_steps) AS step
CROSS JOIN UNNEST(step.actions) AS action
CROSS JOIN UNNEST(action.component_changes) AS change
WHERE
  (
    REGEXP_REPLACE(UPPER(COALESCE(change.part_off_number, '')), r'[^A-Z0-9]', '') = @part_number
    OR REGEXP_REPLACE(UPPER(COALESCE(change.part_on_number, '')), r'[^A-Z0-9]', '') = @part_number
  )
  AND COALESCE(action.performed_ts, step.ts) BETWEEN @range_start AND @range_end
ORDER BY event_ts
LIMIT @limit;
