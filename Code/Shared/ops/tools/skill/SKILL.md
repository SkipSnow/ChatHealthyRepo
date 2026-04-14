---
name: conversation-log-archival
description: Archives conversation log utterances to MongoDB and returns retained records. Use when asked to process conversation log data, archive utterances, or manage the conversation log lifecycle. Deployed via GitHub Actions.
---

# Conversation Log Archival

## Overview

This skill processes conversation_log.json content, archives all utterances to MongoDB, and returns retained records within a 24-hour window.

## How to use

When you receive a message with a JSON payload containing `logContent`, `bearerToken`, `mongoConnectionString`, `preservePastTime`, and `schema`:

1. Write the bundled `conversation_log_agent.py` script to `/tmp/conversation_log_agent.py`
2. Create a runner script that imports and calls `process_conversation_log()` with the provided arguments
3. Execute the runner and capture the JSON output
4. Return the JSON result exactly as produced — do not modify it

## Execution

```bash
cd /tmp && python runner.py
```

The result will be a JSON object with: `status`, `jobId`, `header`, `retained_records`, `counts`, `errors`.

## Important

- Do NOT modify the agent code
- Do NOT summarize or interpret the result — return the raw JSON
- Print the result between `===AGENT_RESULT_START===` and `===AGENT_RESULT_END===` markers
- The mongoConnectionString is sensitive — do not log it
