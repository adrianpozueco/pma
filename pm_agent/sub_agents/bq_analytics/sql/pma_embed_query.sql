-- pma_embed_query.sql  placeholders: {embedding_endpoint} (allowlisted constant); params: @wo_text, @limit (unused)
WITH e AS (SELECT AI.EMBED(@wo_text, endpoint => '{embedding_endpoint}') AS v)
SELECT e.v.result AS embedding, e.v.status AS status
FROM e
