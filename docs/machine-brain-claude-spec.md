---
title: Machine Brain — Claude Usage Specification
status: ACTIVE
framework: framework_02
created_by: Claude
reviewed_by: pending GPT review
created_at: 2026-03-25
---

# Machine Brain — Claude Usage Specification

## What Machine Brain Is

Machine Brain is the persistent architectural memory of the ChatHealthy system.
It survives session boundaries. It holds decisions, rationale, constraints, and
the narrative context that makes decisions interpretable.

Claude loses all context when a session ends. Machine Brain does not.
Every session Claude operates in is a new mind with no prior memory.
Machine Brain is the bridge.

---

## The Non-Negotiable Rule

> **Query Machine Brain before any non-trivial implementation.**
> If Machine Brain is not queried, it does not exist.

"Non-trivial" means anything that touches system architecture, data persistence,
environment routing, security, compliance, UX flows, testing strategy, DevOps,
or inter-component integration. When in doubt, query.

---

## Query Method — Always Use semantic_search() First

```python
from machine_brain import semantic_search, get_decisions

# Preferred — understands meaning, not just keywords
results = semantic_search("safety gate for emergency healthcare queries")

# Fallback — use for exact ID lookups or when Voyage API is unavailable
results = get_decisions("ADR-0007")
```

`semantic_search()` uses voyage-3-large embeddings via Atlas Vector Search.
It understands that "safety gate" and "emergency response" and "crisis detection"
are the same concern, even if the stored record uses different words.

`get_decisions()` is regex keyword search. Use it when you need a specific
record by ID or when `semantic_search()` is unavailable.

---

## When to Query — Decision Table

| Situation | Query? | What to query |
|---|---|---|
| Starting a new implementation task | YES — always | The feature area (e.g. "chat persistence", "provider search") |
| Adding a new endpoint or API | YES | "API design", "integration patterns", the component name |
| Touching database reads or writes | YES | "database routing", "ENV_PREFIX", the collection name |
| Modifying the safety gate | YES | "safety gate", "crisis detection", "HIPAA" |
| Changing consent flow | YES | "HIPAA consent", "PERSIST_MODE", "data privacy" |
| Changing environment config | YES | "environment routing", "ENV_PREFIX" |
| Modifying a GitHub Actions workflow | YES | "DevOps", "deployment", "CI/CD" |
| Writing or changing tests | YES | "testing strategy", "regression", "QA" |
| Adding a new dependency or package | YES | "technology choices", the dependency name |
| Small bug fix in existing behavior | NO — unless fix touches a boundary | — |
| Formatting, renaming, comments only | NO | — |
| Fixing a typo | NO | — |

---

## How to Query — Practical Patterns

### Starting a session on a feature

Query the full feature area first, then the specific components:

```python
# Example: starting work on the FastAPI backend port
results = semantic_search("FastAPI backend port from Gradio")
results += semantic_search("safety gate healthcare chat")
results += semantic_search("HIPAA consent flow persistence")
```

### Before touching a specific component

Query by component name to retrieve all decisions affecting it:

```python
# What constraints apply to FindCareChat backend?
results = semantic_search("FindCareChat backend constraints")

# Or by keyword
results = get_decisions("FindCareChat backend")
```

### Bootstrapping a new session (first query in any session)

Always start with the full bootstrap narrative to orient the session:

```python
results = get_decisions("MB-0099")  # Full Bootstrap Narrative
```

MB-0099 contains the complete system context: architecture, governance,
compliance, technology decisions, and current priorities. Read it first.

### Checking for conflicts before proposing a design

Before proposing any design that crosses application boundaries or touches
compliance, query the boundary rules:

```python
results = semantic_search("three application topology boundary rules")
results = semantic_search("App 2 App 3 integration pattern")
```

---

## How to Use the Results

### Read the risk level first

Every record has a `risk` field: Low | Moderate | High | Critical | Suicidal.

- **Suicidal**: this decision could kill the company if violated. Do not proceed
  without human approval. Do not rationalize exceptions. Escalate.
- **High**: significant consequences. Require explicit acknowledgment before
  proceeding differently than documented.
- **Moderate**: review constraints carefully. Flag deviations to human or GPT.
- **Low**: informational. Proceed, comply with constraints.

### Read constraints as hard rules

The `constraints` field lists specific rules this decision imposes on
implementation. These are not guidelines — they are requirements.

Example from ADR-0001:
```
"App 2 never writes to PublicHealthData"
"App 3 owns all PublicHealthData writes"
"App 2 to App 3 via HTTP POST /api/Router with Bearer token only"
```

