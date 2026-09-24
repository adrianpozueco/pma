-- pma_curated_build_info.sql  placeholders: {curated_information_schema_tables}, {curated_information_schema_table_options}; params: @limit (unused)
SELECT t.table_name, t.creation_time, o.option_value AS labels
FROM {curated_information_schema_tables} t
LEFT JOIN {curated_information_schema_table_options} o
  ON o.table_name = t.table_name AND o.option_name = 'labels'
WHERE t.table_name IN ('dim_focus_components','fct_replacement_events','dim_reference_set','replacement_anchor_embeddings','wo_embeddings','adjudicated_precursors','fct_lead_time_samples')
ORDER BY t.table_name
