# Project name used for resource naming
project_name = "pma-agent"

# Your Google Cloud project id (rotated from qwiklabs-asl-04-1726946cb8ab,
# which is no longer accessible).
project_id = "qwiklabs-asl-04-2a1ac15646fc"

# The Google Cloud region you will use to deploy the infrastructure
region = "us-central1"

# Fresh project: create a datastore under the default id, do not adopt.
knowledge_base_data_store_id = "ipc-part-numbers"
adopt_existing_data_store    = false

# Skip the bash-based PDF importer during the initial apply. The Windows
# environment can't reliably run the shell script; import the PDFs later,
# or run this apply from WSL/Linux and flip this to true.
ingest_ipc_documents = false

# Curated data: run the real-data BigQuery SQL pipeline instead of the
# Plan B synthetic NDJSON load. The two modes are mutually exclusive.
load_curated_data              = false
run_curated_real_data_pipeline = true
