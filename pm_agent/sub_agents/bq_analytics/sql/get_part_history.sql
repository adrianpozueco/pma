-- get_part_history: dated AMOS installation history for one part number
-- across every work order, before a cutoff timestamp. Own-aircraft vs.
-- comparable-fleet tagging and own-work-order exclusion are applied by the
-- Python caller (adapters.get_part_history) after fetch: the shared
-- QueryRunner (queries.py) binds only scalar parameters, so an exclusion set
-- of arbitrary size cannot be pushed into this fixed template.
--
-- Reuses the exact UNNEST chain and part-number matching pattern from
-- get_component_changes.sql (work_steps -> actions -> component_changes,
-- REGEXP_REPLACE normalization on both sides of the swap) so component
-- changes are never cross-produced against sibling arrays.
--
-- `event_ts` is also the "future record" boundary: @before_ts (normally
-- request.analysis_as_of) excludes any change performed at or after the
-- cutoff, so no post-cutoff record can leak into descriptive evidence.
--
-- `{workorders_table}` is substituted by the runner from a code-owned
-- allowlist. @part_number, @before_ts and @limit are real BigQuery query
-- parameters; @family and @position are nullable. @part_number is
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
  aircraft.type_raw AS aircraft_type_raw,
  aircraft.variant AS aircraft_variant,
  aircraft.full_registration AS aircraft_full_registration,
  aircraft.registration AS aircraft_registration,
  aircraft.msn AS aircraft_msn,
  change.uuid AS component_change_uuid,
  change.position AS position,
  NULLIF(change.part_off_number, '') AS part_off_number,
  NULLIF(change.part_off_serial, '') AS part_off_serial,
  NULLIF(change.part_on_number, '') AS part_on_number,
  NULLIF(change.part_on_serial, '') AS part_on_serial,
  change.part_key AS part_key,
  action.action_type AS action_type,
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
  AND COALESCE(action.performed_ts, step.ts) < @before_ts
  AND (@position IS NULL OR change.position = @position)
ORDER BY event_ts DESC
LIMIT @limit;