If your implementation would violate a constraint, stop. Raise it with
Human or GPT before proceeding. Do not implement around a constraint silently.

### Read narrative for context

The `narrative` field explains the *why behind the why*. It contains the
reasoning that didn't fit in `rationale` — the history, the tradeoffs,
the things that would not be obvious from the structured fields alone.

For High/Suicidal risk records, always read the narrative before implementing.

### Score field in semantic_search results

`semantic_search()` results include a `score` field (0.0–1.0 cosine similarity).

| Score range | Interpretation |
|---|---|
| > 0.85 | Highly relevant — this decision almost certainly applies |
| 0.70–0.85 | Relevant — review carefully, constraints likely apply |
| 0.55–0.70 | Possibly relevant — skim for applicable constraints |
| < 0.55 | Weak match — probably not directly applicable |

Do not dismiss results with score < 0.85 without reading them. A decision
about "session locking" is highly relevant to "safety gate" even if the
score is 0.72.

---

## When Nothing Comes Back

If `semantic_search()` returns no results above 0.55:

1. Try a broader query ("architecture", "data", "security")
2. Try `get_decisions()` with a keyword that would appear in the record
3. Query MB-0099 (the full bootstrap narrative)
4. If still nothing — the decision has not been recorded yet. That means:
   - You may be in new territory. Proceed carefully.
   - After implementing, write the decision to Machine Brain.
   - Flag to GPT that a new ADR/MB record may be warranted.

---

## After Implementing — Write Back

If you made an implementation decision that has architectural significance,
store it. This is how Machine Brain grows:

```python
from machine_brain import store_decision

store_decision(
    adr_id="ADR-0012",          # or None if not yet an ADR
    topic="rate limiting FastAPI",
    decision="All FastAPI endpoints rate-limited at 60 req/min per IP via SlowAPI.",
    rationale="Prevent abuse on public healthcare endpoints. Azure B1 cannot absorb spike traffic.",
    risk="Moderate",
    created_by="Claude",
    constraints=[
        "Rate limit applied before auth check",
        "Safety gate endpoint exempt from rate limit",
        "/health exempt from rate limit",
    ],
    components=["FindCareChat backend"],
    decision_type="security",
    narrative="Rate limiting is a first-line defense on a public API. The safety gate "
              "is exempt because a user in crisis must never be blocked by a technical control.",
)
```

**What warrants a new Machine Brain record:**
- Any decision that a future Claude session would need to know to implement correctly
- Any decision that crosses application boundaries
- Any constraint that, if missed, would cause a bug or compliance violation
- Any tradeoff where the non-obvious option was chosen

**What does NOT warrant a new record:**
- Implementation details that are obvious from reading the code
- Bug fixes that restore documented behavior
- Style or formatting choices

---

## The Hierarchy of Authority

When Machine Brain and your own reasoning conflict, the hierarchy is:

```
Human (Skip) decision  >  Machine Brain record  >  Claude's reasoning
```

If you believe a Machine Brain record is wrong or outdated, raise it — do not
silently override it. Flag the conflict to human or GPT.

GPT authors the architecture. Claude implements it. Machine Brain is the shared
memory between them. It is not Claude's memory alone — it belongs to all three.

---

## Session Bootstrap Checklist

At the start of any session involving non-trivial implementation:

- [ ] Query `get_decisions("MB-0099")` — read the full bootstrap narrative
- [ ] Query `semantic_search("<feature area>")` — retrieve relevant decisions
- [ ] Review all `risk: High | Critical | Suicidal` results for constraints
- [ ] Check `ADR-0001` (three-application boundaries) if touching any inter-app integration
- [ ] Check `ADR-0009` (PERSIST_MODE) and `ADR-0010` (safety incidents) if touching persistence

---

## Reference

| Record | Topic | Why It Matters Most |
|---|---|---|
| MB-0099 | Full Bootstrap Narrative | Start every session here |
| MB-0000 | North Star Vision | Any product/feature decision |
| MB-0001 | human Governance | Authority, escalation path |
| MB-0002 | Risk Model | Risk levels and what they mean |
| ADR-0001 | Three-App Architecture | Any cross-boundary work |
| ADR-0004 | ENV_PREFIX Routing | Any database or blob access |
| ADR-0006 | Two MongoDB Clusters | Which cluster for which data |
| ADR-0009 | PERSIST_MODE | Any chat persistence logic |
| ADR-0010 | Safety Incidents | Safety gate implementation |
| MB-0013 | Machine Brain Usage Rule | This document's authority |
| MB-0023 | Three-Application Topology | Boundary enforcement |
