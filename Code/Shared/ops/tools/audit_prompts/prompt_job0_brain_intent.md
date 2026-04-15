You are analyzing the ChatHealthy.ai brain artifacts to extract every intended capability.

Working directory: c:\chatHealthy\findCare

Read ALL JSON files in brain/machine_artifacts/content/ (there are ~29 files). Also read any .md files in that directory.

For each file, extract capabilities, features, design decisions, and unrealized ideas.

Focus especially on:
- design.json — what was designed
- architecture.json — component boundaries
- business_plan.json — what the product should do
- prompts.json — what prompts were designed
- work_log.json — what was worked on
- daily_punch_list_with_results_and_accomplishments.json — what was planned
- unrealized_ideas.json — ideas logged but not built
- agile_backlog.json — use grep to search for feature names and story titles, do NOT read the whole file

Write your output as valid JSON to: test_output/lineage/00_brain_intent.json

Schema:
{
  "generated_at": "ISO timestamp",
  "source_files_read": ["list of files read"],
  "capabilities": [
    {
      "name": "capability name",
      "source_file": "which brain file",
      "description": "what it does",
      "status": "designed|built|lost|active",
      "evidence": "quote or reference"
    }
  ]
}

Be thorough. Every capability matters. Do not skip files.
