# ChatHealthy — Session Startup Protocol

## Load the Brain
Read and retain the entire brain from both sources:

1. **Disk:** Read every JSON file in `brain/machine_artifacts/content/`
2. **MongoDB:** Query `admin` database on the frontend cluster for all brain collections. MongoDB is the source of truth — if a document exists in both disk and MongoDB, MongoDB wins.

Then ask Boss: "I am in Idiot Mode. Should I change my mode? (1=Unattended, 2=Normal, 3=Idiot Mode)"

Do not act until Boss responds.
