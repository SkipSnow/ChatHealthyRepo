**QA Report: v0.1.2**

| Start Build | End Build | Builds Tested |
|:-----------:|:---------:|:-------------:|
| 100 | 157 | 57 |

| Total Scenarios | Passed | Deferred | Failed | To Test |
|:---------------:|:------:|:--------:|:------:|:-------:|
| 13 | 8 | 5 | 0 | 0 |

**Release Gate:** GPT-4o Enterprise Architect: **conditional-go** | Risk: Moderate | Conditions: Consent framework bugs (Sprint 2), Provider Detail link quality (Sprint 2), Automated tests (Sprint 2)


| &nbsp;#&nbsp; | Feature | &nbsp;Done&nbsp; | Bugs Fixed | Feat Impl |
|:---:|---------|:------:|:----------:|:---------:|
|   1 | Provider Search (DE + MS, vector + regex) | Y | 0 | 0 |
|   2 | Specialty Identification (NUCC + AI expansion) | DEF | 0 | 0 |
|   3 | Clinical Trials Search | Y | 0 | 2 |
|   4 | About ChatHealthy / Skip Snow | Y | 0 | 0 |
|   5 | Safety Filter (dual-trigger, IP lock, audit) | Y | 3 | 2 |
|   6 | Lead Capture (follow-up offer) | Y | 0 | 0 |
|   7 | Consent Framework | DEF | 0 | 0 |

| &nbsp;#&nbsp; | Feature | &nbsp;Done&nbsp; | Bugs Fixed | Feat Impl |
|:---:|---------|:------:|:----------:|:---------:|
|   8 | Provider Detail | DEF | 1 | 1 |
|   9 | URL Guardian (validate + defang broken links) | DEF | 0 | 0 |
|  10 | Chat UX (timer, stop, markdown, emergency) | DEF | 0 | 0 |
|  11 | Blob Storage Infrastructure | Y | 0 | 2 |
|  12 | Unanswerable Question Handling | Y | 3 | 4 |
|  13 | Markdown Table Rendering (GFM tables in chat) | Y | 1 | 11 |
| | **TOTALS** | **13/13** | **8** | **22** |

**Notes:**

- **#2 Specialty Identification (NUCC + AI expansion)**
  - DEFERRED: Sufficiently working for release. Deep testing deferred - too complex for alpha.
- **#3 Clinical Trials Search**
  - **Enhancements (2):**
    - Travel distance + drive time via Google Routes API
    - Two-pass UX: results first, travel on request
- **#4 About ChatHealthy / Skip Snow**
  - **Bugs (1):**
    - Sonnet answers questions outside context instead of saying I don't know. Fix: PreScreen class (Sprint 2)
- **#5 Safety Filter (dual-trigger, IP lock, audit)**
  - **Bugs (3):**
    - False positive on provider name lookup (safe-prefix bypass added)
    - Unlock code visible in audit record (deIdentify added)
    - Triggering message missing from audit history (append current msg)
  - **Enhancements (2):**
    - Switched classifier to GPT-4.1-mini (vendor diversity, 2.5x cheaper)
    - Full de-identified conversation history in safety audit trail
- **#7 Consent Framework**
  - FAIL: Two consent streams needed: Stream 1 (questions) always de-identify. Stream 2 (contact me) user chooses verbatim or de-identify with PHI warning.
  - **Bugs (4):**
    - Sonnet set consent_verbatim=true when user asked for de-identification
    - PII not scrubbed when de-identify was requested
    - Exact consent wording needs legal review before beta
    - No separation between question consent and contact-me consent
- **#8 Provider Detail**
  - FAIL: External link quality unreliable. Full fix deferred.
  - **Bugs (2):**
    - Healthgrades fuzzy match returns wrong provider
    - Zocdoc blocks all third-party links (403)
  - **Enhancements (2):**
    - NPI Registry API lookup for real provider data
    - Repositioned links as research sites with user guidance
- **#9 URL Guardian (validate + defang broken links)**
  - DEFERRED: Link check deferred to next build. V2 design ready (3-stage: HEAD, AI content verify, Google search correction).
- **#10 Chat UX (timer, stop, markdown, emergency)**
  - DEFERRED: Sufficient for v0.1.2. Deep testing deferred.
- **#11 Blob Storage Infrastructure**
  - **Enhancements (2):**
    - Created admin + dev-brain containers
    - Consolidated provider-data into chathealthy-public-data
- **#12 Unanswerable Question Handling**
  - **Bugs (3):**
    - System answered unanswerable questions without recording (fixed: RULE 2 source-bounded knowledge)
    - Responses too verbose (fixed: template responses from tool)
    - Mid-session context caused rule bypass (fixed: evaluate each message independently)
  - **Enhancements (4):**
    - 3-path classification (healthcare capability / medical advice / irrelevant)
    - Template responses — verbatim from tool, no Sonnet elaboration
    - Consent before recording questions
    - Follow-up offer after recording
- **#13 Markdown Table Rendering (GFM tables in chat)**
  - **Bugs (1):**
    - GFM tables not rendering (fixed: remark-gfm + sanitize whitelist)
  - **Enhancements (11):**
    - Bordered table cells
    - Repeating header every 7 rows
    - Notes section
    - DEF/FAIL status in Done column
    - Build number auto-increment
    - Summary counts in header
    - Removed deferred columns
    - Frontend fetches /welcome from API
    - QA Report header with build range + scenario summary
    - Column width fix (nowrap for short columns)
    - Structured notes with bugs/enhancements

Test the features above. Type normally to interact with the chatbot.
