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

# Import the IPC PDFs under data/ipc_part_numbers/ into the datastore. The
# importer is a bash script: run the apply from macOS/Linux/WSL, not native
# Windows. To re-import only the knowledge base after adding PDFs:
#   terraform apply -var-file=vars/env.tfvars -target='terraform_data.ipc_document_import[0]'
ingest_ipc_documents = true

# Curated data: run the real-data BigQuery SQL pipeline instead of the
# Plan B synthetic NDJSON load. The two modes are mutually exclusive.
load_curated_data              = false
run_curated_real_data_pipeline = true
