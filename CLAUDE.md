# ChatHealthy — Session Startup Protocol

## Step 1: Load the Brain
Read and retain every file listed in the project manifest:
`brain/manifest/project_manifest.json`

## Step 2: Load Governance
Read and retain all records in:
`brain/machine_artifacts/content/governance.json`

## Step 3: Load DevOps
Read and retain all records in:
`brain/machine_artifacts/content/devops.json`

## Step 4: Load Architecture  
Read and retain all records in:
`brain/machine_artifacts/content/architecture.json`

## Step 5: Load All Brain Content
Read every JSON file in:
`brain/machine_artifacts/content/`

## Step 6: Confirm Context
Before writing any code, state back to Boss:
- What environment am I in (dev/qa/prod)
- What are the database naming conventions ({env}_DatabaseName)
- What are the application boundaries (GOV-005)
- What branch am I on and what deploys from it

Do not write code until context is confirmed.
