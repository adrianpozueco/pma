# Project name used for resource naming
project_name = "pma-agent"

# Your Google Cloud project id
project_id = "qwiklabs-asl-04-1726946cb8ab"

# The Google Cloud region you will use to deploy the infrastructure
region = "us-central1"

# Vertex AI Search datastore serving the IPC manual agent. It was created in
# the console, so Terraform adopts it rather than creating it; the id below
# must stay byte-for-byte identical to the live one.
knowledge_base_data_store_id = "ipc-part-numbers_1789998929768"
adopt_existing_data_store    = true

# Leave off for this project: the three PDFs are already indexed under
# console-assigned document ids, so a documents.import with our ids would
# duplicate them.
ingest_ipc_documents = false
