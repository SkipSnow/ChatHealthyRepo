# Tonight's Solo Punch List (2026-04-07)

## Mode: Unattended

## Pipeline cluster reservation: active until 05:39 UTC

## Tasks:
1. Run quality pipeline on MS and VA (can_prescribe, Part D drugs, crosswalk, specialty normalization)
2. CopyToFrontEnd — providers, SpecialtyMetaData, provider_quality for DE, MS, CA, VA
3. Verify parity on all three collections across all four states

## Key info:
- Pipeline cluster: mongodb+srv://PipelineUser:Pipeline2026!@chathealthydatapipeline.mdwahg.mongodb.net
- Frontend cluster: mongodb+srv://FrontEndUser:DxWVFaFcSpMv4RRr@chathealthyfrontend.mdwahg.mongodb.net
- States: DE (25,591), VA (182,473), MS (53,993), CA (1,055,397)
- Collections: dev_PublicHealthData.providers, dev_PublicHealthData.SpecialtyMetaData, dev_PublicHealthData.provider_quality
- Code: Code/DataPipelines/crosswalk_builder.py, county_enrichment_job.py, prescriber_load_worker.py
- DR-022: Never embed on frontend cluster
- Env file: Code/.env

## After completion:
- Commit results
- Update daily_punch_list_with_results_and_accomplishments.json
- Pause pipeline cluster if reservation expires
