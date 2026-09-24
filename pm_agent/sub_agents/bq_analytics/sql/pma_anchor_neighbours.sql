-- pma_anchor_neighbours.sql  placeholders: {replacement_anchor_embeddings}, {dim_reference_set}
-- params: @query_embedding ARRAY<FLOAT64>, @exclude_wo_uuids ARRAY<STRING> (never NULL elements), @analysis_as_of TIMESTAMP, @min_sim FLOAT64, @limit INT64
WITH anchors AS (
  SELECT
    a.replacement_wo_uuid,
    ANY_VALUE(a.component_key)           AS component_key,
    ANY_VALUE(a.aircraft_reg)            AS aircraft_reg,
    ANY_VALUE(a.replacement_tac)         AS replacement_tac,
    ANY_VALUE(a.content)                 AS content,
    ANY_VALUE(a.anchor_embedding.result) AS emb
  FROM {replacement_anchor_embeddings} a
  WHERE a.anchor_embedding.status = ''
    AND ARRAY_LENGTH(a.anchor_embedding.result) = 768
    AND a.replacement_wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
  GROUP BY a.replacement_wo_uuid
),
dated AS (
  SELECT replacement_wo_uuid, MIN(replacement_date) AS replacement_date,
         ANY_VALUE(replacement_wo_id) AS replacement_wo_id
  FROM {dim_reference_set}
  GROUP BY replacement_wo_uuid
),
scored AS (
  SELECT
    x.replacement_wo_uuid, d.replacement_wo_id, x.component_key, x.aircraft_reg,
    x.replacement_tac, d.replacement_date, SUBSTR(x.content, 1, 400) AS snippet,
    1 - ML.DISTANCE(x.emb, @query_embedding, 'COSINE') AS sim
  FROM anchors x
  JOIN dated d USING (replacement_wo_uuid)
  WHERE d.replacement_date < DATE(@analysis_as_of)
)
SELECT * FROM scored
WHERE sim >= @min_sim
ORDER BY sim DESC
LIMIT @limit
