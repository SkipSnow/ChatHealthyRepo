# Work for 4/5/2026

## Housekeeping
1. Fix the .md memory files — consolidate rules into one JSON, clean up the mess
2. DONE: Engineering rules merged into engineering_rules.json (51 rules, v4-034 added)

## Security Plumbing
3. Set up mTLS between FindCare and EvaluateCare — self-signed CA, mutual cert authentication
4. Admin web server on 443 with simple password challenge (shared password for Alpha friends, OAuth later)
5. Separate public (80) and secure (443) content

## Integration
6. FindCare calls EvaluateCare over HTTPS/mTLS with clinical trial data
7. FindCare passes orchestration control to EvaluateCare for scoring
8. List of clinical trials painted on screen (display only, no scores yet)

## Grooming
9. Review the 22 Evaluate Care features — KEEP / DEFER / CUT using the triage doc
10. Identify the 6 measures that ship for Alpha
11. Identify any missing requirements for those 6

## SIT Prep
12. Write Playwright SIT tests for the 6 measures
13. 10-day SIT starts

## Bug
14. Pipeline cluster didn't auto-pause — debug manager timer
