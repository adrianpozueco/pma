-- pma_wo_neighbours.sql  placeholders: {wo_embeddings}, {wo_workorders}
-- params: @query_embedding ARRAY<FLOAT64>, @exclude_wo_uuids ARRAY<STRING>, @analysis_as_of TIMESTAMP, @min_sim FLOAT64, @limit INT64
WITH scored AS (
  SELECT
    e.wo_uuid, e.wo_id, e.aircraft_reg, e.ata_chapter, e.tac,
    w.closing.date AS closing_date,
    SUBSTR(e.content, 1, 400) AS snippet,
    1 - ML.DISTANCE(e.embedding.result, @query_embedding, 'COSINE') AS sim
  FROM {wo_embeddings} e
  JOIN {wo_workorders} w ON w.workorder_uuid = e.wo_uuid
  WHERE e.embedding.status = ''
    AND e.wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
    AND COALESCE(w.closing.ts, TIMESTAMP(w.closing.date)) < @analysis_as_of
)
SELECT * FROM scored
WHERE sim >= @min_sim
ORDER BY sim DESC
LIMIT @limit
