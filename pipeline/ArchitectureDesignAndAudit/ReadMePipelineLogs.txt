Pipeline Runtime Logs
=====================

This blob container (pipeline-logs) is NOT the runtime log surface for
the ChatHealthy provider pipeline. Content here is auxiliary: trigger
runbook stream events written by the Automation runbook, and one-off
debug dumps written by operators.

For real observability into pipeline runs, query MongoDB:

    cluster:    ChatHealthy front cluster
                (connection: kv-chpipeline-dev / MONGO-FRONTEND-connectionString)
    database:   chathealthyfrontend
    collection: logFileCollection

The runtime observability policy is: every pipeline stage that runs
MUST write structured log events to `logFileCollection`. If write to
that collection fails, the stage MUST abend -- silent operation with a
broken observability surface is a fatal error.
