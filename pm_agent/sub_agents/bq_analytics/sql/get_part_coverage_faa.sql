-- get_part_coverage_faa: FAA report rows matching one part number within a
-- date range, optionally scoped to one aircraft model/family. Feeds
-- get_part_coverage's third, independent counting unit. This table
-- (faa_sdr_wo_parts) carries no work-order linkage, so AMOS own-work-order
-- exclusion never applies to it - FAA reports stay a separate counting unit
-- from AMOS work orders and component changes, never merged into one number.
--
-- `match_key` was computed once at ETL time by
-- scripts/build_faa_sdr_wo_parts.py using the same normalization as
-- amos_data.parser.part_key (strip non-alphanumeric, uppercase), so it is
-- compared directly against @part_number - no per-query REGEXP_REPLACE is
-- needed the way get_part_history.sql needs it for wo_workorders' raw
-- part-number strings.
--
-- Calendar-date filtering here is a plain count window, not a claim about
-- when a report was actually publicly available; that stronger availability
-- check belongs to find_faa_reports (via BQHistoryProvider), which this raw
-- table does not carry the availability_status/available_at columns to
-- support directly. Submission/difficulty dates alone are not treated as
-- proof of historical publication availability anywhere else in this module.
--
-- `{faa_table}` is substituted by the runner from a code-owned allowlist.
-- @part_number, @range_start, @range_end and @limit are real BigQuery query
-- parameters; @family is nullable and matched against the free-text
-- AircraftModel column on a best-effort basis (FAA and AMOS family
-- vocabularies are not guaranteed to align).
SELECT DISTINCT
  f.OperatorControlNumber AS report_id,
  f.match_field AS match_field,
  f.AircraftModel AS aircraft_model,
  f.PartCondition AS part_condition,
  f.SubmissionDate AS submission_date,
  f.DifficultyDate AS difficulty_date
FROM {faa_table} AS f
WHERE
  f.match_key = @part_number
  AND (@family IS NULL OR NULLIF(f.AircraftModel, '') IS NULL OR f.AircraftModel = @family)
  AND (
    (f.SubmissionDate IS NOT NULL AND f.SubmissionDate BETWEEN @range_start AND @range_end)
    OR (
      f.SubmissionDate IS NULL AND f.DifficultyDate IS NOT NULL
      AND TIMESTAMP(f.DifficultyDate) BETWEEN @range_start AND @range_end
    )
  )
LIMIT @limit;
