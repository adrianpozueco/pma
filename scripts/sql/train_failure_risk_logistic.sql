-- Guarded BigQuery ML template. Do not treat this as a validated experiment.
-- Do not run this template until the local
-- feasibility artifact says risk_training_gate.passed = true and reviewed rows
-- have populated the named cohort table. The runner enforces that first gate.
--
-- Required table contract (materialized by the reviewed cohort pipeline):
--   prediction_ts, split, episode_group_id, pn, aircraft_family, position,
--   ata_major, aircraft_tac, compatible_symptom_count, symptom_recency_cycles,
--   failure_within_horizon, label_status, label_matured, training_eligible
-- `failure_within_horizon` is true only for independently reviewed confirmed
-- failures. A false value requires observed event-free component exposure.

ASSERT (
  SELECT COUNT(*) = 0
  FROM `${PROJECT}.${DATASET}.prediction_cohort`
  WHERE training_eligible
    AND (label_status IS NULL OR label_status NOT IN ('positive', 'negative') OR label_matured IS NOT TRUE)
) AS 'Cohort contains unknown/unmatured labels; refusing model training.';

ASSERT (
  SELECT COUNT(*) > 0
  FROM `${PROJECT}.${DATASET}.prediction_cohort`
  WHERE split = 'train' AND training_eligible
) AS 'No eligible training rows.';

ASSERT (
  SELECT COUNT(DISTINCT failure_within_horizon) = 2
  FROM `${PROJECT}.${DATASET}.prediction_cohort`
  WHERE split = 'train' AND training_eligible AND label_matured
    AND label_status IN ('positive', 'negative')
) AS 'Training cohort must contain reviewed positive and negative classes.';

ASSERT (
  SELECT COUNT(DISTINCT CONCAT(CAST(horizon_cycles AS STRING), '|', feature_version, '|', training_cutoff)) = 1
  FROM `${PROJECT}.${DATASET}.prediction_cohort`
  WHERE split = 'train' AND training_eligible AND label_matured
) AS 'Training cohort mixes horizon, feature version, or simulated cutoff.';

CREATE MODEL `${PROJECT}.${DATASET}.failure_risk_logreg_reviewed_v1`
OPTIONS(
  model_type = 'LOGISTIC_REG',
  input_label_cols = ['failure_within_horizon'],
  data_split_method = 'NO_SPLIT',
  enable_global_explain = TRUE
) AS
SELECT
  failure_within_horizon,
  pn,
  aircraft_family,
  position,
  ata_major,
  aircraft_tac,
  compatible_symptom_count,
  symptom_recency_cycles
FROM `${PROJECT}.${DATASET}.prediction_cohort`
WHERE split = 'train'
  AND training_eligible
  AND label_matured
  AND label_status IN ('positive', 'negative');

-- Development and newer-target-final rows are scored separately after threshold
-- selection. Do not use BigQuery ML's random internal split and do not tune on
-- `newer_target_final`.
